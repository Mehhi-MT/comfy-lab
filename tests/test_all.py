"""模擬確認 (実通信なし)。python3 -m unittest -v test_all  (導入先で: .venv/bin/python -m unittest -v tests/test_all.py)"""
import glob, hashlib, time, json, os, shutil, subprocess, sys, tempfile, threading, unittest
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import functools
HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.environ.get("COMFYLAB_PAYLOAD") or os.path.dirname(HERE)  # 導入先 (config.json がある所)
sys.path.insert(0, HERE)
import mocks

FAKE = "TEST-FAKE-KEY"


class Base(unittest.TestCase):
    def setUp(self):
        self.S = mocks.State()
        self.srv, self.port = mocks.start(self.S)
        self.tmp = tempfile.mkdtemp(prefix="cl3-")
        self.root = os.path.join(self.tmp, "comfy-lab")
        shutil.copytree(PAYLOAD, self.root, ignore=shutil.ignore_patterns(".venv", ".backup", "out", "state", "tests", "__pycache__", "images", "images.json", "*.log"))
        os.makedirs(os.path.join(self.tmp, ".ssh"))
        open(os.path.join(self.tmp, ".ssh", "k.pub"), "w").write("ssh-ed25519 AAAATEST comfy-lab\n")
        cfg = json.load(open(self.cfgp))
        cfg.update(ssh_key=os.path.join(self.tmp, ".ssh", "k"), data_center_id="CA-MTL-1", network_volume_id="vol-test", checkpoint="test_model.safetensors")
        json.dump(cfg, open(self.cfgp, "w"))
        self.set_budget(1.0, 0.2)
        self.env = dict(os.environ, RUNPOD_API_BASE=f"http://127.0.0.1:{self.port}/v2", RUNPOD_API_KEY=FAKE,
                        RUNPOD_GQL_BASE=f"http://127.0.0.1:{self.port}/graphql", COMFYLAB_RECONCILE_INTERVAL="0.05",
                        COMFYLAB_RECONCILE_TRIES="4", HOME=self.tmp, COMFY_BASE_URL=f"http://127.0.0.1:{self.port}")
        self.env.pop("CIVITAI_TOKEN", None)

    def tearDown(self):
        try:  # テスト中に起動した見張りを止める
            wp = int(open(os.path.join(self.root, "state", "watchdog.pid")).read())
            if "watchdog" in open(f"/proc/{wp}/cmdline").read():
                os.kill(wp, 9)
        except (OSError, ValueError):
            pass
        self.srv.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def cfgp(self): return os.path.join(self.root, "config.json")
    @property
    def modelsp(self): return os.path.join(self.root, "model-map", "models.json")
    @property
    def reg(self): return json.load(open(os.path.join(self.root, "state", "pods.json")))

    def set_budget(self, total, used):
        m = json.load(open(self.modelsp)); m["budget"].update(total_usd=total, used_usd=used); json.dump(m, open(self.modelsp, "w"))

    def rp(self, *args, py=sys.executable):
        r = subprocess.run([py, os.path.join(self.root, "runpod", "runpod_pod.py"), *args], env=self.env, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        self.assertNotIn(FAKE, out, "API キーが出力に出た")
        return r.returncode, out

    def cr(self, *args):
        r = subprocess.run([sys.executable, os.path.join(self.root, "runpod", "comfy_run.py"), *args], env=self.env, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    def posts(self): return [c for c in self.S.calls if c == ("POST", "/v2/pods")]


class T1_CreateResponseLost(Base):
    def test_lost_after_create_reconciles_no_double(self):
        self.S.create_mode = "lost_after_create"
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.posts()), 1, "作成要求は1回だけ")
        self.assertEqual(len(self.S.pods), 1)
        pid = next(iter(self.S.pods))
        self.assertEqual(self.reg["pods"][pid]["status"], "active")
        self.assertEqual(self.reg["pending"], {})
        self.assertIn("照合で見つかった", out)
        self.assertEqual(self.S.pods[pid]["ports"], ["22/tcp"], "ComfyUI/FileBrowser を公開しない")

    def test_lost_and_not_visible_stops_then_reconcile(self):
        self.S.create_mode = "lost_after_create"; self.S.hide_new_pod_lists = 100
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 6, out)
        self.assertEqual(len(self.posts()), 1, "不明なまま次の候補を作らない")
        self.assertEqual(len(self.reg["pending"]), 1)
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 5, "未確認の要求があるうちは作らない")
        self.S.hide_new_pod_lists = 0
        code, out = self.rp("reconcile")
        self.assertIn("登録: ", out)
        self.assertEqual(len([p for p in self.reg["pods"].values() if p["status"] == "active"]), 1)
        self.assertEqual(len(self.posts()), 1)

    def test_nostock_moves_to_next(self):
        self.S.nostock = {"NVIDIA RTX A5000"}
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.S.pods), 1)


class T2_UnmanagedPods(Base):
    def test_unmanaged_not_touched(self):
        other = self.S.add_pod("someone-training", cost=1.2)
        code, out = self.rp("list")
        self.assertIn("管理対象の Pod なし", out); self.assertIn("管理外の Pod: 1台", out)
        code, out = self.rp("delete", other)
        self.assertNotEqual(code, 0); self.assertIn("管理対象ではない", out)
        self.assertIn(other, self.S.pods)
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.S.pods), 2, "別用途 Pod を再利用せず自分の Pod を作る")
        mine = [k for k in self.S.pods if k != other][0]
        r = json.load(open(os.path.join(self.root, "state", "pods.json")))
        r["pods"][mine]["deadline"] = 0; json.dump(r, open(os.path.join(self.root, "state", "pods.json"), "w"))
        code, out = self.rp("watchdog", "--once")
        self.assertNotIn(mine, self.S.pods, "期限切れの管理対象は削除")
        self.assertIn(other, self.S.pods, "管理外は削除しない")
        code, out = self.rp("list")
        self.assertIn("管理対象の Pod なし", out)

    def test_adopt_requires_yes(self):
        other = self.S.add_pod("mine-made-by-hand")
        code, out = self.rp("adopt", other)
        self.assertNotEqual(code, 0)


class T3_BudgetAndPrice(Base):
    def test_missing_budget_refuses(self):
        self.set_budget(0, 0)
        code, out = self.rp("create", "--hours", "0.25", "--yes", "--no-watchdog")
        self.assertEqual(code, 4); self.assertEqual(self.posts(), [])

    def test_overrun_refuses(self):
        self.set_budget(0.5, 0.62)
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 3, out); self.assertIn("-0.12", out); self.assertEqual(self.posts(), [])

    def test_deadline_cap_exceeds_budget(self):
        self.set_budget(1.0, 0.8)
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "60", "--yes", "--no-watchdog")
        self.assertEqual(code, 3, out); self.assertEqual(self.posts(), [])

    def test_catalog_price_above_max_excluded(self):
        self.S.prices = {"NVIDIA RTX A5000": 0.95, "NVIDIA A40": 1.10}
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 3, out); self.assertEqual(self.posts(), [])

    def test_actual_price_above_max_deleted(self):
        self.S.created_cost = 1.5
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 7, out); self.assertEqual(self.S.pods, {}, "上限超えの Pod はすぐ削除")

    def test_settings_change_keeps_used(self):
        up = os.path.join(self.root, "model-map", "update_models.py")
        r = subprocess.run([sys.executable, up, "model.file=new.safetensors", "budget.total_usd=2.0"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        b = json.load(open(self.modelsp))["budget"]; self.assertEqual((b["total_usd"], b["used_usd"]), (2.0, 0.2))
        r = subprocess.run([sys.executable, up, "budget.used_usd=0"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.load(open(self.modelsp))["budget"]["used_usd"], 0.2)
        code, out = self.rp("set-budget", "--total", "3")
        b = json.load(open(self.modelsp))["budget"]; self.assertEqual((b["total_usd"], b["used_usd"]), (3.0, 0.2))

    def test_delete_records_spend(self):
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        pid = next(iter(self.S.pods))
        self.assertIn(pid, json.load(open(self.modelsp))["budget"]["running"])
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 0, out)
        b = json.load(open(self.modelsp))["budget"]
        self.assertGreaterEqual(b["used_usd"], 0.2); self.assertEqual(b["running"], {}); self.assertIn("last_session", b)


class T4_Generation(Base):
    def test_multi_seed_each_own_record(self):
        out_dir = os.path.join(self.root, "out", "cat", "base")
        code, out = self.cr("--set", "6.text=a cat", "--seed", "increment:40", "--count", "3", "--out", out_dir, "--note", "窓辺の猫", "--pod-id", "podX", "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        jobs = sorted(f for f in glob.glob(os.path.join(out_dir, "jobs", "*.json")) if not f.endswith(".workflow.json"))
        self.assertEqual(len(jobs), 3)
        seeds = []
        for jp in jobs:
            j = json.load(open(jp)); wf = json.load(open(j["workflow_sent"]))
            self.assertEqual(j["status"], "complete"); self.assertEqual(wf["3"]["inputs"]["seed"], j["seed"])
            self.assertEqual(self.S.prompts[j["prompt_id"]]["wf"]["3"]["inputs"]["seed"], j["seed"], "記録した Workflow = 実際に送った Workflow")
            self.assertTrue(all(i["retrieved"] and os.path.exists(i["saved_path"]) for i in j["images"]))
            seeds.append(j["seed"])
        self.assertEqual(seeds, [40, 41, 42])
        # 2回目の生成 (同じ出力先, 別の依頼) で履歴を上書きしない
        code, out = self.cr("--set", "6.text=a dog", "--seed", "fixed:7", "--out", out_dir, "--note", "犬に変更", "--parent", "x.png", "--poll-interval", "0.05")
        self.assertEqual(len([f for f in glob.glob(os.path.join(out_dir, "jobs", "*.json")) if not f.endswith(".workflow.json")]), 4)
        # 管理画面: 画像ごとに自分の依頼・seed
        subprocess.run([sys.executable, os.path.join(self.root, "model-map", "gen-index.py")], check=True, cwd=os.path.join(self.root, "model-map"))
        idx = json.load(open(os.path.join(self.root, "model-map", "images.json")))
        files = [f for g in idx["groups"] for f in g["files"]]
        self.assertEqual(len(files), 4)
        for f in files:
            m = f["meta"]
            if m["seed"] == 7: self.assertEqual((m["note"], m.get("parent")), ("犬に変更", "x.png"))
            else: self.assertEqual(m["note"], "窓辺の猫"); self.assertNotIn("parent", m)
        cur = json.load(open(os.path.join(self.root, "model-map", "current.json")))
        self.assertEqual(cur["settings"]["seed"], 7)

    def test_retrieval_failure_then_resume(self):
        out_dir = os.path.join(self.root, "out", "t", "a")
        self.S.view_fail = 5
        code, out = self.cr("--set", "6.text=x", "--seed", "fixed:1", "--out", out_dir, "--pod-id", "podY", "--poll-interval", "0.05")
        self.assertNotEqual(code, 0)
        jp = [f for f in glob.glob(os.path.join(out_dir, "jobs", "*.json")) if not f.endswith(".workflow.json")][0]
        j = json.load(open(jp)); self.assertIn(j["status"], ("running", "queued", "partial")); self.assertTrue(j["prompt_id"])
        self.assertEqual(len(self.S.prompts), 1)
        code, out = self.cr("--pending", os.path.join(self.root, "out"))
        self.assertEqual(code, 1)
        self.S.view_fail = 0
        code, out = self.cr("--resume", out_dir, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.S.prompts), 1, "再開で新しく生成しない")
        self.assertEqual(json.load(open(jp))["status"], "complete")

    def test_send_response_lost_resume_by_own_prompt_id(self):
        out_dir = os.path.join(self.root, "out", "t", "b")
        self.S.prompt_fail_after_accept = 1
        code, out = self.cr("--set", "6.text=x", "--seed", "increment:1", "--count", "2", "--out", out_dir, "--poll-interval", "0.05", "--timeout", "5")
        self.assertNotEqual(code, 0)
        self.assertEqual(len(self.S.prompts), 1, "送れたか不明のまま次を送らない")
        code, out = self.cr("--resume", out_dir, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.S.prompts), 1)

    def test_delete_blocked_by_unretrieved(self):
        code, out = self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        pid = next(iter(self.S.pods))
        out_dir = os.path.join(self.root, "out", "t", "c")
        self.S.view_fail = 5
        self.cr("--set", "6.text=x", "--seed", "fixed:3", "--out", out_dir, "--pod-id", pid, "--poll-interval", "0.05")
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 8, out); self.assertIn(pid, self.S.pods)
        self.S.view_fail = 0
        self.cr("--resume", out_dir, "--poll-interval", "0.05")
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 0, out); self.assertNotIn(pid, self.S.pods)


class T5_ModelFetch(unittest.TestCase):
    """pod_setup.sh をこのコンピュータ上で実行 (COMFY_ROOT=一時フォルダ、配布元=ローカル HTTP)。"""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cl3f-")
        self.www = os.path.join(self.tmp, "www"); os.makedirs(self.www)
        self.data = os.urandom(5000); open(os.path.join(self.www, "good.safetensors"), "wb").write(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(type("Q", (SimpleHTTPRequestHandler,), {"log_message": lambda *a: None}), directory=self.www))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.comfy = os.path.join(self.tmp, "ComfyUI"); os.makedirs(os.path.join(self.comfy, "models", "checkpoints"))
    def tearDown(self):
        self.srv.shutdown(); shutil.rmtree(self.tmp, ignore_errors=True)
    def run_setup(self, lines, mode=None):
        r = subprocess.run(["bash", os.path.join(PAYLOAD, "runpod", "pod_setup.sh")] + ([mode] if mode else []),
                           input="\n" + "\n".join(lines) + "\n", env=dict(os.environ, COMFY_ROOT=self.comfy), capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr
    def lora(self, name): return os.path.join(self.comfy, "models", "loras", name)
    def test_download_404_fails_no_file(self):
        code, out = self.run_setup([f"loras|a.safetensors|{self.url}/missing.safetensors|5000|{self.sha}|0"])
        self.assertEqual(code, 1, out); self.assertFalse(os.path.exists(self.lora("a.safetensors"))); self.assertFalse(os.path.exists(self.lora("a.safetensors.part")))
    def test_hash_mismatch_fails_no_file(self):
        code, out = self.run_setup([f"loras|a.safetensors|{self.url}/good.safetensors|5000|{'0'*64}|0"])
        self.assertEqual(code, 1, out); self.assertFalse(os.path.exists(self.lora("a.safetensors"))); self.assertIn("SHA256 不一致", out)
    def test_size_mismatch_fails(self):
        code, out = self.run_setup([f"loras|a.safetensors|{self.url}/good.safetensors|9999||0"])
        self.assertEqual(code, 1, out); self.assertFalse(os.path.exists(self.lora("a.safetensors")))
    def test_failure_not_masked_by_later_success(self):
        code, out = self.run_setup([f"loras|bad.safetensors|{self.url}/missing|5000|{self.sha}|0",
                                    f"loras|ok.safetensors|{self.url}/good.safetensors|5000|{self.sha}|0"])
        self.assertEqual(code, 1, out); self.assertTrue(os.path.exists(self.lora("ok.safetensors"))); self.assertFalse(os.path.exists(self.lora("bad.safetensors")))
    def test_good_then_check_only_present(self):
        line = f"checkpoints|m.safetensors|{self.url}/good.safetensors|5000|{self.sha}|0"
        code, out = self.run_setup([line], "--check-only"); self.assertEqual(code, 3, out)
        code, out = self.run_setup([line]); self.assertEqual(code, 0, out)
        code, out = self.run_setup([line], "--check-only"); self.assertEqual(code, 0, out); self.assertIn("揃っている", out)
    def test_civitai_without_token_refused(self):
        code, out = self.run_setup(["loras|c.safetensors|https://civitai.com/api/download/models/1|1|"])
        self.assertEqual(code, 1); self.assertIn("CIVITAI_TOKEN", out)


class T6_HostCheck(unittest.TestCase):
    CASES = {"https://civitai.com/api/download/models/1": True, "https://www.civitai.com/x": True, "https://CIVITAI.COM/x": True,
             "http://civitai.com/x": False, "https://civitai.com.evil.example/x": False, "https://evil.example/?u=civitai.com": False,
             "https://evilcivitai.com/x": False, "https://user@civitai.com/x": False, "https://evil.example/civitai.com": False}
    def test_python(self):
        sys.path.insert(0, os.path.join(PAYLOAD, "runpod")); import model_info
        for u, ok in self.CASES.items(): self.assertEqual(model_info.is_civitai(u), ok, u)
    def test_bash(self):
        src = open(os.path.join(PAYLOAD, "runpod", "pod_setup.sh")).read()
        fn = src[src.index("is_civitai() {"):src.index("detect_root()")]
        for u, ok in self.CASES.items():
            if "@" in u: continue  # bash 版は userinfo を正規表現で弾く (下で確認)
            r = subprocess.run(["bash", "-c", fn + f'\nis_civitai "$1"', "_", u])
            self.assertEqual(r.returncode == 0, ok, u)
        r = subprocess.run(["bash", "-c", fn + '\nis_civitai "$1"', "_", "https://user@civitai.com/x"]); self.assertNotEqual(r.returncode, 0)


class T7_RequiredFiles(Base):
    def req(self):
        code = ("import sys,json;sys.path.insert(0,'%s');import pod_ssh;print(json.dumps(pod_ssh.required_files()))" % os.path.join(self.root, "runpod"))
        return subprocess.run([os.path.join(PAYLOAD, ".venv", "bin", "python"), "-c", code], env=self.env, capture_output=True, text=True)
    def test_vae_follows_config(self):
        cfg = json.load(open(self.cfgp)); cfg.update(checkpoint="m.safetensors", checkpoint_url="https://huggingface.co/a/b/resolve/main/m.safetensors", vae="other_vae.safetensors")
        json.dump(cfg, open(self.cfgp, "w"))
        r = self.req(); self.assertNotEqual(r.returncode, 0); self.assertIn("SDXL の中身で代用しない", r.stdout + r.stderr)
        cfg.update(vae_url="https://huggingface.co/x/y/resolve/main/other_vae.safetensors", vae_sha256="ab" * 32); json.dump(cfg, open(self.cfgp, "w"))
        r = self.req(); self.assertEqual(r.returncode, 0, r.stderr)
        vae = [x for x in json.loads(r.stdout) if x[0] == "vae"][0]
        self.assertEqual(vae[2], cfg["vae_url"]); self.assertEqual(vae[4], "ab" * 32)
    def test_loras_from_config_included(self):
        cfg = json.load(open(self.cfgp)); cfg.update(checkpoint="m.safetensors", checkpoint_url="https://civitai.com/api/download/models/9",
                                                     loras=[{"file": "l.safetensors", "url": "https://civitai.com/api/download/models/5", "sha256": "cd" * 32}])
        json.dump(cfg, open(self.cfgp, "w"))
        r = self.req(); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x[0] for x in json.loads(r.stdout)], ["checkpoints", "vae", "loras"])


class T9_BoxWatchdog(Base):
    def test_background_watchdog_deletes_at_deadline(self):
        import time
        self.env["COMFYLAB_WATCHDOG_INTERVAL"] = "0.3"
        other = self.S.add_pod("not-ours")
        code, out = self.rp("create", "--hours", "0.0003", "--max-minutes", "0.02", "--yes")  # 期限 1.2 秒
        self.assertEqual(code, 0, out); self.assertIn("見張りを起動", out)
        for _ in range(60):
            if len(self.S.pods) == 1: break
            time.sleep(0.2)
        self.assertEqual(list(self.S.pods), [other], "期限で自分の Pod だけ消える")
        time.sleep(1)
        log = open(os.path.join(self.root, "state", "watchdog.log")).read()
        self.assertIn("期限切れのため削除", log); self.assertIn("見張り終了", log)
        self.assertNotIn(FAKE, log)


def _jobs(d):
    return sorted(f for f in glob.glob(os.path.join(d, "jobs", "*.json")) if not f.endswith(".workflow.json"))


class PodHelp(Base):
    CR = ("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")

    def edit_reg(self, pid, **kw):
        p = os.path.join(self.root, "state", "pods.json"); r = json.load(open(p)); r["pods"][pid].update(kw); json.dump(r, open(p, "w"))


class T10_DeleteRetry(PodHelp):

    def test_immediate_delete_failure_left_for_watchdog(self):
        self.S.created_cost = 1.5; self.S.delete_fail = 1
        code, out = self.rp(*self.CR)
        self.assertEqual(code, 7, out); self.assertIn("見張りが削除を再試行", out)
        pid = next(iter(self.S.pods))
        rec = self.reg["pods"][pid]
        self.assertEqual(rec["status"], "active", "削除失敗の Pod を管理から外さない")
        self.assertLessEqual(rec["deadline"], time.time())
        code, out = self.rp("watchdog", "--once")
        self.assertNotIn(pid, self.S.pods, "見張りが削除を再試行する")
        self.assertEqual(self.reg["pods"][pid]["status"], "deleted")

    def test_create_spawns_watchdog_even_when_rejected(self):
        src = open(os.path.join(self.root, "runpod", "runpod_pod.py")).read()
        i = src.index("ok = vet_pod(pod[\"id\"]"); j = src.index("if not ok:", i)
        self.assertIn("spawn_watchdog()", src[i:j], "却下より前に見張りを起動")

    def test_list_omission_is_not_deletion(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.list_omit = {pid}
        for cmd in (("watchdog", "--once"), ("list",)):
            code, out = self.rp(*cmd)
            self.assertEqual(self.reg["pods"][pid]["status"], "active", f"{cmd}: 一覧に一度出ないだけで削除済みにしない\n{out}")
        self.assertIn(("GET", f"/v2/pods/{pid}"), self.S.calls, "個別取得で確認")
        self.S.get_fail = {pid}
        self.rp("watchdog", "--once")
        self.assertEqual(self.reg["pods"][pid]["status"], "active", "個別取得も失敗なら判断を保留")

    def test_deadline_delete_failure_retried(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.edit_reg(pid, deadline=0); self.S.delete_fail = 1
        self.rp("watchdog", "--once")
        self.assertIn(pid, self.S.pods); self.assertEqual(self.reg["pods"][pid]["status"], "active")
        self.rp("watchdog", "--once")
        self.assertNotIn(pid, self.S.pods)


class T11_SingleCharge(PodHelp):
    def used(self): return json.load(open(self.modelsp))["budget"]["used_usd"]

    def test_manual_delete_charged_once(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.edit_reg(pid, created=time.time() - 600)
        self.rp("delete", pid); u1 = self.used()
        self.assertAlmostEqual(u1, 0.2 + 0.27 / 6, places=3)
        self.rp("watchdog", "--once"); self.rp("list")
        self.assertEqual(self.used(), u1, "二重加算しない")
        b = json.load(open(self.modelsp))["budget"]
        self.assertEqual(list(b["charged"]), [pid]); self.assertFalse(b["last_session"]["ended_estimated"])

    def test_pod_side_deletion_charged_once_and_estimated(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.edit_reg(pid, created=time.time() - 600)
        del self.S.pods[pid]  # Pod 内タイマーが先に削除
        self.rp("list"); u1 = self.used()
        self.assertGreater(u1, 0.2 + 0.04, "Pod 側で消えた分も未加算にしない")
        self.rp("watchdog", "--once"); self.rp("list")
        self.assertEqual(self.used(), u1)
        rec = self.reg["pods"][pid]
        self.assertEqual(rec["status"], "gone"); self.assertTrue(rec["ended_estimated"]); self.assertIn("推定", rec["end_note"])
        self.assertTrue(json.load(open(self.modelsp))["budget"]["last_session"]["ended_estimated"])
        self.assertEqual(json.load(open(self.modelsp))["budget"]["running"], {})

    def test_gone_after_deadline_capped_estimate(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        now = time.time(); self.edit_reg(pid, created=now - 7200, deadline=now - 3600)
        del self.S.pods[pid]
        self.rp("watchdog", "--once")
        mins = self.reg["pods"][pid]["minutes"]
        self.assertLess(mins, 65, "期限 + 余裕までで推定 (今まで全部は数えない)"); self.assertGreater(mins, 59)

    def test_deadline_delete_then_list_no_double(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.edit_reg(pid, deadline=0)
        self.rp("watchdog", "--once"); u1 = self.used()
        self.rp("list"); self.rp("watchdog", "--once")
        code, out = self.rp("delete", pid)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.used(), u1)


class T12_Recheck(Base):
    def test_actual_price_under_cap_but_window_exceeds_budget(self):
        self.set_budget(1.0, 0.85); self.S.created_cost = 0.5  # 上限 0.8/h 以内、30分で 0.25 > 残り 0.15
        code, out = self.rp("create", "--gpu-type", "NVIDIA RTX A5000", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 7, out); self.assertIn("予算の残り", out)
        self.assertEqual(self.S.pods, {}, "そのまま続行しない")

    def test_reconcile_rechecks_price_and_budget(self):
        self.set_budget(1.0, 0.85); self.S.created_cost = 0.5
        self.S.create_mode = "lost_after_create"; self.S.hide_new_pod_lists = 100
        code, out = self.rp("create", "--gpu-type", "NVIDIA RTX A5000", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.assertEqual(code, 6, out)
        self.S.hide_new_pod_lists = 0
        code, out = self.rp("reconcile")
        self.assertIn("予算の残り", out)
        self.assertEqual(self.S.pods, {}, "reconcile 後も再確認して削除")
        self.assertFalse([p for p in self.reg["pods"].values() if p["status"] == "active"])

    def test_reconcile_rechecks_hourly_cap(self):
        self.S.created_cost = 1.5
        self.S.create_mode = "lost_after_create"; self.S.hide_new_pod_lists = 100
        self.rp("create", "--hours", "0.25", "--max-minutes", "30", "--yes", "--no-watchdog")
        self.S.hide_new_pod_lists = 0
        code, out = self.rp("watchdog", "--once")
        self.assertEqual(self.S.pods, {}, "見張りの reconcile でも単価上限を再確認")


class T13_ModelsLock(Base):
    def up(self, *a):
        return subprocess.run([sys.executable, os.path.join(self.root, "model-map", "update_models.py"), *a], capture_output=True, text=True, env=self.env)

    def test_cannot_assign_whole_budget(self):
        m = json.load(open(self.modelsp)); m["budget"]["running"] = {"podA": {"cost_per_hr": 0.5}}; json.dump(m, open(self.modelsp, "w"))
        for arg in ('budget={"total_usd": 5}', "budget.running.podA=null", "budget.charged={}"):
            r = self.up(arg)
            self.assertNotEqual(r.returncode, 0, arg)
        b = json.load(open(self.modelsp))["budget"]
        self.assertEqual(b["used_usd"], 0.2); self.assertIn("podA", b["running"])
        self.assertEqual(self.up("budget.total_usd=3").returncode, 0)

    def test_all_writers_share_lock(self):
        src = {f: open(os.path.join(self.root, f)).read() for f in ("model-map/update_models.py", "runpod/pod_ssh.py")}
        self.assertIn("rp.update_models(", src["model-map/update_models.py"])
        self.assertIn("rp.update_models(", src["runpod/pod_ssh.py"])
        self.assertNotIn("save_json(rp.MODELS_JSON", src["runpod/pod_ssh.py"])

    def test_concurrent_updates_not_lost(self):
        procs = []
        for i in range(12):
            procs.append(subprocess.Popen([sys.executable, os.path.join(self.root, "runpod", "runpod_pod.py"), "record-spend", "--usd", "0.01"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            procs.append(subprocess.Popen([sys.executable, os.path.join(self.root, "model-map", "update_models.py"), f"settings.n{i}={i}"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for p in procs:
            self.assertEqual(p.wait(60), 0)
        m = json.load(open(self.modelsp))
        self.assertAlmostEqual(m["budget"]["used_usd"], 0.32, places=4)
        self.assertEqual(sum(1 for i in range(12) if m["settings"].get(f"n{i}") == i), 12)


class T14_Recovery(Base):
    def gen(self, d, *extra):
        return self.cr("--set", "6.text=x", "--seed", "fixed:5", "--out", d, "--poll-interval", "0.05", "--timeout", "3", *extra)

    def test_history_gone_but_job_has_image_source(self):
        d = os.path.join(self.root, "out", "r", "a"); self.S.view_fail = 5
        self.gen(d)
        jp = _jobs(d)[0]; self.assertTrue(json.load(open(jp))["images"])
        self.S.view_fail = 0; self.S.history_fn = lambda pid, seed: None  # ComfyUI 再起動で履歴が消えた
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        j = json.load(open(jp)); self.assertEqual(j["status"], "complete")
        self.assertTrue(all(os.path.exists(i["saved_path"]) for i in j["images"]))

    def test_error_still_retrieves_finished_outputs(self):
        self.S.history_fn = lambda pid, seed: {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {}]]},
                                               "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}}}
        d = os.path.join(self.root, "out", "r", "b")
        code, out = self.gen(d)
        self.assertNotEqual(code, 0)
        j = json.load(open(_jobs(d)[0]))
        self.assertEqual(j["status"], "error"); self.assertTrue(j["images"][0]["retrieved"]); self.assertTrue(os.path.exists(j["images"][0]["saved_path"]))

    def test_same_name_in_different_subfolders(self):
        self.S.history_fn = lambda pid, seed: {"status": {"status_str": "success", "completed": True}, "outputs": {
            "9": {"images": [{"filename": "a.png", "subfolder": "x", "type": "output"}, {"filename": "a.png", "subfolder": "y", "type": "output"},
                             {"filename": "a.png", "subfolder": "", "type": "output"}]}}}
        d = os.path.join(self.root, "out", "r", "c")
        code, out = self.gen(d)
        self.assertEqual(code, 0, out)
        paths = [i["saved_path"] for i in json.load(open(_jobs(d)[0]))["images"]]
        self.assertEqual(len(set(paths)), 3); self.assertTrue(all(os.path.exists(p) for p in paths))

    def test_no_history_not_asserted_as_unsent(self):
        d = os.path.join(self.root, "out", "r", "d")
        self.S.history_fn = lambda pid, seed: None
        self.S.prompt_fail_after_accept = 1
        self.gen(d)
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        self.assertEqual(code, 2, out)
        self.assertNotIn("送信されていない", out); self.assertIn("判断できない", out)
        self.assertEqual(json.load(open(_jobs(d)[0]))["status"], "not_found")


class T15_PodTimer(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(PAYLOAD, "runpod"))
        import pod_ssh
        self.ps = pod_ssh
        self.d = tempfile.mkdtemp(prefix="cltimer-")
        self.S = os.path.join(self.d, "comfylab_watchdog.sh")
        open(self.S, "w").write(pod_ssh.WATCHDOG_SH)
        self.kids = []

    def tearDown(self):
        subprocess.run(["bash", "-c", self.ps.timer_ctl("stop", d=self.d)], capture_output=True)
        for k in self.kids:
            k.kill()
        shutil.rmtree(self.d, ignore_errors=True)

    def sh(self, action, deadline=None):
        r = subprocess.run(["bash", "-c", self.ps.timer_ctl(action, deadline, d=self.d)], capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout

    def pid(self): return int(open(os.path.join(self.d, "comfylab_watchdog.pid")).read())

    def test_no_name_matching(self):
        src = open(os.path.join(PAYLOAD, "runpod", "pod_ssh.py")).read()
        self.assertNotIn("pkill", src.split("def timer_ctl")[1].split("def main")[0].replace("pkill/pgrep", ""))
        self.assertNotRegex(src, r"(pkill|pgrep) -f")

    def test_start_check_stop_by_pid_and_cmdline(self):
        code, out = self.sh("restart", int(time.time()) + 3600)
        self.assertEqual(code, 0, out); self.assertIn("稼働中", out)
        pid = self.pid()
        argv = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
        self.assertEqual(argv[1].decode(), self.S)
        code, out = self.sh("restart", int(time.time()) + 3600)  # 再起動で古いものは止める
        self.assertIn("停止", out); self.assertFalse(os.path.exists(f"/proc/{pid}"), "古いタイマーが残らない")
        self.assertIn("稼働中", self.sh("check")[1])
        pid2 = self.pid()
        self.sh("stop"); time.sleep(0.2)
        self.assertFalse(os.path.exists(f"/proc/{pid2}") and open(f"/proc/{pid2}/stat").read().split()[2] != "Z")
        self.assertIn("なし", self.sh("check")[1])

    def test_decoy_with_name_in_cmdline_not_touched(self):
        decoy = subprocess.Popen(["bash", "-c", "sleep 30 # comfylab_watchdog.sh 99"]); self.kids.append(decoy)
        open(os.path.join(self.d, "comfylab_watchdog.pid"), "w").write(str(decoy.pid))
        self.assertIn("なし", self.sh("check")[1], "名前が含まれるだけの別プロセスを稼働中と見なさない")
        self.sh("stop"); time.sleep(0.2)
        self.assertIsNone(decoy.poll(), "別プロセスを止めない")

    def test_launching_shell_not_targeted(self):
        # 起動用シェルが、タイマーと同じ実行内容 (bash <S> <数字>) で動いていて、PID ファイルに自分の PID がある場合
        launcher = os.path.join(self.d, "launch.sh")
        open(launcher, "w").write(f'echo $$ > {self.d}/comfylab_watchdog.pid\n' + self.ps.timer_ctl("stop", d=self.d) + '\necho ALIVE-AFTER\n')
        body = open(self.S).read()
        open(self.S, "w").write(open(launcher).read())
        r = subprocess.run(["bash", self.S, "123"], capture_output=True, text=True, timeout=30)
        open(self.S, "w").write(body)
        self.assertIn("ALIVE-AFTER", r.stdout, "起動用シェル自身を止めない"); self.assertNotIn("停止", r.stdout)


class T16_ModelInfoErrors(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.join(PAYLOAD, "runpod"))
        import model_info
        self.mi = model_info

    def fake(self, code, headers=None):
        import urllib.error, email.message
        h = email.message.Message()
        for k, v in (headers or {}).items():
            h[k] = v
        mi = self.mi

        class Op:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, code, "x", h, None)
        return Op()

    def test_hf_error_codes_fail(self):
        orig = self.mi.urllib.request.build_opener
        try:
            for code in (401, 403, 404, 500, 503):
                self.mi.urllib.request.build_opener = lambda *a, c=code: self.fake(c, {"Content-Length": "123"})
                with self.assertRaises(SystemExit, msg=str(code)):
                    self.mi.huggingface("https://huggingface.co/a/b/resolve/main/x.safetensors")
            self.mi.urllib.request.build_opener = lambda *a: self.fake(302, {"X-Linked-Size": "10", "X-Linked-ETag": '"' + "a" * 64 + '"'})
            r = self.mi.huggingface("https://huggingface.co/a/b/resolve/main/x.safetensors")
            self.assertEqual((r["size"], r["sha256"]), (10, "a" * 64))
        finally:
            self.mi.urllib.request.build_opener = orig

    def test_civitai_error_codes_fail(self):
        import urllib.error
        orig = self.mi.urllib.request.urlopen
        try:
            for code in (401, 403, 404, 502):
                def boom(req, timeout=None, c=code):
                    raise urllib.error.HTTPError(req.full_url, c, "x", {}, None)
                self.mi.urllib.request.urlopen = boom
                with self.assertRaises(SystemExit) as cm:
                    self.mi.lookup("https://civitai.com/api/v1/model-versions/1")
                self.assertIn(str(code), str(cm.exception.code))
        finally:
            self.mi.urllib.request.urlopen = orig


class T17_PythonMinimum(unittest.TestCase):
    def test_same_minimum_everywhere(self):
        inst = open(os.path.join(PAYLOAD, "install.sh")).read(); rd = open(os.path.join(PAYLOAD, "README.md")).read()
        self.assertIn("sys.version_info >= (3, 11)", inst); self.assertIn("python3 3.11 以上", inst); self.assertIn("python3 3.11 以上", rd)
        self.assertNotIn("3.9 以上", rd + inst)
        for bad in ("All rights reserved", "ライセンス未設定"):
            self.assertNotIn(bad, rd)


ERR_HIST = lambda pid, seed: {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"node_id": "8"}]]},
                              "outputs": {"9": {"images": [{"filename": f"ok_{seed}.png", "subfolder": "", "type": "output"}]}}}


class T18_JobJudgement(PodHelp):
    def gen(self, d, pid, *extra):
        return self.cr("--set", "6.text=x", "--seed", "fixed:5", "--out", d, "--pod-id", pid, "--poll-interval", "0.05", "--timeout", "3", *extra)

    def test_error_all_retrieved_allows_normal_delete(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.history_fn = ERR_HIST
        d = os.path.join(self.root, "out", "j", "a"); self.gen(d, pid)
        j = json.load(open(_jobs(d)[0])); self.assertEqual((j["status"], j["gen_status"]), ("error", "error")); self.assertTrue(j["images"][0]["retrieved"])
        code, out = self.cr("--pending", os.path.join(self.root, "out"))
        self.assertEqual(code, 0, out); self.assertIn("生成=error", out)
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 0, out); self.assertNotIn(pid, self.S.pods)
        j = json.load(open(_jobs(d)[0])); self.assertIn("execution_error", j["error"]); self.assertEqual(j["status"], "error")

    def test_error_with_unretrieved_still_blocks(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.history_fn = ERR_HIST; self.S.view_fail = 5
        d = os.path.join(self.root, "out", "j", "b"); self.gen(d, pid)
        code, out = self.rp("delete", pid); self.assertEqual(code, 8, out); self.assertIn(pid, self.S.pods)
        code, out = self.cr("--pending", os.path.join(self.root, "out")); self.assertEqual(code, 1)
        self.S.view_fail = 0
        code, out = self.cr("--resume", d, "--poll-interval", "0.05"); self.assertEqual(code, 0, out)
        code, out = self.rp("delete", pid); self.assertEqual(code, 0, out)

    def test_error_kept_after_history_loss_recovery(self):
        self.S.history_fn = ERR_HIST; self.S.view_fail = 5
        d = os.path.join(self.root, "out", "j", "c"); self.gen(d, "podZ")
        self.S.view_fail = 0; self.S.history_fn = lambda pid, seed: None
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        j = json.load(open(_jobs(d)[0]))
        self.assertTrue(all(i["retrieved"] for i in j["images"]))
        self.assertEqual((j["status"], j["gen_status"]), ("error", "error"), "履歴消失後の回収でも生成エラーを失わない")
        self.assertIn("execution_error", j["error"])

    def test_running_or_unknown_blocks_delete(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        jd = os.path.join(self.root, "out", "j", "d", "jobs"); os.makedirs(jd)
        for st in ("queued", "send_unknown", "not_found"):
            json.dump({"status": st, "pod_id": pid, "images": []}, open(os.path.join(jd, "x.json"), "w"))
            code, out = self.rp("delete", pid); self.assertEqual(code, 8, st)
        self.assertIn(pid, self.S.pods)

    def test_one_shared_judgement(self):
        src = open(os.path.join(self.root, "runpod", "runpod_pod.py")).read()
        self.assertIn("from comfy_run import job_judgement", src)
        cr = open(os.path.join(self.root, "runpod", "comfy_run.py")).read()
        self.assertEqual(cr.count("job_judgement("), 4, "定義 + resume(2) + pending")


class T19_ReconcileDeadline(PodHelp):
    def setUp(self):
        super().setUp(); self.env["COMFYLAB_WATCHDOG_INTERVAL"] = "0.3"
        self.kids = []

    def tearDown(self):
        for k in self.kids: k.kill()
        super().tearDown()

    def lost_create(self):
        self.S.create_mode = "lost_after_create"; self.S.hide_new_pod_lists = 100
        code, out = self.rp(*self.CR); self.assertEqual(code, 6, out)
        p = os.path.join(self.root, "state", "pods.json"); r = json.load(open(p))
        for v in r["pending"].values(): v["requested"] = time.time() - 3600  # 30分期限はとうに過ぎた
        json.dump(r, open(p, "w")); self.S.hide_new_pod_lists = 0; self.S.create_mode = "ok"
        return next(iter(self.S.pods))

    def wd_pid(self): return int(open(os.path.join(self.root, "state", "watchdog.pid")).read())

    def is_wd(self, pid):
        try: return "watchdog" in open(f"/proc/{pid}/cmdline").read()
        except OSError: return False

    def test_reconcile_expired_is_deleted(self):
        pid = self.lost_create()
        code, out = self.rp("reconcile")
        self.assertIn("期限切れ", out); self.assertNotIn(pid, self.S.pods)
        self.assertEqual(self.reg["pods"][pid]["status"], "deleted")
        self.assertIn(pid, json.load(open(self.modelsp))["budget"]["charged"])

    def test_reconcile_expired_delete_failure_spawns_watchdog(self):
        pid = self.lost_create(); self.S.delete_fail = 1
        code, out = self.rp("reconcile")
        self.assertIn("見張りが削除を再試行", out)
        for _ in range(60):
            if pid not in self.S.pods: break
            time.sleep(0.2)
        self.assertNotIn(pid, self.S.pods, "起動した見張りが削除を再試行")

    def test_stale_watchdog_pid_after_reboot_replaced(self):
        code, out = self.rp(*self.CR)
        decoy = subprocess.Popen(["sleep", "60"]); self.kids.append(decoy)
        open(os.path.join(self.root, "state", "watchdog.pid"), "w").write(str(decoy.pid))  # 再起動後に別プロセスが同じ PID
        code, out = self.rp("reconcile")
        self.assertIn("見張りを起動", out)
        self.assertNotEqual(self.wd_pid(), decoy.pid); self.assertTrue(self.is_wd(self.wd_pid()))
        os.kill(self.wd_pid(), 9); time.sleep(0.2)
        open(os.path.join(self.root, "state", "watchdog.pid"), "w").write("999999")  # 存在しない PID (再起動で消えた)
        code, out = self.rp("reconcile"); self.assertIn("見張りを起動", out)

    def test_alive_watchdog_not_duplicated(self):
        code, out = self.rp(*self.CR)
        self.rp("reconcile"); p1 = self.wd_pid(); self.assertTrue(self.is_wd(p1))
        code, out = self.rp("reconcile"); self.assertNotIn("見張りを起動", out); self.assertEqual(self.wd_pid(), p1)
        n = 0
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    c = open(f"/proc/{d}/cmdline").read()
                    if self.root in c and "watchdog" in c: n += 1
                except OSError: pass
        self.assertEqual(n, 1)


class T20_LastSeen(PodHelp):
    def test_gone_end_not_before_last_seen_past_deadline(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        now = time.time(); self.edit_reg(pid, created=now - 7200, deadline=now - 3600)
        self.rp("list")  # 期限後も存在していたことを確認
        self.assertGreater(self.reg["pods"][pid]["last_seen"], now - 5)
        del self.S.pods[pid]
        self.rp("watchdog", "--once")
        rec = self.reg["pods"][pid]
        self.assertGreaterEqual(rec["minutes"], 119.9, "確認済みの期限後の稼働を費用に含める")
        self.assertTrue(rec["ended_estimated"]); self.assertIn("推定", rec["end_note"])
        self.assertGreaterEqual(json.load(open(self.modelsp))["budget"]["used_usd"], 0.2 + 0.27 * 2 - 0.001)

    def test_estimate_not_before_recorded_last_seen(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        now = time.time(); self.edit_reg(pid, created=now - 4000, deadline=now - 3000, last_seen=now - 1000)
        del self.S.pods[pid]
        self.rp("watchdog", "--once")
        self.assertGreaterEqual(self.reg["pods"][pid]["minutes"], 49.9)

    def test_last_seen_from_status_and_individual_get(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.edit_reg(pid, last_seen=0)
        self.rp("status", pid); s1 = self.reg["pods"][pid]["last_seen"]; self.assertGreater(s1, 0)
        time.sleep(0.05); self.S.list_omit = {pid}
        self.rp("watchdog", "--once"); self.assertGreater(self.reg["pods"][pid]["last_seen"], s1, "個別取得での確認も記録")


OK_HIST = lambda pid, seed: {"status": {"status_str": "success", "completed": True},
                             "outputs": {"9": {"images": [{"filename": f"ok_{seed}.png", "subfolder": "", "type": "output"}]}}}
ZERO_HIST = lambda pid, seed: {"status": {"status_str": "success", "completed": True}, "outputs": {}}
ZERO_ERR = lambda pid, seed: {"status": {"status_str": "error", "completed": False, "messages": [["execution_error", {}]]}, "outputs": {}}


class T21_OutputsConfirmed(PodHelp):
    def gen(self, d, pid, crash=None):
        env = dict(self.env)
        if crash: env["COMFYLAB_TEST_CRASH"] = crash
        r = subprocess.run([sys.executable, os.path.join(self.root, "runpod", "comfy_run.py"), "--set", "6.text=x", "--seed", "fixed:5", "--out", d,
                            "--pod-id", pid, "--poll-interval", "0.05", "--timeout", "3"], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def old_order_crash_record(self, d, pid, gen_status):
        """旧版の順序で gen_status 保存直後 (出力一覧の記録前) に中断した記録を、実際に送った prompt で作る。"""
        self.S.view_fail = 0; self.S.prompt_fail_after_accept = 1  # 送信は受理されたが応答なし -> 記録は prompt_id 付きで残る
        self.gen(d, pid)
        jp = _jobs(d)[0]; j = json.load(open(jp))
        j.update(status="error" if gen_status == "error" else "running", gen_status=gen_status, images=[])
        j.pop("outputs_known", None)
        if gen_status == "error": j["error"] = "[[\"execution_error\", {}]]"
        json.dump(j, open(jp, "w"))
        return jp

    def check_blocked_then_recovered(self, d, pid, jp, hist):
        code, out = self.cr("--pending", os.path.join(self.root, "out"))
        self.assertEqual(code, 1, "出力一覧が未確認のジョブは未完了として出す\n" + out); self.assertIn("回収=unknown", out)
        code, out = self.rp("delete", pid); self.assertEqual(code, 8, "未確認のまま Pod 削除を許可しない\n" + out)
        self.S.history_fn = hist
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        j = json.load(open(jp))
        self.assertTrue(j["outputs_known"]); self.assertTrue(j["images"]); self.assertTrue(all(i["retrieved"] and os.path.exists(i["saved_path"]) for i in j["images"]))
        code, out = self.rp("delete", pid); self.assertEqual(code, 0, out)
        return j

    def test_old_order_crash_success_not_settled(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        d = os.path.join(self.root, "out", "k", "a"); jp = self.old_order_crash_record(d, pid, "success")
        j = self.check_blocked_then_recovered(d, pid, jp, OK_HIST)
        self.assertEqual(j["status"], "complete")

    def test_old_order_crash_error_not_settled(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        d = os.path.join(self.root, "out", "k", "b"); jp = self.old_order_crash_record(d, pid, "error")
        j = self.check_blocked_then_recovered(d, pid, jp, ERR_HIST)
        self.assertEqual((j["status"], j["gen_status"]), ("error", "error")); self.assertIn("execution_error", j["error"])

    def crash_after_confirm(self, hist, sub):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.history_fn = hist
        d = os.path.join(self.root, "out", "k", sub)
        code, out = self.gen(d, pid, crash="after_confirm")
        self.assertEqual(code, 70, out)
        jp = _jobs(d)[0]; j = json.load(open(jp))
        self.assertTrue(j["outputs_known"]); self.assertEqual(len(j["images"]), 1); self.assertFalse(j["images"][0]["retrieved"])
        self.assertIn("gen_status", j, "成否と出力一覧が同じ保存で確定")
        code, out = self.rp("delete", pid); self.assertEqual(code, 8, out)
        self.S.history_fn = lambda pid, seed: None  # 再開時に履歴が消えていても
        code, out = self.cr("--resume", d, "--poll-interval", "0.05")
        self.assertEqual(code, 0, out)
        j = json.load(open(jp)); self.assertTrue(j["images"][0]["retrieved"]); self.assertTrue(os.path.exists(j["images"][0]["saved_path"]))
        code, out = self.rp("delete", pid); self.assertEqual(code, 0, out)
        return j

    def test_crash_after_confirm_success_resume_retrieves(self):
        j = self.crash_after_confirm(OK_HIST, "c"); self.assertEqual((j["status"], j["gen_status"]), ("complete", "success"))

    def test_crash_after_confirm_error_resume_retrieves(self):
        j = self.crash_after_confirm(ERR_HIST, "d"); self.assertEqual((j["status"], j["gen_status"]), ("error", "error"))

    def test_confirmed_zero_outputs_settles(self):
        for hist, sub, want in ((ZERO_HIST, "e", "complete"), (ZERO_ERR, "f", "error")):
            code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
            self.S.history_fn = hist
            d = os.path.join(self.root, "out", "k", sub); self.gen(d, pid)
            j = json.load(open(_jobs(d)[0]))
            self.assertEqual((j["status"], j["outputs_known"], j["images"]), (want, True, []))
            code, out = self.cr("--pending", os.path.join(self.root, "out")); self.assertEqual(code, 0, out)
            code, out = self.cr("--resume", d, "--poll-interval", "0.05"); self.assertEqual(code, 0, out)
            code, out = self.rp("delete", pid); self.assertEqual(code, 0, f"{sub}: 出力0枚を確認済みなら削除できる\n{out}")

    def test_legacy_records_without_flag(self):
        sys.path.insert(0, os.path.join(self.root, "runpod"))
        import comfy_run as c
        J = c.job_judgement
        self.assertFalse(J({"status": "complete", "images": []})["settled"], "旧記録 images=[] は未確認")
        self.assertFalse(J({"status": "error", "gen_status": "error", "images": []})["settled"])
        self.assertEqual(J({"status": "running", "gen_status": "success", "images": []})["retrieval"], "unknown")
        self.assertTrue(J({"status": "complete", "images": [{"filename": "a", "retrieved": True}]})["settled"], "旧版は一覧をまとめて保存していた")
        self.assertFalse(J({"status": "partial", "images": [{"filename": "a", "retrieved": False}]})["settled"])
        self.assertTrue(J({"status": "complete", "outputs_known": True, "images": []})["settled"])
        self.assertTrue(J({"status": "abandoned", "images": []})["settled"])


FIX = os.path.join(HERE, "fixtures", "comfyui-400-validation-3.0.1-real.txt")


def real_400_body():
    """3.0.1 実Pod検証で保存した実際の拒否応答 (記録時に 500 文字で切れている)。先頭の 'HTTP 400 POST <url>: ' を除いた本文。"""
    t = open(FIX, encoding="utf-8").read()
    return t.split("/prompt: ", 1)[1].encode()


def full_400_body():
    """実際の応答と同じ構造の完全な JSON (切れた部分を閉じたもの)。"""
    return json.dumps({"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation", "details": "", "extra_info": {}},
                       "node_errors": {"4": {"errors": [{"type": "value_not_in_list", "message": "Value not in list",
                                                         "details": "ckpt_name: 'nonexistent.safetensors' not in ['waiIllustriousSDXL_v170.safetensors']",
                                                         "extra_info": {"input_name": "ckpt_name"}}], "dependent_outputs": ["9"], "class_type": "CheckpointLoaderSimple"}}}).encode()


class T22_SubmitJudgement(PodHelp):
    def gen(self, d, pid="", count=1, extra_env=None):
        env = dict(self.env, **(extra_env or {}))
        r = subprocess.run([sys.executable, os.path.join(self.root, "runpod", "comfy_run.py"), "--set", "6.text=x", "--seed", "increment:5", "--count", str(count),
                            "--out", d, "--pod-id", pid, "--poll-interval", "0.05", "--timeout", "5"], env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def judge(self):
        sys.path.insert(0, os.path.join(self.root, "runpod"))
        import comfy_run
        return comfy_run.classify_submit

    def test_fixture_has_no_secrets(self):
        t = open(FIX, encoding="utf-8").read()
        for bad in ("rpa_", "Bearer", "token", "/home/", "@"):
            self.assertNotIn(bad, t)

    def test_real_saved_400_is_validation_rejection(self):
        C = self.judge()
        self.assertEqual(C(400, real_400_body()), ("rejected", "prompt_outputs_failed_validation"))
        self.assertEqual(C(400, full_400_body()), ("rejected", "prompt_outputs_failed_validation"))

    def test_rejection_recorded_final_and_does_not_block(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.prompt_response = (400, real_400_body())
        d = os.path.join(self.root, "out", "v", "a")
        code, out = self.gen(d, pid, count=3)
        self.assertNotEqual(code, 0, "生成コマンドは失敗を返す"); self.assertIn("入力を拒否", out)
        jobs = _jobs(d); self.assertEqual(len(jobs), 1, "同じ Workflow の残りは送らない")
        j = json.load(open(jobs[0]))
        self.assertEqual((j["status"], j["gen_status"], j["outputs_known"], j["images"], j["accepted"]), ("rejected", "error", True, [], False))
        self.assertEqual(j["submit_http"]["status"], 400); self.assertEqual(j["submit_http"]["body"].encode(), real_400_body(), "HTTP 状態と応答本文を保持")
        self.assertEqual(j["submit_http"]["judgement"], "rejected")
        code, out = self.cr("--pending", os.path.join(self.root, "out")); self.assertEqual(code, 0, out)
        code, out = self.cr("--resume", d, "--poll-interval", "0.05"); self.assertEqual(code, 0, out)
        code, out = self.rp("delete", pid); self.assertEqual(code, 0, "入力拒否は通常の削除を止めない\n" + out)

    def test_400_not_validation_is_unknown_and_protected(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        C = self.judge()
        for body in (b'{"error": {"type": "something_else", "message": "x"}}', b"Bad Request", b"", b'{"prompt_id": "abc", "error": {"type": "invalid_prompt"}}'):
            self.assertEqual(C(400, body)[0], "unknown", body)
        self.S.prompt_response = (400, b"<html>400 Bad Request (proxy)</html>")
        d = os.path.join(self.root, "out", "v", "b")
        code, out = self.gen(d, pid, count=2)
        self.assertNotEqual(code, 0); self.assertIn("受理されたか不明", out)
        j = json.load(open(_jobs(d)[0])); self.assertEqual(j["status"], "send_unknown"); self.assertEqual(j["submit_http"]["status"], 400)
        self.assertEqual(len(_jobs(d)), 1, "自動で再送・次の送信をしない")
        code, out = self.rp("delete", pid); self.assertEqual(code, 8, "判定できない応答は保護を維持")

    def test_5xx_is_unknown_and_protected(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        for c in (500, 502, 503):
            self.assertEqual(self.judge()(c, b'{"error": {"type": "prompt_outputs_failed_validation"}}')[0], "unknown", c)
        self.S.prompt_response = (502, b'{"error": "bad gateway"}')
        d = os.path.join(self.root, "out", "v", "c"); code, out = self.gen(d, pid)
        self.assertNotEqual(code, 0); self.assertEqual(json.load(open(_jobs(d)[0]))["status"], "send_unknown")
        self.assertEqual(self.rp("delete", pid)[0], 8)

    def test_timeout_is_unknown_then_resume_without_resend(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        self.S.prompt_delay = 2.5
        d = os.path.join(self.root, "out", "v", "d")
        code, out = self.gen(d, pid, extra_env={"COMFY_POST_TIMEOUT": "1"})
        self.assertNotEqual(code, 0); self.assertIn("受理されたか不明", out)
        j = json.load(open(_jobs(d)[0])); self.assertEqual(j["status"], "send_unknown"); self.assertIsNone(j["submit_http"]["status"])
        self.assertEqual(self.rp("delete", pid)[0], 8)
        time.sleep(2); self.S.prompt_delay = 0
        code, out = self.cr("--resume", d, "--poll-interval", "0.05"); self.assertEqual(code, 0, out)
        self.assertEqual(len(self.S.prompts), 1, "自動再送しない (自分の prompt_id で見つけて回収)")
        self.assertEqual(json.load(open(_jobs(d)[0]))["status"], "complete")
        self.assertEqual(self.rp("delete", pid)[0], 0)

    def test_200_with_node_errors_and_prompt_id_is_accepted(self):
        self.S.accept_node_errors = {"7": {"errors": [{"type": "warning"}]}}
        d = os.path.join(self.root, "out", "v", "e"); code, out = self.gen(d)
        self.assertEqual(code, 0, out)
        j = json.load(open(_jobs(d)[0]))
        self.assertEqual(j["status"], "complete"); self.assertTrue(j["accepted"]); self.assertIn("node_errors_on_accept", j)
        self.assertEqual(self.judge()(200, b'{"prompt_id": "p1", "node_errors": {"3": {}}}')[0], "accepted")
        self.assertEqual(self.judge()(200, b'{"node_errors": {}}')[0], "unknown")

    def test_retrieval_errors_after_accept_not_rejection(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        for knob, sub in (("history_fail", "f"), ("view_fail", "g")):
            setattr(self.S, knob, 1000)
            d = os.path.join(self.root, "out", "v", sub); code, out = self.gen(d, pid)
            self.assertNotEqual(code, 0); self.assertNotIn("入力を拒否", out)
            j = json.load(open(_jobs(d)[0]))
            self.assertNotEqual(j["status"], "rejected"); self.assertTrue(j["accepted"]); self.assertEqual(j["submit_http"]["judgement"], "accepted")
            self.assertEqual(self.rp("delete", pid)[0], 8, "受付後の取得エラーでは保護を外さない")
            setattr(self.S, knob, 0)
            code, out = self.cr("--resume", d, "--poll-interval", "0.05"); self.assertEqual(code, 0, out)
        self.assertEqual(self.rp("delete", pid)[0], 0)


class T23_BlockedNotice(PodHelp):
    def test_delete_blocked_notifies_reason_running_deadline_options(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        dl = self.reg["pods"][pid]["deadline"]
        jd = os.path.join(self.root, "out", "n", "jobs"); os.makedirs(jd)
        json.dump({"status": "queued", "pod_id": pid, "images": []}, open(os.path.join(jd, "j1.json"), "w"))
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 8)
        for s in ("利用者へ通知", "理由:", "稼働中", "課金が続いて", "停止期限:", "期限は変えていません", "--resume", "--abandon", "--allow-unretrieved", "j1.json"):
            self.assertIn(s, out)
        self.assertEqual(self.reg["pods"][pid]["deadline"], dl, "期限を変えない"); self.assertIn(pid, self.S.pods)
        self.assertEqual(json.load(open(os.path.join(jd, "j1.json")))["status"], "queued", "承認なしに放棄しない")

    def test_delete_failure_notifies(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        dl = self.reg["pods"][pid]["deadline"]; self.S.delete_fail = 1
        code, out = self.rp("delete", pid)
        self.assertEqual(code, 9, out); self.assertIn("利用者へ通知", out); self.assertIn("稼働中", out)
        self.assertEqual(self.reg["pods"][pid]["status"], "active"); self.assertEqual(self.reg["pods"][pid]["deadline"], dl)


class T24_Guidance(PodHelp):
    def test_wait_next_steps_in_order(self):
        code, out = self.rp(*self.CR); pid = next(iter(self.S.pods))
        code, out = self.rp("wait", pid, "--tries", "2", "--interval", "0.1")
        self.assertEqual(code, 0, out)
        i = [out.index(s) for s in ("arm-watchdog", "setup-model", "tunnel-start")]
        self.assertEqual(i, sorted(i), "arm-watchdog → setup-model → tunnel-start の順")

    def test_vet_success_prints_confirmation(self):
        code, out = self.rp(*self.CR)
        self.assertIn("再確認OK: 実単価", out); self.assertIn("予算の残り", out)

    def test_runpodctl_config_warning_explained(self):
        src = open(os.path.join(self.root, "runpod", "pod_ssh.py")).read()
        self.assertIn("grep -iv 'config file'", src); self.assertIn("キーを保存していないという意味", src)
        self.assertNotIn("runpodctl version 2>&1", src)


class T25_Desktop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cldesk-")
        shutil.copytree(PAYLOAD, os.path.join(self.tmp, "cl"), ignore=shutil.ignore_patterns(".venv", "out", "state", "tests", "__pycache__", ".backup"))
        self.D = os.path.join(self.tmp, "cl", "model-map")
        import socket
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = str(s.getsockname()[1]); s.close()
        self.env = dict(os.environ, PORT=self.port)
        self.kids = []

    def tearDown(self):
        self.sh("stop-desktop.sh")
        for k in self.kids: k.kill(); k.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, name):
        r = subprocess.run(["bash", os.path.join(self.D, name)], env=self.env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def pid(self, f): return int(open(os.path.join(self.tmp, "cl", "state", f)).read())

    def test_start_twice_then_stop(self):
        code, out = self.sh("start-desktop.sh"); self.assertIn("サーバー起動", out); self.assertIn("表示OK", out)
        sp, ip = self.pid("desktop-server.pid"), self.pid("desktop-index.pid")
        self.assertEqual(os.readlink(f"/proc/{sp}/cwd"), self.D)
        code, out = self.sh("start-desktop.sh"); self.assertIn(f"稼働中 (port {self.port}, pid {sp})", out)
        code, out = self.sh("stop-desktop.sh"); self.assertIn("停止", out); self.assertIn("空き", out)
        time.sleep(0.3)
        for p in (sp, ip):
            self.assertFalse(os.path.exists(f"/proc/{p}") and open(f"/proc/{p}/stat").read().split()[2] != "Z")

    def test_other_server_on_port_not_mistaken_or_stopped(self):
        other = subprocess.Popen([sys.executable, "-m", "http.server", self.port, "--directory", self.tmp], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.kids.append(other); time.sleep(0.7)
        code, out = self.sh("start-desktop.sh")
        self.assertEqual(code, 1, out); self.assertIn("別のプロセスが使っている", out); self.assertNotIn("サーバー: 稼働中", out)
        open(os.path.join(self.tmp, "cl", "state", "desktop-server.pid"), "w").write(str(other.pid))  # PID ファイルが別プロセスを指していても
        code, out = self.sh("stop-desktop.sh")
        self.assertIsNone(other.poll(), "別のサーバーを止めない"); self.assertIn("触れない", out)

    def test_no_name_matching(self):
        for f in ("start-desktop.sh", "stop-desktop.sh", "desktop_lib.sh"):
            src = open(os.path.join(self.D, f)).read()
            self.assertNotIn("pgrep", src); self.assertNotIn("pkill", src)


class T8_Dashboard(unittest.TestCase):
    def test_overrun_and_running_text(self):
        js = open(os.path.join(HERE, "dashboard_budget_test.js")).read()
        r = subprocess.run(["node", "-e", js, os.path.join(PAYLOAD, "model-map", "index.html")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        print(r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
