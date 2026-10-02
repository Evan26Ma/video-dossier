"""video-dossier 统一配置加载。

设计约束：**所有脚本只通过这里解析路径**，任何脚本里都不出现写死的绝对路径，
这样同一套代码既能在本机跑，也能被别人 clone 后在任意目录跑。

用法（每个脚本开头）：

    from vd_config import load
    cfg, extra = load()                 # 读 ./config.json
    cfg, extra = load(argv=sys.argv[1:])  # 读 --config 指定的文件
    trans_dir = cfg.path("transcripts", mkdir=True)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = "config.json"
PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


class Config:
    def __init__(self, path: Path):
        # 注意：属性名是 config_path，不能叫 path —— 那会遮蔽下面的 path() 方法
        # （曾经踩过：self.path = <配置文件路径> 之后所有 cfg.path("stats") 调用都报
        #  'WindowsPath' object is not callable）。
        self.config_path = path
        self.root = path.parent
        self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        self._paths = {k: (self.root / v) for k, v in (self.data.get("paths") or {}).items()}
        self._profiles: dict[str, dict] = {}

    # --- 取值 ---
    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def section(self, key: str) -> dict:
        """取一个配置段，缺失时返回空 dict（脚本不必到处写 or {}）。"""
        v = self.data.get(key)
        return v if isinstance(v, dict) else {}

    # --- 路径 ---
    def path(self, name: str, mkdir: bool = False) -> Path:
        p = self._paths.get(name)
        if p is None:
            raise KeyError(f"config.paths 里没有 '{name}'；请在配置里补上")
        if mkdir:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def rel(self, p: Path) -> str:
        """转成相对 run 根的路径（写进产物里用，便于整体搬走）。"""
        try:
            return str(p.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(p).replace("\\", "/")

    # --- 域配置（术语表 / 屏幕关键词等） ---
    def profile(self) -> dict:
        name = self.section("video").get("profile") or "generic"
        if name not in self._profiles:
            f = PROFILE_DIR / f"{name}.json"
            if not f.exists():
                f = PROFILE_DIR / "generic.json"
            self._profiles[name] = json.loads(f.read_text(encoding="utf-8"))
        return self._profiles[name]

    def term_categories(self) -> dict[str, list[str]]:
        return self.profile().get("terms") or {}

    # --- 常用便捷属性 ---
    @property
    def media(self) -> dict:
        return self.section("media")

    @property
    def asr(self) -> dict:
        return self.section("asr")

    @property
    def slides(self) -> dict:
        return self.section("slides")

    @property
    def outline(self) -> dict:
        return self.section("outline")

    @property
    def title(self) -> str:
        return self.section("video").get("title") or self.data.get("name") or "video-dossier"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--config", default=os.environ.get("VD_CONFIG", DEFAULT_CONFIG),
                    help="配置文件路径，默认 ./config.json（可用环境变量 VD_CONFIG）")
    args, rest = ap.parse_known_args(argv)
    return args.config, rest


def load(config_path: str | None = None, argv=None) -> tuple[Config, list[str]]:
    cfg_arg, rest = parse_args(argv)
    p = Path(config_path or cfg_arg)
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.exists():
        raise SystemExit(
            f"找不到配置文件：{p}\n"
            f"先复制模板：copy scripts\\config.example.json config.json 然后按需修改。"
        )
    return Config(p), rest


if __name__ == "__main__":
    cfg, _ = load()
    print(f"config : {cfg.config_path}")
    print(f"runRoot: {cfg.root}")
    print(f"title  : {cfg.title}")
    print(f"media  : {json.dumps(cfg.media, ensure_ascii=False)}")
    print(f"profile: {cfg.section('video').get('profile', 'generic')}"
          f"（术语类别 {len(cfg.term_categories())} 个）")
    for k, v in cfg.data.get("paths", {}).items():
        print(f"path[{k}] = {cfg.path(k)}  exists={cfg.path(k).exists()}")
