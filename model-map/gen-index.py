#!/usr/bin/env python3
"""out/ を走査して images.json を作る。再実行で新しい画像が反映される。"""
import json, os, time, datetime
try:
    from PIL import Image
except Exception:
    Image = None
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = [os.path.join(ROOT, "out")]  # 生成画像の保存先 (comfy_run.py --out ~/comfy-lab/out/<作品名>/<条件>)
IMG = os.path.join(HERE, "images")
EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif")

def group_desc(p):
    """フォルダの説明: <作品>/desc.txt があれば1行目、無ければ最新 run.json の note"""
    f = os.path.join(p, "desc.txt")
    if os.path.exists(f):
        return open(f, encoding="utf-8").readline().strip()
    best = (0, "")
    for dp, dn, fn in os.walk(p, followlinks=True):
        for jf in ([os.path.join(dp, x) for x in fn if x.endswith(".json") and not x.endswith(".workflow.json")] if os.path.basename(dp) == "jobs" else []):
            try:
                best = max(best, (os.path.getmtime(jf), json.load(open(jf, encoding="utf-8")).get("note") or ""))
            except Exception:
                pass
        if "run.json" in fn:
            q = os.path.join(dp, "run.json")
            try:
                best = max(best, (os.path.getmtime(q), json.load(open(q, encoding="utf-8")).get("note") or ""))
            except Exception:
                pass
    return best[1]

def load_jobs():
    """out/**/jobs/*.json を読み、保存画像の絶対パス -> ジョブ記録 の対応を作る (画像ごとに自分の依頼・親画像・Workflow)"""
    by_img, done = {}, []
    for src in SOURCES:
        for dp, dn, fn in os.walk(src, followlinks=True):
            if os.path.basename(dp) != "jobs": continue
            for f in fn:
                if not f.endswith(".json") or f.endswith(".workflow.json"): continue
                try: j = json.load(open(os.path.join(dp, f), encoding="utf-8"))
                except Exception: continue
                for im in j.get("images", []):
                    if im.get("saved_path"): by_img[os.path.realpath(im["saved_path"])] = j
                if j.get("status") == "complete": done.append(j)
    done.sort(key=lambda j: j.get("created", 0))
    return by_img, done

def legacy_run(d, full):
    """旧形式 run.json: その画像が images に載っているときだけ使う (別の生成の依頼を付けない)"""
    try:
        r = json.load(open(os.path.join(d, "run.json"), encoding="utf-8"))
        if os.path.realpath(full) in {os.path.realpath(x) for x in r.get("images", [])}:
            return {k: r[k] for k in ("note", "parent") if r.get(k)}
    except Exception:
        pass
    return {}

def job_meta(j):
    g = None
    try: g = json.load(open(j["workflow_sent"], encoding="utf-8"))
    except Exception: pass
    m = dict(graph_meta(g) or {}) if g else {}
    for k in ("note", "parent", "job_id", "prompt_id", "seed"):
        if j.get(k) not in (None, ""): m[k] = j[k]
    return m

def _src(graph, ref, want, depth=0):
    """リンク ref=[node_id, idx] をたどって want クラスのノードを探す"""
    if depth > 12 or not isinstance(ref, list) or not ref: return None
    n = graph.get(str(ref[0]))
    if not isinstance(n, dict): return None
    if n.get("class_type") in want: return n
    ins = n.get("inputs", {})
    for k in ("positive", "negative", "conditioning", "conditioning_to", "model", "clip", "vae"):
        if isinstance(ins.get(k), list):
            r = _src(graph, ins[k], want, depth + 1)
            if r: return r
    return None

def extract_meta(full):
    """PNG埋め込みの 'prompt'(API形式)から生成設定を取り出す。失敗時はNone"""
    if Image is None: return None
    try:
        with Image.open(full) as im:
            raw = im.info.get("prompt")
        return graph_meta(json.loads(raw)) if raw else None
    except Exception:
        return None

def graph_meta(g):
    """API形式の Workflow から モデル・VAE・LoRA・基本設定 を取り出す"""
    try:
        ks = next((n for n in g.values() if isinstance(n, dict) and n.get("class_type") in ("KSampler", "KSamplerAdvanced")), None)
        if not ks: return None
        i = ks["inputs"]; m = {}
        for key, k in (("positive", "positive"), ("negative", "negative")):
            t = _src(g, i.get(k), ("CLIPTextEncode",))
            if t and isinstance(t["inputs"].get("text"), str): m[key] = t["inputs"]["text"]
        for key, k in (("seed", "seed"), ("steps", "steps"), ("cfg", "cfg"), ("sampler", "sampler_name"), ("scheduler", "scheduler"), ("denoise", "denoise")):
            v = i.get(k, i.get("noise_seed") if k == "seed" else None)
            if v is not None and not isinstance(v, list): m[key] = v
        lat = _src(g, i.get("latent_image"), ("EmptyLatentImage",))
        if lat and "width" in lat["inputs"]: m["size"] = "%sx%s" % (lat["inputs"]["width"], lat["inputs"]["height"])
        ck = _src(g, i.get("model"), ("CheckpointLoaderSimple",)) or next((n for n in g.values() if isinstance(n, dict) and n.get("class_type") == "CheckpointLoaderSimple"), None)
        if ck: m["model"] = ck["inputs"].get("ckpt_name")
        vl = next((n for n in g.values() if isinstance(n, dict) and n.get("class_type") == "VAELoader"), None)
        if vl: m["vae"] = vl["inputs"].get("vae_name")
        loras = []; cur = g.get(str(i["model"][0])) if isinstance(i.get("model"), list) else None; d = 0
        while cur and cur.get("class_type") == "LoraLoader" and d < 12:
            ci = cur["inputs"]
            loras.append({"name": ci.get("lora_name"), "model": ci.get("strength_model"), "clip": ci.get("strength_clip")})
            cur = g.get(str(ci["model"][0])) if isinstance(ci.get("model"), list) else None; d += 1
        loras.reverse()  # 適用順(モデル側→出力側)
        if loras: m["loras"] = loras
        return m or None
    except Exception:
        return None

_mc = {}
def meta_for(full, mtime):
    k = (full, mtime)
    if k not in _mc: _mc[k] = extract_meta(full)
    return _mc[k]
os.makedirs(IMG, exist_ok=True)
groups = {}  # name -> source path
rootfiles = []
for src in SOURCES:
    if not os.path.isdir(src): continue
    for n in sorted(os.listdir(src)):
        p = os.path.join(src, n)
        if os.path.isdir(p):
            groups.setdefault(n, p)
        elif n.lower().endswith(EXT):
            rootfiles.append(p)
def link(dst, target):
    if os.path.islink(dst):
        if os.readlink(dst) == target: return
        os.remove(dst)
    elif os.path.exists(dst): return
    os.symlink(target, dst)
if rootfiles:
    rd = os.path.join(IMG, "_root"); os.makedirs(rd, exist_ok=True)
    for f in rootfiles: link(os.path.join(rd, os.path.basename(f)), f)
    groups["_root"] = rd
for g, p in groups.items():
    if g != "_root": link(os.path.join(IMG, g), p)
JOBS, DONE = load_jobs()
out = []
for g, p in groups.items():
    files = []
    for dp, dn, fn in os.walk(p, followlinks=True):
        for f in fn:
            if not f.lower().endswith(EXT): continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, p)
            m = os.path.getmtime(full)
            e = {"name": rel, "path": "images/" + g + "/" + rel, "mtime": m,
                 "time": datetime.datetime.fromtimestamp(m).strftime("%m/%d %H:%M")}
            j = JOBS.get(os.path.realpath(full))
            if j:
                mt = job_meta(j)  # 実際に送った Workflow とその画像自身の依頼
            else:
                mt = dict(meta_for(full, m) or {})
                mt.update(legacy_run(dp, full))
            if mt: e["meta"] = mt
            files.append(e)
    files.sort(key=lambda x: x["name"])
    if not files: continue
    out.append({"folder": g if g != "_root" else "(直下)", "desc": group_desc(p),
                "mtime": max(f["mtime"] for f in files), "files": files})
out.sort(key=lambda x: -x["mtime"])
data = json.dumps({"updated": time.strftime("%H:%M:%S"), "groups": out}, ensure_ascii=False)
# 内容が変わらない時は書き換えない(更新時刻だけの差は無視)
cmp = json.dumps(out, ensure_ascii=False)
tmp = os.path.join(HERE, ".images.cmp")
old = open(tmp).read() if os.path.exists(tmp) else ""
if cmp != old:
    open(os.path.join(HERE, "images.json.tmp"), "w").write(data)
    os.replace(os.path.join(HERE, "images.json.tmp"), os.path.join(HERE, "images.json"))
    open(tmp, "w").write(cmp)

# 管理画面の「今の構成」: 最後に完了したジョブで実際に送った Workflow から
if DONE:
    j = DONE[-1]; cm = job_meta(j)
    cur = {"from_job": j.get("job_id"), "time": datetime.datetime.fromtimestamp(j.get("created", 0)).strftime("%m/%d %H:%M"),
           "model": cm.get("model"), "vae": cm.get("vae"), "loras": cm.get("loras", []),
           "settings": {k: cm.get(k) for k in ("steps", "cfg", "sampler", "scheduler", "seed", "size")}}
    tmpc = os.path.join(HERE, "current.json.tmp")
    json.dump(cur, open(tmpc, "w"), ensure_ascii=False); os.replace(tmpc, os.path.join(HERE, "current.json"))
