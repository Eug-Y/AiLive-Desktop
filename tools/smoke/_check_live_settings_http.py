"""防录播护栏 + 新增开播检查项的 HTTP 端到端自检。

需要服务已在 8010 运行，并用本机已有的管理员会话令牌调用真实接口
（不新建账号、不改密码）。脚本会把「话术 / 人工检查项 / 直播设置 / 账号间隔」
先备份、再还原，默认是只读式验证。

    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_live_settings_http.py
    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_live_settings_http.py --apply

--apply 额外把运营值设为推荐值：循环间隔 30 秒、打乱顺序开启、账号级间隔 30 秒。

用法前提：服务已启动；data/auth/sessions.json 里存在未过期的管理员会话。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8010"
SESSIONS = ROOT / "LiveTalking" / "data" / "auth" / "sessions.json"
ACCOUNT_ID = "default"
COOKIE = "ailive_session"
LOOP_GAP_FLOOR = 15.0
RECOMMENDED_GAP = 30.0

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
        if expires <= now:
            continue
        if best is None or expires > best[0]:
            best = (expires, token)
    if best is None:
        raise SystemExit("没有可用的未过期管理员会话，请先在浏览器登录一次工作台。")
    return best[1]


TOKEN = ""


def api(method: str, path: str, body=None):
    request = urllib.request.Request(BASE + path, method=method)
    request.add_header("Cookie", f"{COOKIE}={TOKEN}")
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=data, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} 失败：HTTP {exc.code} {exc.read()[:300]!r}")
    if payload.get("success") is False:
        raise SystemExit(f"{method} {path} 返回失败：{payload.get('error')}")
    return payload.get("data", payload)


def status():
    return api("GET", "/api/live/status")


def anti_replay(data):
    for item in data["workflow"]["checks"]:
        if item["key"] == "anti_replay":
            return item
    raise SystemExit("检查项里没有 anti_replay")


def main() -> int:
    global TOKEN
    TOKEN = admin_token()
    apply_mode = "--apply" in sys.argv

    base = status()
    settings_before = dict(base["settings"])
    scripts_before = list(base["scripts"])
    manual_before = dict(base["workflow"]["manual_checks"])
    account_before = api("GET", f"/api/accounts/{ACCOUNT_ID}")
    gap_before = account_before.get("script_gap_seconds")
    print("=== 防录播与开播检查项 HTTP 自检 ===")
    print(f"现场：间隔 {settings_before['script_gap_seconds']}s / 循环 {settings_before['repeat_scripts']}"
          f" / 打乱 {settings_before.get('shuffle_scripts')} / 话术 {len(scripts_before)} 条")
    print("（结束后会还原以上现场）")

    try:
        print("\n1. 设置项已支持打乱顺序")
        check("settings 含 shuffle_scripts", isinstance(settings_before.get("shuffle_scripts"), bool))

        print("\n2. 循环播放的间隔下限护栏")
        got = api("POST", "/api/live/settings",
                  {"script_gap_seconds": 8, "repeat_scripts": True,
                   "shuffle_scripts": True})["settings"]["script_gap_seconds"]
        check(f"提交 8 秒 + 循环 → 上浮到 {LOOP_GAP_FLOOR:.0f} 秒", got == LOOP_GAP_FLOOR, f"实际 {got}")
        got = api("POST", "/api/live/settings",
                  {"script_gap_seconds": 8, "repeat_scripts": False})["settings"]["script_gap_seconds"]
        check("提交 8 秒 + 关闭循环 → 不干预", got == 8.0, f"实际 {got}")
        got = api("POST", "/api/live/settings",
                  {"script_gap_seconds": 999, "repeat_scripts": True})["settings"]["script_gap_seconds"]
        check("上限仍为 300 秒", got == 300.0, f"实际 {got}")

        print("\n3. 两个新人工检查项可读可写")
        updated = api("POST", "/api/live/workflow",
                      {"supervision_visible": True, "category_qualified": True})
        manual = updated["workflow"]["manual_checks"]
        check("supervision_visible 写入生效", manual["supervision_visible"] is True)
        check("category_qualified 写入生效", manual["category_qualified"] is True)
        keys = {item["key"] for item in updated["workflow"]["checks"]}
        check("检查中心列出这两项", {"supervision_visible", "category_qualified"} <= keys)

        print("\n4. 打乱顺序作用于真实保存的话术")
        probe = ["自检话术甲", "自检话术乙", "自检话术丙", "自检话术丁"]
        saved = api("PUT", "/api/live/scripts",
                    {"scripts": probe, "repeat": True})
        check("话术按作者顺序保存（便于编辑）", saved["scripts"] == probe, f"{saved['scripts']}")
        order = list(saved["play_order"])
        check("play_order 是 0..3 的合法全排列", sorted(order) == [0, 1, 2, 3], f"{order}")
        orders = {tuple(order)}
        for _ in range(6):
            orders.add(tuple(api("PUT", "/api/live/scripts",
                                 {"scripts": probe, "repeat": True})["play_order"]))
        check("多次保存得到多种顺序", len(orders) > 1, f"仅 {len(orders)} 种")
        fixed = api("POST", "/api/live/settings", {"shuffle_scripts": False})["settings"]
        fixed_order = api("PUT", "/api/live/scripts", {"scripts": probe, "repeat": True})["play_order"]
        check("关闭打乱 → 恢复 0,1,2,3 顺序", fixed_order == [0, 1, 2, 3], f"{fixed_order}")
        check("设置里 shuffle_scripts 已关闭", fixed["shuffle_scripts"] is False)

        print("\n5. 防录播检查随设置变化")
        anti = anti_replay(status())
        check("固定顺序 → 判不通过", anti["passed"] is False, anti["label"])
        api("POST", "/api/live/settings",
            {"shuffle_scripts": True, "script_gap_seconds": 30, "repeat_scripts": True})
        anti = anti_replay(status())
        check("打乱 + 30 秒 → 判通过", anti["passed"] is True, anti["label"])
        api("POST", "/api/live/settings",
            {"shuffle_scripts": True, "script_gap_seconds": 30, "repeat_scripts": False})
        anti = anti_replay(status())
        check("关闭循环 → 判通过", anti["passed"] is True, anti["label"])

        if apply_mode:
            print("\n6. --apply：写入推荐运营值")
            applied = api("POST", "/api/live/settings",
                          {"script_gap_seconds": RECOMMENDED_GAP, "repeat_scripts": True,
                           "shuffle_scripts": True})["settings"]
            check(f"直播间隔 = {RECOMMENDED_GAP:.0f} 秒", applied["script_gap_seconds"] == RECOMMENDED_GAP)
            check("打乱顺序 = 开启", applied["shuffle_scripts"] is True)
            account = api("PUT", f"/api/accounts/{ACCOUNT_ID}",
                          {"script_gap_seconds": RECOMMENDED_GAP})
            check(f"账号级间隔 = {RECOMMENDED_GAP:.0f} 秒",
                  account["script_gap_seconds"] == RECOMMENDED_GAP,
                  f"实际 {account['script_gap_seconds']}")
    finally:
        # 自检话术与人工检查项一律还原（--apply 也一样）
        api("PUT", "/api/live/scripts", {"scripts": scripts_before,
                                        "repeat": settings_before["repeat_scripts"]})
        api("POST", "/api/live/workflow", manual_before)
        if apply_mode:
            print("\n已清理自检话术与人工检查项（保留推荐运营值）。")
        else:
            api("POST", "/api/live/settings", settings_before)
            if gap_before is not None:
                api("PUT", f"/api/accounts/{ACCOUNT_ID}", {"script_gap_seconds": gap_before})
            print("\n已还原现场。")

    final = status()
    print(f"\n最终：间隔 {final['settings']['script_gap_seconds']}s"
          f" / 循环 {final['settings']['repeat_scripts']}"
          f" / 打乱 {final['settings']['shuffle_scripts']}"
          f" / 话术 {len(final['scripts'])} 条")
    print(f"=== 通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
    for name in FAILED:
        print(f"  失败：{name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())