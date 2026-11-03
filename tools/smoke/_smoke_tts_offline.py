# -*- coding: utf-8 -*-
"""离线语音保障冒烟测试（临时脚本，不属于 LiveTalking 仓库）。

覆盖：
  1. 音色口径：config.yaml / 启动脚本 / TTS 缓存必须使用同一个音色
  2. 缓存覆盖率：意图话术是否已全部预热
  3. 断网命中：edge-tts 必然失败时，已预热话术仍能立即播出（不发网络请求、不等待重试）
  4. 未命中：未预热话术不崩溃，走在线合成失败路径，由上层兜底

运行：在 E:\\AiLive\\LiveTalking 目录下
      E:\\AiLive\\env\\python.exe ..\\tools\\smoke\\_smoke_tts_offline.py
"""
import os
import pathlib
import re
import sys
import time
import types

sys.path.insert(0, os.getcwd())
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import edge_tts  # noqa: E402

from server.intent_router import intent_router                     # noqa: E402
from server.sales_catalog import product_catalog                   # noqa: E402
from tts.cache import TTSCache, cache_file, DEFAULT_VOICE, configured_voice, coverage  # noqa: E402
from tts.edge import EdgeTTS                                       # noqa: E402

LINE = "=" * 78
RESULTS = []


def report(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok))
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {name}{('：' + detail) if detail else ''}")


class FakeParent:
    """接收音频帧的假 Avatar。"""

    def __init__(self):
        self.frames = 0
        self.events = []

    def put_audio_frame(self, stream, eventpoint):
        self.frames += 1
        self.events.append(eventpoint)


class OfflineEdge:
    """模拟断网：任何网络调用都立刻抛错，并统计次数。"""

    calls = 0

    def __init__(self, *args, **kwargs):
        OfflineEdge.calls += 1
        raise ConnectionError("模拟断网：无法连接 EdgeTTS")


def build_engine(parent):
    opt = types.SimpleNamespace(REF_FILE=configured_voice(), fps=25)
    return EdgeTTS(opt, parent)


print(LINE)
print("【1】音色口径一致性")
print(LINE)
voice = configured_voice()
print(f"  config.yaml 解析音色：{voice}")
launcher = pathlib.Path(r"E:\AiLive\tools\start_ai_live.ps1")
if launcher.exists():
    text = launcher.read_text(encoding="utf-8-sig")
    found = re.search(r"--REF_FILE'?,\s*'([^']+)'", text)
    if found:
        print(f"  启动脚本写死音色：    {found.group(1)}")
        report("启动脚本音色与配置一致", found.group(1) == voice)
    else:
        print("  启动脚本未写死音色（跟随 config.yaml）")
        report("启动脚本音色与配置一致", True)
report("缓存内置默认音色与配置一致", DEFAULT_VOICE == voice, f"DEFAULT_VOICE={DEFAULT_VOICE}")

print()
print(LINE)
print("【2】意图话术缓存覆盖率")
print(LINE)
phrases = intent_router.collect_phrases(product_catalog.list_ready())
cov = coverage(phrases)
print(f"  待播话术 {cov['total']} 条 · 已缓存 {cov['cached']} 条 · 缺失 {cov['missing']} 条")
report("全部话术已预热（断网可播）", cov["missing"] == 0)

print()
print(LINE)
print("【3】断网时命中缓存：直接本地播放")
print(LINE)
edge_tts.Communicate = OfflineEdge  # 必须在创建 TTS 之前打补丁
parent = FakeParent()
engine = build_engine(parent)
cached_text = phrases[0] if phrases else "价格以商品卡为准，我只报核对过的。"
OfflineEdge.calls = 0
start = time.perf_counter()
engine._try_synthesize((cached_text, {}))
elapsed = time.perf_counter() - start
print(f"  话术：{cached_text}")
print(f"  音频帧：{parent.frames} 帧（{parent.frames * 0.02:.2f}s 语音）")
print(f"  端到端耗时：{elapsed * 1000:.1f} ms")
print(f"  edge-tts 网络调用：{OfflineEdge.calls} 次")
report("断网仍能播出", parent.frames > 0)
report("全程未发起网络调用", OfflineEdge.calls == 0)
report("毫秒级响应（<500ms）", elapsed < 0.5, f"{elapsed * 1000:.1f} ms")

print()
print(LINE)
print("【4】未预热话术（断网）：不崩溃、由上层兜底")
print(LINE)
parent2 = FakeParent()
engine2 = build_engine(parent2)
OfflineEdge.calls = 0
start = time.perf_counter()
engine2._try_synthesize(("这是一句没有预热过的新话术", {}))
elapsed = time.perf_counter() - start
print(f"  音频帧：{parent2.frames}")
print(f"  edge-tts 尝试：{OfflineEdge.calls} 次（主引擎重试 + 降级兜底）")
print(f"  耗时：{elapsed:.2f}s，无异常抛出")
report("未预热话术不阻塞、不崩溃", parent2.frames == 0 and OfflineEdge.calls >= 1)

print()
print(LINE)
print("【5】缓存文件损坏：自动删除并尝试重新合成")
print(LINE)
broken_text = "损坏文件自愈测试话术"
broken_path = cache_file(configured_voice(), broken_text)
broken_path.parent.mkdir(parents=True, exist_ok=True)
broken_path.write_bytes(b"this is not a wav file")
cache_engine = TTSCache(types.SimpleNamespace(REF_FILE=configured_voice(), fps=25), FakeParent())
OfflineEdge.calls = 0
raised = False
try:
    cache_engine.txt_to_audio((broken_text, {}))
except Exception:
    raised = True
report("损坏缓存文件已被清理", not broken_path.exists())
report("重新合成失败时抛错交给降级链", raised)

print()
print(LINE)
failed = [name for name, ok in RESULTS if not ok]
print(f"结果：{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
if failed:
    print("未通过：" + "、".join(failed))
else:
    print("离线语音链路就绪：预热命中即可零延迟、断网播报。")
print(LINE)
