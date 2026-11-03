"""交互式重置 AiLive 账号密码（配合「重置管理员密码.cmd」使用）。

用法：
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\reset_password.py [用户名]
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "LiveTalking"))

from server.auth_manager import AuthError, get_auth_manager  # noqa: E402


def main() -> int:
    manager = get_auth_manager()
    username = (sys.argv[1].strip() if len(sys.argv) > 1 else "")
    if not username:
        username = input("要重置的用户名（直接回车 = admin）：").strip() or "admin"

    user = manager.get_user(username)
    if not user:
        print(f"用户 {username} 不存在。现有账号：")
        for row in manager.list_users():
            print(f"  - {row['username']:<16} {row['role']:<6} {row['plan']}")
        return 1

    print(f"即将重置「{user.get('username')}」的密码（{user.get('role')} / {user.get('plan')}）")
    password = getpass.getpass("请输入新密码（至少 8 位，需同时包含字母和数字）：")
    confirm = getpass.getpass("请再次输入新密码：")
    if password != confirm:
        print("两次输入不一致，已取消。")
        return 1
    try:
        manager.reset_password(user["username"], password)
    except AuthError as exc:
        print(f"失败：{exc.message}")
        return 1
    print(f"已重置 {user['username']} 的密码；该账号在其它设备上的登录已失效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())