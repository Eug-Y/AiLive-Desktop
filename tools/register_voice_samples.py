# -*- coding: utf-8 -*-
"""把 E:\\AiLive\\音频素材 里的录音登记成本机音色（预录音色）。

用法：
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\register_voice_samples.py                # 缺哪个补哪个
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\register_voice_samples.py --force        # 覆盖同名音色
    E:\\AiLive\\env\\python.exe E:\\AiLive\\tools\\register_voice_samples.py --list         # 只看现有音色

登记结果落在 LiveTalking\\data\\voices\\，语音合成页的「音色列表」会直接显示。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / 'LiveTalking'))

from server import voice_library  # noqa: E402

# 素材文件 → 音色名 → 描述
SAMPLES = [
    ('1.m4a', '音色一', '项目本地录音素材（音频素材\\1.m4a）'),
    ('2.m4a', '音色二', '项目本地录音素材（音频素材\\2.m4a）'),
]


def main() -> int:
    ap = argparse.ArgumentParser(description='把音频素材登记为本机音色')
    ap.add_argument('--force', action='store_true', help='同名音色已存在时覆盖')
    ap.add_argument('--list', action='store_true', help='只列出当前音色')
    ap.add_argument('--dir', default=str(PROJECT / '音频素材'), help='素材目录')
    args = ap.parse_args()

    if args.list:
        items = voice_library.list_voices()
        print('当前音色（%d 个）：' % len(items))
        for it in items:
            print('  - %-8s %6.1fs  %s' % (it['name'], it.get('duration') or 0,
                                          voice_library.resolve_audio(it).name))
        return 0

    source = Path(args.dir)
    failed = 0
    for filename, name, desc in SAMPLES:
        path = source / filename
        if not path.is_file():
            print('  ✗ 找不到素材：%s' % path)
            failed += 1
            continue
        exists = voice_library.get_voice(name)
        if exists and not args.force:
            print('  = 已有音色「%s」（%.0f 秒），跳过（要覆盖加 --force）'
                  % (name, exists.get('duration') or 0))
            continue
        try:
            entry = voice_library.add_voice(name, path.read_bytes(), path.name,
                                            ref_text='', description=desc,
                                            overwrite=args.force)
        except ValueError as exc:
            print('  ✗ %s：%s' % (name, exc))
            failed += 1
            continue
        print('  ✓ %s ← %s（%.1f 秒，wav=%s）' % (entry['name'], path.name,
                                                entry['duration'], entry['wav'] or '无'))
    print('音色目录：%s' % voice_library.VOICE_DIR)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
