#!/usr/bin/env python3
"""
将翻译写回原始 .ks.scn (PSB) 二进制文件。

功能：
  1. 读取 translations_zh.jsonl，按文件找到对应译文
  2. 读取 TOML 人名字典，替换说话者名
  3. 在 PSB 二进制字符串表中原地替换文本
  4. 输出到 patched/ 目录

用法：
  python scripts/patch_scn.py
  python scripts/patch_scn.py --dry-run          # 预览，不做实际修改
"""

import json
import re
import struct
import sys
from pathlib import Path

import tomllib


# ---------------------------------------------------------------------------
# PSB 二进制工具
# ---------------------------------------------------------------------------

def read_psb_header(data):
    """解析 PSB 文件头，返回各节偏移。"""
    sig = data[0:4]
    if sig != b'PSB\0':
        raise ValueError(f'不是 PSB 文件: {sig!r}')
    ver = struct.unpack_from('<H', data, 4)[0]
    fmt = '<IIIIIIIIII' if ver < 3 else '<IIIIIIIIIII'
    fields = struct.unpack_from(fmt, data, 8)
    return {
        'version': ver,
        'offset_encrypt': fields[0],
        'offset_names': fields[1],
        'offset_strings': fields[2],
        'offset_strings_data': fields[3],
        'offset_chunk_offsets': fields[4],
        'offset_chunk_lengths': fields[5],
        'offset_chunk_data': fields[6],
        'offset_entries': fields[7],
        'offset_emote': fields[8] if len(fields) > 8 else 0,
    }


def read_psb_array(data, offset):
    """读取 PSB 变长整数数组。返回 (count, entry_width, values)"""
    p = offset
    type_byte = data[p]; p += 1
    n1 = type_byte - 0x0C
    count = 0
    for i in range(n1):
        count |= data[p] << (i * 8); p += 1
    el = data[p] - 0x0C; p += 1
    values = []
    for i in range(count):
        v = 0
        for j in range(el):
            v |= data[p + j] << (j * 8)
        values.append(v)
        p += el
    return count, el, values, p


def list_strings(data):
    """列出 PSB 文件字符串表中所有字符串。返回 list of (index, offset_in_data, raw_bytes, text)"""
    hdr = read_psb_header(data)
    off_base = hdr['offset_strings_data']

    # 读取字符串索引表
    cnt, el, values, _ = read_psb_array(data, hdr['offset_strings'])

    strings = []
    for i, voff in enumerate(values):
        abs_off = off_base + voff
        end = data.find(b'\0', abs_off)
        if end < 0:
            raw = data[abs_off:]
        else:
            raw = data[abs_off:end]
        try:
            text = raw.decode('utf-8')
        except:
            text = raw.decode('utf-8', errors='replace')
        strings.append({
            'index': i,
            'data_offset': voff,
            'abs_offset': abs_off,
            'byte_length': len(raw),
            'raw_bytes': raw,
            'text': text,
        })
    return strings


def find_string_by_clean_text(strings, clean_target):
    """通过 clean 文本（去掉 %...; 标签后）匹配字符串。"""
    pat = re.compile(r'%[^;]*;')
    for s in strings:
        cleaned = pat.sub('', s['text']).strip()
        if cleaned == clean_target:
            return s
    return None


def find_string_by_text(strings, text_target):
    """通过完整文本精确匹配。"""
    for s in strings:
        if s['text'] == text_target:
            return s
    return None


def replace_string_inplace(data, string_info, new_text):
    """在 bytearray 中原地替换字符串。
    新字符串必须不超过原字符串的字节长度（多出的用 \\0 填充）。
    返回替换后的 bytearray 和新长度。
    """
    new_bytes = new_text.encode('utf-8')
    old_len = string_info['byte_length']
    new_len = len(new_bytes)

    if new_len <= old_len:
        # 原地替换，剩余填充 \0
        off = string_info['abs_offset']
        data[off:off + new_len] = new_bytes
        if new_len < old_len:
            data[off + new_len:off + old_len] = b'\0' * (old_len - new_len)
        return data, old_len  # 原占位无变化
    else:
        # 新字符串更长 —— 需要原地扩展 + 标记偏移失效
        return None, new_len  # 调用方处理


def rebuild_string_section(data, hdr, str_arr_offset, strings, replacements):
    """重建 PSB 字符串表（解决需要扩展空间的翻译）。
    
    步骤：
      1. 用新文本替换旧文本，重新生成 string data
      2. 更新字符串索引表中的偏移量
      3. 调整所有受影响的节偏移（header 中）
    """
    data = bytearray(data)
    off_strdata = hdr['offset_strings_data']

    # --- 1. 构建新的 string data ---
    # new_offsets[i] = 新 string data 中第 i 个字符串的偏移量
    new_strings_buf = bytearray()
    new_offsets = []
    for s in strings:
        new_text = replacements.get(s['text'], s['text'])
        encoded = new_text.encode('utf-8')
        new_offsets.append(len(new_strings_buf))
        new_strings_buf.extend(encoded)
        new_strings_buf.append(0)  # null terminator

    old_size = hdr['offset_chunk_offsets'] - off_strdata
    new_size = len(new_strings_buf)
    delta = new_size - old_size

    # --- 2. 更新字符串索引表中的偏移量 ---
    # str_arr_offset 处的数组存储了每个字符串在 string data 中的偏移
    # 读取数组结构再写回
    cnt, el, old_values, arr_end = read_psb_array(data, str_arr_offset)
    p = str_arr_offset
    # 跳过数组头部（type + count bytes + entry_len byte）
    type_byte = data[p]; p += 1
    n1 = type_byte - 0x0C
    p += n1 + 1  # count bytes + entry_len byte
    for i in range(cnt):
        for j in range(el):
            data[p + j] = (new_offsets[i] >> (j * 8)) & 0xFF
        p += el

    # --- 3. 替换 string data ---
    # 把旧的 string data 替换为新的
    before = data[:off_strdata]
    after  = data[off_strdata + old_size:]
    data = bytearray(before) + new_strings_buf + bytearray(after)

    # --- 4. 调整字符串数据之后各节的偏移 ---
    # PSB 中节序不按 header 字段排列。
    # 实际文件布局：
    #   header | name_table | entries | str_index | string_data | chunk_offsets | chunk_lengths | chunk_data
    # 只有 string_data 之后的节才需调整
    if delta != 0:
        # 确定哪些节在 string data 之后
        sections_after = []
        # 按 offset 排序找出在 off_strdata 之后的节
        entries = [
            ('offset_chunk_offsets', 8 + 4 * 4, hdr['offset_chunk_offsets']),
            ('offset_chunk_lengths', 8 + 4 * 5, hdr['offset_chunk_lengths']),
            ('offset_chunk_data',    8 + 4 * 6, hdr['offset_chunk_data']),
        ]
        if hdr['version'] >= 3:
            entries.append(('offset_emote', 8 + 4 * 8, hdr['offset_emote']))
        # 只调整偏移在 string data 之后的节
        for field, byte_pos, old_val in entries:
            if old_val > off_strdata:
                new_val = (old_val + delta) & 0xFFFFFFFF
                struct.pack_into('<I', data, byte_pos, new_val)

    return data


def needs_rebuild(strings, replacements):
    """检查是否有字符串需要扩展（原长度不足）。"""
    for s in strings:
        if s['text'] in replacements:
            new_text = replacements[s['text']]
            new_bytes = new_text.encode('utf-8')
            if len(new_bytes) > s['byte_length']:
                return True
    return False


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def patch_file(scn_path, translations, name_map, dry_run=False):
    """处理单个 .scn 文件。返回 (修改数, 跳过数, 错误数)"""
    fname = scn_path.name.replace('.scn', '')
    # .ks.scn → .ks
    if fname.endswith('.ks'):
        pass

    with open(scn_path, 'rb') as f:
        data = bytearray(f.read())

    # 列出所有字符串
    try:
        strings = list_strings(data)
    except ValueError as e:
        print(f'  [跳过] 无法解析: {e}')
        return 0, 0, 1

    # 构建替换映射
    # 1) 文本替换: text_clean → translation
    # 2) 说话者替换: 原名 → 译名
    file_translations = [t for t in translations if t.get('file', '').startswith(fname)]
    
    replacements = {}  # old_text -> new_text
    modified_count = 0
    skipped = []
    errors = []

    # -- 文本替换 --
    for entry in file_translations:
        clean_text = entry.get('text_clean', '').strip()
        trans_text = entry.get('translation', '') or ''
        if not clean_text or not trans_text:
            continue
        if clean_text == trans_text:
            continue  # 没有翻译

        s = find_string_by_clean_text(strings, clean_text)
        if s is None:
            skipped.append(f'未找到: {clean_text[:50]}')
            continue

        old_text = s['text']
        replacements[old_text] = trans_text

    # -- 说话者替换 --
    hdr = read_psb_header(data)
    # 说话者名也在字符串表里，按原名查找替换
    for old_name, new_name in name_map.items():
        if not new_name or old_name == new_name:
            continue
        s = find_string_by_text(strings, old_name)
        if s:
            replacements[s['text']] = new_name
        else:
            # 也尝试匹配带后缀的（如 れんか.stand）
            for s2 in strings:
                if s2['text'].startswith(old_name + '.'):
                    new_with_suffix = new_name + s2['text'][len(old_name):]
                    replacements[s2['text']] = new_with_suffix

    if not replacements:
        print(f'  [跳过] 无替换项')
        return 0, len(skipped), 0

    # -- 执行替换 --
    need_expand = sum(1 for old, new in replacements.items()
                      if len(new.encode('utf-8')) > len(old.encode('utf-8')))

    if dry_run:
        safe_print = lambda s: print(s.encode('utf-8', errors='replace').decode('utf-8', errors='replace'))
        safe_print(f'\n  === Dry-Run: {scn_path.name} ===')
        safe_print(f'  共 {len(replacements)} 处替换（{need_expand} 处需扩展空间）:')
        for old, new in sorted(replacements.items()):
            ol = len(old.encode('utf-8'))
            nl = len(new.encode('utf-8'))
            delta = nl - ol
            tag = ' ⚠扩展' if delta > 0 else (' 缩短' if delta < 0 else ' 等长')
            text_display = old[:50].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            trans_display = new[:40].encode('utf-8', errors='replace').decode('utf-8', errors='replace')
            safe_print(f'    {text_display:50s} → {trans_display:40s} ({ol:3d}B→{nl:3d}B,{delta:+d}){tag}')
        return len(replacements), len(skipped), need_expand

    # 实际写入
    data = bytearray(data)

    if need_expand > 0:
        # 需要扩展 → 重建字符串表
        data = rebuild_string_section(data, hdr, hdr['offset_strings'], strings, replacements)
        modified_count = len(replacements)
        print(f'  [重建] 字符串表重建完成 ({modified_count} 处替换, 含 {need_expand} 处扩展)')
    else:
        # 全部原地替换
        modified_count = 0
        for old_text, new_text in replacements.items():
            s = (find_string_by_text(strings, old_text) or
                 find_string_by_clean_text(strings, re.sub(r'%[^;]*;', '', old_text).strip()))
            if s is None:
                errors.append(f'字符串索引丢失: {old_text[:50]}')
                continue
            data, _ = replace_string_inplace(data, s, new_text)
            modified_count += 1

        print(f'  [原地替换] {modified_count} 处')

    # 保存
    if modified_count > 0:
        root = Path(__file__).resolve().parent.parent
        out_dir = root / 'output' / 'scn'
        out_dir.mkdir(exist_ok=True)
        out_path = out_dir / scn_path.name
        with open(out_path, 'wb') as f:
            f.write(data)
        print(f'  [写入] {out_path}')
    else:
        print(f'  [跳过] 无修改')

    return modified_count, len(skipped), need_expand


def load_name_map(toml_path):
    """从 TOML 加载人名字典。"""
    with open(toml_path, 'rb') as f:
        cfg = tomllib.load(f)
    name_map = {}
    prompt = cfg.get('prompt', {}).get('system', {}).get('char_dict', {}).get('template', '')
    for line in prompt.split('\n'):
        line = line.strip()
        if '=' in line and '"' in line:
            parts = line.split('=', 1)
            key = parts[0].strip()
            val = parts[1].strip().strip('"')
            if val:
                name_map[key] = val
    return name_map


def main():
    import argparse
    ap = argparse.ArgumentParser(description='将翻译写回 .scn 文件')
    ap.add_argument('--dry-run', action='store_true', help='预览，不做修改')
    ap.add_argument('--translations', default='output/translations/translations_zh.jsonl')
    ap.add_argument('--scn-dir', default='input/scn', help='原始 .scn 文件目录')
    ap.add_argument('--toml', default='config/translation_config.toml')
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent

    # 加载译文
    t_path = root / args.translations
    with open(t_path, 'r', encoding='utf-8') as f:
        translations = [json.loads(l) for l in f if l.strip()]
    print(f'加载译文: {len(translations)} 条')

    # 加载人名字典
    name_map = load_name_map(root / args.toml)
    if name_map:
        print(f'人名字典: {len(name_map)} 项')
    else:
        print('人名字典为空')

    # 遍历 .scn 文件
    scn_dir = root / args.scn_dir
    scn_files = sorted(scn_dir.glob('*.scn'))
    print(f'\n找到 {len(scn_files)} 个 .scn 文件')

    total_modified = 0
    for scn in scn_files:
        print(f'\n处理: {scn.name}')
        modified, skipped, errors = patch_file(
            scn, translations, name_map, dry_run=args.dry_run)
        total_modified += modified

    print(f'\n总共修改: {total_modified} 处')
    if args.dry_run:
        print('(此为 dry-run，未写入任何文件)')


if __name__ == '__main__':
    main()
