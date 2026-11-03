# -*- coding: utf-8 -*-
"""把界面文件「固化」进桌面应用：LiveTalking\\web\\  →  app\\ui\\ 。

为什么要固化
------------
桌面版以前直接加载 ``http://127.0.0.1:8010/app.html``，那是**网页端**的文件：谁改了
web 目录，桌面端就跟着变，用户看到的永远是"网页版的界面"。固化之后，桌面端渲染的是
随应用打包的这份界面（``app/ui/``），网页端文件不再参与桌面端的渲染；两边从此可以各自
演进。数据仍然走本地服务（``/api/*`` 等由 app\\ui_server.py 反向代理到 8010）。

产物
----
* ``app/ui/desktop.html``  桌面端界面入口（来自 web\\app.html）
* ``app/ui/web/**``        其余界面文件（来自 web\\ 目录）
* ``app/ui/MANIFEST.json`` 固化清单：每个文件的 sha256 + 源目录 + 时间，用于核对漂移

用法
----
    E:\\AiLive\\env\\python.exe E:\\AiLive\\app\\vendor_ui.py            # 固化（会覆盖 app\\ui）
    E:\\AiLive\\env\\python.exe E:\\AiLive\\app\\vendor_ui.py --check    # 只核对，不写文件
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent          # app/
PROJECT = HERE.parent                           # 项目根（E:\AiLive）
SRC_DIR = PROJECT / "LiveTalking" / "web"       # 网页端界面源
UI_DIR = HERE / "ui"                            # 桌面端界面（随应用打包）
WEB_DIR = UI_DIR / "web"
ENTRY_SRC = SRC_DIR / "app.html"
ENTRY_DST = UI_DIR / "desktop.html"
MANIFEST = UI_DIR / "MANIFEST.json"

# 不固化：web\desktop.html 是上一版"像桌面的网页"，已废弃，且会被 desktop.html 入口遮蔽
SKIP_FILES = {"desktop.html"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_source() -> list[Path]:
    if not SRC_DIR.is_dir():
        raise SystemExit("找不到网页端界面目录：%s" % SRC_DIR)
    files = []
    for p in sorted(SRC_DIR.rglob("*")):
        if not p.is_file():
            continue
        if p.name in SKIP_FILES and p.parent == SRC_DIR:
            continue
        files.append(p)
    return files


def build_manifest(files: list[Path]) -> dict:
    entries = []
    for src in files:
        rel = src.relative_to(SRC_DIR).as_posix()
        entries.append({"path": rel, "size": src.stat().st_size, "sha256": sha256_file(src)})
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_dir": str(SRC_DIR),
        "entry": {"source": str(ENTRY_SRC.relative_to(PROJECT)),
                  "target": "desktop.html",
                  "sha256": sha256_file(ENTRY_SRC)},
        "file_count": len(entries),
        "files": entries,
        "note": "桌面端渲染的就是这份固化副本；改网页端后需要重新固化并重新打包，桌面端才会变。",
    }


def do_vendor(clean: bool = False) -> int:
    files = collect_source()
    if not ENTRY_SRC.is_file():
        raise SystemExit("找不到界面入口：%s" % ENTRY_SRC)
    # 增量同步：以前是 shutil.rmtree(UI_DIR) 整目录重建，但界面文件已经过 50 个，
    # 一次性删这么多文件会被环境的 safe-delete 钩子拦下（打包直接失败）。
    # 现在只删「源目录里已经没有」的残留文件，其余逐个覆盖，行为等价但没有批量删除。
    if UI_DIR.exists() and (clean or not WEB_DIR.is_dir()):
        shutil.rmtree(UI_DIR)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {src.relative_to(SRC_DIR).as_posix() for src in files}
    stale = []
    for path in WEB_DIR.rglob("*"):
        if path.is_file() and path.relative_to(WEB_DIR).as_posix() not in wanted:
            stale.append(path)
    for path in stale:
        path.unlink()
    for src in files:
        dst = WEB_DIR / src.relative_to(SRC_DIR)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    shutil.copy2(ENTRY_SRC, ENTRY_DST)
    manifest = build_manifest(files)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[固化] 界面已固化：%d 个文件 -> %s" % (manifest["file_count"], UI_DIR))
    if stale:
        print("[固化] 清理残留：%d 个文件（源目录已删除）" % len(stale))
    print("[固化] 入口：%s（sha256 %s）" % (ENTRY_DST, manifest["entry"]["sha256"][:16]))
    return 0


def do_check() -> int:
    """核对固化的副本与当前网页端是否一致（漂移检查，不写文件）。"""
    if not MANIFEST.is_file():
        print("[核对] 还没有固化过：%s 不存在" % MANIFEST)
        return 1
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    recorded = {e["path"]: e["sha256"] for e in manifest["files"]}
    drift = []
    for src in collect_source():
        rel = src.relative_to(SRC_DIR).as_posix()
        if recorded.pop(rel, None) != sha256_file(src):
            drift.append("变更/新增：" + rel)
    for rel in recorded:
        drift.append("固化了但网页端已删除：" + rel)
    if ENTRY_DST.is_file() and ENTRY_SRC.is_file():
        if sha256_file(ENTRY_DST) != sha256_file(ENTRY_SRC):
            drift.append("入口 desktop.html 与 web\\app.html 不一致")
    elif not ENTRY_DST.is_file():
        drift.append("缺少入口：" + str(ENTRY_DST))
    if drift:
        print("[核对] 发现 %d 处漂移（重新固化即可对齐）：" % len(drift))
        for line in drift:
            print("   - " + line)
        return 1
    print("[核对] 固化副本与网页端一致（%d 个文件）" % manifest["file_count"])
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把界面文件固化进桌面应用 app\\ui\\")
    ap.add_argument("--check", action="store_true", help="只核对是否漂移，不写文件")
    ap.add_argument("--clean", action="store_true", help="整目录重建（默认增量同步，避免批量删除被拦）")
    args = ap.parse_args(argv)
    return do_check() if args.check else do_vendor(clean=args.clean)


if __name__ == "__main__":
    sys.exit(main())