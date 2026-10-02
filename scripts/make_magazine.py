"""杂志风报告渲染器（默认样式）：把一次 run 的产物渲染成 magazine.html。

设计分工（重要，别混）：
  - **数据**：表格、数字、图表、目录、文件索引、质量读数 —— 全部由本脚本从 run 产物里读出来，
    不允许手写进模板，否则数字会跟产物脱节。
  - **文案**：标题句、导语、一句话结论、清单、引用块 —— 由 `{content}/magazine-copy.json` 提供
    （由 agent 写，是编辑判断，不可能从数据推出来）。缺这个文件也能出片，只是文案部分用兜底。

产物：`{run 根}/magazine.html`（自包含，图片走相对路径），可直接用浏览器打印成 PDF；
     `make_pdf.py --config ...` 默认就是走这条路。

用法：python scripts/make_magazine.py --config config.json [--out magazine.html]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from vd_config import load

ASSETS = Path(__file__).resolve().parent.parent / "assets"
CSS_FILE = ASSETS / "magazine.css"
COPY_NAME = "magazine-copy.json"

# 大纲 kind → 默认展示名（可用 copy.kindLabels 覆盖）
DEFAULT_KIND_LABELS = {
    "导学": "开课铺垫", "新授": "讲方法", "例题": "带着做题",
    "答疑": "回答问题", "互动": "和观众互动", "收尾": "收尾", "其他": "其他",
}
# 兜底「后续能做什么调查」
DEFAULT_QUESTIONS = [
    ["这段视频里，多少时间在讲正题、多少时间在闲聊？", "`content/outline.json`（每段都有类型与时长）"],
    ["说话人的口头禅排行（去掉识别伪影后）", "`stats/top-words.csv`、`language-stats.json`"],
    ["语速什么时候最快、什么时候最慢？", "`stats/per-minute.csv` + 图 1"],
    ["屏幕上出现的内容与口播对得上吗？", "`slides/屏幕关键文本.json` + `content/outline.json`"],
    ["一共讲了多少个可独立命名的知识点、平均每个多少秒？", "`content/outline-points.csv`"],
]


# ---------------------------------------------------------------- 小工具

def esc(v) -> str:
    return html.escape(str(v), quote=True)


def hhmmss(sec) -> str:
    try:
        t = int(float(sec))
    except (TypeError, ValueError):
        return ""
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def num(v, nd=0):
    """数字加千分位；非数字原样返回。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return esc(v)
    if nd == 0 and abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.{nd}f}"


def pct(v, nd=1) -> str:
    try:
        return f"{float(v) * 100:.{nd}f}"
    except (TypeError, ValueError):
        return ""


class Renderer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.copy = self._load_copy()
        self.media = cfg.media
        self.kind_labels = {**DEFAULT_KIND_LABELS, **(self.copy.get("kindLabels") or {})}
        self.bvid = str(self.media.get("bvid") or "").strip()
        self.page = int(self.media.get("page") or 1)
        self.is_bili = bool(self.bvid) and str(self.media.get("kind") or "bilibili") == "bilibili"
        self.sections_rendered: list[tuple[str, str]] = []   # (编号, 标题纯文本)
        self.use_pagebreak: bool = True

    # ---- 读产物（缺失一律降级，不崩） ----
    def _load_json(self, key_or_path, name=None):
        try:
            p = self.cfg.path(key_or_path) / name if name else Path(key_or_path)
        except KeyError:
            return None
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _load_copy(self) -> dict:
        p = self.cfg.path("content") / COPY_NAME
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[warn] {COPY_NAME} 解析失败，改用兜底文案：{exc!r}", file=sys.stderr)
        return {}

    def load_all(self):
        c = self.cfg
        self.meta = self._load_json(c.path("stats") / "video-meta.json")
        self.outline = self._load_json(c.path("content") / "outline.json")
        self.lang = self._load_json(c.path("stats") / "language-stats.json")
        self.ocr = self._load_json(c.path("slides") / "ocr-quality.json")
        self.screen_states = self._load_json(c.path("slides") / "屏幕状态时间轴.json")
        self.screen_texts = self._load_json(c.path("slides") / "屏幕关键文本.json")
        self.cleanrep = self._load_json(c.path("stats") / "clean-vs-raw.json")
        self.corr = self._load_json(c.path("content") / "corrections-reviewed.json")
        self.repohead = self._load_json(c.path("stats") / "repo-head.json")
        self.verify = self._load_json(c.path("stats") / "verify-summary.json")
        figs = c.path("stats") / "figures"
        self.figures = sorted(figs.glob("*.png")) if figs.exists() else []

    # ---- 组件 ----
    def ts(self, sec, text=None) -> str:
        label = text or hhmmss(sec)
        if self.is_bili:
            url = f"https://www.bilibili.com/video/{self.bvid}/?p={self.page}&amp;t={int(float(sec))}"
            return f'<a class="ts" href="{url}">{esc(label)}</a>'
        return f'<span class="tstext">{esc(label)}</span>'

    def fmt(self, text: str) -> str:
        """把文案里的 {ts:秒} 占位符换成可点时间戳（其余原样，允许内联 HTML）。"""
        if not isinstance(text, str):
            return esc(text)
        return re.sub(r"\{ts:(\d+(?:\.\d+)?)\}", lambda m: self.ts(m.group(1)), text)

    def table(self, caption, headers, rows, widths=None) -> str:
        if not rows:
            return ""
        th = "".join(
            f'<th class="num">{esc(h)}</th>' if str(h).startswith("#") else f"<th>{esc(h)}</th>"
            for h in headers
        )
        out = ['<table>']
        if caption:
            out.append(f"<caption>{self.fmt(caption)}</caption>")
        out.append(f"<thead><tr>{th}</tr></thead><tbody>")
        for r in rows:
            tds = []
            for i, cell in enumerate(r):
                cls = ""
                if isinstance(cell, tuple):      # (内容, 样式类) —— 内容允许含 HTML
                    cell, cls = cell
                raw = str(cell)
                attr = f' class="{cls}"' if cls else ""
                tds.append(f"<td{attr}>{raw}</td>")
            out.append("<tr>" + "".join(tds) + "</tr>")
        out.append("</tbody></table>")
        return "\n".join(out)

    def bar(self, ratio, hot=False) -> str:
        try:
            w = max(2, min(100, round(float(ratio) * 100)))
        except (TypeError, ValueError):
            w = 2
        return f'<div class="bar"><i style="width:{w}%"></i></div>'

    def cards(self, items) -> str:
        if not items:
            return ""
        out = ['<div class="cards">']
        for it in items:
            cls = "card red" if it.get("hot") else "card"
            v = self.fmt(it.get("value", ""))
            out.append(f'<div class="{cls}"><div class="v">{v}</div><div class="l">{self.fmt(it.get("label", ""))}</div></div>')
        out.append("</div>")
        return "\n".join(out)

    def ticks(self, items) -> str:
        items = [i for i in (items or []) if str(i).strip()]
        if not items:
            return ""
        return '<ul class="tick">\n' + "\n".join(f"  <li>{self.fmt(i)}</li>" for i in items) + "\n</ul>"

    def blockquote(self, text) -> str:
        return f"<blockquote>{self.fmt(text)}</blockquote>" if text else ""

    def figs(self, captions=None) -> str:
        if not self.figures:
            return ""
        rel = self.cfg.rel(self.cfg.path("stats"))
        out = ['<div class="figs">']
        for i, f in enumerate(self.figures, start=1):
            cap = (captions or {}).get(f.name) or (captions or {}).get(str(i)) or ""
            cls = "fig wide" if len(self.figures) % 2 == 1 and i == len(self.figures) else "fig"
            out.append(
                f'<figure class="{cls}"><img src="{esc(rel)}/figures/{esc(f.name)}" alt="{esc(f.stem)}">'
                f'<figcaption>图 {i} · {self.fmt(cap) if cap else esc(f.stem)}</figcaption></figure>'
            )
        out.append("</div>")
        return "\n".join(out)

    def rhythm(self, words, note="") -> str:
        if not words:
            return ""
        top = max(w["count"] for w in words) or 1
        rows = []
        for w in words:
            width = max(6, round(w["count"] / top * 100))
            warn = ' <span class="warn">（识别伪影）</span>' if w.get("artifact") else ""
            rows.append(
                f'<div class="unit"><b></b><i style="width:{width}%;min-width:6px"></i>'
                f'<span style="font-size:12.5px;color:var(--ink-2);white-space:nowrap">&nbsp;{esc(w["word"])} {num(w["count"])} 次{warn}</span></div>'
            )
        n = f'<p class="rhythmnote">{self.fmt(note)}</p>' if note else ""
        return '<div class="rhythm"><div class="flexmax">\n' + "\n".join(rows) + f"\n</div>\n{n}\n</div>"

    def bignums(self, items) -> str:
        if not items:
            return ""
        out = ['<div class="bignums">']
        for it in items:
            unit = f'<small>{esc(it["unit"])}</small>' if it.get("unit") else ""
            cls = "n hot" if it.get("hot") else "n"
            out.append(
                f'<div class="bignum"><span class="{cls}">{self.fmt(it.get("value", ""))}{unit}</span>'
                f'<div class="k"><b>{self.fmt(it.get("title", ""))}</b>{self.fmt(it.get("note", ""))}</div></div>'
            )
        out.append("</div>")
        return "\n".join(out)

    # ---- 章节 ----
    def section(self, key, kicker, title, body_html):
        self.sections_rendered.append((kicker.split("·")[0].strip() or "", re.sub(r"<[^>]+>", "", title)))
        cls = "section pagebreak" if self.use_pagebreak else "section"
        return (
            f'<section class="{cls}">\n'
            f'  <div class="kicker">{esc(kicker)}</div>\n'
            f'  <h2 class="big">{self.fmt(title)}</h2>\n{body_html}\n</section>'
        )

    def resolve_pagebreak(self) -> bool:
        """篇幅自适应：内容够多才按杂志节奏「每章另起一页」。

        真实长视频（主题几十个、几百条知识点、4 张图）→ 分页，观感像杂志；
        短视频/片段（几条知识点）→ 不分页，否则会出一堆半页空白。
        可用 copy.pageBreaks = true/false 强制。
        """
        v = self.copy.get("pageBreaks")
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in ("true", "false"):
            return v.lower() == "true"
        score = 0
        if self.outline:
            score += int(self.outline.get("stats", {}).get("themeCount") or 0)
            score += int(self.outline.get("stats", {}).get("pointCount") or 0) // 4
        score += 2 * len(self.figures)
        if (self.lang or {}).get("wordFrequency"):
            score += 4
        if self.ocr:
            score += 4
        return score >= 25

    def hero(self) -> str:
        cp = self.copy
        m = self.meta or {}
        title = cp.get("coverline") or self._auto_coverline()
        cover = cp.get("cover")
        cover_html = ""
        if cover and (self.cfg.root / cover).exists():
            cover_html = f'<img class="coverphoto" src="{esc(cover)}" alt="视频封面">'
        bignums = cp.get("bignums") or self._auto_bignums()
        tshint = cp.get("tshint") or self._auto_tshint()
        verdict = cp.get("verdict") or {}
        vparas = "".join(f"<p>{self.fmt(p)}</p>" for p in (verdict.get("paras") or []))
        vtag = f'<span class="tag">{esc(verdict.get("tag"))}</span>' if verdict.get("tag") else ""
        vcls = "verdict" if self.use_pagebreak else "verdict nobreak"

        toc_rows = []
        for i, (no, t) in enumerate(self.sections_rendered, start=1):
            toc_rows.append(f'<li><span class="no">{no or f"{i:02d}"}</span><span class="t">{esc(t)}</span></li>')
        toc = ('<div class="toc"><div class="toc-h">本 期 目 录</div><ol>' + "".join(toc_rows) + "</ol></div>") if toc_rows else ""

        flag_meta = cp.get("flagMeta") or self._auto_flag_meta()
        flag_lines = "".join(f"<div>{self.fmt(x)}</div>" for x in flag_meta)
        deck = cp.get("deck") or {}
        deck_html = (
            f'<div class="deck"><span>{esc(deck.get("vol", "VOL.01"))}</span>'
            f'<span>{esc(deck.get("label", "数 据 解 读"))}</span>'
            f'<span>{esc(deck.get("tagline", ""))}</span></div>'
        )
        return f"""  <header class="flag">
    <div class="logo">{self.fmt(cp.get("logo", "数 据"))}</div>
    <div class="meta">
{flag_lines}
    </div>
  </header>

  {deck_html}

  <section class="hero">
    <div class="over">{esc(cp.get("over", "总 览 · 一 眼 看 完"))}</div>
    {cover_html}
    <h1 class="coverline">{self.fmt(title)}</h1>
    {self.bignums(bignums)}
    {f'<div class="tshint">{self.fmt(tshint)}</div>' if tshint else ''}
    <div class="{vcls}">{vtag}{vparas}</div>
    {toc}
  </section>"""

    def _auto_coverline(self) -> str:
        m = self.meta or {}
        dur = hhmmss((m.get("pages") or [{}])[self.page - 1].get("duration") or m.get("duration") or 0)
        top = ""
        if self.outline:
            ks = self.outline.get("stats", {}).get("kindSeconds") or {}
            if ks:
                k, v = max(ks.items(), key=lambda kv: kv[1])
                total = sum(ks.values()) or 1
                top = f"其实<span>{pct(v / total)}% 的时间在{esc(self.kind_labels.get(k, k))}</span>"
        return f"{esc(dur)} 的视频，{top}" if top else esc(self.cfg.title)

    def _auto_bignums(self):
        out = []
        if self.outline:
            ks = self.outline["stats"].get("kindSeconds") or {}
            if ks:
                k, v = max(ks.items(), key=lambda kv: kv[1])
                total = sum(ks.values()) or 1
                out.append({"value": pct(v / total), "unit": "%", "hot": True,
                            "title": self.kind_labels.get(k, k), "note": f"{num(v)} 秒，占的时间最长"})
            out.append({"value": num(self.outline["stats"].get("pointCount")), "title": "知识点条数",
                        "note": f"归到 {num(self.outline['stats'].get('themeCount'))} 个一级主题"})
        if self.lang:
            c = self.lang["chars"]
            out.append({"value": num(c.get("han")), "title": "说了多少汉字",
                        "note": f"分成 {num(self.lang['source'].get('segmentCount'))} 段"})
            out.append({"value": num(self.lang["rate"].get("hanPerMin"), 1), "title": "每分钟说多少字",
                        "note": f"人声占 {pct(self.lang['speech'].get('speakingRatio'))}%"})
        return out[:4]

    def _auto_tshint(self) -> str:
        if not self.is_bili:
            return ""
        first = None
        if self.outline and self.outline.get("themes"):
            first = self.outline["themes"][0]["startSeconds"]
        sample = self.ts(first or 0)
        return (f"<b>▸ 报告里带淡红底的时间都能点。</b>点一下会打开 B 站并跳到那一秒，例如 {sample}。"
                f"手机上会自动唤起 B 站 App。")

    def _auto_flag_meta(self):
        m = self.meta or {}
        line1_parts = []
        if self.bvid:
            line1_parts.append(f"<b>{esc(self.bvid)}</b>")
        pg = next((p for p in (m.get("pages") or []) if p.get("page") == self.page), None)
        if pg and pg.get("part"):
            line1_parts.append(esc(pg["part"]))
        if self.lang:
            line1_parts.append(f"时长 {hhmmss(self.lang['source'].get('durationSeconds'))}")
        lines = [" · ".join(line1_parts)] if line1_parts else []
        if (m.get("owner") or {}).get("name"):
            lines.append(f"UP：{esc(m['owner']['name'])}")
        if m.get("pubdateLocal"):
            lines.append(f"发布 {esc(m['pubdateLocal'][:10])}")
        if m.get("collectedAtLocal"):
            lines.append(f"数据采集 {esc(m['collectedAtLocal'][:16])}")
        return [x for x in lines if x]

    # ---- 各数据章节 ----
    def sec_structure(self):
        if not self.outline:
            return ""
        cp = (self.copy.get("sections") or {}).get("structure") or {}
        st = self.outline["stats"]
        ks = st.get("kindSeconds") or {}
        total = sum(ks.values()) or 1
        rows = []
        for k, v in sorted(ks.items(), key=lambda kv: -kv[1]):
            rows.append([
                (esc(self.kind_labels.get(k, k)), "name"),
                (f"{num(v)}s", "num"),
                (f"{pct(v / total)}%", "num hot" if v == max(ks.values()) else "num"),
                (self.bar(v / total), ""),
            ])
        table1 = self.table(
            f"{esc(cp.get('table1Caption', '各类内容各占多久'))}（合计 {num(total)}s ≈ {hhmmss(total)}）",
            ["在干什么", "#时长", "#占比", "占比条"], rows)

        timeline = []
        for th in self.outline["themes"][:cp.get("timelineRows", 12)]:
            timeline.append([
                self.ts(th["startSeconds"]), self.ts(th["endSeconds"]),
                (f"{num(th['durationSeconds'])}s", "num"),
                esc(self.kind_labels.get(th["kind"], th["kind"])),
                esc(th["title"]),
            ])
        table2 = self.table(cp.get("table2Caption", "主干时间线（时间可点，会跳到那一秒）"),
                            ["从", "到", "#多久", "在干什么", "内容"], timeline)

        longest = []
        for th in sorted(self.outline["themes"], key=lambda t: -t["durationSeconds"])[:6]:
            longest.append([self.ts(th["startSeconds"], th["title"]), (f"{num(th['durationSeconds'])}s", "num"),
                            esc(self.kind_labels.get(th["kind"], th["kind"]))])
        table3 = self.table(cp.get("table3Caption", "单段最长的六块"),
                            ["点了直接看", "#多久", "在干什么"], longest)

        pts = [p for t in self.outline["themes"] for p in t["points"]]
        sample = pts[0] if pts else None
        sample_html = ""
        if sample:
            lines = "　".join(
                f'{self.ts(p["startSeconds"])} {esc(p["title"])}'
                for p in (self.outline["themes"][0]["points"][:4])
            )
            sample_html = self.blockquote(
                f"<b>举个例子（大纲里的一段）：</b>「{esc(self.outline['themes'][0]['title'])}」在 "
                f"{self.ts(self.outline['themes'][0]['startSeconds'])}–{hhmmss(self.outline['themes'][0]['endSeconds'])}"
                f"（{num(self.outline['themes'][0]['durationSeconds'])} 秒，{self.outline['themes'][0]['pointCount']} 个点）：<br>　{lines}"
            )

        body = "\n".join(x for x in [
            f'<p class="lead">{self.fmt(cp.get("lead", ""))}</p>' if cp.get("lead") else "",
            table1,
            f'<h3 class="sub">{esc(cp.get("sub2", "整场的时间线"))}</h3>' if table2 else "",
            table2,
            f'<h3 class="sub">{esc(cp.get("sub3", "花时间最多的几段"))}</h3>' if table3 else "",
            table3,
            f'<h3 class="sub">{esc(cp.get("sub4", "讲到的要点"))}</h3>' if cp.get("ticks") else "",
            self.ticks(cp.get("ticks")),
            sample_html,
        ] if x)
        return self.section("structure", cp.get("kicker", "01 · 讲了什么"),
                            cp.get("title", f"{hhmmss(total)}，都花在哪了"), body)

    def sec_language(self):
        if not self.lang:
            return ""
        cp = (self.copy.get("sections") or {}).get("language") or {}
        L = self.lang
        rowsp = []
        add = rowsp.append
        add([("分成多少段", "name"), (f"{num(L['source']['segmentCount'])} 段", "num"),
             f"全片 {hhmmss(L['source']['durationSeconds'])}"])
        add([("总字数（含标点）", "name"), (num(L["chars"]["withPunct"]), "num"), ""])
        add([("汉字", "name"), (num(L["chars"]["han"]), "num hot"), ""])
        add([("说得多快", "name"), (f"{num(L['rate']['hanPerMin'], 1)} 字/分钟", "num hot"), ""])
        add([("有人说话的时间", "name"), (f"{num(L['speech']['speakingSeconds'], 1)}s", "num"),
             f"占 <b>{pct(L['speech']['speakingRatio'])}%</b>"])
        add([("第一句话在第几秒", "name"), (f"{num(L['speech']['firstSpeechAtSeconds'], 1)}s", "num"),
             self.ts(L["speech"]["firstSpeechAtSeconds"], "跳到第一句话") if L["speech"].get("firstSpeechAtSeconds") else ""])
        b = L["pauses"]["buckets"]
        add([("停顿超过阈值次数", "name"), (f"{num(L['pauses']['count'])} 次", "num"),
             "、".join(f"{esc(k)} {num(v)} 次" for k, v in b.items())])
        if L["pauses"].get("longest"):
            lg = L["pauses"]["longest"]
            add([("最长的一次停顿", "name"), (f"{num(lg['gap'], 1)}s", "num hot"),
                 f"在 {self.ts(lg['beforeStart'])} 前后"])
        add([("提问句", "name"), (f"{num(L['questions']['count'])} 次", "num"),
             f"平均每分钟 {num(L['questions']['perMinute'], 2)} 次"])
        table = self.table(cp.get("tableCaption", "说话与节奏（主数字用原始听写稿）"),
                           ["看什么", "#数字", "说明"], rowsp)

        words = cp.get("rhythmWords")
        if words is None:
            words = [{"word": w["word"], "count": w["count"]} for w in (L["wordFrequency"]["top"] or [])[:10]]
        rhythm = self.rhythm(words, cp.get("rhythmNote", "统计去掉了虚词，完整榜单见 <code>stats/top-words.csv</code>。"))

        catch = cp.get("catchphrases")
        if catch is None:
            catch = [{"word": c["ngram"], "count": c["count"]} for c in (L.get("catchphrases") or [])[:10]]
        half = (len(catch) + 1) // 2
        left, right = catch[:half], catch[half:]
        crows = []
        for i in range(half):
            a = left[i]
            bcell = right[i] if i < len(right) else {"word": "", "count": ""}
            crows.append([(esc(a["word"]), "name"), (num(a["count"]), "num"),
                          (esc(bcell.get("word", "")), "name"),
                          (num(bcell["count"]) if bcell.get("count") != "" else "", "num")])
        ctable = self.table(cp.get("catchCaption", "连着说的固定词组，按出现次数排"),
                            ["口头禅", "#次数", "口头禅", "#次数"], crows)

        terms = L.get("termHits", {}).get("total") or {}
        trows = []
        top_terms = L.get("termHits", {}).get("topTerms") or {}
        tmax = max(terms.values()) if terms else 1
        for k, v in sorted(terms.items(), key=lambda kv: -kv[1]):
            detail = "、".join(f"{esc(w)} {num(c)}" for w, c in list((top_terms.get(k) or {}).items())[:6])
            trows.append([(esc(k), "name"), (num(v), "num hot" if v == tmax else "num"),
                          (self.bar(v / tmax), ""), detail])
        ttable = self.table(cp.get("termCaption", "领域词表命中次数（同类最长词优先、不重复计数）"),
                            ["门类", "#出现", "对比", "最常出现的几个词"], trows)

        rep = L.get("repetitionArtifact") or {}
        rep_table = ""
        if rep.get("sensitivity"):
            s = rep["sensitivity"]
            rep_table = self.table(
                cp.get("repCaption", "压掉识别复读伪影前后，数字差多少"),
                ["怎么数", "#汉字", "#字母数字", "#字/分钟"],
                [[("原始稿（<b>报告用的是这个</b>）", "name"), (num(s["raw"]["han"]), "num hot"),
                  (num(s["raw"]["latinDigit"]), "num"), (num(s["raw"]["hanPerMin"], 2), "num hot")],
                 [("压掉重复后（仅作对照）", "name"), (num(s["collapsed"]["han"]), "num"),
                  (num(s["collapsed"]["latinDigit"]), "num"), (num(s["collapsed"]["hanPerMin"], 2), "num")]])

        body = "\n".join(x for x in [
            f'<p class="lead">{self.fmt(cp.get("lead", ""))}</p>' if cp.get("lead") else "",
            table,
            self.figs(cp.get("figCaptions")),
            f'<h3 class="sub">{esc(cp.get("sub2", "最常说的词"))}</h3>' if rhythm else "",
            rhythm,
            f'<h3 class="sub">{esc(cp.get("sub3", "口头禅"))}</h3>' if ctable else "",
            ctable,
            f'<h3 class="sub">{esc(cp.get("sub4", "领域词命中"))}</h3>' if ttable else "",
            ttable,
            self.blockquote(cp.get("blockquote")),
            rep_table,
        ] if x)
        return self.section("language", cp.get("kicker", "02 · 说话统计"),
                            cp.get("title", "说得多快"), body)

    def sec_screen(self):
        if not self.ocr:
            return ""
        cp = (self.copy.get("sections") or {}).get("screen") or {}
        q = self.ocr
        rows = [
            [("画面实况", "name"), (esc(q.get("screenIsWhat", ""))[:120], "num"), ""],
            [("去重后的页面", "name"), (f"{num(q.get('dedupKeptPages'))} 张", "num"), ""],
            [("实际认字的页数", "name"), (f"{num(q.get('ocrPages'))} 张", "num"),
             f"每 {num(q.get('ocrBucketSeconds'))} 秒取一张"],
            [("认字准不准", "name"), (num(q.get("meanScore"), 3), "num hot"), "满分 1"],
            [("有看板文字的页", "name"), (f"{num(q.get('pagesWithDashboardText'))} / {num(q.get('ocrPages'))}", "num"),
             f"<b>{pct(q.get('pagesWithDashboardTextRatio'))}%</b>"],
            [("画面变过几种样子", "name"), (f"{num(q.get('distinctScreenStates'))} 种", "num"), ""],
            [("判定", "name"), (esc(q.get("verdict", "")), "num hot"), esc(q.get("ocrEngine", ""))],
        ]
        kinds = q.get("blockKindCounts") or {}
        if kinds:
            rows.append([("认出的文字块", "name"), (num(sum(kinds.values())), "num"),
                         " / ".join(f"{esc(k)} {num(v)}" for k, v in kinds.items())])
        table = self.table(cp.get("tableCaption", "截图与认字的过程"), ["看什么", "#数字", "说明"], rows)

        cards = cp.get("cards")
        if cards is None:
            cards = self._auto_screen_cards()
        states = ""
        if self.screen_states and self.screen_states.get("states"):
            srows = []
            for s in self.screen_states["states"][: cp.get("stateRows", 8)]:
                txt = (s.get("dashboardText") or "").splitlines()
                first = next((x for x in txt if x.strip()), "")
                srows.append([self.ts(s["firstTimeSeconds"]), (esc(first)[:80], "name"),
                              (f"×{num(s.get('pageCount'))}", "num")])
            states = self.table(cp.get("stateCaption", "屏幕上出现过的内容（点时间可直接看）"),
                                ["在视频的", "屏幕上写着", "#命中"], srows)
        body = "\n".join(x for x in [
            f'<p class="lead">{self.fmt(cp.get("lead", ""))}</p>' if cp.get("lead") else "",
            table,
            f'<h3 class="sub">{esc(cp.get("sub2", "屏幕上认出来的关键内容"))}</h3>' if cards else "",
            self.cards(cards),
            states,
            self.blockquote(cp.get("blockquote")),
        ] if x)
        return self.section("screen", cp.get("kicker", "03 · 画面识别"),
                            cp.get("title", "屏幕上有什么"), body)

    def _auto_screen_cards(self):
        items = (self.screen_texts or {}).get("items") or []
        frac = re.compile(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)")
        seen, cards = set(), []
        for it in items:
            m = frac.search(it.get("text", ""))
            if not m or m.group(0) in seen:
                continue
            seen.add(m.group(0))
            cards.append({"value": f"{esc(m.group(1))}<small style=\"font-size:52%\">/{esc(m.group(2))}</small>",
                          "label": esc(it.get("text", "")[:40]), "hot": len(cards) == 0})
            if len(cards) >= 4:
                break
        return cards

    def sec_quality(self):
        cp = (self.copy.get("sections") or {}).get("quality") or {}
        blocks = []
        if cp.get("lead"):
            blocks.append(f'<p class="lead">{self.fmt(cp["lead"])}</p>')

        # 复读伪影
        rep = (self.lang or {}).get("repetitionArtifact") or {}
        if rep.get("sensitivity"):
            s = rep["sensitivity"]
            delta = abs(s["raw"]["hanPerMin"] - s["collapsed"]["hanPerMin"]) / (s["raw"]["hanPerMin"] or 1)
            blocks.append("<h3 class=\"sub\">机器听写会重复</h3>")
            blocks.append(f"<p>原始稿里有 <b>{num(rep.get('segmentsAffected'))} 段</b>出现同一串被重复写成一大串，"
                          f"共移除 {num(rep.get('charsRemoved'))} 字。压掉重复后再数一遍，语音密度只差 "
                          f"<b>{pct(delta)}%</b>，所以主数字仍用原始稿。</p>")
        # 纠错
        if self.cleanrep or self.corr:
            cr = self.cleanrep or {}
            app = (self.corr or {}).get("approvedCount")
            rej = (self.corr or {}).get("rejectedCount")
            blocks.append("<h3 class=\"sub\">听写错字是怎么改的（以及哪些没改）</h3>")
            blocks.append(self.ticks([
                f"逐段找可疑错字，去重后 <b>{num((self.corr or {}).get('candidateCount', ''))}</b> 条候选。"
                if (self.corr or {}).get("candidateCount") else
                "先用子代理逐段清点可疑错字，再用词典筛一遍。",
                f"拿汉语词典查：<b>错串本身不是词 → 采纳；本身是正常词 → 保守不改</b>，除非逐条给出理由。",
                f"最终采纳 <b>{num(app)}</b> 条、拒绝 <b>{num(rej)}</b> 条（{num((self.corr or {}).get('rejectedOccurrences'))} 处未改）。"
                if app is not None else "",
                f"校验：改完字数变化 <b>{num(cr.get('charDelta'))}</b>，与预期 <b>{num(cr.get('charDeltaExpected'))}</b> "
                f"完全一致（只换字、没删改）—— 长度守恒：" + ("通过" if cr.get("lengthConserved") else "未通过") + "。"
                if cr else "",
                "这份修正稿只给人读，<b>不参与任何统计</b>。",
            ]))
            rej_list = (self.corr or {}).get("rejected") or []
            if rej_list:
                rows = [[(esc(r.get("wrong", "")) + " → " + esc(r.get("candidateRight", "")), "name"),
                         ("不改", "warn"), esc((r.get("reason") or "")[:70])] for r in rej_list[:6]]
                blocks.append(self.table("几个「看着像错、其实不该改」的例子", ["本来想改成", "结论", "为什么"], rows))
            top = cr.get("topApplied") or []
            if top:
                blocks.append('<p class="muted">改动最多的几处：' +
                              "、".join(f"<code>{esc(x['wrong'])}→{esc(x['right'])}</code> {num(x['occurrences'])} 次" for x in top[:5]) +
                              "</p>")
        # OCR 误差
        if self.ocr:
            blocks.append("<h3 class=\"sub\">认字也会认错</h3>")
            blocks.append(self.ticks([
                "「这块文字属于看板 / 聊天 / 界面 / 其他」是<b>靠关键词猜的</b>，不是人工分类。",
                "认字有错：数字与标点最容易错（逗号常被认成句点），屏幕数字<b>未逐项校对</b>。",
                "每张图的时刻按抽帧间隔推算，误差最多 1 帧。",
            ]))
        # 仓库
        rh = self.repohead or {}
        if rh.get("headAtStart") and rh.get("headAtVerify") and rh["headAtStart"] != rh["headAtVerify"]:
            blocks.append("<h3 class=\"sub\">这次没动代码仓库</h3>")
            blocks.append(self.table("仓库版本变化", ["什么时候", "版本号", "发生了什么"], [
                [("开工时", "name"), (f"<code>{esc(rh['headAtStart'][:7])}</code>", ""), esc(rh.get("headSubject", ""))],
                [("校验时", "name"), (f"<code>{esc(rh['headAtVerify'][:7])}</code>", ""), esc(rh.get("explanation", ""))[:80]],
            ]))
            blocks.append(f'<p>判据不是「每一行都一样」（有并行任务在跑），而是<b>「现在被改过的文件」没有超出「开工时就已被改过的文件」</b>。'
                          f'证据在 <code>stats/repo-baseline.txt</code>、<code>repo-status-after.txt</code>、<code>repo-head.json</code>。</p>')
        blocks.append(self.ticks(cp.get("ticks")))
        body = "\n".join(x for x in blocks if x)
        if not body.strip():
            return ""
        return self.section("quality", cp.get("kicker", "04 · 怎么算的"),
                            cp.get("title", "这些数字有多准"), body)

    def sec_usage(self):
        cp = (self.copy.get("sections") or {}).get("usage") or {}
        q = cp.get("questions") or self.copy.get("questions") or DEFAULT_QUESTIONS
        qtable = self.table(cp.get("questionsCaption", "想接着往下挖的话，每个问题该翻哪个文件"),
                            ["想问的问题", "去哪找数据"],
                            [[self.fmt(a), self.fmt(b)] for a, b in q])
        cannnot = cp.get("cannot") or self.copy.get("cannot") or []
        files = cp.get("files") or self._auto_file_index()
        cav = ('<div class="caveats"><ol>' +
               "".join(f"<li>{self.fmt(x)}</li>" for x in files) + "</ol></div>") if files else ""
        body = "\n".join(x for x in [
            f'<h3 class="sub">{esc(cp.get("qsub", "已经能直接做的一些数据调查"))}</h3>' if qtable else "",
            qtable,
            f'<h3 class="sub">{esc(cp.get("csub", "这些事它做不了"))}</h3>' if cannnot else "",
            self.ticks(cannnot),
            self.blockquote(cp.get("blockquote")),
            f'<h3 class="sub">{esc(cp.get("fsub", "附 · 每个文件里是什么"))}</h3>' if cav else "",
            cav,
        ] if x)
        return self.section("usage", cp.get("kicker", "05 · 用法"),
                            cp.get("title", "这份数据能拿它干嘛"), body)

    def _auto_file_index(self):
        items = []
        for key, desc in [("README.md", "统计口径与复现步骤"),
                          ("transcripts", "原始转写（所有统计的唯一依据）/ 逐字稿 / 修正稿"),
                          ("content/outline.json", "两级内容大纲（主题 + 知识点）"),
                          ("content/outline-points.csv", "知识点长表（Excel / pandas 直接开）"),
                          ("content/corrections.jsonl", "错字逐条记录与裁决结果"),
                          ("stats/language-stats.json", "说话、语速、停顿、词频全量"),
                          ("stats/per-minute.csv", "逐分钟序列"),
                          ("stats/top-words.csv", "词频与领域词命中"),
                          ("stats/figures", "报告里的图表"),
                          ("slides/slides.jsonl", "逐页认字明细"),
                          ("slides/屏幕状态时间轴.json", "屏幕上出现过的内容"),
                          ("magazine.html", "本文件（杂志版报告）")]:
            p = self.cfg.root / key
            if p.exists():
                items.append(f"<b><code>{esc(key)}</code></b> —— {esc(desc)}")
        return items

    # ---- 组装 ----
    def render(self) -> str:
        self.load_all()
        self.use_pagebreak = self.resolve_pagebreak()
        # hero 里的目录要知道有几节，所以先渲染各节、再拼 hero
        body_sections = [self.sec_structure(), self.sec_language(), self.sec_screen(), self.sec_quality(), self.sec_usage()]
        body_sections = [s for s in body_sections if s]
        hero = self.hero()
        css = CSS_FILE.read_text(encoding="utf-8") if CSS_FILE.exists() else ""
        wm = (self.copy.get("watermark") or "").strip()
        wm_html = f'<div class="watermark"><span>{esc(wm)}</span></div>' if wm else ""
        foot = self.copy.get("footer") or [self.cfg.title, "", ""]
        foot_html = "".join(f"<div>{self.fmt(x)}</div>" for x in foot)
        title = self.copy.get("htmlTitle") or f"{self.cfg.title} · 数据解读报告"
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
{css}
</style>
</head>
<body>
{wm_html}
<div class="wrap">

{hero}

{chr(10).join(body_sections)}

  <footer>
    {foot_html}
  </footer>

</div>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="把 run 产物渲染成杂志风 magazine.html")
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default=None, help="输出文件名（默认杂志.html/magazine.html，见下）")
    args, _extra = ap.parse_known_args()

    cfg, _ = load(args.config)
    r = Renderer(cfg)
    html_text = r.render()
    out = Path(args.out) if args.out else (cfg.root / (cfg.section("magazine").get("output") or "magazine.html"))
    if not out.is_absolute():
        out = cfg.root / out
    out.write_text(html_text, encoding="utf-8")
    print(f"magazine.html: {out}")
    print(f"  章节 {len(r.sections_rendered)} 个： " + " / ".join(t for _, t in r.sections_rendered))
    print(f"  图表 {len(r.figures)} 张；数据源： " + ", ".join(
        n for n, v in [("video-meta", r.meta), ("outline", r.outline), ("language", r.lang),
                       ("ocr", r.ocr), ("clean", r.cleanrep), ("repo", r.repohead)] if v))
    if not r.copy:
        print(f"  提示：没找到 content/{COPY_NAME}，标题句/结论用的是兜底文案。"
              f"想让它像人写的，请按 references/杂志文案.md 提供这个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
