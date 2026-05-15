#!/usr/bin/env python3
"""萌系AI汉化工具链 - 启动器"""

import os, sys, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
S = ROOT / 'scripts'
C = ROOT / 'config'
I = ROOT / 'input' / 'scn'
J = ROOT / 'output' / 'json'
SCNOUT = ROOT / 'output' / 'scn'

SCRIPTS_CMDS = {
    '1': f'python "{S/"a_scn2json.py"}" "{I}" "{ROOT/"output"}"',
    '2': f'python "{S/"b_extract_speakers.py"}"',
    '3': f'python "{S/"c_translate.py"}"',
    '4': f'python "{S/"d_clean_translations.py"}"',
    '5': f'python "{S/"e_jsonl2scn.py"}"',
}

STEPS = [
    ('1', '正向: SCN → JSON + ori_text.jsonl', SCRIPTS_CMDS['1']),
    ('2', '提取角色名', SCRIPTS_CMDS['2']),
    ('3', 'AI 翻译', SCRIPTS_CMDS['3']),
    ('4', '清洗译文', SCRIPTS_CMDS['4']),
    ('5', '反向: JSONL → 修补 SCN', SCRIPTS_CMDS['5']),
    ('Q', '退出', None),
]


def run(cmd):
    print(f'\n>>> {cmd}\n')
    r = subprocess.run(cmd, shell=True, cwd=str(ROOT))
    if r.returncode: print(f'\n[!] 返回码 {r.returncode}'); return False
    return True


def menu():
    print('\n' + '=' * 50 + '\n  萌系AI汉化工具链\n' + '=' * 50)
    print(f'  SCN: {I}  输出: {ROOT/"output"}\n')
    for k, lbl, _ in STEPS: print(f'  [{k}] {lbl}')
    print()


def main():
    for d in [I, J, SCNOUT, ROOT / '_logs']: d.mkdir(parents=True, exist_ok=True)
    while True:
        menu()
        c = input('请选择: ').strip().lower()
        if c == 'q': break
        s = next((s for s in STEPS if s[0] == c), None)
        if s and s[2]: run(s[2])
        else: print('无效选择')
        input('\n按 Enter...')
    print('再见！')

if __name__ == '__main__':
    try: main()
    except KeyboardInterrupt: print('\n已取消')
