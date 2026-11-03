# -*- coding: utf-8 -*-
"""意图路由冒烟测试（临时脚本，不属于 LiveTalking 仓库）。

覆盖：语法/导入检查、意图命中与误命中回归、拆行识别、性能实测、合规与口头禅。
运行：在 E:\\AiLive\\LiveTalking 目录下
      E:\\AiLive\\env\\python.exe ..\\tools\\smoke\\_smoke_intent.py
"""
import os
import pathlib
import sys
import time

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

LINE = "=" * 78

print(LINE)
print("【0】语法与导入检查")
print(LINE)
for name in [
    "server/intent_router.py",
    "server/subtitle_ocr.py",
    "server/sales_catalog.py",
    "server/subtitle_routes.py",
    "server/live_studio.py",
]:
    path = pathlib.Path(name)
    if not path.exists():
        print(f"  [SKIP] 文件不存在 {name}")
        continue
    compile(path.read_text(encoding="utf-8"), name, "exec")
    print(f"  [OK]   语法 {name}")

for module in ["server.intent_router", "server.subtitle_routes", "server.live_studio"]:
    try:
        __import__(module)
        print(f"  [OK]   导入 {module}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] 导入 {module}: {type(exc).__name__}: {exc}")

from server.intent_router import intent_router                       # noqa: E402
from server.sales_catalog import local_flavor, product_catalog       # noqa: E402
from server.subtitle_ocr import pick_question_line                   # noqa: E402

# (评论文本, 期望意图, 是否回归用例)
CASES = [
    ("多少钱一斤", "price", False),
    ("这个多少钱", "price", False),
    ("能不能便宜点", "discount", False),
    ("几斤一箱", "spec", False),
    ("是活的吗", "fresh", False),
    ("包邮吗", "shipping_fee", False),
    ("几天能到", "shipping_time", True),      # 回归：旧关键词「几天到」中间插字漏命中
    ("什么时候发货", "shipping_time", False),
    ("是不是本地发货", "origin", False),
    ("怎么做才好吃", "cook", False),
    ("有没有优惠券", "discount", False),
    ("还有货吗", "stock", False),
    ("坏了怎么办", "aftersale", False),
    ("点个赞", "follow", False),
    ("今天天气不错", None, False),
    ("主播多大了", None, True),               # 回归：旧关键词「多大」误命中 spec
    ("老板几岁了", None, True),               # 回归：排除词机制
    ("欢迎来到直播间", None, False),
]

print()
print(LINE)
print("【1】意图命中与误命中回归")
print(LINE)
passed = 0
regressions = 0
for text, expect, is_regression in CASES:
    hit = intent_router.match(text, product=None)
    got = hit["id"] if hit else None
    ok = got == expect
    passed += 1 if ok else 0
    if is_regression:
        regressions += 1 if ok else 0
    flag = "PASS" if ok else "FAIL"
    tag = " [回归]" if is_regression else ""
    print(f"[{flag}]{tag} {text:<12} 期望={str(expect):<15} 实际={str(got):<15}")
    if hit and not ok:
        print(f"         误命中话术：{hit['response']}")
print(f"\n总体命中：{passed}/{len(CASES)}   回归用例：{regressions}/3")

print()
print(LINE)
print("【2】拆行识别（修复「多条评论揉成糊问题」）")
print(LINE)
screen_lines = ["欢迎来到直播间", "这个多少钱", "包邮吗", "主播好看"]
print("模拟屏幕分行：    ", screen_lines)
print("旧行为（直接拼接）：", "".join(screen_lines))
print("新行为（挑提问行）：", pick_question_line(screen_lines))
hit = intent_router.match_lines(screen_lines, product=None)
print("逐行意图命中：    ", (hit or {}).get("id"), "| 来源行:", (hit or {}).get("source_text"))

print()
print(LINE)
print("【3】关键词长度优先（更具体的规则胜出）")
print(LINE)
for text in ["多少钱一斤", "多少钱", "几斤", "今天多少钱一斤啊"]:
    hit = intent_router.match(text, product=None)
    print(f"  {text:<12} -> {str((hit or {}).get('id')):<14} 命中词={str((hit or {}).get('keyword'))}")

print()
print(LINE)
print("【4】性能实测：意图直答省掉了多少时间")
print(LINE)
sample = "这个多少钱一斤"
rounds = 2000
start = time.perf_counter()
for _ in range(rounds):
    intent_router.match(sample, product=None)
elapsed = (time.perf_counter() - start) / rounds * 1000
print(f"  单次意图匹配平均耗时：{elapsed:.4f} ms（{rounds} 次）")
print(f"  对比：调用一次大模型通常 800~3000 ms")
if elapsed > 0:
    print(f"  加速比：约 {1000 / elapsed:.0f}x（仅比较匹配环节）")
print(f"  截图周期 500 ms 占主导，意图直答的端到端延迟 ≈ 0.5~1.0 s")

print()
print(LINE)
print("【5】合规兜底与口头禅轮换")
print(LINE)
print("  合规内置规则数：", len(product_catalog.status()["compliance"]["builtin_rules"]))
print("  greeting 轮换：", [local_flavor.pick("greeting") for _ in range(3)])
print("  urge 轮换：    ", [local_flavor.pick("urge") for _ in range(3)])
print("  dialect（待本地确认）：", local_flavor.status()["categories"].get("dialect"))

print()
print(LINE)
print("【6】引擎状态")
print(LINE)
status = intent_router.status()
print("  启用规则：", status["enabled_rules"], "| 内置:", status["builtin_rules"],
      "| 自定义:", status["custom_rules"])
print("  命中统计：", status["hit_counts"])