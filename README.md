# comfy-lab 3.0.2 (プレリリース)

RunPod 上の ComfyUI を、Grok Bot のテンプレート「RunPod ComfyUI 画像生成ラボ」からチャットで操作するための道具一式です。
Tools for driving ComfyUI on your own RunPod account from a Grok Bot template (prerelease).

## できること
- Pod の見積・作成・削除と停止期限の見張り、予算の記録 (runpod/runpod_pod.py)
- SSH でのモデル・LoRA 配置と SHA256 照合、トンネル、Pod 内タイマー (runpod/pod_ssh.py, runpod/pod_setup.sh)
- Civitai / Hugging Face の配布ページからのモデル情報取得 (runpod/model_info.py)
- 生成・保存・ジョブ単位の制作履歴・中断からの回収 (runpod/comfy_run.py、ワークフロー3種)
- 管理画面: モデル・LoRA・VAE・設定・容量・予算・画像一覧・画像ごとの設定と依頼・元画像 (model-map/)

## 導入
通常は Grok Bot のスキル **comfy-lab-install** が行います。導入先は **~/comfy-lab** です。利用者の Git 操作は不要です。
手動で同じことをする場合:
1. リリースの `comfy-lab-3.0.2.zip` をダウンロード
2. `sha256sum` の値がリリースノート (とスキル) の値と一致することを確認
3. 展開して、`comfy-lab-3.0.2` フォルダで `bash install.sh`

install.sh は MANIFEST.sha256 の照合、コードの配置、`.venv` 作成と依存関係の導入、SSH 鍵 (無いときだけ)、導入確認 (py_compile・bash -n・JSON・--help) を行い、最後に「導入OK」を表示します。失敗したら原因を表示して止まります。

## 必要なもの
Linux、python3 3.11 以上 (pillow 13.0.0 の要件) (venv 付き)、curl、bash。Python パッケージは requirements.txt (paramiko 5.1.0、pillow 13.0.0、cryptography 50.0.2)。利用には利用者本人の RunPod アカウントと API キーが必要です (課金は RunPod 側で発生します)。

## 再導入・更新で上書きしないもの
config.json、model-map/models.json、out/ (画像と制作履歴)、state/、.venv、~/.ssh/comfylab_ed25519。置き換える前のコードは ~/comfy-lab/.backup/<旧版>-<日時>/ に残ります。

## ファイル
runpod/ (runpod_pod.py, pod_ssh.py, pod_setup.sh, model_info.py, comfy_run.py, workflow-api.base/lora1/lora2.json)、model-map/ (index.html, gen-index.py, update_models.py, start-desktop.sh, stop-desktop.sh, desktop_lib.sh)、templates/ (config.example.json, models.example.json — 秘密や実際の値は入っていません)、tests/ (模擬サーバーによる試験)、install.sh、requirements.txt、VERSION、MANIFEST.sha256。

## 確認済み (Linux・Python 3.13 のみ)
- 空のフォルダへの ZIP からの導入 (file:// とローカル HTTP 経由、SHA256 照合込み)
- 模擬サーバーと実プロセスによる試験 87 件 (`cd ~/comfy-lab/tests && ../.venv/bin/python -m unittest -q test_all`)
- 再導入で設定・モデル表示設定・画像・ジョブ記録・状態・SSH 鍵が変わらないこと、改変されたコードが戻ること
- SHA256 不一致・取得失敗 (404) で導入せずに止まること
- py_compile・bash -n・JSON 読み込み・--help
- 管理画面の表示 (Chrome で表示、模擬サーバーで作ったジョブ記録。モデル名・VAE名・LoRA は設定どおりの実名で表示)

## 未確認
- GPU を使った実 Pod での通し生成
- 実 Pod での RunPod v2 API (作成要求の内容、ssh.direct、Pod の自己削除、クライアント指定の prompt_id)
- ネットワークボリュームの月額などの実費
- Linux / Python 3.13 以外の環境

## ライセンス
このリポジトリと配布ZIPに含まれる本プロジェクトのコードは MIT License です (`LICENSE`、Copyright (c) 2026 Mehhi-MT)。第三者から取り込んだコードは含みません。

次のものは MIT の対象外で、それぞれ独自の利用条件・ライセンスがあります。利用者が各配布元で確認し、従ってください。
- 依存ライブラリ (paramiko・pillow・cryptography など)
- ComfyUI 本体、Pod 上で取得するカスタムノード (comfyui_controlnet_aux など)、Runpod のイメージ
- モデル・VAE・LoRA
