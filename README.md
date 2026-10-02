# video-dossier · 视频档案一条龙

给一段视频（B 站 BV 号，或本地视频/音频文件），产出一份**可复核、可反复取数**的档案，最后输出 PDF。

不是「AI 总结视频」那种一句话概括，而是把视频**拆成可查证的数据**：

```
平台数据  ──┐
语音转写  ──┤
两级大纲  ──┼──▶  stats/ content/ slides/  ──▶  报告.md  ──▶  报告.pdf
屏幕 OCR  ──┤        （每个数字都能指回文件与字段）
语言统计  ──┘
```

## 它产出什么

| 产物 | 内容 |
|---|---|
| **`magazine.html` / `magazine.pdf`** | **默认样式（杂志风）**：给人看的那份报告 —— 大标题、大数字、占比条、深色结论块、A4 分页 |
| `报告-*.md` | 数据报告（Markdown）：平台数据、内容结构、核心信息、图表、数据质量 |
| `README.md` | **统计口径**：每个指标来自哪个接口/字段、公式、采集时刻、已知误差、不可得清单 |
| `transcripts/*.json` | 原始转写（统计唯一基准） |
| `transcripts/*.raw.txt` / `*.clean.md` | 带时间戳逐字稿 / 术语级修正可读稿（不参与统计） |
| `content/outline.json` / `.md` / `-points.csv` | 两级大纲：一级主题 + 二级知识点，都带时间戳与原文摘录 |
| `content/magazine-copy.json` | 杂志版文案（标题句、结论、清单）—— 与数据分离，**数据由脚本填、文案由你写** |
| `content/corrections.jsonl` | 纠错逐条记录：采纳了什么、拒绝了什么、为什么 |
| `stats/language-stats.json` | 语速、停顿、人声占比、词频、口头禅、术语命中 |
| `stats/per-minute.csv` / `top-words.csv` | 逐分钟序列 / 词频长表（Excel、pandas 直接吃） |
| `stats/figures/*.png` | 语速曲线、累计字数、停顿分布、人声占比 |
| `slides/` | 屏幕抽帧 OCR：页面图、屏幕状态时间轴、关键屏幕文本、OCR 质量读数 |
| `scripts/verify.py` | 全量断言校验（能证伪的那种），退出码即结论 |

## 三条设计原则

1. **口径是交付物**。数字没有口径就是噪音——所以 `README.md` 里的统计口径和代码一样重要，且与产物同目录。
2. **raw 为准**。所有统计只用原始转写。纠错稿、OCR 文本、字幕都可以存在，但绝不进统计。
3. **不伪造**。拿不到的指标（完播率、留存、涨粉、评论全量……）写进「不可得清单」；达不到的精度写「降级声明」。宁可写出来，不要静默略过。

## 默认样式：杂志风

报告默认出**杂志版**（`magazine.html` → `magazine.pdf`）：暖纸底 + 朱红点缀、衬线大标题、66px 大数字、带占比条的表格、深色「一句话结论」块、A4 打印分页规则。

它的关键设计是**数据与文案分离**：

| 谁 | 负责什么 | 落在哪 |
|---|---|---|
| `make_magazine.py` | 表格、数字、图表、目录、文件索引、质量读数 —— **全部从 run 产物读** | 自动生成 |
| 你（agent） | 标题句、导语、一句话结论、清单、引用块 —— 编辑判断，数据推不出来 | `content/magazine-copy.json` |

这样数字永远不会跟产物脱节（改数据不用改模板），而该像人写的地方仍然是人写的。字段见 [`references/杂志文案.md`](references/杂志文案.md)。

样式本身在 [`assets/magazine.css`](assets/magazine.css)：改它 = 改所有产物的观感。PDF 由无头 Chromium 打印（这套版式 Markdown 转换器还原不了）；找不到浏览器时自动回退到 `md2pdf.py` 的 Markdown 路线。

## 快速开始

```bash
git clone https://github.com/Evan26Ma/video-dossier.git
cd video-dossier

# 依赖
python -m pip install faster-whisper rapidocr_onnxruntime jieba matplotlib   # 按需
# 另需：ffmpeg、Node 18+、yt-dlp（B 站模式）

# 建一次 run
mkdir my-run && cd my-run
cp ../scripts/config.example.json config.json     # 改 media.bvid / video.profile / asr.model
python ../scripts/vd_config.py                    # 自检

# 一条龙
python ../scripts/pipeline.py --stages all
```

`pipeline.py` 会把可自动化的阶段跑完，并在**必须你介入的两步**停下：派子代理做大纲、写主报告。它会把下一步该做什么直接打给你。

```bash
python ../scripts/pipeline.py --status                 # 看产物跑到哪了
python ../scripts/pipeline.py --stages collect,asr     # 只跑其中几段
python ../scripts/pipeline.py --dry-run                # 只打印将执行的命令
```

单跑某阶段也行，参数一致（都吃 `--config`）：

```bash
node scripts/fetch_meta.mjs --config config.json       # 平台数据
python scripts/run_asr.py --config config.json         # 转写
python scripts/build_slices.py --config config.json    # 切片
python scripts/merge_outline.py --config config.json   # 合并大纲
python scripts/build_corrections.py --config config.json
python scripts/build_clean.py --config config.json
python scripts/build_stats.py --config config.json
python scripts/extract_slides.py --stage all --config config.json
python scripts/verify.py --config config.json
python scripts/make_pdf.py --config config.json
```

## 领域配置（profile）

术语表、屏幕关键词、大纲标签都放在 `profiles/*.json`，不写死在脚本里：

- `generic`：通用（空术语表）
- `politics-kaoyan`：考研政治五门术语表（马原/毛中特/史纲/思修法基/时政）+ 备考词 + 直播间屏幕关键词

换学科就复制一份改，**不要改脚本**。

## 作为技能安装

这个仓库本身就是一个 Agent Skill（根目录 `SKILL.md`）。装到技能目录即可被自动发现：

```bash
# 用户级（对当前用户所有项目生效）
git clone https://github.com/Evan26Ma/video-dossier.git ~/.agents/skills/video-dossier
```

DSH 的技能发现顺序：`<项目根>/.dsh/skills` → `<项目根>/.agents/skills` → 自定义目录 → `<dshHome>/skills` → `<agentsHome>/skills`。

## 依赖

| 用途 | 依赖 | 必需 |
|---|---|---|
| 平台采集 | Node 18+（原生 fetch）、网络 | B 站模式必需 |
| 音视频 | `ffmpeg`、`yt-dlp` | 必需 |
| 转写 | `faster-whisper` + 本地模型目录 | 必需 |
| 屏幕 OCR | `rapidocr_onnxruntime`（本地 ONNX，离线） | 画面层必需 |
| 统计 | `jieba`、`matplotlib` | 推荐 |
| PDF | `lov-any2pdf` 技能的 `scripts/md2pdf.py`（`reportlab`） | PDF 步骤 |

`make_pdf.py` 会按 `pdf.md2pdf` → `$VD_MD2PDF` → `$DSH_AGENTS_HOME/skills/lov-any2pdf/...` → `~/.agents/skills/...` 的顺序找 `md2pdf.py`，找不到会给出安装提示（不会静默失败）。

## 合规与边界

- **匿名合规**：B 站接口只用 `buvid` 设备指纹 + wbi 签名，不使用登录态或 cookies；不绕过付费内容、不去水印。
- **劳动成果归属**：视频、音轨、转写属于原作者；档案供个人研究分析，请勿二次分发。
- **隐私自检**：画面层抽出来的页面图可能包含聊天窗、群名、未读消息——**发布任何产物前先回看这些图**。

## 已知边界

- 转写是 ASR，**专有名词一定有错字**；纠错走「候选 → 真词筛查 → 人工裁决」三道闸，且修正是纯字符串替换（有长度守恒校验）。
- ASR 的 VAD 会吃掉段内停顿，所以「停顿次数」是系统性低估。
- 画面层的文本块分类是启发式，OCR 数字会有错字，屏幕数字未逐项校对。
- 平台计数是**累计值快照**，没有历史曲线；视频越新，比率越不可比。

## License

MIT
