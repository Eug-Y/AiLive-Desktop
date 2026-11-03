"""防录播护栏 + 新增开播检查项自检。

不启动服务、不加载数字人模型，直接对 LiveStudioManager 的内存逻辑做断言：

    1. 循环播放时间隔下限自动上浮（含历史 8 秒旧数据）
    2. 关闭循环时不干预间隔
    3. 打乱顺序：排列合法、确实被打乱、新一轮首条避开上一轮末条
    4. 关闭打乱时保持作者顺序（与改动前行为一致）
    5. 间隔随机抖动落在设定区间内且不是固定值
    6. 开播检查中心已包含「值守可观测 / 百货类目资质 / 防录播」三项

只读内存，不调用 _persist()，不会改动 data/ 下的真实运营数据。

用法：
    E:\\AiLive\\env\\python.exe tools\\smoke\\_check_anti_replay.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "LiveTalking"
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，会把中文与符号写崩；统一切到 UTF-8。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from server.live_studio import (  # noqa: E402
    DEFAULT_SETTINGS,
    DEFAULT_WORKFLOW_CHECKS,
    GAP_JITTER_RATIO,
    LOOP_GAP_FLOOR_SECONDS,
    live_studio_manager,
)

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


def set_settings(**kwargs) -> None:
    live_studio_manager._settings.update(kwargs)
    live_studio_manager._normalize_settings()


def set_scripts_in_memory(items) -> None:
    live_studio_manager._scripts = list(items)
    live_studio_manager._script_index = 0
    live_studio_manager._rebuild_play_order()


def main() -> int:
    print("=== 防录播护栏与开播检查项自检 ===")

    print("\n1. 循环播放的间隔下限")
    set_settings(script_gap_seconds=5.0, repeat_scripts=True)
    check(
        f"历史 5 秒 + 循环 → 自动上浮到 {LOOP_GAP_FLOOR_SECONDS:.0f} 秒",
        live_studio_manager._settings["script_gap_seconds"] == LOOP_GAP_FLOOR_SECONDS,
        f"实际 {live_studio_manager._settings['script_gap_seconds']}",
    )
    set_settings(script_gap_seconds=5.0, repeat_scripts=False)
    check(
        "关闭循环 → 不干预，保持 5 秒",
        live_studio_manager._settings["script_gap_seconds"] == 5.0,
        f"实际 {live_studio_manager._settings['script_gap_seconds']}",
    )
    set_settings(script_gap_seconds=999.0, repeat_scripts=True)
    check(
        "上限仍为 300 秒",
        live_studio_manager._settings["script_gap_seconds"] == 300.0,
        f"实际 {live_studio_manager._settings['script_gap_seconds']}",
    )

    print("\n2. 默认设置")
    check("默认间隔已从 8 秒提高到 30 秒", DEFAULT_SETTINGS["script_gap_seconds"] == 30.0)
    check("默认开启打乱话术顺序", DEFAULT_SETTINGS["shuffle_scripts"] is True)

    print("\n3. 打乱顺序")
    set_settings(repeat_scripts=True, shuffle_scripts=True, script_gap_seconds=30.0)
    set_scripts_in_memory([f"话术{i}" for i in range(6)])
    orders = []
    for _ in range(40):
        live_studio_manager._rebuild_play_order()
        orders.append(tuple(live_studio_manager._play_order))
    check("每个排列都是 0..5 的合法全排列", all(sorted(o) == list(range(6)) for o in orders))
    check("确实被打乱（出现多种顺序）", len(set(orders)) > 1, f"仅 {len(set(orders))} 种")
    repeats = sum(
        1
        for _ in range(60)
        if (live_studio_manager._rebuild_play_order(avoid_first=3) or True)
        and live_studio_manager._play_order[0] == 3
    )
    check("新一轮首条避开上一轮末条", repeats == 0, f"仍有 {repeats} 次相同")
    set_scripts_in_memory(["唯一一条"])
    live_studio_manager._rebuild_play_order(avoid_first=0)
    check("单条话术不会崩", live_studio_manager._play_order == [0])
    set_scripts_in_memory([])
    live_studio_manager._rebuild_play_order()
    check("空话术队列安全", live_studio_manager._play_order == [])

    print("\n4. 向后兼容：关闭打乱时按作者顺序")
    set_settings(shuffle_scripts=False)
    set_scripts_in_memory([f"话术{i}" for i in range(6)])
    check("关闭后顺序为 0..5", live_studio_manager._play_order == list(range(6)))

    print("\n5. 间隔随机抖动")
    set_settings(shuffle_scripts=True, repeat_scripts=True, script_gap_seconds=30.0)
    gaps = [live_studio_manager._next_gap_target() for _ in range(300)]
    low, high = 30.0 * (1 - GAP_JITTER_RATIO), 30.0 * (1 + GAP_JITTER_RATIO)
    check(f"抖动全部落在 {low:.1f}~{high:.1f} 秒", all(low <= g <= high for g in gaps))
    check("抖动不是固定值", len({round(g, 3) for g in gaps}) > 100)

    print("\n6. 开播检查中心")
    check(
        "新增两个人工确认项",
        {"supervision_visible", "category_qualified"} <= set(DEFAULT_WORKFLOW_CHECKS),
    )
    checks = {item["key"]: item for item in live_studio_manager.readiness()["checks"]}
    check("含 anti_replay 自动检查", "anti_replay" in checks)
    check("含 supervision_visible 检查", "supervision_visible" in checks)
    check("含 category_qualified 检查", "category_qualified" in checks)
    check(
        "循环 + 打乱 + 30 秒 → 防录播通过",
        checks.get("anti_replay", {}).get("passed") is True,
        checks.get("anti_replay", {}).get("label", ""),
    )
    set_settings(shuffle_scripts=False)
    anti = next(i for i in live_studio_manager.readiness()["checks"] if i["key"] == "anti_replay")
    check("固定顺序 → 防录播判不通过", anti["passed"] is False, anti["label"])
    set_settings(repeat_scripts=False)
    anti = next(i for i in live_studio_manager.readiness()["checks"] if i["key"] == "anti_replay")
    check("关闭循环 → 防录播通过", anti["passed"] is True, anti["label"])

    print(f"\n=== 通过 {PASSED} 项，失败 {len(FAILED)} 项 ===")
    if FAILED:
        for name in FAILED:
            print(f"  失败：{name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())