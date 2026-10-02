"""把转写切成带时间戳的窗口切片，供子代理/大模型产出主题与知识点大纲。

口径（与实跑版一致）：
- 窗口长度与重叠来自 config.outline.windowSeconds / overlapSeconds（默认 600s / 150s）：
  相邻窗口重叠是为了不在窗口边界丢掉一个主题；
- 时间戳一律取该 segment 的 start（与既有规范一致：宁晚不早），
  它表示「这句开始说话的时刻」，不是屏幕内容出现的时刻；
- 每行格式 `[hh:mm:ss] (绝对秒) 文本`；
- 产物写到 config.paths.content 下的 _slices/，index.json 里的 file 字段记相对路径。

用法：python scripts/build_slices.py --config config.json
"""
import json
from pathlib import Path

from vd_config import load  # 与本脚本同目录；python scripts/build_slices.py 时脚本目录已在 sys.path[0]


def transcript_path(cfg) -> Path:
    """定位本次运行的转写 json。

    解析顺序与 build_clean.py / merge_outline.py 保持一致，避免上下游找的不是同一个文件：
      1. config.media.transcript 显式指定（相对路径相对 run 根）；
      2. transcripts/<bvid>_p<page>.json —— run_asr.py 的默认产物名；
      3. transcripts/<localAudio|localVideo|audioFile|videoFile 的 stem>.json —— local 模式命名。
    返回第一个存在的候选；都不存在时返回第一候选，报错时给出期望路径。
    """
    media = cfg.media
    tdir = cfg.path("transcripts")
    explicit = media.get("transcript")
    if explicit:
        p = Path(str(explicit))
        return p if p.is_absolute() else cfg.root / p
    cands = []
    bvid = str(media.get("bvid") or "")
    if bvid:
        cands.append(tdir / f"{bvid}_p{int(media.get('page') or 1)}.json")
    for k in ("localAudio", "localVideo", "audioFile", "videoFile"):
        v = media.get(k)
        if v:
            cands.append(tdir / f"{Path(str(v)).stem}.json")
    if not cands:
        raise SystemExit("config.media 里既没有 bvid 也没有本地音源/视频字段，无法定位转写 json")
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def main() -> None:
    cfg, _extra = load()
    outline = cfg.outline
    window = float(outline.get("windowSeconds") or 600.0)
    overlap = float(outline.get("overlapSeconds") or 150.0)
    if window <= 0 or overlap < 0 or overlap >= window:
        raise SystemExit(
            f"config.outline 窗口参数不合法：windowSeconds={window} overlapSeconds={overlap}"
            "（要求 window > 0 且 0 <= overlap < window，否则窗口会原地打转）"
        )

    trans = transcript_path(cfg)
    if not trans.exists():
        raise SystemExit(f"找不到转写文件：{trans}\n先跑 scripts/run_asr.py，或检查 config.media 的音源配置")

    data = json.loads(trans.read_text(encoding="utf-8"))
    segs = data["segments"]
    duration = data["duration"]

    out_dir = cfg.path("content", mkdir=True) / "_slices"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 头部说明里的模型/语言/域信息全部从配置取，脚本里不写死领域词。
    model_ref = str(cfg.asr.get("model") or "")
    model_label = Path(model_ref).name if model_ref else "ASR"
    language = str(cfg.asr.get("language") or "auto")
    profile = cfg.profile()
    profile_name = str(profile.get("name") or cfg.section("video").get("profile") or "generic")

    slices = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + window, duration)
        idx += 1
        rows = [s for s in segs if s["start"] >= start and s["start"] < end]
        lines = [
            f"# 切片 {idx:02d} · 窗口 {hhmmss(start)}–{hhmmss(end)}（绝对秒 {start:.0f}–{end:.0f}）",
            "",
            f"- 本片转写行数：{len(rows)}",
            f"- 全片总时长：{duration:.0f}s（{hhmmss(duration)}）",
            "- 时间戳是「该句开始说话的时刻」，不是屏幕内容出现的时刻。",
            f"- 文本来自 {model_label}（{language}），专有名词可能有错字，请按本域语境理解。",
            f"- 域配置：{profile_name}（术语表/停用词/屏幕关键词见 profiles/{profile_name}.json）。",
            "",
            "## 转写",
            "",
        ]
        for s in rows:
            lines.append(f"[{hhmmss(s['start'])}] ({s['start']:.1f}s) {s['text'].strip()}")
        slice_file = out_dir / f"slice-{idx:02d}.md"
        slice_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        slices.append({
            "id": f"slice-{idx:02d}",
            "file": cfg.rel(slice_file),
            "startSeconds": start,
            "endSeconds": end,
            "lineCount": len(rows),
            "charCount": sum(len(s["text"]) for s in rows),
        })
        if end >= duration:
            break
        start = end - overlap

    (out_dir / "index.json").write_text(json.dumps(
        {
            "transcript": cfg.rel(trans),
            "profile": profile_name,
            "duration": duration,
            "windowSeconds": window,
            "overlapSeconds": overlap,
            "slices": slices,
        },
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"transcript={trans}")
    print(f"slices={len(slices)} duration={duration:.0f} window={window:.0f} overlap={overlap:.0f}")
    for s in slices:
        print(f"  {s['id']} {s['startSeconds']:.0f}-{s['endSeconds']:.0f} lines={s['lineCount']} chars={s['charCount']}")


if __name__ == "__main__":
    main()
