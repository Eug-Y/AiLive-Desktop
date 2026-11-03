# -*- coding: utf-8 -*-
"""清掉端到端自检留下的垃圾账号 / 激活码 / 过期会话。

背景：`_e2e_app_ui.py`、`_e2e_desktop_app.py` 这类验收脚本每跑一次就会注册一个自检账号
（`e2e_app_qa`、`e2e_desktop_<数字>`、更早的 `e2e<数字>`），并可能生成测试激活码。
跑得多了，登录页的用户名、用户管理页的列表、激活码列表里就全是测试残留。

识别规则（宁可漏删，绝不误删真账号）：
  * 账号：用户名匹配 `^e2e[_0-9]`（`e2e_` 或 `e2e` 后紧跟数字才算，`eug`/`yyy`/`admin` 这类不会命中）
  * 会话：该会话的 username 命中上面的规则
  * 激活码：备注里含 "E2E"，或使用记录全部是自检账号

安全措施：
  * **默认只预览，不改文件**；确认无误再加 `--apply`
  * 服务在运行时会拒绝执行（认证数据在内存里，服务一保存就会把改动覆盖回去），
    除非显式 `--force`；正确做法是先停服务、清完再启动
  * 落地前把 users.json / codes.json / sessions.json 三个文件整体备份到 logs\\archive\\auth-backup-<时间戳>\\
  * 写回格式与服务端 auth_manager._write_json 完全一致（utf-8 / ensure_ascii=False / indent=2 / 原子替换）

用法：
  E:\\AiLive\\env\\python.exe tools\\cleanup_e2e_accounts.py            # 预览
  E:\\AiLive\\env\\python.exe tools\\cleanup_e2e_accounts.py --apply    # 真删（服务需先停）
"""
import argparse
import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT = Path(r"E:\AiLive")
AUTH_DIR = PROJECT / "LiveTalking" / "data" / "auth"
BACKUP_ROOT = PROJECT / "logs" / "archive"
HEALTH = "http://127.0.0.1:8010/health"

TEST_USER = re.compile(r"^e2e[_0-9]", re.I)


def service_running(timeout=2.0):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_json(path):
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh) or {}


def write_json(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser(description="清理端到端自检残留账号/激活码/会话")
    ap.add_argument("--apply", action="store_true", help="真正写回文件（默认只预览）")
    ap.add_argument("--force", action="store_true", help="服务在运行时也强行执行（不推荐）")
    ap.add_argument("--auth-dir", default=str(AUTH_DIR))
    args = ap.parse_args()

    auth = Path(args.auth_dir)
    users_p, codes_p, sess_p = auth / "users.json", auth / "codes.json", auth / "sessions.json"
    if not users_p.is_file():
        print("找不到认证数据：%s" % users_p)
        return 1

    users, codes, sessions = read_json(users_p), read_json(codes_p), read_json(sess_p)

    del_users = [u for u in users if TEST_USER.match(str(u))]
    del_sessions = [t for t, v in sessions.items() if TEST_USER.match(str((v or {}).get("username", "")))]
    del_codes = []
    for code, v in codes.items():
        v = v or {}
        note = str(v.get("note", ""))
        used_by = [str(x) for x in (v.get("used_by") or [])]
        if "e2e" in note.lower() or (used_by and all(TEST_USER.match(x) for x in used_by)):
            del_codes.append(code)

    print("认证数据目录：%s" % auth)
    print("现有：账号 %d 个 / 激活码 %d 个 / 会话 %d 条" % (len(users), len(codes), len(sessions)))
    print("")
    print("待清理自检账号（%d）：" % len(del_users))
    for u in del_users:
        rec = users.get(u) or {}
        print("  - %-28s role=%-5s plan=%-6s note=%s" % (u, rec.get("role", "?"), rec.get("plan", "?"),
                                                         rec.get("note", "") or "-"))
    print("待清理激活码（%d）：" % len(del_codes))
    for c in del_codes[:20]:
        rec = codes.get(c) or {}
        print("  - %-24s revoked=%-5s note=%s used_by=%s" % (c, rec.get("revoked"),
                                                             rec.get("note", "") or "-",
                                                             ",".join(rec.get("used_by") or []) or "-"))
    if len(del_codes) > 20:
        print("  ... 其余 %d 个同类" % (len(del_codes) - 20))
    print("待清理会话（%d）：对应上面这些账号的登录态" % len(del_sessions))
    keep = [u for u in users if u not in del_users]
    print("")
    print("清理后保留的账号（%d）：%s" % (len(keep), ", ".join(sorted(keep)) or "（空）"))

    if not del_users and not del_codes and not del_sessions:
        print("\n没有需要清理的内容。")
        return 0

    if not args.apply:
        print("\n（预览模式，未改动任何文件；确认无误后加 --apply 执行）")
        return 0

    if service_running() and not args.force:
        print("\n[阻止] 8010 服务正在运行：认证数据在服务内存里，服务下次保存会把改动覆盖回去。")
        print("       请先双击“停止AI直播服务.cmd”，清完再启动；确实要冒险执行就加 --force。")
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_ROOT / ("auth-backup-" + stamp)
    backup.mkdir(parents=True, exist_ok=True)
    for p in (users_p, codes_p, sess_p):
        if p.is_file():
            shutil.copy2(p, backup / p.name)
    print("\n已备份原文件到：%s" % backup)

    for u in del_users:
        users.pop(u, None)
    for t in del_sessions:
        sessions.pop(t, None)
    for c in del_codes:
        codes.pop(c, None)

    write_json(users_p, users)
    if codes_p.is_file():
        write_json(codes_p, codes)
    if sess_p.is_file():
        write_json(sess_p, sessions)

    # 回读验证
    users2, codes2, sessions2 = read_json(users_p), read_json(codes_p), read_json(sess_p)
    left = [u for u in users2 if TEST_USER.match(str(u))]
    ok = not left and len(users2) == len(users) and len(codes2) == len(codes) and len(sessions2) == len(sessions)
    print("清理后：账号 %d 个 / 激活码 %d 个 / 会话 %d 条" % (len(users2), len(codes2), len(sessions2)))
    print("保留账号：%s" % (", ".join(sorted(users2)) or "（空）"))
    print("\n%s" % ("完成，回读校验通过，无自检账号残留。" if ok else "注意：回读校验异常，请检查上面的备份。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())