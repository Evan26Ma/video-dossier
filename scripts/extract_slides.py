"""画面挖掘：抽帧 -> dHash 去重 -> RapidOCR -> 屏幕内容汇编（video-dossier 版）。

分阶段执行（--stage）：
  probe  只用 ffprobe 读容器/流信息（不抽帧，零成本）
  pass1  1 帧/N 秒抽样，判断画面里到底有没有字（低成本画像）
  pass2  1 帧/N 秒抽帧 + dHash 去重，产出「页面」（屏幕内容切换后的代表帧）
  ocr    对去重后的页面按时间桶分层抽样，跑本地 RapidOCR（离线）
  report 落 slides.jsonl / ocr-quality.json / 屏幕状态时间轴.json / 屏幕内容时间轴.md / 屏幕关键文本.json
  all    依次跑 pass1 -> pass2 -> ocr -> report

口径要点（原实跑版本验证过的坑，逐条保留）：
  - 帧时间戳由 fps 均匀采样推算（fps=1/N -> 第 n 帧 = (n-1)*N 秒），误差 <=1 帧（33ms）。
  - dHash 为 8x8（64bit）灰度均值哈希，与「上一张保留页」比较，汉明距离 <= 阈值视为同一页。
    注意是「上一张保留页」而不是「上一帧」：同一页内缓慢变化（光标、进度条、聊天滚动）时
    不会误切，否则页数会成倍膨胀。
  - 页面图保留 JPEG 原帧（不做 PNG 无损重编码，避免无意义的体积膨胀）。
  - 状态键优先用「数字集合」（百分比 + 分数对）判定屏幕状态，比整页文本鲁棒得多：
    整页文本里混着滚动群聊，逐字符比对几乎每次都不一样。
  - OCR 必须在去重之后再按时间桶分层抽样：这块屏幕的背景是常驻看板、前景是持续滚动的
    群聊与视频窗，逐帧 dHash 只能粗筛，「聊天在滚」会被当成新页面，去重后页数会爆掉。
    按固定时间桶每桶只取该时段最早一张，既压住 OCR 成本，又覆盖看板真实变化。
  - EMPTY 状态（该页没抽到数字、也没抽到可用文本）必须合并成一个状态，而不是每页一个。
  - OCR 文本只作辅助证据，不参与任何语言统计。

配置读取：抽帧参数与阈值一律来自 cfg.slides，屏幕关键词一律来自 cfg.profile()["screen"]；
本脚本里没有任何写死的绝对路径（视频路径只能来自 cfg.media.localVideo 或 cfg.path("video")）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from vd_config import load

# ---- 默认口径：与原实跑版本写死的值一致；每一项都可以被 config.slides 覆盖 ----
DHASH_SIZE = 8            # dHash 网格（8x8 -> 64bit）
HAMMING_MAX = 6           # 「与上一张保留页」的汉明距离阈值
PASS1_EVERY = 10.0        # pass1 抽帧间隔（秒）
PASS2_EVERY = 2.0         # pass2 抽帧间隔（秒）
OCR_BUCKET = 30.0         # OCR 分层抽样桶宽（秒）
OCR_MIN_SCORE = 0.60      # 「这页有字」的置信度下限
PASS1_OCR_SAMPLES = 30    # pass1 抽样 OCR 的帧数
MIN_CHARS_FOR_TEXT = 10   # 判定「这页有字」的最少字符数
VERDICT_USABLE_RATIO = 0.30    # 有字帧占比达到该值 -> usable
VERDICT_UNUSABLE_RATIO = 0.05  # 低于该值 -> unusable，中间为 degraded

# 画面构成描述：属于域知识，不写死在流程里；可用 config.slides.screenIsWhat 覆盖。
DEFAULT_SCREEN_NOTE = ("按 pass1/pass2 抽帧与 OCR 得到的屏幕画像；具体画面构成"
                       "（课件/看板/聊天窗叠加等）请结合 slides/pages 下的页面图人工确认。")
DEFAULT_OCR_ENGINE = "rapidocr_onnxruntime / PP-OCRv4 det+rec+cls（本地 ONNX，离线）"

LOG_PATH: Path | None = None


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if LOG_PATH is not None:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(line, flush=True)


def die(msg: str, code: int = 2) -> None:
    log(f"ERROR {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- 上下文

class Ctx:
    """一次 run 的输入/输出路径与阈值。所有路径都从配置解析。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.slides = cfg.path("slides", mkdir=True)
        self.pages = self.slides / "pages"
        self.pass1_dir = self.slides / "_pass1"
        self.pass2_dir = self.slides / "_pass2"
        for d in (self.pages, self.pass1_dir, self.pass2_dir):
            d.mkdir(parents=True, exist_ok=True)

        s = cfg.slides
        self.pass1_every = float(s.get("pass1EverySeconds", PASS1_EVERY))
        self.pass2_every = float(s.get("pass2EverySeconds", PASS2_EVERY))
        self.hamming_max = int(s.get("hammingMax", HAMMING_MAX))
        self.dhash_size = int(s.get("dhashSize", DHASH_SIZE))
        self.ocr_bucket = float(s.get("ocrBucketSeconds", OCR_BUCKET))
        self.ocr_min_score = float(s.get("ocrMinScore", OCR_MIN_SCORE))
        self.pass1_samples = int(s.get("pass1OcrSamples", PASS1_OCR_SAMPLES))
        self.min_chars = int(s.get("minCharsForText", MIN_CHARS_FOR_TEXT))
        self.usable_ratio = float(s.get("verdictUsableRatio", VERDICT_USABLE_RATIO))
        self.unusable_ratio = float(s.get("verdictUnusableRatio", VERDICT_UNUSABLE_RATIO))
        self.screen_note = s.get("screenIsWhat") or DEFAULT_SCREEN_NOTE
        self.ocr_engine = s.get("ocrEngine") or DEFAULT_OCR_ENGINE

        # 屏幕关键词来自域配置，不在脚本里写死
        screen = cfg.profile().get("screen") or {}
        self.dash_words = [str(w) for w in (screen.get("dashboardWords") or [])]
        self.chrome_words = [str(w) for w in (screen.get("chromeWords") or [])]
        patterns = [str(p) for p in (screen.get("chatPatterns") or []) if str(p)]
        # 空 pattern 会匹配一切：没有配置就视为「不识别聊天块」
        self.chat_re = re.compile("|".join(patterns)) if patterns else None
        label = screen.get("topicLabelPattern")
        self.label_re = re.compile(label) if label else None
        self.pct_re = re.compile(r"\d+(?:\.\d+)?%")
        # 分数对形如 12/20、1,234/5,678：要求两侧都有数字，避免把 2024/1 这类切歪
        self.frac_re = re.compile(r"\d[\d,]*\s*/\s*\d[\d,]*")

    # --- 外部工具 ---
    def tool(self, name: str, default: str) -> str:
        tools = self.cfg.section("tools")
        return str(tools.get(name) or self.cfg.slides.get(name) or default)

    @property
    def ffmpeg(self) -> str:
        return self.tool("ffmpeg", "ffmpeg")

    @property
    def ffprobe(self) -> str:
        explicit = self.cfg.section("tools").get("ffprobe") or self.cfg.slides.get("ffprobe")
        if explicit:
            return str(explicit)
        # ffmpeg 与 ffprobe 通常同目录；给出裸名时退回 PATH
        p = Path(self.ffmpeg)
        cand = p.with_name("ffprobe" + p.suffix)
        return str(cand) if cand.exists() else "ffprobe"

    @property
    def ytdlp(self) -> str:
        return self.tool("ytdlp", "yt-dlp")


# ---------------------------------------------------------------- 输入视频

def resolve_path(cfg, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (cfg.root / p)


def resolve_video(ctx: Ctx) -> Path:
    """定位输入视频：cfg.media.localVideo 优先；否则按 bvid/page 下载到 cfg.path("video")。

    两者都没有就明确报错 —— 不静默跳过，否则后面每一阶段都会产出空产物。
    """
    cfg = ctx.cfg
    local = cfg.media.get("localVideo")
    if local:
        p = resolve_path(cfg, str(local))
        if not p.exists():
            die(f"config.media.localVideo 指向的文件不存在：{p}")
        log(f"input video: {p}（来自 config.media.localVideo）")
        return p

    video_dir = cfg.path("video", mkdir=True)
    bvid = (cfg.media.get("bvid") or "").strip()
    if not bvid:
        die("没有输入视频可处理：config.media.localVideo 为空，且 config.media.bvid 也是空的。\n"
            "      local 模式请填 media.localVideo（绝对路径或相对 run 根）；\n"
            "      bilibili 模式请填 media.bvid + media.page。")
    page = int(cfg.media.get("page") or 1)
    out = video_dir / f"{bvid}_p{page}.mp4"

    vf = str(cfg.slides.get("videoFormat") or "").strip()
    fmt = f"{vf}+bestaudio/best" if vf else "bv*+ba/b"
    cmd = [ctx.ytdlp, "--no-playlist", "--playlist-items", str(page),
           "-f", fmt, "--merge-output-format", "mp4"]
    cmd += [str(a) for a in (cfg.slides.get("ytdlpArgs") or [])]
    cmd += ["-o", str(out), f"https://www.bilibili.com/video/{bvid}"]
    log("download: " + " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        die(f"找不到 yt-dlp（{ctx.ytdlp}）；可用 config.tools.ytdlp 指定路径，"
            f"或直接给 config.media.localVideo 跳过下载。")
    except subprocess.CalledProcessError as e:
        die(f"yt-dlp 下载失败（退出码 {e.returncode}）：{bvid} p{page}")
    if not out.exists():
        die(f"下载命令已返回但未找到产物：{out}")
    log(f"input video: {out}（yt-dlp 下载，format={fmt}）")
    return out


# ---------------------------------------------------------------- 抽帧 / 哈希

def probe_video(ctx: Ctx, video: Path) -> dict:
    out = subprocess.run(
        [ctx.ffprobe, "-v", "error", "-show_entries",
         "format=duration,size,bit_rate", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate,bit_rate",
         "-of", "json", str(video)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": (out.stderr or out.stdout or "").strip()[:500]}


def duration_seconds(ctx: Ctx, video: Path) -> float | None:
    """从 ffprobe 结果里抠时长；抠不到就返回 None（不编造读数）。"""
    info = probe_video(ctx, video)
    try:
        return round(float(info["format"]["duration"]), 3)
    except (KeyError, TypeError, ValueError):
        return None


def extract(ctx: Ctx, video: Path, every: float, outdir: Path) -> int:
    """按 fps=1/every 均匀抽帧，输出 jpg（q=3）。返回帧数。"""
    for old in outdir.glob("*.jpg"):
        old.unlink()
    cmd = [
        ctx.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vf", f"fps=1/{every}",
        "-q:v", "3",
        str(outdir / "f_%06d.jpg"),
    ]
    t0 = time.time()
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        die(f"找不到 ffmpeg（{ctx.ffmpeg}）；可用 config.tools.ffmpeg 指定路径。", 3)
    except subprocess.CalledProcessError as e:
        die(f"ffmpeg 抽帧失败（退出码 {e.returncode}）：every={every}s")
    n = len(list(outdir.glob("*.jpg")))
    log(f"extract every={every}s -> {outdir.name} frames={n} in {time.time() - t0:.1f}s")
    return n


def require_cv2():
    try:
        import cv2  # noqa: F401
    except ImportError:
        die("缺少图像依赖：pip install opencv-python（dHash 去重要用 cv2）", 3)
    import cv2
    return cv2


def require_ocr():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        die("缺少 OCR 依赖：pip install rapidocr_onnxruntime\n"
            "      RapidOCR 走本地 ONNX 推理，不需要联网；首次运行会下载模型到用户目录。", 3)
    return RapidOCR()


def dhash(ctx: Ctx, path: Path) -> int:
    cv2 = require_cv2()
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return 0
    size = ctx.dhash_size
    small = cv2.resize(img, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = 0
    for row in (small[:, 1:] > small[:, :-1]).flatten():
        bits = (bits << 1) | int(row)
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def frame_time(idx: int, every: float) -> float:
    """第 idx 帧（1-based）对应的视频时间。"""
    return round((idx - 1) * every, 3)


def verdict_of(ratio: float, ctx: Ctx) -> str:
    if ratio >= ctx.usable_ratio:
        return "usable"
    if ratio < ctx.unusable_ratio:
        return "unusable"
    return "degraded"


# ---------------------------------------------------------------- 阶段

def stage_probe(ctx: Ctx, video: Path) -> None:
    info = probe_video(ctx, video)
    (ctx.slides / "_video-probe.json").write_text(
        json.dumps({"video": ctx.cfg.rel(video), "probe": info}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    dur = duration_seconds(ctx, video)
    log(f"probe: duration={dur}s  {json.dumps(info, ensure_ascii=False)[:300]}")


def stage_pass1(ctx: Ctx, video: Path) -> None:
    n = extract(ctx, video, ctx.pass1_every, ctx.pass1_dir)
    engine = require_ocr()
    # 均匀取若干帧做抽样 OCR，估计画面文字量（不需要逐帧 OCR）
    k = max(1, ctx.pass1_samples)
    idxs = sorted(set(max(1, round(i * n / k)) for i in range(1, k + 1)))
    rows = []
    for i in idxs:
        p = ctx.pass1_dir / f"f_{i:06d}.jpg"
        if not p.exists():
            continue
        result, _ = engine(str(p))
        texts = [r[1] for r in (result or [])]
        scores = [float(r[2]) for r in (result or [])]
        chars = sum(len(t) for t in texts)
        rows.append({
            "frame": i, "timeSeconds": frame_time(i, ctx.pass1_every),
            "blockCount": len(texts), "charCount": chars,
            "meanScore": round(sum(scores) / len(scores), 4) if scores else None,
            "sample": " / ".join(texts)[:120],
        })
        log(f"pass1 ocr frame {i}/{n} chars={chars} blocks={len(texts)}")
    with_text = [r for r in rows
                 if r["charCount"] >= ctx.min_chars and (r["meanScore"] or 0) >= ctx.ocr_min_score]
    ratio = len(with_text) / len(rows) if rows else 0.0
    out = {
        "stage": "pass1",
        "video": ctx.cfg.rel(video),
        "pass1EverySeconds": ctx.pass1_every,
        "sampledFrames": n,
        "ocrProbedFrames": len(rows),
        "framesWithText": len(with_text),
        "framesWithTextRatio": round(ratio, 4),
        "meanCharCount": round(sum(r["charCount"] for r in rows) / len(rows), 2) if rows else 0,
        "verdict": verdict_of(ratio, ctx),
        "rows": rows,
    }
    (ctx.slides / "pass1-profile.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"pass1 verdict={out['verdict']} textRatio={ratio:.3f} meanChars={out['meanCharCount']}")


def stage_pass2(ctx: Ctx, video: Path) -> None:
    n = extract(ctx, video, ctx.pass2_every, ctx.pass2_dir)
    hashes: dict[int, int] = {}
    kept: list[int] = []
    last_hash = None
    for i in range(1, n + 1):
        p = ctx.pass2_dir / f"f_{i:06d}.jpg"
        if not p.exists():
            continue
        h = dhash(ctx, p)
        hashes[i] = h
        if last_hash is None or hamming(h, last_hash) > ctx.hamming_max:
            kept.append(i)
        # 无论保留与否都更新参照：与「上一张保留页」比较，
        # 同一页内缓慢变化时不误切（若改成与上一帧比较，页数会成倍膨胀）
        last_hash = h
    # 把保留页复制到 pages/ 并命名
    for old in ctx.pages.glob("*.jpg"):
        old.unlink()
    meta = []
    for order, i in enumerate(kept, start=1):
        src = ctx.pass2_dir / f"f_{i:06d}.jpg"
        t = frame_time(i, ctx.pass2_every)
        hh, mm, ss = int(t // 3600), int(t % 3600 // 60), int(t % 60)
        name = f"{order:04d}_{hh:02d}{mm:02d}{ss:02d}.jpg"
        shutil.copyfile(src, ctx.pages / name)
        meta.append({
            "idx": order, "frame": i, "timeSeconds": t, "timeText": f"{hh:02d}:{mm:02d}:{ss:02d}",
            "imagePath": f"pages/{name}", "dhash": f"{hashes[i]:016x}",
        })
    (ctx.slides / "_pass2-kept.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"pass2 frames={n} keptPages={len(meta)} dedupeRatio={len(meta) / n if n else 0:.3f}")
    # 抽帧中间产物体积大，落盘后即清理
    for old in ctx.pass2_dir.glob("*.jpg"):
        old.unlink()
    log("pass2 temp frames cleaned")


def ocr_subset(ctx: Ctx, meta: list) -> list:
    """分层抽样：每 OCR_BUCKET 秒只取该时段最早的一张去重页。

    原因（实跑结论）：这块屏幕的背景是常驻的看板、前景是持续滚动的群聊与视频窗，
    逐帧 dHash 会把「聊天在滚」误判成新页面，去重后的页数会爆掉。
    按固定时间桶抽样既能把 OCR 成本压到可控，又能覆盖看板的真实变化。
    """
    buckets: dict[int, dict] = {}
    for m in meta:
        b = int(m["timeSeconds"] // ctx.ocr_bucket)
        buckets.setdefault(b, m)
    return [buckets[b] for b in sorted(buckets)]


def stage_ocr(ctx: Ctx, video: Path) -> None:
    engine = require_ocr()
    kept_path = ctx.slides / "_pass2-kept.json"
    if not kept_path.exists():
        die(f"缺少 {ctx.cfg.rel(kept_path)}：请先跑 --stage pass2。")
    meta = json.loads(kept_path.read_text(encoding="utf-8"))
    subset = ocr_subset(ctx, meta)
    (ctx.slides / "_ocr-subset.json").write_text(json.dumps(
        {"bucketSeconds": ctx.ocr_bucket, "keptPages": len(meta), "ocrPages": len(subset),
         "selection": "每 ocrBucketSeconds 秒取该时段最早的去重页"},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"ocr subset: kept={len(meta)} -> ocr={len(subset)} (bucket={ctx.ocr_bucket}s)")
    rows = []
    t0 = time.time()
    for k, m in enumerate(subset, start=1):
        p = ctx.slides / m["imagePath"]
        result, _ = engine(str(p))
        blocks = []
        for r in (result or []):
            box, text, score = r[0], r[1], float(r[2])
            blocks.append({"text": text, "score": round(score, 4), "box": box})
        texts = [b["text"] for b in blocks]
        scores = [b["score"] for b in blocks]
        row = dict(m)
        row.update({
            "blocks": blocks,
            "ocrText": "\n".join(texts),
            "charCount": sum(len(t) for t in texts),
            "meanScore": round(sum(scores) / len(scores), 4) if scores else None,
            "minScore": round(min(scores), 4) if scores else None,
            "textKey": re.sub(r"\s+", "", "".join(texts))[:200],
        })
        rows.append(row)
        if k % 25 == 0 or k == len(subset):
            log(f"ocr {k}/{len(subset)} elapsed={time.time() - t0:.0f}s")
    (ctx.slides / "_ocr-raw.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    log(f"ocr done pages={len(rows)} in {time.time() - t0:.0f}s")


def classify_block(ctx: Ctx, text: str) -> str:
    t = text.strip()
    if ctx.chat_re and ctx.chat_re.search(t):
        return "chat"
    if any(w in t for w in ctx.chrome_words) and not any(w in t for w in ctx.dash_words):
        return "chrome"
    if any(w in t for w in ctx.dash_words):
        return "dashboard"
    if ctx.pct_re.fullmatch(t) or ctx.frac_re.search(t):
        return "dashboard"
    if len(t) <= 2:
        return "noise"
    return "other"


def stage_report(ctx: Ctx, video: Path) -> None:
    raw_path = ctx.slides / "_ocr-raw.json"
    if not raw_path.exists():
        die(f"缺少 {ctx.cfg.rel(raw_path)}：请先跑 --stage ocr。")
    rows = json.loads(raw_path.read_text(encoding="utf-8"))
    subset_path = ctx.slides / "_ocr-subset.json"
    subset = json.loads(subset_path.read_text(encoding="utf-8")) if subset_path.exists() else \
        {"bucketSeconds": ctx.ocr_bucket, "keptPages": len(rows), "ocrPages": len(rows)}
    prof_path = ctx.slides / "pass1-profile.json"
    profile = json.loads(prof_path.read_text(encoding="utf-8")) if prof_path.exists() else {}

    kind_counter: dict[str, int] = {}
    for r in rows:
        for b in r["blocks"]:
            b["kind"] = classify_block(ctx, b["text"])
            kind_counter[b["kind"]] = kind_counter.get(b["kind"], 0) + 1
        r["blockKinds"] = {k: sum(1 for b in r["blocks"] if b["kind"] == k)
                           for k in ("dashboard", "chat", "chrome", "noise", "other")}
        dash_lines = [b["text"] for b in r["blocks"] if b["kind"] == "dashboard"]
        r["dashboardText"] = "\n".join(dash_lines)
        r["dashboardCharCount"] = sum(len(x) for x in dash_lines)
        norm = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff%.]", "", "".join(sorted(dash_lines)))
        r["dashboardKey"] = norm[:200]
        r["numbers"] = {
            "percent": sorted(set(ctx.pct_re.findall(r["dashboardText"]))),
            "fraction": sorted({ctx.frac_re.search(x).group(0).replace(" ", "")
                                for x in dash_lines if ctx.frac_re.search(x)}),
        }
        # 状态键：优先用「数字集合」判定看板状态（比整段文本鲁棒得多）
        nums = r["numbers"]["percent"] + r["numbers"]["fraction"]
        r["stateKey"] = "|".join(sorted(set(nums))) if nums else ("TXT:" + norm[:80] if norm else "EMPTY")

    with_text = [r for r in rows
                 if r["charCount"] >= ctx.min_chars and (r["meanScore"] or 0) >= ctx.ocr_min_score]
    scores = [r["meanScore"] for r in rows if r["meanScore"] is not None]
    low_conf = [r["idx"] for r in rows if (r["meanScore"] or 0) < ctx.ocr_min_score]
    ratio = len(with_text) / len(rows) if rows else 0.0
    dash_ratio = (sum(1 for r in rows if r["dashboardCharCount"] >= 10) / len(rows)) if rows else 0.0
    verdict = verdict_of(ratio, ctx)

    # 屏幕状态时间轴：按 stateKey 聚合。
    # 关键：没有抽到数字也没有可用文本的页面全部落到同一个 "EMPTY" 键上，
    # 合并成一个状态而不是每页一个 —— 否则 EMPTY 会刷出几十个「状态」，时间轴不可读。
    states: dict[str, list] = {}
    for r in rows:
        key = r["stateKey"] or "EMPTY"
        states.setdefault(key, []).append(r)
    state_list = []
    for key, members in states.items():
        members.sort(key=lambda x: x["timeSeconds"])
        state_list.append({
            "stateKey": key[:200],
            "firstTimeSeconds": members[0]["timeSeconds"],
            "firstTimeText": members[0]["timeText"],
            "pageCount": len(members),
            "timeTexts": [m["timeText"] for m in members],
            "percents": sorted({p for m in members for p in m["numbers"]["percent"]}),
            "fractions": sorted({f for m in members for f in m["numbers"]["fraction"]}),
            "dashboardText": max((m["dashboardText"] for m in members), key=len),
            "meanScore": round(sum(m["meanScore"] or 0 for m in members) / len(members), 4),
        })
    state_list.sort(key=lambda s: s["firstTimeSeconds"])

    # 屏幕关键文本摘录（原「问卷看板数字」，条目结构不变）。
    # 命中的行都来自已按 profile 关键词判为 dashboard 的文本，再筛 label 正则或看板词，
    # 不在脚本里写死任何域内词。
    key_text = []
    for m in rows:
        for line in m["dashboardText"].split("\n"):
            hit = (ctx.label_re and ctx.label_re.search(line)) or \
                  any(w and w in line for w in ctx.dash_words)
            if hit:
                key_text.append({"timeText": m["timeText"], "timeSeconds": m["timeSeconds"],
                                 "text": line})
    seen = set()
    key_text_dedup = []
    for s in key_text:
        k = re.sub(r"\s+", "", s["text"])
        if k in seen:
            continue
        seen.add(k)
        key_text_dedup.append(s)

    probe_path = ctx.slides / "_video-probe.json"
    duration = None
    if probe_path.exists():
        try:
            duration = round(float(json.loads(probe_path.read_text(encoding="utf-8"))
                                   ["probe"]["format"]["duration"]), 3)
        except (KeyError, TypeError, ValueError):
            duration = None

    quality = {
        "video": ctx.cfg.rel(video),
        "durationSeconds": duration,
        "videoFormat": ctx.cfg.slides.get("videoFormat"),
        "screenIsWhat": ctx.screen_note,
        "pass1": {k: profile.get(k) for k in ("sampledFrames", "ocrProbedFrames", "framesWithText",
                                              "framesWithTextRatio", "meanCharCount", "verdict")},
        "pass2SamplingSeconds": ctx.pass2_every,
        "hammingMax": ctx.hamming_max,
        "dedupKeptPages": subset["keptPages"],
        "ocrPages": len(rows),
        "ocrBucketSeconds": subset["bucketSeconds"],
        "uniquePages": len(rows),
        "pagesWithText": len(with_text),
        "pagesWithTextRatio": round(ratio, 4),
        "pagesWithDashboardText": sum(1 for r in rows if r["dashboardCharCount"] >= 10),
        "pagesWithDashboardTextRatio": round(dash_ratio, 4),
        "distinctScreenStates": len(state_list),
        "blockKindCounts": kind_counter,
        "meanScore": round(sum(scores) / len(scores), 4) if scores else None,
        "minScore": round(min(scores), 4) if scores else None,
        "lowConfPageCount": len(low_conf),
        "lowConfPages": low_conf[:50],
        "emptyPageCount": sum(1 for r in rows if not r["textKey"]),
        "verdict": verdict,
        "ocrEngine": ctx.ocr_engine,
        "notes": [
            f"帧时间戳由 fps=1/{ctx.pass2_every:g} 均匀采样推算，误差 <=1 帧。",
            f"pass2 去重：与上一张保留页的 dHash 汉明距离 <={ctx.hamming_max} 视为同一页；"
            "因前景内容持续滚动，该步只能粗筛。",
            f"OCR 分层抽样：去重页 {subset['keptPages']} 张中，每 {subset['bucketSeconds']:g} 秒"
            f"取该时段最早一张，共 OCR {len(rows)} 张。",
            "文本块分类（dashboard/chat/chrome/noise/other）是关键词+正则启发式，"
            "不是人工标注，存在误分类。",
            "看板「状态键」优先用该页抽到的数字集合（百分比+分数对）判定，"
            "避免把滚动群聊误判成新状态；无数字无文本的页统一并入 EMPTY 状态。",
            "OCR 文本只作辅助证据，不参与任何语言统计；摘录内容未做人工逐项校对。",
        ],
    }
    (ctx.slides / "ocr-quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (ctx.slides / "屏幕状态时间轴.json").write_text(json.dumps(
        {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
         "stateCount": len(state_list), "states": state_list}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    (ctx.slides / "屏幕关键文本.json").write_text(json.dumps(
        {"note": "从屏幕 OCR 中抽出的关键条目（关键词+正则启发式，未人工逐项校对）",
         "count": len(key_text_dedup), "items": key_text_dedup}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    with (ctx.slides / "slides.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    lines = ["# 屏幕上挖到的内容（OCR）", "",
             f"- 源视频：`{video.name}`",
             f"- 画面实况：{quality['screenIsWhat']}",
             f"- pass2 去重页：**{subset['keptPages']}** 张；实际 OCR：**{len(rows)}** 张"
             f"（每 {subset['bucketSeconds']:g} 秒取一张）",
             f"- 有文本页：**{len(with_text)}**（{ratio:.1%}）；"
             f"含看板文字页：{quality['pagesWithDashboardText']}（{dash_ratio:.1%}）",
             f"- 识别出的不同屏幕状态：**{len(state_list)}** 个",
             f"- 平均置信度：**{quality['meanScore']}**；低置信页面 {len(low_conf)} 张",
             f"- OCR：{quality['ocrEngine']}",
             "", "> 说明：文本块分类是关键词启发式；OCR 有错字；OCR 文本不参与语言统计。",
             "", "---", ""]
    for i, s in enumerate(state_list, start=1):
        lines.append(f"## 状态 {i:02d} · 首现 {s['firstTimeText']}（{s['pageCount']} 次抽样命中）")
        lines.append("")
        span = "、".join(s["timeTexts"][:14]) + ("…" if len(s["timeTexts"]) > 14 else "")
        lines.append(f"- 出现时间：{span}")
        if s["percents"]:
            lines.append(f"- 百分比：{'、'.join(s['percents'])}")
        if s["fractions"]:
            lines.append(f"- 分数对：{'、'.join(s['fractions'])}")
        lines.append("")
        for ln in s["dashboardText"].split("\n"):
            if ln.strip():
                lines.append(f"- {ln}")
        lines.append("")
    (ctx.slides / "屏幕内容时间轴.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"report written: ocrPages={len(rows)} states={len(state_list)} verdict={verdict} "
        f"meanScore={quality['meanScore']} dashRatio={dash_ratio:.3f}")


# ---------------------------------------------------------------- 入口

def main() -> None:
    global LOG_PATH
    cfg, rest = load(argv=sys.argv[1:])
    ap = argparse.ArgumentParser(prog="extract_slides.py")
    ap.add_argument("--stage", required=True,
                    choices=["probe", "pass1", "pass2", "ocr", "report", "all"])
    args = ap.parse_args(rest)

    if cfg.slides.get("enabled") is False and args.stage != "probe":
        log("slides.enabled=false：按配置跳过画面挖掘。")
        return

    ctx = Ctx(cfg)
    LOG_PATH = ctx.slides / "progress.log"
    log(f"config={cfg.config_path} runRoot={cfg.root} stage={args.stage}")

    video = resolve_video(ctx)

    if args.stage == "probe":
        stage_probe(ctx, video)
    if args.stage in ("pass1", "all"):
        stage_pass1(ctx, video)
    if args.stage in ("pass2", "all"):
        stage_pass2(ctx, video)
    if args.stage in ("ocr", "all"):
        stage_ocr(ctx, video)
    if args.stage in ("report", "all"):
        stage_report(ctx, video)


if __name__ == "__main__":
    main()
