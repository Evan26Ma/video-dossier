"""交付前校验：对每个产物做断言，逐条打印 PASS/FAIL 与实际读数，最后以退出码表达结果。

设计原则（保留自实跑版本）：**断言必须能证伪**。只做「文件存在」这类弱断言没有意义；
真正有价值的是长度守恒、时间戳单调、行数守恒、覆盖率、空档审计、仓库零改动。

与实跑版本的差别：
  - 所有写死的数字阈值都换成「从产物里读出来的自洽性断言」，例如
    per-minute.csv 行数 == ceil(duration/60)、pointCount 与 stats 一致、
    slides.jsonl 行数 == ocr-quality.ocrPages、状态 pageCount 之和 == OCR 页数。
    绝不出现「必须恰好 56 个主题」这种只对某一个视频成立的断言。
  - 平台相关断言（弹幕计数、分P 时长对账、字幕轨）改成数据驱动：只有在
    stats/video-meta.json 存在、且配置声明了平台阶段（media.kind=bilibili 或有 bvid）
    时才检查；local 模式（没有 B 站数据）必须能整组跳过并正常跑通。
  - 覆盖率类阈值集中在脚本顶部常量，可用 config.verify.* 覆盖。
  - 本脚本没有写死的绝对路径：run 目录、仓库路径都从 config 读。

退出码：0 = 无 FAIL；1 = 有 FAIL；2 = 一条断言都没跑（产物缺失或配置不对）。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

from vd_config import load

# ---- 默认阈值（config.verify.* 可覆盖）----
MIN_TILED_COVERAGE = 0.90            # 主题铺满时间轴覆盖率下限
MIN_ANNOTATED_SPAN = 0.85            # 首末知识点跨度占比下限
MAX_POINT_INTERVAL_MEDIAN = 120.0    # 知识点间隔中位数上限（秒；<=0 关闭）
MIN_POINTS_WITH_SLIDE_RATIO = 0.30   # 带屏幕证据的知识点占比下限
MIN_FIGURES = 4                      # 统计图表张数下限
DURATION_MATCH_TOLERANCE = 0.01      # 转写时长与平台分P时长允许偏差（相对值）
EXPECT_DANMAKU = 0                   # 匿名纯净版预期弹幕计数；None 表示不检查
REQUIRE_NO_SUBTITLES = None          # True = 额外要求各分P 无字幕轨/章节；None = 只查字段自洽
MIN_SLICE_WITH_CORE_RATIO = 0.90     # 有核心产出的切片占比下限
MIN_TRANSCRIPT_CHARS = 1000          # 逐字稿/纠错稿的最小体量
MIN_DASHBOARD_TEXT_RATIO = 0.0       # >0 才要求含看板文字页占比（域相关，默认关闭）
CHECK_SLICE_COUNT_FROM_WINDOW = True  # 用 window/overlap 推导切片数并校验
SLIDE_WINDOW_BEFORE = 30             # 知识点与所引 OCR 页的时间窗（秒）
SLIDE_WINDOW_AFTER = 90

BAD_STATUS_PREFIXES = (" M", "M ", "MM", " D", "D ", "R ", "RM", "AM", "AD")


class Report:
    def __init__(self) -> None:
        self.items: list[tuple[str, bool, str]] = []
        self.skipped: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        ok = bool(ok)
        self.items.append((name, ok, detail))
        print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append((name, reason))
        print(f"SKIP  {name}  [{reason}]")

    def note(self, msg: str) -> None:
        print(f"      {msg}")


class Th:
    """阈值：默认值写在脚本顶部，config.verify.* 覆盖。"""

    def __init__(self, cfg) -> None:
        v = cfg.section("verify")

        def num(key, default):
            raw = v.get(key, default)
            return default if raw is None else raw

        self.min_tiled_coverage = float(num("minTiledCoverage", MIN_TILED_COVERAGE))
        self.min_annotated_span = float(num("minAnnotatedSpan", MIN_ANNOTATED_SPAN))
        self.max_point_interval_median = float(
            num("maxPointIntervalMedianSeconds", MAX_POINT_INTERVAL_MEDIAN))
        self.min_points_with_slide_ratio = float(
            num("minPointsWithSlideRatio", MIN_POINTS_WITH_SLIDE_RATIO))
        self.min_figures = int(num("minFigures", MIN_FIGURES))
        self.duration_tolerance = float(num("durationTolerance", DURATION_MATCH_TOLERANCE))
        self.expect_danmaku = num("danmakuExpected", EXPECT_DANMAKU)
        self.require_no_subtitles = num("requireNoSubtitles", REQUIRE_NO_SUBTITLES)
        self.min_slice_with_core_ratio = float(
            num("minSliceWithCoreRatio", MIN_SLICE_WITH_CORE_RATIO))
        self.min_transcript_chars = int(num("minTranscriptChars", MIN_TRANSCRIPT_CHARS))
        self.min_dashboard_text_ratio = float(
            num("minDashboardTextRatio", MIN_DASHBOARD_TEXT_RATIO))
        self.check_slice_count = bool(num("checkSliceCountFromWindow", CHECK_SLICE_COUNT_FROM_WINDOW))
        self.slide_before = float(num("slideWindowBeforeSeconds", SLIDE_WINDOW_BEFORE))
        self.slide_after = float(num("slideWindowAfterSeconds", SLIDE_WINDOW_AFTER))
        self.repo_path = v.get("repoPath") or cfg.get("repoPath")
        self.artifact_markers = [str(m) for m in (v.get("artifactMarkers") or [])]
        rc = v.get("requireCorrections", True)
        self.require_corrections = True if rc is None else bool(rc)


# ---------------------------------------------------------------- 基础工具

def cpath(cfg, name: str) -> Path | None:
    """取配置里的路径；配置里没有这个键就返回 None（不崩）。"""
    try:
        return cfg.path(name)
    except KeyError:
        return None


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def read_csv_lines(path: Path) -> list[str]:
    return [l for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def seg_text(s: dict) -> str:
    for k in ("text", "content", "sentence"):
        v = s.get(k)
        if isinstance(v, str):
            return v
    return ""


def find_transcript(cfg) -> tuple[Path | None, dict | None]:
    """找一个含 segments 的转写 JSON；有 bvid 时优先同名的那个。"""
    d = cpath(cfg, "transcripts")
    if d is None or not d.exists():
        return None, None
    cands: list[tuple[Path, dict]] = []
    for p in sorted(d.glob("*.json")):
        try:
            j = read_json(p)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(j, dict) and isinstance(j.get("segments"), list):
            cands.append((p, j))
    if not cands:
        return None, None
    bvid = str(cfg.media.get("bvid") or "").strip()
    if bvid:
        for p, j in cands:
            if bvid in p.name:
                return p, j
    return cands[0]


# ---------------------------------------------------------------- 转写

def group_transcript(cfg, th: Th, rep: Report):
    path, trans = find_transcript(cfg)
    rep.check("转写：transcripts/*.json 存在且含 segments", trans is not None,
              "" if trans is None else cfg.rel(path))
    if trans is None:
        return None
    segs = trans["segments"]
    rep.check("转写：segments 非空", len(segs) > 0, f"{len(segs)} 段")
    # 只对时间戳字段齐全的段做单调性/时长校验，缺字段单独报出来
    slots = [s for s in segs if num(s.get("start")) is not None and num(s.get("end")) is not None]
    rep.check("转写：每段都有数值 start/end", len(slots) == len(segs),
              f"{len(segs) - len(slots)} 段缺字段")
    rep.check("转写：时间戳升序",
              all(slots[i]["start"] <= slots[i + 1]["start"] for i in range(len(slots) - 1)))
    rep.check("转写：每段 end > start", all(s["end"] > s["start"] for s in slots))
    dur = num(trans.get("duration"))
    if dur is None and slots:
        dur = slots[-1]["end"]
    rep.check("转写：duration 为正数", num(dur) is not None and dur > 0,
              f"{dur}s" if num(dur) is not None else "缺失")
    rep.note(f"转写时长 {dur}s，片段 {len(segs)}，来源 {cfg.rel(path)}")
    return {"path": path, "json": trans, "segs": segs, "dur": dur}


# ---------------------------------------------------------------- 平台数据

def platform_enabled(cfg) -> bool:
    kind = str(cfg.media.get("kind") or "").strip().lower()
    return kind == "bilibili" or bool(str(cfg.media.get("bvid") or "").strip())


def group_platform(cfg, th: Th, rep: Report, tr):
    stats_dir = cpath(cfg, "stats")
    meta_path = (stats_dir / "video-meta.json") if stats_dir else None
    if not platform_enabled(cfg):
        rep.skip("平台：全部平台数据断言",
                 "local 模式（config.media.kind 非 bilibili 且无 bvid），本 run 没有 B 站数据")
        return None
    rep.check("平台：stats/video-meta.json 存在", bool(meta_path and meta_path.exists()),
              "" if meta_path else "config.paths 里没有 stats")
    if not (meta_path and meta_path.exists()):
        return None
    try:
        meta = read_json(meta_path)
    except (json.JSONDecodeError, OSError) as e:
        rep.check("平台：video-meta.json 可解析", False, str(e)[:120])
        return None
    rep.check("平台：collectedAt 存在", bool(meta.get("collectedAt")), str(meta.get("collectedAt"))[:40])

    pages = meta.get("pages") or []
    videos = meta.get("videos")
    rep.check("平台：分P 数 videos 与 pages 长度自洽",
              isinstance(videos, int) and videos == len(pages), f"videos={videos} pages={len(pages)}")

    page_no = int(cfg.media.get("page") or 1)
    rec = next((p for p in pages if p.get("page") == page_no), pages[0] if pages else None)
    if tr and tr["dur"] and rec and num(rec.get("duration")):
        dev = abs(tr["dur"] - rec["duration"]) / rec["duration"]
        rep.check("平台：转写时长与对应分P 时长对账 < 容差",
                  dev < th.duration_tolerance,
                  f"ASR={tr['dur']:.2f}s 平台={rec['duration']}s 偏差={dev:.4%}")
    else:
        rep.skip("平台：转写时长与分P 时长对账", "缺转写时长或分P 记录")

    stat = meta.get("stat") or {}
    danmaku = stat.get("danmaku")
    if th.expect_danmaku is None:
        rep.skip("平台：弹幕计数", "config.verify.danmakuExpected 为 null（不检查）")
    else:
        rep.check("平台：弹幕计数与配置预期一致",
                  num(danmaku) is not None and danmaku == th.expect_danmaku,
                  f"danmaku={danmaku} 预期={th.expect_danmaku}")

    sub_ok = all(num(p.get("subtitleCount")) is not None and num(p.get("viewPointsCount")) is not None
                 for p in pages) and bool(pages)
    rep.check("平台：各分P 字幕轨/章节计数字段齐全", sub_ok,
              f"subtitleCount={[p.get('subtitleCount') for p in pages]}")
    if th.require_no_subtitles is True:
        rep.check("平台：各分P 无字幕轨与章节（按配置要求）",
                  sum(int(p.get("subtitleCount") or 0) for p in pages) == 0
                  and sum(int(p.get("viewPointsCount") or 0) for p in pages) == 0)
    elif th.require_no_subtitles is None:
        rep.skip("平台：无字幕轨/章节要求", "config.verify.requireNoSubtitles 未设，只查字段自洽")

    cc = meta.get("commentCollection")
    rep.check("平台：评论采集状态已声明",
              isinstance(cc, dict) and isinstance(cc.get("collected"), bool),
              f"collected={cc.get('collected') if isinstance(cc, dict) else None}")
    return meta


# ---------------------------------------------------------------- 语言统计

def group_stats(cfg, th: Th, rep: Report, tr):
    stats_dir = cpath(cfg, "stats")
    ls_path = (stats_dir / "language-stats.json") if stats_dir else None
    rep.check("统计：stats/language-stats.json 存在", bool(ls_path and ls_path.exists()),
              "" if ls_path else "config.paths 里没有 stats")
    if not (ls_path and ls_path.exists()):
        return None
    try:
        ls = read_json(ls_path)
    except (json.JSONDecodeError, OSError) as e:
        rep.check("统计：language-stats.json 可解析", False, str(e)[:120])
        return None

    chars = ls.get("chars") or {}
    han, latin, punct = num(chars.get("han")), num(chars.get("latinDigit")), num(chars.get("punct"))
    without = num(chars.get("withoutPunct"))
    withp = num(chars.get("withPunct"))

    if tr:
        han_from_trans = len(re.findall(r"[\u4e00-\u9fff]", "".join(seg_text(s) for s in tr["segs"])))
        rep.check("统计：汉字数与转写逐字重算一致", han == han_from_trans,
                  f"stats={han} 转写重算={han_from_trans}")
    else:
        rep.skip("统计：汉字数与转写一致", "没有可用的转写 JSON")

    rep.check("统计：withoutPunct = han + latinDigit",
              None not in (without, han, latin) and without == han + latin,
              f"{without} vs {han}+{latin}")
    rep.check("统计：withPunct = withoutPunct + punct",
              None not in (withp, without, punct) and withp == without + punct,
              f"{withp} vs {without}+{punct}")

    src = ls.get("source") or {}
    dur = num(src.get("durationSeconds")) or (tr["dur"] if tr else None)
    rate = ls.get("rate") or {}
    hpm = num(rate.get("hanPerMin"))
    if dur and han is not None and hpm is not None:
        expect = han / (dur / 60)
        rep.check("统计：语速与字数/时长自洽", abs(hpm - expect) < 0.02,
                  f"hanPerMin={hpm} 重算={expect:.2f}")
    else:
        rep.skip("统计：语速自洽", "缺 duration 或 hanPerMin")

    speech = ls.get("speech") or {}
    sr, si = num(speech.get("speakingRatio")), num(speech.get("silenceRatio"))
    rep.check("统计：说话占比 + 空白占比 = 1",
              None not in (sr, si) and abs(sr + si - 1) < 1e-6, f"{sr}+{si}")

    pauses = ls.get("pauses") or {}
    buckets = pauses.get("buckets") or {}
    bucket_sum = sum(int(v) for v in buckets.values() if num(v) is not None)
    pause_count = num(pauses.get("count"))
    rep.check("统计：停顿分档之和 = 停顿总数",
              pause_count is not None and bucket_sum == pause_count,
              f"{buckets} 之和={bucket_sum} vs count={pauses.get('count')}")

    rart = ls.get("repetitionArtifact") or {}
    sens = rart.get("sensitivity") or {}
    raw_han = num((sens.get("raw") or {}).get("han"))
    col_han = num((sens.get("collapsed") or {}).get("han"))
    rep_ok = (isinstance(rart.get("segmentsAffected"), int)
              and num(rart.get("charsRemoved")) is not None
              and raw_han is not None and col_han is not None)
    rep.check("统计：复读伪影敏感度对照已执行（raw/collapsed 双读数）", rep_ok,
              f"受影响段={rart.get('segmentsAffected')} 移除={rart.get('charsRemoved')} "
              f"raw.han={raw_han} collapsed.han={col_han}")
    rep.check("统计：折叠只会减少字数（长度方向守恒）",
              rep_ok and col_han <= raw_han, f"collapsed={col_han} <= raw={raw_han}")

    wf = ls.get("wordFrequency") or {}
    rep.check("统计：词频模式已记录", bool(str(wf.get("mode") or "").strip()), str(wf.get("mode"))[:60])

    pm_path = (stats_dir / "per-minute.csv") if stats_dir else None
    if pm_path and pm_path.exists():
        rows = read_csv_lines(pm_path)
        data_rows = max(0, len(rows) - 1)
        if dur:
            expect = math.ceil(dur / 60)
            rep.check("统计：per-minute.csv 行数 = ceil(时长/60)", data_rows == expect,
                      f"{data_rows} vs {expect}（时长 {dur}s）")
        per_minute = ls.get("perMinute")
        if isinstance(per_minute, list):
            rep.check("统计：per-minute.csv 行数与 language-stats.perMinute 一致",
                      data_rows == len(per_minute), f"{data_rows} vs {len(per_minute)}")
    else:
        rep.check("统计：stats/per-minute.csv 存在", False, "文件缺失")

    figs_dir = (stats_dir / "figures") if stats_dir else None
    figs = list(figs_dir.glob("*.png")) if (figs_dir and figs_dir.exists()) else []
    rep.check("统计：图表已生成（>= 下限）", len(figs) >= th.min_figures,
              f"{len(figs)} 张（下限 {th.min_figures}）")
    return ls


# ---------------------------------------------------------------- 纠错稿

def group_corrections(cfg, th: Th, rep: Report):
    stats_dir = cpath(cfg, "stats")
    content_dir = cpath(cfg, "content")
    trans_dir = cpath(cfg, "transcripts")
    cr_path = (stats_dir / "clean-vs-raw.json") if stats_dir else None
    if not (cr_path and cr_path.exists()):
        if not th.require_corrections:
            rep.skip("纠错：全部纠错稿断言",
                     "config.verify.requireCorrections=false 且本 run 没有 clean-vs-raw.json")
            return
        rep.check("纠错：stats/clean-vs-raw.json 存在", False, "文件缺失（纠错阶段未跑或未启用）")
        return
    try:
        cr = read_json(cr_path)
    except (json.JSONDecodeError, OSError) as e:
        rep.check("纠错：clean-vs-raw.json 可解析", False, str(e)[:120])
        return
    raw_n, clean_n = num(cr.get("rawChars")), num(cr.get("cleanChars"))
    delta, expect = num(cr.get("charDelta")), num(cr.get("charDeltaExpected"))
    rep.check("纠错：长度守恒标记为真（证明只替换未改写）", cr.get("lengthConserved") is True,
              f"delta={delta} expected={expect}")
    rep.check("纠错：charDelta = cleanChars - rawChars（重算一致）",
              None not in (delta, clean_n, raw_n) and delta == clean_n - raw_n,
              f"{delta} vs {clean_n}-{raw_n}")
    rep.check("纠错：charDelta 与预期替换量一致", None not in (delta, expect) and delta == expect,
              f"{delta} vs {expect}")

    cj = (content_dir / "corrections.jsonl") if content_dir else None
    if cj and cj.exists():
        n = len(read_jsonl(cj))
        rep.check("纠错：corrections.jsonl 非空", n > 0, f"{n} 条替换")
    else:
        rep.check("纠错：content/corrections.jsonl 存在", False, "文件缺失")

    for pat, label in (("*.clean.md", "纠错稿"), ("*.raw.txt", "raw 逐字稿")):
        files = sorted(trans_dir.glob(pat)) if (trans_dir and trans_dir.exists()) else []
        biggest = max((f.stat().st_size for f in files), default=0)
        rep.check(f"纠错：{label} 已生成且体量达标", biggest > th.min_transcript_chars,
                  f"{len(files)} 个文件，最大 {biggest} 字节（下限 {th.min_transcript_chars}）")


# ---------------------------------------------------------------- 大纲

def group_outline(cfg, th: Th, rep: Report, tr, slides_rows):
    content_dir = cpath(cfg, "content")
    ol_path = (content_dir / "outline.json") if content_dir else None
    rep.check("大纲：content/outline.json 存在", bool(ol_path and ol_path.exists()),
              "" if ol_path else "config.paths 里没有 content")
    if not (ol_path and ol_path.exists()):
        return None
    try:
        ol = read_json(ol_path)
    except (json.JSONDecodeError, OSError) as e:
        rep.check("大纲：outline.json 可解析", False, str(e)[:120])
        return None

    themes = ol.get("themes") if isinstance(ol.get("themes"), list) else []
    dur = num(ol.get("durationSeconds")) or (tr["dur"] if tr else None)
    rep.check("大纲：themes 非空", len(themes) > 0, f"{len(themes)} 个主题")
    rep.check("大纲：problems 为空", (ol.get("problems") or []) == [],
              str(ol.get("problems"))[:200])

    pts, missing_start = [], 0
    for t in themes:
        for p in (t.get("points") or []):
            if num(p.get("startSeconds")) is None:
                missing_start += 1
            else:
                pts.append(p)
    bad_theme_start = sum(1 for t in themes if num(t.get("startSeconds")) is None)
    rep.check("大纲：所有主题与知识点都有数值 startSeconds",
              missing_start == 0 and bad_theme_start == 0,
              f"知识点缺 {missing_start} 条，主题缺 {bad_theme_start} 个")

    st = ol.get("stats") or {}
    rep.check("大纲：pointCount 与 stats 一致", st.get("pointCount") == len(pts),
              f"{len(pts)} vs stats={st.get('pointCount')}")
    rep.check("大纲：themeCount 与 stats 一致", st.get("themeCount") == len(themes),
              f"{len(themes)} vs stats={st.get('themeCount')}")

    if dur:
        out_of_range = [p for p in pts if not (0 <= p["startSeconds"] <= dur)]
        out_of_range += [t for t in themes if num(t.get("startSeconds")) is not None
                         and not (0 <= t["startSeconds"] <= dur)]
        rep.check("大纲：所有时间戳落在 [0, duration]", not out_of_range,
                  f"越界 {len(out_of_range)} 条（duration={dur}s）")
    else:
        rep.skip("大纲：时间戳落在 [0, duration]", "缺 duration")

    theme_starts = [num(t.get("startSeconds")) for t in themes]
    rep.check("大纲：主题按起点升序",
              all(v is not None for v in theme_starts)
              and all(theme_starts[i] <= theme_starts[i + 1] for i in range(len(theme_starts) - 1)))
    inner_ok = True
    for t in themes:
        ts = [num(p.get("startSeconds")) for p in (t.get("points") or [])]
        if any(v is None for v in ts) or any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
            inner_ok = False
            break
    rep.check("大纲：主题内知识点升序", inner_ok)
    no_excerpt = sum(1 for p in pts if not str(p.get("excerpt") or "").strip())
    rep.check("大纲：每条知识点都有 excerpt", no_excerpt == 0, f"缺失 {no_excerpt} 条")

    tiled = num(st.get("tiledCoverageRatio"))
    rep.check("大纲：主题铺满覆盖率 >= 下限",
              tiled is not None and tiled >= th.min_tiled_coverage,
              f"{tiled}（下限 {th.min_tiled_coverage}）")
    span = num(st.get("annotatedSpanRatio"))
    rep.check("大纲：首末知识点跨度 >= 下限",
              span is not None and span >= th.min_annotated_span,
              f"{span}（下限 {th.min_annotated_span}）")
    median = num((st.get("pointIntervalSeconds") or {}).get("median"))
    if th.max_point_interval_median > 0:
        rep.check("大纲：知识点间隔中位数 <= 上限",
                  median is not None and median <= th.max_point_interval_median,
                  f"{median}s（上限 {th.max_point_interval_median}s）")
    else:
        rep.skip("大纲：知识点间隔中位数", "config.verify.maxPointIntervalMedianSeconds <= 0")

    # 空档审计：不仅要「已执行」，还要 alert 列表与计数自洽
    gap_alerts = st.get("gapAlerts")
    rep.check("大纲：空档审计已执行且计数自洽",
              "gapAlertCount" in st and isinstance(gap_alerts, list)
              and st.get("gapAlertCount") == len(gap_alerts),
              f">={st.get('gapAlertThresholdSeconds')}s 空档 {st.get('gapAlertCount')} 处，"
              f"gapAlerts 长度 {len(gap_alerts) if isinstance(gap_alerts, list) else 'N/A'}")

    psr = num(st.get("pointsWithSlideRatio"))
    if slides_rows:
        rep.check("大纲：带屏幕证据的知识点占比 >= 下限",
                  psr is not None and psr >= th.min_points_with_slide_ratio,
                  f"{psr}（下限 {th.min_points_with_slide_ratio}）")
    else:
        rep.skip("大纲：带屏幕证据的知识点占比", "本次 run 没有屏幕 OCR 产物")

    # 切片解析自洽：inCore <= parsed；切片数与 window/overlap 推导一致
    counts = (ol.get("mergeRule") or {}).get("sliceParsedCounts") or {}

    def core_of(v) -> int:
        return int(num((v or {}).get("inCore")) or 0)

    if counts:
        bad = [k for k, v in counts.items()
               if core_of(v) > int(num((v or {}).get("parsed")) or 0)]
        rep.check("大纲：每片 inCore <= parsed", not bad, f"异常 {bad[:5]}")
        with_core = sum(1 for v in counts.values() if core_of(v) > 0)
        ratio = with_core / len(counts)
        rep.check("大纲：有核心产出的切片占比 >= 下限", ratio >= th.min_slice_with_core_ratio,
                  f"{with_core}/{len(counts)} = {ratio:.3f}（下限 {th.min_slice_with_core_ratio}）")
        mr = ol.get("mergeRule") or {}
        win, ov = num(mr.get("windowSeconds")), num(mr.get("overlapSeconds"))
        if th.check_slice_count and dur and win and ov and win > ov:
            expect = 1 + math.ceil((dur - win) / (win - ov))
            rep.check("大纲：切片数与 window/overlap 推导一致", len(counts) == expect,
                      f"{len(counts)} vs 期望 {expect}（window={win:g} overlap={ov:g}）")
        else:
            rep.skip("大纲：切片数与 window/overlap 推导", "缺 mergeRule 窗口参数或 duration")
    else:
        rep.check("大纲：mergeRule.sliceParsedCounts 存在", False, "缺失或为空")

    olcsv = (content_dir / "outline-points.csv") if content_dir else None
    if olcsv and olcsv.exists():
        n = max(0, len(read_csv_lines(olcsv)) - 1)
        rep.check("大纲：outline-points.csv 行数 = 知识点数 + 表头", n == len(pts),
                  f"{n} vs {len(pts)}")
    else:
        rep.check("大纲：content/outline-points.csv 存在", False, "文件缺失")

    # 交叉校验：知识点引用的 OCR 页必须真实存在，且时间窗自洽
    if slides_rows:
        by_idx = {r.get("idx"): r for r in slides_rows}
        cited = [p for p in pts if p.get("slidePageIdx") is not None]

        def window_ok(p) -> bool:
            row = by_idx.get(p["slidePageIdx"])
            t = num(row.get("timeSeconds")) if row else None
            return t is not None and (p["startSeconds"] - th.slide_before
                                      <= t <= p["startSeconds"] + th.slide_after)

        if cited:
            bad = [p["startSeconds"] for p in cited if not window_ok(p)]
            rep.check("大纲：slidePageIdx 指向存在的 OCR 页且时间窗自洽",
                      not bad, f"异常 {len(bad)} 条 / 引用 {len(cited)} 条")
        else:
            rep.skip("大纲：slidePageIdx 时间窗自洽", "没有知识点标注屏幕页索引")
    else:
        rep.skip("大纲：slidePageIdx 时间窗自洽", "本次 run 没有屏幕 OCR 产物")
    return ol


# ---------------------------------------------------------------- 屏幕 OCR

def group_slides(cfg, th: Th, rep: Report, tr):
    if cfg.slides.get("enabled") is False:
        rep.skip("屏幕：全部 OCR 断言", "config.slides.enabled = false")
        return None
    slides_dir = cpath(cfg, "slides")
    sj = (slides_dir / "slides.jsonl") if slides_dir else None
    rep.check("屏幕：slides/slides.jsonl 存在", bool(sj and sj.exists()),
              "" if sj else "config.paths 里没有 slides")
    if not (sj and sj.exists()):
        return None
    rows = read_jsonl(sj)
    q_path = slides_dir / "ocr-quality.json"
    if not q_path.exists():
        rep.check("屏幕：slides/ocr-quality.json 存在", False, "文件缺失")
        return None
    q = read_json(q_path)
    rep.check("屏幕：slides.jsonl 行数 = ocr-quality.ocrPages", len(rows) == q.get("ocrPages"),
              f"{len(rows)} vs {q.get('ocrPages')}")

    times = [num(r.get("timeSeconds")) for r in rows]
    rep.check("屏幕：每行都有数值 timeSeconds 且升序",
              all(t is not None for t in times)
              and all(times[i] <= times[i + 1] for i in range(len(times) - 1)),
              f"{sum(1 for t in times if t is None)} 行缺时间戳")
    dur = (num(q.get("durationSeconds")) or (tr["dur"] if tr else None))
    if dur:
        bad = [t for t in times if t is None or not (0 <= t <= dur)]
        rep.check("屏幕：时间戳落在 [0, duration]", not bad, f"越界 {len(bad)} 条（duration={dur}s）")
    else:
        rep.skip("屏幕：时间戳落在 [0, duration]", "缺 duration")

    bad_hash = [r.get("idx") for r in rows
                if not re.fullmatch(r"[0-9a-f]{8,16}", str(r.get("dhash") or ""))]
    rep.check("屏幕：每行都有合法 dhash", not bad_hash, f"异常 {len(bad_hash)} 行")
    bad_kinds = [r.get("idx") for r in rows if not isinstance(r.get("blockKinds"), dict)]
    rep.check("屏幕：每行都有 blockKinds", not bad_kinds, f"异常 {len(bad_kinds)} 行")

    def kinds_ok(r) -> bool:
        kinds = r.get("blockKinds")
        if not isinstance(kinds, dict) or any(num(v) is None for v in kinds.values()):
            return False
        return sum(int(v) for v in kinds.values()) == len(r.get("blocks") or [])

    bad_sum = [r.get("idx") for r in rows if not kinds_ok(r)]
    rep.check("屏幕：blockKinds 分类完备（各档之和 = 文本块数）", not bad_sum,
              f"异常 {len(bad_sum)} 行")

    missing_img = [r.get("idx") for r in rows
                   if not (slides_dir / str(r.get("imagePath") or "")).is_file()]
    rep.check("屏幕：每行引用的页面图都存在", not missing_img, f"缺图 {len(missing_img)} 张")

    pages_dir = slides_dir / "pages"
    page_files = list(pages_dir.glob("*.jpg")) if pages_dir.exists() else []
    kept = q.get("dedupKeptPages")
    rep.check("屏幕：pages/ 图片数 = dedupKeptPages", kept is not None and len(page_files) == kept,
              f"{len(page_files)} vs {kept}")

    bucket = num(q.get("ocrBucketSeconds"))
    if bucket and rows and all(t is not None for t in times):
        buckets = {int(t // bucket) for t in times}
        rep.check("屏幕：OCR 分层抽样自洽（每个时间桶恰好一页）", len(buckets) == len(rows),
                  f"桶数 {len(buckets)} vs 页数 {len(rows)}（桶宽 {bucket:g}s）")
        rep.check("屏幕：OCR 页数不超过去重页数", len(rows) <= int(kept or 0),
                  f"ocr={len(rows)} kept={kept}")
    else:
        rep.skip("屏幕：分层抽样自洽", "缺 ocrBucketSeconds")

    verdict = q.get("verdict")
    rep.check("屏幕：verdict 已给出且非 unusable", verdict in ("usable", "degraded", "ok"),
              str(verdict))

    st_path = slides_dir / "屏幕状态时间轴.json"
    if st_path.exists():
        sto = read_json(st_path)
        states = sto.get("states") or []
        rep.check("屏幕：状态数 stateCount 与 states 长度自洽",
                  sto.get("stateCount") == len(states), f"{sto.get('stateCount')} vs {len(states)}")
        rep.check("屏幕：各状态 pageCount 之和 = OCR 页数",
                  sum(int(s.get("pageCount") or 0) for s in states) == len(rows),
                  f"{sum(int(s.get('pageCount') or 0) for s in states)} vs {len(rows)}")
        keys = [s.get("stateKey") for s in states]
        rep.check("屏幕：stateKey 无重复", len(keys) == len(set(keys)),
                  f"{len(keys)} 个状态，去重后 {len(set(keys))}")
        empty_states = sum(1 for k in keys if k == "EMPTY")
        rep.check("屏幕：EMPTY 状态已合并（至多 1 个）", empty_states <= 1,
                  f"EMPTY 状态 {empty_states} 个（>1 说明无内容页没合并）")
        firsts = [num(s.get("firstTimeSeconds")) for s in states]
        rep.check("屏幕：状态按首现时间升序",
                  all(f is not None for f in firsts)
                  and all(firsts[i] <= firsts[i + 1] for i in range(len(firsts) - 1)))
    else:
        rep.check("屏幕：slides/屏幕状态时间轴.json 存在", False, "文件缺失")

    kt_path = slides_dir / "屏幕关键文本.json"
    if kt_path.exists():
        kt = read_json(kt_path)
        items = kt.get("items") or []
        rep.check("屏幕：关键文本 count 与 items 长度自洽",
                  kt.get("count") == len(items), f"{kt.get('count')} vs {len(items)}")
    else:
        rep.check("屏幕：slides/屏幕关键文本.json 存在", False, "文件缺失")

    ratio = num(q.get("pagesWithDashboardTextRatio"))
    if th.min_dashboard_text_ratio > 0:
        rep.check("屏幕：含看板文字页占比 >= 下限",
                  ratio is not None and ratio >= th.min_dashboard_text_ratio,
                  f"{ratio}（下限 {th.min_dashboard_text_ratio}）")
    else:
        rep.skip("屏幕：含看板文字页占比", "域相关阈值未启用（config.verify.minDashboardTextRatio=0）")
    return rows


# ---------------------------------------------------------------- 仓库零改动

def git(repo: Path, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def group_repo(cfg, th: Th, rep: Report):
    """仓库零改动：判据是「当前脏文件集合 ⊆ 开工基线集合」，不是「状态逐行相同」。

    原因（实跑教训）：仓库里可能有**并行工作流**在推进提交，把开工时已脏的文件提交掉了；
    那样「逐行相同」必然失败，但并不能证明我改过仓库。真正要证明的是：
    (1) 我没有新增任何被改动的已跟踪文件；(2) 本次产物路径没有进到仓库状态里。
    另外未跟踪文件（?? 新文件）不计入判定，只做提示 —— 并行工作流新增文件属正常。
    """
    repo_raw = th.repo_path
    if not repo_raw:
        rep.skip("仓库：零改动检查",
                 "未配置 config.verify.repoPath（本脚本不写死任何仓库路径）")
        return
    repo = Path(str(repo_raw))
    if not repo.is_absolute():
        repo = cfg.root / repo
    if not (repo / ".git").exists():
        rep.check("仓库：repoPath 指向一个 git 工作区", False, str(repo))
        return
    stats_dir = cpath(cfg, "stats")
    baseline_path = (stats_dir / "repo-baseline.txt") if stats_dir else None
    rep.check("仓库：开工基线 stats/repo-baseline.txt 存在",
              bool(baseline_path and baseline_path.exists()), str(baseline_path))
    if not (baseline_path and baseline_path.exists()):
        return

    base_txt = baseline_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    base_lines = {l for l in base_txt.splitlines() if l.strip() and not l.startswith("##")}
    now_txt = git(repo, "status", "--short", "--branch").replace("\r\n", "\n")
    now_lines = {l for l in now_txt.splitlines() if l.strip() and not l.startswith("##")}
    if stats_dir and stats_dir.exists():
        (stats_dir / "repo-status-after.txt").write_text(now_txt, encoding="utf-8")

    added = sorted(now_lines - base_lines)
    newly_dirty = [l for l in added if l.startswith(BAD_STATUS_PREFIXES)]
    rep.check("仓库：当前脏的已跟踪文件 ⊆ 开工基线（我未改任何仓库文件）",
              not newly_dirty, f"新增 {len(newly_dirty)} 条：{newly_dirty[:5]}")
    untracked_new = [l for l in added if l.startswith("??")]
    rep.note(f"开工基线 {len(base_lines)} 条 -> 现在 {len(now_lines)} 条；"
             f"新出现的未跟踪文件 {len(untracked_new)} 条（不计入判定）")

    markers = th.artifact_markers or [cfg.root.name, str(cfg.get("name") or "")]
    markers = [m for m in markers if len(m) >= 3]
    leaked = sorted(l for l in now_lines for m in markers if m and m in l)
    rep.check("仓库：本次产物路径未出现在仓库状态里", not leaked,
              f"标记 {markers}；命中 {leaked[:5]}")

    head_now = git(repo, "rev-parse", "HEAD").strip()
    head_subject = git(repo, "log", "-1", "--format=%s").strip()
    head_path = (stats_dir / "repo-head.json") if stats_dir else None
    head_start = None
    if head_path and head_path.exists():
        head_start = (read_json(head_path) or {}).get("headAtStart")
    rep.check("仓库：HEAD 可读且变更已记录", bool(head_now),
              f"{str(head_start)[:7]} -> {head_now[:7]} 「{head_subject}」")
    if head_path and stats_dir.exists():
        head_path.write_text(json.dumps({
            "headAtStart": head_start,
            "headAtVerify": head_now,
            "headSubject": head_subject,
            "explanation": "判据是「当前脏文件集合 ⊆ 开工基线集合」；并行工作流可能在工作期间推进提交，"
                           "那会把开工时已脏的文件从状态里消掉，不影响本判据。",
            "baselineLineCount": len(base_lines),
            "nowLineCount": len(now_lines),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 入口

def main() -> int:
    cfg, rest = load(argv=sys.argv[1:])
    ap = argparse.ArgumentParser(prog="verify.py")
    ap.add_argument("--repo", default=None, help="覆盖 config.verify.repoPath")
    args = ap.parse_args(rest)
    th = Th(cfg)
    if args.repo:
        th.repo_path = args.repo
    rep = Report()

    print(f"config : {cfg.config_path}")
    print(f"runRoot: {cfg.root}")
    print(f"title  : {cfg.title}")
    print("-" * 72)

    tr = group_transcript(cfg, th, rep)
    print("-" * 72)
    group_platform(cfg, th, rep, tr)
    print("-" * 72)
    group_stats(cfg, th, rep, tr)
    print("-" * 72)
    group_corrections(cfg, th, rep)
    print("-" * 72)
    # 先在独立函数里决定屏幕组，拿到 rows 供大纲做交叉校验
    slides_rows = group_slides(cfg, th, rep, tr)
    print("-" * 72)
    group_outline(cfg, th, rep, tr, slides_rows)
    print("-" * 72)
    group_repo(cfg, th, rep)

    fails = [i for i in rep.items if not i[1]]
    print()
    print(f"总计 {len(rep.items)} 项断言，PASS {len(rep.items) - len(fails)}，"
          f"FAIL {len(fails)}，SKIP {len(rep.skipped)}")
    for name, _, detail in fails:
        print(f"  FAIL {name}" + (f"  [{detail}]" if detail else ""))
    if rep.skipped:
        print("跳过项（平台/可选阶段未启用，不算失败）：")
        for name, reason in rep.skipped:
            print(f"  SKIP {name}  [{reason}]")
    if not rep.items:
        print("没有执行任何断言：请先用 --config 指向本次 run 的 config.json。")
        return 2
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
