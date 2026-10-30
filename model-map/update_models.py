#!/usr/bin/env python3
"""models.json の表示用設定を書き換える (予算の使用額 used_usd と稼働中記録 running は変更できない)。
  python3 update_models.py model.file=X.safetensors model.name="名前" settings.steps=28 budget.total_usd=1.0
  python3 update_models.py --lora-add file=A.safetensors name=A weight=0.7 status=使用中 license=...
値は JSON として読めれば JSON、読めなければ文字列。"""
import json, os, sys, time
P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
LOCKED = {"budget.used_usd", "budget.running", "budget.last_session"}
def val(s):
    try:
        return json.loads(s)
    except ValueError:
        return s
m = json.load(open(P, encoding="utf-8"))
args = sys.argv[1:]
if not args:
    sys.exit(__doc__)
if args[0] == "--lora-add":
    m.setdefault("loras", []).append({k: val(v) for k, v in (x.split("=", 1) for x in args[1:])})
else:
    for a in args:
        k, v = a.split("=", 1)
        if k in LOCKED or any(k.startswith(x + ".") for x in LOCKED):
            sys.exit(f"{k} は変更できない (使用額は runpod_pod.py が記録する。0 に戻さない)")
        cur = m
        *path, last = k.split(".")
        for p in path:
            cur = cur.setdefault(p, {})
        cur[last] = val(v)
m["updated"] = time.strftime("%Y-%m-%d %H:%M")
tmp = P + ".tmp"
json.dump(m, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
os.replace(tmp, P)
b = m.get("budget", {})
print(f"更新しました。予算 総額 ${float(b.get('total_usd') or 0):.2f} / 使用 ${float(b.get('used_usd') or 0):.3f} (維持)")
