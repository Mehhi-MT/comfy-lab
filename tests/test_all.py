"""模擬確認 (実通信なし)。python3 -m unittest -v test_all  (導入先で: .venv/bin/python -m unittest -v tests/test_all.py)"""
import glob, hashlib, json, os, shutil, subprocess, sys, tempfile, threading, unittest
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


class T8_Dashboard(unittest.TestCase):
    def test_overrun_and_running_text(self):
        js = open(os.path.join(HERE, "dashboard_budget_test.js")).read()
        r = subprocess.run(["node", "-e", js, os.path.join(PAYLOAD, "model-map", "index.html")], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        print(r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
