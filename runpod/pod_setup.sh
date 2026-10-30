#!/usr/bin/env bash
# Pod 内で実行 (pod_ssh.py が送る)。必要なファイルが揃っているか確認し、足りないものだけ取得する。
# 標準入力: 1行目 = CIVITAI_TOKEN (空でもよい。表示・保存しない)、2行目以降 = 必要ファイル一覧
#   "<subdir>|<file>|<url>|<size or 空>|<sha256 or 空>"   例: checkpoints|model.safetensors|https://...|6938...|ab12...
# 取得は <file>.part へ。成功・サイズ・SHA256 を確認してから正式名へ mv。失敗したら .part を消して終了コード 1。
# --check-only: 取得せず、揃っているかだけ報告 (足りなければ終了コード 3)
set -Eeuo pipefail
MODE="${1:-fetch}"
IFS= read -r CIVITAI_TOKEN || CIVITAI_TOKEN=""
log() { printf '[pod_setup] %s\n' "$*"; }
die() { printf '[pod_setup] ERROR: %s\n' "$*" >&2; exit 1; }
is_civitai() {  # https のみ、ホスト名が civitai.com / *.civitai.com のときだけ真
  local re='^https://([A-Za-z0-9.-]+)(:[0-9]+)?(/|\?|#|$)'
  [[ "$1" =~ $re ]] || return 1
  local h="${BASH_REMATCH[1],,}"; h="${h%.}"
  [[ "$h" == "civitai.com" || "$h" == *.civitai.com ]]
}
detect_root() {
  if [[ -n "${COMFY_ROOT:-}" ]]; then printf '%s\n' "$COMFY_ROOT"; return; fi
  local d
  for _ in $(seq 1 60); do
    d="$(find /workspace -maxdepth 5 -type d -path '*/models/checkpoints' 2>/dev/null | head -n1 || true)"
    if [[ -n "$d" ]]; then dirname "$(dirname "$d")"; return; fi
    log "ComfyUI の models/checkpoints を待機中..." >&2; sleep 10
  done
  return 1
}
verify() {  # verify <path> <size> <sha>  (size は Civitai だと KB 由来の概算なので ±2KB 許容は呼び出し側の SIZE_TOL)
  local f="$1" size="$2" sha="$3" a
  [[ -s "$f" ]] || return 1
  if [[ -n "$size" ]]; then a="$(stat -c%s "$f")"; local d=$(( a > size ? a - size : size - a ));
    (( d <= ${SIZE_TOL:-0} )) || { log "サイズ不一致 $(basename "$f"): $a != $size"; return 1; }; fi
  if [[ -n "$sha" ]]; then a="$(sha256sum "$f" | cut -d' ' -f1)"; [[ "${a,,}" == "${sha,,}" ]] || { log "SHA256 不一致 $(basename "$f")"; return 1; }; fi
  return 0
}
ROOT="$(detect_root)" || die "/workspace 配下に ComfyUI の models が無い"
log "ComfyUI root: $ROOT"
missing=0; failed=0
while IFS='|' read -r sub file url size sha tol; do
  [[ -z "${sub}${file}" ]] && continue
  [[ "$file" == */* || "$sub" == *..* ]] && die "不正なパス: $sub/$file"
  dest="$ROOT/models/$sub/$file"; mkdir -p "$ROOT/models/$sub"
  if SIZE_TOL="${tol:-0}" verify "$dest" "$size" "$sha"; then log "揃っている: $sub/$file"; continue; fi
  if [[ -e "$dest" && -z "$sha" && -z "$size" ]]; then log "揃っている (照合値なし): $sub/$file"; continue; fi
  missing=$((missing+1))
  if [[ "$MODE" == "--check-only" ]]; then log "不足: $sub/$file"; continue; fi
  [[ -n "$sha" ]] || log "注意: $file は SHA256 が無いのでサイズだけ確認"
  hdr=()
  if is_civitai "$url"; then
    [[ -n "$CIVITAI_TOKEN" ]] || die "Civitai からの取得に CIVITAI_TOKEN が必要 ($file)"
    hdr=(-H "Authorization: Bearer $CIVITAI_TOKEN")   # curl はホストが変わるリダイレクト先へ Authorization を送らない
  fi
  log "取得: $sub/$file"
  rm -f "$dest.part"
  if ! curl -fL --retry 3 --connect-timeout 30 -sS "${hdr[@]}" -o "$dest.part" "$url"; then
    rm -f "$dest.part"; log "取得失敗: $file"; failed=$((failed+1)); continue
  fi
  if ! SIZE_TOL="${tol:-0}" verify "$dest.part" "$size" "$sha"; then
    rm -f "$dest.part"; log "照合失敗のため配置しない: $file"; failed=$((failed+1)); continue
  fi
  mv -f "$dest.part" "$dest"; log "配置: $sub/$file ($(stat -c%s "$dest") bytes)"
done
df -h /workspace | tail -1
if (( failed > 0 )); then die "$failed 件の取得/照合に失敗"; fi
if [[ "$MODE" == "--check-only" && $missing -gt 0 ]]; then log "不足 $missing 件"; exit 3; fi
log "必要なファイルは揃っている"
