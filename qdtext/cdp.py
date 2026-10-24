"""最小 DevTools 协议客户端（纯标准库）：RFC 6455 WebSocket 客户端（只连 127.0.0.1）+ 无头 Chromium 启停 + 会话调用。

只允许的外部东西是本机已装的 Chromium / Chrome 可执行文件：按 QDTEXT_CHROME 环境变量 → PATH 上的常见名字 → Playwright /
常见安装路径查找；找不到返回 None，调用方整项 SKIP。启动参数关掉后台联网（组件更新 / 同步 / 扩展 / 翻译）；本模块自身
的 socket 只连 127.0.0.1（WebSocket 构造时断言）。
"""
import base64
import glob
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time

CHROME_ENV = "QDTEXT_CHROME"
CHROME_NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome", "chrome-headless-shell")
CHROME_GLOBS = (
    "/opt/pw-browsers/chromium-*/chrome-linux*/chrome",
    "/opt/pw-browsers/chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
    os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "C:/Program Files/Google/Chrome/Application/chrome.exe",
    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)
CHROME_FLAGS = (
    "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", "--no-first-run",
    "--no-default-browser-check", "--disable-background-networking", "--disable-component-update", "--disable-sync",
    "--disable-default-apps", "--disable-extensions", "--metrics-recording-only", "--disable-translate",
    "--disable-features=TranslateUI,OptimizationHints,MediaRouter", "--hide-scrollbars", "--mute-audio",
    "--remote-debugging-port=0",
)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LOCALHOST = "127.0.0.1"


class CDPError(Exception):
    pass


class ConnectionClosed(CDPError):
    pass


def find_chrome():
    """→ 可执行文件路径或 None。"""
    env = os.environ.get(CHROME_ENV)
    if env:
        return env if os.path.isfile(env) and os.access(env, os.X_OK) else None
    for name in CHROME_NAMES:
        p = shutil.which(name)
        if p:
            return p
    for pattern in CHROME_GLOBS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


# ---- WebSocket --------------------------------------------------------------------------
class WebSocket:
    """够 DevTools 用的客户端：握手、掩码文本帧、分片重组、ping→pong、close。只连 127.0.0.1。"""

    def __init__(self, host, port, path, timeout=30.0):
        if host != LOCALHOST:
            raise CDPError(f"refusing to connect to {host}: only {LOCALHOST} is allowed")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.buf = b""
        self._handshake(host, port, path)

    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionClosed("closed during handshake")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0]:
            raise CDPError("handshake failed: " + lines[0])
        accept = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}.get("sec-websocket-accept")
        expect = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if accept != expect:
            raise CDPError("bad Sec-WebSocket-Accept")

    def _read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionClosed("socket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _read_frame(self):
        b1, b2 = self._read_exact(2)
        fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        """下一条完整文本消息（自动拼分片、回 pong、处理 close）。"""
        parts, op0 = [], None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x8:
                raise ConnectionClosed("close frame received")
            if opcode in (0x1, 0x2):
                op0 = opcode
            parts.append(payload)
            if fin:
                data = b"".join(parts)
                if op0 == 0x1:
                    return data.decode("utf-8")
                parts, op0 = [], None                            # 二进制消息：DevTools 不发，丢弃

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ---- 浏览器 -------------------------------------------------------------------------------
def wait_devtools_port(user_data_dir, proc, timeout):
    """等 Chrome 写出 DevToolsActivePort（端口 + browser ws 路径）。"""
    path = os.path.join(user_data_dir, "DevToolsActivePort")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise CDPError(f"chrome exited early with code {proc.returncode}")
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            if len(lines) >= 2 and lines[0].strip().isdigit():
                return int(lines[0]), lines[1].strip()
        except OSError:
            pass
        time.sleep(0.05)
    raise CDPError("timeout waiting for DevToolsActivePort")


class Browser:
    """起一个无头 Chromium，走 browser 级 WebSocket；页面会话用 Target.attachToTarget(flatten) 的 sessionId。"""

    def __init__(self, exe=None, timeout=30.0):
        self.exe = exe or find_chrome()
        if not self.exe:
            raise CDPError("no chromium/chrome executable found")
        self.timeout = timeout
        self.user_data_dir = tempfile.mkdtemp(prefix="qdtext-chrome-")
        self.proc = subprocess.Popen([self.exe, *CHROME_FLAGS, f"--user-data-dir={self.user_data_dir}", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        self.events, self.pending, self.next_id = [], {}, 0
        try:
            port, ws_path = wait_devtools_port(self.user_data_dir, self.proc, timeout)
            self.ws = WebSocket(LOCALHOST, port, ws_path, timeout)
        except Exception:
            self.close()
            raise
        self.version = self.call("Browser.getVersion").get("product", "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pump(self, deadline, what):
        """收一条消息（响应进 pending，事件进 events）；到 deadline 还没消息 → CDPError。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CDPError(f"timeout waiting for {what}")
        self.ws.sock.settimeout(remaining)
        try:
            msg = json.loads(self.ws.recv_text())
        except TimeoutError:
            raise CDPError(f"timeout waiting for {what}") from None
        if "id" in msg:
            self.pending[msg["id"]] = msg
        else:
            self.events.append(msg)

    def call(self, method, params=None, session_id=None, timeout=None):
        self.next_id += 1
        mid = self.next_id
        msg = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send_text(json.dumps(msg))
        deadline = time.monotonic() + (timeout or self.timeout)
        while mid not in self.pending:
            self._pump(deadline, method)
        reply = self.pending.pop(mid)
        if "error" in reply:
            raise CDPError(f"{method}: {reply['error'].get('message')} {reply['error'].get('data', '')}".strip())
        return reply.get("result", {})

    def wait_event(self, method, session_id=None, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            for i, ev in enumerate(self.events):
                if ev.get("method") == method and (session_id is None or ev.get("sessionId") == session_id):
                    return self.events.pop(i)
            self._pump(deadline, "event " + method)

    def new_page(self):
        """→ (targetId, sessionId)。"""
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable", session_id=session)
        self.call("Runtime.enable", session_id=session)
        return target, session

    def close_page(self, target_id):
        self.call("Target.closeTarget", {"targetId": target_id})

    def navigate(self, session, url, width, height, scale=2):
        """设手机视口 → 导航 → 等 load → 等两帧让排版落定。"""
        self.call("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": True,
                   "screenWidth": width, "screenHeight": height}, session_id=session)
        self.call("Page.navigate", {"url": url}, session_id=session)
        self.wait_event("Page.loadEventFired", session)
        self.evaluate(session, "new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(1))))", True)

    def evaluate(self, session, expression, await_promise=False):
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
                        session_id=session)
        if "exceptionDetails" in res:
            raise CDPError("evaluate failed: " + json.dumps(res["exceptionDetails"])[:500])
        return res.get("result", {}).get("value")

    def close(self):
        ws = getattr(self, "ws", None)
        if ws is not None:
            try:
                self.call("Browser.close", timeout=3)
            except Exception:
                pass
            ws.close()
            self.ws = None
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.user_data_dir, ignore_errors=True)
