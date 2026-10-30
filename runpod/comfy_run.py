#!/usr/bin/env python3
"""ComfyUI API クライアント (標準ライブラリのみ)。ジョブごとに記録し、通信が切れても --resume で未回収分だけ取る。
COMFY_BASE_URL 既定 http://127.0.0.1:18188 (pod_ssh.py tunnel-start の SSH トンネル)。

使い方: comfy_run.py [--dry-run] --set 6.text="..." --seed fixed:N --out ../out/<作品>/<条件> --note "..."
        comfy_run.py --resume <jobファイル|フォルダ> | --pending | --abandon <jobファイル>
"""
import argparse
import copy
import glob
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_WORKFLOW = os.path.join(HERE, "workflow-api.base.json")
DEFAULT_BASE = "http://127.0.0.1:18188"
PLACEHOLDER_LORA = "example.safetensors"
UA = "comfy-lab-comfy_run/3.0"


def load_json(p, d=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return d


def save_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


CFG = load_json(os.path.join(ROOT, "config.json"), {}) or {}


class NetError(Exception):
    pass


def parse_value(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def apply_override(wf, spec, allow_new_input=False):
    if "=" not in spec:
        sys.exit(f"--set は NODE.INPUT=VALUE 形式: {spec!r}")
    key, raw = spec.split("=", 1)
    if "." not in key:
        sys.exit(f"--set のキーは NODE.INPUT 形式: {key!r}")
    node_id, name = key.split(".", 1)
    if node_id not in wf:
        sys.exit(f"ノード {node_id} がない (あるノード: {', '.join(sorted(wf, key=int))})")
    inputs = wf[node_id].setdefault("inputs", {})
    if name not in inputs and not allow_new_input:
        sys.exit(f"ノード {node_id} に入力 {name!r} がない (ある入力: {', '.join(inputs)})")
    inputs[name] = raw if isinstance(inputs.get(name), str) else parse_value(raw)


def seed_sequence(mode, count):
    name, _, arg = mode.partition(":")
    if name == "fixed" and arg:
        return [int(arg)] * count
    if name == "random":
        return [random.randint(0, 2**53 - 1) for _ in range(count)]
    if name == "increment" and arg:
        return [int(arg) + i for i in range(count)]
    sys.exit(f"--seed は fixed:N / random / increment:N: {mode!r}")


def http(method, url, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise NetError(f"HTTP {e.code} {method} {url}: {e.read().decode('utf-8', 'replace')[:500]}")
    except (urllib.error.URLError, OSError) as e:
        raise NetError(f"接続失敗 {method} {url}: {getattr(e, 'reason', e)}")


def history(base, pid):
    return json.loads(http("GET", f"{base}/history/{urllib.parse.quote(pid)}")).get(pid)


def in_queue(base, pid):
    q = json.loads(http("GET", f"{base}/queue"))
    for k in ("queue_running", "queue_pending"):
        for item in q.get(k, []):
            if isinstance(item, list) and len(item) > 1 and item[1] == pid:
                return True
    return False


def job_files(target):
    if os.path.isfile(target):
        return [target]
    return sorted(f for f in glob.glob(os.path.join(target, "**", "jobs", "*.json"), recursive=True) + glob.glob(os.path.join(target, "jobs", "*.json"))
                  if not f.endswith(".workflow.json"))


def fetch_images(base, job_path, job, entry):
    out_dir = os.path.dirname(os.path.dirname(job_path))
    known = {(i["filename"], i.get("subfolder", ""), i.get("type", "output")): i for i in job.get("images", [])}
    for out in (entry.get("outputs") or {}).values():
        for img in out.get("images", []):
            k = (img["filename"], img.get("subfolder", ""), img.get("type", "output"))
            if k not in known:
                known[k] = {"filename": k[0], "subfolder": k[1], "type": k[2], "retrieved": False}
                job.setdefault("images", []).append(known[k])
    save_json(job_path, job)
    for rec in job["images"]:
        if rec.get("retrieved"):
            continue
        q = urllib.parse.urlencode({"filename": rec["filename"], "subfolder": rec["subfolder"], "type": rec["type"]})
        blob = http("GET", f"{base}/view?{q}", timeout=300)
        name = f"{job['tag']}_{rec['filename']}"
        path = os.path.join(out_dir, name)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        rec.update(retrieved=True, saved_path=os.path.abspath(path), sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob), retrieved_at=time.time())
        save_json(job_path, job)  # 1枚ごとに記録
        print(path)
    job["status"] = "complete" if job["images"] and all(i.get("retrieved") for i in job["images"]) else "partial"
    save_json(job_path, job)


def wait_and_fetch(base, job_path, job, timeout, interval):
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = history(base, job["prompt_id"])
        if entry:
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                job.update(status="error", error=json.dumps(st.get("messages", []), ensure_ascii=False)[:2000])
                save_json(job_path, job)
                raise SystemExit(f"ComfyUI 実行エラー ({job_path})")
            if entry.get("outputs") or st.get("completed"):
                job["status"] = "running"
                save_json(job_path, job)
                return fetch_images(base, job_path, job, entry)
        time.sleep(interval)
    raise NetError(f"待ち時間切れ prompt_id={job['prompt_id']}")


def resume(args, base):
    rc = 0
    for jp in job_files(args.resume):
        job = load_json(jp)
        if not job or job.get("status") in ("complete", "abandoned"):
            continue
        print(f"[resume] {jp} 状態={job['status']}")
        try:
            entry = history(base, job["prompt_id"])
            if entry:
                wait_and_fetch(base, jp, job, args.timeout, args.poll_interval)
            elif in_queue(base, job["prompt_id"]):
                print("  まだ待ち行列にある。待って回収")
                wait_and_fetch(base, jp, job, args.timeout, args.poll_interval)
            else:
                job["status"] = "not_found"
                save_json(jp, job)
                print("  ComfyUI に記録が無い (送信されていない/Pod が替わった)。新しく生成はしない。必要なら利用者に確認して作り直す")
                rc = 2
        except NetError as e:
            print(f"  通信失敗: {e}。後で --resume を再実行")
            rc = 1
    return rc


def pending(target):
    n = 0
    for jp in job_files(target):
        j = load_json(jp) or {}
        if j.get("status") not in ("complete", "abandoned"):
            n += 1
            print(f"{jp}\t{j.get('status')}\t未回収 {sum(1 for i in j.get('images', []) if not i.get('retrieved'))}")
    if not n:
        print("未完了・未回収のジョブなし")
    return 1 if n else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    ap.add_argument("--base-url", default=os.environ.get("COMFY_BASE_URL") or DEFAULT_BASE)
    ap.add_argument("--pod-id", default=os.environ.get("COMFY_POD_ID", ""), help="生成した Pod (削除前の未回収確認に使う)")
    ap.add_argument("--set", action="append", default=[], metavar="NODE.INPUT=VALUE")
    ap.add_argument("--allow-new-input", action="store_true")
    ap.add_argument("--seed", default=None)
    ap.add_argument("--seed-target", default="3.seed")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "misc"))
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--poll-interval", type=float, default=2.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--note", default="")
    ap.add_argument("--parent", default="")
    ap.add_argument("--allow-placeholder", action="store_true")
    ap.add_argument("--resume", metavar="JOB_OR_DIR")
    ap.add_argument("--pending", nargs="?", const=os.path.join(ROOT, "out"), metavar="DIR")
    ap.add_argument("--abandon", metavar="JOB")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if args.pending:
        sys.exit(pending(args.pending))
    if args.abandon:
        j = load_json(args.abandon)
        j["status"] = "abandoned"
        j["abandoned_at"] = time.time()
        save_json(args.abandon, j)
        print("abandoned:", args.abandon)
        return
    if args.resume:
        sys.exit(resume(args, base))
    if args.count < 1:
        sys.exit("--count は1以上")

    base_wf = load_json(args.workflow)
    if base_wf is None:
        sys.exit(f"workflow を読めない: {args.workflow}")
    for node in base_wf.values():
        for k, v in node.get("inputs", {}).items():
            if v in ("{checkpoint}", "{vae}"):
                name = CFG.get(v.strip("{}"))
                if not name:
                    sys.exit(f"config.json に {v.strip('{}')} が未設定")
                node["inputs"][k] = name
    for spec in args.set:
        apply_override(base_wf, spec, args.allow_new_input)
    if not args.allow_placeholder:
        for nid, node in base_wf.items():
            if node.get("inputs", {}).get("lora_name") == PLACEHOLDER_LORA:
                sys.exit(f"ノード {nid} の lora_name がプレースホルダのまま (--set {nid}.lora_name=ファイル名)")

    seeds = seed_sequence(args.seed, args.count) if args.seed else [None] * args.count
    batch = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    jobs = []
    for i, sd in enumerate(seeds):
        wf = copy.deepcopy(base_wf)
        if sd is not None:
            apply_override(wf, f"{args.seed_target}={sd}")
        jobs.append((i, sd, wf))
    if args.dry_run:
        for i, sd, wf in jobs:
            print(f"# job {i + 1}/{args.count} seed={sd}", file=sys.stderr)
            print(json.dumps(wf, indent=2, ensure_ascii=False))
        print("# dry-run: 何も送信していません", file=sys.stderr)
        return

    jdir = os.path.join(args.out, "jobs")
    os.makedirs(jdir, exist_ok=True)
    client_id = str(uuid.uuid4())
    rc = 0
    for i, sd, wf in jobs:
        job_id = f"{batch}-{i + 1:02d}"
        jp = os.path.join(jdir, job_id + ".json")
        wp = os.path.join(jdir, job_id + ".workflow.json")
        save_json(wp, wf)
        job = {"job_id": job_id, "batch": batch, "index": i + 1, "count": args.count, "status": "prepared",
               "tag": f"{batch}-{i + 1:02d}" + (f"_seed{sd}" if sd is not None else ""),
               "prompt_id": str(uuid.uuid4()), "client_id": client_id, "pod_id": args.pod_id, "base_url": base,
               "seed": sd, "seed_mode": args.seed, "note": args.note, "parent": args.parent,
               "workflow_source": os.path.abspath(args.workflow), "workflow_sent": os.path.abspath(wp),
               "set": args.set, "created": time.time(), "images": []}
        save_json(jp, job)  # 送信前に記録
        try:
            res = json.loads(http("POST", f"{base}/prompt", json.dumps({"prompt": wf, "client_id": client_id, "prompt_id": job["prompt_id"]}).encode()))
            if res.get("node_errors"):
                job.update(status="error", error=json.dumps(res["node_errors"], ensure_ascii=False)[:2000])
                save_json(jp, job)
                sys.exit(f"node_errors ({jp})")
            if res.get("prompt_id") and res["prompt_id"] != job["prompt_id"]:
                job["prompt_id_requested"] = job["prompt_id"]
                job["prompt_id"] = res["prompt_id"]  # 古い ComfyUI は自分で採番する
            job.update(status="queued", queued_at=time.time())
            save_json(jp, job)  # 受領直後
            print(f"[{i + 1}/{args.count}] queued {job['prompt_id']} seed={sd}  記録 {jp}", file=sys.stderr)
            wait_and_fetch(base, jp, job, args.timeout, args.poll_interval)
        except NetError as e:
            job["status"] = job["status"] if job["status"] != "prepared" else "send_unknown"
            job["last_error"] = str(e)[:500]
            save_json(jp, job)
            print(f"[{i + 1}/{args.count}] 通信失敗: {e}\n  新しく生成はしない。回復後に --resume {args.out}", file=sys.stderr)
            rc = 1
            if job["status"] == "send_unknown":
                break  # 送れたか不明のまま次を送らない
    sys.exit(rc)


if __name__ == "__main__":
    main()
