"""清理自检产生的测试账号与激活码（服务需在运行中）。

浏览器/接口自检每跑一次就会建一个 e2e*/diag* 账号和一个自检激活码，
反复调试后会在用户管理后台里堆积。这个脚本通过管理员接口把它们删干净。

    E:\\AiLive\\env\\python.exe tools\\smoke\\_cleanup_test_data.py            # 只列出会删什么
    E:\\AiLive\\env\\python.exe tools\\smoke\\_cleanup_test_data.py --apply    # 真正删除
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8010"
# 管理员口令从环境变量取，不要写死在脚本里（改过密码后照样能用）：
#   $env:AILIVE_ADMIN_PASSWORD = "你的密码"
ADMIN_USER = os.environ.get("AILIVE_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("AILIVE_ADMIN_PASSWORD", "admin123456")
USER_PATTERN = re.compile(r"^(e2e|diag)", re.I)
CODE_NOTE_HINTS = ("E2E", "自检", "diag")

# 必须带 Cookie 会话：管理员登录后拿到的 ailive_session 要跟着后续请求走
COOKIES = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIES))


def call(path: str, payload: dict | None = None, method: str | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with OPENER.open(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(body)
        except json.JSONDecodeError:
            return error.code, {"raw": body[:200]}


def main() -> int:
    apply_changes = "--apply" in sys.argv

    status, data = call("/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PASSWORD})
    if status != 200:
        print(f"管理员登录失败（{status}）：{data}")
        print("提示：改过管理员密码的话，先设置环境变量再跑，例如")
        print('      $env:AILIVE_ADMIN_PASSWORD = "你的密码"')
        return 1
    print("管理员登录成功")

    status, data = call("/api/auth/admin/users")
    users = [item for item in (data.get("data", {}).get("users") or [])
             if USER_PATTERN.match(item.get("username", ""))]
    print(f"测试账号 {len(users)} 个：" + ("、".join(item["username"] for item in users) or "无"))

    status, data = call("/api/auth/admin/codes")
    codes = [item for item in (data.get("data", {}).get("codes") or [])
             if any(hint.lower() in (item.get("note", "") or "").lower() for hint in CODE_NOTE_HINTS)
             or item.get("created_by") == "admin"]
    codes = [item for item in codes if not item.get("revoked")]
    print(f"自检激活码 {len(codes)} 个：" + ("、".join(item["code"] for item in codes) or "无"))

    if not apply_changes:
        print("\n这是预演模式，没有改动任何数据。加 --apply 才会真正删除/作废。")
        return 0

    ok = True
    for item in users:
        status, result = call(f"/api/auth/admin/users/{item['username']}", method="DELETE")
        print(f"删除账号 {item['username']} -> {status} {result.get('msg', '')}")
        ok = ok and status == 200
    for item in codes:
        status, result = call("/api/auth/admin/codes/revoke", {"code": item["code"]})
        print(f"作废激活码 {item['code']} -> {status} {result.get('msg', '')}")
        ok = ok and status == 200

    print("\n清理完成" if ok else "\n清理过程中有失败项，请检查上面的输出")
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())