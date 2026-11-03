"""脚本编码守卫：防止「中文脚本丢了 BOM」导致启动器直接解析失败。

背景（真实事故）：`tools/start_ai_live.ps1` 里的中文是 UTF-8，但文件没有 BOM。
Windows PowerShell 5.1 在没有 BOM 时按系统 ANSI（中文机器是 GBK）解析脚本，
中文串的字节会把收尾引号吃掉，于是报：
    字符串缺少终止符: '。
    AI digital human startup failed.
表现就是「双击一键启动没反应/启动失败」，而 .cmd 本身其实是好的。

规则：
    *.ps1            —— 含非 ASCII 就必须带 UTF-8 BOM
    *.cmd / *.bat    —— 含非 ASCII 就必须有 chcp 65001（UTF-8），否则得存成 GBK
                        有 chcp 65001 就必须是 CRLF 行尾：bare LF 会让 cmd 换代码页后
                        按字节偏移读错行，把后面的命令切碎执行（2026-10-11 实测复现）

    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_script_encoding.py          # 查启动器
    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_script_encoding.py --all    # 全盘扫描（只报警告）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(r"E:\AiLive")
BOM = b"\xef\xbb\xbf"

# 用户实际双击/依赖的启动器：这些必须零问题
LAUNCHERS = [
    ROOT / "一键启动AI数字人.cmd",
    ROOT / "停止AI直播服务.cmd",
    ROOT / "重置管理员密码.cmd",
    ROOT / "外网分享给别人看.cmd",
    ROOT / "停止外网分享.cmd",
    ROOT / "桌面版AI数字人.cmd",
    ROOT / "desktop" / "同步桌面版副本.cmd",
    ROOT / "tools" / "start_ai_live.ps1",
    ROOT / "tools" / "open_desktop.ps1",
    ROOT / "tools" / "share_public.ps1",
    ROOT / "tools" / "stop_ai_live.ps1",
]

SKIP_PARTS = {"env", "node_modules", ".git", "__pycache__", "LiveTalking", "_qa", "tmp"}


def has_non_ascii(data: bytes) -> bool:
    return any(byte > 127 for byte in data)


def decodes_as(data: bytes, encoding: str) -> bool:
    try:
        data.decode(encoding)
        return True
    except UnicodeDecodeError:
        return False


def inspect(path: Path) -> tuple[list[str], list[str]]:
    """返回（错误, 提示）。"""
    data = path.read_bytes()
    errors: list[str] = []
    notes: list[str] = []
    suffix = path.suffix.lower()
    if not has_non_ascii(data):
        return errors, notes  # 纯 ASCII 脚本怎么解析都不会错

    if suffix == ".ps1":
        if not data.startswith(BOM):
            errors.append(
                "含中文但没有 UTF-8 BOM —— PowerShell 5.1 会按 GBK 解析，"
                "中文串可能吃掉引号导致「字符串缺少终止符」"
            )
        elif not decodes_as(data[3:], "utf-8"):
            errors.append("带 BOM 但内容不是合法 UTF-8")
    elif suffix in (".cmd", ".bat"):
        _add_cmd_rules(data, errors, notes)
    else:
        notes.append(f"未检查的扩展名 {suffix}")
    return errors, notes


def _add_cmd_rules(data: bytes, errors: list[str], notes: list[str]) -> None:
    """cmd 的两条真实踩坑规则（都实测复现过）。"""
    # 规则 A：含中文、是 UTF-8、又没有 chcp 65001 → cmd 显示乱码
    if b"chcp 65001" not in data:
        if decodes_as(data, "utf-8") and has_non_ascii(data):
            errors.append("含中文、是 UTF-8、又没有 chcp 65001 —— cmd 会显示乱码；"
                          "补 `chcp 65001 >nul` 或另存为 GBK")
        elif has_non_ascii(data):
            notes.append("含中文且不是 UTF-8（可能是 GBK 本地编码，cmd 下正常）")
        return

    # 规则 B：有 chcp 65001 就必须 CRLF 行尾。
    # 2026-10-11 实测复现：UTF-8 的 .cmd 含中文 + chcp 65001 + bare LF 行尾时，
    # cmd.exe 换代码页后按【字节偏移】重新定位批处理文件会错位，把后面的行切碎执行，
    # 表现为 「'nul' is not recognized」『'CHER' 不是内部或外部命令』这类碎片报错，
    # 双击等于不执行。同一个文件只把行尾改成 CRLF 就正常（对照实验 vA 失败 / vB 正常）。
    if re.search(rb"(?<!\r)\n", data):
        errors.append("有 chcp 65001 但行尾是 bare LF —— cmd 换代码页后会按字节偏移读错行、"
                      "把后面的命令切碎执行（双击等于没反应）。请把行尾统一改成 CRLF")


def collect_all() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if path.suffix.lower() not in (".ps1", ".cmd", ".bat"):
            continue
        parts = set(path.parts)
        if parts & SKIP_PARTS:
            continue
        found.append(path)
    return sorted(found)


def main() -> int:
    scan_all = "--all" in sys.argv
    print("启动器编码检查：")
    failed = 0
    for path in LAUNCHERS:
        if not path.exists():
            print(f"[SKIP] {path.name} 不存在")
            continue
        errors, notes = inspect(path)
        if errors:
            failed += 1
            print(f"[FAIL] {path}")
            for item in errors:
                print(f"       - {item}")
        else:
            extra = f"（{notes[0]}）" if notes else ""
            print(f"[ OK ] {path.name}{extra}")

    if scan_all:
        others = [path for path in collect_all() if path not in LAUNCHERS]
        risky = []
        for path in others:
            errors, _ = inspect(path)
            if errors:
                risky.append((path, errors))
        print(f"\n全盘扫描：{len(others)} 个脚本，{len(risky)} 个有同样的编码风险（不影响启动器）")
        for path, errors in risky:
            print(f"[WARN] {path}")
            for item in errors:
                print(f"       - {item}")

    print("-" * 56)
    if failed:
        print(f"{failed} 个启动器脚本编码有问题")
        return 1
    print("启动器脚本编码全部正常")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())