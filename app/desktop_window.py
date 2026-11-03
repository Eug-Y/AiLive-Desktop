# -*- coding: utf-8 -*-
"""桌面窗口的本机能力：真正铺满屏幕的全屏切换。

为什么不用 pywebview 自带的 toggle_fullscreen
---------------------------------------------
pywebview 的做法是「去掉边框 + 最大化 + 把 Bounds 设成显示器矩形」。矩形本身是对的，但它
**没有把窗口置顶**，而 Windows 的任务栏（Shell_TrayWnd）带 ``WS_EX_TOPMOST``：

    实测（本机 2560x1600 @150%，任务栏自动隐藏）
      Shell_TrayWnd hwnd=196646 exstyle=0x00000088 WS_EX_TOPMOST=True

也就是说，只要鼠标碰到屏幕底边，自动隐藏的任务栏就会滑出来**盖在无边框全屏窗口之上**，
这正是「全屏的时候没有完全覆盖」。系统只在它认定"前台窗口是真全屏"时才抑制自动隐藏任务栏，
而这个判断在 WebView2 宿主窗口上并不可靠。

这里的做法
----------
1. 边框：走 WinForms 的 ``FormBorderStyle=None``（让 WinForms 自己的状态与之一致，避免它
   稍后把边框改回来）；几何与置顶用 Win32 直接做，取的是**物理像素**的显示器矩形，
   多显示器 / 高 DPI 都准。
2. 置顶：进入全屏后窗口设为 TOPMOST，任务栏再也压不上来；但**只在窗口是前台时置顶**，
   一旦用户切到别的程序立刻降回普通层，免得这个全屏窗口挡住别人（0.4s 看门狗维持）。
3. 退出：把进入前保存的窗口样式与 ``WINDOWPLACEMENT`` 原样放回去（含原来的最大化状态）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040

MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2
GW_OWNER = 4

LONG_PTR = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT),
                ("ptMinPosition", wt.POINT), ("ptMaxPosition", wt.POINT),
                ("rcNormalPosition", RECT)]


# ── 函数签名（必须显式声明：句柄是高 64 位值，不声明 restype 会被截断成 32 位）──
user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.IsWindow.argtypes = [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsZoomed.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
# 单实例激活用：句柄是 64 位值，不声明 argtypes 会被截成 32 位并调到错误的窗口上
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.BringWindowToTop.argtypes = [wt.HWND]

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNA = 8
ABM_GETSTATE = 0x00000004
ABS_AUTOHIDE = 0x00000001
ABS_ALWAYSONTOP = 0x00000002
CLSCTX_INPROC_SERVER = 1


class APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uCallbackMessage", wt.UINT),
                ("uEdge", wt.UINT), ("rc", RECT), ("lParam", LONG_PTR)]
_GET_LONG = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_SET_LONG = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
_GET_LONG.restype = LONG_PTR
_GET_LONG.argtypes = [wt.HWND, ctypes.c_int]
_SET_LONG.restype = LONG_PTR
_SET_LONG.argtypes = [wt.HWND, ctypes.c_int, LONG_PTR]

HWND_TOPMOST = wt.HWND(-1)
HWND_NOTOPMOST = wt.HWND(-2)


def _get_style(hwnd: int, index: int) -> int:
    return int(_GET_LONG(wt.HWND(hwnd), index) or 0)


def _set_style(hwnd: int, index: int, value: int) -> None:
    _SET_LONG(wt.HWND(hwnd), index, LONG_PTR(value))


def _place(hwnd: int, insert_after, x: int, y: int, cx: int, cy: int, flags: int) -> None:
    user32.SetWindowPos(wt.HWND(hwnd), insert_after, int(x), int(y), int(cx), int(cy), int(flags))


def classify_fullscreen_focus(hwnd: int) -> str:
    """前台窗口与本窗口的关系：self / owned / other / none。"""
    fg = int(user32.GetForegroundWindow() or 0)
    if not fg:
        return "none"
    if fg == hwnd or int(user32.GetAncestor(wt.HWND(fg), GA_ROOT) or 0) == hwnd:
        return "self"
    if int(user32.GetWindow(wt.HWND(fg), GW_OWNER) or 0) == hwnd:
        return "owned"
    return "other"


# ═══════════════════════════════════════════════════════════════════════════
# 任务栏：全屏期间必须让它消失
#   实测（本机）：Shell_TrayWnd 是置顶窗口；把无边框全屏窗口也设成置顶**依然不够**——
#   鼠标碰到屏幕底边时外壳会再次把任务栏抬到所有置顶窗口之上（实测 WindowFromPoint 在
#   屏幕底部命中的仍是 Shell_TrayWnd）。所以真正的确定性做法是：全屏期间隐藏任务栏，
#   退出/失焦立刻还原（游戏全屏就是这么做的）。崩溃兜底：隐藏期间写一个标记文件，
#   下次启动时若发现任务栏还是隐藏状态就把它还原。
# ═══════════════════════════════════════════════════════════════════════════
def tray_windows() -> list:
    """主任务栏 + 各显示器上的次级任务栏。"""
    out = []
    for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        h = int(user32.FindWindowW(cls, None) or 0)
        if h and h not in out:
            out.append(h)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == "Shell_SecondaryTrayWnd":
            v = int(hwnd)
            if v not in out:
                out.append(v)
        return True

    user32.EnumWindows(cb, 0)
    return out


def taskbar_state() -> dict:
    abd = APPBARDATA()
    abd.cbSize = ctypes.sizeof(APPBARDATA)
    bits = int(shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd)) or 0)
    trays = tray_windows()
    return {"autohide": bool(bits & ABS_AUTOHIDE), "always_on_top": bool(bits & ABS_ALWAYSONTOP),
            "hwnds": trays, "visible": [h for h in trays if user32.IsWindowVisible(wt.HWND(h))]}


def hide_taskbar() -> list:
    """隐藏任务栏，返回被隐藏的句柄（退出时按这份清单还原）。"""
    hidden = []
    for h in tray_windows():
        if user32.IsWindowVisible(wt.HWND(h)):
            user32.ShowWindow(wt.HWND(h), SW_HIDE)
            hidden.append(h)
    return hidden


def show_taskbar(hwnds=None) -> None:
    """还原任务栏（SW_SHOWNA：显示但不抢焦点，保住自动隐藏行为）。"""
    for h in (hwnds if hwnds is not None else tray_windows()):
        user32.ShowWindow(wt.HWND(h), SW_SHOWNA)


def recover_taskbar(marker_path, log=None) -> bool:
    """崩溃/被强杀兜底：上次隐藏任务栏后没来得及还原，这次启动补上还原。"""
    log = log or (lambda _m: None)
    if marker_path is None:
        return False
    try:
        from pathlib import Path
        p = Path(marker_path)
        if not p.exists():
            return False
        show_taskbar()
        p.unlink()
        log("[全屏] 上次退出时任务栏没来得及还原，已补还原（%s）" % p)
        return True
    except Exception as exc:
        log("[全屏] 补还原任务栏失败：%s" % exc)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# 单实例：第二次启动时把已经在跑的那个窗口提到前台
#   原来的行为是弹一个「已经在运行了」的消息框，用户还得自己去任务栏找窗口。
#   标题里的模块名会随标签变化（「控制中心 — AI 数字人桌面版」），所以这里按
#   **标题包含**匹配，而不是等于。
# ═══════════════════════════════════════════════════════════════════════════
SW_RESTORE = 9
SW_SHOW = 5


def find_window_by_title(part: str) -> int:
    """按标题包含关系找一个可见的顶层窗口，返回句柄；找不到返回 0。"""
    if not part:
        return 0
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = int(user32.GetWindowTextLengthW(hwnd) or 0)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if part in buf.value:
                hits.append(int(hwnd))
                return False        # 找到就停
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return hits[0] if hits else 0


def focus_window(hwnd: int) -> bool:
    """把窗口从最小化还原并提到前台（尽力而为：前台锁由系统决定，可能被拒）。"""
    if not hwnd or not user32.IsWindow(wt.HWND(hwnd)):
        return False
    user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE if user32.IsIconic(wt.HWND(hwnd)) else SW_SHOW)
    user32.BringWindowToTop(wt.HWND(hwnd))
    user32.SetForegroundWindow(wt.HWND(hwnd))
    return True


def mark_fullscreen_window(hwnd: int, on: bool) -> bool:
    """ITaskbarList2::MarkFullscreenWindow —— 正式告知外壳"这是全屏窗口"。

    vtable：IUnknown(0-2) → HrInit(3) AddTab(4) DeleteTab(5) ActivateTab(6) SetActiveAlt(7)
            → MarkFullscreenWindow(8)。调用放在 try 里，失败不影响功能（还有隐藏任务栏兜底）。
    """
    clsid = ctypes.c_void_p()
    iid = ctypes.c_void_p()
    try:
        ole32.CLSIDFromString(ctypes.c_wchar_p("{56FDF344-FD6D-11d0-958A-006097C9A090}"),
                              ctypes.byref(clsid))
        ole32.CLSIDFromString(ctypes.c_wchar_p("{602D4995-B13A-429b-A66E-1935E44F4317}"),
                              ctypes.byref(iid))
        ptr = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                    ctypes.byref(iid), ctypes.byref(ptr))
        if hr != 0 or not ptr:
            return False
        table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, wt.HWND, wt.BOOL)
        fn = proto(table[8])
        fn(ptr, wt.HWND(hwnd), bool(on))
        return True
    except Exception:
        return False


class FullscreenController:
    """把窗口切成"真正铺满屏幕"的全屏，并保证任务栏压不上来。"""

    def __init__(self, window, log=None, marker_path=None):
        self._window = window
        self._log = log or (lambda _m: None)
        self._lock = threading.RLock()
        self._hwnd = 0
        self._fullscreen = False
        self._watch_stop = threading.Event()
        self._watch_thread = None
        self._saved = {}                     # 进入全屏前的样式与位置
        self._tray_hidden = []               # 被我们隐藏的任务栏句柄
        self._marker = marker_path          # 崩溃兜底：隐藏任务栏期间留下的标记文件
        self._tray_restore_armed = False     # 看门狗当前是否让任务栏处于"已还原"状态
        self._marked_fullscreen = False      # 是否已经告知过外壳"这是全屏窗口"

    # ── 基础 ────────────────────────────────────────────────────────────
    @property
    def is_fullscreen(self) -> bool:
        return self._fullscreen

    def is_minimized(self) -> bool:
        """窗口当前是否最小化。

        用途：退出时保存窗口几何。最小化状态下量到的坐标/尺寸全是垃圾（pywebview 报的是
        最小化占位矩形按 DPI 折算的结果，本机 150% 缩放 → -21332），存进 config.json 会让
        下次启动的窗口跑到屏幕外。
        """
        try:
            hwnd = self.hwnd()
            return bool(hwnd and user32.IsIconic(wt.HWND(hwnd)))
        except Exception:
            return False

    def hwnd(self) -> int:
        with self._lock:
            if self._hwnd and user32.IsWindow(wt.HWND(self._hwnd)):
                return self._hwnd
            self._hwnd = self._resolve_hwnd()
            return self._hwnd

    def _resolve_hwnd(self) -> int:
        native = getattr(self._window, "native", None)
        if native is not None:
            try:
                return int(native.Handle.ToInt64())
            except Exception:
                try:
                    return int(native.Handle)
                except Exception:
                    pass
        try:
            title = self._window.title
        except Exception:
            title = ""
        return int(user32.FindWindowW(None, title) or 0)

    def monitor_rect(self):
        """当前显示器矩形（物理像素）：(x, y, w, h)。"""
        hwnd = self.hwnd()
        if not hwnd:
            return None
        hmon = user32.MonitorFromWindow(wt.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        return (m.left, m.top, m.right - m.left, m.bottom - m.top)

    def is_topmost(self) -> bool:
        hwnd = self.hwnd()
        return bool(hwnd and (_get_style(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST))

    # ── 任务栏开关（全屏期间隐藏，退出/失焦还原）───────────────────────
    def tray_state(self) -> dict:
        st = taskbar_state()
        st["hidden_by_app"] = list(self._tray_hidden)
        return st

    def _take_over_taskbar(self) -> None:
        """全屏期间：隐藏任务栏，并告知外壳这是全屏窗口。

        幂等自愈：外壳会在某些时机（自动隐藏任务栏被触发、通知弹出等）把任务栏重新显示
        出来，所以看门狗每次都会调这里复查一遍，一旦又露出来就立刻压回去。
        """
        before = len(self._tray_hidden)
        for h in hide_taskbar():
            if h not in self._tray_hidden:
                self._tray_hidden.append(h)
        if not before and self._tray_hidden:
            self._log("[全屏] 已隐藏任务栏（共 %d 个），避免鼠标触底时把任务栏弹到全屏窗口上面"
                      % len(self._tray_hidden))
        if self._tray_hidden:
            self._write_marker()
        self._tray_restore_armed = False
        if not self._marked_fullscreen:
            self._marked_fullscreen = mark_fullscreen_window(self.hwnd(), True)

    def _release_taskbar(self) -> None:
        """还原任务栏（幂等）。"""
        hidden = self._tray_hidden
        self._tray_hidden = []
        show_taskbar(hidden if hidden else None)
        self._tray_restore_armed = True
        if self._marked_fullscreen:
            mark_fullscreen_window(self.hwnd(), False)
            self._marked_fullscreen = False
        if hidden:
            self._log("[全屏] 已还原任务栏（共 %d 个）" % len(hidden))
        self._clear_marker()

    # ── 崩溃兜底标记文件 ────────────────────────────────────────────────
    def _write_marker(self) -> None:
        if not self._marker:
            return
        try:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._marker.write_text(json.dumps({"pid": os.getpid(), "hwnd": self.hwnd(),
                                                "tray": self._tray_hidden}), encoding="utf-8")
        except Exception as exc:
            self._log("[全屏] 写任务栏兜底标记失败：%s" % exc)

    def _clear_marker(self) -> None:
        if not self._marker:
            return
        try:
            if self._marker.exists():
                self._marker.unlink()
        except Exception:
            pass

    def window_rect(self):
        """窗口当前的 Win32 矩形（物理像素）：(left, top, right, bottom)。"""
        hwnd = self.hwnd()
        if not hwnd:
            return None
        r = RECT()
        if not user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)

    def covers_monitor(self) -> bool:
        """窗口是否已经完整覆盖它所在显示器的矩形（自检/排障的判据）。"""
        mon, rect = self.monitor_rect(), self.window_rect()
        if not mon or not rect:
            return False
        return (rect[0] <= mon[0] and rect[1] <= mon[1]
                and rect[2] >= mon[0] + mon[2] and rect[3] >= mon[1] + mon[3])

    def _set_topmost(self, on: bool) -> bool:
        hwnd = self.hwnd()
        if not hwnd:
            return False
        want = bool(on)
        if want == self.is_topmost():
            return want
        _place(hwnd, HWND_TOPMOST if want else HWND_NOTOPMOST, 0, 0, 0, 0,
               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        return want

    # ── 进出全屏 ────────────────────────────────────────────────────────
    def toggle(self) -> bool:
        """切换全屏，返回**切换后的真实状态**（True=已全屏）。

        注意别写成 `return self.exit()`——exit() 的返回值是"操作成功"，退全屏时也是 True，
        界面/日志会把它误读成"已全屏"。
        """
        if self._fullscreen:
            self.exit()
        else:
            self.enter()
        return self._fullscreen

    def enter(self) -> bool:
        with self._lock:
            if self._fullscreen:
                return True
            hwnd = self.hwnd()
            if not hwnd:
                self._log("[全屏] 拿不到窗口句柄，放弃")
                return False
            rect = self.monitor_rect()
            if rect is None:
                self._log("[全屏] 取显示器矩形失败，放弃")
                return False

            place = WINDOWPLACEMENT()
            place.length = ctypes.sizeof(WINDOWPLACEMENT)
            user32.GetWindowPlacement(wt.HWND(hwnd), ctypes.byref(place))
            self._saved = {"style": _get_style(hwnd, GWL_STYLE), "placement": place,
                           "outer": self.window_rect(), "maximized": bool(user32.IsZoomed(wt.HWND(hwnd)))}

            # 最小化状态下没法直接量矩形，先还原；退出全屏时用上面保存的 placement 放回去
            if user32.IsIconic(wt.HWND(hwnd)):
                user32.ShowWindow(wt.HWND(hwnd), 9)      # SW_RESTORE
                self._saved["maximized"] = False

            # 1) 去掉标题栏与边框（交给 WinForms，保证它自己的状态一致，不会稍后把边框改回来）
            self._invoke_ui(lambda: self._borderless(True))
            # 2) 精确铺满当前显示器的物理像素矩形，同时置顶（任务栏压不上来）
            _place(hwnd, HWND_TOPMOST, rect[0], rect[1], rect[2], rect[3],
                   SWP_FRAMECHANGED | SWP_SHOWWINDOW)
            user32.SetForegroundWindow(wt.HWND(hwnd))
            # 3) 任务栏：光置顶不够（实测鼠标触底时外壳仍会把任务栏抬到我们上面），
            #    全屏期间直接把它隐藏；失焦或退出时立刻还原。
            self._take_over_taskbar()
            self._fullscreen = True
            self._log("[全屏] 已进入全屏：显示器 %dx%d @ (%d,%d)，窗口置顶 + 任务栏已让位"
                      % (rect[2], rect[3], rect[0], rect[1]))
        self._start_watch()
        return True

    def exit(self) -> bool:
        with self._lock:
            if not self._fullscreen:
                return False
            hwnd = self.hwnd()
            self._fullscreen = False
            self._watch_stop.set()
            self._release_taskbar()
            if not hwnd:
                return True
            _place(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._invoke_ui(lambda: self._borderless(False))
            style = self._saved.get("style")
            if style:
                _set_style(hwnd, GWL_STYLE, style)
            self._restore_geometry(hwnd)
            _place(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                   SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED | SWP_NOACTIVATE)
            self._log("[全屏] 已退出全屏，恢复原来的窗口样式与位置")
        return True

    def _restore_geometry(self, hwnd: int, tries: int = 6, gap: float = 0.15) -> bool:
        """把窗口摆回进入全屏前的矩形，并**核对结果**（不一致就再摆一次）。

        只调一次 SetWindowPlacement 是不够的：WinForms 对 FormBorderStyle 的变更处理是**延迟**的
        （下一次消息循环才做布局），它可能在我们摆好之后又把窗口挪走——实测出现过"退出全屏后
        窗口跑到屏幕外(y=1344)"。所以这里摆完就量一次，不对就再摆，收敛为止。
        """
        target = self._saved.get("outer")
        placement = self._saved.get("placement")
        if placement is not None:
            user32.SetWindowPlacement(wt.HWND(hwnd), ctypes.byref(placement))
        for i in range(tries):
            if target is None:
                return True
            cur = self.window_rect()
            if cur and all(abs(cur[k] - target[k]) <= 4 for k in range(4)):
                if i:
                    self._log("[全屏] 还原窗口位置：第 %d 次核对后一致 %s" % (i + 1, cur))
                return True
            if self._saved.get("maximized"):
                user32.ShowWindow(wt.HWND(hwnd), 3)      # SW_MAXIMIZE
            else:
                _place(hwnd, HWND_NOTOPMOST, target[0], target[1],
                       target[2] - target[0], target[3] - target[1], SWP_NOACTIVATE)
            time.sleep(gap)
        self._log("[全屏] 警告：窗口没能完全摆回 %s（当前 %s）" % (target, self.window_rect()))
        return False

    def stop(self) -> None:
        self._watch_stop.set()
        if self._fullscreen:
            self.exit()
        else:
            mark_fullscreen_window(self.hwnd(), False)
            self._release_taskbar()      # 幂等：确保任务栏一定被还原

    # ── WinForms 侧（边框开关）──────────────────────────────────────────
    def _invoke_ui(self, fn) -> bool:
        native = getattr(self._window, "native", None)
        if native is None:
            return False
        try:
            if native.InvokeRequired:
                delegate = _to_delegate(fn)
                if delegate is None:
                    return False
                native.Invoke(delegate)
            else:
                fn()
            return True
        except Exception as exc:
            self._log("[全屏] 调整窗口边框失败：%s" % exc)
            return False

    def _borderless(self, on: bool) -> None:
        native = getattr(self._window, "native", None)
        if native is None:
            return
        from System.Windows.Forms import FormBorderStyle, FormWindowState
        if on:
            if native.WindowState == FormWindowState.Maximized:
                native.WindowState = FormWindowState.Normal
            # 注意：None 是 Python 关键字，只能 getattr 取（FormBorderStyle.None 是语法错误）
            native.FormBorderStyle = getattr(FormBorderStyle, "None")
        else:
            native.WindowState = FormWindowState.Normal
            native.FormBorderStyle = FormBorderStyle.Sizable

    # ── 置顶看门狗：前台才置顶，切走就降级 ──────────────────────────────
    def _start_watch(self, interval: float = 0.4) -> None:
        self._watch_stop.clear()
        if self._watch_thread and self._watch_thread.is_alive():
            return

        def _loop():
            while not self._watch_stop.is_set():
                try:
                    if not self._fullscreen:
                        break
                    focused = classify_fullscreen_focus(self.hwnd()) in ("self", "owned")
                    self._set_topmost(focused)
                    # 失焦就把任务栏还回去（否则用户切到别的程序时任务栏不见了）；
                    # 回到前台再让位，继续保证"完全覆盖"。接管是幂等的，顺手自愈。
                    if focused:
                        self._take_over_taskbar()
                    elif not self._tray_restore_armed:
                        self._release_taskbar()
                except Exception:
                    pass
                time.sleep(interval)

        self._watch_thread = threading.Thread(target=_loop, name="fullscreen-watch", daemon=True)
        self._watch_thread.start()


def _to_delegate(fn):
    """把 Python 可调用对象包成 .NET 委托（pythonnet 的两种写法都试一下）。"""
    try:
        from System import Action
        return Action(fn)
    except Exception:
        try:
            from System import Func, Type
            return Func[Type](fn)
        except Exception:
            return None


ESC_EXIT_FULLSCREEN_JS = (
    "(function(){try{"
    "if(window.__ailiveFsKey) return 1; window.__ailiveFsKey = 1;"
    "document.addEventListener('keydown', function(e){"
    "  if(e.key !== 'Escape') return;"
    "  var api = window.pywebview && window.pywebview.api;"
    "  if(!api || !api.is_fullscreen) return;"
    "  Promise.resolve(api.is_fullscreen()).then(function(on){"
    "    if(on && api.toggle_fullscreen) api.toggle_fullscreen();"
    "  }).catch(function(){});"
    "}, true); return 1;}catch(e){return 0;}})()"
)