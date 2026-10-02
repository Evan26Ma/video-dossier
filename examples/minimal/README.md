# 最小示例（全合成数据）

这个目录是**给人看的**，也是**能直接跑的**：它用一段编造的 120 秒「产品评审会」数据，把
**非媒体阶段**（切片 → 大纲合并 → 纠错裁决 → 可读稿 → 统计 → 校验）完整跑通一遍。

数据全部是合成的，不含任何真实录音、姓名或业务内容。

## 直接跑

```bash
cd examples/minimal

python ../../scripts/merge_outline.py    --config config.json
python ../../scripts/build_corrections.py --config config.json
python ../../scripts/build_clean.py      --config config.json
python ../../scripts/build_stats.py      --config config.json
python ../../scripts/verify.py           --config config.json

# 默认样式：杂志版 HTML → 无头 Chromium 打印 PDF
python ../../scripts/make_magazine.py    --config config.json
python ../../scripts/make_pdf.py         --config config.json
```

期望看到的读数：

| 阶段 | 期望 |
|---|---|
| `merge_outline` | `problems=[]`，2 个主题 / 4 条知识点 |
| `build_corrections` | `approved=1`（`记要→纪要`）、`rejected=1`（`评审→评议` 被词典兜底拦下） |
| `build_clean` | `conserved=True`，`delta=0` |
| `build_stats` | 输出 `stats/language-stats.json` 与 4 张图 |
| `verify` | **PASS 43 / FAIL 0 / SKIP 5**（平台、屏幕、仓库三组按配置跳过，不算失败） |
| `make_magazine` | `magazine.html`，4 个章节、4 张图 |
| `make_pdf` | `magazine.pdf`（页数 > 1；深色结论块与红色占比条要印出来） |

`magazine.pdf` 就是仓库里那份可以直接点开看的样例——默认样式的观感看它最快。

## 这个示例刻意演示的几件事

1. **转写契约**：`transcripts/example.json` 的字段就是 `run_asr.py` 的产出格式，可以直接被下游吃。
2. **两级大纲的最小形状**：`content/_outline/slice-01.json` 是子代理该写出的结构（主题 → 知识点，都带时间戳与原文摘录）。
3. **纠错裁决的三个机制**（`content/_terms/` + `content/corrections-decisions.json`）：
   - 错串不是词典词 → 自动采纳（`记要→纪要`）；
   - 错串是词典真词 → 必须人工裁决，否则保守拒绝（示例里 `评审→评议` 被明确 reject）；
   - `override` / `phraseRules` / `letterNoise` 的优先级。
4. **阈值可覆盖**：示例只有 120 秒，所以 `config.json` 的 `verify` 段把覆盖率与体量下限调低了——真实长视频应该用脚本默认值。

## 这个示例**不能**验证什么

- **ASR**：需要真实音视频（`run_asr.py` 找不到音源会以退出码 2 结束）。
- **抽帧 / OCR**：示例里 `slides.enabled = false`，所以屏幕组断言整体 SKIP。真实跑法见 `references/画面层.md`。
- **平台采集**：示例是 `kind: local`，平台组断言整体 SKIP。真实跑法见 `references/平台采集.md`。

## 目录

```
examples/minimal/
├─ config.json                     配置样例（含 verify 阈值覆盖）
├─ transcripts/example.json        合成转写（转写契约）
├─ content/
│   ├─ _slices/                    build_slices.py 生成（切片 + index.json）
│   ├─ _outline/slice-01.json      子代理该产出的结构
│   ├─ _terms/slice-01.json        子代理清点出的错字线索
│   ├─ corrections-decisions.json  人工裁决（reject / override / phraseRules）
│   ├─ magazine-copy.json          杂志版文案（标题句 / 结论 / 清单）—— 数据由脚本填
│   ├─ outline.json / .md / -points.csv
│   └─ corrections.jsonl
├─ stats/                          语言统计、逐分钟 CSV、词频 CSV、4 张图
├─ magazine.html                   默认样式（杂志风）的源文件
└─ magazine.pdf                    默认样式的成品（点开就能看）
```
