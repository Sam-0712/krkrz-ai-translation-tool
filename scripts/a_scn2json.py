#!/usr/bin/env python3
"""
SCN ↔ JSON/JSONL 双向转换（萌系AI汉化工具链核心）

正向:  python scripts/a_scn2json.py input output
反向:  python scripts/a_scn2json.py input output --patch zh.jsonl [--names config.toml]

正向输出:
  output/json/*.ks.json             完整反编译脚本
  output/translations/ori_text.jsonl  可翻译文本
反向输出:
  output/scn/*.ks.scn               已注入翻译的修补版
"""

import json, os, re, struct, sys, tomllib, argparse
from tqdm import tqdm
from pathlib import Path

# ──────────────────────────────────────────────
# 一、PSB 二进制解析
# ──────────────────────────────────────────────

def _sign_extend(v, bits):
    if bits <= 0: return 0
    if v & (1 << (bits - 1)): v |= ~((1 << bits) - 1)
    return v


_TYPE_KIND = [0, 0, 2, 2, 3, 3, 3, 3, 3, 4, 4, 4, 4,
              5, 5, 5, 5, 6, 6, 6, 6, 7, 7, 7, 7,
              8, 8, 8, 8, 9, 10, 11, 12, 12]


def _read_psb_array(data, p):
    """读取 PSB 变长整数数组，返回 (count, entry_len, data_start, end_pos)"""
    tb = data[p]; p += 1
    n = tb - 0x0C; cnt = 0
    for i in range(n): cnt |= data[p] << (i * 8); p += 1
    el = data[p] - 0x0C; p += 1
    return cnt, el, p, p + cnt * el


def _read_psb_header(data):
    if data[0:4] != b'PSB\0': raise ValueError('Not a PSB file')
    ver = struct.unpack_from('<H', data, 4)[0]
    fmt = '<IIIIIIIIII' if ver < 3 else '<IIIIIIIIIII'
    f = struct.unpack_from(fmt, data, 8)
    return dict(enc=f[0], names=f[1], strs=f[2], strdata=f[3],
                chkoff=f[4], chklen=f[5], chkdata=f[6],
                entries=f[7], emote=f[8] if len(f) > 8 else 0, version=ver)


class PsbParser:
    """解析 PSB 二进制为 Python dict。"""

    def __init__(self, data: bytes):
        self.data = data; self._hdr = _read_psb_header(data)
        self._parse_names(); self._parse_strings()
        self._parse_entries()

    def _get_arr(self, base, el, idx):
        p = base + idx * el; v = 0
        for i in range(el): v |= self.data[p + i] << (i * 8)
        return v

    def _parse_names(self):
        p = self._hdr['names']
        self._s1c, self._s1e, self._s1b, p = _read_psb_array(self.data, p)
        self._s2c, self._s2e, self._s2b, p = _read_psb_array(self.data, p)
        self._s3c, self._s3e, self._s3b, p = _read_psb_array(self.data, p)

    def get_name(self, idx):
        a = self._get_arr(self._s3b, self._s3e, idx)
        b = self._get_arr(self._s2b, self._s2e, a); cs = []
        while True:
            c = self._get_arr(self._s2b, self._s2e, b)
            d = self._get_arr(self._s1b, self._s1e, c)
            e = b - d; b = c; cs.append(chr(e))
            if not b: break
        return ''.join(reversed(cs))

    def _parse_strings(self):
        p = self._hdr['strs']
        self._stc, self._ste, self._stb, _ = _read_psb_array(self.data, p)
        self._strdata = self.data[self._hdr['strdata']:]

    def get_string(self, idx):
        off = self._get_arr(self._stb, self._ste, idx)
        end = self._strdata.find(b'\0', off)
        raw = self._strdata[off:end] if end >= 0 else self._strdata[off:]
        return raw.decode('utf-8', errors='replace')

    def _unpack(self, p):
        tb = self.data[p]; p += 1
        k = _TYPE_KIND[tb] if tb < len(_TYPE_KIND) else 0
        if tb <= 1: return None, p
        if tb == 2: return False, p
        if tb == 3: return True, p
        if k in (3, 4):
            n = tb - 4; v = 0
            for i in range(n): v |= self.data[p] << (i * 8); p += 1
            return (_sign_extend(v, n * 8) if n else 0), p
        if k in (5, 6):
            if 0x0D <= tb <= 0x14:
                return self._unpack_arr(p - 1)
            n = tb - 12; v = 0
            for i in range(n): v |= self.data[p] << (i * 8); p += 1
            return v, p
        if k == 7:
            n = tb - 0x14; v = 0
            for i in range(n): v |= self.data[p] << (i * 8); p += 1
            return self.get_string(v), p
        if k == 8:
            n = tb - 0x18; v = 0
            for i in range(n): v |= self.data[p] << (i * 8); p += 1
            return f'#resource#{v}', p
        if k == 9: return 0.0, p
        if k == 10: x = struct.unpack_from('<f', self.data, p)[0]; return x, p + 4
        if k == 11: x = struct.unpack_from('<d', self.data, p)[0]; return x, p + 8
        if tb == 0x20: return self._unpack_coll(p)
        if tb == 0x21: return self._unpack_obj(p)
        return None, p

    def _unpack_arr(self, p):
        c, el, b, e = _read_psb_array(self.data, p)
        return [self._get_arr(b, el, i) for i in range(c)], e

    def _unpack_obj(self, p):
        buf = p; nc, ne, nb, buf = _read_psb_array(self.data, buf)
        oc, oe, ob, buf = _read_psb_array(self.data, buf)
        base = buf; r = {}
        for i in range(nc):
            nm = self.get_name(self._get_arr(nb, ne, i))
            off = self._get_arr(ob, oe, i)
            r[nm], _ = self._unpack(base + off)
        return r, buf

    def _unpack_coll(self, p):
        buf = p; oc, oe, ob, buf = _read_psb_array(self.data, buf)
        base = buf
        return [self._unpack(base + self._get_arr(ob, oe, i))[0] for i in range(oc)], buf

    def _parse_entries(self):
        self.root, _ = self._unpack(self._hdr['entries'])

    def to_dict(self): return getattr(self, 'root', {})


# ──────────────────────────────────────────────
# 二、文本提取（JSON → ori_text.jsonl）
# ──────────────────────────────────────────────

def extract_texts(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    fname = data.get('name', os.path.basename(json_path))
    entries = []
    for scene in data.get('scenes', []):
        label = scene.get('label', '')
        for idx, entry in enumerate(scene.get('texts', [])):
            if not isinstance(entry, list) or len(entry) < 2: continue
            sp = entry[0]
            for tg in (entry[1] or []):
                if not isinstance(tg, list) or len(tg) < 2: continue
                raw = tg[1]
                if not isinstance(raw, str) or not raw.strip(): continue
                clean = re.sub(r'%[^;]*;', '', raw).strip()
                if not clean: continue
                entries.append(dict(file=fname, scene=label, index=idx,
                                    speaker=sp, text_raw=raw, text_clean=clean))
    return entries


# ──────────────────────────────────────────────
# 三、PSB 修补（translations_zh.jsonl → SCN）
# ──────────────────────────────────────────────

def _list_psb_strings(data):
    hdr = _read_psb_header(data)
    _, sel, soff_base, _ = _read_psb_array(data, hdr['strs'])
    # get values
    p2 = hdr['strs']
    tb = data[p2]; p2 += 1
    n = tb - 0x0C; cnt = 0
    for i in range(n): cnt |= data[p2] << (i * 8); p2 += 1
    el = data[p2] - 0x0C; p2 += 1
    offs = []
    for _ in range(cnt):
        v = 0
        for j in range(el): v |= data[p2 + j] << (j * 8)
        offs.append(v); p2 += el
    ob = hdr['strdata']
    strs = []
    for i, vo in enumerate(offs):
        ao = ob + vo
        end = data.find(b'\0', ao)
        raw = data[ao:end] if end >= 0 else data[ao:]
        strs.append(dict(idx=i, off=vo, abs=ao, blen=len(raw), text=raw.decode('utf-8', errors='replace')))
    return strs, hdr, cnt, el, offs


def patch_scn(data, translations, name_map=None):
    strs, hdr, sc, sel, soff = _list_psb_strings(data)
    data = bytearray(data)
    pat = re.compile(r'%[^;]*;')
    repl = {}
    for t in translations:
        cl = t.get('text_clean', '').strip()
        zh = t.get('translation', '') or ''
        if not cl or not zh or cl == zh: continue
        for s in strs:
            if pat.sub('', s['text']).strip() == cl:
                repl[s['text']] = zh; break
    if name_map:
        for o, n in name_map.items():
            if not n or o == n: continue
            for s in strs:
                if s['text'] == o: repl[o] = n
                elif s['text'].startswith(o + '.'): repl[s['text']] = n + s['text'][len(o):]
    if not repl: return bytes(data), 0

    need_exp = any(len(nn.encode('utf-8')) > len(oo.encode('utf-8')) for oo, nn in repl.items())
    if need_exp:
        nb = bytearray(); no = []
        for s in strs:
            nt = repl.get(s['text'], s['text'])
            en = nt.encode('utf-8'); no.append(len(nb))
            nb.extend(en); nb.append(0)
        delta = len(nb) - (hdr['chkoff'] - hdr['strdata'])
        # update index table
        pp = hdr['strs'] + 1
        n1 = 0; pp2 = pp
        for i in range(sel): n1 |= data[pp2] << 0; pp2 += 1
        # simpler approach: calculate position directly
        pp = hdr['strs']; tb = data[pp]; pp += 1
        n = tb - 0x0C; pp += n + 1
        for i in range(sc):
            for j in range(sel):
                data[pp + j] = (no[i] >> (j * 8)) & 0xFF
            pp += sel
        bef = bytes(data[:hdr['strdata']])
        aft = bytes(data[hdr['strdata'] + (hdr['chkoff'] - hdr['strdata']):])
        data = bytearray(bef) + nb + bytearray(aft)
        if delta:
            for pos, ov in [(4+4*4, hdr['chkoff']), (4+4*5, hdr['chklen']), (4+4*6, hdr['chkdata'])]:
                if ov > hdr['strdata']:
                    struct.pack_into('<I', data, pos, (ov + delta) & 0xFFFFFFFF)
    else:
        for ot, nt in repl.items():
            en = nt.encode('utf-8')
            for s in strs:
                if s['text'] == ot or pat.sub('', s['text']).strip() == ot:
                    data[s['abs']:s['abs'] + len(en)] = en
                    if len(en) < s['blen']:
                        data[s['abs'] + len(en):s['abs'] + s['blen']] = b'\0' * (s['blen'] - len(en))
                    break
    return bytes(data), len(repl)


# ──────────────────────────────────────────────
# 四、辅助函数
# ──────────────────────────────────────────────

def load_jsonl(path):
    with open(path, 'r', encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def save_jsonl(path, entries):
    with open(path, 'w', encoding='utf-8') as f:
        for e in entries: f.write(json.dumps(e, ensure_ascii=False) + '\n')


def load_name_map(toml_path):
    with open(toml_path, 'rb') as f:
        cfg = tomllib.load(f)
    tpl = cfg.get('prompt', {}).get('system', {}).get('char_dict', {}).get('template', '')
    names = {}
    for line in tpl.split('\n'):
        line = line.strip()
        if '=' in line and '"' in line:
            k, v = line.split('=', 1)
            v = v.strip().strip('"')
            if v: names[k.strip()] = v
    return names


# ──────────────────────────────────────────────
# 五、CLI
# ──────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='SCN ↔ JSON/JSONL 双向转换')
    ap.add_argument('input_dir', help='SCN 输入目录')
    ap.add_argument('output_dir', help='输出根目录')
    ap.add_argument('--patch', metavar='ZH_JSONL', help='反向模式：修补 SCN')
    ap.add_argument('--names', metavar='TOML', help='人名字典（patch 时生效）')
    args = ap.parse_args()

    inp = Path(args.input_dir)
    out = Path(args.output_dir)
    json_dir = out / 'json'
    scn_dir = out / 'scn'
    trans_dir = out / 'translations'
    scn_files = sorted(inp.glob('*.ks.scn'))
    if not scn_files:
        print(f'在 {inp} 中未找到 *.ks.scn'); sys.exit(1)

    # ── 正向 ──
    if not args.patch:
        json_dir.mkdir(parents=True, exist_ok=True)
        trans_dir.mkdir(parents=True, exist_ok=True)
        all_entries = []
        for sp in tqdm(scn_files, desc='解析进度', unit='json'):
            with open(sp, 'rb') as f:
                parser = PsbParser(f.read())
            result = parser.to_dict()
            jp = json_dir / f'{sp.stem}.json'
            with open(jp, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            all_entries.extend(extract_texts(str(jp)))
        jlp = trans_dir / 'ori_text.jsonl'
        save_jsonl(str(jlp), all_entries)
        print(f'  → JSONL: {jlp.name} ({len(all_entries)} 条)')

    # ── 反向 ──
    else:
        scn_dir.mkdir(parents=True, exist_ok=True)
        zh_path = Path(args.patch)
        if not zh_path.exists():
            print(f'错误: 找不到 {zh_path}'); sys.exit(1)
        tl = load_jsonl(str(zh_path))
        nm = load_name_map(Path(args.names)) if args.names else {}
        total = 0
        for sp in scn_files:
            print(f'修补: {sp.name}')
            with open(sp, 'rb') as f:
                data = f.read()
            ft = [t for t in tl if t.get('file', '').startswith(sp.stem.replace('.ks', ''))]
            patched, n = patch_scn(data, ft, nm)
            if n:
                op = scn_dir / sp.name
                with open(op, 'wb') as f: f.write(patched)
                print(f'  → {op.name} ({n} 处)')
                total += n
            else:
                print(f'  (无修改)')
        print(f'\n共 {total} 处修补')


if __name__ == '__main__':
    main()
