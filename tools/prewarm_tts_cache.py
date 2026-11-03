# -*- coding: utf-8 -*-
"""预热 TTS 音频缓存：把意图话术全部合成到本地，实现「断网也能播报」。

背景
----
意图直答（T1）的话术是有限集合，同一句话会被反复播报。
把它们提前合成成 16k WAV 缓存后：
  1. 断网也能播报（现场演示保命）
  2. 播报零合成延迟（直接读本地文件）
  3. 减少云端 TTS 调用量

用法（在任意目录执行）
----------------------
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\prewarm_tts_cache.py --dry-run
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\prewarm_tts_cache.py
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\prewarm_tts_cache.py --voice zh-CN-XiaoxiaoNeural

注意：预热需要联网（用 edge-tts 合成）。预热完成后即可断网使用。
"""

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
PROJECT = BASE / "LiveTalking"
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from server.intent_router import intent_router          # noqa: E402
from server.sales_catalog import product_catalog        # noqa: E402
from tts.cache import CACHE_DIR, DEFAULT_VOICE, cache_file, warm_phrases  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="预热意图话术的 TTS 音频缓存")
    parser.add_argument("--voice", default="", help="音色名，留空则用默认音色")
    parser.add_argument("--dry-run", action="store_true", help="只列出话术，不实际合成")
    parser.add_argument("--no-flavor", action="store_true", help="不生成口头禅变体")
    parser.add_argument("--clear", action="store_true", help="预热前清空缓存目录（话术改版后使用）")
    args = parser.parse_args()

    if args.clear and CACHE_DIR.exists():
        removed = 0
        for path in CACHE_DIR.glob("*.wav"):
            path.unlink()
            removed += 1
        print(f"已清空缓存，删除 {removed} 个文件")

    voice = args.voice or os.getenv("TTS_VOICE") or DEFAULT_VOICE
    products = product_catalog.list_ready()
    phrases = intent_router.collect_phrases(products, use_flavor=not args.no_flavor)
    cached = sum(1 for text in phrases if cache_file(voice, text).exists())

    print("=" * 78)
    print("TTS 音频缓存预热")
    print("=" * 78)
    print(f"音色：      {voice}")
    print(f"缓存目录：  {CACHE_DIR}")
    print(f"已核对商品：{len(products)} 件")
    print(f"待处理话术：{len(phrases)} 条（已缓存 {cached} 条）")
    print()

    if args.dry_run:
        for index, text in enumerate(phrases, 1):
            mark = "HIT " if cache_file(voice, text).exists() else "MISS"
            print(f"[{mark}] {index:>3}. {text}")
        print()
        print(f"预计需新建合成：{len(phrases) - cached} 条")
        return

    def progress(index: int, total: int, text: str) -> None:
        print(f"  ({index}/{total}) {text[:44]}")

    result = warm_phrases(phrases, voice=voice, on_progress=progress)

    print()
    print("=" * 78)
    print(
        f"完成：新建 {result['created']} 条 · 命中已有 {result['skipped']} 条 "
        f"· 失败 {len(result['failed'])} 条"
    )
    if result["failed"]:
        print("失败明细（通常是网络问题）：")
        for item in result["failed"][:10]:
            print(f"  - {item['text'][:44]} :: {item['error']}")

    # 时长统计：语音长度直接决定吞吐，是调话术的核心指标
    try:
        import soundfile as sf

        durations = []
        for text in phrases:
            path = cache_file(voice, text)
            if path.exists():
                data, rate = sf.read(str(path))
                durations.append(len(data) / rate)
        if durations:
            durations.sort()
            avg = sum(durations) / len(durations)
            print()
            print(
                f"语音时长：最短 {durations[0]:.2f}s · 中位 {durations[len(durations) // 2]:.2f}s "
                f"· 最长 {durations[-1]:.2f}s · 平均 {avg:.2f}s"
            )
            print(f"吞吐参考：平均 {avg:.1f}s 语音 + 5s 冷却 → 单条回应占用约 {avg + 5:.1f}s")
    except Exception:
        pass

    print("=" * 78)


if __name__ == "__main__":
    main()