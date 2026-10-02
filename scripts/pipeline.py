"""video-dossier 一条龙调度器。

把能自动化的阶段按顺序跑完，并在**必须由人/子代理介入**的阶段显式停下，
告诉你下一步该做什么（而不是假装跑完了）。

用法：
    python pipeline.py                                  # 跑到第一个需要介入的阶段
    python pipeline.py --stages collect,asr,slices      # 只跑指定阶段
    python pipeline.py --stages all --continue-on-error # 跑到底（失败也继续）
    python pipeline.py --status                         # 只看产物到哪一步了
    python pipeline.py --dry-run                        # 只打印将执行的命令
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from vd_config import load  # noqa: E402

PY = sys.executable
NODE = shutil.which("node") or "node"
SKILL = HERE.parent

# 阶段定义：auto=可自动化（cmd），manual=必须人/子代理介入
STAGES: dict[str, dict] = {
    "collect": {
        "desc": "采集平台数据（B 站匿名接口）",
        "cmd": [NODE, str(HERE / "fetch_meta.mjs")],
        "only_bilibili": True,
        "produces": ["stats/video-meta.json"],
    },
    "asr": {
        "desc": "语音转写（faster-whisper）",
        "cmd": [PY, str(HERE / "run_asr.py")],
        "produces": ["transcripts"],
    },
    "slices": {
        "desc": "把转写切成带时间戳的窗口切片",
        "cmd": [PY, str(HERE / "build_slices.py")],
        "produces": ["content/_slices/index.json"],
    },
    "outline": {
        "desc": "派子代理产出两级大纲（你必须亲自派）",
        "manual": True,
        "produces": ["content/_outline"],
    },
    "merge": {
        "desc": "合并切片结果成两级大纲 + 挂屏幕证据",
        "cmd": [PY, str(HERE / "merge_outline.py")],
        "produces": ["content/outline.json"],
    },
    "corrections": {
        "desc": "生成纠错候选 + 裁决表（裁决需人工闸门）",
        "cmd": [PY, str(HERE / "build_corrections.py")],
        "manual_after": True,
        "produces": ["content/corrections-reviewed.json"],
    },
    "clean": {
        "desc": "确定性替换生成可读稿（长度守恒校验）",
        "cmd": [PY, str(HERE / "build_clean.py")],
        "produces": ["transcripts"],
    },
    "stats": {
        "desc": "语言/节奏/词频统计 + 图表",
        "cmd": [PY, str(HERE / "build_stats.py")],
        "produces": ["stats/language-stats.json"],
    },
    "slides": {
        "desc": "画面层：抽帧 → 去重 → OCR → 屏幕状态（最慢的一步）",
        "cmd": [PY, str(HERE / "extract_slides.py"), "--stage", "all"],
        "optional": True,
        "produces": ["slides/ocr-quality.json"],
    },
    "verify": {
        "desc": "全量断言校验",
        "cmd": [PY, str(HERE / "verify.py")],
        "produces": [],
    },
    "report": {
        "desc": "写主报告（你亲自写，照 references/报告模板.md）",
        "manual": True,
        "produces": [],
    },
    "pdf": {
        "desc": "转 PDF（复用 lov-any2pdf 的 md2pdf.py）",
        "cmd": [PY, str(HERE / "make_pdf.py")],
        "produces": [],
    },
}

ORDER = ["collect", "asr", "slices", "outline", "merge", "corrections", "clean",
         "stats", "slides", "verify", "report", "pdf"]


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def manual_block(name: str, cfg) -> None:
    hr(f"[需要你介入] {name}")
    if name == "outline":
        print(f"""这一步脚本做不了：要读转写做判断。做法：

1. 看切片：{cfg.path('content') / '_slices' / 'index.json'}
2. 照 {SKILL / 'references' / '子代理任务书.md'} 写任务书，**每片派一个子代理**（并发 ≤4）
3. 铁律：代理必须用 write 把结果写到 {cfg.path('content') / '_outline'}/<slice-id>.json，
   只回一行 `DONE <file> n=<条数>`；不要让大 JSON 走返回值通道（会静默截断）
4. 全部回来后再跑：python pipeline.py --stages merge""")
    elif name == "report":
        print(f"""报告要你亲自写（脚本不知道什么值得说）。做法：

1. 结构照 {SKILL / 'references' / '报告模板.md'}
2. 口径照 {SKILL / 'references' / '口径模板.md'} 写进 {cfg.root / 'README.md'}
3. 文件名：报告-<视频标题>-数据与内容.md（pdf 阶段会自动找它）
4. 写完跑：python pipeline.py --stages pdf""")
    print()


def status(cfg) -> None:
    hr(f"产物状态 · {cfg.root}")
    for name in ORDER:
        st = STAGES[name]
        marks = []
        for rel in st.get("produces", []):
            p = cfg.root / rel
            marks.append(("OK " if p.exists() else "-- ") + rel)
        tag = "手动" if st.get("manual") else "自动"
        print(f"  {name:12s} [{tag}] {st['desc']}")
        for m in marks:
            print(f"      {m}")
    print("\n提示：大文件（音轨/视频）在 audio/ video/ 下，确认不再需要就可以删。")


def run_stage(name: str, cfg, dry: bool) -> int:
    st = STAGES[name]
    if st.get("only_bilibili") and cfg.media.get("kind", "bilibili") != "bilibili":
        print(f"[skip] {name}：local 模式没有平台数据")
        return 0
    cmd = st.get("cmd")
    if not cmd:
        manual_block(name, cfg)
        return 0
    full = cmd + ["--config", str(cfg.config_path)]
    hr(f"[{name}] {st['desc']}")
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in full))
    if dry:
        return 0
    t0 = time.time()
    proc = subprocess.run(full, cwd=str(cfg.root))
    dt = time.time() - t0
    print(f"[{name}] 退出码 {proc.returncode}，用时 {dt:.1f}s")
    if proc.returncode != 0:
        print(f"[{name}] 失败。先看上面的输出；上游产物不齐时下游会明确报错，不要跳过它继续。")
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description="video-dossier 一条龙调度器")
    ap.add_argument("--config", default=None)
    ap.add_argument("--stages", default="all",
                    help="逗号分隔的阶段名，或 all（默认跑到第一个需要介入的阶段）")
    ap.add_argument("--skip", default="", help="逗号分隔要跳过的阶段")
    ap.add_argument("--continue-on-error", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true", help="只打印产物状态")
    args = ap.parse_args()

    cfg, _ = load(args.config)

    if args.status:
        status(cfg)
        return 0

    wanted = ORDER if args.stages.strip() == "all" else [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in STAGES]
    if unknown:
        print(f"未知阶段：{unknown}\n可用：{', '.join(ORDER)}")
        return 2
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    hr(f"video-dossier · {cfg.title}")
    print(f"run 根目录 : {cfg.root}")
    print(f"配置       : {cfg.config_path}")
    print(f"域 profile : {cfg.section('video').get('profile', 'generic')}")
    print(f"媒体       : {json.dumps(cfg.media, ensure_ascii=False)}")
    print(f"阶段       : {', '.join(wanted)}")

    failed: list[str] = []
    for name in wanted:
        if name in skip:
            print(f"[skip] {name}（--skip 指定）")
            continue
        # all 模式下遇到手动阶段就停下等人
        if args.stages.strip() == "all" and STAGES[name].get("manual"):
            manual_block(name, cfg)
            print("→ 完成上面这一步后，继续：python pipeline.py --stages "
                  + ",".join(ORDER[ORDER.index(name) + 1:]))
            break
        rc = run_stage(name, cfg, args.dry_run)
        if rc != 0:
            failed.append(name)
            if not args.continue_on_error:
                print(f"\n在阶段 {name} 停下。修好再跑，或用 --continue-on-error 强行继续（不推荐）。")
                return rc
        if STAGES[name].get("manual_after"):
            manual_block("corrections-review", cfg)
            print(f"→ 裁决完 {cfg.path('content') / 'corrections-decisions.json'} 后继续。")

    hr("结束")
    if failed:
        print(f"失败阶段：{', '.join(failed)}")
        return 1
    print("所选阶段全部完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
