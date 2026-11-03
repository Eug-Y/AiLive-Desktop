"""工作台界面自检：防录播开关与两个新开播检查项是否真的渲染并可读。

需要服务已在 8010 运行、data/auth/sessions.json 里有未过期的管理员会话。
用真实浏览器（headless Chromium）打开工作台，检查：

    * 「打乱话术顺序（防录播）」开关存在，且与后端状态一致
    * 「值守痕迹可观测」「百货各子类目资质」两个人工确认项存在
    * 话术间隔回填为后端值（默认 30 秒）
    * 页面无 JS 报错
    * 截图落到 tmp/ 便于人工复核

用法：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_anti_replay_ui.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8010"
SESSIONS = ROOT / "LiveTalking" / "data" / "auth" / "sessions.json"
SHOT = ROOT / "tmp" / "live_console_anti_replay.png"
# 断网/内网环境里 CDN 静态资源会 404，不算页面脚本错误
NOISE = ("failed to load resource", "net::err", "favicon", "cdn.jsdelivr.net",
         "bootstrap-icons", "status of 404")


def find_chromium() -> str | None:
    """本机 playwright 版本与已下载浏览器版本不一致时，直接指定可执行文件。"""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(root.glob("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"))
    candidates += sorted(root.glob("chromium-*/chrome-win/chrome.exe"))
    return str(candidates[-1]) if candidates else None

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PASSED = 0
FAILED = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def admin_token() -> str:
    data = json.loads(SESSIONS.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    best = None
    for token, meta in data.items():
        if not isinstance(meta, dict):
            continue
        if str(meta.get("username", "")).lower() not in {"eug", "admin"}:
            continue
        try:
            expires = datetime.fromisoformat(str(meta.get("expires_at", "")))
        except ValueError:
            continue
        if expires > now and (best is None or expires > best[0]):
            best = (expires, token)
    if best is None:
        raise SystemExit("没有可用的未过期管理员会话，请先在浏览器登录一次工作台。")
    return best[1]


def backend_settings() -> dict:
    request = urllib.request.Request(
        BASE + "/api/live/status",
        headers={"Cookie": f"ailive_session={admin_token()}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))["data"]["settings"]


def main() -> int:
    from playwright.sync_api import sync_playwright

    settings = backend_settings()
    print("=== 工作台界面自检（防录播 + 新检查项） ===")
    print(f"后端：间隔 {settings['script_gap_seconds']}s / 循环 {settings['repeat_scripts']}"
          f" / 打乱 {settings['shuffle_scripts']}")

    SHOT.parent.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []

    with sync_playwright() as pw:
        executable = find_chromium()
        if executable:
            browser = pw.chromium.launch(headless=True, executable_path=executable)
        else:
            browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_cookies([{
            "name": "ailive_session",
            "value": admin_token(),
            "domain": "127.0.0.1",
            "path": "/",
        }])
        page = context.new_page()
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(BASE + "/live_console.html", wait_until="networkidle")
        page.wait_for_timeout(1500)
        real_errors = [
            e for e in errors
            if not any(noise in e.lower() for noise in NOISE)
        ]

        check("未被弹回登录页", "login" not in page.url.lower(), page.url)

        # 人工确认项在默认打开的「开播检查」页
        for key, keyword in (("supervision_visible", "值守痕迹"),
                             ("category_qualified", "资质")):
            box = page.locator(f'[data-workflow="{key}"]')
            check(f"存在人工确认项 {key}", box.count() == 1)
            check(f"{key} 在默认页可见", box.is_visible() if box.count() else False)
            text = box.locator("xpath=..").inner_text() if box.count() else ""
            check(f"{key} 文案含「{keyword}」", keyword in text, text)
        page.screenshot(path=str(SHOT.with_name("live_console_check.png")), full_page=False)

        # 话术设置在「操作步骤 1-6」页，默认隐藏，先切过去
        page.click("#lcTabSteps")
        page.wait_for_timeout(600)

        shuffle = page.locator("#shuffleScripts")
        switch_label = page.locator("label.form-switch", has=shuffle)
        check("存在「打乱话术顺序」开关", shuffle.count() == 1)
        check("切到话术页后开关可见", switch_label.is_visible() if shuffle.count() else False)
        check("开关状态与后端一致",
              shuffle.is_checked() == bool(settings["shuffle_scripts"]),
              f"界面 {shuffle.is_checked()} / 后端 {settings['shuffle_scripts']}")
        label = switch_label.inner_text() if shuffle.count() else ""
        check("开关文案含「防录播」", "防录播" in label, label)
        repeat_visible = page.locator("label.form-switch", has=page.locator("#repeatScripts")).is_visible()
        check("原有「循环播放」开关同样可见（对照）", repeat_visible)

        gap = page.locator("#scriptGap")
        check("间隔输入框回填后端值",
              float(gap.input_value()) == float(settings["script_gap_seconds"]),
              f"界面 {gap.input_value()} / 后端 {settings['script_gap_seconds']}")
        hint = page.locator("#scriptGap ~ p.hint").inner_text()
        check("间隔旁提示了 15 秒下限", "15 秒" in hint, hint)

        check("页面无 JS 报错", not real_errors, "; ".join(real_errors[:3]))

        page.screenshot(path=str(SHOT), full_page=False)
        browser.close()

    print(f"\n截图：{SHOT.with_name('live_console_check.png')}、{SHOT}")
    print(f"=== 通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
    for name in FAILED:
        print(f"  失败：{name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())