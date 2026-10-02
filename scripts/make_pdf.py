"""把本次 run 的主报告 markdown 转成 PDF。

本脚本**不自己实现 PDF 生成**：它只负责定位第三方脚本 lov-any2pdf 的 `md2pdf.py`、
把 config.pdf 里的排版选项透传成 CLI、并把命令行原样打印出来便于复现。
找不到 md2pdf.py 时给出清晰安装提示并以退出码 2 退出（绝不静默失败）。

定位顺序（先命中先用）：
  1. config.pdf.md2pdf（相对路径按 run 根解析）
  2. 环境变量 VD_MD2PDF
  3. %DSH_AGENTS_HOME%/skills/lov-any2pdf/scripts/md2pdf.py
  4. ~/.agents/skills/lov-any2pdf/scripts/md2pdf.py
  5. ~/.dsh/skills/lov-any2pdf/scripts/md2pdf.py

用法：
  python scripts/make_pdf.py --config <run>/config.json [--input 报告-xxx.md] [--output xxx.pdf]

说明：按 lov-any2pdf 技能的约定，主题/扉页/水印等选项应先在交互中与使用者确认；
本脚本是流水线里的非交互一步，只负责把确认好的选项（写在 config.pdf 里）透传下去。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from vd_config import load

SKILL_REL = Path("skills") / "lov-any2pdf" / "scripts" / "md2pdf.py"

# 配置键 -> md2pdf.py CLI 参数；字符串/数字原样透传，布尔转 true/false
STR_OPTS = {
    "title": "--title", "subtitle": "--subtitle", "author": "--author",
    "date": "--date", "version": "--version", "watermark": "--watermark",
    "theme": "--theme", "themeFile": "--theme-file", "pageSize": "--page-size",
    "frontispiece": "--frontispiece", "banner": "--banner",
    "headerTitle": "--header-title", "footerLeft": "--footer-left",
    "statsLine": "--stats-line", "statsLine2": "--stats-line2",
    "editionLine": "--edition-line", "disclaimer": "--disclaimer",
    "copyright": "--copyright", "codeMaxLines": "--code-max-lines",
}
BOOL_OPTS = {"cover": "--cover", "toc": "--toc"}


def find_md2pdf(cfg) -> Path | None:
    cands: list[Path] = []
    conf = cfg.section("pdf").get("md2pdf")
    if conf:
        p = Path(str(conf))
        cands.append(p if p.is_absolute() else cfg.root / p)
    env = os.environ.get("VD_MD2PDF")
    if env:
        cands.append(Path(env))
    agents_home = os.environ.get("DSH_AGENTS_HOME")
    if agents_home:
        cands.append(Path(agents_home) / SKILL_REL)
    home = Path.home()
    cands.append(home / ".agents" / SKILL_REL)
    cands.append(home / ".dsh" / SKILL_REL)
    for c in cands:
        if c.is_file():
            print(f"md2pdf  : {c}")
            return c
    return None


def install_hint() -> str:
    return ("找不到 md2pdf.py（lov-any2pdf 技能的转换脚本）。请任选一种方式解决：\n"
            "  1) 把 lov-any2pdf 技能放到 <agents home>/skills/lov-any2pdf/；\n"
            "  2) 直接指定脚本路径：config.pdf.md2pdf = \"<绝对路径>/scripts/md2pdf.py\"；\n"
            "  3) 设环境变量 VD_MD2PDF=<绝对路径>/scripts/md2pdf.py。\n"
            "依赖：pip install reportlab（公式渲染可选 pip install matplotlib）。")


def find_input(cfg, override: str | None = None) -> Path:
    conf = override or cfg.section("pdf").get("input")
    if conf:
        p = Path(str(conf))
        p = p if p.is_absolute() else cfg.root / p
        if not p.is_file():
            raise SystemExit(f"config.pdf.input 指向的文件不存在：{p}")
        return p
    # 默认：run 根目录下唯一的「报告-*.md」
    found = sorted(p for p in cfg.root.glob("报告-*.md") if p.is_file())
    if not found:
        raise SystemExit(f"在 run 根目录找不到报告 md：{cfg.root}\\报告-*.md\n"
                         f"请用 config.pdf.input 指定，或命令行 --input 指定。")
    if len(found) > 1:
        names = "、".join(p.name for p in found)
        raise SystemExit(f"run 根目录有多个「报告-*.md」，无法自动判定：{names}\n"
                         f"请用 config.pdf.input 或命令行 --input 指定唯一输入。")
    return found[0]


def truthy(v) -> str:
    if isinstance(v, str):
        return "false" if v.strip().lower() in ("", "0", "false", "no", "off") else "true"
    return "true" if v else "false"


def build_cmd(md2pdf: Path, pdf: dict, src: Path, dst: Path, py: str) -> list[str]:
    cmd = [py, str(md2pdf), "--input", str(src), "--output", str(dst)]
    for key, flag in STR_OPTS.items():
        v = pdf.get(key)
        if v not in (None, ""):
            cmd += [flag, str(v)]
    for key, flag in BOOL_OPTS.items():
        if pdf.get(key) is not None:
            cmd += [flag, truthy(pdf.get(key))]
    for extra in (pdf.get("extraArgs") or []):
        cmd.append(str(extra))
    return cmd


def main() -> int:
    cfg, rest = load(argv=sys.argv[1:])
    ap = argparse.ArgumentParser(prog="make_pdf.py")
    ap.add_argument("--input", default=None, help="覆盖 config.pdf.input")
    ap.add_argument("--output", default=None, help="覆盖输出 PDF 路径")
    args, unknown = ap.parse_known_args(rest)
    if unknown:
        print(f"忽略未知参数：{unknown}")
    if args.input:
        print(f"input 覆盖: {args.input}")

    md2pdf = find_md2pdf(cfg)
    if md2pdf is None:
        print(install_hint(), file=sys.stderr)
        return 2

    src = find_input(cfg, args.input)
    pdf = cfg.section("pdf")
    out_conf = args.output or pdf.get("output")
    if out_conf:
        dst = Path(str(out_conf))
        dst = dst if dst.is_absolute() else cfg.root / dst
    else:
        dst = src.with_suffix(".pdf")
    dst.parent.mkdir(parents=True, exist_ok=True)

    # reportlab 可能装在另一个解释器里：config.pdf.python 可指定用哪个 python 跑 md2pdf.py
    py = str(pdf.get("python") or sys.executable)
    cmd = build_cmd(md2pdf, pdf, src, dst, py)
    quoted = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(f"input   : {src}")
    print(f"output  : {dst}")
    print(f"复现命令: {quoted}")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(f"md2pdf.py 退出码 {proc.returncode}；PDF 未生成或不完整。", file=sys.stderr)
        return proc.returncode
    print(f"PDF 已生成: {dst}  ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
