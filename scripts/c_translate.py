#!/usr/bin/env python3
"""
KiriKiriZ 游戏文本 AI 翻译 pipeline

工作流：
  1. 读取 ref/translation_config.toml 中的配置与提示词
  2. 读取 translations.jsonl 中的待翻译文本
  3. 按文件分组，每组按 batch_size 分批
  4. 每批拼接提示词，调用 AI API 翻译
  5. 将翻译结果写回新的 .jsonl

用法：
  python scripts/translate.py [--resume] [--log LOGFILE]
"""

import json
import logging
import os
import sys
import time
import re
from datetime import datetime
from pathlib import Path

# 确保 config/ 在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent / 'config'))

import tomllib

try:
    from tqdm import tqdm
except ModuleNotFoundError:
    print("请安装 tqdm: pip install tqdm")
    sys.exit(1)

from openai import OpenAI


# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

def setup_logging(log_path: Path, verbosity: int = logging.INFO):
    """配置日志：同时输出到控制台（简洁）和日志文件（详细）。"""
    root_logger = logging.getLogger('translate')
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()

    # 文件 handler — 完整详细
    fh = logging.FileHandler(str(log_path), encoding='utf-8', mode='a')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    root_logger.addHandler(fh)

    # 控制台 handler — 简洁（等级以上的消息），通过 translate.log 屏蔽 tqdm 干扰
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(verbosity)
    ch.setFormatter(logging.Formatter('%(message)s'))
    root_logger.addHandler(ch)

    return root_logger


log = logging.getLogger('translate')


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_jsonl(path: str) -> list[dict]:
    entries = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def save_jsonl(path: str, entries: list[dict]):
    with open(path, 'w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def group_by_file(entries: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for e in entries:
        fname = e.get('file', 'unknown')
        groups.setdefault(fname, []).append(e)
    return groups


def build_system_prompt(cfg: dict) -> str:
    """将 TOML 中 [prompt.system.*] 的各段拼接为一条 system prompt。"""
    parts = []
    system_cfg = cfg.get('prompt', {}).get('system', {})
    for section_name in ('header', 'game_intro', 'char_dict', 'place_dict', 'footer'):
        section = system_cfg.get(section_name, {})
        template = section.get('template', '').strip()
        if not template:
            continue
        template = template.replace('{游戏名}', cfg.get('game_name', '未命名游戏'))
        parts.append(template)
    return '\n\n'.join(parts)


def build_user_message(batch: list[dict]) -> str:
    """将一批文本拼成模型可读的 JSON 数组字符串。"""
    items = []
    for i, entry in enumerate(batch):
        items.append({
            'index': i,
            'speaker': entry.get('speaker'),
            'text': entry['text_clean'],
        })
    return json.dumps(items, ensure_ascii=False)


def parse_translation_response(response_text: str, batch_size: int) -> list[str]:
    """从模型回复中解析出翻译结果列表。
    期望返回 JSON 数组: [{"index": 0, "translation": "..."}, ...]
    """
    text = response_text.strip()

    # 尝试从代码块中提取 JSON
    if '```' in text:
        blocks = re.findall(r'```(?:json)?\s*([\s\S]*?)```', text)
        if blocks:
            text = blocks[0].strip()

    # 尝试提取最外层的 JSON 数组
    try:
        arr = json.loads(text)
        if isinstance(arr, list):
            # 按 index 排序并提取 translation
            arr.sort(key=lambda x: x.get('index', 0))
            result = []
            # 如果模型只返回部分条目，允许缺失
            for item in arr:
                idx = item.get('index')
                translation = item.get('translation', '')
                # 确保 result 长度足够
                while len(result) <= idx:
                    result.append(None)
                result[idx] = translation
            # 填充缺失的为 None
            while len(result) < batch_size:
                result.append(None)
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # 备选：尝试逐行解析 "index: translation" 格式
    lines = text.split('\n')
    result: list[str | None] = [None] * batch_size
    for line in lines:
        m = re.match(r'^\s*(\d+)\s*[:\-=]\s*(.*)', line)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < batch_size:
                result[idx] = m.group(2).strip()
    return result


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def translate_batch(
    client: OpenAI,
    model: str,
    system_prompt: str,
    batch: list[dict],
    batch_idx: int,
    total_batches: int,
    file_name: str,
    retries: int = 3,
) -> list[str]:
    """翻译一批文本，失败时重试。"""
    user_msg = build_user_message(batch)

    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_msg},
                ],
                temperature=0.3,
            )
            reply = response.choices[0].message.content
            translations = parse_translation_response(reply, len(batch))

            ok_count = sum(1 for t in translations if t is not None)
            log.debug(f'  [批 {batch_idx+1}/{total_batches}] {file_name}: '
                      f'发送 {len(batch)} 条, 收到 {ok_count}/{len(batch)} 条')
            return translations

        except Exception as e:
            log.warning(f'  [批 {batch_idx+1}/{total_batches}] {file_name}: '
                        f'失败 (尝试 {attempt}/{retries}): {e}')
            if attempt < retries:
                wait = 2 ** attempt
                log.info(f'    等待 {wait} 秒后重试...')
                time.sleep(wait)
            else:
                log.error(f'    放弃此批')
                return [None] * len(batch)


def main():
    import argparse

    ap = argparse.ArgumentParser(description='KiriKiriZ 游戏文本 AI 翻译')
    ap.add_argument('--resume', action='store_true',
                    help='从已有的输出文件中恢复（跳过已翻译的条目）')
    ap.add_argument('--log', default=None,
                    help='日志文件路径（默认 translate_<时间戳>.log）')
    args = ap.parse_args()

    # 定位项目根目录
    root = Path(__file__).resolve().parent.parent
    config_path = root / 'config' / 'translation_config.toml'

    if not config_path.exists():
        log.error(f'错误: 找不到 {config_path}')
        log.error('请先在 ref/ 中创建并填写 translation_config.toml')
        sys.exit(1)

    # 初始化日志
    if args.log:
        log_path = root / args.log
    else:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        log_path = root / '_logs' / f'translate_{ts}.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)  # 确保目录存在
    setup_logging(log_path)

    cfg = load_config(str(config_path))
    api_cfg = cfg.get('api', {})
    trans_cfg = cfg.get('translation', {})

    api_key = api_cfg.get('api_key', '')
    base_url = api_cfg.get('base_url', '')
    model = api_cfg.get('model', '')
    batch_size = trans_cfg.get('batch_size', 30)
    transl_dir = root / 'output' / 'translations'
    input_path = transl_dir / trans_cfg.get('input', 'translations', 'ori_text.jsonl')
    output_path = transl_dir / trans_cfg.get('output', 'translations', 'translations_zh.jsonl')

    if not api_key:
        log.error('错误: TOML 中未设置 api_key')
        sys.exit(1)

    # 加载数据
    log.info(f'加载文本: {input_path}')
    all_entries = load_jsonl(str(input_path))
    log.info(f'  共 {len(all_entries)} 条文本')

    file_groups = group_by_file(all_entries)
    file_names = sorted(file_groups.keys())
    log.info(f'  共 {len(file_names)} 个文件')

    # 初始化 API 客户端
    client = OpenAI(api_key=api_key, base_url=base_url)

    # 构建 system prompt 并记录到日志
    system_prompt = build_system_prompt(cfg)
    log.info('\n=== System Prompt ===')
    for line in system_prompt.strip().split('\n'):
        log.info(f'  | {line}')
    log.info('=== End System Prompt ===\n')

    # 预览各文件批次
    total_batches_global = 0
    for fname in file_names:
        entries = file_groups[fname]
        nb = (len(entries) + batch_size - 1) // batch_size
        total_batches_global += nb
        log.info(f'📄 {fname} ({len(entries)} 条, {nb} 批)')
    log.info(f'\n总计 {total_batches_global} 批')
    log.info(f'日志文件: {log_path}')

    # 开始确认
    confirm = input('\n是否开始翻译? (y/N): ').strip().lower()
    if confirm != 'y':
        log.info('已取消')
        return

    # --- 恢复模式 ---
    translated_indices: set[tuple] = set()
    if args.resume and output_path.exists():
        log.info(f'检测到已有输出文件，尝试恢复...')
        with open(str(output_path), 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    existing = json.loads(line)
                    # 用 (file, scene, index) 三元组作为唯一标识
                    translated_indices.add(
                        (existing.get('file'), existing.get('scene'), existing.get('index'))
                    )
        log.info(f'  已翻译 {len(translated_indices)} 条，跳过')
        output_entries = load_jsonl(str(output_path))
    else:
        output_entries = []

    # 建立已翻译集合
    done_set = translated_indices

    # --- 开始翻译 ---
    translated_count = len(done_set)  # 恢复模式下为已翻译数量，否则为 0
    total_count = len(all_entries)
    batch_global = 0

    log.info(f'开始翻译，batch_size={batch_size}')

    with tqdm(total=total_count, desc='翻译进度', unit='条',
              bar_format='{l_bar}{bar:30}{r_bar}') as pbar:

        # 先更新进度条到恢复的位置
        pbar.update(translated_count)
        tqdm.write(f'已恢复 {translated_count} 条，继续翻译剩余 {total_count - translated_count} 条')

        for fname in file_names:
            entries = file_groups[fname]
            batches = [entries[i:i + batch_size] for i in range(0, len(entries), batch_size)]
            num_batches = len(batches)

            for bi, batch in enumerate(batches):
                batch_global += 1

                # 恢复模式：检查本批是否全部已翻译
                batch_keys = [(e.get('file'), e.get('scene'), e.get('index')) for e in batch]
                if all(k in done_set for k in batch_keys):
                    pbar.update(len(batch))
                    continue

                translations = translate_batch(
                    client, model, system_prompt, batch,
                    bi, num_batches, fname,
                )

                # 更新条目的 translation 字段
                for j, entry in enumerate(batch):
                    entry['translation'] = translations[j] if j < len(translations) else None

                output_entries.extend(batch)
                # 只累加本次 batch 新翻译成功的数量
                new_translations = sum(1 for t in translations if t is not None)
                translated_count += new_translations

                # 保存
                save_jsonl(str(output_path), output_entries)
                pbar.update(len(batch))
                pbar.set_postfix(batch=f'{batch_global}/{total_batches_global}', file=fname)

    # --- 最终报告 ---
    tqdm.write('')
    tqdm.write('=' * 55)
    tqdm.write(f' 翻译完成!')
    tqdm.write(f'   总条数: {total_count}')
    tqdm.write(f'   已翻译: {translated_count}')
    tqdm.write(f'   失败:   {total_count - translated_count}')
    tqdm.write(f'   输出:   {output_path}')
    tqdm.write(f'   日志:   {log_path}')
    tqdm.write('=' * 55)

    log.info(f'翻译完成: {total_count} 总, {translated_count} 成功, '
             f'{total_count - translated_count} 失败')
    log.info(f'输出: {output_path}')
    log.info(f'日志: {log_path}')


if __name__ == '__main__':
    main()
