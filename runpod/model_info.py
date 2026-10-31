#!/usr/bin/env python3
"""モデル配布ページから取得情報を調べる (読み取りのみ, stdlib)。利用者に技術情報を聞かずに済ませる。

  python3 model_info.py <URL>    # Civitai のモデル/バージョンページ・ダウンロードURL、Hugging Face の resolve URL
出力 (JSON): file, url, size, sha256, kind, base_model, license 関連, source_page
Civitai: https://civitai.com/api/v1/model-versions/<id> と /api/v1/models/<id> (公開情報。トークンは制限付きモデルのときだけ使う)
Hugging Face: resolve URL への HEAD (サイズと LFS の SHA256 = X-Linked-ETag)
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "comfy-lab-model_info/3.0"


def is_civitai(url):
    """https で、ホスト名が civitai.com かそのサブドメインのときだけ True (トークンを付けてよい先)。"""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    h = (u.hostname or "").lower().rstrip(".")
    return u.scheme == "https" and u.username is None and u.password is None and (h == "civitai.com" or h.endswith(".civitai.com"))


def is_hf(url):
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and (u.hostname or "").lower() in ("huggingface.co", "hf.co")


class FetchError(SystemExit):
    pass


def fail(url, code):
    hint = {401: "認証が必要/トークンが無効", 403: "アクセス拒否 (制限付きモデル・規約同意が必要など)", 404: "見つからない"}.get(code)
    if hint is None and code and code >= 500:
        hint = "配布元のサーバーエラー。時間をおいて再実行"
    raise FetchError(f"取得失敗 HTTP {code}: {hint or '想定外の応答'} ({urllib.parse.urlsplit(url).hostname})")


def get_json(url):
    h = {"User-Agent": UA}
    tok = os.environ.get("CIVITAI_TOKEN")
    if tok and is_civitai(url):
        h["Authorization"] = f"Bearer {tok}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
            if r.status != 200:
                fail(url, r.status)
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        fail(url, e.code)
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"接続失敗: {getattr(e, 'reason', e)}")


def civitai(url):
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(u.query)
    vid = (q.get("modelVersionId") or [None])[0]
    m = re.search(r"/api/download/models/(\d+)", u.path) or re.search(r"/model-versions/(\d+)", u.path)
    if m:
        vid = m.group(1)
    mid = None
    m = re.search(r"/models/(\d+)", u.path)
    if m and "/api/download/" not in u.path:
        mid = m.group(1)
    if not vid:
        if not mid:
            sys.exit("Civitai の URL からモデル/バージョンIDが読めない")
        vid = str(get_json(f"https://civitai.com/api/v1/models/{mid}")["modelVersions"][0]["id"])  # 最新版
    v = get_json(f"https://civitai.com/api/v1/model-versions/{vid}")
    files = v.get("files") or []
    f = next((x for x in files if x.get("primary")), files[0] if files else None)
    if not f:
        sys.exit("ファイル情報が無い")
    mid = mid or str(v.get("modelId"))
    model = get_json(f"https://civitai.com/api/v1/models/{mid}") if mid else {}
    return {"file": f.get("name"), "url": f.get("downloadUrl") or f"https://civitai.com/api/download/models/{vid}",
            "size": int(round(float(f.get("sizeKB") or 0) * 1024)) or None, "size_exact": False,
            "sha256": ((f.get("hashes") or {}).get("SHA256") or "").lower() or None,
            "kind": model.get("type"), "base_model": v.get("baseModel"), "version_id": int(vid), "model_id": mid,
            "name": f"{model.get('name', '')} {v.get('name', '')}".strip(),
            "license": {k: model.get(k) for k in ("allowCommercialUse", "allowDerivatives", "allowDifferentLicense", "allowNoCredit") if k in model},
            "source_page": f"https://civitai.com/models/{mid}?modelVersionId={vid}", "requires_token": bool(v.get("downloadUrl"))}


def huggingface(url):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    op = urllib.request.build_opener(NoRedirect)
    try:
        r = op.open(urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA}), timeout=30)
        code, hd = r.status, r.headers
    except urllib.error.HTTPError as e:
        code, hd = e.code, e.headers
    except (urllib.error.URLError, OSError) as e:
        raise FetchError(f"接続失敗: {getattr(e, 'reason', e)}")
    if not (200 <= code < 300 or code in (301, 302, 303, 307, 308)):  # 転送 (LFS の実体) は成功扱い。401/403/404/5xx は失敗
        fail(url, code)
    etag = (hd.get("X-Linked-ETag") or hd.get("ETag") or "").strip('"')
    size = hd.get("X-Linked-Size") or hd.get("Content-Length")
    return {"file": os.path.basename(urllib.parse.urlsplit(url).path), "url": url, "size": int(size) if size else None, "size_exact": True,
            "sha256": etag.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", etag) else None, "source_page": url.split("/resolve/")[0],
            "license": "配布ページ (README のライセンス欄) を利用者が確認"}


def lookup(url):
    if is_civitai(url):
        return civitai(url)
    if is_hf(url):
        return huggingface(url)
    sys.exit("対応していない配布元 (Civitai / Hugging Face の https URL)。ファイル名・URL・SHA256 を配布ページから読み取って config に書く")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(lookup(sys.argv[1]), ensure_ascii=False, indent=2))
