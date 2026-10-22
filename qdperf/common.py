"""公用小件：机器信息、分位数、相对变化、文本表格 / 曲线、子进程覆盖率与审计钩子、spawn 进程工具。

分位数口径（全包统一）：线性插值（numpy 缺省 / Hyndman-Fan 第 7 型）：
    排序后 x[0..n-1]，位置 h = (n-1)·q，取 x[floor h] 与 x[ceil h] 之间线性插值。
相对变化：(new - base) / base；base == 0 时 new == 0 记 0，否则记 +inf。

子进程（multiprocessing spawn / python -m 起的进程）在入口调 child_init()：
- 环境变量 QDPERF_COV_DIR 存在时用 sys.monitoring 记录本进程执行过的 qdperf 行，退出时写入该目录（供 tools/cov.py 合并）；
- 环境变量 QDPERF_AUDIT_LOG 存在时装 sys.addaudithook，把写文件 / 删改名 / sqlite3.connect / socket.connect 事件逐行追加到该文件
  （tests/test_constraints.py 用它在运行时证明“只写 results/ 与 tempfile、只连 127.0.0.1”）。
"""
import atexit
import hashlib
import json
import math
import multiprocessing
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
RESULTS = os.path.join(PKG, "results")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


# ---- 机器信息 --------------------------------------------------------------------------
def cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def machine():
    """报告里每个数字都附这份信息：平台 / Python / CPU / 核数 / SQLite 版本 / 1 分钟负载 / 提交。"""
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"platform": platform.platform(), "python": platform.python_version(),
            "implementation": platform.python_implementation(), "cpu_model": cpu_model(),
            "cpu_count": os.cpu_count(), "sqlite_version": sqlite3.sqlite_version,
            "load_avg_1m": None if load is None else round(load, 2), "git_commit": git_commit(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}


def git_commit():
    try:
        out = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


# ---- 统计 ------------------------------------------------------------------------------
def percentile(values, q):
    """q ∈ [0, 1]；线性插值（第 7 型）。空序列返回 None。"""
    if not values:
        return None
    xs = sorted(values)
    h = (len(xs) - 1) * q
    lo = math.floor(h)
    hi = math.ceil(h)
    return xs[lo] + (xs[hi] - xs[lo]) * (h - lo)


def summarize(values, scale=1.0):
    """{n, min, p50, p95, p99, max, mean}，每个值乘 scale（例如 1000 把秒变毫秒）。"""
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None, "mean": None}
    return {"n": len(values), "min": min(values) * scale, "p50": percentile(values, 0.50) * scale,
            "p95": percentile(values, 0.95) * scale, "p99": percentile(values, 0.99) * scale,
            "max": max(values) * scale, "mean": sum(values) / len(values) * scale}


def rel_change(base, new):
    if base == 0:
        return 0.0 if new == 0 else math.inf
    return (new - base) / base


def rel_spread(values):
    """噪声幅度：(max - min) / min；少于 2 个值或 min == 0 时为 0。"""
    if len(values) < 2 or min(values) <= 0:
        return 0.0
    return (max(values) - min(values)) / min(values)


def fmt_pct(x):
    return "+inf%" if x == math.inf else f"{x * 100:+.1f}%"


# ---- 文本输出 --------------------------------------------------------------------------
def text_table(headers, rows):
    """等宽文本表（中文按 2 个字符宽计）。"""
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(_width(r[i]) for r in cells) for i in range(len(headers))]
    lines = []
    for k, r in enumerate(cells):
        lines.append("  ".join(c + " " * (widths[i] - _width(c)) for i, c in enumerate(r)).rstrip())
        if k == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _width(s):
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def sparkline(values, width=60):
    """把序列压成 width 个字符的文本曲线（8 级块字符）；全 0 / 空返回空格串。"""
    if not values:
        return ""
    step = max(1, math.ceil(len(values) / width))
    pts = [max(values[i:i + step]) for i in range(0, len(values), step)]
    top = max(pts)
    blocks = " ▁▂▃▄▅▆▇█"
    if top <= 0:
        return " " * len(pts)
    return "".join(blocks[min(8, int(round(8 * v / top)))] for v in pts)


# ---- 文件 ------------------------------------------------------------------------------
def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def scratch_dir(prefix="qdperf-"):
    """tempfile 下的新目录（工具的所有中间文件只落这里或 qdperf/results/）。"""
    return tempfile.mkdtemp(prefix=prefix)


def is_loopback_url(url):
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in LOOPBACK_HOSTS or host.startswith("127.")


# ---- 子进程：覆盖率 + 审计 -----------------------------------------------------------------
_CHILD = {"hits": None, "dumped": False}


def child_init():
    """每个 qdperf 子进程入口调用一次（幂等）。"""
    cov = os.environ.get("QDPERF_COV_DIR")
    if cov and _CHILD["hits"] is None:
        _start_child_coverage(cov)
    log = os.environ.get("QDPERF_AUDIT_LOG")
    if log and not _CHILD.get("audit"):
        install_audit(log)
        _CHILD["audit"] = True


def _start_child_coverage(cov_dir):
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    if mon.get_tool(tool) is not None:
        return                                          # 已被占用（例如在 tools/cov.py 主进程里）：不重复记
    hits = _CHILD["hits"] = {}
    prefix = PKG + os.sep

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(prefix):
            hits.setdefault(fn, set()).add(line)
        return mon.DISABLE

    mon.use_tool_id(tool, "qdperf-child-cov")
    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)
    atexit.register(child_finish)
    _CHILD["dir"] = cov_dir


def child_finish():
    """把子进程的覆盖行写到 QDPERF_COV_DIR/child-<pid>-<ns>.json（幂等）。"""
    hits = _CHILD["hits"]
    if hits is None or _CHILD["dumped"]:
        return
    _CHILD["dumped"] = True
    path = os.path.join(_CHILD["dir"], f"child-{os.getpid()}-{time.perf_counter_ns()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in hits.items()}, f)


_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_EVENTS = ("os.remove", "os.rename", "os.replace", "os.mkdir", "os.rmdir", "shutil.rmtree", "os.truncate",
                "shutil.copyfile", "shutil.move", "os.symlink", "os.link")


def install_audit(log_path):
    """装审计钩子：把可能写盘 / 出网的事件逐行（JSON）追加到 log_path。钩子装上后不可卸载（只在子进程里用）。"""
    fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    busy = [False]

    def emit(kind, target):
        os.write(fd, (json.dumps({"pid": os.getpid(), "event": kind, "target": target}) + "\n").encode())

    def hook(event, args):
        if busy[0]:
            return
        busy[0] = True
        try:
            _audit_event(event, args, emit)
        finally:
            busy[0] = False

    sys.addaudithook(hook)


def _audit_event(event, args, emit):
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if isinstance(path, (str, bytes)) and _is_write(mode, flags):
            emit("write", os.path.abspath(os.fsdecode(path)))
    elif event in _PATH_EVENTS:
        fds = [a for a in args if isinstance(a, int) and not isinstance(a, bool) and a >= 0]
        base = _fd_dir(fds[0]) if fds else None
        for p in args:
            if isinstance(p, (str, bytes)):
                p = os.fsdecode(p)
                if fds and not os.path.isabs(p):
                    if base is None:
                        continue                  # 相对某个目录 fd 的名字（shutil.rmtree 内部）：外层 rmtree 事件已带全路径
                    p = os.path.join(base, p)
                emit(event, os.path.abspath(p))
    elif event == "sqlite3.connect":
        db = args[0] if args else None
        if isinstance(db, (str, bytes, os.PathLike)):
            emit("sqlite3.connect", os.fsdecode(db))
    elif event == "socket.connect":
        addr = args[1] if len(args) > 1 else None
        emit("socket.connect", list(addr) if isinstance(addr, tuple) else str(addr))


def _fd_dir(fd):
    """目录 fd → 路径（Linux 的 /proc/self/fd）；拿不到返回 None。"""
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _is_write(mode, flags):
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


# ---- spawn 进程 ------------------------------------------------------------------------
def spawn_context():
    """全包统一用 spawn（Linux / macOS / Windows 行为一致；子进程从头导入、不继承父进程状态）。"""
    return multiprocessing.get_context("spawn")


def start_process(target, *args, name=None, daemon=True):
    """daemon=False 用于自己还要再起子进程的进程（daemonic 进程不允许有子进程）；超时仍由 join_all 强制结束。"""
    proc = spawn_context().Process(target=target, args=args, name=name, daemon=daemon)
    proc.start()
    return proc


def join_all(procs, timeout):
    """等全部子进程结束；返回失败者名字列表（超时被强制结束的，或退出码非 0 的）。"""
    deadline = time.monotonic() + timeout
    bad = []
    for p in procs:
        p.join(max(0.0, deadline - time.monotonic()))
        if p.is_alive():
            p.terminate()
            p.join(5)
            bad.append(p.name)
        elif p.exitcode != 0:
            bad.append(p.name)
    return bad


def sleep_until(t_wall):
    """睡到墙钟 t_wall（time.time() 口径，用于跨进程对齐起跑时刻）。"""
    while True:
        d = t_wall - time.time()
        if d <= 0:
            return
        time.sleep(min(d, 0.05))
