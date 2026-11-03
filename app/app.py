# -*- coding: utf-8 -*-
"""AI 数字人桌面版 —— 桌面应用宿主（pywebview + 本地服务托管）。

这个文件就是「桌面应用」的本体：
  * 原生 Windows 窗口（WebView2 内核），标题栏/任务栏图标/单实例都是系统级的；
  * 启动时自己把 LiveTalking 本地服务作为**子进程**拉起来，并在启动页上显示真实进度；
  * 退出时（只有服务是本应用启动的才）把服务停掉，不留后台僵尸进程；
  * 通过 js_api 把「打开系统浏览器 / 打开文件夹 / 重启服务 / 退出应用 / 全屏」等
    本机能力暴露给界面；界面代码（web/app.html）不依赖这些能力也能在浏览器里跑。

命令行：
  AILiveDesktop.exe                        正常启动
  AILiveDesktop.exe --selftest             自检：跑完整启动链路，打印结构化结果后退出
  AILiveDesktop.exe --selftest --close-after 30 --debug-port 9222
                                          自检并开放 CDP 调试端口（供 Playwright 验收）
  AILiveDesktop.exe --reset-profile        清空 WebView 配置目录（登录态/缓存）
  AILiveDesktop.exe --no-service           只开窗口，不启动服务（界面进入离线演示模式）
  AILiveDesktop.exe --project-root <路径>   指定项目根目录
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from desktop_window import (FullscreenController, ESC_EXIT_FULLSCREEN_JS,
                            classify_fullscreen_focus, find_window_by_title, focus_window,
                            recover_taskbar, taskbar_state)
from ui_server import LocalUiServer

APP_NAME = "AI 数字人桌面版"
APP_ID = "AILiveDesktop"
VERSION = "1.4.1"
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8010
SERVICE_URL = "http://%s:%d" % (SERVICE_HOST, SERVICE_PORT)
HEALTH_URL = SERVICE_URL + "/health"
# 桌面端自己的界面入口（随应用打包，见 app\vendor_ui.py）
UI_ENTRY = "desktop.html"
UI_FALLBACK_URL = SERVICE_URL + "/app.html?host=1"
# 本地界面源（同源提供打包界面 + 反向代理接口）；启动后由 start_local_ui() 填好。
_LOCAL_UI = {"server": None, "origin": "", "url": UI_FALLBACK_URL}
# 服务启动参数：与 tools/start_ai_live.ps1 完全一致（已验证过的组合）
DEFAULT_SERVICE_ARGS = [
    "app.py", "--transport", "webrtc", "--model", "wav2lip",
    "--avatar_id", "wav2lip256_avatar1", "--tts", "edgetts",
    "--REF_FILE", "zh-CN-XiaoxiaoNeural",
]
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

IS_FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序所在目录（打包后是 exe 目录）。"""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def res_dir() -> Path:
    """随程序分发的资源目录（PyInstaller 解包目录）。"""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """用户数据目录：配置、WebView 配置、日志兜底。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_PATH = data_dir() / "config.json"
# 全屏期间隐藏了任务栏时会写这个标记，下次启动据此补还原（崩溃兜底）
FULLSCREEN_MARKER = data_dir() / "fullscreen_tray.json"


# ═══════════════════════════════════════════════════════════════════════════
# 桌面端自己的界面来源（本地界面源）
#   * 界面文件随应用打包（app\ui\，由 app\vendor_ui.py 固化），由 ui_server 在
#     127.0.0.1 上以独立端口同源提供 —— 桌面端渲染的不再是网页端那份 app.html；
#   * /api/*、/offer、/whep、/record/* 等数据请求由同一个源反向代理给本地服务，
#     同源带来的是 Cookie、相对路径、iframe、EventSource 全部照旧可用。
# ═══════════════════════════════════════════════════════════════════════════
def ui_dir() -> Path:
    """随应用分发的界面目录（固化副本）。"""
    return res_dir() / "ui"


def ui_origin() -> str:
    """桌面端界面的来源；没有本地界面源时退回服务地址。"""
    return _LOCAL_UI["origin"] or SERVICE_URL


def ui_url() -> str:
    return _LOCAL_UI["url"] or UI_FALLBACK_URL


def ui_is_local() -> bool:
    return bool(_LOCAL_UI["origin"])


def start_local_ui(log) -> str:
    """启动本地界面源；失败不致命，退回加载服务端页面（老行为）并说明原因。"""
    entry = ui_dir() / UI_ENTRY
    if not entry.is_file():
        log("[界面源] 没有找到随应用打包的界面（%s）：本次退回加载服务端页面。"
            "要恢复本地界面，先跑 app\\vendor_ui.py 再重新打包。" % entry)
        return ""
    server = LocalUiServer(ui_dir(), service_host=SERVICE_HOST, service_port=SERVICE_PORT, log=log)
    try:
        origin = server.start()
    except Exception as exc:
        log("[界面源] 本地界面源启动失败：%s（本次退回加载服务端页面）" % exc)
        return ""
    _LOCAL_UI["server"] = server
    _LOCAL_UI["origin"] = origin
    _LOCAL_UI["url"] = "%s/%s?host=1" % (origin, UI_ENTRY)
    log("[界面源] 桌面端界面：%s（界面文件来自 %s，数据仍走 %s）"
        % (_LOCAL_UI["url"], ui_dir(), SERVICE_URL))
    return origin


def stop_local_ui(log=None) -> None:
    server = _LOCAL_UI.get("server")
    if server is None:
        return
    try:
        server.stop()
        if log:
            log("[界面源] 本地界面源已关闭")
    except Exception as exc:
        if log:
            log("[界面源] 关闭本地界面源失败：%s" % exc)
    _LOCAL_UI["server"] = None


def service_url_of(url: str) -> str:
    """把指向本地界面源的地址换回真实服务地址。

    "用浏览器打开工作台 / 手机访问"这类入口必须给外部浏览器一个**不随应用退出而消失**的
    地址；本地界面源的端口只在应用存活期间有效，所以统一换回 8010。
    """
    origin = _LOCAL_UI["origin"]
    if origin and url.startswith(origin):
        return SERVICE_URL + url[len(origin):]
    return url


# 窗口停在哪些地址上算「已经进入应用界面」：桌面端自带入口、网页端入口、登录页
APP_URL_MARKERS = ("desktop.html", "app.html", "login.html")


def is_app_url(url: str) -> bool:
    """窗口是否已经落在应用界面上（启动流程用它判断"不用再导航了"）。"""
    u = url or ""
    return any(marker in u for marker in APP_URL_MARKERS)


def load_config() -> dict:
    cfg = {}
    try:
        if CONFIG_PATH.is_file():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    cfg.setdefault("project_root", "")
    cfg.setdefault("service_args", list(DEFAULT_SERVICE_ARGS))
    cfg.setdefault("window", {"width": 1360, "height": 860, "maximized": False})
    cfg.setdefault("close_service_on_exit", True)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# 窗口坐标超过这个量级一定是垃圾：Windows 最小化占位矩形是 -32000，按 DPI 折算后是 -21332
# （实测坑：窗口在最小化状态下被关掉，旧代码把 -21332 存进了 config.json，下次启动窗口直接
# 跑到屏幕外，用户会以为"程序打不开"）。正常窗口坐标不会超过几千像素，所以阈值取 20000。
GEOM_LIMIT = 20000


def sane_window_cfg(raw: dict) -> dict:
    """把窗口几何洗一遍：最小化占位矩形/越界坐标一律丢掉，尺寸过小也丢掉。

    逐字段独立判断——不要因为坐标是垃圾就把整个 window 段扔掉（尺寸还是好的）。
    """
    out = {}
    for key, low in (("width", 320), ("height", 240)):
        try:
            val = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if val >= low:
            out[key] = val
    for key in ("x", "y"):
        try:
            val = int(raw.get(key))
        except (TypeError, ValueError):
            continue
        if abs(val) < GEOM_LIMIT:
            out[key] = val
    out["maximized"] = bool(raw.get("maximized"))
    return out


class Log:
    """宿主日志：写文件（界面「服务日志」看的就是这个）+ 可选打印到控制台。"""

    def __init__(self, path: Path, echo: bool = False):
        self.path = path
        self.echo = echo
        self.lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.path = data_dir() / "logs" / "host.log"
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with self.lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
            if self.echo:
                try:
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                except Exception:
                    pass

    def repoint(self, path: Path) -> None:
        """定位到项目目录后，把日志挪到项目的 logs 目录（界面「打开日志」看的就是这里）。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = self.path
            self.path = path
            if old and old.is_file() and old != path:
                try:
                    with path.open("a", encoding="utf-8") as f, old.open("r", encoding="utf-8", errors="ignore") as g:
                        f.write(g.read())
                    old.unlink()
                except Exception:
                    pass
            self("日志已切换到 %s" % path)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 项目目录发现：桌面应用本身很小，后端（LiveTalking + env + 模型）还在项目目录里
# ═══════════════════════════════════════════════════════════════════════════
def looks_like_project(root: Path) -> bool:
    try:
        return (root / "LiveTalking" / "app.py").is_file() and (root / "env" / "python.exe").is_file()
    except Exception:
        return False


def project_candidates(explicit: str = "") -> list:
    cands = []
    if explicit:
        cands.append(Path(explicit))
    env_root = os.environ.get("AILIVE_ROOT", "")
    if env_root:
        cands.append(Path(env_root))
    cfg = load_config()
    if cfg.get("project_root"):
        cands.append(Path(cfg["project_root"]))
    here = app_dir()
    for up in range(0, 5):          # exe 在 <项目>\app\dist\AILiveDesktop\ 时的回溯
        try:
            cands.append(here.parents[up])
        except Exception:
            break
    cands.append(Path.cwd())
    for drive in ("E:", "D:", "C:"):
        cands.append(Path(drive + "\\AiLive"))
        cands.append(Path(drive + "\\AiLive\\LiveTalking\\.."))
    cands.append(Path.home() / "AiLive")
    cands.append(Path.home() / "Desktop" / "AiLive")
    seen, out = set(), []
    for c in cands:
        try:
            key = str(c.resolve()).lower()
        except Exception:
            key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def find_project(explicit: str = "") -> Path | None:
    for c in project_candidates(explicit):
        try:
            if looks_like_project(c):
                return c.resolve()
        except Exception:
            continue
    return None


# ═══════════════════════════════════════════════════════════════════════════
# 本地服务进程管理
# ═══════════════════════════════════════════════════════════════════════════
def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError:
        return True          # 有响应就算服务活着（401/302 也算）
    except Exception:
        return False


def auth_state(timeout: float = 4.0):
    """问一下服务端当前登录态：{auth_enabled, authenticated, ...}；问不到返回 None。

    用来决定"进界面之前要不要先去登录页"。桌面端的界面是随应用打包的，服务端拦不到
    它，所以这个判断只能由宿主自己做——否则未登录时会直接进界面并降级成"离线演示"
    （看起来像服务没起来，其实是没登录）。
    """
    try:
        with urllib.request.urlopen(SERVICE_URL + "/api/auth/me", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore") or "{}")
        return data.get("data") if isinstance(data, dict) else {}
    except Exception:
        return None


def ui_entry_url() -> str:
    """进界面的地址：未登录时先给登录页（和网页端 8010 的行为保持一致）。

    登录页也在打包副本里（app\\ui\\web\\login.html），它的表单 POST /api/auth/login 会被
    本地界面源同源代理到服务；登录成功后服务端的 302 与本页的 next 回跳都落在本地界面源上，
    于是又回到桌面端自己的界面。
    """
    if not ui_is_local():
        return ui_url()
    state = auth_state()
    if state is None:                     # 服务不可用：直接进界面，界面会自己降级为离线演示
        return ui_url()
    if state.get("auth_enabled") and state.get("authenticated") is False:
        return "%s/login.html?next=/%s" % (ui_origin(), UI_ENTRY)
    return ui_url()


def port_listening(port: int = SERVICE_PORT) -> bool:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex((SERVICE_HOST, port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def listener_pid(port: int = SERVICE_PORT) -> int | None:
    """占用端口的进程 PID（用 netstat，避免依赖 psutil）。"""
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                      creationflags=CREATE_NO_WINDOW, timeout=8).decode("utf-8", "ignore")
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].endswith(":%d" % port):
                try:
                    return int(parts[4])
                except Exception:
                    continue
    return None


def process_exe(pid: int) -> str:
    """进程可执行文件路径（停止服务前的安全校验：只停项目自带 python）。"""
    try:
        out = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").ExecutablePath" % pid],
            creationflags=CREATE_NO_WINDOW, timeout=15).decode("utf-8", "ignore").strip()
        return out
    except Exception:
        return ""


class ServiceManager:
    """把 LiveTalking 服务当作本应用的子进程来管：启动 / 就绪等待 / 退出即停。"""

    def __init__(self, log: Log):
        self._log = log
        self.proc: subprocess.Popen | None = None
        self.managed = False          # True = 由本应用启动（退出时要停掉）
        self.root: Path | None = None
        self.python = ""
        self.stdout_log = None
        self.stderr_log = None

    # ── 启动 ────────────────────────────────────────────────────────────
    def start(self, root: Path, wait_seconds: int = 150, on_progress=None) -> tuple[bool, str]:
        self.root = Path(root)
        self.python = str(self.root / "env" / "python.exe")
        if not Path(self.python).is_file():
            return False, "找不到项目自带的 Python：%s" % self.python

        if http_ok(HEALTH_URL, 2.0) or port_listening():
            if http_ok(HEALTH_URL, 4.0):
                self.managed = False
                self._log("[服务] 8010 端口已有 LiveTalking 服务在运行，直接复用（退出应用不会停止它）")
                return True, "服务已在运行"
            return False, ("端口 %d 被其它程序占用，但它不是本项目的 LiveTalking 服务。"
                           "请先关闭占用该端口的程序，或在项目的 tools 目录里查端口占用。" % SERVICE_PORT)

        logs = self.root / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except Exception:
            logs = data_dir() / "logs"
        self.stdout_log = (logs / "app_service.stdout.log").open("a", encoding="utf-8", errors="ignore")
        self.stderr_log = (logs / "app_service.stderr.log").open("a", encoding="utf-8", errors="ignore")
        args = load_config().get("service_args") or list(DEFAULT_SERVICE_ARGS)
        cmd = [self.python] + [str(a) for a in args]
        self._log("[服务] 启动：%s（工作目录 %s）" % (" ".join(cmd), self.root / "LiveTalking"))
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(self.root / "LiveTalking"),
                stdout=self.stdout_log, stderr=self.stderr_log, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
        except Exception as exc:
            return False, "启动服务进程失败：%s" % exc
        self.managed = True
        self._log("[服务] 子进程 PID=%d，等待 /health 就绪（最多 %d 秒）" % (self.proc.pid, wait_seconds))

        t0 = time.time()
        step = 0
        while time.time() - t0 < wait_seconds:
            if self.proc.poll() is not None:
                tail = self._tail(self.stderr_log.name, 8) or self._tail(self.stdout_log.name, 8)
                return False, "服务进程提前退出（退出码 %s）。日志末尾：\n%s" % (self.proc.returncode, tail)
            if http_ok(HEALTH_URL, 2.0):
                self._log("[服务] /health 就绪，用时 %.1f 秒" % (time.time() - t0))
                return True, "服务已就绪（用时 %.1f 秒）" % (time.time() - t0)
            if on_progress and time.time() - t0 > step * 5:
                on_progress("正在启动数字人服务…已等待 %d 秒" % int(time.time() - t0))
                step += 1
            time.sleep(1.0)
        tail = self._tail(self.stderr_log.name, 12)
        return False, "服务在 %d 秒内没有就绪。日志末尾：\n%s" % (wait_seconds, tail)

    @staticmethod
    def _tail(path: str, lines: int = 10) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                data = f.readlines()[-lines:]
            return "".join(data).strip()[-1200:]
        except Exception:
            return ""

    # ── 停止 ────────────────────────────────────────────────────────────
    def stop(self, force_after: float = 8.0) -> bool:
        """停止服务：只停「本应用启动的」或「确认是本项目 env\\python.exe 的」进程。"""
        pid = None
        if self.proc and self.proc.poll() is None:
            pid = self.proc.pid
        if pid is None:
            lp = listener_pid()
            if lp is None:
                return True
            exe = process_exe(lp)
            expect = str((self.root / "env" / "python.exe")) if self.root else self.python
            if expect and exe and os.path.normcase(exe) != os.path.normcase(expect):
                self._log("[服务] 拒绝停止 PID=%d（%s 不是本项目 python）" % (lp, exe or "路径未知"))
                return False
            pid = lp
        if pid is None:
            return True
        self._log("[服务] 停止 PID=%d" % pid)
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"], creationflags=CREATE_NO_WINDOW,
                           timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        t0 = time.time()
        while time.time() - t0 < force_after:
            if not port_listening() and (self.proc is None or self.proc.poll() is not None):
                break
            time.sleep(0.5)
        if port_listening():
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATE_NO_WINDOW,
                               timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            t0 = time.time()
            while time.time() - t0 < 8 and port_listening():
                time.sleep(0.5)
        ok = not port_listening()
        self._log("[服务] 停止%s（端口%s）" % ("完成" if ok else "后端口仍被占用",
                                          "已释放" if ok else "仍占用"))
        for f in (self.stdout_log, self.stderr_log):
            try:
                if f:
                    f.close()
            except Exception:
                pass
        self.stdout_log = self.stderr_log = None
        if ok:
            self.proc = None
        return ok


# ═══════════════════════════════════════════════════════════════════════════
# 启动流程状态机（启动页轮询它拿进度）
# ═══════════════════════════════════════════════════════════════════════════
class Bootstrap:
    def __init__(self, log: Log, args):
        self._log = log
        self._args = args
        self.lock = threading.Lock()
        self._window = None
        self.svc = ServiceManager(log)
        self.thread: threading.Thread | None = None
        self.navigated = False
        self.t0 = time.time()
        self.state = {
            "step": "正在准备启动…", "pct": 6, "logs": [], "done": False,
            "error": "", "error_title": "", "diag": "",
            "project_root": "", "version": VERSION, "managed": False,
            "ui_url": ui_url(), "service_url": SERVICE_URL, "port": SERVICE_PORT,
            "ui_local": ui_is_local(), "ui_origin": ui_origin(),
        }

    # ── 状态读写 ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self.lock:
            st = dict(self.state)
            st["logs"] = list(self.state["logs"])
            return st

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def _step(self, text: str, ok=None, pct: int | None = None, running: bool = False) -> None:
        with self.lock:
            if running:
                self.state["logs"] = [it for it in self.state["logs"] if it.get("ok") is not None]
                self.state["logs"].append({"text": text, "ok": None})
            else:
                self.state["logs"] = [it for it in self.state["logs"] if it.get("ok") is not None]
                self.state["logs"].append({"text": text, "ok": bool(ok)})
            self.state["step"] = text
            if pct is not None:
                self.state["pct"] = pct
        self._log("[启动] " + text)

    def fail(self, title: str, text: str, diag: str = "") -> None:
        self._set(error=text, error_title=title, diag=diag or "", done=False)
        self._log("[启动] 失败：%s | %s" % (title, text))

    # ── 主流程 ──────────────────────────────────────────────────────────
    def start_async(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run_guarded, daemon=True)
        self.thread.start()

    def retry(self) -> dict:
        self._set(error="", error_title="", diag="", done=False, pct=6, logs=[],
                  step="正在重新启动…")
        self.t0 = time.time()
        self.start_async()
        return self.snapshot()

    def _run_guarded(self) -> None:
        try:
            self._run()
        except Exception as exc:      # 任何未预期异常都要显示在启动页上，不能白屏
            import traceback
            self.fail("启动过程出现异常", str(exc), traceback.format_exc()[-1200:])

    def _run(self) -> None:
        self._step("初始化桌面应用 v%s" % VERSION, ok=True, pct=10)

        root = find_project(self._args.project_root)
        if root is None:
            self.fail("没有找到 AI 数字人项目目录",
                      "桌面应用需要项目目录（含 LiveTalking\\app.py 与 env\\python.exe）才能启动本地服务。"
                      "请点「选择项目目录」指定，例如 E:\\AiLive。",
                      "已尝试：\n" + "\n".join(str(c) for c in project_candidates(self._args.project_root)[:8]))
            return
        self._set(project_root=str(root))
        self._step("已定位项目目录：%s" % root, ok=True, pct=22)
        self._log.repoint(root / "logs" / "desktop_host.log")

        python_exe = root / "env" / "python.exe"
        if not python_exe.is_file():
            self.fail("项目里缺少 Python 运行环境", "没有找到 %s" % python_exe)
            return
        self._step("Python 运行环境：%s" % python_exe, ok=True, pct=30)

        cfg = load_config()
        cfg["project_root"] = str(root)
        save_config(cfg)

        if self._args.no_service:
            self._step("按参数跳过服务启动（--no-service）", ok=True, pct=70)
        else:
            self._step("正在启动数字人服务（首次加载模型较慢）…", running=True, pct=40)
            ok, msg = self.svc.start(root, wait_seconds=int(self._args.wait),
                                     on_progress=lambda t: self._step(t, running=True))
            if not ok:
                self.fail("数字人服务没能启动", msg,
                          "服务日志：%s\n标准输出：%s" % (root / "logs" / "app_service.stderr.log",
                                                      root / "logs" / "app_service.stdout.log"))
                return
            self._set(managed=bool(self.svc.managed))
            self._step(msg if self.svc.managed else "检测到服务已在运行，直接复用", ok=True, pct=72)

        if http_ok(ui_url(), 4.0):
            self._step("界面可访问（桌面端自带界面）：%s" % ui_url(), ok=True, pct=88)
        elif http_ok(SERVICE_URL + "/app.html", 4.0):
            self._step("界面可访问（未用自带界面，走服务端页面）", ok=True, pct=88)
        else:
            self._step("界面暂时访问不到，仍先打开（界面会提示服务离线）", ok=False, pct=88)

        self._set(done=True, step="启动完成，正在进入应用…", pct=100)
        self._log("[启动] 完成，用时 %.1f 秒，managed=%s" % (time.time() - self.t0, self.svc.managed))

        # 兜底：启动页若没有自己跳转（例如桥接没起来），由宿主加载界面。
        # 注意：load_url 走 WinForms Invoke，实测可能阻塞数分钟，所以只做有限次尝试，
        # 并且每次都以「窗口当前 URL」为准——已经到应用界面就绝不重复加载。
        def fallback():
            time.sleep(4.0)
            if self.navigated:
                self._log("[启动] 启动页已自行跳转到应用界面")
                return
            self._log("[启动] 启动页未自行跳转，由宿主加载界面")
            self.navigate_to_app(tries=8, delay=2.0)
        threading.Thread(target=fallback, daemon=True).start()

    def navigate_to_app(self, tries: int = 15, delay: float = 1.2) -> bool:
        """把窗口带到应用界面，并且**以窗口当前 URL 为准**确认是否成功。

        不能只调用一次就认为成功：启动流程有时 0.x 秒就完成了，此时启动页可能还在加载中，
        随后的启动页导航会把 load_url 覆盖掉（踩过的坑）。所以这里循环重试 + 校验 URL。
        """
        if self._window is None:
            return False
        for i in range(tries):
            cur = ""
            try:
                cur = self._window.get_current_url() or ""
            except Exception:
                cur = ""
            if is_app_url(cur):
                self.navigated = True
                self._log("[启动] 窗口已进入应用界面：%s" % cur)
                return True
            try:
                self._window.load_url(ui_entry_url())
                if i == 0:
                    self._log("[启动] 正在加载界面 %s（当前 %s）" % (ui_entry_url(), cur or "未知"))
            except Exception as exc:
                self._log("[启动] 加载界面失败（第 %d 次）：%s" % (i + 1, exc))
                return False
            time.sleep(delay)
        try:
            cur = self._window.get_current_url() or ""
        except Exception:
            cur = ""
        self._log("[启动] 多次尝试后窗口仍在 %s" % cur)
        return is_app_url(cur)


# ═══════════════════════════════════════════════════════════════════════════
# 暴露给界面的本机能力（window.pywebview.api.*）
# ═══════════════════════════════════════════════════════════════════════════
class HostApi:
    def __init__(self, boot: Bootstrap, log: Log, args):
        self._boot = boot
        self._log = log
        self._args = args
        self._window = None
        self._started_at = time.time()
        self._fullscreen = None      # desktop_window.FullscreenController，窗口建好后注入

    # ── 启动页用 ────────────────────────────────────────────────────────
    def bootstrap_status(self) -> dict:
        return self._boot.snapshot()

    def retry_bootstrap(self) -> dict:
        self._log("[界面] 用户点了重试启动")
        return self._boot.retry()

    def enter_app(self) -> str:
        """启动页拿到 ok 后调用：返回要跳转的地址并标记已跳转。

        未登录时返回的是本地的登录页（与网页端一致），登录完成后仍回到桌面端自带界面。

        这里**不能只把地址交出去就完事**：启动页是用 `enter_app().then(go)` 拿到地址后
        自己 location.replace 的，而 pywebview 的「返回值回调」偶发丢失（本机日志里出现过
        `window.pywebview._returnValuesCallbacks.enter_app... is not a function`）。回调一丢，
        go 永远不执行，启动页就停在「完成，正在进入应用…」不动了；而宿主这边已经把
        navigated 记成 True，原本的兜底导航也不会再触发 —— 表现为**应用启动后卡在启动页**。

        所以：给出地址的同时起一个短延时看门狗，只要还停在启动页，就由宿主自己导航过去。
        """
        self._boot.navigated = True
        url = ui_entry_url()
        try:
            threading.Thread(target=self._ensure_entered, args=(url,), daemon=True,
                             name="enter-app-guard").start()
        except Exception as exc:
            self._log("[启动] 启动看门狗没起来：%s" % exc)
        return url

    def _ensure_entered(self, url: str, delay: float = 1.6) -> None:
        """延时确认启动页是否真的离开了；没离开就由宿主接管导航。"""
        time.sleep(delay)
        win = self._window
        if win is None:
            return
        try:
            cur = win.get_current_url() or ""
        except Exception:
            cur = ""
        if cur and "splash.html" not in cur:
            return                      # 启动页自己跳成功了，什么都不用做
        self._log("[启动] 启动页没有跳转成功（当前 %s），由宿主接管导航：%s" % (cur or "未知", url))
        try:
            win.load_url(url)
        except Exception as exc:
            self._log("[启动] 宿主接管导航失败：%s" % exc)


    def choose_project_folder(self) -> dict:
        """让用户挑项目目录（例如 E:\\AiLive），保存后自动重试启动。"""
        try:
            import webview
            picked = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception as exc:
            self._log("[界面] 打开目录选择框失败：%s" % exc)
            return {"ok": False, "error": str(exc)}
        if not picked:
            return {"ok": False, "error": "未选择目录"}
        path = picked[0] if isinstance(picked, (list, tuple)) else picked
        root = Path(path)
        if not looks_like_project(root):
            self._log("[界面] 选择的目录不是项目目录：%s" % root)
            return {"ok": False, "error": "该目录里没有 LiveTalking\\app.py 与 env\\python.exe，请选择项目根目录（例如 E:\\AiLive）"}
        cfg = load_config()
        cfg["project_root"] = str(root)
        save_config(cfg)
        self._log("[界面] 项目目录已更新为 %s，重新启动" % root)
        self._boot.retry()
        return {"ok": True, "project_root": str(root)}

    # ── 界面用 ──────────────────────────────────────────────────────────
    def get_app_info(self) -> dict:
        root = self._boot.state.get("project_root") or ""
        log_dir = str(Path(root) / "logs") if root else str(data_dir() / "logs")
        info = {
            "managed": bool(self._boot.svc.managed),
            "version": VERSION,
            "frozen": IS_FROZEN,
            "project_root": root,
            "log_dir": log_dir,
            "webview": "WebView2（Chromium 内核）",
            "host_os": "Windows %s" % (".".join(str(x) for x in sys.getwindowsversion()[:3])
                                       if hasattr(sys, "getwindowsversion") else ""),
            "python": sys.version.split()[0],
            "service_url": SERVICE_URL,
            "ui_url": ui_url(),
            "ui_origin": ui_origin(),
            "ui_local": ui_is_local(),
            "ui_dir": str(ui_dir()),
            "port": SERVICE_PORT,
            "service_pid": (self._boot.svc.proc.pid if self._boot.svc.proc else (listener_pid() or 0)),
            "service_exe": str(Path(root) / "env" / "python.exe") if root else "",
            "host_pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._started_at)),
            "uptime_seconds": int(time.time() - self._started_at),
            "config_path": str(CONFIG_PATH),
            "log_path": str(self._log.path),
        }
        self._log("[界面] get_app_info -> managed=%s root=%s" % (info["managed"], root))
        return info

    def open_external(self, url: str) -> bool:
        """用系统默认浏览器打开（只允许 http/https）。

        指向本地界面源的地址要换回真实服务地址：本地界面源的端口只在本应用存活期间有效，
        丢给外部浏览器会打不开。
        """
        url = service_url_of(str(url or ""))
        if not (url.startswith("http://") or url.startswith("https://")):
            self._log("[界面] 拒绝打开非 http 链接：%s" % url)
            return False
        self._log("[界面] 用系统浏览器打开：%s" % url)
        try:
            os.startfile(url)          # noqa: S606 - 本地应用，按用户请求打开链接
            return True
        except Exception as exc:
            self._log("[界面] 打开链接失败：%s" % exc)
            return False

    def open_folder(self, path: str) -> bool:
        try:
            p = Path(path)
            p.mkdir(parents=True, exist_ok=True)
            os.startfile(str(p))
            self._log("[界面] 打开目录：%s" % p)
            return True
        except Exception as exc:
            self._log("[界面] 打开目录失败 %s：%s" % (path, exc))
            return False

    def open_log_folder(self) -> bool:
        root = self._boot.state.get("project_root") or ""
        return self.open_folder(str(Path(root) / "logs") if root else str(data_dir() / "logs"))

    def open_project_folder(self) -> bool:
        root = self._boot.state.get("project_root") or ""
        if not root:
            return False
        return self.open_folder(root)

    def restart_service(self) -> dict:
        root = self._boot.state.get("project_root") or ""
        if not root:
            return {"ok": False, "error": "还不知道项目目录"}
        self._log("[界面] 请求重启服务")
        self._boot.svc.stop()
        ok, msg = self._boot.svc.start(Path(root), wait_seconds=int(self._args.wait))
        self._boot._set(managed=bool(self._boot.svc.managed))
        return {"ok": ok, "message": msg}

    def toggle_fullscreen(self) -> bool:
        """切换真正铺满屏幕的全屏（见 desktop_window.FullscreenController）。

        返回值是**切换后的状态**：True=已全屏，False=已退出。界面可以据此更新菜单文案。
        """
        if self._fullscreen is None:
            self._log("[界面] 全屏控制器还没准备好")
            return False
        try:
            state = bool(self._fullscreen.toggle())
            self._log("[界面] 切换全屏 -> %s" % ("已全屏" if state else "已退出全屏"))
            return state
        except Exception as exc:
            self._log("[界面] 切换全屏失败：%s" % exc)
            return False

    def is_fullscreen(self) -> bool:
        """界面用来判断 Esc / 菜单该显示什么。"""
        return bool(self._fullscreen and self._fullscreen.is_fullscreen)

    def set_window_title(self, text: str) -> bool:
        """让原生窗口标题跟着当前标签走。

        界面切换标签时 document.title 会变，但 pywebview 的原生窗口标题**不会**自动
        跟，于是任务栏悬停、Alt+Tab 里永远只有「AI 数字人桌面版」，切到哪个模块看不出来。
        这里由界面显式把标题推过来。空标题或不合法内容一律回落到应用名，避免窗口变成
        空白标题（那种窗口在任务栏里只剩一个图标，反而更难找）。
        """
        if self._window is None:
            return False
        title = " ".join(str(text or "").split())[:120] or APP_NAME
        try:
            self._window.title = title
            return True
        except Exception as exc:
            self._log("[窗口] 设置标题失败（%s）：%s" % (title, exc))
            return False

    def get_fullscreen_state(self) -> dict:
        """排障与自检用：把 Win32 层的真实状态读出来（显示器矩形 / 窗口矩形 / 是否置顶）。"""
        if self._fullscreen is None:
            return {"available": False}
        ctl = self._fullscreen
        tray = ctl.tray_state()
        return {"available": True, "fullscreen": ctl.is_fullscreen,
                "topmost": ctl.is_topmost(), "monitor": ctl.monitor_rect(),
                "window_rect": ctl.window_rect(), "covers": ctl.covers_monitor(),
                "focus": classify_fullscreen_focus(ctl.hwnd()),
                "taskbar_autohide": tray["autohide"],
                "taskbar_visible": len(tray["visible"]),
                "taskbar_hidden_by_app": len(tray["hidden_by_app"])}

    def exit_app(self) -> bool:
        self._log("[界面] 用户请求退出应用")
        try:
            close_window(self._boot, self._window, self._log, 0,
                         reason="界面请求退出后主循环未结束")
            return True
        except Exception as exc:
            self._log("[界面] 退出失败：%s" % exc)
            return False

    def save_settings(self, patch: dict) -> dict:
        """界面可以改宿主配置（例如关闭“退出即停服务”）。"""
        cfg = load_config()
        if isinstance(patch, dict):
            for k in ("close_service_on_exit", "service_args"):
                if k in patch:
                    cfg[k] = patch[k]
        save_config(cfg)
        return {"ok": True, "config": cfg}

    def get_settings(self) -> dict:
        return load_config()


# ═══════════════════════════════════════════════════════════════════════════
# 单实例 + 窗口 + 主流程
# ═══════════════════════════════════════════════════════════════════════════
_LOCK_SOCK = None


def acquire_single_instance(port: int = 8011) -> bool:
    global _LOCK_SOCK
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind((SERVICE_HOST, port))
        s.listen(1)
        _LOCK_SOCK = s          # 进程存活期间一直持有，退出自动释放
        return True
    except Exception:
        return False


def message_box(text: str, title: str = APP_NAME, flags: int = 0x40) -> None:
    """原生提示框（不依赖 tkinter）。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


def splash_url() -> str:
    """启动页地址：必须给**普通文件路径**，不能给 file:// URI。

    pywebview 的 is_local_url() 名字与语义相反——它判断的是「是不是普通文件路径」，
    file:// 开头的反而返回 False。给 file:// 时 pywebview 不会为它起本地 http 服务，
    页面就停在 file:// 上，js_api 端点不存在 → window.pywebview.api 里一个方法都没有
    （实测踩到的大坑：启动页拿不到桥接，也就无法显示进度）。给纯路径时 pywebview 会用
    http://127.0.0.1:<port>/splash.html 提供它，桥接才完整可用。
    """
    p = res_dir() / "splash.html"
    return str(p.resolve())


def api_func_list(js_api) -> list:
    """自己算一份 js_api 方法清单（只取公开方法），用于缺方法时手动补注入。"""
    import inspect
    out = []
    for name in dir(js_api):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(js_api, name)
        except Exception:
            continue
        if inspect.ismethod(attr) or inspect.isfunction(attr):
            try:
                params = list(inspect.getfullargspec(attr).args)[1:]
            except Exception:
                params = []
            out.append({"func": name, "params": params})
    return out


BRIDGE_CHECK_JS = ("(function(){try{return !!(window.pywebview && window.pywebview.api && "
                   "typeof window.pywebview.api.get_app_info === 'function');}catch(e){return false;}})()")


def ensure_bridge(window, js_api, log, tries: int = 5) -> bool:
    """确认 window.pywebview.api 上真的有我们的方法；缺失就手动补一次。

    pywebview 是在每次导航的 before_load 阶段注入方法的，偶发竞态下会漏（尤其首个页面），
    而界面的「打开浏览器 / 打开文件夹 / 重启服务 / 退出应用」都靠它，所以这里做一次自检+补注入。
    """
    inject_js = ("(function(){try{ window.pywebview._createApi(%s); return true; }"
                 "catch(e){ return false; }})()" % json.dumps(api_func_list(js_api), ensure_ascii=False))
    for i in range(tries):
        try:
            if window.evaluate_js(BRIDGE_CHECK_JS):
                if i:
                    log("[桥接] 第 %d 次检查：js_api 方法已可用" % (i + 1))
                return True
            log("[桥接] 检测到 js_api 方法缺失，手动补注入（第 %d 次）" % (i + 1))
            window.evaluate_js(inject_js)
        except Exception as exc:
            log("[桥接] 检查/补注入失败：%s" % exc)
            return False
        time.sleep(0.8)
    return False


def start_bridge_keepalive(window, js_api, log, boot, interval: float = 1.0,
                           tail_seconds: float = 30.0) -> None:
    """反复补注入 js_api 方法（直到启动完成后再多补 30 秒）。

    为什么必须自己做：pywebview 注入 api.js 之后，紧接着执行的 finish 脚本
    （window.pywebview._createApi(...)）在首个页面上会赶在 api.js 生效之前跑，抛错被吞掉，
    结果 window.pywebview.api 是个空对象——启动页因此拿不到桥接、没法显示进度。
    这里用 window.run_js（只等 before_load，不等 _pywebviewready）反复补，幂等且无副作用。
    """
    js = ("(function(){try{if(window.pywebview&&window.pywebview._createApi){"
          "window.pywebview._createApi(%s);return 1;}return 0;}catch(e){return 0;}})()"
          % json.dumps(api_func_list(js_api), ensure_ascii=False))

    def _loop():
        n = 0
        done_at = None
        while True:
            try:
                window.run_js(js)
                n += 1
            except Exception as exc:
                log("[桥接] 补注入中断：%s" % exc)
                return
            st = boot.snapshot()
            if st.get("done") or st.get("error"):
                done_at = done_at or time.time()
                if time.time() - done_at > tail_seconds:
                    break
            time.sleep(interval)
        log("[桥接] 补注入结束，共执行 %d 次（每次导航都会重新注入，这是竞态兜底）" % n)
    threading.Thread(target=_loop, daemon=True).start()


def arm_exit_watchdog(boot, log, code: int = 0, delay: float = 25.0,
                      reason: str = "窗口已关闭但主循环没有退出") -> None:
    def _run():
        time.sleep(delay)
        log("[退出] %s → 强制结束进程（退出码 %d）" % (reason, code))
        try:
            if boot.svc.managed and load_config().get("close_service_on_exit", True):
                boot.svc.stop(force_after=4.0)
        except Exception:
            pass
        os._exit(code)
    threading.Thread(target=_run, daemon=True).start()


def close_window(boot, window, log, code: int = 0,
                 reason: str = "窗口关闭后主循环未退出") -> None:
    """统一退出：先关掉 pywebview 的原生“确认退出”弹窗，再销毁窗口，并挂上保命看门狗。

    为什么不直接 destroy()：pywebview 的 on_closing 里如果 confirm_close 为真，会弹一个
    MessageBox 等用户点确定，主循环就一直不返回（实测被卡了 25 秒才被看门狗强杀）。
    界面自己已经问过用户了，所以这里必须先把 confirm_close 关掉。
    """
    try:
        if window is not None:
            window.confirm_close = False
    except Exception:
        pass
    arm_exit_watchdog(boot, log, code, delay=12.0, reason=reason)
    try:
        if window is not None:
            window.destroy()
    except Exception as exc:
        log("[退出] 销毁窗口失败：%s" % exc)


JS_PROBE = r"""
(function(){
  try{
    var q = function(sel){ return document.querySelectorAll(sel).length; };
    var t = function(sel){ var e = document.querySelector(sel); return e ? (e.innerText || '').trim() : ''; };
    return JSON.stringify({
      url: location.href,
      title: document.title,
      ready: document.readyState,
      hasAppShell: !!document.getElementById('app'),
      navCount: q('#navList .nav'),
      navGroups: q('#navList .grp'),
      tabCount: q('#tabbar .tab[data-app]'),
      mode: t('#modeText'),
      statusService: t('#stSvc'),
      brand: getComputedStyle(document.documentElement).getPropertyValue('--brand').trim(),
      hasBridge: !!(window.pywebview && window.pywebview.api),
      panes: q('#panes .pane')
    });
  }catch(e){ return JSON.stringify({error: String(e && e.message || e)}); }
})()
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=APP_NAME, add_help=True)
    ap.add_argument("--selftest", action="store_true", help="自检模式：跑完启动链路打印结果后退出")
    ap.add_argument("--close-after", type=int, default=0, help="自检模式：N 秒后自动关闭窗口")
    ap.add_argument("--debug-port", type=int, default=0, help="开放 CDP 调试端口（供自动化验收）")
    ap.add_argument("--reset-profile", action="store_true", help="启动前清空 WebView 配置目录")
    ap.add_argument("--clear-cookies", action="store_true", help="清空 WebView 登录态后退出")
    ap.add_argument("--no-service", action="store_true", help="不启动本地服务（界面进离线演示）")
    ap.add_argument("--project-root", default="", help="指定项目根目录")
    ap.add_argument("--wait", type=int, default=150, help="等待服务就绪的最长秒数")
    ap.add_argument("--allow-multiple", action="store_true", help="允许同时运行多个实例")
    ap.add_argument("--minimized", action="store_true", help="以最小化方式启动")
    ap.add_argument("--test-fullscreen", action="store_true",
                    help="自检时顺带实测全屏：进入全屏→读窗口/显示器矩形与置顶状态→退出")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.reset_profile or args.clear_cookies:
        target = data_dir() / "webview"
        shutil.rmtree(target, ignore_errors=True)
        print("已清空 WebView 配置目录：%s" % target)

    if not args.allow_multiple and not acquire_single_instance():
        # 已经有实例在跑：把它提到前台，而不是丢一个「已经在运行了」的消息框让用户
        # 自己去任务栏里翻。找不到窗口（例如上次是被强杀、只剩锁）才退回提示框。
        hwnd = find_window_by_title(APP_NAME)
        if hwnd and focus_window(hwnd):
            print("已有实例在运行，已切换到该窗口（hwnd=%s）" % hwnd)
            return 0
        message_box("AI 数字人桌面版已经在运行了。\n\n请在任务栏里切换到已打开的窗口；"
                    "如果确认没有窗口，请先在任务管理器结束 AILiveDesktop.exe 再试。",
                    APP_NAME, 0x30)
        return 0

    cfg = load_config()
    log = Log(Path(cfg["project_root"]) / "logs" / "desktop_host.log" if cfg.get("project_root")
              else data_dir() / "logs" / "desktop_host.log", echo=args.selftest)
    log("=" * 78)
    log("%s v%s 启动（frozen=%s, pid=%d, argv=%s）" % (APP_NAME, VERSION, IS_FROZEN, os.getpid(),
                                                    " ".join(sys.argv[1:])))
    log("配置文件：%s" % CONFIG_PATH)

    # 全屏崩溃兜底：上次在全屏里被强杀，任务栏可能还是隐藏状态，先补还原
    recover_taskbar(FULLSCREEN_MARKER, log)

    # 先把「桌面端自己的界面源」起起来：窗口从它加载，界面文件来自随应用的固化副本，
    # 数据请求由它反向代理给 8010（详见 app\ui_server.py 顶部说明）。
    start_local_ui(log)

    import webview

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["SHOW_DEFAULT_MENUS"] = False
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False
    if args.debug_port:
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(args.debug_port)
        log("已开放 CDP 调试端口 %d" % args.debug_port)

    boot = Bootstrap(log, args)
    api = HostApi(boot, log, args)

    win_raw = cfg.get("window") or {}
    win_cfg = sane_window_cfg(win_raw)
    dropped = [k for k in ("x", "y", "width", "height")
               if win_raw.get(k) is not None and k not in win_cfg]
    if dropped:
        log("[窗口] 配置里的窗口几何有异常项 %s（x=%s y=%s w=%s h=%s），已忽略并让系统居中"
            % (dropped, win_raw.get("x"), win_raw.get("y"), win_raw.get("width"), win_raw.get("height")))
    icon = res_dir() / "app.ico"
    try:
        window = webview.create_window(
            APP_NAME,
            url=splash_url(),
            js_api=api,
            width=int(win_cfg.get("width") or 1360),
            height=int(win_cfg.get("height") or 860),
            x=win_cfg.get("x"), y=win_cfg.get("y"),
            min_size=(1024, 660),
            resizable=True,
            maximized=bool(win_cfg.get("maximized")),
            minimized=bool(args.minimized),
            background_color="#0f2b40",
            confirm_close=True,
            text_select=False,
            easy_drag=False,
        )
    except Exception as exc:
        log("创建窗口失败：%s" % exc)
        message_box("创建窗口失败：%s" % exc)
        return 3

    boot._window = window
    # 注意：js_api 对象上的属性一律用下划线开头。pywebview 的 get_functions() 会递归遍历
    # js_api 的所有非下划线属性去找要暴露的方法；一旦它走到 Window（→ WinForms → WebView2）这个
    # 原生对象图，就会疯狂递归（Bounds.Empty.Empty…）甚至跨线程访问 COM，结果 js_api 方法列表残缺、
    # 整个进程还会卡上几分钟。这是实测踩到的大坑。
    api._window = window

    # 全屏控制器：自己实现的「无边框 + 精确铺屏 + 活动期置顶」，见 desktop_window.py
    fullscreen = FullscreenController(window, log, marker_path=FULLSCREEN_MARKER)
    api._fullscreen = fullscreen

    def on_loaded():
        try:
            cur = window.get_current_url() or "?"
        except Exception:
            cur = "?"
        log("[窗口] 页面加载完成：%s" % cur)

        # 应用界面（http 页面）加载完成后，确认桥接方法可用；缺失就补注入
        def after_load():
            try:
                time.sleep(1.0)
                ok = ensure_bridge(window, api, log)
                log("[桥接] 界面 %s 的 js_api 可用性检查：%s" % (cur, "通过" if ok else "未通过"))
                # Esc 退出全屏：宿主侧注入，界面文件保持与网页端一致的原文
                try:
                    window.run_js(ESC_EXIT_FULLSCREEN_JS)
                except Exception as exc:
                    log("[全屏] 注入 Esc 快捷键失败：%s" % exc)
            except Exception as exc:
                log("[桥接] 检查异常：%s" % exc)
        threading.Thread(target=after_load, daemon=True).start()

    def on_closing():
        try:
            st = window.state
            cfg2 = load_config()
            old = sane_window_cfg(cfg2.get("window") or {})
            ctl = getattr(api, "_fullscreen", None)
            minimized = bool(ctl.is_minimized()) if ctl is not None else False
            cur = {} if minimized else sane_window_cfg(
                {"x": window.x, "y": window.y, "width": window.width, "height": window.height})
            merged = dict(old)
            for key in ("x", "y", "width", "height"):
                if key in cur:
                    merged[key] = cur[key]
            merged["maximized"] = bool(st.get("is_maximized", False))
            cfg2["window"] = merged
            save_config(cfg2)
            if minimized:
                log("[窗口] 关闭时窗口是最小化的（量不到有效坐标），保留上次的位置：%s" % merged)
            else:
                log("[窗口] 关闭，窗口几何已保存：%s" % merged)
        except Exception as exc:
            log("[窗口] 保存窗口尺寸失败：%s" % exc)

    try:
        window.events.loaded += on_loaded
        window.events.closing += on_closing
    except Exception as exc:
        log("注册窗口事件失败：%s" % exc)

    # 桥接兜底：反复补注入 js_api 方法（pywebview 在首个页面上会漏注入）
    start_bridge_keepalive(window, api, log, boot)

    # 自检模式也顺带把「桥接到底可不可用」记进日志，作为证据
    def bridge_evidence():
        time.sleep(4.0)
        try:
            ok = ensure_bridge(window, api, log)
            log("[桥接] 首次页面 js_api 可用性：%s" % ("通过" if ok else "未通过"))
        except Exception as exc:
            log("[桥接] 可用性检查异常：%s" % exc)
    threading.Thread(target=bridge_evidence, daemon=True).start()

    boot.start_async()

    # ── 自检模式：等启动完成 → 探查界面 → 定时关闭 ──────────────────────
    if args.selftest:
        def selftest_watch():
            t_start = time.time()
            deadline = t_start + max(60, int(args.wait) + 30)
            while time.time() < deadline:
                st = boot.snapshot()
                if st.get("done") or st.get("error"):
                    break
                time.sleep(0.5)
            st = boot.snapshot()
            log("[自检] 启动状态：done=%s error=%s managed=%s pct=%s" %
                (st.get("done"), st.get("error") or "-", st.get("managed"), st.get("pct")))
            for it in st.get("logs", []):
                log("[自检]   %s %s" % ("OK " if it.get("ok") else (".. " if it.get("ok") is None else "NG "), it.get("text")))
            if args.close_after:
                time.sleep(min(12, max(3, args.close_after - 12)))
            if st.get("done"):
                if not boot.navigated:
                    boot.navigate_to_app()   # 以窗口真实 URL 为准反复确认，不再依赖启动页脚本
                time.sleep(2.0)
                try:
                    probe = window.evaluate_js(JS_PROBE)
                except Exception as exc:
                    probe = json.dumps({"error": str(exc)})
                log("[自检] 界面探查：%s" % probe)
                try:
                    info = api.get_app_info()
                    log("[自检] 宿主信息：%s" % json.dumps({k: info[k] for k in
                        ("managed", "frozen", "project_root", "port", "service_pid", "webview",
                         "ui_url", "ui_origin", "ui_local", "ui_dir")},
                        ensure_ascii=False))
                except Exception as exc:
                    log("[自检] 读取宿主信息失败：%s" % exc)
                # 全屏实测：进入→读真实矩形→退出，把证据写进日志
                try:
                    before = api.get_fullscreen_state()
                    log("[自检] 全屏前：%s" % json.dumps(before, ensure_ascii=False))
                    if args.test_fullscreen:
                        api.toggle_fullscreen()
                        time.sleep(1.5)
                        during = api.get_fullscreen_state()
                        log("[自检] 全屏中：%s" % json.dumps(during, ensure_ascii=False))
                        log("[自检] 全屏铺满显示器：%s；置顶（任务栏压不上来）：%s"
                            % (during.get("covers"), during.get("topmost")))
                        api.toggle_fullscreen()
                        time.sleep(1.0)
                        log("[自检] 退出全屏后：%s"
                            % json.dumps(api.get_fullscreen_state(), ensure_ascii=False))
                except Exception as exc:
                    log("[自检] 全屏实测失败：%s" % exc)
            if args.close_after:
                left = args.close_after - (time.time() - t_start)
                if left > 0:
                    time.sleep(left)
                log("[自检] 自动关闭窗口")
                close_window(boot, window, log, 0 if st.get("done") else 1,
                             reason="自检自动关闭后主循环未结束")
        threading.Thread(target=selftest_watch, daemon=True).start()

    localization = {
        "global.quitConfirmation": "关闭「AI 数字人桌面版」？\n\n如果数字人服务是由本应用启动的，退出会同时停止服务；"
                                  "如果是外部已经开着的服务，本应用不会去动它。",
        "global.ok": "确定", "global.cancel": "取消", "global.quit": "退出", "global.save": "保存",
        "global.retry": "重试", "global.file": "文件", "global.edit": "编辑", "global.view": "视图",
        "global.window": "窗口", "global.help": "帮助", "global.about": "关于",
    }
    storage = data_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    try:
        webview.start(gui="edgechromium", debug=bool(args.debug_port), private_mode=False,
                      storage_path=str(storage), icon=str(icon) if icon.is_file() else None,
                      localization=localization,
                      # 关键：http_server=True 时 pywebview 会把本地启动页用 http 提供，
                      # 并建立 js_api 端点；否则 file:// 启动页上的 window.pywebview.api
                      # 没有方法可调（这是实测踩到的坑）。
                      http_server=True)
    except Exception as exc:
        log("WebView2 启动失败：%s" % exc)
        message_box("无法启动内置浏览器内核（WebView2）。\n\n%s\n\n"
                    "请安装 Microsoft Edge WebView2 Runtime 后重试：\n"
                    "https://developer.microsoft.com/microsoft-edge/webview2/" % exc, APP_NAME, 0x10)
        exit_code = 4
    finally:
        st = boot.snapshot()
        if st.get("error"):
            exit_code = exit_code or 1
        try:
            fullscreen.stop()
        except Exception:
            pass
        stop_local_ui(log)
        if boot.svc.managed and load_config().get("close_service_on_exit", True):
            log("[退出] 服务由本应用启动，正在停止它…")
            boot.svc.stop()
        else:
            log("[退出] 服务不是本应用启动的，保持运行（%s）" % ("外部服务" if not boot.svc.managed else "按配置保留"))
        log("%s 退出，退出码 %d" % (APP_NAME, exit_code))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())