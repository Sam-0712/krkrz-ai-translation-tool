#!/usr/bin/env python3
"""清洗 translations_zh.jsonl。

修复项：
  1. 去除残留的 \n 和 \u3000（全角空格）
  2. 去除译文首尾空白
  3. 去除 AI 误加的「」（原文无「」但译文整句用「」包裹）
  4. 恢复「」丢失
  5. 转义 & 为全角 ＆（KAG 命令冲突）
  6. 引号规范化：所有引号转换为嵌套结构（半角单引号对强制转为全角单引号）

用法：
  python scripts/clean_translations.py
  python scripts/clean_translations.py --input translations_zh.jsonl --output cleaned.jsonl
"""

import json, re, argparse
from pathlib import Path

def load_jsonl(path):
    with open(path, 'r', encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]

def save_jsonl(path, entries):
    with open(path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')

def clean_entry(entry):
    """对单条记录应用所有清洗规则。返回"""
    raw = entry.get('text_clean', '')
    zh = entry.get('translation', '') or ''
    if not zh:
        return entry, []

    original = zh
    changes = []

    # 规则1：去掉任意位置的 \n
    zh = zh.replace('\\n', '')

    # 规则2：去掉空格
    zh = re.sub(r'\u300c\s+', '\u300c', zh)
    zh = re.sub(r'\s+\u300d', '\u300d', zh)
    zh = zh.strip(' \u3000')
    if zh != original:
        changes.append('strip_whitespace')

    # 规则3：去除 AI 误加的「」
    lp, rp = '\u300c', '\u300d'
    if lp not in raw and zh.startswith(lp) and rp in zh:
        if zh.startswith(lp) and zh.endswith(rp):
            zh = zh[1:-1].strip()
            changes.append('remove_outer_kakko')
        elif zh.startswith(lp):
            idx = zh.rindex(rp)
            if idx >= len(zh) - 3:
                zh = zh[1:idx] + zh[idx+1:]
                zh = zh.strip()
                changes.append('remove_outer_kakko')

    # 规则4：恢复「」
    if lp in raw and lp not in zh:
        changed = False
        if '"' in zh:
            parts = zh.split('"')
            if len(parts) >= 3:
                zh = parts[0] + ''.join(lp + parts[k] if k % 2 else rp + parts[k] for k in range(1, len(parts)))
                changed = True
        if not changed:
            for old, new in [('\u201c', lp), ('\u201d', rp), ('\u2018', lp), ('\u2019', rp)]:
                if old in zh:
                    zh = zh.replace(old, new)
                    changed = True
                    break
        if changed:
            changes.append('restore_kakko_from_quote')

    # 规则5：& 转全角 ＆
    if '&' in zh:
        zh = zh.replace('&', '\uff06')
        changes.append('sanitize_ampersand')

    # 规则6：引号规范化（半角单引号对强制转为全角单引号）
    LP_DBL, RP_DBL = '\u201c', '\u201d'
    LP_SGL, RP_SGL = '\u2018', '\u2019'
    
    def is_english_letter(ch):
        return ('a' <= ch <= 'z') or ('A' <= ch <= 'Z')
    
    def fix_quotes(text):
        quotes = []
        for i, ch in enumerate(text):
            if ch in (LP_DBL, RP_DBL, LP_SGL, RP_SGL, '"', "'"):
                if ch in ('"', "'") and 0 < i < len(text)-1:
                    if is_english_letter(text[i-1]) and is_english_letter(text[i+1]):
                        continue
                quotes.append((i, ch))
        
        if not quotes:
            return text
        
        result = list(text)
        depth = 0
        for idx, (pos, ch) in enumerate(quotes):
            is_left = (idx % 2 == 0)   # 按出现顺序奇偶决定左右
            if ch == "'":              # 半角单引号：强制变为全角单引号
                if is_left:
                    result[pos] = LP_SGL
                    depth += 1
                else:
                    depth = max(0, depth - 1)
                    result[pos] = RP_SGL
            else:                      # 其他引号按嵌套深度交替双/单引号
                if is_left:
                    result[pos] = LP_DBL if depth % 2 == 0 else LP_SGL
                    depth += 1
                else:
                    depth = max(0, depth - 1)
                    result[pos] = RP_DBL if depth % 2 == 0 else RP_SGL
        
        return ''.join(result)
    
    zh = fix_quotes(zh).strip()
    
    if zh != original:
        entry['translation'] = zh
        entry['cleaned'] = True
    
    return entry, changes

def main():
    ap = argparse.ArgumentParser(description='清洗翻译后的 JSONL')
    ap.add_argument('--input', default='output/translations/translations_zh.jsonl')
    ap.add_argument('--output', default='output/translations/translations_zh.jsonl')
    ap.add_argument('--backup', default='output/translations/translations_zh_backup.jsonl')
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    in_path, out_path, bak_path = root / args.input, root / args.output, root / args.backup

    entries = load_jsonl(str(in_path))
    total = len(entries)
    print(f'加载 {in_path}: {total} 条')

    # 备份
    if in_path != out_path:
        save_jsonl(str(bak_path), entries)
    elif not bak_path.exists():
        save_jsonl(str(bak_path), entries)
        print(f'备份原文件 -> {bak_path}')
    else:
        print(f'备份已存在：{bak_path}，跳过备份')

    # 清洗
    change_counts = {}
    modified = 0
    label_map = {
        'strip_whitespace': '去除首尾空白/开头 \\n/全角空格',
        'remove_outer_kakko': '去除 AI 误加的「」',
        'restore_kakko_from_quote': '恢复「」角括号',
        'sanitize_ampersand': '& 转全角 ＆',
        'fix_outer_quote': '引号规范化',
    }

    for entry in entries:
        if not entry.get('translation'):
            continue
        entry, changes = clean_entry(entry)
        if changes:
            modified += 1
            for c in changes:
                change_counts[c] = change_counts.get(c, 0) + 1
        if entry.get('cleaned'):
            del entry['cleaned']

    save_jsonl(str(out_path), entries)
    print(f'\n保存 -> {out_path}')
    print(f'\n{"=" * 50}')
    print(f'清洗完成!  总条数：{total}  修改条数：{modified}')
    print(f'\n修改明细:')
    for key, count in sorted(change_counts.items(), key=lambda x: -x[1]):
        print(f'  {label_map.get(key, key)}: {count} 条')

if __name__ == '__main__':
    main()