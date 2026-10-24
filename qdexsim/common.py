"""公用小件：路径、JSON、时钟、机器信息、子进程审计 / 覆盖率钩子。

环境变量（只在测试 / 自检里用）：
- QDEXSIM_AUDIT_LOG：存在时装 sys.addaudithook，把写文件 / 删改名 / sqlite3.connect / socket.connect / socket.bind
  逐行追加到该文件（tests/test_constraints.py 用它证明“只写 qdexsim/results/ 与 tempfile、只连 / 只听 127.0.0.1”）；
- QDEXSIM_COV_DIR：存在时子进程用 sys.monitoring 记录自己执行过的 qdexsim 行，退出时写出（tools/cov.py 合并）。
"""
import atexit
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time

PKG = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(PKG)
RESULTS = os.path.join(PKG, "results")
LOOPBACK = "127.0.0.1"
EIGHT_HOURS_MS = 8 * 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def fnum(x, places=8):
    """浮点 → 交易所风格的十进制字符串（去掉多余的 0；-0 归 0）。"""
    s = f"{float(x):.{places}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def round_step(x, step):
    """按步长归整（四舍五入到最接近的 step 倍数，用整数算避免 0.1+0.2 问题）。"""
    if step <= 0:
        return float(x)
    n = round(float(x) / step)
    return round(n * step, 12)


def is_multiple(x, step, tol=1e-9):
    return step <= 0 or abs(round_step(x, step) - float(x)) <= tol


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True,
                              timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def machine():
    load = os.getloadavg()[0] if hasattr(os, "getloadavg") else None
    return {"platform": platform.platform(), "python": platform.python_version(), "cpu_count": os.cpu_count(),
            "sqlite_version": sqlite3.sqlite_version, "load_avg_1m": None if load is None else round(load, 2),
            "git_commit": git_commit(), "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}


# ---- 子进程钩子 ----------------------------------------------------------------------
_CHILD = {"hits": None, "dir": None, "dumped": False, "audit": False}


def child_init():
    """每个 qdexsim 子进程入口调用一次（幂等）：按环境变量装覆盖率记录 / 审计钩子。"""
    cov = os.environ.get("QDEXSIM_COV_DIR")
    if cov and _CHILD["hits"] is None:
        _start_child_coverage(cov)
    log = os.environ.get("QDEXSIM_AUDIT_LOG")
    if log and not _CHILD["audit"]:
        install_audit(log)
        _CHILD["audit"] = True


def _start_child_coverage(cov_dir):
    tool = sys.monitoring.COVERAGE_ID
    hits = {}
    prefix = PKG + os.sep

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(prefix):
            hits.setdefault(f, set()).add(line)
        else:
            return sys.monitoring.DISABLE
        return None

    sys.monitoring.use_tool_id(tool, "qdexsim-child")
    sys.monitoring.register_callback(tool, sys.monitoring.events.LINE, on_line)
    sys.monitoring.set_events(tool, sys.monitoring.events.LINE)
    _CHILD["hits"], _CHILD["dir"] = hits, cov_dir
    atexit.register(child_finish)


def child_finish():
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
_DIR_FD_ARG = {"os.remove": {0: 1}, "os.rmdir": {0: 1}, "shutil.rmtree": {0: 1}, "os.mkdir": {0: 2},
               "os.rename": {0: 2, 1: 3}, "os.replace": {0: 2, 1: 3}, "os.link": {0: 2, 1: 3}, "os.symlink": {1: 2}}
UNRESOLVED_DIR_FD = "<unresolved dir_fd="


def install_audit(log_path):
    """装审计钩子：把可能写盘 / 出网 / 监听的事件逐行（JSON）追加到 log_path。装上后不可卸载。"""
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
        _audit_path_event(event, args, emit)
    elif event == "sqlite3.connect":
        db = args[0] if args else None
        if isinstance(db, (str, bytes, os.PathLike)):
            emit("sqlite3.connect", os.fsdecode(db))
    elif event in ("socket.connect", "socket.bind"):
        addr = args[1] if len(args) > 1 else None
        emit(event, list(addr) if isinstance(addr, tuple) else str(addr))


def _audit_path_event(event, args, emit):
    fd_arg = _DIR_FD_ARG.get(event, {})
    for i, p in enumerate(args):
        if isinstance(p, os.PathLike):
            p = os.fspath(p)
        if not isinstance(p, (str, bytes)):
            continue
        p = os.fsdecode(p)
        j = fd_arg.get(i)
        fd = args[j] if j is not None and j < len(args) else -1
        if os.path.isabs(p) or not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
            emit(event, os.path.abspath(p))
            continue
        base = _fd_dir(fd)
        emit(event, os.path.abspath(os.path.join(base, p)) if base else f"{UNRESOLVED_DIR_FD}{fd}>/{p}")


def _fd_dir(fd):
    try:
        path = os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None
    return path if os.path.isabs(path) else None


def _is_write(mode, flags):
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
