---
name: video-dossier
description: >
  把一段视频（B 站 BV 号或本地视频/音频文件）做成一份可复核的「视频档案」，最后输出 PDF：
  平台数据采集、语音转写逐字稿、两级内容大纲（主题 + 知识点，带时间戳）、屏幕抽帧 OCR、
  语言与节奏统计、以及一份写明统计口径的说明文档。
  当用户说「把这个视频跑出来」「视频数据统计」「从视频里提取内容」「视频转 PDF」「一条龙」
  「做个视频档案」，或直接给了一个 BV 号/本地视频并问它讲了什么、要数据、要报告时使用。
  也适用于只跑其中一段：仅转写、仅大纲、仅平台数据、仅屏幕 OCR。
  视频可以是课堂录播、直播回放、讲座、会议、访谈、播客。
license: MIT
compatibility: >
  必需：Python 3.8+、ffmpeg、网络（B 站模式下）。推荐：Node 18+（平台采集）、
  faster-whisper + 本地模型（转写）、rapidocr_onnxruntime（屏幕 OCR）、jieba（中文分词）、
  matplotlib（图表）。PDF 步骤复用 lov-any2pdf 技能的 scripts/md2pdf.py（可选依赖，缺失时会提示安装）。
metadata:
  author: Evan26Ma
  version: "1.0.0"
  tags: video asr ocr bilibili transcript outline report pdf
---

# video-dossier — 视频档案一条龙

把一段视频变成一份**能被人复核、能被后续反复取数**的档案，最后出一份 PDF。

## 三个贯穿全程的词

- **dossier（档案）**：一次 run 的全部产物，全部落在 run 根目录下，任何时候都能整体搬走。
- **口径**：每个数字都要能回答「来自哪个接口/哪个文件、怎么算、什么时候采的、误差在哪」。口径不是装饰，它是交付物本身。
- **raw 为准**：统计只用**原始转写**。纠错稿、OCR 文本、字幕都可以存在，但绝不进统计。

## 一、先建 run

```bash
mkdir <runRoot> && cd <runRoot>
cp <skill>/scripts/config.example.json config.json   # 改 media / video.profile / asr.model
python <skill>/scripts/vd_config.py                  # 自检：回显 run 根、profile、所有路径
```

`video.profile` 决定领域词表（术语表、屏幕关键词、大纲标签）。通用场景用 `generic`；考研政治用 `politics-kaoyan`；换学科就照 `profiles/` 里的样子加一个文件，**不要改脚本**。

**完成判据**：`vd_config.py` 打印出 run 根、profile 与每个 path 的绝对路径，且 `config.json` 里没有指向别人机器的路径。

## 二、一条龙

```bash
python <skill>/scripts/pipeline.py --stages collect,asr,slices,stats,verify
```

`pipeline.py` 把能自动化的阶段按序跑完，并在**需要你介入的阶段**（`outline`、`report`）显式停下、告诉你下一步做什么。单跑某个阶段就直接调对应脚本，参数一致（都吃 `--config`）。

| 阶段 | 命令 | 完成判据 |
|---|---|---|
| `collect` | `fetch_meta.mjs` | `stats/video-meta.json` 存在且 `collectedAt` 是采集瞬间 |
| `asr` | `run_asr.py` | `transcripts/<id>.json` 的时长与视频分P 时长偏差 <1% |
| `slices` | `build_slices.py` | `content/_slices/index.json` 的窗口能覆盖全时长 |
| `outline` | 见「三、派子代理」 | 每片都有 `content/_outline/<id>.json` |
| `merge` | `merge_outline.py` | `problems=[]`；覆盖率与首末跨度 ≥0.9；`gapAlertCount` 为 0 |
| `corrections` | `build_corrections.py` + 人工裁决 | `content/corrections-reviewed.json` 里每条拒绝都带理由 |
| `clean` | `build_clean.py` | 长度守恒通过（`charDelta == charDeltaExpected`） |
| `stats` | `build_stats.py` | `stats/language-stats.json` + ≥4 张图 |
| `slides` | `extract_slides.py --stage all` | `slides/ocr-quality.json` 有 `verdict`；`slides.jsonl` 行数 == `ocrPages` |
| `verify` | `verify.py` | 全部断言 PASS、退出码 0 |
| `magazine` | `make_magazine.py`（通常由 pdf 阶段带起） | 生成 `magazine.html`；章节数 ≥3、图表挂上 |
| `report` | 你写 `报告-*.md` + `content/magazine-copy.json` | 报告里每个读数都能在产物里找到出处；杂志文案见 `references/杂志文案.md` |
| `pdf` | `make_pdf.py` | **默认杂志风**：magazine.html → 无头 Chromium 打印；PDF 生成、能打开、页数 > 1 |

## 三、派子代理（唯一必须你亲自做的阶段）

`outline` 阶段不能自动化：它要读转写做判断。按 `references/子代理任务书.md` 写任务书，**每个切片派一个代理**，并守三条铁律：

1. **写文件，不回传**：代理把结果写到 `content/_outline/<slice>.json`，只回一行 `DONE <file> n=<条数>`。让代理把大 JSON 当返回值回传，通道会在几十 KB 处**静默截断**——真实事故：一次丢了 89,901 字符，整批只能重跑。
2. **并发 ≤4，失败先查文件**：返回 null 不等于没干活，结果文件可能已经落盘；查完再决定重派。
3. **核对 schema**：代理不守 schema 是常态（时间键名会出现 `startSeconds`/`startSec`/`timestamp` 三种，主题会缺 `endSeconds`，描述会藏在 `note`/`text`）。`merge_outline.py` 已做容错，但你要看它报的 `schemaTolerance`——异常高说明任务书写得不清楚，回去改任务书。

**完成判据**：`merge_outline.py` 输出 `problems=[]`，覆盖率与首末跨度都 ≥0.9，`gapAlertCount` 为 0。出现空档就要逐个读窗口核查，不许当没看见（空档可能是代理漏标，也可能是老师在放视频）。

## 四、写报告与出 PDF（默认杂志风）

报告分两份，别混：

1. **杂志版 HTML → PDF**（默认样式，给人看的）：数据由脚本自动填，你只需要写**文案**——标题句、导语、一句话结论、清单、引用块，写进 `content/magazine-copy.json`（字段见 `references/杂志文案.md`）。
2. **Markdown 数据报告**（给口径看的）：结构照 `references/报告模板.md`，口径照 `references/口径模板.md`。

```bash
python <skill>/scripts/make_magazine.py    # 数据 + 文案 → magazine.html
python <skill>/scripts/make_pdf.py         # 默认：magazine.html → 无头 Chromium 打印 PDF
```

`make_pdf.py` 的默认风格就是杂志风（`pdf.style = "magazine"`）。找不到 Chromium 时会提示并回退到 markdown 路线；想强制只用杂志风就设 `pdf.magazineStrict = true`。**这套版式（衬线大标题、大数字、占比条、深色结论块、A4 分页规则）只有浏览器引擎能还原，Markdown 转换器做不到。**

**完成判据**：`magazine.html` 章节 ≥3 且图表挂上；PDF 能打开、页数 > 1、深色块与红色占比条**印出来了**（不是黑白线条——那是丢了背景色，检查 `print-color-adjust`）；两份报告里每个数字都能在 `stats/`、`content/`、`slides/` 里指到文件与字段；口径章节覆盖「不可得清单」。

## 五、按需读的参考

| 你要做什么 | 读 |
|---|---|
| 写统计口径（含不可得清单） | `references/口径模板.md` |
| 写杂志版文案（默认样式） | `references/杂志文案.md` |
| 采集平台数据、搞清哪些指标拿不到 | `references/平台采集.md` |
| 派子代理做大纲 / 写任务书 | `references/子代理任务书.md` |
| 抽帧、去重、OCR、屏幕状态聚合 | `references/画面层.md` |
| 写主报告 | `references/报告模板.md` |
| 别重复踩坑 | `references/踩坑清单.md` |

## 六、只做其中一段

用户只要转写、只要大纲、只要平台数据都可以：按阶段表挑命令跑，其它阶段产物缺失时下游脚本会**明确报错**，而不是产出半成品。

## 边界

- **匿名合规**：B 站接口只用 `buvid` 设备指纹 + wbi 签名，**不使用登录态或 cookies**；不绕过付费内容、不去水印。
- **不伪造**：拿不到的指标（完播率、留存曲线、涨粉、逐日增长、评论全量、弹幕原文）写进口径的「不可得清单」，不估算、不编造。
- **降级要声明**：OCR 命中率低、ASR 疑似截断、代理返回残缺，都必须在报告里显式写出来。
- **劳动成果归属**：视频、音轨、转写是原作者的；档案供个人研究与分析用，别二次分发。
