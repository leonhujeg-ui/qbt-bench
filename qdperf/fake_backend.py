"""标准库 http.server 的假后端（G4 自检用）：每条路径可配延迟分布 / 抖动 / 错误率 / 并发上限，只监听 127.0.0.1。

    python3.12 -m qdperf.fake_backend --config backend.json [--port 0] [--ready-file F] [--seconds 0]
    → stdout 第一行：FAKE_BACKEND LISTENING http://127.0.0.1:<端口>

配置（字段表见 CONTRACT.md §4.2）::

    {"format": "qdperf.fake_backend.v1", "seed": 1, "max_concurrency": 0,
     "routes": {"/api/orders": {"latency": {"kind": "lognormal", "p50_ms": 30, "p99_ms": 120},
                                "error_rate": 0.01, "max_concurrency": 8, "body_bytes": 256}},
     "default": {"latency": {"kind": "fixed", "ms": 1}}}

延迟分布：fixed(ms) / uniform(lo_ms, hi_ms) / lognormal(p50_ms, p99_ms)。缺省“分层抽样”：每条路径第 k 个请求取
u = (perm[k mod 1000] + 0.5) / 1000（perm 为按 seed 打乱的 0..999），延迟 = 分布的逆 CDF(u)——每连续 1000 个请求
的经验分布与目标分布的千分位完全一致，注入的 p99 是“已知”的；schedule="random" 时改用固定 seed 的伪随机数。
错误同样分层：第 k 个请求当 perm2[k mod 1000] < error_rate·1000 时返回 500。
并发上限：路径级（routes.*.max_concurrency）与全局（max_concurrency）信号量，超出的请求排队等待（等待计入延迟）。
特殊路径：/__health（立即 200）、/__stats（各路径已处理请求数）。
"""
import argparse
import http.server
import json
import math
import os
import random
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time

from qdperf import common

FORMAT = "qdperf.fake_backend.v1"
BLOCK = 1000
Z99 = statistics.NormalDist().inv_cdf(0.99)


class ConfigError(ValueError):
    pass


def quantile(lat, u):
    """延迟分布的逆 CDF（秒）；u ∈ (0, 1)。"""
    kind = lat.get("kind")
    if kind == "fixed":
        return lat["ms"] / 1000.0
    if kind == "uniform":
        return (lat["lo_ms"] + u * (lat["hi_ms"] - lat["lo_ms"])) / 1000.0
    if kind == "lognormal":
        mu = math.log(lat["p50_ms"])
        sigma = (math.log(lat["p99_ms"]) - mu) / Z99
        return math.exp(mu + sigma * statistics.NormalDist().inv_cdf(u)) / 1000.0
    raise ConfigError(f"unknown latency kind {kind!r}")


def check_latency(lat):
    if not isinstance(lat, dict):
        raise ConfigError("latency must be an object")
    need = {"fixed": ("ms",), "uniform": ("lo_ms", "hi_ms"), "lognormal": ("p50_ms", "p99_ms")}.get(lat.get("kind"))
    if need is None:
        raise ConfigError(f"unknown latency kind {lat.get('kind')!r}")
    for k in need:
        v = lat.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v < 1e6:
            raise ConfigError(f"latency.{k} must be a number in [0, 1e6)")
    if lat["kind"] == "uniform" and lat["hi_ms"] < lat["lo_ms"]:
        raise ConfigError("uniform: hi_ms < lo_ms")
    if lat["kind"] == "lognormal" and not 0 < lat["p50_ms"] <= lat["p99_ms"]:
        raise ConfigError("lognormal: need 0 < p50_ms <= p99_ms")


def load_config(cfg):
    if not isinstance(cfg, dict) or cfg.get("format") != FORMAT:
        raise ConfigError(f"config.format must be {FORMAT!r}")
    routes = cfg.get("routes")
    if not isinstance(routes, dict) or not all(isinstance(k, str) and k.startswith("/") for k in routes):
        raise ConfigError("config.routes must map '/path' -> route")
    out = {"seed": cfg.get("seed", 1), "max_concurrency": cfg.get("max_concurrency", 0), "routes": {}}
    for path, r in list(routes.items()) + [("*", cfg.get("default", {"latency": {"kind": "fixed", "ms": 0}}))]:
        if not isinstance(r, dict):
            raise ConfigError(f"route {path} must be an object")
        check_latency(r.get("latency", {"kind": "fixed", "ms": 0}) if "latency" in r else {"kind": "fixed", "ms": 0})
        er = r.get("error_rate", 0.0)
        if isinstance(er, bool) or not isinstance(er, (int, float)) or not 0 <= er <= 1:
            raise ConfigError(f"route {path}: error_rate must be in [0, 1]")
        bb = r.get("body_bytes", 64)
        if isinstance(bb, bool) or not isinstance(bb, int) or bb < 0:
            raise ConfigError(f"route {path}: body_bytes must be an int >= 0")
        if r.get("schedule", "stratified") not in ("stratified", "random"):
            raise ConfigError(f"route {path}: schedule must be 'stratified' or 'random'")
        mc = r.get("max_concurrency", 0)
        if isinstance(mc, bool) or not isinstance(mc, int) or mc < 0:
            raise ConfigError(f"route {path}: max_concurrency must be an int >= 0")
        out["routes"][path] = {"latency": r.get("latency", {"kind": "fixed", "ms": 0}), "error_rate": er,
                               "max_concurrency": mc, "body_bytes": r.get("body_bytes", 64),
                               "schedule": r.get("schedule", "stratified")}
    return out


class Route:
    """一条路径的状态：分层抽样表、计数器、并发信号量。线程安全。"""

    def __init__(self, path, spec, seed):
        self.path, self.spec = path, spec
        rnd = random.Random(f"{seed}:{path}")
        self.perm = list(range(BLOCK))
        rnd.shuffle(self.perm)
        self.perm_err = list(range(BLOCK))
        rnd.shuffle(self.perm_err)
        self.rng = random.Random(f"{seed}:{path}:random")
        self.count = 0
        self.lock = threading.Lock()
        self.sem = threading.BoundedSemaphore(spec["max_concurrency"]) if spec["max_concurrency"] else None

    def draw(self):
        """→ (延迟秒, 是否错误, 序号)。"""
        with self.lock:
            k = self.count
            self.count += 1
            u = self.rng.random() if self.spec["schedule"] == "random" else (self.perm[k % BLOCK] + 0.5) / BLOCK
            err = self.perm_err[k % BLOCK] < self.spec["error_rate"] * BLOCK
        return quantile(self.spec["latency"], min(max(u, 1e-9), 1 - 1e-9)), err, k


class Backend:
    def __init__(self, config):
        self.cfg = load_config(config)
        self.routes = {p: Route(p, s, self.cfg["seed"]) for p, s in self.cfg["routes"].items()}
        mc = self.cfg["max_concurrency"]
        self.sem = threading.BoundedSemaphore(mc) if mc else None

    def route(self, path):
        path = path.split("?", 1)[0]
        if path in self.routes:
            return self.routes[path]
        best = max((p for p in self.routes if p != "*" and path.startswith(p.rstrip("/") + "/")), key=len, default=None)
        return self.routes[best] if best else self.routes["*"]

    def stats(self):
        return {p: r.count for p, r in self.routes.items()}


def make_handler(backend):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        wbufsize = 1 << 16                  # 头和体攒进同一个缓冲、一次写出（handle_one_request 结束时 flush）
        disable_nagle_algorithm = True      # TCP_NODELAY：否则 keep-alive 上“头 / 体分两次小写”撞上对端延迟 ACK，每个响应多 ~40 ms

        def log_message(self, *_a):
            pass

        def _reply(self, code, obj, size=0):
            body = json.dumps(obj).encode()
            if len(body) + 9 <= size:                   # 补一个 pad 字段至少多 9 字节（,"pad":""）；差不到 9 字节就不补（宁短勿长）
                body = body[:-1] + b',"pad":"' + b"x" * (size - len(body) - 9) + b'"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            if self.path == "/__health":
                return self._reply(200, {"ok": True})
            if self.path == "/__stats":
                return self._reply(200, backend.stats())
            r = backend.route(self.path)
            delay, err, k = r.draw()
            for sem in (backend.sem, r.sem):
                if sem:
                    sem.acquire()
            try:
                time.sleep(delay)
            finally:
                for sem in (r.sem, backend.sem):
                    if sem:
                        sem.release()
            if err:
                return self._reply(500, {"ok": False, "route": r.path, "n": k})
            self._reply(200, {"ok": True, "route": r.path, "n": k, "method": self.command}, r.spec["body_bytes"])

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def serve(config, port=0):
    """在当前进程里起服务器（后台线程）→ (server, base_url)。用完 server.shutdown(); server.server_close()。"""
    srv = Server(("127.0.0.1", port), make_handler(Backend(config)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def start(config, timeout=20.0):
    """在独立子进程里起假后端（与压测客户端不抢同一个 GIL）→ (Popen, base_url)。"""
    work = common.scratch_dir("qdperf-backend-")
    cfg_path, ready = os.path.join(work, "cfg.json"), os.path.join(work, "ready")
    common.write_json(cfg_path, config)
    proc = subprocess.Popen([sys.executable, "-m", "qdperf.fake_backend", "--config", cfg_path, "--ready-file", ready],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)     # 只有一行 LISTENING，不会写满管道
    proc.qdperf_work = work
    deadline = time.monotonic() + timeout
    while not os.path.exists(ready):
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            out, err = proc.communicate()                   # 原因在子进程 stdout（FAKE_BACKEND ERROR ...）或 stderr
            raise RuntimeError("fake backend failed to start: " + ((out or "") + (err or "")).strip()[-500:])
        time.sleep(0.02)
    with open(ready, encoding="utf-8") as f:
        url = f.read().strip()
    return proc, url


def stop(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(10)
    for pipe in (proc.stdout, proc.stderr):
        if pipe:
            pipe.close()
    shutil.rmtree(getattr(proc, "qdperf_work", ""), ignore_errors=True)


def _on_term(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3.12 -m qdperf.fake_backend", description="假后端（只监听 127.0.0.1）")
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--ready-file", default=None)
    ap.add_argument("--seconds", type=float, default=0.0, help="> 0 时运行这么久后自动退出")
    a = ap.parse_args(argv)
    common.child_init()
    signal.signal(signal.SIGTERM, _on_term)                 # terminate() 时也走 finally（关服务器、写覆盖率）
    try:
        srv, url = serve(common.read_json(a.config), a.port)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"FAKE_BACKEND ERROR {exc}")
        return 2
    print(f"FAKE_BACKEND LISTENING {url}", flush=True)
    if a.ready_file:
        tmp = a.ready_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(url)
        os.replace(tmp, a.ready_file)
    try:
        if a.seconds > 0:
            time.sleep(a.seconds)
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
        common.child_finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
