#!/usr/bin/env python3
"""
从 ori_text.jsonl 中提取所有唯一的 speaker，
输出为可直接粘贴到 TOML 的列表格式。
"""

import json
import sys


def extract_speakers(jsonl_path: str) -> list[str]:
    speakers: set[str] = set()
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            sp = entry.get('speaker')
            if sp:
                speakers.add(sp)
    return sorted(speakers, key=lambda x: x or '')


def main():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    jsonl_path = sys.argv[1] if len(sys.argv) > 1 else str(root / 'output' / 'translations' / 'ori_text.jsonl')
    speakers = extract_speakers(jsonl_path)

    print(f'共 {len(speakers)} 个角色：\n')
    for sp in speakers:
        print(f'{sp} = ""')

    print(f'完成角色配置之后，将结果粘贴到 config/translation_config.toml 中。')


if __name__ == '__main__':
    main()
