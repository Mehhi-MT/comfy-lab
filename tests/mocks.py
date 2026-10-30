"""実通信なしの模擬 Runpod API v2 + 模擬 ComfyUI (テスト専用)。"""
import json, threading, time, uuid, re, struct, zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

def tiny_png():
    raw = b"\x00\x00\x00\x00"
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.pods = {}          # id -> pod
        self.calls = []         # (method, path)
        # 注入
        self.create_mode = "ok"     # ok | lost_after_create | lost_before_create | nostock
        self.hide_new_pod_lists = 0  # 作成後 N 回の一覧で新Podを隠す (反映遅延)
        self.created_cost = None     # 作成Podの実単価を上書き
        self.prices = {"NVIDIA RTX A5000": 0.27, "NVIDIA A40": 0.59}
        self.nostock = set()
        # ComfyUI
        self.prompts = {}       # prompt_id -> {"wf":..., "polls":0}
        self.view_fail = 0
        self.prompt_fail_after_accept = 0
    def add_pod(self, name, cost=0.5, status="RUNNING"):
        pid = uuid.uuid4().hex[:12]
        self.pods[pid] = {"id": pid, "name": name, "status": status, "cost": cost, "gpu": {"id": "NVIDIA A40", "count": 1},
                          "dataCenterId": "CA-MTL-1", "ssh": {"direct": {"host": "127.0.0.1", "port": 2222, "username": "root", "command": "ssh"}, "proxy": None},
                          "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return pid

def make_handler(S):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
            self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"null") if n else None
        def do_GET(self): self.route("GET")
        def do_POST(self): self.route("POST")
        def do_DELETE(self): self.route("DELETE")
        def route(self, m):
            p = self.path; S.calls.append((m, p.split("?")[0]))
            body = self._body() if m == "POST" else None
            if p.startswith("/v2/"):
                if self.headers.get("Authorization") != "Bearer TEST-FAKE-KEY":
                    return self._send(401, {"title": "Unauthorized", "status": 401, "detail": "bad key"})
                return self.runpod(m, p, body)
            return self.comfy(m, p, body)
        def runpod(self, m, p, body):
            path = p.split("?")[0]
            with S.lock:
                if m == "GET" and path == "/v2/pods":
                    pods = list(S.pods.values())
                    if S.hide_new_pod_lists > 0:
                        S.hide_new_pod_lists -= 1
                        pods = [x for x in pods if not x["name"].startswith("comfylab-")]
                    return self._send(200, {"pods": pods, "pagination": {"nextCursor": None, "hasNextPage": False}})
                if m == "POST" and path == "/v2/pods":
                    gid = body["gpu"]["id"]
                    if S.create_mode == "nostock" or gid in S.nostock:
                        return self._send(409, {"title": "Conflict", "status": 409, "detail": "There are no longer any instances available with the requested specifications."})
                    if S.create_mode == "lost_before_create":
                        return self._send(504, {"title": "Gateway Timeout", "status": 504, "detail": "timeout"})
                    pid = S.add_pod(body["name"], cost=S.created_cost if S.created_cost is not None else S.prices[gid])
                    S.pods[pid]["gpu"]["id"] = gid
                    S.pods[pid]["env_keys"] = sorted(body.get("env", {}))
                    S.pods[pid]["ports"] = body.get("ports")
                    if S.create_mode == "lost_after_create":
                        return self._send(504, {"title": "Gateway Timeout", "status": 504, "detail": "upstream timeout"})
                    return self._send(201, S.pods[pid])
                mm = re.fullmatch(r"/v2/pods/([^/]+)", path)
                if mm and m == "GET":
                    pod = S.pods.get(mm.group(1))
                    return self._send(200, pod) if pod else self._send(404, {"title": "Not Found", "status": 404, "detail": "x"})
                if mm and m == "DELETE":
                    if mm.group(1) in S.pods:
                        del S.pods[mm.group(1)]; return self._send(204)
                    return self._send(404, {"title": "Not Found", "status": 404, "detail": "x"})
                if m == "GET" and path == "/v2/catalog/gpus":
                    gpus = [{"id": k, "name": k, "memory": 24, "secure": True, "community": True, "price": {"secure": v, "community": v / 2},
                             "pool": None, "manufacturer": "NVIDIA", "maxCount": {"secure": 8, "community": 8},
                             **({"dataCenters": [{"id": "CA-MTL-1", "name": "CA-MTL-1", "availability": "HIGH"}]} if "include=AVAILABILITY" in p else {})}
                            for k, v in S.prices.items()]
                    return self._send(200, {"gpus": gpus})
            return self._send(404, {"title": "Not Found", "status": 404, "detail": path})
        def comfy(self, m, p, body):
            path = p.split("?")[0]
            if path == "/system_stats": return self._send(200, {"system": {}})
            if m == "POST" and path == "/prompt":
                pid = body.get("prompt_id") or str(uuid.uuid4())
                S.prompts[pid] = {"wf": body["prompt"], "polls": 0}
                if S.prompt_fail_after_accept > 0:
                    S.prompt_fail_after_accept -= 1
                    self.close_connection = True
                    return  # 受け付けたが応答を返さない
                return self._send(200, {"prompt_id": pid, "number": len(S.prompts), "node_errors": {}})
            mm = re.fullmatch(r"/history/(.+)", path)
            if mm:
                pid = mm.group(1); pr = S.prompts.get(pid)
                if not pr: return self._send(200, {})
                pr["polls"] += 1
                seed = pr["wf"]["3"]["inputs"]["seed"]
                return self._send(200, {pid: {"status": {"status_str": "success", "completed": True},
                                              "outputs": {"9": {"images": [{"filename": f"img_{seed}_{pid[:6]}.png", "subfolder": "", "type": "output"}]}}}})
            if path == "/queue": return self._send(200, {"queue_running": [], "queue_pending": []})
            if path == "/view":
                if S.view_fail > 0:
                    S.view_fail -= 1
                    return self._send(502, {"error": "bad gateway"})
                return self._send(200, raw=tiny_png(), ctype="image/png")
            return self._send(404, {})
    return H

def start(S):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(S))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
