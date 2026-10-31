#!/usr/bin/env python3
"""models.json の表示用設定を書き換える (予算の使用額 used_usd・稼働中記録 running・費用記録 charged/last_session は変更できない)。
  python3 update_models.py model.file=X.safetensors model.name="名前" settings.steps=28 budget.total_usd=1.0
  python3 update_models.py --lora-add file=A.safetensors name=A weight=0.7 status=使用中 license=...
値は JSON として読めれば JSON、読めなければ文字列。読み込みから保存まで runpod_pod.py と同じロックを使う。"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runpod"))
import runpod_pod as rp  # noqa: E402  (file_lock / update_models を共用)
P = os.path.join(HERE, "models.json")
LOCKED = {"budget.used_usd", "budget.running", "budget.last_session", "budget.charged"}


def val(s):
    try:
        return json.loads(s)
    except ValueError:
        return s


def blocked(k):
    # budget 全体や budget.running.x の代入も、保護対象を消せるので拒否する
    return any(k == x or k.startswith(x + ".") or x.startswith(k + ".") for x in LOCKED)


def main(args):
    if not args:
        sys.exit(__doc__)
    if args[0] == "--lora-add":
        item = {k: val(v) for k, v in (x.split("=", 1) for x in args[1:])}
        fn = lambda m: m.setdefault("loras", []).append(item)  # noqa: E731
    else:
        sets = []
        for a in args:
            if "=" not in a:
                sys.exit(f"KEY=VALUE 形式: {a!r}")
            k, v = a.split("=", 1)
            if blocked(k):
                sys.exit(f"{k} は変更できない (使用額・稼働記録は runpod_pod.py が記録する。0 に戻さない)")
            sets.append((k, val(v)))

        def fn(m):
            for k, v in sets:
                cur = m
                *path, last = k.split(".")
                for p in path:
                    cur = cur.setdefault(p, {})
                cur[last] = v
    rp.update_models(fn, P)
    b = rp.load_json(P, {}).get("budget", {})
    print(f"更新しました。予算 総額 ${float(b.get('total_usd') or 0):.2f} / 使用 ${float(b.get('used_usd') or 0):.3f} (維持)")


if __name__ == "__main__":
    main(sys.argv[1:])
