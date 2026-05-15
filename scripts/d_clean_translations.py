#!/usr/bin/env python3
"""
清洗 translations_zh.jsonl。

修复项目：
  1. 去除译文首尾空白
  2. 去除译文开头残留的 \n 和 \u3000（全角空格）
  3. 去除 AI 误加的「」（原文无「」但译文将整句话用「」包裹）
  4. 恢复「」丢失
  5. 转义 & 为全角 ＆（KAG 命令冲突）
  6. 外层引号规范化：全角单引号／半角单引号／半角双引号 → 全角双引号 ""（嵌套按中文规范换为 ''）

用法：
  python scripts/clean_translations.py
  python scripts/clean_translations.py --input translations_zh.jsonl --output cleaned.jsonl
"""

import json
import re
import argparse
from pathlib import Path


def load_jsonl(path):
    with open(path, 'r', encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def save_jsonl(path, entries):
    with open(path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def clean_entry(entry):
    """Apply all cleaning rules to a single entry. Returns dict of changes."""
    raw = entry.get('text_clean', '')
    zh = entry.get('translation', '') or ''
    if not zh:
        return entry, {}

    original = zh
    changes = []

    # ---- Rule 1: strip whitespace ----
    zh = zh.strip()
    if zh != original:
        changes.append('strip_whitespace')

    # ---- Rule 2: strip leading \n and \u3000 ----
    zh = zh.lstrip('\n\u3000 ')
    if zh != original:
        changes.append('strip_leading_junk')

    # ---- Rule 3: remove wrongly added 「」 wrappers ----
    # If original does NOT start with 「 but translation starts with 「
    # AND the 「 wraps the entire sentence (there's a matching 」 at/near the end)
    lp, rp = '\u300c', '\u300d'
    
    if lp not in raw and zh.startswith(lp) and rp in zh:
        # Remove the outermost 「」 pair
        # Case A: 「...」 → ...
        if zh.startswith(lp) and zh.endswith(rp):
            zh = zh[1:-1].strip()
            changes.append('remove_outer_kakko')
        # Case B: 「...」...  or ...「...」 (partial) - harder
        # For now, only handle full-wrapper case
        # Also handle the common pattern: 「整句。」 → 整句。
        # or where the last char before 」 is not a period
        elif zh.startswith(lp):
            # Find matching closing bracket
            if rp in zh:
                idx = zh.rindex(rp)
                # Only remove if it's at the end or close to it
                if idx >= len(zh) - 3:
                    zh = zh[1:idx] + zh[idx+1:]
                    zh = zh.strip()
                    changes.append('remove_outer_kakko')

    # ---- Rule 4: restore missing 「」 ----
    # Original has 「」 but translation doesn't
    if lp in raw and lp not in zh:
        # Handle different quote styles: ASCII "", Unicode ""/"'"
        changed = False
        if '"' in zh:
            parts = zh.split('"')
            if len(parts) >= 3:  # at least one pair of ASCII quotes
                result = parts[0]
                for k in range(1, len(parts)):
                    if k % 2 == 1:
                        result += lp + parts[k]
                    else:
                        result += rp + parts[k]
                zh = result
                changed = True
        if not changed and '\u201c' in zh and '\u201d' in zh:
            zh = zh.replace('\u201c', lp).replace('\u201d', rp)
            changed = True
        if not changed and '\u2018' in zh and '\u2019' in zh:
            zh = zh.replace('\u2018', lp).replace('\u2019', rp)
            changed = True
        if changed:
            changes.append('restore_kakko_from_quote')

    # ---- Rule 5: sanitize & (KAG 特殊字符) ----
    # KiriKiri KAG 中 & 是命令起始符，直接出现会导致引擎崩溃
    # 替换为全角 ＆ 以安全显示
    if '&' in zh:
        # 不替换 KAG 标签内的 &（如果有 [&...] 这样的标签）
        # text_clean 中不应有 KAG 标签，直接全局替换
        zh = zh.replace('&', '\uff06')  # ＆ (全角)
        changes.append('sanitize_ampersand')

    # ---- Rule 6: 引号规范化（所有成对引号 → 全角双引号） ----
    LQ, RQ = "\u201c", "\u201d"  # 全角双引号（外层）
    LI, RI = "\u2018", "\u2019"  # 全角单引号（内层）

    def convert_quote_pair(text, open_q, close_q):
        """将 text 中所有 open_q/close_q 配对转换为 LQ/RQ，嵌套层用 LI/RI。"""
        result = []
        depth = 0
        for ch in text:
            if ch == open_q:
                if depth == 0:
                    result.append(LQ)
                else:
                    result.append(LI)
                depth += 1
            elif ch == close_q:
                depth -= 1
                if depth == 0:
                    result.append(RQ)
                else:
                    result.append(RI)
            else:
                result.append(ch)
        return ''.join(result), depth == 0

    def convert_ascii_quotes(text, quote_char, left, right):
        """将 ASCII 引号（开闭字符相同）按奇偶规则转换为指定左右引号。
        返回 (新字符串, 是否平衡)。
        """
        result = []
        is_open = True
        for ch in text:
            if ch == quote_char:
                if is_open:
                    result.append(left)
                else:
                    result.append(right)
                is_open = not is_open
            else:
                result.append(ch)
        return ''.join(result), not is_open

    # 1) 全角单引号（左右不同，可正确处理嵌套）
    if '\u2018' in zh or '\u2019' in zh:
        zh, ok = convert_quote_pair(zh, '\u2018', '\u2019')
        if ok:
            changes.append('fix_outer_quote')

    # 2) 半角引号：根据有无双引号决定外层/内层角色
    has_double = '"' in zh
    has_single = "'" in zh

    if has_double:
        # 双引号作为外层 → 全角双引号
        zh, ok_double = convert_ascii_quotes(zh, '"', LQ, RQ)
        if ok_double:
            changes.append('fix_outer_quote')
        # 单引号作为内层 → 全角单引号
        if has_single:
            zh, ok_single = convert_ascii_quotes(zh, "'", LI, RI)
            if ok_single:
                changes.append('fix_outer_quote')
    elif has_single:
        # 只有单引号，作为外层 → 全角双引号
        zh, ok_single = convert_ascii_quotes(zh, "'", LQ, RQ)
        if ok_single:
            changes.append('fix_outer_quote')
    
    # ---- Final: re-strip after modifications ----
    zh = zh.strip()
    
    if zh != original:
        entry['translation'] = zh
        entry['cleaned'] = True
    
    return entry, changes


def main():
    ap = argparse.ArgumentParser(description='清洗翻译后的 JSONL')
    ap.add_argument('--input', default='output/translations/translations_zh.jsonl')
    ap.add_argument('--output', default='output/translations/translations_zh.jsonl')
    ap.add_argument('--backup', default='output/translations/translations_zh_backup.jsonl',
                    help='备份原文件路径')
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    in_path = root / args.input
    out_path = root / args.output
    bak_path = root / args.backup

    entries = load_jsonl(str(in_path))
    total = len(entries)
    print(f'加载 {in_path}: {total} 条')

    # Backup
    if in_path != out_path:
        save_jsonl(str(bak_path), entries)
    elif bak_path.exists():
        print(f'备份已存在: {bak_path}，跳过备份')
    else:
        save_jsonl(str(bak_path), entries)
        print(f'备份原文件 -> {bak_path}')

    # ---- Clean ----
    change_counts = {}
    modified = 0
    warnings = []

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

    # ---- Save ----
    save_jsonl(str(out_path), entries)
    print(f'\n保存 -> {out_path}')

    # ---- Report ----
    print(f'\n{"=" * 50}')
    print(f'清洗完成!')
    print(f'  总条数: {total}')
    print(f'  修改条数: {modified}')
    print(f'\n  修改明细:')
    label_map = {
        'strip_whitespace': '去除首尾空白',
        'strip_leading_junk': '去除开头 \\n / 全角空格',
        'remove_outer_kakko': '去除 AI 误加的「」',
        'restore_kakko_from_quote': '恢复「」角括号',
        'sanitize_ampersand': '& 转全角 ＆',
        'fix_outer_quote': '外层引号 → 全角双引号',
    }
    for key, count in sorted(change_counts.items(), key=lambda x: -x[1]):
        label = label_map.get(key, key)
        print(f'    {label}: {count} 条')

    if warnings:
        print(f'\n  警告/需手动检查: {len(warnings)} 条')
        for w in warnings:
            print(f'    {w}')


if __name__ == '__main__':
    main()
