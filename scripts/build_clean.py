"""用**已裁决**的纠错表做确定性替换，产出术语级修正可读稿。

原则（写进 README 口径）：
- 只用 `{content}/corrections-reviewed.json` 里 approved + phraseRules 的条目，做纯字符串替换，
  **不改句意、不删句、不重写**；因此 clean 稿完全可复现、可审计。
- 被拒绝的候选（含理由）也写进 `{content}/corrections.jsonl`，便于复核「为什么没改这里」；
  若裁决表里有 `needsReview`（例如本机没有 jieba 词典），同样逐条写进该日志。
- 统计一律不用 clean 稿；本脚本做长度守恒校验，证明「只替换、没重写」：
  Σ(新串长-旧串长)×命中次数 必须等于 clean 与 raw 的总长度差，不相等即说明发生了预期外改写，
  此时**不产出 clean 稿**，只写报告与日志并以非零退出码结束。
- 按主题组织时读 `{content}/outline.json`（由 merge_outline.py 产出）；不存在则按时间顺序直排。

产物：`{transcripts}/{stem}.raw.txt`、`{transcripts}/{stem}.clean.md`、`{content}/corrections.jsonl`、
`{stats}/clean-vs-raw.json`
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from vd_config import load

APPLIED_NOTE = ("本稿只做术语级字符串替换（错字→正确术语），不改句意、不删句、不重写；"
                "替换表来自子代理逐片错字清点 + 人工裁决闸门（拒绝条目含理由），"
                "逐条记录在 corrections.jsonl。**本稿不参与任何统计**。")


def hhmmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


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
    content_dir = cfg.path("content")
    trans_path = resolve_transcript(cfg)
    reviewed_path = content_dir / "corrections-reviewed.json"
    outline_path = content_dir / "outline.json"
    corr_log = content_dir / "corrections.jsonl"
    stats_dir = cfg.path("stats", mkdir=True)

    if not reviewed_path.exists():
        raise SystemExit(f"找不到裁决表 {reviewed_path}；请先运行 build_corrections.py。")
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))

    data = json.loads(trans_path.read_text(encoding="utf-8"))
    segs = sorted(data["segments"], key=lambda s: s["start"])
    duration = float(data.get("duration") or 0)

    # 短语规则优先（更长上下文先落），随后是单词级替换（长串优先，避免短串吃掉长串）
    rules = []
    for p in reviewed.get("phraseRules") or []:
        rules.append({"wrong": p["wrong"], "right": p["right"], "guard": None, "kind": "phrase"})
    for a in sorted(reviewed.get("approved") or [], key=lambda x: -len(x["wrong"])):
        if a["wrong"] == a["right"]:
            continue
        rules.append({"wrong": a["wrong"], "right": a["right"],
                      "guard": a.get("notFollowedBy"), "kind": "word"})

    # 同一错串只保留优先级最高的一条（短语规则在前）：后一条在前一条替换后已无命中，
    # 保留会让「命中次数 × 长度差」重复入账，破坏长度守恒对账。
    deduped = {}
    for r in rules:
        if r["wrong"] in deduped:
            print(f"警告：错串「{r['wrong']}」有 {deduped[r['wrong']]['kind']} 与 {r['kind']} 两条规则，"
                  f"只保留先出现的（短语规则优先）")
            continue
        deduped[r["wrong"]] = r
    rules = list(deduped.values())

    raw_len = sum(len(s["text"]) for s in segs)
    clean_segs = []
    hits = {r["wrong"]: 0 for r in rules}
    for s in segs:
        t = s["text"]
        for r in rules:
            w, right = r["wrong"], r["right"]
            if r["guard"]:
                pat = re.compile(re.escape(w) + "(?!" + re.escape(r["guard"]) + ")")
                n = len(pat.findall(t))
                if n:
                    t = pat.sub(right, t)
            else:
                n = t.count(w)
                if n:
                    t = t.replace(w, right)
            hits[w] += n
        clean_segs.append({"start": s["start"], "end": s["end"], "text": t})

    clean_len = sum(len(s["text"]) for s in clean_segs)
    expected_delta = sum(hits[r["wrong"]] * (len(r["right"]) - len(r["wrong"])) for r in rules)
    actual_delta = clean_len - raw_len
    conserved = actual_delta == expected_delta
    applied = [r for r in rules if hits[r["wrong"]]]

    # ---- 逐条纠错日志：applied / rejected / needs-review 全写，便于复核「为什么改了/没改」----
    rejected = reviewed.get("rejected") or []
    needs_review = reviewed.get("needsReview") or []
    corr_log.parent.mkdir(parents=True, exist_ok=True)
    with corr_log.open("w", encoding="utf-8") as f:
        for r in rules:
            if hits[r["wrong"]]:
                f.write(json.dumps({
                    "status": "applied", "wrong": r["wrong"], "right": r["right"],
                    "kind": r["kind"], "guard": r["guard"], "occurrences": hits[r["wrong"]],
                }, ensure_ascii=False) + "\n")
        for rj in rejected:
            f.write(json.dumps({
                "status": "rejected", "wrong": rj["wrong"], "candidateRight": rj.get("candidateRight"),
                "occurrences": rj.get("occurrences"), "reason": rj.get("reason"),
            }, ensure_ascii=False) + "\n")
        for nr in needs_review:
            f.write(json.dumps({
                "status": "needs-review", "wrong": nr["wrong"], "candidateRight": nr.get("candidateRight"),
                "occurrences": nr.get("occurrences"), "reason": nr.get("reason"),
            }, ensure_ascii=False) + "\n")

    raw_txt = trans_path.with_suffix(".raw.txt")
    with raw_txt.open("w", encoding="utf-8") as f:
        for s in segs:
            f.write(f"[{hhmmss(s['start'])}] {s['text'].strip()}\n")

    report = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runName": cfg.get("name") or cfg.title,
        "transcriptSource": cfg.rel(trans_path),
        "tableSource": cfg.rel(reviewed_path),
        "rawChars": raw_len,
        "cleanChars": clean_len,
        "charDelta": actual_delta,
        "charDeltaExpected": expected_delta,
        "lengthConserved": conserved,
        "replacements": sum(hits.values()),
        "appliedRuleCount": len(applied),
        "approvedRuleCount": len(reviewed.get("approved") or []) + len(reviewed.get("phraseRules") or []),
        "rejectedCount": reviewed.get("rejectedCount", len(rejected)),
        "rejectedOccurrences": reviewed.get("rejectedOccurrences",
                                           sum(r.get("occurrences") or 0 for r in rejected)),
        "needsReviewCount": reviewed.get("needsReviewCount", len(needs_review)),
        "topApplied": sorted(
            ({"wrong": r["wrong"], "right": r["right"], "occurrences": hits[r["wrong"]]} for r in applied),
            key=lambda x: -x["occurrences"])[:25],
        "notes": [
            "长度守恒校验：Σ(新串长-旧串长)×命中次数 必须等于 clean 与 raw 的总长度差；不相等即说明发生了预期外改写。",
            "替换为纯字符串操作（含 notFollowedBy 护栏），不含模型改写，故 clean 稿可完全复现。",
            "统计一律使用 raw；clean 稿仅供人读。",
        ],
    }
    stats_dir.mkdir(parents=True, exist_ok=True)
    report_path = stats_dir / "clean-vs-raw.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"rules={len(rules)} appliedRules={len(applied)} replacements={sum(hits.values())}")
    print(f"rawChars={raw_len} cleanChars={clean_len} delta={actual_delta} "
          f"expected={expected_delta} conserved={conserved}")
    print(f"rejected={report['rejectedCount']} ({report['rejectedOccurrences']} 次未改)  "
          f"needsReview={report['needsReviewCount']}")
    print(f"wrote {cfg.rel(raw_txt)} / {cfg.rel(corr_log)} / {cfg.rel(report_path)}")

    if not conserved:
        print("长度守恒校验失败：clean 稿与 raw 的字符差与替换表预期不一致，说明发生了预期外改写。", file=sys.stderr)
        print("已写出报告与纠错日志，但**不产出 clean 稿**；请核对 corrections-reviewed.json。", file=sys.stderr)
        raise SystemExit(1)

    # ---- clean 稿 ----
    lines = [f"# {cfg.title} · 逐字稿（术语级修正可读稿）", "",
             f"- 源：`{source_desc(cfg)}`，时长 {hhmmss(duration)}",
             f"- 替换规则：{APPLIED_NOTE}",
             f"- 实际替换 {sum(hits.values())} 处、命中 {len(applied)} 条规则；"
             f"另有 {report['rejectedCount']} 条候选被保守拒绝（{report['rejectedOccurrences']} 次未改）",
             "", "---", ""]
    if outline_path.exists():
        outline = json.loads(outline_path.read_text(encoding="utf-8"))
        used = set()
        for th in outline["themes"]:
            lines.append(f"## {th['id']} {th['title']}（{th['startText']}–{th['endText']}）")
            lines.append("")
            for i, s in enumerate(clean_segs):
                if th["startSeconds"] <= s["start"] <= th["endSeconds"] + 1:
                    used.add(i)
                    lines.append(f"**[{hhmmss(s['start'])}]** {s['text'].strip()}")
                    lines.append("")
        lines.append("## 附：未落入任何主题的转写行")
        lines.append("")
        for i, s in enumerate(clean_segs):
            if i not in used:
                lines.append(f"**[{hhmmss(s['start'])}]** {s['text'].strip()}")
                lines.append("")
    else:
        for s in clean_segs:
            lines.append(f"**[{hhmmss(s['start'])}]** {s['text'].strip()}")
            lines.append("")
    clean_md = trans_path.with_suffix(".clean.md")
    clean_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {cfg.rel(clean_md)}（主题组织：{'outline.json' if outline_path.exists() else '按时间顺序'}）")


if __name__ == "__main__":
    main()
