"""把本次 run 的报告转成 PDF。**默认走杂志风**（magazine.html → 无头 Chromium 打印）。

两条路：
  1. `magazine`（默认）：调用同目录的 make_magazine.py 渲染 `magazine.html`，再用无头
     Chromium 打印成 PDF。这套版式（衬线大标题、大数字、占比条、A4 分页规则、深色结论块）
     只有浏览器引擎能还原，md2pdf 那类 Markdown→PDF 做不到。
  2. `markdown`（回退）：定位第三方脚本 lov-any2pdf 的 `md2pdf.py` 转 Markdown 报告。

找不到 Chromium 时**默认回退到 markdown 并在末尾提示**（不想回退就设 `pdf.magazineStrict = true`）。

Chromium 定位顺序：
  1. config.pdf.chrome（相对路径按 run 根解析）
  2. 环境变量 VD_CHROME
  3. ~/.cache/ms-playwright/chromium-*/chrome-*/chrome.exe（Playwright 自带）
  4. %LOCALAPPDATA%/ms-playwright/chromium-*/chrome-win64/chrome.exe
  5. PATH 上的 chrome / chrome.exe / msedge / msedge.exe

用法：
  python scripts/make_pdf.py --config <run>/config.json [--style magazine|markdown] [--output x.pdf]
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

from vd_config import load

SKILL_DIR = Path(__file__).resolve().parent
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


# ---------------------------------------------------------------- 杂志风路径

def load_magazine_module():
    """按同目录文件加载 make_magazine.py（不依赖 sys.path 状态）。"""
    p = SKILL_DIR / "make_magazine.py"
    spec = importlib.util.spec_from_file_location("make_magazine", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def find_chromium(cfg) -> tuple[Path | None, str]:
    cands: list[Path] = []
    conf = cfg.section("pdf").get("chrome")
    if conf:
        p = Path(str(conf))
        cands.append(p if p.is_absolute() else cfg.root / p)
    if os.environ.get("VD_CHROME"):
        cands.append(Path(os.environ["VD_CHROME"]))
    home = Path.home()
    for pat in ("~/.cache/ms-playwright/chromium-*/chrome-*/chrome",
                "~/.cache/ms-playwright/chromium-*/chrome-*/chrome.exe"):
        cands += [Path(x) for x in glob.glob(str(home / pat[2:]))]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands += [Path(x) for x in glob.glob(str(Path(la) / "ms-playwright" / "chromium-*" / "chrome-win64" / "chrome.exe"))]
    for name in ("chrome", "chrome.exe", "chromium", "chromium.exe", "msedge", "msedge.exe"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        if c.is_file():
            return c, "found"
    return None, ("未找到 Chromium。任选一种：\n"
                  "  1) 装 Playwright 的 Chromium：python -m playwright install chromium；\n"
                  "  2) 指定路径：config.pdf.chrome = \"C:/.../chrome.exe\"；\n"
                  "  3) 设环境变量 VD_CHROME=<chrome.exe 路径>；\n"
                  "  4) 或把 config.pdf.style 设为 \"markdown\"，改用 lov-any2pdf 的 md2pdf.py。")


def html_to_pdf(chrome: Path, src: Path, dst: Path, pdf: dict, cfg) -> int:
    # 把子进程的 cwd 指到 tmp/：Chromium 可能往当前目录写临时垃圾（实测见过 8MB 的
    # .tmp 落在产物目录里）。注意这里只是拿它当 cwd，**不是** --user-data-dir。
    try:
        tmpdir = cfg.path("tmp", mkdir=True)
    except KeyError:
        tmpdir = src.parent / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True)
    # Path.as_uri() 会把中文/空格正确百分号编码，别手工拼 file://
    #
    # 关于这一串 flag（都是实测结论，别随意改）：
    #   * **不要传 --user-data-dir**：给无头 Chromium 一个全新 profile 时，本机实测
    #     --print-to-pdf 会挂住不返回（45s 超时都不出 PDF），不带它则 5s 出片。
    #   * 不带 profile 时 Chromium 可能往「当前目录」写临时垃圾，所以把 cwd 指到 tmp/。
    #   * --disable-component-update 等只是减少无谓的网络与首启行为，保留无妨。
    # Path.as_uri() 会把中文/空格正确百分号编码，别手工拼 file://
    cmd = [
        str(chrome), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-component-update", "--disable-background-networking",
        "--disable-client-side-phishing-detection", "--disable-default-apps",
        "--disable-sync", "--disable-crash-reporter", "--disable-breakpad",
        "--metrics-recording-only", "--no-service-autorun",
        "--no-pdf-header-footer",
        f"--print-to-pdf={dst}",
    ]
    for extra in (pdf.get("chromeArgs") or []):
        cmd.append(str(extra))
    cmd.append(src.as_uri())
    quoted = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(f"html    : {src}")
    print(f"output  : {dst}")
    print(f"复现命令: {quoted}")
    timeout = pdf.get("timeoutSeconds") or 180
    # 显式给子进程指定 TMP/TEMP：Chromium 会把 "HeadlessChrome for Testing<pid>" 这个
    # 临时 profile 建在 TMP 里；调用方的 TMP 万一指到 run 根（很常见，比如 shell 里
    # `set TMP=.`），产物目录里就会多出一坨浏览器垃圾。这里兜住，一律写到 tmp/。
    env = dict(os.environ)
    env["TMP"] = str(tmpdir)
    env["TEMP"] = str(tmpdir)
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              cwd=str(tmpdir), env=env)
    except subprocess.TimeoutExpired:
        print(f"Chromium 打印超时（>{timeout}s）。magazine.html 已生成，"
              f"可直接用浏览器「打印 → 另存为 PDF」。", file=sys.stderr)
        return 4
    tail = (proc.stdout or "").strip().splitlines()[-4:]
    for line in tail:
        print(f"  [chrome] {line}")
    return proc.returncode


def do_magazine(cfg, pdf: dict, out_override: str | None) -> int:
    chrome, note = find_chromium(cfg)
    if chrome is None:
        print(note, file=sys.stderr)
        return 3
    print(f"chrome  : {chrome}")
    mod = load_magazine_module()
    r = mod.Renderer(cfg)
    html_text = r.render()
    html_path = cfg.root / (cfg.section("magazine").get("output") or "magazine.html")
    html_path.write_text(html_text, encoding="utf-8")
    print(f"magazine: {html_path}（章节 {len(r.sections_rendered)} 个，图 {len(r.figures)} 张）")
    if not r.copy:
        print(f"  提示：没有 content/{mod.COPY_NAME}，标题句/结论用的是兜底文案。")

    dst = out_override or pdf.get("output")
    if dst:
        dst = Path(str(dst))
        dst = dst if dst.is_absolute() else cfg.root / dst
    else:
        dst = html_path.with_suffix(".pdf")
    dst.parent.mkdir(parents=True, exist_ok=True)
    rc = html_to_pdf(chrome, html_path, dst, pdf, cfg)
    if rc != 0 or not dst.exists():
        print(f"Chromium 打印失败（退出码 {rc}）。magazine.html 已经生成，可用浏览器手动「打印 → 另存为 PDF」。",
              file=sys.stderr)
        return rc or 1
    print(f"PDF 已生成: {dst}  ({dst.stat().st_size} bytes)")
    return 0


# ---------------------------------------------------------------- markdown 路径

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


def do_markdown(cfg, pdf: dict, in_override: str | None, out_override: str | None) -> int:
    md2pdf = find_md2pdf(cfg)
    if md2pdf is None:
        print(install_hint(), file=sys.stderr)
        return 2

    src = find_input(cfg, in_override)
    out_conf = out_override or pdf.get("output")
    if out_conf:
        dst = Path(str(out_conf))
        dst = dst if dst.is_absolute() else cfg.root / dst
    else:
        dst = src.with_suffix(".pdf")
    dst.parent.mkdir(parents=True, exist_ok=True)

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


# ---------------------------------------------------------------- 入口

def main() -> int:
    cfg, rest = load(argv=sys.argv[1:])
    ap = argparse.ArgumentParser(prog="make_pdf.py")
    ap.add_argument("--input", default=None, help="markdown 路径下覆盖 config.pdf.input")
    ap.add_argument("--output", default=None, help="覆盖输出 PDF 路径")
    ap.add_argument("--style", default=None, choices=["magazine", "markdown"],
                    help="覆盖 config.pdf.style（默认 magazine）")
    args, unknown = ap.parse_known_args(rest)
    if unknown:
        print(f"忽略未知参数：{unknown}")

    pdf = cfg.section("pdf")
    style = args.style or pdf.get("style") or "magazine"

    if style == "magazine":
        rc = do_magazine(cfg, pdf, args.output)
        if rc == 0:
            return 0
        if pdf.get("magazineStrict"):
            print("pdf.magazineStrict = true，不回退。", file=sys.stderr)
            return rc
        print("\n→ 杂志风这条路没走通，回退到 markdown 转 PDF。"
              "（想强制只走杂志风：设 pdf.magazineStrict = true；想换回 markdown：pdf.style = \"markdown\"）",
              file=sys.stderr)
        return do_markdown(cfg, pdf, args.input, args.output)
    return do_markdown(cfg, pdf, args.input, args.output)


if __name__ == "__main__":
    sys.exit(main())
