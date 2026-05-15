# KiriKiriZ AI 汉化工具链

KiriKiriZ 引擎游戏（.ks.scn 文件）的 AI 汉化自动化工具链。

## 目录结构

```
package/
├── input/scn/           # 原始 .ks.scn 文件
├── output/
│   ├── json/           # 反编译后的 JSON 文件
│   ├── translations/   # 翻译文本
│   │   ├── ori_text.jsonl         # 待翻译文本
│   │   └── translations_zh.jsonl  # AI 翻译结果
│   └── scn/            # 修补后的 SCN 文件
├── config/
│   └── translation_config.toml  # 翻译配置
├── scripts/            # 处理脚本
└── launcher.py         # 启动器
```

## 使用流程

### 方式一：使用启动器（推荐）

```bash
python launcher.py
```

按数字选择步骤：
1. **正向**: SCN → JSON + ori_text.jsonl
2. **提取角色名**: 从 JSON 中提取说话者名称
3. **AI 翻译**: 翻译 ori_text.jsonl → translations_zh.jsonl
4. **清洗译文**: 清理翻译结果 → translations_zh_cleaned.jsonl
5. **反向**: JSONL → 修补 SCN

### 方式二：手动执行脚本

#### 步骤 1: 反编译 SCN

```bash
python scripts/a_scn2json.py input/scn output
```

输出：
- `output/json/*.ks.json` - 完整反编译脚本
- `output/translations/ori_text.jsonl` - 待翻译文本

#### 步骤 2: 提取角色名（可选）

```bash
python scripts/b_extract_speakers.py
```

输出角色列表，用于配置人名字典。

#### 步骤 3: AI 翻译

编辑 `config/translation_config.toml`，配置：
- API 密钥和地址
- 人名字典
- 游戏简介

```bash
python scripts/c_translate.py
```

输出：`output/translations/translations_zh.jsonl`

#### 步骤 4: 清洗译文

```bash
python scripts/d_clean_translations.py
```

清洗项目：
- 去除首尾空白和残留换行
- 去除 AI 误加的「」
- 恢复丢失的「」
- & 转全角 ＆
- 引号规范化

输出：`output/translations/translations_zh.jsonl`

#### 步骤 5: 写回 SCN

```bash
python scripts/e_jsonl2scn.py
```

输出：`output/scn/*.ks.scn`（修补后的文件）

## 配置文件说明

### config/translation_config.toml

```toml
[api]
api_key = "your-api-key"
base_url = "https://api.example.com/v1"
model = "deepseek-ai/DeepSeek-V4-Flash"

[translation]
batch_size = 30  # 每批翻译条数
input = "ori_text.jsonl"
output = "translations_zh.jsonl"

[prompt.system.char_dict]
template = """
## 人名字典

"""
```

## 注意事项

完成 `.scn` 补丁文件后，使用 [GARbro](https://github.com/morkt/GARbro) 打包机翻补丁，并在游戏根目录使用 [KrkrPatch](https://github.com/crskycode/KrkrPatch) 加载 `.xp3` 补丁进行游戏。

> 原则上来说应该把借鉴原始代码真正弄成一键包，但是我太菜了就暂且先这样吧。

## 依赖

```bash
pip install tomllib tqdm openai
```

Python 3.11+

## 致谢
- [GARbro](https://github.com/morkt/GARbro)
- [KrkrPatch](https://github.com/crskycode/KrkrPatch)
- [PSB Analysis](https://github.com/number201724/psbfile)

## License

MIT
