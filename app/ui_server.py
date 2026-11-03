# -*- coding: utf-8 -*-
"""桌面应用的「本地界面源」：把随应用打包的界面文件本地渲染，数据转发给本地服务。

为什么需要它
------------
桌面版以前是直接把窗口指向 ``http://127.0.0.1:8010/app.html`` —— 那是**网页端**的
文件，界面的所有权在服务那边：服务换个版本、删个文件，桌面端就跟着变，用户看到的
也始终是"网页版的界面"。

现在换成：界面文件随应用一起打包（``app/ui/``），由本模块在 127.0.0.1 上**另起一个
同源端口**提供；凡是本地界面目录里有的路径一律走本地文件，其余的（``/api/*``、
``/offer``、``/whep``、``/record/*`` 等）原样反向代理到 LiveTalking 服务。

为什么必须是"同源"而不是直接给页面一个别的 URL
----------------------------------------------
界面里大量使用相对路径与 ``fetch(path, {credentials:'same-origin'})``（例如
``app.html`` 第 791 行），并且依赖登录 Cookie。只要页面源和接口源不是同一个 origin，
Cookie 就不会带上、相对路径也会 404。于是一个 origin 同时提供"本地界面 + 代理接口"
是最省事也最不容易出错的做法：**界面代码一行都不用改**。

代理需要覆盖的形态（都是这个项目真实用到的）
--------------------------------------------
* 普通 GET/POST（含 JSON 与 multipart 上传，服务端 client_max_size=100MB）
* 流式响应：``live_output.html`` 用 EventSource(SSE)，``/record/*`` 是大文件下载
* 分块传输：上游用 chunked 时必须由我们重新分块，否则客户端会解析错帧
* WebSocket 升级：``/api/asr`` 等（握手 + 双向原始字节转发）
"""
from __future__ import annotations

import http.client
import json
import mimetypes
import os
import select
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

# 逐跳首部：不能原样转发（RFC 7230 8.1）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade",
}
# 这些由我们自己决定，不照抄上游
RESPONSE_OVERRIDE = {"transfer-encoding", "content-length", "connection"}

LOCAL_ENTRY = "desktop.html"      # 桌面端自己的界面入口（随应用打包）
UI_ROOT_SUBDIR = "web"            # 其余界面文件的存放子目录


class _UiHttpd(ThreadingHTTPServer):
    """必须关掉 SO_REUSEADDR。

    Python 的 HTTPServer 默认 ``allow_reuse_address = 1``；这个选项在 **Windows 上语义不同**：
    它允许"绑定一个已经被别人监听的端口"。后果是两个进程共用一个端口，请求全跑到先绑定的
    那一个（实测踩到：自检脚本的界面源被正在运行的应用实例"抢走"，所有断言拿到的是真实服务
    的 401）。关掉之后被占用的端口会正常抛 OSError，于是按 preferred_ports 顺序换下一个。
    """

    allow_reuse_address = False
    daemon_threads = True


class LocalUiServer:
    """本地界面源：静态文件优先，其余反向代理到本地服务。"""

    def __init__(self, ui_root, service_host: str = "127.0.0.1", service_port: int = 8010,
                 log=None, preferred_ports=(8015, 8016, 8017, 8018, 8019, 8020)):
        self.ui_root = Path(ui_root)
        self.entry_file = self.ui_root / LOCAL_ENTRY
        self.web_root = self.ui_root / UI_ROOT_SUBDIR
        self.service_host = service_host
        self.service_port = int(service_port)
        self.log = log or (lambda _msg: None)
        self.preferred_ports = list(preferred_ports)
        self._httpd = None
        self._thread = None
        self.port = 0

    # ── 生命周期 ────────────────────────────────────────────────────────
    @property
    def origin(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self) -> str:
        if self._httpd is not None:
            return self.origin
        handler = _make_handler(self)
        last_exc = None
        for port in self.preferred_ports + [0]:
            try:
                httpd = _UiHttpd(("127.0.0.1", port), handler)
            except OSError as exc:          # 端口被占：换下一个
                last_exc = exc
                continue
            self._httpd = httpd
            self.port = httpd.server_address[1]
            break
        if self._httpd is None:
            raise RuntimeError("本地界面源启动失败：%s" % last_exc)
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.3},
                                        name="ui-server", daemon=True)
        self._thread.start()
        self.log("[界面源] 本地界面已就绪：%s（%s）" % (self.origin, self.ui_root))
        return self.origin

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ── 路径解析 ────────────────────────────────────────────────────────
    def map_local(self, raw_path: str):
        """把请求路径映射到本地界面文件；返回 None 表示"本地没有，应该代理"。"""
        path = unquote(urlsplit(raw_path).path or "/")
        if path in ("/", "/index.html", "/app.html", "/app", "/desktop.html"):
            return self.entry_file if self.entry_file.is_file() else None
        rel = path.lstrip("/")
        if not rel:
            return None
        candidate = (self.web_root / rel)
        try:
            candidate = candidate.resolve()
            root = self.web_root.resolve()
        except OSError:
            return None
        if root not in candidate.parents and candidate != root:
            return None                      # 目录穿越
        if candidate.is_file():
            return candidate
        return None


def _content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    if not ctype:
        ctype = "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json",
                                             "image/svg+xml"):
        ctype += "; charset=utf-8"
    return ctype


def _relay(client: socket.socket, upstream: socket.socket, max_pending: int = 1 << 20) -> None:
    """两个 socket 之间双向原样转发，带背压保护（WebSocket 帧不需要解析）。"""
    client.setblocking(False)
    upstream.setblocking(False)
    peers = {client: upstream, upstream: client}
    pending = {client: b"", upstream: b""}
    try:
        while True:
            rlist = [s for s in peers if len(pending[peers[s]]) < max_pending]
            wlist = [s for s in peers if pending[s]]
            if not rlist and not wlist:
                return
            try:
                readable, writable, _ = select.select(rlist, wlist, [], 60)
            except (OSError, ValueError):
                return
            for src in readable:
                try:
                    data = src.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                if not data:
                    return
                pending[peers[src]] += data
            for dst in writable:
                buf = pending[dst]
                try:
                    sent = dst.send(buf)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return
                pending[dst] = buf[sent:]
    finally:
        for sock in peers:
            try:
                sock.setblocking(True)
            except Exception:
                pass


def _make_handler(server: LocalUiServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "AILiveDesktopUI"
        sys_version = ""

        # ── 统一入口 ────────────────────────────────────────────────────
        def _dispatch(self):
            try:
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self._proxy_websocket()
                    return
                target = server.map_local(self.path)
                if target is not None:
                    self._serve_file(target)
                else:
                    self._proxy_http()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as exc:                       # 代理失败也要给出可读响应
                server.log("[界面源] %s %s 处理失败：%r" % (self.command, self.path, exc))
                self._send_plain(502, "本地界面源转发失败：%s" % exc)
                self.close_connection = True

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _dispatch

        # ── 本地静态文件 ────────────────────────────────────────────────
        def _parse_range(self, total: int):
            """解析 Range 头，支持音频/视频拖动。

            返回 (start, end) 闭区间表示部分响应，None 表示整文件，'bad' 表示
            区间不合法（应回 416）。只处理单段 bytes 范围，多段直接按整文件回。
            """
            raw = (self.headers.get("Range") or "").strip()
            if not raw.startswith("bytes=") or "," in raw:
                return None
            spec = raw[6:].strip()
            if "-" not in spec:
                return None
            head, _, tail = spec.partition("-")
            try:
                if not head.strip():                        # bytes=-N：最后 N 字节
                    size = int(tail)
                    if size <= 0:
                        return None
                    return max(0, total - size), total - 1
                start = int(head)
                end = int(tail) if tail.strip() else total - 1
            except ValueError:
                return None
            if start > end or start >= total:
                return "bad"
            return start, min(end, total - 1)

        def _serve_file(self, path: Path):
            try:
                data = path.read_bytes()
            except OSError as exc:
                self._send_plain(404, "读取界面文件失败：%s" % exc)
                return
            total = len(data)
            ctype = _content_type(path)
            span = self._parse_range(total)
            if span == "bad":
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % total)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if span:
                start, end = span
                chunk = data[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(total))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _send_plain(self, code: int, text: str):
            body = text.encode("utf-8", "replace")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except Exception:
                pass

        # ── HTTP 反向代理 ───────────────────────────────────────────────
        def _body_length(self) -> bytes:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                return self._read_chunked_body()
            length = self.headers.get("Content-Length")
            if not length:
                return b""
            try:
                n = int(length)
            except ValueError:
                return b""
            return self.rfile.read(n) if n > 0 else b""

        def _read_chunked_body(self) -> bytes:
            """把客户端发来的 chunked 请求体拼成完整字节（上传体积不大，且服务端自己会限流）。"""
            chunks = []
            while True:
                line = self.rfile.readline(65536).strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65536)          # 尾部 CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)                       # 块尾 CRLF
            return b"".join(chunks)

        def _proxy_http(self):
            body = self._body_length()
            headers = {}
            for key, value in self.headers.items():
                low = key.lower()
                if low in HOP_BY_HOP or low == "host":
                    continue
                headers[key] = value
            headers["Host"] = "%s:%d" % (server.service_host, server.service_port)
            headers.setdefault("Accept-Encoding", "identity")
            if body:
                headers["Content-Length"] = str(len(body))
            elif self.command in ("POST", "PUT", "PATCH", "DELETE"):
                headers["Content-Length"] = "0"

            conn = http.client.HTTPConnection(server.service_host, server.service_port, timeout=60)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except Exception:
                conn.close()
                raise

            try:
                self.send_response(resp.status, resp.reason)
                chunked = resp.getheader("Transfer-Encoding", "").lower() == "chunked"
                declared = resp.getheader("Content-Length")
                for key, value in resp.getheaders():
                    low = key.lower()
                    if low in HOP_BY_HOP or low in RESPONSE_OVERRIDE:
                        continue
                    self.send_header(key, value)
                if chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif declared is not None:
                    self.send_header("Content-Length", declared)
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()

                if self.command == "HEAD" or resp.status in (204, 304):
                    return

                if chunked:
                    # 重新分块：上游的帧已经被 http.client 解掉，必须自己再包一层
                    while True:
                        piece = resp.read(65536)
                        if not piece:
                            break
                        self.wfile.write(b"%x\r\n" % len(piece))
                        self.wfile.write(piece)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()               # SSE / 直播流要立刻出去
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                elif declared is not None:
                    left = int(declared)
                    while left > 0:
                        piece = resp.read(min(65536, left))
                        if not piece:
                            break
                        self.wfile.write(piece)
                        self.wfile.flush()
                        left -= len(piece)
                else:
                    while True:
                        piece = resp.read(65536)
                        if not piece:
                            break
                        self.wfile.write(piece)
                        self.wfile.flush()
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                conn.close()

        # ── WebSocket 升级转发 ─────────────────────────────────────────
        def _proxy_websocket(self):
            """握手原样转发，之后做双向字节中继（不解析帧）。

            注：按 RFC 6455，客户端在收到 101 之前不会发帧，所以 rfile 的预读缓冲里
            不会藏着 WebSocket 数据，直接用底层 socket 中继是安全的。
            """
            upstream = socket.create_connection((server.service_host, server.service_port), timeout=10)
            try:
                lines = ["%s %s HTTP/1.1" % (self.command, self.path)]
                for key, value in self.headers.items():
                    if key.lower() == "host":
                        continue
                    lines.append("%s: %s" % (key, value))
                lines.append("Host: %s:%d" % (server.service_host, server.service_port))
                upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))

                head = b""
                while b"\r\n\r\n" not in head:
                    piece = upstream.recv(4096)
                    if not piece:
                        break
                    head += piece
                if b"\r\n\r\n" not in head:
                    self._send_plain(502, "上游没有完成 WebSocket 握手")
                    return
                head, extra = head.split(b"\r\n\r\n", 1)
                self.connection.sendall(head + b"\r\n\r\n")
                if not head.startswith(b"HTTP/1.1 101"):
                    self.close_connection = True
                    return
                if extra:                       # 上游抢跑的字节，先送到客户端
                    self.connection.sendall(extra)
                self.close_connection = True
                _relay(self.connection, upstream)
            finally:
                try:
                    upstream.close()
                except Exception:
                    pass

        # 默认实现会打日志到 stderr，改成走宿主日志
        def log_message(self, fmt, *args):
            pass

        def log_error(self, fmt, *args):
            server.log("[界面源] " + (fmt % args))

    return Handler


def service_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    """服务是否已经在监听（用于启动页/自检提示）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if __name__ == "__main__":       # 手工排查用：python ui_server.py <ui_root> [服务端口]
    import sys
    import time

    root = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent / "ui")
    sport = int(sys.argv[2]) if len(sys.argv) > 2 else 8010
    srv = LocalUiServer(root, service_port=sport, log=lambda m: print(m, flush=True))
    origin = srv.start()
    print(json.dumps({"origin": origin, "health": origin + "/health",
                      "entry": origin + "/desktop.html", "service_alive": service_alive("127.0.0.1", sport)},
                     ensure_ascii=False, indent=2))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()