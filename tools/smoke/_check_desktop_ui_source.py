# -*- coding: utf-8 -*-
"""桌面端「本地界面源」自检：app\\ui_server.py（本地界面 + 反向代理）。

为什么要有这个自检
------------------
桌面版现在从「应用自带的界面副本」渲染，数据经反向代理转给本地服务。代理这块一旦有错
（分块响应没重新分块、流式响应被缓冲、WebSocket 没转发、路径穿越），表现会非常隐蔽：
页面能开、但直播流不动、字幕不出、上传失败。所以这里用一个**内置的上游桩服务**把各种
响应形态都造出来，逐条断言，不依赖真实的 LiveTalking 服务，随时可复跑。

用法：E:\\AiLive\\env\\python.exe tools\\smoke\\_check_desktop_ui_source.py
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

PROJECT = Path(r"E:\AiLive")
sys.path.insert(0, str(PROJECT / "app"))

from ui_server import LocalUiServer  # noqa: E402

UI_DIR = PROJECT / "app" / "ui"
ENTRY = UI_DIR / "desktop.html"
RESULTS = []
UPSTREAM_HITS = []
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
BIG_BODY = (b"AI-LIVE-BIG-" * 200000)          # 约 2.4 MB，验大响应完整性


# ─── 上游桩：把代理需要覆盖的响应形态都造出来 ─────────────────────────────
class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _hit(self):
        UPSTREAM_HITS.append((self.command, self.path))

    def do_GET(self):
        self._hit()
        if self.path.startswith("/api/ping"):
            body = json.dumps({"code": 0, "data": {"from": "upstream"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "ailive_session=stub; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/stream"):
            # SSE：分块传输 + 逐条推送，用来验证代理有没有把数据缓冲住
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(5):
                piece = ("data: tick-%d\n\n" % i).encode()
                self.wfile.write(b"%x\r\n" % len(piece) + piece + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        if self.path.startswith("/big"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(BIG_BODY)))
            self.end_headers()
            self.wfile.write(BIG_BODY)
            return
        if self.path.startswith("/ws"):
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.wfile.flush()
            # 原样回显（代理是中继，不解析帧）
            self.connection.settimeout(10)
            try:
                while True:
                    data = self.connection.recv(4096)
                    if not data:
                        break
                    self.connection.sendall(data)
            except OSError:
                pass
            self.close_connection = True
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._hit()
        if self.path.startswith("/api/echo"):
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                body = b""
                while True:
                    line = self.rfile.readline().strip()
                    size = int(line.split(b";")[0], 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    body += self.rfile.read(size)
                    self.rfile.read(2)
            else:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            out = json.dumps({"code": 0, "data": {"len": len(body),
                                                  "sha": hashlib.sha256(body).hexdigest()[:16]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), str(detail)[:220]))
    print(("PASS " if ok else "FAIL ") + name + ("  | " + str(detail)[:220] if detail else ""))


def raw_request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    out = (resp.status, dict((k.lower(), v) for k, v in resp.getheaders()), data)
    conn.close()
    return out


def websocket_roundtrip(port, path, payload=b"hello-frame"):
    sock = socket.create_connection(("127.0.0.1", port), timeout=15)
    key = base64.b64encode(b"0123456789abcdef").decode()
    req = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, port, key))
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        piece = sock.recv(4096)
        if not piece:
            break
        head += piece
    status_ok = head.startswith(b"HTTP/1.1 101")
    accept_ok = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()) in head
    sock.sendall(payload)
    sock.settimeout(15)
    echoed = b""
    while len(echoed) < len(payload):
        piece = sock.recv(len(payload) - len(echoed))
        if not piece:
            break
        echoed += piece
    sock.close()
    return status_ok, accept_ok, echoed == payload


def main():
    if not ENTRY.is_file():
        check("桌面端界面副本已固化（app\\ui\\desktop.html）", False, "先跑 app\\vendor_ui.py")
        return finish()
    entry_bytes = ENTRY.read_bytes()
    check("桌面端界面副本已固化（app\\ui\\desktop.html）", True,
          "%s（%.1f KB）" % (ENTRY.name, len(entry_bytes) / 1024))

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    upstream.daemon_threads = True
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    server = LocalUiServer(UI_DIR, service_host="127.0.0.1", service_port=up_port, log=lambda m: None)
    origin = server.start()
    port = server.port
    check("本地界面源启动并拿到端口", port > 0, origin)
    time.sleep(0.2)
    try:
        # ── 本地界面：入口与别名 ────────────────────────────────────────
        status, headers, data = raw_request(port, "GET", "/desktop.html")
        check("GET /desktop.html 由本地副本提供", status == 200 and data == entry_bytes,
              "status=%s bytes=%d 期望=%d" % (status, len(data), len(entry_bytes)))
        check("入口 Content-Type 正确", headers.get("content-type", "").startswith("text/html"),
              headers.get("content-type"))

        status, _, data = raw_request(port, "GET", "/app.html?host=1")
        check("GET /app.html 被映射到桌面端自己的界面（绝不落到网页端）",
              status == 200 and data == entry_bytes, "status=%s bytes=%d" % (status, len(data)))

        status, _, data = raw_request(port, "GET", "/")
        check("GET / 也落在桌面端界面", status == 200 and data == entry_bytes, "status=%s" % status)

        css = UI_DIR / "web" / "common.css"
        status, headers, data = raw_request(port, "GET", "/common.css")
        check("静态资源走本地副本（common.css）",
              status == 200 and css.is_file() and data == css.read_bytes(),
              "status=%s bytes=%d" % (status, len(data)))

        status, _, data = raw_request(port, "GET", "/asr/index.html")
        local_asr = UI_DIR / "web" / "asr" / "index.html"
        hit_before = len(UPSTREAM_HITS)
        ok = local_asr.is_file() and status == 200 and data == local_asr.read_bytes()
        check("子目录静态资源也走本地副本（asr/index.html）", ok, "status=%s" % status)

        # ── 接口：反向代理 ─────────────────────────────────────────────
        before = len(UPSTREAM_HITS)
        status, headers, data = raw_request(port, "GET", "/api/ping")
        proxied = any(p == "/api/ping" for _m, p in UPSTREAM_HITS[before:])
        payload = json.loads(data.decode()) if status == 200 else {}
        check("GET /api/ping 反向代理到上游服务", status == 200 and proxied
              and payload.get("data", {}).get("from") == "upstream", "status=%s %s" % (status, payload))
        check("上游 Set-Cookie 原样透传（登录态必需）",
              "ailive_session=stub" in headers.get("set-cookie", ""), headers.get("set-cookie"))
        check("代理响应不带逐跳首部", "keep-alive" not in headers and "proxy-authenticate" not in headers,
              "connection=%s" % headers.get("connection"))

        body = json.dumps({"hello": "世界", "n": 1}).encode("utf-8")
        status, _, data = raw_request(port, "POST", "/api/echo", body=body,
                                      headers={"Content-Type": "application/json"})
        got = json.loads(data.decode()) if status == 200 else {}
        check("POST 请求体经代理完整送达（含中文 UTF-8）",
              status == 200 and got.get("data", {}).get("len") == len(body)
              and got.get("data", {}).get("sha") == hashlib.sha256(body).hexdigest()[:16],
              "status=%s %s" % (status, got))

        status, headers, data = raw_request(port, "GET", "/big")
        check("大响应（2.4MB）经代理字节一致",
              status == 200 and hashlib.sha256(data).hexdigest() == hashlib.sha256(BIG_BODY).hexdigest(),
              "status=%s bytes=%d" % (status, len(data)))

        status, headers, data = raw_request(port, "GET", "/api/stream")
        ticks = [b"tick-%d" % i for i in range(5)]
        check("分块流式响应（SSE）逐条完整到达",
              status == 200 and all(t in data for t in ticks) and b"tick-4" in data,
              "status=%s body=%r" % (status, data[:60]))
        check("流式响应保留了 chunked 分帧（客户端不会解析错帧）",
              headers.get("transfer-encoding", "").lower() == "chunked",
              "transfer-encoding=%s" % headers.get("transfer-encoding"))

        # ── WebSocket 升级转发 ─────────────────────────────────────────
        try:
            ok101, accept_ok, echoed = websocket_roundtrip(port, "/ws")
        except Exception as exc:
            ok101, accept_ok, echoed = False, False, False
            print("   websocket 异常：%r" % exc)
        check("WebSocket 握手转发成功（101 + Sec-WebSocket-Accept）", ok101 and accept_ok,
              "101=%s accept=%s" % (ok101, accept_ok))
        check("WebSocket 数据双向中继正确", echoed, "echoed=%s" % echoed)

        # ── 安全 / 状态码 ──────────────────────────────────────────────
        status, _, data = raw_request(port, "GET", "/..%2f..%2fapp.py")
        check("路径穿越不会读到本地任意文件",
              b"def main" not in data and b"import" not in data[:200],
              "status=%s body=%r" % (status, data[:60]))

        status, _, _ = raw_request(port, "GET", "/api/not-exist")
        check("上游 404 状态码原样透传", status == 404, "status=%s" % status)
    finally:
        server.stop()
        upstream.shutdown()
        upstream.server_close()
    return finish()


def finish():
    ok = sum(1 for _, o, _ in RESULTS if o)
    print("\n==== %d/%d 通过 ====" % (ok, len(RESULTS)))
    if ok != len(RESULTS):
        for n, o, d in RESULTS:
            if not o:
                print("  - %s | %s" % (n, d))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())