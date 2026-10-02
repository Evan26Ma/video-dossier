"""ASR 转写：把音轨转成带词级时间戳的转写 JSON。

音源选择（优先级从高到低）：
  1. config.media.localAudio：本地已有音轨，**不发起任何下载请求**（实跑版口径，最可控）；
  2. 没有本地音轨时，按 config.media.bvid/page 用 yt-dlp 把音频下到 config.paths.audio。

输出契约（与实跑版一致，键名未改动）：
  <paths.transcripts>/<bvid>_p<page>.json
  {"bvid","page","duration","language","segments":[{"start","end","text","words":[...]}]}
  local 模式下产物文件名取 localAudio 的文件名 stem（例如 audio/讲课录音.m4a -> 讲课录音.json）。
  config.media.transcript 可以显式指定产物路径（相对路径相对 run 根），其它脚本按同一规则去找它。

模型/设备/精度/批大小/vad 全部来自 config.asr；GPU 不可用时回退 config.asr.deviceFallback。

用法：python scripts/run_asr.py --config config.json
"""
import glob
import json
import os
import site
import subprocess
import sys
import time
from pathlib import Path

from vd_config import load  # 与本脚本同目录；python scripts/run_asr.py 时脚本目录已在 sys.path[0]

AUDIO_EXTS = (".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav", ".flac", ".webm", ".mp4")


def add_nvidia_dll_dirs(cfg) -> list:
    """把 nvidia-* wheel 带的 DLL 目录塞进 PATH / add_dll_directory。

    与既有批次一致：cudnn/cublas 的 Windows wheel 把 DLL 放在 site-packages/nvidia/*/bin，
    不进 PATH 的话 ctranslate2 加载会失败。区别是不再写死某个 Python 版本目录
    （原版写死了 .../Python312/site-packages），改为从当前解释器的 site-packages 里找，
    并允许用 config.asr.nvidiaBinDirs 显式补充（相对路径相对 run 根解析）。
    """
    dirs = set()
    bases = []
    try:
        bases.extend(site.getsitepackages())
    except Exception:
        pass
    try:
        bases.append(site.getusersitepackages())
    except Exception:
        pass
    for base in bases:
        dirs.update(glob.glob(str(Path(base) / "nvidia" / "*" / "bin")))
    for extra in cfg.asr.get("nvidiaBinDirs") or []:
        p = Path(str(extra))
        if not p.is_absolute():
            p = cfg.root / p
        if p.is_dir():
            dirs.add(str(p))
    ordered = sorted(dirs)
    if not ordered:
        return []
    os.environ["PATH"] = os.pathsep.join(ordered) + os.pathsep + os.environ.get("PATH", "")
    if hasattr(os, "add_dll_directory"):  # Windows 专用
        for d in ordered:
            os.add_dll_directory(d)
    return ordered


def resolve_model(cfg) -> str:
    """config.asr.model 可以是本地模型目录，也可以是 HuggingFace 模型名（如 small）。"""
    raw = str(cfg.asr.get("model") or "").strip()
    if not raw:
        raise SystemExit(
            "config.asr.model 为空：填本地模型目录（例如 models/faster-whisper-small）"
            "或 HuggingFace 模型名（例如 small）"
        )
    looks_like_path = Path(raw).is_absolute() or ("/" in raw) or ("\\" in raw)
    if not looks_like_path:
        return raw  # 交给 faster-whisper 按模型名去 HF 取
    p = Path(raw)
    if not p.is_absolute():
        p = cfg.root / p
    if not p.exists():
        raise SystemExit(f"config.asr.model 指向的目录不存在：{p}")
    return str(p)


def find_local_audio(audio_dir: Path, bvid: str, page: int):
    """在 audio 目录里找已经下好的 <bvid>_p<page>.*，避免重复下载。"""
    if not bvid:
        return None
    for p in sorted(audio_dir.glob(f"{bvid}_p{page}.*")):
        if p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


def download_audio(cfg, bvid: str, page: int, audio_dir: Path) -> Path:
    """yt-dlp 匿名下载音频（不带任何登录态）。

    多分P 视频用 --playlist-items 指定分P（比 URL 上的 ?p= 可靠：后者在 yt-dlp 里
    仍会被当成整个播放列表下载）；文件名固定 <bvid>_p<page>.<ext>，与其它环节命名一致。
    可用 config.asr.ytdlp 指定 yt-dlp 可执行文件，config.asr.ytdlpArgs 追加额外参数。
    """
    if not bvid:
        raise SystemExit("config.media.bvid 为空，无法下载音频：请填 bvid，或改用 config.media.localAudio")
    asr = cfg.asr
    ytdlp = asr.get("ytdlp")
    cmd = [str(ytdlp)] if ytdlp else [sys.executable, "-m", "yt_dlp"]
    cmd += [
        "-f", str(asr.get("audioFormat") or "bestaudio[ext=m4a]/bestaudio"),
        "--playlist-items", str(page),
        "--retries", "3",
        "--no-warnings",
        "-o", str(audio_dir / f"{bvid}_p{page}.%(ext)s"),
    ]
    cmd += [str(a) for a in (asr.get("ytdlpArgs") or [])]
    cmd.append(f"https://www.bilibili.com/video/{bvid}")
    subprocess.run(cmd, check=True)
    got = find_local_audio(audio_dir, bvid, page)
    if got is None:
        raise SystemExit(f"yt-dlp 跑完但没找到音频文件：{audio_dir / (bvid + '_p' + str(page) + '.*')}")
    return got


def main() -> None:
    cfg, _extra = load()
    audio_dir = cfg.path("audio", mkdir=True)
    trans_dir = cfg.path("transcripts", mkdir=True)
    log_file = trans_dir / "asr-progress.log"

    def log(msg: str) -> None:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
        print(msg, flush=True)

    media, asr = cfg.media, cfg.asr
    bvid = str(media.get("bvid") or "").strip()
    page = int(media.get("page") or 1)
    kind = str(media.get("kind") or "bilibili")

    # ---------- 音源 ----------
    # 先按配置定出产物文件名（与音源无关），已完成的转写直接跳过，避免白下载一遍音频。
    local_audio = media.get("localAudio")
    audio_file = None
    stem = f"{bvid}_p{page}" if bvid else ""
    if local_audio:
        p = Path(str(local_audio))
        if not p.is_absolute():
            p = cfg.root / p
        if p.exists():
            # local 模式：产物文件名取 localAudio 的文件名 stem
            audio_file, stem = p, (p.stem or stem)
        elif kind == "local":
            log(f"ERROR audio missing: {p}")
            # 非零退出：pipeline.py 按退出码判失败；返回 0 会被当成成功，等下游报错就晚了
            raise SystemExit(2)
        else:
            log(f"WARN config.media.localAudio 不存在，回退 yt-dlp 下载：{p}")
    if not stem:
        raise SystemExit("config.media 里既没有 localAudio 也没有 bvid，无法确定音源与产物文件名")

    # media.transcript 显式指定时优先（流水线里其它脚本按同一规则找这个文件）。
    explicit = media.get("transcript")
    if explicit:
        out = Path(str(explicit))
        if not out.is_absolute():
            out = cfg.root / out
    else:
        out = trans_dir / f"{stem}.json"
    if out.exists() and out.stat().st_size > 100:
        log(f"SKIP already done: {out}")
        return

    if audio_file is None:
        audio_file = find_local_audio(audio_dir, bvid, page)
        if audio_file is not None:
            log(f"OK reuse audio in audio dir: {audio_file}")
    if audio_file is None:
        try:
            audio_file = download_audio(cfg, bvid, page, audio_dir)
        except Exception as exc:  # 下载失败就停在这一步，别把半成品当成功
            log(f"ERROR audio download failed: {exc!r}")
            raise SystemExit(2) from exc

    log(f"audio={audio_file} stem={stem}")

    # ---------- 模型 ----------
    # nvidia DLL 目录必须在 import faster_whisper 之前加好，所以这里的顺序不能改。
    dll_dirs = add_nvidia_dll_dirs(cfg)
    if dll_dirs:
        log(f"nvidia dll dirs={len(dll_dirs)}")
    from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402

    model_ref = resolve_model(cfg)
    log(f"model={model_ref}")

    device = str(asr.get("device") or "cuda")
    compute_type = str(asr.get("computeType") or "float16")
    fallback = asr.get("deviceFallback") or {}
    try:
        model = WhisperModel(model_ref, device=device, compute_type=compute_type)
        pipeline = BatchedInferencePipeline(model=model)
    except Exception as exc:  # GPU 不可用时回退
        device = str(fallback.get("device") or "cpu")
        compute_type = str(fallback.get("computeType") or "int8")
        log(f"FALLBACK to {device}/{compute_type}: {exc!r}")
        model = WhisperModel(model_ref, device=device, compute_type=compute_type)
        pipeline = BatchedInferencePipeline(model=model)

    language = asr.get("language", "zh") or None
    batch_size = int(asr.get("batchSize") or 8)
    beam_size = int(asr.get("beamSize") or 1)
    vad_filter = bool(asr.get("vadFilter", True))

    t0 = time.time()
    segs, info = pipeline.transcribe(
        str(audio_file),
        language=language,
        batch_size=batch_size,
        beam_size=beam_size,
        vad_filter=vad_filter,
        word_timestamps=True,
    )
    rows = [
        {
            "start": float(s.start),
            "end": float(s.end),
            "text": s.text,
            "words": [
                {"start": float(w.start), "end": float(w.end), "word": w.word}
                for w in (s.words or [])
            ],
        }
        for s in segs
    ]
    payload = {
        "bvid": bvid,
        "page": page,
        "duration": float(info.duration or 0),
        "language": info.language,
        "segments": rows,
    }
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    log(
        f"OK {bvid} p{page} device={device}/{compute_type} audio={audio_file.name} "
        f"duration={info.duration:.2f} segments={len(rows)} asr={time.time() - t0:.1f}s"
    )
    log(f"out={out}")


if __name__ == "__main__":
    main()
