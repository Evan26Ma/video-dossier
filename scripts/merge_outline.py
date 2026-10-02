"""把各切片子代理产出的大纲合并成两级大纲，并挂上屏幕 OCR 证据。

输入（全部走配置）：
- 子代理产出：`{content}/_outline/slice-*.json`（每片一份，schema 不完全统一）
- 切片窗口：`{content}/_slices/index.json`（由切片脚本产出，记录了本次实际窗口，优先采用）；
  缺失时按 `config.outline` 的 windowSeconds/overlapSeconds 推导，两种情况都写进 problems/mergeRule 便于核对
- 屏幕证据：`{slides}/slides.jsonl` 的 dashboardText
- 标签白名单：`profiles/<profile>.json` 的 outlineTags；类型白名单：outlineKinds
- 空档阈值：`config.outline.gapAlertSeconds`

合并口径（关键）：
- 切片窗口相互重叠 `overlapSeconds`（实跑取 150s）。为消除重复，只保留落在该片「核心区」的条目：
  slice-01 核心区 = [0, end]；其余 = [start+overlap, end]。全片任一时刻因此**恰好**归属一个核心区。
- 主题（一级）若在核心区内没有任何条目，则整条丢弃（其内容由相邻片覆盖）。
- 知识点（二级）按 (四舍五入到 2 秒的起点, 标题前 8 字) 全局去重。
- 主题起止时间重算为该主题核心区内条目的最小/最大时间，保证与条目自洽。
- slideExcerpt：取时间落在 [起点-30s, 起点+90s] 内最接近的 OCR 页面里被判定为「看板」的文本块。

容错：子代理并不总是守 schema（有人用 timestamp/time/startSec 代替 startSeconds、主题缺 endSeconds），
本脚本按多键名兼容解析，并统计「需要规范化」的条目数写进产物，便于口径追溯。

产物：`{content}/outline.json`、`{content}/outline.md`、`{content}/outline-points.csv`
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from vd_config import load

TIME_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")

# profile 里没写时的兜底白名单（通用域口径，不含任何学科内容）
DEFAULT_KINDS = ["导学", "新授", "例题", "答疑", "互动", "收尾", "其他"]
DEFAULT_TAGS = ["知识点", "方法", "例题", "答疑", "互动", "事务"]

# 多键名兼容：子代理用过的所有别名
POINT_START_KEYS = ("startSeconds", "startSec", "timestamp", "startTime", "time", "start", "sec", "seconds", "t")
THEME_START_KEYS = ("startSeconds", "startSec", "timestamp", "startTime", "time", "start")
THEME_END_KEYS = ("endSeconds", "endSec", "endTime", "end")


def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def parse_time(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    m = TIME_RE.match(s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    try:
        return float(s)
    except ValueError:
        return None


def first_time(d, keys):
    for k in keys:
        t = parse_time(d.get(k))
        if t is not None:
            return t
    return None


def norm_title(s: str) -> str:
    return re.sub(r"[\s【】（）()、，,。.．:：·\-—「」“”\"'|/]", "", s or "")


def pick_text(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def resolve_transcript(cfg) -> Path | None:
    """解析本次运行的转写 json；一个候选都不存在时返回 None。

    命名契约与 run_asr.py / build_slices.py 一致：configured localAudio 优先取它的文件名 stem，
    否则 `<bvid>_p<page>`；也可用 config.media.transcript 直接指定（相对路径按配置根解析）。
    """
    tdir = cfg.path("transcripts")
    media = cfg.media
    explicit = media.get("transcript") or cfg.get("transcript")
    if explicit:
        p = Path(str(explicit))
        return p if p.is_absolute() else (cfg.root / p)
    for stem in transcript_stems(cfg):
        cand = tdir / f"{stem}.json"
        if cand.exists():
            return cand
    files = sorted(tdir.glob("*.json")) if tdir.exists() else []
    if len(files) == 1:
        return files[0]
    if not files:
        return None
    raise SystemExit(
        f"{tdir} 下有多个转写 json（{'、'.join(p.name for p in files[:8])}）；"
        f"请在 config.media.transcript 里指定本次使用哪一个。"
    )


def transcript_stems(cfg) -> list[str]:
    """按 run_asr.py 的命名口径列出候选文件名（不含扩展名）。"""
    media = cfg.media
    stems: list[str] = []
    for k in ("localAudio", "localVideo", "audioFile", "videoFile"):
        if media.get(k):
            s = Path(str(media[k])).stem
            if s and s not in stems:
                stems.append(s)
    bvid = str(media.get("bvid") or "").strip()
    if bvid:
        try:
            page = int(float(media.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        s = f"{bvid}_p{page}"
        if s not in stems:
            stems.append(s)
    return stems


def derive_windows(duration: float, window: float, overlap: float) -> list[tuple[float, float]]:
    """复刻切片脚本的切分口径：窗口长 window，每次前进 window-overlap。"""
    if overlap >= window:
        raise SystemExit(f"config.outline.overlapSeconds({overlap}) 必须小于 windowSeconds({window})，否则窗口无法推进。")
    out = []
    start = 0.0
    while start < duration:
        end = min(start + window, duration)
        out.append((start, end))
        if end >= duration:
            break
        start = end - overlap
    return out


def source_desc(cfg) -> str:
    media = cfg.media
    bvid = str(media.get("bvid") or "").strip()
    if bvid:
        return f"{bvid} P{media.get('page') or 1}"
    for k in ("localVideo", "localAudio"):
        if media.get(k):
            return Path(str(media[k])).name
    return cfg.title


def main() -> None:
    cfg, _ = load(argv=sys.argv[1:])
    profile = cfg.profile()
    kinds = list(profile.get("outlineKinds") or DEFAULT_KINDS)
    tags_white = list(profile.get("outlineTags") or DEFAULT_TAGS)

    content_dir = cfg.path("content")
    outdir = content_dir / "_outline"
    slices_index = content_dir / "_slices" / "index.json"
    outline_path = content_dir / "outline.json"
    outline_md = content_dir / "outline.md"
    points_csv = content_dir / "outline-points.csv"
    ocr_raw = cfg.path("slides") / "slides.jsonl"

    # 窗口参数口径来自 config.outline；若切片 index 记录了本次实际使用的窗口，以 index 为准
    # （核心区必须与真实切片一致，否则会把重叠区条目算重或算丢），不一致时写进 problems。
    cfg_window = float(cfg.outline.get("windowSeconds") or 600.0)
    cfg_overlap = float(cfg.outline.get("overlapSeconds") or 150.0)
    window, overlap = cfg_window, cfg_overlap
    gap_alert = float(cfg.outline.get("gapAlertSeconds") or 420.0)

    norm_stats = {"pointsNormalized": 0, "themesNeedingEnd": 0, "pointsWithThemeTags": 0,
                  "invalidKind": 0, "missingExcerpt": 0}
    problems: list[str] = []
    notes: list[str] = []

    # ---- 窗口与时长 ----
    index = json.loads(slices_index.read_text(encoding="utf-8")) if slices_index.exists() else None
    windows: dict[str, dict] = {}
    duration = 0.0
    window_source = ""
    if index is not None:
        for s in index.get("slices") or []:
            windows[str(s["id"])] = {"startSeconds": float(s["startSeconds"]), "endSeconds": float(s["endSeconds"])}
        duration = float(index.get("duration") or 0.0)
        window_source = cfg.rel(slices_index)
        idx_window = index.get("windowSeconds")
        idx_overlap = index.get("overlapSeconds")
        if idx_window is not None:
            window = float(idx_window)
        if idx_overlap is not None:
            overlap = float(idx_overlap)
        if idx_window is not None and abs(float(idx_window) - cfg_window) > 0.5:
            problems.append(f"切片 index 的 windowSeconds={idx_window} 与 config.outline={cfg_window:g} 不一致，"
                            f"合并按 index 的实际窗口执行")
        if idx_overlap is not None and abs(float(idx_overlap) - cfg_overlap) > 0.5:
            problems.append(f"切片 index 的 overlapSeconds={idx_overlap} 与 config.outline={cfg_overlap:g} 不一致，"
                            f"核心区按 index 的实际重叠执行")

    if not windows:
        trans = resolve_transcript(cfg)
        if trans is None or not trans.exists():
            raise SystemExit(
                f"找不到切片窗口（{slices_index}）也找不到转写 json；"
                f"请先产出 {cfg.rel(slices_index)} 或在 config 里指定 media.transcript。"
            )
        duration = float(json.loads(trans.read_text(encoding="utf-8")).get("duration") or 0.0)
        ids = [p.stem for p in sorted(outdir.glob("slice-*.json"))] if outdir.exists() else []
        derived = derive_windows(duration, window, overlap)
        if ids and len(ids) != len(derived):
            problems.append(f"找到 {len(ids)} 份切片产出，按 config.outline 推导出 {len(derived)} 个窗口，"
                            f"数量不一致，请核对窗口参数")
        if not ids:
            problems.append(f"{cfg.rel(outdir)} 下没有 slice-*.json，合并结果将为空")
        for i, (a, b) in enumerate(derived):
            sid = ids[i] if i < len(ids) else f"slice-{i + 1:02d}"
            windows[sid] = {"startSeconds": a, "endSeconds": b}
        window_source = "config.outline 推导"
        problems.append("未找到切片 index，窗口按 config.outline 推导，请确认与实际切片一致")

    if duration <= 0:
        raise SystemExit("切片 index 的 duration 缺失或为 0，无法计算核心区与覆盖率。")

    # ---- 逐片解析 ----
    themes_all = []
    slice_summaries: dict[str, str] = {}
    parsed_counts: dict[str, dict] = {}
    for sid in sorted(windows):
        w = windows[sid]
        f = outdir / f"{sid}.json"
        if not f.exists():
            problems.append(f"{sid}: 文件缺失")
            continue
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{sid}: JSON 解析失败 {exc!r}")
            continue
        if isinstance(obj, list):
            obj = {"themes": obj, "windowSummary": ""}
        slice_summaries[sid] = pick_text(obj, "windowSummary", "summary")
        n_parsed = 0
        n_core = 0
        core_start = 0.0 if w["startSeconds"] <= 0 else w["startSeconds"] + overlap
        core_end = w["endSeconds"]
        for th in obj.get("themes", []) or []:
            if not isinstance(th, dict):
                continue
            theme_tags = [t for t in (th.get("tags") or []) if t in tags_white]
            kind = th.get("kind")
            if kind not in kinds:
                kind = None
            pts = []
            for p in th.get("points", []) or []:
                if isinstance(p, str):
                    p = {"title": p}
                if not isinstance(p, dict):
                    continue
                n_parsed += 1
                if "startSeconds" not in p:
                    norm_stats["pointsNormalized"] += 1
                st = first_time(p, POINT_START_KEYS)
                if st is None or not (core_start <= st <= core_end) or not (0 <= st <= duration):
                    continue
                ptitle = pick_text(p, "title", "name", "point", "note", "text", "desc")
                excerpt = pick_text(p, "excerpt", "quote", "evidence", "text", "source", "raw")
                if not excerpt:
                    norm_stats["missingExcerpt"] += 1
                ptags = [t for t in (p.get("tags") or []) if t in tags_white]
                if not ptags and theme_tags:
                    ptags = list(theme_tags)
                    norm_stats["pointsWithThemeTags"] += 1
                pk = p.get("kind")
                if kind is None and pk in kinds:
                    kind = pk
                pts.append({"startSeconds": st, "title": ptitle, "excerpt": excerpt, "tags": ptags})
            if not pts:
                continue
            n_core += len(pts)
            start = first_time(th, THEME_START_KEYS)
            end = first_time(th, THEME_END_KEYS)
            if end is None:
                norm_stats["themesNeedingEnd"] += 1
            if kind is None:
                kind = "其他" if "其他" in kinds else (kinds[-1] if kinds else "其他")
                norm_stats["invalidKind"] += 1
            themes_all.append({
                "title": pick_text(th, "title", "name") or "(无标题)",
                "kind": kind,
                "summary": pick_text(th, "summary", "desc", "description"),
                "points": pts,
                "slice": sid,
                "givenStart": start,
                "givenEnd": end,
            })

        parsed_counts[sid] = {"parsed": n_parsed, "inCore": n_core}
        if n_core == 0:
            problems.append(f"{sid}: 核心区内 0 条知识点（原始解析 {n_parsed} 条）")

    # ---- 全局去重知识点 ----
    seen = set()
    dropped = 0
    for th in themes_all:
        uniq = []
        for p in th["points"]:
            key = (round(p["startSeconds"] / 2) * 2, norm_title(p["title"])[:8])
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            uniq.append(p)
        th["points"] = sorted(uniq, key=lambda x: x["startSeconds"])
    themes_all = [th for th in themes_all if th["points"]]
    themes_all.sort(key=lambda th: th["points"][0]["startSeconds"])
    for th in themes_all:
        th["startSeconds"] = th["points"][0]["startSeconds"]
        last = th["points"][-1]["startSeconds"]
        th["endSeconds"] = max(last, min(th["givenEnd"] or 0, duration)) if th["givenEnd"] else last
        th["pointCount"] = len(th["points"])
        th["lastPointSeconds"] = last

    # 一级主题的结束时间：用「下一个主题的起点」铺满时间轴（子代理普遍没给 endSeconds，
    # 这是口径，不是实测值）；最后一个主题用「最后一个知识点 + 中位知识点间隔」封口。
    all_points_sorted = sorted((p["startSeconds"], th["slice"]) for th in themes_all for p in th["points"])
    gaps_between_points = [b[0] - a[0] for a, b in zip(all_points_sorted, all_points_sorted[1:])]
    median_gap = sorted(gaps_between_points)[len(gaps_between_points) // 2] if gaps_between_points else 60.0
    for i, th in enumerate(themes_all):
        th["lastPointSeconds"] = th["points"][-1]["startSeconds"]
        th["endSeconds"] = (themes_all[i + 1]["startSeconds"] if i + 1 < len(themes_all)
                            else min(duration, th["lastPointSeconds"] + median_gap))
        if th["endSeconds"] < th["lastPointSeconds"]:
            th["endSeconds"] = th["lastPointSeconds"]

    # 空档审计（阈值来自 config.outline.gapAlertSeconds）
    gap_alerts = []
    for (ts, _), (te, _) in zip(all_points_sorted, all_points_sorted[1:]):
        if te - ts > gap_alert:
            gap_alerts.append({"fromSeconds": ts, "toSeconds": te, "durationSeconds": round(te - ts, 1),
                               "fromText": hhmmss(ts), "toText": hhmmss(te)})
    first_point = all_points_sorted[0][0] if all_points_sorted else 0.0
    last_point = all_points_sorted[-1][0] if all_points_sorted else 0.0

    # ---- 屏幕证据 ----
    ocr_by_time = []
    if ocr_raw.exists():
        for line in ocr_raw.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            dash = r.get("dashboardText") or ""
            if dash.strip():
                ocr_by_time.append((r["timeSeconds"], r.get("idx"), r.get("timeText"), dash))
        ocr_by_time.sort()
    else:
        # 画面层是可选阶段：显式关掉它（slides.enabled=false）就不该算问题，
        # 只有「配了要跑画面层但产物不在」才值得报警。
        if cfg.slides.get("enabled", True):
            problems.append(f"屏幕证据缺失：{cfg.rel(ocr_raw)} 不存在，slideExcerpt 全为空")
        else:
            notes.append(f"画面层已关闭（slides.enabled=false），slideExcerpt 全为空")

    def slide_for(t):
        best = None
        for ts, idx, tt, dash in ocr_by_time:
            if t - 30 <= ts <= t + 90:
                d = abs(ts - t)
                if best is None or d < best[0]:
                    best = (d, idx, tt, dash)
        return best

    attached = 0
    for th in themes_all:
        for p in th["points"]:
            hit = slide_for(p["startSeconds"])
            if hit:
                _, idx, tt, dash = hit
                p["slideExcerpt"] = dash[:400]
                p["slidePageIdx"] = idx
                p["slideTimeText"] = tt
                attached += 1
            else:
                p["slideExcerpt"] = ""
                p["slidePageIdx"] = None
                p["slideTimeText"] = None
            p["source"] = "voice+slide" if p["slideExcerpt"] else "voice"

    # ---- 覆盖率 ----
    intervals = sorted((th["startSeconds"], th["endSeconds"]) for th in themes_all)
    merged = []
    for a, b in intervals:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    covered = sum(b - a for a, b in merged)
    kind_seconds = {}
    for th in themes_all:
        kind_seconds[th["kind"]] = kind_seconds.get(th["kind"], 0) + (th["endSeconds"] - th["startSeconds"])

    kept_points = sum(th["pointCount"] for th in themes_all)
    media = cfg.media
    out = {
        "schemaVersion": 1,
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runName": cfg.get("name") or cfg.title,
        "bvid": media.get("bvid") or None,
        "page": media.get("page"),
        "profile": cfg.section("video").get("profile") or "generic",
        "basis": "raw ASR（未纠错）为主；slideExcerpt 为屏幕 OCR（启发式分类，仅辅助）",
        "durationSeconds": duration,
        "mergeRule": {
            "windowSeconds": window,
            "overlapSeconds": overlap,
            "configWindowSeconds": cfg_window,
            "configOverlapSeconds": cfg_overlap,
            "windowSource": window_source,
            "coreRule": f"slice-01 核心区 [0,end]；其余 [start+{overlap:g},end]，重叠区条目只归一片，天然去重",
            "pointDedupe": "(四舍五入到 2s 的起点, 标题前 8 字) 全局唯一",
            "themeDrop": "核心区内无条目的主题整条丢弃",
            "boundaryRule": "边界取该条目第一句话所在行的行首秒数（宁晚不早）",
            "tagWhitelist": tags_white,
            "kindWhitelist": kinds,
            "schemaTolerance": norm_stats,
            "sliceParsedCounts": parsed_counts,
        },
        "stats": {
            "themeCount": len(themes_all),
            "pointCount": kept_points,
            "droppedDuplicatePoints": dropped,
            "kindSeconds": kind_seconds,
            "tiledCoverageSeconds": round(covered, 1),
            "tiledCoverageRatio": round(covered / duration, 4),
            "tiledCoverageNote": "主题按「下一个主题起点」铺满时间轴后的并集占比；子代理未提供 endSeconds，故这是口径值而非实测值。",
            "annotatedSpanSeconds": round(last_point - first_point, 1),
            "annotatedSpanRatio": round((last_point - first_point) / duration, 4),
            "firstPointSeconds": first_point,
            "lastPointSeconds": last_point,
            "silentHeadSeconds": round(first_point, 1),
            "pointIntervalSeconds": {
                "mean": round(sum(gaps_between_points) / len(gaps_between_points), 1) if gaps_between_points else None,
                "median": round(median_gap, 1),
                "max": round(max(gaps_between_points), 1) if gaps_between_points else None,
            },
            "gapAlertThresholdSeconds": gap_alert,
            "gapAlertCount": len(gap_alerts),
            "gapAlerts": gap_alerts,
            "pointsWithSlide": attached,
            "pointsWithSlideRatio": round(attached / kept_points, 4) if kept_points else 0,
            "windowSummaries": slice_summaries,
        },
        "themes": [],
    }
    for i, th in enumerate(themes_all, start=1):
        out["themes"].append({
            "id": f"T{i:02d}",
            "title": th["title"],
            "kind": th["kind"],
            "summary": th["summary"],
            "startSeconds": th["startSeconds"],
            "endSeconds": th["endSeconds"],
            "startText": hhmmss(th["startSeconds"]),
            "endText": hhmmss(th["endSeconds"]),
            "durationSeconds": round(th["endSeconds"] - th["startSeconds"], 1),
            "pointCount": th["pointCount"],
            "fromSlice": th["slice"],
            "points": [
                {
                    "startSeconds": p["startSeconds"],
                    "startText": hhmmss(p["startSeconds"]),
                    "title": p["title"],
                    "excerpt": p["excerpt"],
                    "tags": p["tags"],
                    "source": p["source"],
                    "slideExcerpt": p["slideExcerpt"],
                    "slidePageIdx": p["slidePageIdx"],
                    "slideTimeText": p["slideTimeText"],
                }
                for p in th["points"]
            ],
        })
    out["problems"] = problems
    out["notes"] = notes
    outline_path.parent.mkdir(parents=True, exist_ok=True)
    outline_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # CSV 单元格里的换行会让「行数 = 知识点数 + 表头」的自查失真（verify.py 按物理行数数），
    # 所以所有文本字段统一把换行折成 " / "。
    def csv_cell(v) -> str:
        return re.sub(r"\r\n|\r|\n", " / ", str(v or ""))

    with points_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["theme_id", "theme_title", "kind", "point_startSeconds", "point_startText",
                    "point_title", "excerpt", "tags", "source", "slide_timeText", "slide_excerpt"])
        for th in out["themes"]:
            for p in th["points"]:
                w.writerow([th["id"], th["title"], th["kind"], p["startSeconds"], p["startText"],
                            csv_cell(p["title"]), csv_cell(p["excerpt"]), "|".join(p["tags"]), p["source"],
                            p["slideTimeText"] or "", csv_cell(p["slideExcerpt"])])

    lines = [f"# {cfg.title} · 内容大纲（两级）", "",
             f"- 源：`{source_desc(cfg)}`，时长 {hhmmss(duration)}（{duration:.0f}s）",
             f"- 一级主题 **{out['stats']['themeCount']}** 个；二级知识点 **{kept_points}** 条",
             f"- 主题时间覆盖（铺满口径）{out['stats']['tiledCoverageSeconds']:.0f}s（{out['stats']['tiledCoverageRatio']:.1%}）；"
             f"首末知识点跨度 {out['stats']['annotatedSpanSeconds']:.0f}s（{out['stats']['annotatedSpanRatio']:.1%}）",
             f"- 开头静音 {out['stats']['silentHeadSeconds']:.0f}s；知识点间隔中位 {out['stats']['pointIntervalSeconds']['median']}s、"
             f"最大 {out['stats']['pointIntervalSeconds']['max']}s；≥{out['stats']['gapAlertThresholdSeconds']:.0f}s 空档 {out['stats']['gapAlertCount']} 处",
             f"- 带屏幕证据的知识点 {attached} 条（{out['stats']['pointsWithSlideRatio']:.1%}）",
             "", f"> 口径：{out['mergeRule']['boundaryRule']}；时间戳来自 ASR，屏幕证据来自 OCR（启发式分类）。", "", "---", ""]
    for th in out["themes"]:
        lines.append(f"## {th['id']} {th['title']}")
        lines.append("")
        lines.append(f"`{th['startText']}–{th['endText']}`（{th['durationSeconds']:.0f}s）· {th['kind']} · {th['pointCount']} 条")
        lines.append("")
        if th["summary"]:
            lines.append(f"> {th['summary']}")
            lines.append("")
        for p in th["points"]:
            lines.append(f"- **{p['startText']}**（{p['startSeconds']:.0f}s）{p['title']}")
            if p["excerpt"]:
                lines.append(f"  - 口播：{p['excerpt']}")
            if p["slideExcerpt"]:
                lines.append(f"  - 屏幕：{p['slideExcerpt'].splitlines()[0][:80]}…")
            if p["tags"]:
                lines.append(f"  - 标签：{'、'.join(p['tags'])}")
        lines.append("")
    outline_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"themes={len(themes_all)} points={kept_points} dropped={dropped} "
          f"tiledCoverage={out['stats']['tiledCoverageRatio']:.3f} span={out['stats']['annotatedSpanRatio']:.3f} "
          f"gaps={len(gap_alerts)} withSlide={attached}")
    print(f"kindSeconds={kind_seconds}")
    print(f"schemaTolerance={norm_stats}")
    print(f"problems={problems}")
    print(f"wrote {cfg.rel(outline_path)} / {cfg.rel(outline_md)} / {cfg.rel(points_csv)}")


if __name__ == "__main__":
    main()
