// video-dossier 统一配置加载（Node 侧）。
// 与 vd_config.py 同构：脚本里不出现写死的绝对路径，一切走配置。
//
//   import { load } from "./vd_config.mjs";
//   const cfg = load();                 // 读 ./config.json
//   cfg.path("stats", true)             // 绝对路径（可要求自动建目录）
import fs from "node:fs";
import path from "node:path";
import url from "node:url";

const HERE = path.dirname(url.fileURLToPath(import.meta.url));
const PROFILE_DIR = path.join(HERE, "..", "profiles");

export function parseArgs(argv = process.argv.slice(2)) {
  let configPath = process.env.VD_CONFIG || "config.json";
  const rest = [];
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === "--config") { configPath = argv[i + 1]; i += 1; continue; }
    if (a.startsWith("--config=")) { configPath = a.slice("--config=".length); continue; }
    rest.push(a);
  }
  return { configPath, rest };
}

export class Config {
  constructor(configPath) {
    // 属性名是 configPath，不能叫 path —— 那会遮蔽 path() 方法。
    this.configPath = path.resolve(configPath);
    this.root = path.dirname(this.configPath);
    this.data = JSON.parse(fs.readFileSync(this.path, "utf8"));
    this._paths = {};
    for (const [k, v] of Object.entries(this.data.paths || {})) {
      this._paths[k] = path.resolve(this.root, v);
    }
    this._profiles = {};
  }

  get(key, dflt = null) { return this.data[key] ?? dflt; }

  section(key) {
    const v = this.data[key];
    return v && typeof v === "object" && !Array.isArray(v) ? v : {};
  }

  path(name, mkdir = false) {
    const p = this._paths[name];
    if (!p) throw new Error(`config.paths 里没有 '${name}'；请在配置里补上`);
    if (mkdir) fs.mkdirSync(p, { recursive: true });
    return p;
  }

  rel(p) { return path.relative(this.root, p).split(path.sep).join("/"); }

  profile() {
    const name = this.section("video").profile || "generic";
    if (!this._profiles[name]) {
      let f = path.join(PROFILE_DIR, `${name}.json`);
      if (!fs.existsSync(f)) f = path.join(PROFILE_DIR, "generic.json");
      this._profiles[name] = JSON.parse(fs.readFileSync(f, "utf8"));
    }
    return this._profiles[name];
  }

  get media() { return this.section("media"); }
  get asr() { return this.section("asr"); }
  get slides() { return this.section("slides"); }
  get outline() { return this.section("outline"); }
  get title() { return this.section("video").title || this.data.name || "video-dossier"; }
}

export function load(configPath) {
  const { configPath: p } = parseArgs();
  const cfg = new Config(configPath || p);
  return cfg;
}

if (process.argv[1] && process.argv[1].endsWith("vd_config.mjs")) {
  const cfg = load();
  console.log(`config : ${cfg.configPath}`);
  console.log(`runRoot: ${cfg.root}`);
  console.log(`title  : ${cfg.title}`);
  console.log(`media  : ${JSON.stringify(cfg.media)}`);
  for (const k of Object.keys(cfg.data.paths || {})) {
    console.log(`path[${k}] = ${cfg.path(k)}  exists=${fs.existsSync(cfg.path(k))}`);
  }
}
