"""cdp：WebSocket 客户端对着测试里自己写的小服务器（RFC 6455 服务端）验证帧格式；浏览器部分对真实 Chromium（没有则跳过）。"""
import base64
import hashlib
import os
import socket
import struct
import threading
import unittest

from qdtext import cdp
from qdtext import common as C


def read_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


def read_client_frame(conn):
    """服务端读一帧：→ (opcode, payload)；客户端帧必须有掩码。"""
    b1, b2 = read_exact(conn, 2)
    assert b2 & 0x80, "client frames must be masked"
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", read_exact(conn, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read_exact(conn, 8))[0]
    mask = read_exact(conn, 4)
    data = bytes(c ^ mask[i % 4] for i, c in enumerate(read_exact(conn, n)))
    return b1 & 0x0F, data, bool(b1 & 0x80)


def server_frame(opcode, payload, fin=True):
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


class EchoServer(threading.Thread):
    """握手后：收到文本就按脚本回；脚本由测试通过 self.script 决定。"""

    def __init__(self, script, bad_accept=False, status="101 Switching Protocols"):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.script, self.bad_accept, self.status = script, bad_accept, status
        self.received, self.error = [], None

    def run(self):
        conn, _ = self.sock.accept()
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += conn.recv(4096)
            key = [ln.split(":", 1)[1].strip() for ln in req.decode().split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1((key + cdp.WS_GUID).encode()).digest()).decode()
            if self.bad_accept:
                accept = ("x" if accept[0] != "x" else "y") + accept[1:]             # 保证真的不一样（首字母本来就是 x 时换 y）
            conn.sendall(f"HTTP/1.1 {self.status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
            if self.status.startswith("101"):
                self.script(conn, self)
        except Exception as e:                                             # noqa: BLE001 记下来给测试看
            self.error = e
        finally:
            conn.close()
            self.sock.close()


def connect(server, timeout=5):
    server.start()
    return cdp.WebSocket("127.0.0.1", server.port, "/devtools/browser/x", timeout)


class TestWebSocket(unittest.TestCase):
    def test_refuses_non_loopback(self):
        with self.assertRaises(cdp.CDPError):
            cdp.WebSocket("10.0.0.1", 1, "/")

    def test_handshake_failures(self):
        s = EchoServer(None, bad_accept=True)
        with self.assertRaises(cdp.CDPError):
            connect(s)
        s = EchoServer(None, status="403 Forbidden")
        with self.assertRaises(cdp.CDPError):
            connect(s)

    def test_text_roundtrip_all_lengths(self):
        sizes = [0, 1, 125, 126, 65535, 65536, 200000]

        def script(conn, srv):
            for _ in sizes:
                op, data, fin = read_client_frame(conn)
                srv.received.append((op, len(data), fin))
                conn.sendall(server_frame(0x1, data))

        s = EchoServer(script)
        ws = connect(s)
        for n in sizes:
            msg = "字" * (n // 3) + "a" * (n - 3 * (n // 3))
            ws.send_text(msg)
            self.assertEqual(ws.recv_text(), msg)
        ws.close()
        s.join(5)
        self.assertIsNone(s.error)
        self.assertEqual(s.received, [(0x1, len(("字" * (n // 3) + "a" * (n - 3 * (n // 3))).encode()), True) for n in sizes])

    def test_fragments_ping_binary_and_close(self):
        def script(conn, srv):
            read_client_frame(conn)
            conn.sendall(server_frame(0x9, b"ping!"))                              # ping → 客户端应回 pong
            op, data, _ = read_client_frame(conn)
            srv.received.append((op, data))
            conn.sendall(server_frame(0xA, b""))                                   # 无端 pong，忽略
            conn.sendall(server_frame(0x2, b"\x00\x01"))                           # 二进制，丢弃
            conn.sendall(server_frame(0x1, "你".encode(), fin=False) + server_frame(0x0, "好".encode()[:1], fin=False)
                         + server_frame(0x0, "好".encode()[1:]))                   # 三片，连 UTF-8 多字节都被切开
            op, data, _ = read_client_frame(conn)                                   # 等客户端发第二条
            conn.sendall(server_frame(0x8, struct.pack("!H", 1000)))
            op, data, _ = read_client_frame(conn)
            srv.received.append((op, data[:2]))

        s = EchoServer(script)
        ws = connect(s)
        ws.send_text("x")
        self.assertEqual(ws.recv_text(), "你好")
        ws.send_text("y")
        with self.assertRaises(cdp.ConnectionClosed):
            ws.recv_text()
        ws.close()
        ws.close()                                                                  # 幂等
        s.join(5)
        self.assertIsNone(s.error)
        self.assertEqual(s.received, [(0xA, b"ping!"), (0x8, struct.pack("!H", 1000))])

    def test_closed_during_handshake(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)

        def run():
            conn, _ = srv.accept()
            conn.recv(4096)
            conn.close()
        threading.Thread(target=run, daemon=True).start()
        with self.assertRaises(cdp.ConnectionClosed):
            cdp.WebSocket("127.0.0.1", srv.getsockname()[1], "/")
        srv.close()

    def test_masked_server_frame_and_long_lengths(self):
        """服务端也可以发掩码帧（协议允许），客户端要能解；顺带覆盖 16 位与 64 位长度的接收路径。"""
        def script(conn, srv):
            read_client_frame(conn)
            mask = b"\x01\x02\x03\x04"
            payload = b"m" * 300
            masked = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            conn.sendall(bytes([0x81, 0x80 | 126]) + struct.pack("!H", 300) + mask + masked)
            conn.sendall(server_frame(0x1, b"z" * 70000))

        s = EchoServer(script)
        ws = connect(s)
        ws.send_text("go")
        self.assertEqual(ws.recv_text(), "m" * 300)
        self.assertEqual(len(ws.recv_text()), 70000)
        ws.close()

    def test_server_vanishes(self):
        def script(conn, srv):
            read_client_frame(conn)

        s = EchoServer(script)
        ws = connect(s)
        ws.send_text("bye")
        with self.assertRaises(cdp.ConnectionClosed):
            ws.recv_text()


class TestFindChrome(unittest.TestCase):
    def test_env_override(self):
        old = os.environ.get(cdp.CHROME_ENV)
        try:
            os.environ[cdp.CHROME_ENV] = "/definitely/not/here"
            self.assertIsNone(cdp.find_chrome())
            os.environ[cdp.CHROME_ENV] = os.path.abspath(__import__("sys").executable)
            self.assertEqual(cdp.find_chrome(), os.path.abspath(__import__("sys").executable))
        finally:
            if old is None:
                del os.environ[cdp.CHROME_ENV]
            else:
                os.environ[cdp.CHROME_ENV] = old

    def test_glob_fallback(self):
        old = os.environ.pop(cdp.CHROME_ENV, None)
        old_names, old_globs = cdp.CHROME_NAMES, cdp.CHROME_GLOBS
        d = C.mkdtemp("chrome-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        fake = os.path.join(d, "chrome-9", "chrome")
        os.makedirs(os.path.dirname(fake))
        open(fake, "w").close()
        try:
            cdp.CHROME_NAMES, cdp.CHROME_GLOBS = ("qdtext-no-such-binary",), (os.path.join(d, "chrome-*", "chrome"),)
            self.assertEqual(cdp.find_chrome(), fake)
            cdp.CHROME_GLOBS = (os.path.join(d, "nothing-*", "chrome"),)
            self.assertIsNone(cdp.find_chrome())
        finally:
            cdp.CHROME_NAMES, cdp.CHROME_GLOBS = old_names, old_globs
            if old is not None:
                os.environ[cdp.CHROME_ENV] = old

    def test_flags_disable_networking(self):
        self.assertIn("--disable-background-networking", cdp.CHROME_FLAGS)
        self.assertIn("--remote-debugging-port=0", cdp.CHROME_FLAGS)
        self.assertIn("--headless=new", cdp.CHROME_FLAGS)

    def test_browser_without_exe(self):
        old = os.environ.get(cdp.CHROME_ENV)
        os.environ[cdp.CHROME_ENV] = "/definitely/not/here"
        try:
            with self.assertRaises(cdp.CDPError):
                cdp.Browser()
        finally:
            if old is None:
                del os.environ[cdp.CHROME_ENV]
            else:
                os.environ[cdp.CHROME_ENV] = old

    def test_wait_port_exit_early_and_timeout(self):
        class P:
            returncode = 7

            def __init__(self, alive):
                self.alive = alive

            def poll(self):
                return None if self.alive else 7

        d = C.mkdtemp("x-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        with self.assertRaisesRegex(cdp.CDPError, "exited early"):
            cdp.wait_devtools_port(d, P(False), 1)
        with open(os.path.join(d, "DevToolsActivePort"), "w") as f:
            f.write("notaport\n/x\n")                                            # 文件在但内容还没写完整
        with self.assertRaisesRegex(cdp.CDPError, "timeout"):
            cdp.wait_devtools_port(d, P(True), 0.2)
        with open(os.path.join(d, "DevToolsActivePort"), "w") as f:
            f.write("4321\n/devtools/browser/abc\n")
        self.assertEqual(cdp.wait_devtools_port(d, P(True), 1), (4321, "/devtools/browser/abc"))

    def test_browser_exe_that_dies(self):
        """给一个马上退出的「浏览器」：Browser 应报 CDPError 并清掉 user-data-dir。"""
        d = C.mkdtemp("fake-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        fake = os.path.join(d, "fakechrome")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        os.chmod(fake, 0o755)
        with self.assertRaisesRegex(cdp.CDPError, "exited early"):
            cdp.Browser(fake, timeout=5)
        self.assertEqual([n for n in os.listdir(__import__("tempfile").gettempdir()) if n.startswith("qdtext-chrome-")], [])


@unittest.skipUnless(cdp.find_chrome(), "no chromium executable")
class TestRealBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = cdp.Browser()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.browser.close()                                                         # 幂等
        cls.assertFalse(cls, os.path.exists(cls.browser.user_data_dir))

    def test_navigate_and_evaluate(self):
        b = self.browser
        self.assertTrue(b.version)
        target, session = b.new_page()
        url = "file://" + os.path.join(C.FIXTURES, "site", "index.html")
        b.navigate(session, url, 375, 812)
        self.assertEqual(b.evaluate(session, "innerWidth"), 375)
        self.assertEqual(b.evaluate(session, "document.title"), "资产总览")
        self.assertEqual(b.evaluate(session, "Promise.resolve(42)", True), 42)
        with self.assertRaises(cdp.CDPError):
            b.evaluate(session, "throw new Error('boom')")
        with self.assertRaises(cdp.CDPError):
            b.call("No.SuchMethod", session_id=session)
        with self.assertRaises(cdp.CDPError):
            b.wait_event("Page.neverFires", session, timeout=0.2)
        b.close_page(target)


if __name__ == "__main__":
    unittest.main()
