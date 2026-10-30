#!/usr/bin/env python3
"""comfy-lab の Runpod Pod 管理 (標準ライブラリのみ・REST API v2、残高だけ GraphQL)。
キーは環境変数 RUNPOD_API_KEY (表示・保存しない)。操作するのは ../state/pods.json に記録した管理対象 Pod だけ。

使い方: runpod_pod.py {create|list|status|wait|stop|delete|adopt|reconcile|watchdog|balance|volumes|record-spend|set-budget} ...
  create は見積のみ、--yes で作成 (--max-minutes の期限で自動削除)。delete は未回収ジョブがあれば止める。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = os.environ.get("RUNPOD_API_BASE", "https://api.runpod.io/v2").rstrip("/")
GQL = os.environ.get("RUNPOD_GQL_BASE", "https://api.runpod.io/graphql")
UA = "comfy-lab-runpod_pod/3.0"
DEFAULT_IMAGE = "runpod/comfyui:cuda12.8"
# ComfyUI (8188) と FileBrowser (8080) は公開しない。ComfyUI は SSH トンネル (pod_ssh.py tunnel) で使う。
DEFAULT_PORTS = ["22/tcp"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_JSON = os.path.join(ROOT, "model-map", "models.json")
STATE = os.path.join(ROOT, "state", "pods.json")
WD_PID = os.path.join(ROOT, "state", "watchdog.pid")
WD_LOG = os.path.join(ROOT, "state", "watchdog.log")
OUT = os.path.join(ROOT, "out")
NAME_PREFIX = "comfylab-"
RECONCILE_TRIES = int(os.environ.get("COMFYLAB_RECONCILE_TRIES", "6"))
RECONCILE_INTERVAL = float(os.environ.get("COMFYLAB_RECONCILE_INTERVAL", "10"))
AMBIGUOUS = (0, 408, 500, 502, 503, 504)
NO_STOCK_RE = re.compile(r"no (longer )?(any )?instances|currently available|not enough|insufficient|no available|out of stock|capacity", re.I)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


CFG = load_json(os.path.join(ROOT, "config.json"), {})


def budget():
    """(total, used, remaining) を返す。予算が未設定なら None。remaining は負 (超過) もありうる。"""
    b = load_json(MODELS_JSON, {}).get("budget")
    if not isinstance(b, dict) or b.get("total_usd") in (None, "") or float(b.get("total_usd") or 0) <= 0:
        return None
    tot, used = float(b["total_usd"]), float(b.get("used_usd") or 0)
    return tot, used, round(tot - used, 6)


def update_budget(fn):
    m = load_json(MODELS_JSON, None)
    if m is None:
        sys.exit(f"{MODELS_JSON} がない (comfy-lab-install-dashboard を先に)")
    b = m.setdefault("budget", {})
    b.setdefault("total_usd", 0)
    b.setdefault("used_usd", 0)
    fn(b)
    m["updated"] = time.strftime("%Y-%m-%d %H:%M")
    save_json(MODELS_JSON, m)
    return b


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code, self.msg = code, msg


def _key():
    k = os.environ.get("RUNPOD_API_KEY")
    if not k:
        sys.exit("環境変数 RUNPOD_API_KEY が未設定 (秘密入力で設定する。値は表示しない)")
    return k


def api(method, path, body=None, timeout=60):
    key = _key()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8", "replace").replace(key, "***")[:600])
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise ApiError(0, f"接続失敗/応答なし: {getattr(e, 'reason', e)}")


def api_or_exit(method, path, body=None):
    try:
        return api(method, path, body)
    except ApiError as e:
        sys.exit(f"[API] {method} {path}: {e}")


def gql(query):
    key = _key()
    req = urllib.request.Request(GQL, data=json.dumps({"query": query}).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"GraphQL 失敗: {getattr(e, 'code', '')} {getattr(e, 'reason', e)}")
    if d.get("errors"):
        sys.exit("GraphQL エラー: " + json.dumps(d["errors"], ensure_ascii=False).replace(key, "***")[:300])
    return d["data"]


def list_all_pods():
    pods, cur = [], None
    while True:
        q = "/pods?limit=1000" + (f"&cursor={urllib.parse.quote(cur)}" if cur else "")
        d = api("GET", q)
        pods += d.get("pods") or []
        pg = d.get("pagination") or {}
        if not pg.get("hasNextPage") or not pg.get("nextCursor"):
            return pods
        cur = pg["nextCursor"]


def registry():
    st = load_json(STATE, {})
    if "pods" not in st:  # v2 以前の形式 {podid: {...}} を移行
        st = {"pods": {k: dict(v, status="active", created_by="legacy") for k, v in st.items() if isinstance(v, dict)}, "pending": {}}
    st.setdefault("pending", {})
    return st


def active_managed(st=None):
    st = st or registry()
    return {k: v for k, v in st["pods"].items() if v.get("status") == "active"}


def register(st, pod, tag, gpu, max_minutes, created_by):
    now = time.time()
    st["pods"][pod["id"]] = {
        "name": pod.get("name"), "tag": tag, "gpu": gpu, "cost_per_hr": pod.get("cost"),
        "created": now, "deadline": now + max_minutes * 60, "max_minutes": max_minutes,
        "status": "active", "created_by": created_by}
    st["pending"].pop(tag, None)
    save_json(STATE, st)
    set_running(pod["id"], st["pods"][pod["id"]])


def set_running(pod_id, rec):
    """管理画面用: 稼働中の見込額を出すため models.json の budget.running に単価と開始時刻を置く。"""
    def f(b):
        run = {k: v for k, v in (b.get("running") or {}).items()}
        if rec is None:
            run.pop(pod_id, None)
        else:
            run[pod_id] = {"cost_per_hr": rec.get("cost_per_hr"), "started": rec["created"], "deadline": rec["deadline"], "gpu": rec.get("gpu")}
        b["running"] = run
    if os.path.exists(MODELS_JSON):
        update_budget(f)


def find_by_name(pods, name):
    return [p for p in pods if p.get("name") == name]


def unfinished_jobs(pod_id=None):
    bad = []
    for f in glob.glob(os.path.join(OUT, "**", "jobs", "*.json"), recursive=True):
        if f.endswith(".workflow.json"):
            continue
        j = load_json(f, None)
        if not isinstance(j, dict) or j.get("status") in ("complete", "abandoned"):
            continue
        if pod_id and j.get("pod_id") not in (None, "", pod_id):
            continue
        bad.append((f, j.get("status"), sum(1 for i in j.get("images", []) if not i.get("retrieved"))))
    return bad


def build_body(a, gpu_id, name):
    env = {}
    key = os.path.expanduser(CFG.get("ssh_key") or "")
    if key and os.path.exists(key + ".pub"):
        with open(key + ".pub") as f:
            env["PUBLIC_KEY"] = f.read().strip()
    elif os.environ.get("PUBLIC_KEY"):
        env["PUBLIC_KEY"] = os.environ["PUBLIC_KEY"]
    if not env.get("PUBLIC_KEY"):
        sys.exit("SSH 公開鍵がない (config.json の ssh_key と .pub を確認。comfy-lab-install-generate の確認手順で作る)")
    body = {"name": name, "image": a.image, "cloud": a.cloud_type, "gpu": {"id": gpu_id, "count": a.gpu_count},
            "disk": a.container_disk_gb, "ports": a.ports, "env": env}
    if a.network_volume_id:
        if not a.data_center_id:
            sys.exit("network_volume_id には data_center_id も必要 (ボリュームは作成DCにしか付かない)")
        body["mounts"] = {"network": [{"volumeId": a.network_volume_id, "path": "/workspace"}]}
    else:
        body["mounts"] = {"persistent": {"size": a.volume_gb, "path": "/workspace"}}
    if a.data_center_id:
        body["dataCenterIds"] = [a.data_center_id]
    return body


def resolve_candidates(a, names):
    gpus = {g["id"]: g for g in api_or_exit("GET", "/catalog/gpus").get("gpus", [])}
    avail = {}
    if a.data_center_id:
        try:
            q = "/catalog/gpus?include=AVAILABILITY&product=POD&cloud=" + a.cloud_type
            for g in api("GET", q).get("gpus", []):
                avail[g["id"]] = {d["id"]: d.get("availability") for d in (g.get("dataCenters") or [])}
        except ApiError as e:
            print(f"  (在庫表示の取得に失敗: {e.code}。在庫は作成時に判定)")
    ok = []
    print("--- GPU候補の確認 (優先順。単価は catalog の一覧価格) ---")
    for n in names:
        g = gpus.get(n)
        if not g:
            print(f"  除外  {n}: Runpod の GPU 種別IDに無い")
            continue
        offered = g.get("secure") if a.cloud_type == "SECURE" else g.get("community")
        price = (g.get("price") or {}).get("secure" if a.cloud_type == "SECURE" else "community")
        if not offered or not price:
            print(f"  除外  {n}: {a.cloud_type} では提供なし")
            continue
        total = float(price) * a.gpu_count
        if total > a.max_hourly_usd + 1e-9:
            print(f"  除外  {n}: ${total:.2f}/h > 上限 ${a.max_hourly_usd:.2f}/h")
            continue
        stock = "?"
        if a.data_center_id and avail:
            per = avail.get(n, {})
            if a.data_center_id not in per:
                print(f"  除外  {n}: {a.data_center_id} では提供されていない/在庫なし (ボリュームはDC固定)")
                continue
            stock = per[a.data_center_id]
        print(f"  候補  {n} ({g.get('memory')}GB): ${total:.2f}/h  在庫表示={stock}")
        ok.append((n, total))
    return ok


def reconcile_name(name, tries=RECONCILE_TRIES, interval=RECONCILE_INTERVAL):
    """作成要求の結果が不明なとき、一意の名前で Pod 一覧を照合する。見つかれば Pod、無ければ None。"""
    for i in range(tries):
        try:
            hit = find_by_name(list_all_pods(), name)
            if hit:
                return hit[0]
            print(f"  照合 {i + 1}/{tries}: {name} はまだ一覧に無い")
        except ApiError as e:
            print(f"  照合 {i + 1}/{tries}: 一覧取得に失敗 ({e.code})")
        if i + 1 < tries:
            time.sleep(interval)
    return None


def cmd_create(a):
    b = budget()
    if b is None:
        print("[中止] 予算が未設定 (models.json の budget.total_usd)。承認額を set-budget --total で記録してから。")
        return 4
    tot, used, rem = b
    if a.hours <= 0 or a.max_minutes <= 0 or a.gpu_count < 1 or a.max_hourly_usd <= 0:
        sys.exit("--hours / --max-minutes / --max-hourly-usd は正、--gpu-count は1以上")
    if a.hours * 60 > a.max_minutes + 1e-9:
        print(f"[中止] 想定 {a.hours}h が停止期限 {a.max_minutes}分 を超えている。--max-minutes を見直す。")
        return 3
    st = registry()
    act = active_managed(st)
    if act:
        print(f"[中止] 管理対象の Pod が既にある: {', '.join(act)} (1台まで。使うか delete する)")
        return 5
    if st["pending"]:
        print(f"[中止] 作成結果が未確認の要求がある: {', '.join(st['pending'])}。先に reconcile を実行。")
        return 5
    names = a.gpu_type or list(CFG.get("gpu_candidates") or [])
    if not names:
        sys.exit("GPU 候補が無い (config.json の gpu_candidates か --gpu-type)")
    cands = resolve_candidates(a, names)
    if not cands:
        print("[中止] 使える候補が0件")
        return 3
    worst = max(p for _, p in cands)
    est, cap = worst * a.hours, worst * a.max_minutes / 60
    print("--- 見積 (GPU料金のみ。ディスク/ボリューム料金は含まない) ---")
    print(f"試行順: {' -> '.join(n for n, _ in cands)}   単価上限 ${a.max_hourly_usd:.2f}/h")
    print(f"想定 {a.hours}h: 最大 ${est:.3f}   停止期限 {a.max_minutes:.0f}分で自動削除: 上限 ${cap:.3f}")
    print(f"予算: 総額 ${tot:.2f} / 使用 ${used:.2f} / 残り ${rem:.2f}")
    if cap > rem + 1e-9:
        print(f"[中止] 停止期限までの上限額 ${cap:.3f} が予算の残り ${rem:.2f} を超える。--max-minutes を減らすか予算の承認を取り直す。")
        return 3
    if a.dry_run or not a.yes:
        print("[作成しません] " + ("dry-run" if a.dry_run else "作成するには承認後に --yes"))
        return 0 if a.dry_run else 2

    deadline_wait = time.time() + a.max_wait_min * 60
    sweep = 0
    while True:
        sweep += 1
        for n, price in cands:
            tag = uuid.uuid4().hex[:10]
            name = NAME_PREFIX + tag
            st["pending"][tag] = {"name": name, "gpu": n, "requested": time.time(), "max_minutes": a.max_minutes}
            save_json(STATE, st)  # 要求前に記録 (応答を失っても照合できる)
            try:
                pod = api("POST", "/pods", build_body(a, n, name), timeout=90)
            except ApiError as e:
                if e.code in AMBIGUOUS:
                    print(f"[巡回{sweep}] {n}: 作成応答なし/一時エラー ({e.code})。作成されたか照合する (次の候補へは進まない)")
                    pod = reconcile_name(name)
                    if not pod:
                        print(f"[中止] {name} は照合時間内に見つからない。作成されたか不明なので次の候補は作らない。"
                              f" 記録を残した (pending)。数分後に reconcile を実行し、出てきたら登録・削除の判断をする。")
                        if not a.no_watchdog:
                            spawn_watchdog()
                        return 6
                    print(f"  照合で見つかった: {pod['id']} (二重作成なし)")
                else:
                    st["pending"].pop(tag, None)
                    save_json(STATE, st)
                    if NO_STOCK_RE.search(e.msg or "") or e.code == 429:
                        print(f"[巡回{sweep}] {n}: 在庫なし/混雑 ({e.code}) -> 次へ")
                        continue
                    print(f"[中止] {n}: {e}")
                    return 1
            register(st, pod, tag, n, a.max_minutes, "create")
            cost = pod.get("cost")
            print(f"作成: id={pod['id']} GPU={n} 単価={cost} 期限={time.strftime('%H:%M', time.localtime(st['pods'][pod['id']]['deadline']))}")
            if cost is None:
                try:
                    cost = api("GET", f"/pods/{pod['id']}").get("cost")
                except ApiError:
                    pass
            if cost is None or float(cost) > a.max_hourly_usd + 1e-9:
                print(f"[中止] 実単価 {cost} が上限 ${a.max_hourly_usd:.2f}/h を超える/不明。この Pod はすぐ削除する。")
                a2 = argparse.Namespace(pod_id=pod["id"], allow_unretrieved=True)
                cmd_delete(a2)
                return 7
            st["pods"][pod["id"]]["cost_per_hr"] = float(cost)
            save_json(STATE, st)
            set_running(pod["id"], st["pods"][pod["id"]])
            if not a.no_watchdog:
                spawn_watchdog()
            print(f"次: python3 {os.path.basename(__file__)} wait {pod['id']}")
            return 0
        if time.time() + a.retry_interval_sec > deadline_wait:
            print(f"[中止] {a.max_wait_min}分以内に在庫が取れなかった。Pod は作成していない。")
            return 3
        print(f"[巡回{sweep}] 全候補が在庫なし。{a.retry_interval_sec}s 後に再巡回")
        time.sleep(a.retry_interval_sec)


def do_reconcile(st, pods):
    changed = []
    for tag, p in list(st["pending"].items()):
        hit = find_by_name(pods, p["name"])
        if hit:
            pod = hit[0]
            register(st, pod, tag, p.get("gpu"), p.get("max_minutes", 60), "reconcile")
            # 期限は要求時刻から数える (応答を失った間も時間は進んでいる)
            st["pods"][pod["id"]]["created"] = p["requested"]
            st["pods"][pod["id"]]["deadline"] = p["requested"] + p.get("max_minutes", 60) * 60
            save_json(STATE, st)
            set_running(pod["id"], st["pods"][pod["id"]])
            changed.append(pod["id"])
        elif time.time() - p["requested"] > 3600:
            st["pending"].pop(tag)  # 1時間出てこない要求は作成されなかったと判断
            save_json(STATE, st)
    return changed


def cmd_reconcile(a):
    st = registry()
    try:
        pods = list_all_pods()
    except ApiError as e:
        sys.exit(f"一覧取得に失敗: {e}")
    ch = do_reconcile(st, pods)
    print("登録: " + (", ".join(ch) if ch else "なし") + f" / 未確認の要求: {len(st['pending'])}件")
    return 0


def cmd_list(a):
    st = registry()
    try:
        pods = list_all_pods()
    except ApiError as e:
        sys.exit(f"一覧取得に失敗: {e}")
    do_reconcile(st, pods)
    live = {p["id"]: p for p in pods}
    mine = active_managed(st)
    for pid, rec in list(mine.items()):
        if pid not in live:  # 一覧に無い = 既に消えている
            rec["status"] = "gone"
            save_json(STATE, st)
            set_running(pid, None)
            mine.pop(pid)
    others = [p for p in pods if p["id"] not in mine]
    if mine:
        for pid in mine:
            p = live[pid]
            left = (st["pods"][pid]["deadline"] - time.time()) / 60
            print(f"[管理対象] {pid}\t{p.get('status')}\t${p.get('cost')}/h\t{p.get('name')}\t期限まで {left:.0f}分")
    else:
        print("管理対象の Pod なし")
    if st["pending"]:
        print(f"作成結果が未確認の要求: {len(st['pending'])}件 (reconcile で照合)")
    if others:
        print(f"管理外の Pod: {len(others)}台 (別用途。このボットは操作しない): " +
              ", ".join(f"{p['id']}({p.get('status')}, ${p.get('cost')}/h)" for p in others))
    return 0


def require_managed(pod_id):
    st = registry()
    rec = st["pods"].get(pod_id)
    if not rec or rec.get("status") != "active":
        sys.exit(f"[拒否] {pod_id} は管理対象ではない。別用途の Pod は操作しない (自分の Pod なら adopt --yes で登録)")
    return st, rec


def ssh_direct(p):
    s = (p.get("ssh") or {}).get("direct") or {}
    return s if s.get("host") and s.get("port") else None


def cmd_status(a):
    p = api_or_exit("GET", f"/pods/{a.pod_id}")
    rec = registry()["pods"].get(a.pod_id)
    print(f"id: {p.get('id')}  name: {p.get('name')}  管理対象: {'はい' if rec and rec.get('status') == 'active' else 'いいえ'}")
    print(f"status: {p.get('status')}  cost: ${p.get('cost')}/h  gpu: {(p.get('gpu') or {}).get('id')}  dc: {p.get('dataCenterId')}")
    d = ssh_direct(p)
    print(f"ssh direct: {d['username']}@{d['host']}:{d['port']}" if d else "ssh direct: まだ無い")
    if rec:
        print(f"期限: {time.strftime('%H:%M', time.localtime(rec['deadline']))} (自動削除)")
    return 0


def cmd_wait(a):
    require_managed(a.pod_id)
    t0 = time.time()
    for _ in range(a.tries):
        try:
            p = api("GET", f"/pods/{a.pod_id}")
            if p.get("status") == "RUNNING" and ssh_direct(p):
                d = ssh_direct(p)
                print(f"RUNNING ({time.time() - t0:.0f}s)  SSH {d['host']}:{d['port']}")
                print(f"次: ../.venv/bin/python pod_ssh.py arm-watchdog {a.pod_id} && ../.venv/bin/python pod_ssh.py tunnel-start {a.pod_id}")
                return 0
            print(f"  status={p.get('status')} ssh={'あり' if ssh_direct(p) else 'なし'}")
        except ApiError as e:
            print(f"  取得失敗 {e.code}")
        time.sleep(a.interval)
    sys.exit("RUNNING/SSH 接続先が出ない。status で確認")


def cmd_adopt(a):
    if not a.yes:
        sys.exit("adopt は利用者が「この Pod をボットに任せる」と明示したときだけ。--yes を付ける")
    p = api_or_exit("GET", f"/pods/{a.pod_id}")
    st = registry()
    if active_managed(st):
        sys.exit("既に管理対象の Pod がある (1台まで)")
    register(st, p, "adopted", (p.get("gpu") or {}).get("id"), a.max_minutes, "adopt")
    st["pods"][p["id"]]["cost_per_hr"] = p.get("cost")
    save_json(STATE, st)
    print(f"登録: {p['id']} ({p.get('name')}) 期限 {a.max_minutes:.0f}分")
    spawn_watchdog()
    return 0


def delete_pod(pod_id, rec, reason):
    try:
        api("DELETE", f"/pods/{pod_id}")
    except ApiError as e:
        if e.code != 404:
            raise
    st = registry()
    r = st["pods"].get(pod_id, rec)
    hours = (time.time() - r["created"]) / 3600
    usd = float(r.get("cost_per_hr") or 0) * hours
    r.update(status="deleted", deleted=time.time(), recorded_usd=round(usd, 4), delete_reason=reason)
    st["pods"][pod_id] = r
    save_json(STATE, st)
    if os.path.exists(MODELS_JSON):
        def f(b):
            b["used_usd"] = round(float(b.get("used_usd") or 0) + usd, 4)
            (b.setdefault("running", {})).pop(pod_id, None)
            b["last_session"] = {"pod_id": pod_id, "usd": round(usd, 4), "minutes": round(hours * 60, 1), "ended": time.strftime("%Y-%m-%d %H:%M")}
        update_budget(f)
    return usd, hours


def cmd_delete(a):
    st, rec = require_managed(a.pod_id)
    bad = unfinished_jobs(a.pod_id)
    if bad and not a.allow_unretrieved:
        print("[中止] 未完了・未回収の生成ジョブがある (Pod を消すと Pod 側の待ち行列は失われる):")
        for f, s, n in bad:
            print(f"  {f}  状態={s}  未回収={n}枚")
        print("先に python3 comfy_run.py --resume <ジョブ> で回収する。諦める場合は利用者の了承を得て --allow-unretrieved")
        return 8
    try:
        usd, hours = delete_pod(a.pod_id, rec, "manual")
    except ApiError as e:
        sys.exit(f"削除要求に失敗: {e}。list で確認して再実行")
    print(f"削除しました: {a.pod_id}  概算 ${usd:.3f} ({hours * 60:.1f}分 × ${float(rec.get('cost_per_hr') or 0):.2f}/h。ディスク・ボリューム料金は含まない)")
    print("確認: python3 runpod_pod.py list で「管理対象の Pod なし」")
    return 0


def cmd_stop(a):
    print("stop は使わない (停止中もディスク料金が続く)。delete を使う。")
    return 2


def watchdog_once(log=print):
    st = registry()
    try:
        pods = list_all_pods()
    except ApiError as e:
        log(f"一覧取得に失敗 {e.code}")
        return True
    do_reconcile(st, pods)
    st = registry()
    live = {p["id"] for p in pods}
    now = time.time()
    for pid, rec in active_managed(st).items():
        if pid not in live:
            rec["status"] = "gone"
            save_json(STATE, st)
            set_running(pid, None)
            log(f"{pid}: 既に無い")
        elif now >= rec["deadline"]:
            try:
                usd, h = delete_pod(pid, rec, "deadline")
                log(f"{pid}: 期限切れのため削除 (概算 ${usd:.3f}, {h * 60:.0f}分)")
            except ApiError as e:
                log(f"{pid}: 削除失敗 {e.code} (次回再試行)")
    st = registry()
    return bool(active_managed(st) or st["pending"])


def spawn_watchdog():
    pid = load_json(WD_PID, None)
    if isinstance(pid, int):
        try:
            os.kill(pid, 0)
            return
        except OSError:
            pass
    os.makedirs(os.path.dirname(WD_LOG), exist_ok=True)
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "watchdog"], stdin=subprocess.DEVNULL,
                         stdout=open(WD_LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True, cwd=ROOT)
    save_json(WD_PID, p.pid)
    print(f"見張りを起動 (このコンピュータ上, pid {p.pid}, ログ {WD_LOG})")


def cmd_watchdog(a):
    def log(m):
        print(time.strftime("%Y-%m-%d %H:%M:%S"), m, flush=True)
    if a.once:
        watchdog_once(log)
        return 0
    save_json(WD_PID, os.getpid())
    log("見張り開始")
    while watchdog_once(log):
        time.sleep(a.interval)
    log("管理対象の Pod なし。見張り終了")
    return 0


def cmd_volumes(a):
    """ネットワークボリュームの一覧 (読み取りのみ。作成・削除・容量変更はしない)"""
    for v in api_or_exit("GET", "/network-volumes").get("networkVolumes", []):
        print(f"{v.get('id')}\t{v.get('name')}\tDC={v.get('dataCenter')}\t{v.get('size')}GB\t{v.get('type', '')}")
    return 0


def cmd_balance(a):
    me = gql("{myself{clientBalance currentSpendPerHr}}")["myself"]
    print(f"残高: ${float(me['clientBalance']):.2f}  アカウント全体の現在の課金: ${float(me['currentSpendPerHr'] or 0):.3f}/h")
    b = budget()
    if b is None:
        print("予算: 未設定 (create できない)")
    else:
        tot, used, rem = b
        print(f"予算: 総額 ${tot:.2f} / 記録済み使用 ${used:.3f} / " + (f"残り ${rem:.3f}" if rem >= 0 else f"超過 ${-rem:.3f}"))
    for pid, rec in active_managed().items():
        h = (time.time() - rec["created"]) / 3600
        print(f"稼働中 {pid}: 見込み ${float(rec.get('cost_per_hr') or 0) * h:.3f} (まだ予算に未記録)")
    return 0


def cmd_record_spend(a):
    b = update_budget(lambda b: b.__setitem__("used_usd", round(float(b.get("used_usd") or 0) + a.usd, 4)))
    print(f"予算: 総額 ${float(b['total_usd']):.2f} / 使用 ${b['used_usd']:.3f}")
    return 0


def cmd_set_budget(a):
    if a.total <= 0:
        sys.exit("--total は正の値")
    b = update_budget(lambda b: b.__setitem__("total_usd", a.total))  # used_usd は触らない
    print(f"予算総額を ${a.total:.2f} に (使用 ${float(b.get('used_usd') or 0):.3f} は維持)")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--gpu-type", action="append", default=[])
    c.add_argument("--max-hourly-usd", type=float, default=float(CFG.get("max_hourly_usd", 0.8)))
    c.add_argument("--hours", type=float, default=0.5, help="想定稼働 (見積用のみ)")
    c.add_argument("--max-minutes", type=float, default=float(CFG.get("max_session_minutes", 60)),
                   help="停止期限 (分)。過ぎたら見張りが削除する")
    c.add_argument("--max-wait-min", type=float, default=15.0)
    c.add_argument("--retry-interval-sec", type=float, default=30.0)
    c.add_argument("--gpu-count", type=int, default=1)
    c.add_argument("--image", default=CFG.get("image") or DEFAULT_IMAGE)
    c.add_argument("--cloud-type", choices=["SECURE", "COMMUNITY"], default="SECURE")
    c.add_argument("--container-disk-gb", type=int, default=int(CFG.get("container_disk_gb", 30)))
    c.add_argument("--volume-gb", type=int, default=40)
    c.add_argument("--network-volume-id", default=CFG.get("network_volume_id") or None)
    c.add_argument("--data-center-id", default=CFG.get("data_center_id") or None)
    c.add_argument("--ports", nargs="+", default=DEFAULT_PORTS)
    c.add_argument("--no-watchdog", action="store_true", help="(テスト用) 見張りを起動しない")
    c.add_argument("--yes", action="store_true")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=cmd_create)
    for name, fn in [("status", cmd_status), ("stop", cmd_stop)]:
        s = sub.add_parser(name)
        s.add_argument("pod_id")
        s.set_defaults(fn=fn)
    d = sub.add_parser("delete")
    d.add_argument("pod_id")
    d.add_argument("--allow-unretrieved", action="store_true", help="未回収ジョブがあっても削除 (利用者の了承後のみ)")
    d.set_defaults(fn=cmd_delete)
    w = sub.add_parser("wait")
    w.add_argument("pod_id")
    w.add_argument("--tries", type=int, default=60)
    w.add_argument("--interval", type=float, default=8.0)
    w.set_defaults(fn=cmd_wait)
    ad = sub.add_parser("adopt")
    ad.add_argument("pod_id")
    ad.add_argument("--max-minutes", type=float, default=float(CFG.get("max_session_minutes", 60)))
    ad.add_argument("--yes", action="store_true")
    ad.set_defaults(fn=cmd_adopt)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("reconcile").set_defaults(fn=cmd_reconcile)
    sub.add_parser("balance").set_defaults(fn=cmd_balance)
    sub.add_parser("volumes", help="ネットワークボリューム一覧 (読み取りのみ)").set_defaults(fn=cmd_volumes)
    wd = sub.add_parser("watchdog")
    wd.add_argument("--once", action="store_true")
    wd.add_argument("--interval", type=float, default=float(os.environ.get("COMFYLAB_WATCHDOG_INTERVAL", "30")))
    wd.set_defaults(fn=cmd_watchdog)
    r = sub.add_parser("record-spend")
    r.add_argument("--usd", type=float, required=True)
    r.set_defaults(fn=cmd_record_spend)
    sb = sub.add_parser("set-budget")
    sb.add_argument("--total", type=float, required=True)
    sb.set_defaults(fn=cmd_set_budget)
    a = ap.parse_args()
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
