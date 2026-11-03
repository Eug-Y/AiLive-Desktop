"""网页静态自检：HTML 标签配平 + 内联脚本语法（调用 node --check）。

不需要启动服务，也不会请求网络。用于改动 web/ 页面后的快速回归：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_web_pages.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "LiveTalking" / "web"

PAGES = [
    "app.html",
    "login.html",
    "upgrade.html",
    "admin_users.html",
    "forbidden.html",
    "live_console.html",
]

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}


class Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list = []
        self.errors: list = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        return

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"多余的 </{tag}>（第 {self.getpos()[0]} 行）")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        names = [item[0] for item in self.stack]
        if tag in names:
            index = len(names) - 1 - names[::-1].index(tag)
            opened_line = self.stack[index][1]
            self.errors.append(
                f"</{tag}>（第 {self.getpos()[0]} 行）与 <{self.stack[-1][0]}>"
                f"（第 {self.stack[-1][1]} 行）交叉闭合；<{tag}> 开始于第 {opened_line} 行"
            )
            del self.stack[index:]
        else:
            self.errors.append(f"没有对应的 <{tag}> 开始标签（第 {self.getpos()[0]} 行）")


SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def check_page(path: Path) -> list:
    problems = []
    text = path.read_text(encoding="utf-8")

    parser = Balance()
    parser.feed(text)
    parser.close()
    problems.extend(parser.errors)
    for tag, line in parser.stack:
        problems.append(f"<{tag}>（第 {line} 行）没有闭合")

    for index, block in enumerate(SCRIPT_RE.findall(text), start=1):
        body = block.strip()
        if not body or len(body) < 20:
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(body)
            temp = Path(handle.name)
        try:
            result = subprocess.run(
                ["node", "--check", str(temp)],
                capture_output=True, text=True, timeout=60, shell=False,
            )
            if result.returncode != 0:
                first = (result.stderr or "").strip().splitlines()
                detail = first[0] if first else "未知语法错误"
                problems.append(f"内联脚本 #{index} 语法错误：{detail}")
        except FileNotFoundError:
            problems.append("未找到 node，无法检查内联脚本语法")
            break
        except subprocess.TimeoutExpired:
            problems.append(f"内联脚本 #{index} 检查超时")
        finally:
            temp.unlink(missing_ok=True)

    ids = re.findall(r"\bid=\"([^\"{}]+)\"", text)
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        problems.append("重复的 id：" + "、".join(duplicates))

    # 依赖脚本自检：页面用到某个全局库，就必须真的把它引进来。
    # （upgrade.html 曾经用 Auth.* 却没引 auth.js，浏览器里一路抛
    #  "Auth is not defined"，页面卡在“正在读取账号状态…”，静态配平检查完全看不出来。）
    sources = set(re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", text, re.I))
    basenames = {value.rsplit("/", 1)[-1].split("?")[0] for value in sources}
    for global_name, script_name in (("Auth", "auth.js"), ("LT", "common.js")):
        if re.search(rf"\b{global_name}\.", text) and script_name not in basenames:
            problems.append(
                f"页面使用了 {global_name}.* 但没有引入 {script_name}"
                f"（浏览器会抛 “{global_name} is not defined”）"
            )
    problems.extend(check_script_order(text))

    return problems


def check_script_order(text: str) -> list:
    """内联脚本先用了 LT./Auth.，而对应的库脚本在它后面才引入。

    这种情况下整块内联脚本第一行就抛 “X is not defined”，后面所有函数都不会定义，
    页面看起来正常、实际整页失效。`tts/index.html` 曾经就是这样（common.js 被放在
    文件末尾，导航和所有按钮全都没了反应），静态配平与依赖检查都看不出来。
    """
    problems = []
    lib_positions: dict = {}
    for match in re.finditer(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"'][^>]*>", text, re.I):
        name = match.group(1).rsplit("/", 1)[-1].split("?")[0]
        if name in ("common.js", "auth.js"):
            lib_positions.setdefault(name, match.start())
    for match in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, re.S | re.I):
        usage = re.search(r"\b(Auth|LT)\.", match.group(1))
        if not usage:
            continue
        script_name = "auth.js" if usage.group(1) == "Auth" else "common.js"
        position = lib_positions.get(script_name)
        if position is None:
            break                       # 完全没引入，交给依赖检查报
        absolute = match.start(1) + usage.start()
        if absolute < position:
            problems.append(
                f"内联脚本第 {text[:absolute].count(chr(10)) + 1} 行就用了 {usage.group(0)}，"
                f"但 {script_name} 到第 {text[:position].count(chr(10)) + 1} 行才引入"
                f"（会抛 “{usage.group(1)} is not defined”，整块脚本中断）"
            )
        break
    return problems


def check_dependencies(path: Path) -> list:
    """只查脚本依赖与顺序：用到 Auth.* / LT.* 却没引入对应 js，或引入得太晚。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    sources = set(re.findall(r"<script[^>]*\bsrc=[\"']([^\"']+)[\"']", text, re.I))
    basenames = {value.rsplit("/", 1)[-1].split("?")[0] for value in sources}
    problems = []
    for global_name, script_name in (("Auth", "auth.js"), ("LT", "common.js")):
        if re.search(rf"\b{global_name}\.", text) and script_name not in basenames:
            problems.append(f"使用了 {global_name}.* 但没有引入 {script_name}")
    problems.extend(check_script_order(text))
    return problems


def main() -> int:
    failed = 0
    for name in PAGES:
        path = WEB / name
        if not path.exists():
            print(f"[SKIP] {name} 不存在")
            continue
        problems = check_page(path)
        if problems:
            failed += 1
            print(f"[FAIL] {name}")
            for item in problems:
                print(f"       - {item}")
        else:
            print(f"[ OK ] {name}（标签配平 + 内联脚本语法 + id 唯一 + 脚本依赖）")

    others = sorted(path for path in WEB.rglob("*.html")
                    if path.name not in PAGES and "node_modules" not in path.parts)
    if others:
        print("-" * 56)
        print("其余页面（含子目录；只查脚本依赖与引入顺序，历史页面不做配平强校验）：")
        bad = []
        for path in others:
            problems = check_dependencies(path)
            if problems:
                failed += 1
                bad.append(path)
                print(f"[FAIL] {path.relative_to(WEB)}")
                for item in problems:
                    print(f"       - {item}")
        if not bad:
            print(f"[ OK ] 其余 {len(others)} 个页面脚本依赖与顺序正常")

    print("-" * 56)
    if failed:
        print(f"{failed} 个页面存在问题")
        return 1
    print("全部页面自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())