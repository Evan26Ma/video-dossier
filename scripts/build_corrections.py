"""生成「纠错裁决表」：把子代理的错字线索按可审计规则裁决为 采纳 / 拒绝。

裁决规则（通用规则写在代码里；学科相关裁决一律外置到数据文件）：
1. 通用规则：用 jieba 词典判断「错串本身是不是真词」。
   - 不是真词 → 默认采纳（ASR 生造串，替换不会误伤）。
   - 是真词   → 默认拒绝（替换可能把正确用词改错）。
   - jieba 缺失（未安装 / 找不到词典）时**不崩**：所有未被人工裁决覆盖的候选一律标记为
     `needsReview`（需人工裁决），绝不默认采纳，也就不会静默改稿。
2. 人工裁决外置在 `{content}/corrections-decisions.json`（可选，不存在视为空裁决）：
   - `approve`     逐条采纳（错串是真词但语境确凿），可给 `right` / `notFollowedBy` / `reason`
   - `reject`      逐条拒绝并写明理由
   - `override`    修正子代理给错的目标串（例：`4D` 语境是选项 D，代理写成了 AD）
   - `phraseRules` 真词的短语级替换（用更长上下文，不会误伤）
   - `letterNoise` 选项字母类噪声（保守不替换），可选
   脚本里不再出现任何学科词，换学科只改这个数据文件。
3. 输出 `{content}/corrections-reviewed.json`，供 build_clean.py 确定性应用。

候选来源：`{content}/_terms/slice-*.json` 的 `terms: [{wrong, right}]`（子代理逐片清点）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from vd_config import load

# 词典缺失时降级为「全部需人工裁决」，不中断流水线。
# 注意：必须在 import jieba 之前把 tools/pylibs 加进 sys.path，否则随仓库带的离线
# jieba（tools/pylibs/jieba）会找不到——build_stats.py 有同样的引导逻辑，两处必须一致。
jieba = None
JIEBA_ERROR = "尚未尝试导入"


def _pylibs_dirs(cfg=None) -> list:
    dirs = [Path(__file__).resolve().parent.parent / "tools" / "pylibs"]
    if cfg is not None:
        dirs.append(cfg.root / "tools" / "pylibs")
    return [d for d in dirs if d.is_dir()]


def import_jieba(cfg=None):
    """把 tools/pylibs 插进 sys.path 后再尝试 import jieba（仓库根与 run 根都找）。"""
    global jieba, JIEBA_ERROR
    for d in _pylibs_dirs(cfg):
        s = str(d)
        if s not in sys.path:
            sys.path.insert(0, s)
    try:
        import jieba as _jieba
        jieba = _jieba
        JIEBA_ERROR = None
    except Exception as exc:  # pragma: no cover - 取决于本机环境
        jieba = None
        JIEBA_ERROR = repr(exc)
    return jieba

DECISIONS_NAME = "corrections-decisions.json"
REVIEWED_NAME = "corrections-reviewed.json"


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


def resolve_transcript(cfg) -> Path:
    """解析本次运行的转写 json。

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
        raise SystemExit(f"{tdir} 下找不到转写 json；请先跑 ASR，或在 config.media.transcript 里指定。")
    raise SystemExit(
        f"{tdir} 下有多个转写 json（{'、'.join(p.name for p in files[:8])}）；"
        f"请在 config.media.transcript 里指定本次使用哪一个。"
    )


def jieba_words() -> tuple[set[str] | None, str]:
    """返回 (词典词集合, 不可用原因)；词典路径从 jieba 包自身推导，不写死绝对路径。"""
    if jieba is None:
        return None, f"jieba 不可用（{JIEBA_ERROR}）"
    dictpath = Path(jieba.__file__).parent / "dict.txt"
    if not dictpath.exists():
        return None, f"未找到 jieba 词典文件 {dictpath.name}"
    words = set()
    for line in dictpath.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            words.add(parts[0])
    return (words or None), ("词典为空" if not words else "")


def as_items(v) -> list[dict]:
    """容忍多种写法：对象列表 / 字符串列表 / {错串: 目标串} / {错串: [目标串, 理由]}。"""
    if v is None:
        return []
    items: list[dict] = []
    if isinstance(v, dict):
        for k, val in v.items():
            if isinstance(val, str):
                items.append({"wrong": k, "right": val})
            elif isinstance(val, dict):
                items.append({**val, "wrong": val.get("wrong") or k})
            elif isinstance(val, (list, tuple)) and val:
                d = {"wrong": k, "right": str(val[0])}
                if len(val) > 1:
                    d["reason"] = str(val[1])
                items.append(d)
    elif isinstance(v, (list, tuple)):
        for it in v:
            if isinstance(it, str):
                items.append({"wrong": it})
            elif isinstance(it, dict):
                items.append(dict(it))
    return [it for it in items if str(it.get("wrong") or "").strip()]


def load_decisions(cfg) -> tuple[Path, dict, dict, dict, dict, set, list[dict]]:
    """读外置裁决文件。返回 (路径, approve, reject, override, letterNoise, phraseRules)。"""
    path = cfg.path("content") / DECISIONS_NAME
    if not path.exists():
        return path, {}, {}, {}, set(), []
    dec = json.loads(path.read_text(encoding="utf-8"))

    approve = {}
    for it in as_items(dec.get("approve")):
        w = str(it["wrong"]).strip()
        approve[w] = {
            "right": str(it["right"]).strip() if it.get("right") else None,
            "reason": str(it.get("reason") or "").strip(),
            "notFollowedBy": str(it["notFollowedBy"]).strip() if it.get("notFollowedBy") else None,
        }
    reject = {}
    for it in as_items(dec.get("reject")):
        reject[str(it["wrong"]).strip()] = str(it.get("reason") or "人工裁决拒绝").strip()
    override = {}
    for it in as_items(dec.get("override")):
        override[str(it["wrong"]).strip()] = {
            "right": str(it.get("right") or "").strip(),
            "reason": str(it.get("reason") or "人工裁决修正目标串").strip(),
        }
    noise = {str(x).strip() for x in (dec.get("letterNoise") or []) if str(x).strip()}
    phrases = []
    for it in as_items(dec.get("phraseRules")):
        w = str(it["wrong"]).strip()
        r = str(it.get("right") or "").strip()
        if w and r and w != r:
            phrases.append({"wrong": w, "right": r, "reason": str(it.get("reason") or "").strip()})
    return path, approve, reject, override, noise, phrases


def main() -> None:
    cfg, _ = load(argv=sys.argv[1:])
    import_jieba(cfg)
    content_dir = cfg.path("content")
    terms_dir = content_dir / "_terms"
    trans = resolve_transcript(cfg)
    reviewed_path = content_dir / REVIEWED_NAME
    decisions_path, approve, reject, override, noise, phrase_rules = load_decisions(cfg)

    words, dict_note = jieba_words()
    dictionary_ok = words is not None

    # ---- 候选汇总（同串多目标时按票数取胜出目标；平票取先出现者，与实跑口径一致）----
    votes: dict[str, dict[str, int]] = {}
    for f in sorted(terms_dir.glob("slice-*.json")) if terms_dir.exists() else []:
        obj = json.loads(f.read_text(encoding="utf-8"))
        for t in obj.get("terms", []) or []:
            if not isinstance(t, dict):
                continue
            w = str(t.get("wrong", "")).strip()
            r = str(t.get("right", "")).strip()
            if not w or not r or w == r:
                continue
            votes.setdefault(w, {})
            votes[w][r] = votes[w].get(r, 0) + 1

    if not votes:
        print(f"警告：{cfg.rel(terms_dir)} 下没有可用候选（terms: [{{wrong, right}}]），产物将是空裁决表")

    segs = json.loads(trans.read_text(encoding="utf-8"))["segments"]
    text = "".join(s["text"] for s in segs)

    approved, rejected, needs_review, phrase = [], [], [], []
    used_approve, used_reject, used_override, used_phrase = set(), set(), set(), set()

    for w, rights in sorted(votes.items()):
        occ = text.count(w)
        if occ == 0:
            continue
        best = max(rights.items(), key=lambda kv: kv[1])
        right, nvotes = best
        reason_source = f"子代理投票 {nvotes} 次"

        if w in override:
            ov = override[w]
            used_override.add(w)
            approved.append({"wrong": w, "right": ov["right"] or right, "occurrences": occ, "votes": nvotes,
                             "basis": "人工裁决（decisions.override）：" + ov["reason"]})
            continue
        if w in reject:
            used_reject.add(w)
            rejected.append({"wrong": w, "candidateRight": right, "occurrences": occ, "votes": nvotes,
                             "reason": reject[w]})
            continue
        if w in noise:
            rejected.append({"wrong": w, "candidateRight": right, "occurrences": occ, "votes": nvotes,
                             "reason": "选项字母类噪声，语义不确定，保守不替换"})
            continue
        if w in approve:
            used_approve.add(w)
            ap = approve[w]
            entry = {"wrong": w, "right": ap["right"] or right, "occurrences": occ, "votes": nvotes,
                     "basis": "人工裁决（decisions.approve）：" + (ap["reason"] or "语境确凿，逐条采纳")}
            if ap["notFollowedBy"]:
                entry["notFollowedBy"] = ap["notFollowedBy"]
            approved.append(entry)
            continue
        if not dictionary_ok:
            needs_review.append({"wrong": w, "candidateRight": right, "occurrences": occ, "votes": nvotes,
                                 "reason": f"无法判断错串是否为词典真词（{dict_note}），需人工裁决"})
            continue
        if w not in words:
            approved.append({"wrong": w, "right": right, "occurrences": occ, "votes": nvotes,
                             "basis": f"错串不是词典词（默认采纳）；{reason_source}"})
            continue
        rejected.append({"wrong": w, "candidateRight": right, "occurrences": occ, "votes": nvotes,
                         "reason": "错串本身是词典真词，语境不足以判定，保守不替换"})

    for p in phrase_rules:
        c = text.count(p["wrong"])
        if c:
            used_phrase.add(p["wrong"])
            entry = {"wrong": p["wrong"], "right": p["right"], "occurrences": c,
                     "basis": "短语级替换：用更长上下文避开真词误伤"}
            if p["reason"]:
                entry["basis"] += "；" + p["reason"]
            phrase.append(entry)

    approved.sort(key=lambda x: -x["occurrences"])
    phrase.sort(key=lambda x: -x["occurrences"])
    rejected.sort(key=lambda x: -x["occurrences"])
    needs_review.sort(key=lambda x: -x["occurrences"])

    unused = {
        "approve": sorted(set(approve) - used_approve),
        "reject": sorted(set(reject) - used_reject),
        "override": sorted(set(override) - used_override),
        "phraseRules": sorted({p["wrong"] for p in phrase_rules} - used_phrase),
        "letterNoise": sorted(noise - {r["wrong"] for r in rejected}),
    }
    unused = {k: v for k, v in unused.items() if v}

    payload = {
        "schemaVersion": 1,
        "basis": f"候选来自各切片子代理逐片错字清点（{cfg.rel(terms_dir)}/slice-*.json）；"
                 f"通用规则见 scripts/build_corrections.py 头部注释，学科裁决见 {cfg.rel(decisions_path)}",
        "decisionRules": {
            "default": "错串不是 jieba 词典词 → 采纳；是词典词 → 拒绝（除非人工在 decisions 里逐条确认）",
            "dictionaryAvailable": dictionary_ok,
            "dictionaryNote": dict_note or "jieba 词典可用",
            "decisionsSource": cfg.rel(decisions_path) if decisions_path.exists() else None,
            "manualApprovedHighRisk": sorted(approve),
            "manualRejected": sorted(reject),
            "overrides": {k: v["right"] for k, v in override.items()},
            "letterNoise": sorted(noise),
            "phraseRuleSource": [{"wrong": p["wrong"], "right": p["right"]} for p in phrase_rules],
        },
        "decisionsUnused": unused,
        "approvedCount": len(approved),
        "approvedOccurrences": sum(a["occurrences"] for a in approved),
        "phraseRuleCount": len(phrase),
        "phraseOccurrences": sum(p["occurrences"] for p in phrase),
        "rejectedCount": len(rejected),
        "rejectedOccurrences": sum(r["occurrences"] for r in rejected),
        "needsReviewCount": len(needs_review),
        "needsReviewOccurrences": sum(n["occurrences"] for n in needs_review),
        "approved": approved,
        "phraseRules": phrase,
        "rejected": rejected,
        "needsReview": needs_review,
    }
    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
    reviewed_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"approved={len(approved)} ({payload['approvedOccurrences']} 次)  "
          f"phrase={len(phrase)} ({payload['phraseOccurrences']} 次)  "
          f"rejected={len(rejected)} ({payload['rejectedOccurrences']} 次)  "
          f"needsReview={len(needs_review)} ({payload['needsReviewOccurrences']} 次)")
    if not dictionary_ok:
        print(f"注意：词典不可用（{dict_note}），未人工裁决的候选全部标记为 needsReview，不会自动改稿")
    if unused:
        print(f"decisions 里未被命中的条目（可能是拼写错误或本次转写不含该串）：{unused}")
    print("top approved:")
    for a in approved[:12]:
        print(f"   {a['occurrences']:3d}x {a['wrong']} -> {a['right']}")
    print("top rejected:")
    for r in rejected[:8]:
        print(f"   {r['occurrences']:3d}x {r['wrong']} (候选 {r['candidateRight']}) — {r['reason'][:50]}")
    print(f"wrote {cfg.rel(reviewed_path)}")


if __name__ == "__main__":
    main()
