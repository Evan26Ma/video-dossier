"""语言与节奏统计：读 raw ASR 转写，产出 language-stats.json / per-minute.csv / top-words.csv / figures/*.png。

口径（与实跑 README 一致，未改动）：
- 统计文本一律使用 raw ASR（未纠错）；clean 稿与 OCR 文本都不参与统计。
- 字数：han=[\\u4e00-\\u9fff]；latinDigit=[A-Za-z0-9]；punct=其余非空白字符；withoutPunct=han+latinDigit。
- 语速：hanPerMin = han / (duration/60)；charsPerMin = withoutPunct / (duration/60)。
- 停顿：相邻 segment 间隔 gap = next.start - prev.end，gap >= config.stats.pauseThresholdSeconds 记一次停顿，分档统计。
- 逐分钟：按 segment 与分钟区间的重叠时长把字数**按比例摊分**到各分钟（不是把整段都算给起始分钟）。
- 词频：jieba 精确模式（可用时）+ 停用词表 + 最小词长 config.stats.minWordLength；
  口头禅用 config.stats.ngramMin–ngramMax 的 n-gram 并做极大化过滤。
- 术语命中：按 config.video.profile 指向的 profile 里的 terms 分类词表，
  同类内最长词优先且不重复计数；跨类别可重复计。
- 停用词 = 内置通用表 ∪ profile.stopwordsExtra（领域口头禅/称呼在 profile 里维护，脚本不写死领域词）。

用法：python scripts/build_stats.py --config config.json
"""
import datetime
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

from vd_config import load  # 与本脚本同目录；python scripts/build_stats.py 时脚本目录已在 sys.path[0]

HAN = re.compile(r"[\u4e00-\u9fff]")
LATIN = re.compile(r"[A-Za-z0-9]")
WS = re.compile(r"\s")

# 内置通用停用词：只放「任何中文口语都高频」的词，领域词一律进 profile.stopwordsExtra。
BUILTIN_STOPWORDS = set("""
的 了 是 我 你 他 她 它 我们 你们 他们 咱们 大家 自己 这个 那个 这些 那些 这样 那样 这里 那里
就是 还有 然后 因为 所以 但是 可是 如果 那么 什么 怎么 为什么 怎么样 多少 哪个 哪些 现在 时候
一个 一些 一样 一直 一起 一定 没有 不是 不能 不会 不要 不用 可以 可能 应该 必须 需要 觉得 认为
知道 看到 听到 说 讲 做 去 来 上 下 里 外 中 前 后 左 右 好 对 行 吧 呢 吗 啊 呀 哦 嗯 哈
老师 同学 同学们 各位 今天 明天 昨天 今年 明年 去年 上午 下午 晚上 第一 第二 第三 一次 一点
其实 当然 反正 真的 特别 非常 比较 有点 稍微 完全 基本 大概 差不多 一般 通常 经常 总是 有时候
东西 事情 问题 情况 地方 时间 方面 内容 部分 过程 结果 原因 方法 方式 意思 感觉 样子 关系
这 那 哪 谁 跟 和 与 或 或者 而且 并且 不过 只是 还是 也是 也要 就会 就是 有种 有点 用 把 被
里面 上面 下面 前面 后面 出来 起来 过来 上去 下去 出来 的话 一样 这种 那种 哪种 每个 各种
""".split())

QUESTION_MARKS = ["吗", "呢", "对不对", "是不是", "好不好", "对吧", "懂吗", "懂不懂", "有没有", "什么是", "为什么", "怎么办", "多少"]


def add_pylibs(cfg) -> None:
    """jieba 优先用 tools/pylibs（离线随仓库带的第三方库），仓库根和 run 根都找一遍。

    实跑版的 tools/pylibs 是放在 run 根下的；仓库化以后更常见的是放在仓库根下（.gitignore 里）。
    两处都插，仓库根优先，保证任何目录布局下都能命中。
    """
    here = Path(__file__).resolve().parent
    candidates = [here.parent / "tools" / "pylibs", cfg.root / "tools" / "pylibs"]
    for p in reversed(candidates):
        if p.is_dir():
            sys.path.insert(0, str(p))


def transcript_path(cfg) -> Path:
    """定位本次运行的转写 json（解析顺序与 build_slices.py / build_clean.py / merge_outline.py 一致）。

    1. config.media.transcript 显式指定（相对路径相对 run 根）；
    2. transcripts/<bvid>_p<page>.json —— run_asr.py 的默认产物名；
    3. transcripts/<localAudio|localVideo|audioFile|videoFile 的 stem>.json。
    统计口径只吃 raw ASR，所以这里解析到的就是唯一的统计输入。
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


def count_categories(texts, categories):
    """按类别统计术语出现次数：同类内最长词优先、span 不重复计。"""
    hits = {k: Counter() for k in categories}
    total = {k: 0 for k in categories}
    for text in texts:
        for cat, terms in categories.items():
            ordered = sorted(set(terms), key=len, reverse=True)
            masked = text
            for term in ordered:
                n = masked.count(term)
                if n:
                    hits[cat][term] += n
                    total[cat] += n
                    masked = masked.replace(term, "\u0000" * len(term))
    return total, {k: dict(v.most_common(20)) for k, v in hits.items()}


def jieba_words(texts, stopwords, min_word_length):
    try:
        import jieba  # noqa: F401
    except Exception:
        return None, None
    import jieba
    jieba.setLogLevel(60)
    words = Counter()
    for t in texts:
        for w in jieba.cut(t, cut_all=False):
            w = w.strip()
            if len(w) < min_word_length or w in stopwords:
                continue
            if not HAN.search(w) and not LATIN.search(w):
                continue
            words[w] += 1
    return words, jieba.__version__ if hasattr(jieba, "__version__") else "unknown"


def ngram_words(texts, stopwords, nmin=2, nmax=4, min_count=8):
    """2–4 gram 口头禅：先统计，再剔除被更长且出现次数相近的 n-gram 覆盖的子串。"""
    counters = {}
    for n in range(nmin, nmax + 1):
        c = Counter()
        for t in texts:
            clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", t)
            for i in range(len(clean) - n + 1):
                g = clean[i:i + n]
                if g in stopwords:
                    continue
                c[g] += 1
        counters[n] = c
    merged = Counter()
    for n, c in counters.items():
        for g, v in c.items():
            if v >= min_count:
                merged[g] = v
    # 极大化过滤：若某 n-gram 是更长 n-gram 的子串，且后者次数 >= 前者 * 0.6，则丢掉短的
    items = sorted(merged.items(), key=lambda kv: (-len(kv[0]), -kv[1]))
    kept = []
    for g, v in items:
        covered = False
        for h, hv in kept:
            if len(h) > len(g) and g in h and hv >= v * 0.6:
                covered = True
                break
        if not covered:
            kept.append((g, v))
    kept.sort(key=lambda kv: -kv[1])
    return kept


def rep_collapse(text: str, max_unit: int = 12, min_repeat: int = 4):
    """折叠 Whisper 的复读伪影：同一短串连续重复 >= min_repeat 次只保留一次。

    返回 (折叠后文本, 被移除的字符数, 命中的片段样本)。只用于「敏感性对照」，
    不改写 raw 转写，也不作为任何主口径。
    """
    removed = 0
    samples = []
    for unit_len in range(1, max_unit + 1):
        pattern = re.compile(r"(.{%d})\1{%d,}" % (unit_len, min_repeat - 1))
        def _sub(m):
            nonlocal removed
            unit = m.group(1)
            total = len(m.group(0))
            removed += total - unit_len
            if len(samples) < 8:
                samples.append({"unit": unit, "repeatTimes": total // unit_len})
            return unit
        text = pattern.sub(_sub, text)
    return text, removed, samples


def main():
    cfg, _extra = load()
    stats_cfg = cfg.section("stats")
    pause_min = float(stats_cfg.get("pauseThresholdSeconds") or 1.5)
    min_word_length = int(stats_cfg.get("minWordLength") or 2)
    top_word_count = int(stats_cfg.get("topWordCount") or 200)
    ngram_min = int(stats_cfg.get("ngramMin") or 2)
    ngram_max = int(stats_cfg.get("ngramMax") or 4)
    ngram_min_count = int(stats_cfg.get("ngramMinCount") or 8)
    # 停顿分档的第一档上界跟阈值绑定，阈值改了这个标签也跟着改（默认 1.5-3）。
    bucket_labels = [f"{pause_min:g}-3", "3-10", "10-30", ">=30"]

    stats_dir = cfg.path("stats", mkdir=True)
    figs_dir = stats_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)

    add_pylibs(cfg)

    profile = cfg.profile()
    profile_name = str(profile.get("name") or cfg.section("video").get("profile") or "generic")
    categories = cfg.term_categories()
    stopwords = set(BUILTIN_STOPWORDS) | {str(w) for w in (profile.get("stopwordsExtra") or [])}

    trans = transcript_path(cfg)
    if not trans.exists():
        raise SystemExit(f"找不到转写文件：{trans}\n先跑 scripts/run_asr.py，或检查 config.media 的音源配置")

    data = json.loads(trans.read_text(encoding="utf-8"))
    segs = sorted(data["segments"], key=lambda s: s["start"])
    duration = float(data["duration"])
    texts = [s["text"] for s in segs]

    joined = "".join(texts)
    han = len(HAN.findall(joined))
    latin = len(LATIN.findall(joined))
    non_ws = len(WS.sub("", joined))
    punct = non_ws - han - latin

    # 复读伪影敏感性对照：逐段折叠后再统计一遍
    collapsed_texts = []
    rep_removed = 0
    rep_segments = 0
    rep_samples = []
    for t in texts:
        c, removed, samples = rep_collapse(t)
        collapsed_texts.append(c)
        if removed:
            rep_segments += 1
            rep_removed += removed
            rep_samples.extend(samples)
    c_joined = "".join(collapsed_texts)
    c_han = len(HAN.findall(c_joined))
    c_latin = len(LATIN.findall(c_joined))

    speaking = sum(max(0.0, s["end"] - s["start"]) for s in segs)
    gaps = []
    for a, b in zip(segs, segs[1:]):
        g = b["start"] - a["end"]
        if g > 0:
            gaps.append({"gap": round(g, 3), "afterEnd": round(a["end"], 3), "beforeStart": round(b["start"], 3)})
    pauses = [g for g in gaps if g["gap"] >= pause_min]

    def bucket(g):
        if g < 3:
            return bucket_labels[0]
        if g < 10:
            return "3-10"
        if g < 30:
            return "10-30"
        return ">=30"

    buckets = Counter(bucket(p["gap"]) for p in pauses)
    longest = max(pauses, key=lambda p: p["gap"]) if pauses else None

    # 逐分钟（按重叠比例摊分）
    minutes = int(math.ceil(duration / 60.0))
    per_minute = [{"minute": i, "han": 0.0, "speakingSeconds": 0.0, "pauseCount": 0, "segmentCount": 0} for i in range(minutes)]
    for s in segs:
        seg_han = len(HAN.findall(s["text"]))
        seg_len = max(1e-6, s["end"] - s["start"])
        m0, m1 = int(s["start"] // 60), int(s["end"] // 60)
        for m in range(m0, min(m1, minutes - 1) + 1):
            lo, hi = m * 60.0, (m + 1) * 60.0
            ov = min(s["end"], hi) - max(s["start"], lo)
            if ov <= 0:
                continue
            per_minute[m]["han"] += seg_han * (ov / seg_len)
            per_minute[m]["speakingSeconds"] += ov
            per_minute[m]["segmentCount"] += 1
    for p in pauses:
        m = int(p["beforeStart"] // 60)
        if m < minutes:
            per_minute[m]["pauseCount"] += 1
    for r in per_minute:
        r["han"] = round(r["han"], 1)
        r["hanPerMin"] = round(r["han"], 1)
        r["speakingSeconds"] = round(r["speakingSeconds"], 1)

    # 提问句
    sentences = [x.strip() for x in re.split(r"[。！？!?；;，,\n]", joined) if x.strip()]
    questions = [s for s in sentences if any(q in s for q in QUESTION_MARKS)]

    term_total, term_top = count_categories(texts, categories)

    words, jieba_version = jieba_words(texts, stopwords, min_word_length)
    if words is None:
        word_mode = "fallback-ngram（jieba 不可用）"
        top_words = []
    else:
        word_mode = f"jieba {jieba_version} 精确模式 + 停用词 + 最小词长 {min_word_length}"
        top_words = words.most_common(top_word_count)
    catchphrases = ngram_words(texts, stopwords, nmin=ngram_min, nmax=ngram_max, min_count=ngram_min_count)
    words_c, _ = jieba_words(collapsed_texts, stopwords, min_word_length)
    top_words_collapsed = words_c.most_common(30) if words_c else []

    result = {
        "generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "basis": {"raw": True, "clean": False, "ocr": False,
                  "note": "所有语言统计只使用 raw ASR；术语纠错稿与屏幕 OCR 文本一律不参与统计。"},
        "source": {"transcript": cfg.rel(trans), "profile": profile_name, "durationSeconds": duration,
                   "segmentCount": len(segs), "language": data.get("language")},
        "chars": {"han": han, "latinDigit": latin, "punct": punct, "withPunct": non_ws, "withoutPunct": han + latin},
        "rate": {"hanPerMin": round(han / (duration / 60), 2), "charsPerMin": round((han + latin) / (duration / 60), 2)},
        "speech": {
            "speakingSeconds": round(speaking, 2),
            "speakingRatio": round(speaking / duration, 4),
            "silenceRatio": round(1 - speaking / duration, 4),
            "firstSpeechAtSeconds": round(segs[0]["start"], 2) if segs else None,
            "lastSpeechEndSeconds": round(segs[-1]["end"], 2) if segs else None,
        },
        "pauses": {
            "thresholdSeconds": pause_min,
            "count": len(pauses),
            "buckets": {k: buckets.get(k, 0) for k in bucket_labels},
            "totalPauseSeconds": round(sum(p["gap"] for p in pauses), 1),
            "longest": longest,
            "top5": sorted(pauses, key=lambda p: -p["gap"])[:5],
        },
        "questions": {"count": len(questions), "perMinute": round(len(questions) / (duration / 60), 2),
                      "samples": [q[:60] for q in questions[:15]],
                      "caveat": "断句用 [,，。！？!?；;] 切分；本转写基本只有 ASCII 逗号，几乎没有中文句号，故按子句计。"},
        "repetitionArtifact": {
            "detector": "同一 1–12 字短串连续重复 ≥4 次即折叠（逐 segment 处理，只用于对照）",
            "segmentsAffected": rep_segments,
            "charsRemoved": rep_removed,
            "samples": rep_samples[:8],
            "sensitivity": {
                "raw": {"han": han, "latinDigit": latin, "hanPerMin": round(han / (duration / 60), 2)},
                "collapsed": {"han": c_han, "latinDigit": c_latin, "hanPerMin": round(c_han / (duration / 60), 2)},
                "note": "主口径一律用 raw；collapsed 只用于说明 Whisper 复读伪影对读数的影响幅度。",
            },
            "topWordsCollapsed": [{"word": w, "count": c} for w, c in top_words_collapsed],
        },
        "termHits": {"categories": list(categories), "total": term_total, "topTerms": term_top,
                     "caveat": f"同类内最长词优先、不重复计数；跨类别可重复计；"
                               f"术语表见 profiles/{profile_name}.json 的 terms（config.video.profile={profile_name}）。"},
        "wordFrequency": {"mode": word_mode, "top": [{"word": w, "count": c} for w, c in top_words],
                          "stopwordCount": len(stopwords),
                          "stopwordsBuiltin": len(BUILTIN_STOPWORDS),
                          "stopwordsExtra": len(stopwords) - len(BUILTIN_STOPWORDS)},
        "catchphrases": [{"ngram": g, "count": c} for g, c in catchphrases[:80]],
        "perMinute": per_minute,
    }
    (stats_dir / "language-stats.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with (stats_dir / "per-minute.csv").open("w", encoding="utf-8-sig") as f:
        f.write("minute,time_text,han,han_per_min,speaking_seconds,pause_count,segment_count\n")
        for r in per_minute:
            t = f"{r['minute'] // 60:02d}:{r['minute'] % 60:02d}"
            f.write(f"{r['minute']},{t},{r['han']},{r['hanPerMin']},{r['speakingSeconds']},{r['pauseCount']},{r['segmentCount']}\n")

    with (stats_dir / "top-words.csv").open("w", encoding="utf-8-sig") as f:
        f.write("kind,item,count\n")
        for w, c in top_words:
            f.write(f"word,{w},{c}\n")
        for g, c in catchphrases:
            f.write(f"ngram,{g},{c}\n")
        for cat, terms in term_top.items():
            for t, c in terms.items():
                f.write(f"term:{cat},{t},{c}\n")

    # ---------- 图 ----------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    xs = [r["minute"] for r in per_minute]
    ys = [r["hanPerMin"] for r in per_minute]
    win = 15
    ma = [sum(ys[max(0, i - win + 1):i + 1]) / len(ys[max(0, i - win + 1):i + 1]) for i in range(len(ys))]

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(xs, ys, lw=0.8, color="#9ec5fe", label="每分钟汉字数")
    ax.plot(xs, ma, lw=2.0, color="#1f4e9c", label=f"{win} 分钟移动平均")
    ax.set_xlabel("视频时间（分钟）")
    ax.set_ylabel("汉字 / 分钟")
    ax.set_title("逐分钟语速（raw ASR，按重叠时长摊分）")
    ax.legend()
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(figs_dir / "01-逐分钟语速.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(14, 5))
    cum = []
    total = 0
    for r in per_minute:
        total += r["han"]
        cum.append(total)
    ax.plot(xs, cum, lw=2, color="#1f4e9c")
    ax.set_xlabel("视频时间（分钟）")
    ax.set_ylabel("累计汉字数")
    ax.set_title(f"累计说话量（全程 {han} 字，人均语速 {result['rate']['hanPerMin']} 字/分钟）")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(figs_dir / "02-累计字数.png", dpi=130)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    labels = list(result["pauses"]["buckets"].keys())
    vals = list(result["pauses"]["buckets"].values())
    axes[0].bar(labels, vals, color="#7aa7d9")
    axes[0].set_title(f"停顿时长分布（≥{pause_min:g}s，共 {len(pauses)} 次）")
    axes[0].set_xlabel("停顿档位（秒）")
    axes[0].set_ylabel("次数")
    seg_lens = [s["end"] - s["start"] for s in segs]
    axes[1].hist(seg_lens, bins=30, color="#c4a484")
    axes[1].set_title(f"转写段落时长分布（中位 {sorted(seg_lens)[len(seg_lens) // 2]:.0f}s）")
    axes[1].set_xlabel("段长（秒）")
    axes[1].set_ylabel("段数")
    fig.tight_layout()
    fig.savefig(figs_dir / "03-停顿与段长.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7, 5))
    ax.pie([speaking, duration - speaking], labels=["有人声", "无人声/空白"],
           autopct="%.1f%%", colors=["#1f4e9c", "#e6e6e6"], startangle=90)
    ax.set_title("全程人声占比")
    fig.tight_layout()
    fig.savefig(figs_dir / "04-人声占比.png", dpi=130)
    plt.close(fig)

    print(f"transcript={trans}")
    print(f"han={han} latin={latin} punct={punct} withoutPunct={han + latin}")
    print(f"rate={result['rate']} speakingRatio={result['speech']['speakingRatio']} firstSpeech={result['speech']['firstSpeechAtSeconds']}")
    print(f"pauses={len(pauses)} buckets={result['pauses']['buckets']} longest={longest}")
    print(f"questions={len(questions)} ({result['questions']['perMinute']}/min)")
    print(f"termHits={term_total}")
    print(f"wordMode={word_mode} top10={top_words[:10]}")
    print(f"catchphrases={catchphrases[:15]}")
    print(f"out={stats_dir}")


if __name__ == "__main__":
    main()
