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
import re
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
POST_TIMEOUT = float(os.environ.get("COMFY_POST_TIMEOUT", "60"))
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


def job_judgement(j):
    """ジョブの判定 (resume・--pending・Pod 削除前の確認で共通)。
    返り値: {"generation": running|finished|error|unknown|abandoned, "retrieval": all|partial|none,
            "unretrieved": 未回収枚数, "settled": True なら Pod を消してよい (実行が終わり、完成済み画像をすべて回収済み)}"""
    st = j.get("status")
    imgs = j.get("images") or []
    un = sum(1 for i in imgs if not i.get("retrieved"))
    known = outputs_known(j)
    if st == "abandoned":
        gen = "abandoned"
    elif j.get("gen_status") == "error" or st == "error":
        gen = "error"
    elif j.get("gen_status") == "success" or st == "complete":
        gen = "finished"
    elif st in ("not_found", "send_unknown"):
        gen = "unknown"
    else:
        gen = "running"
    if not known:
        ret = "unknown"  # 出力一覧が未確認: images=[] でも「回収済み」としない
    else:
        ret = "all" if un == 0 else ("none" if un == len(imgs) else "partial")
    settled = gen == "abandoned" or (gen in ("finished", "error") and known and un == 0)
    return {"generation": gen, "retrieval": ret, "unretrieved": un, "outputs_known": known, "settled": settled}


def outputs_known(j):
    """出力一覧が確定しているか。outputs_known フラグがあればそれに従う。
    フラグの無い旧記録: 旧版は出力一覧を1回の保存でまとめて書いてから画像を取っていたので、images が空でなければ確定とみなす。
    images が空の旧記録は「0枚を確認済み」か「一覧の記録前に中断」か区別できないので、未確認として扱う (安全側)。"""
    if "outputs_known" in j:
        return j["outputs_known"] is True
    return bool(j.get("images"))


def confirm_outputs(job_path, job, entry, gen_status, error=None):
    """生成の成否と出力一覧を、1回の保存 (一時ファイル + rename) で確定させる。画像の取得はこの後。"""
    known = {(i["filename"], i.get("subfolder", ""), i.get("type", "output")): i for i in job.get("images", [])}
    for out in ((entry or {}).get("outputs") or {}).values():
        for img in out.get("images", []):
            k = (img["filename"], img.get("subfolder", ""), img.get("type", "output"))
            if k not in known:
                known[k] = {"filename": k[0], "subfolder": k[1], "type": k[2], "retrieved": False}
                job.setdefault("images", []).append(known[k])
    job.setdefault("images", [])
    job.update(gen_status=gen_status, outputs_known=True, outputs_confirmed_at=time.time(),
               status="error" if gen_status == "error" else "running")
    if error is not None:
        job["error"] = error
    save_json(job_path, job)
    if os.environ.get("COMFYLAB_TEST_CRASH") == "after_confirm":  # 試験用: 確定直後の中断を再現
        os._exit(70)


class NetError(Exception):
    """通信の失敗。code は HTTP ステータス (応答が無ければ None)、body は応答本文 (bytes)。"""
    def __init__(self, msg, code=None, body=b""):
        super().__init__(msg)
        self.code, self.body = code, body or b""


# ComfyUI が POST /prompt の入力検証で拒否したときの error.type (このとき prompt は待ち行列に入らない)
VALIDATION_TYPES = {"prompt_outputs_failed_validation", "prompt_no_outputs", "invalid_prompt", "missing_node_type"}
_TRUNC_RE = re.compile(r'^\s*\{\s*"error"\s*:\s*\{\s*"type"\s*:\s*"([a-z_]+)"')
BODY_KEEP = 65536


def classify_submit(code, body):
    """POST /prompt の結果を判定する。
    返り値 (kind, info): kind は accepted (受付済み。info=応答 dict) / rejected (入力検証で拒否が確定。info=error.type) /
    unknown (受理されたか判断できない。info=理由)。400 なら何でも拒否、にはしない。"""
    text = (body or b"").decode("utf-8", "replace")
    try:
        res = json.loads(text) if text.strip() else None
    except ValueError:
        res = None
    if code == 200:
        if isinstance(res, dict) and isinstance(res.get("prompt_id"), str) and res["prompt_id"]:
            return "accepted", res  # node_errors があっても prompt_id があれば受付済み
        return "unknown", "200 だが prompt_id が無い/読めない応答"
    if code == 400:
        if isinstance(res, dict):
            if res.get("prompt_id"):
                return "unknown", "400 なのに prompt_id がある"
            err = res.get("error")
            etype = err.get("type") if isinstance(err, dict) else None
            if etype in VALIDATION_TYPES:
                return "rejected", etype
            return "unknown", f"400 だが入力検証の拒否と確認できない (error.type={etype!r})"
        m = _TRUNC_RE.match(text)  # 途中で切れた本文 (旧版の記録など)。先頭が ComfyUI の拒否の形のときだけ
        if m and m.group(1) in VALIDATION_TYPES:
            return "rejected", m.group(1)
        return "unknown", "400 だが本文を判定できない"
    if code is None:
        return "unknown", "応答なし (タイムアウト・切断)"
    return "unknown", f"HTTP {code}"


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
        try:
            body = e.read()[:BODY_KEEP]
        except OSError:
            body = b""
        raise NetError(f"HTTP {e.code} {method} {url}: {body.decode('utf-8', 'replace')[:500]}", e.code, body)
    except (urllib.error.URLError, OSError) as e:
        raise NetError(f"接続失敗 {method} {url}: {getattr(e, 'reason', e)}")


def summarize_rejection(raw):
    """拒否応答の要点 (どのノードのどの入力か)。"""
    try:
        res = json.loads((raw or b"").decode("utf-8", "replace"))
    except ValueError:
        return (raw or b"").decode("utf-8", "replace")[:300]
    parts = []
    for nid, ne in (res.get("node_errors") or {}).items():
        for er in (ne.get("errors") or [])[:3]:
            parts.append(f"ノード {nid}: {er.get('details') or er.get('message')}")
    return "; ".join(parts) or json.dumps(res.get("error"), ensure_ascii=False)[:300]


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


def save_name(job, rec):
    """保存名。サブフォルダ・種別も名前に入れ、別フォルダの同名画像が同じ保存先にならないようにする。"""
    parts = [job["tag"]]
    if rec.get("type", "output") != "output":
        parts.append(rec["type"])
    sub = (rec.get("subfolder") or "").strip("/\\")
    if sub:
        parts.append(sub.replace("/", "_").replace("\\", "_"))
    return "_".join(parts + [os.path.basename(rec["filename"])])


def fetch_images(base, job_path, job, final_status=None):
    """ジョブ記録の出力一覧 (confirm_outputs で確定済み) から未回収の画像を取る。"""
    out_dir = os.path.dirname(os.path.dirname(job_path))
    used = {i.get("saved_path") for i in job.get("images", []) if i.get("saved_path")}
    for rec in job.get("images", []):
        if rec.get("retrieved"):
            continue
        q = urllib.parse.urlencode({"filename": rec["filename"], "subfolder": rec.get("subfolder", ""), "type": rec.get("type", "output")})
        blob = http("GET", f"{base}/view?{q}", timeout=300)
        path = os.path.join(out_dir, save_name(job, rec))
        stem, ext = os.path.splitext(path)
        n = 2
        while os.path.abspath(path) in used:  # 念のため: 同じ保存先は使わない
            path = f"{stem}-{n}{ext}"; n += 1
        used.add(os.path.abspath(path))
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, path)
        rec.update(retrieved=True, saved_path=os.path.abspath(path), sha256=hashlib.sha256(blob).hexdigest(), bytes=len(blob), retrieved_at=time.time())
        save_json(job_path, job)  # 1枚ごとに記録
        print(path)
    done = outputs_known(job) and all(i.get("retrieved") for i in job.get("images", []))
    if job.get("gen_status") == "error":
        final_status = "error"  # 履歴消失後の回収でも生成エラーの記録を消さない
    job["status"] = final_status or ("complete" if done else "partial")
    save_json(job_path, job)


def wait_and_fetch(base, job_path, job, timeout, interval):
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = history(base, job["prompt_id"])
        if entry:
            st = entry.get("status") or {}
            if st.get("status_str") == "error":
                confirm_outputs(job_path, job, entry, "error", json.dumps(st.get("messages", []), ensure_ascii=False)[:2000])
                if job["images"]:  # エラーでも完成済みの出力は回収する
                    fetch_images(base, job_path, job, final_status="error")
                    print(f"  実行エラー。完成済みの {sum(1 for i in job['images'] if i.get('retrieved'))} 枚は回収した")
                raise SystemExit(f"ComfyUI 実行エラー ({job_path})")
            if entry.get("outputs") or st.get("completed"):
                confirm_outputs(job_path, job, entry, job.get("gen_status") if job.get("gen_status") == "error" else "success")
                return fetch_images(base, job_path, job)
        time.sleep(interval)
    raise NetError(f"待ち時間切れ prompt_id={job['prompt_id']}")


def resume(args, base):
    rc = 0
    for jp in job_files(args.resume):
        job = load_json(jp)
        if not job or job_judgement(job)["settled"]:
            continue
        print(f"[resume] {jp} 状態={job['status']}")
        try:
            entry = history(base, job["prompt_id"])
            if entry:
                try:
                    wait_and_fetch(base, jp, job, args.timeout, args.poll_interval)
                except SystemExit as e:  # 生成エラー: 完成分は回収済み。記録は残して次のジョブへ
                    print(f"  {e}")
            elif in_queue(base, job["prompt_id"]):
                print("  まだ待ち行列にある。待って回収")
                wait_and_fetch(base, jp, job, args.timeout, args.poll_interval)
            elif any(not i.get("retrieved") for i in job.get("images", [])):
                print("  ComfyUI の履歴は無いが、ジョブ記録に画像の取得先が残っている。回収を試す")
                fetch_images(base, jp, job)
            else:
                job["status"] = "not_found"
                save_json(jp, job)
                print("  ComfyUI の履歴にも待ち行列にも見当たらない。送信されなかったのか、ComfyUI の再起動や Pod の交代で履歴が消えたのかは"
                      "ここからは判断できない。新しく生成はしない。必要なら利用者に確認して作り直す")
                rc = 2
        except NetError as e:
            print(f"  通信失敗: {e}。後で --resume を再実行")
            rc = 1
        job = load_json(jp) or job
        jj = job_judgement(job)
        if jj["generation"] == "error":
            print(f"  生成エラーの記録あり: 回収={jj['retrieval']} 未回収 {jj['unretrieved']}" + (" (Pod 削除の妨げにならない)" if jj["settled"] else ""))
        if not jj["settled"] and rc == 0:
            rc = 1
    return rc


def pending(target):
    n = 0
    for jp in job_files(target):
        j = load_json(jp) or {}
        jj = job_judgement(j)
        if not jj["settled"]:
            n += 1
            print(f"{jp}\t生成={jj['generation']}\t回収={jj['retrieval']}\t未回収 {jj['unretrieved']}")
        elif jj["generation"] == "error":
            print(f"{jp}\t生成=error (記録あり・完成分は回収済み。Pod 削除の妨げにならない)")
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
        payload = json.dumps({"prompt": wf, "client_id": client_id, "prompt_id": job["prompt_id"]}).encode()
        try:
            raw, code = http("POST", f"{base}/prompt", payload, timeout=POST_TIMEOUT), 200
        except NetError as e:
            raw, code, neterr = e.body, e.code, e
        else:
            neterr = None
        kind, info = classify_submit(code, raw)
        job["submit_http"] = {"status": code, "body": (raw or b"").decode("utf-8", "replace")[:BODY_KEEP], "judgement": kind,
                              "at": time.time()}
        if kind == "rejected":
            # 入力検証で拒否 = 待ち行列に入っていない。出力0枚が確定した失敗として1回の保存で記録する
            job.update(status="rejected", gen_status="error", accepted=False, outputs_known=True, images=[],
                       error=f"ComfyUI が入力を拒否 ({info})", rejected_at=time.time())
            save_json(jp, job)
            print(f"[{i + 1}/{args.count}] ComfyUI が入力を拒否しました ({info})。生成されていません (待ち行列に入っていない)。記録 {jp}", file=sys.stderr)
            print("  内容: " + summarize_rejection(raw), file=sys.stderr)
            print("  このジョブは終了扱い (出力0枚が確定)。--pending や Pod の削除は止めない。設定を直して作り直す", file=sys.stderr)
            rc = 1
            break  # 同じ Workflow の残りは送らない
        if kind == "unknown":
            job["status"] = "send_unknown"
            job["last_error"] = (str(neterr) if neterr else info)[:500]
            save_json(jp, job)
            print(f"[{i + 1}/{args.count}] 受理されたか不明: {info}。自動では再送しない。回復後に --resume {args.out}"
                  " (ComfyUI の履歴と待ち行列を自分の prompt_id で確認する)", file=sys.stderr)
            rc = 1
            break  # 送れたか不明のまま次を送らない
        res = info
        job["accepted"] = True
        try:
            if res.get("node_errors"):
                job["node_errors_on_accept"] = json.dumps(res["node_errors"], ensure_ascii=False)[:2000]  # 受付済み。記録だけ残す
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
