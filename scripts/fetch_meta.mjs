// 采集视频的平台公开数据（视频级元数据 + 分P 元数据 + 标签 + UP 数据）。
//
// 匿名访问：只使用 buvid 设备指纹 + wbi 签名，不使用任何登录态（无 SESSDATA）。
// 评论正文默认**不采集**；只有显式加 --comments 才取评论正文，默认口径下只做一次
// 「可见性探针」，以便产物里如实说明为什么没有评论数据。
// --comments 的分页口径沿用实跑探针 scripts/_probe_reply_wbi.mjs 已验证的端点：
//   /x/v2/reply/wbi/main + pagination_str={"offset":...}，
//   offset 取自 cursor.pagination_reply.next_offset；评论挂在整个稿件（aid）上，不区分分P。
//
// 产物（目录全部来自 config.paths，本脚本里不出现任何写死的绝对路径）：
//   <paths.stats>/video-meta.json / video-meta.csv（长表，自带来源与口径列）
//   <paths.stats>/raw/*.json（每个 API 的原始响应，便于复核）
//   <paths.stats>/comments.json（仅 --comments 时产出）
//
// 用法：node scripts/fetch_meta.mjs --config config.json [--comments]
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { load, parseArgs } from "./vd_config.mjs";

const cfg = load();
const { rest } = parseArgs();
const COLLECT_COMMENTS = rest.includes("--comments");

const STATS = cfg.path("stats", true);
const RAW = path.join(STATS, "raw");
fs.mkdirSync(RAW, { recursive: true });
// 相对路径口径：写进产物的路径字段一律用 cfg.rel()，便于整个 run 目录搬走。
const RAW_REL = cfg.rel(RAW);
const COMMENTS_REL = cfg.rel(path.join(STATS, "comments.json"));

// bvid / page 来自 config.media；评论采集开关来自命令行。
const BVID = String(cfg.media.bvid || "").trim();
if (!BVID) {
  throw new Error("config.media.bvid 为空：fetch_meta.mjs 只适用于 media.kind=bilibili 的配置；本地文件请用 media.kind=local 走 ASR 链路");
}
const PAGE = Number(cfg.media.page || 1);

// 评论分页参数（可选用 config.comments 覆盖，缺省值与实跑探针一致）。
const COMMENTS = cfg.section("comments");
const COMMENT_MODE = Number(COMMENTS.mode || 3);
const COMMENT_PAGE_SIZE = Number(COMMENTS.pageSize || 20);
const COMMENT_MAX_PAGES = Number(COMMENTS.maxPages || 12);

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const REFERER = `https://www.bilibili.com/video/${BVID}/`;
const GAP_MS = 700; // 匿名访问的请求间隔：别调太小，避免被风控
const MIXIN = [46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,22,25,54,21,56,59,6,63,57,62,11,36,20,34,44,52];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const iso = (sec) => (sec ? new Date(sec * 1000).toISOString() : null);
function localISO(d = new Date()) {
  const p = (n, w = 2) => String(n).padStart(w, "0");
  const off = -d.getTimezoneOffset();
  const a = Math.abs(off);
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:` +
    `${p(d.getMinutes())}:${p(d.getSeconds())}${off >= 0 ? "+" : "-"}${p(Math.floor(a / 60))}:${p(a % 60)}`
  );
}

// ---------- 匿名会话：buvid 设备指纹 + wbi 签名 ----------
const jar = new Map();
const setCookies = (res) => {
  const raw = res.headers.getSetCookie ? res.headers.getSetCookie() : [];
  for (const c of raw) {
    const kv = c.split(";")[0];
    const i = kv.indexOf("=");
    if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim());
  }
};
const cookieHeader = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ");

async function raw(url, referer = REFERER) {
  const res = await fetch(url, {
    headers: {
      "User-Agent": UA,
      Referer: referer,
      Accept: "application/json, text/plain, */*",
      "Accept-Language": "zh-CN,zh;q=0.9",
      ...(jar.size ? { Cookie: cookieHeader() } : {}),
    },
  });
  setCookies(res);
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch {}
  return { status: res.status, text, json };
}

const md5 = (s) => crypto.createHash("md5").update(s).digest("hex");
const mixinKeyEncTab = (img, sub) => MIXIN.map((n) => (img + sub)[n]).join("");
function signWbi(params, mixinKey) {
  const wts = Math.floor(Date.now() / 1000);
  const p = { ...params, wts };
  const query = Object.keys(p)
    .sort()
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(String(p[k]).replace(/[!'()*]/g, ""))}`)
    .join("&");
  return `${query}&w_rid=${md5(query + mixinKey)}`;
}

async function api(url, tag, { retries = 3, referer } = {}) {
  let lastErr = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const r = await raw(url, referer);
      if (r.json && r.json.code === 0) {
        fs.writeFileSync(path.join(RAW, `${tag}.json`), r.text, "utf8");
        return r.json.data;
      }
      lastErr = r.json ? `code=${r.json.code} ${r.json.message}` : `HTTP ${r.status} non-JSON`;
    } catch (err) {
      lastErr = String(err.message || err);
    }
    if (attempt < retries) await sleep(GAP_MS * (attempt + 1));
  }
  throw new Error(`[${tag}] failed after ${retries} attempts: ${lastErr}`);
}

await raw("https://www.bilibili.com/", "https://www.bilibili.com/");
const spi = await raw("https://api.bilibili.com/x/frontend/finger/spi", "https://www.bilibili.com/");
if (spi.json?.data?.b_3) jar.set("buvid3", spi.json.data.b_3);
if (spi.json?.data?.b_4) jar.set("buvid4", spi.json.data.b_4);
const nav = await raw("https://api.bilibili.com/x/web-interface/nav", "https://www.bilibili.com/");
const pickKey = (u) => (u.split("/").pop() || "").split(".")[0];
const mixinKey = mixinKeyEncTab(
  pickKey(nav.json?.data?.wbi_img?.img_url ?? ""),
  pickKey(nav.json?.data?.wbi_img?.sub_url ?? ""),
);

// ---------- CSV ----------
const csvCell = (v) => {
  if (v === null || v === undefined) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const toCsv = (header, rows) => [header.join(","), ...rows.map((r) => r.map(csvCell).join(","))].join("\n") + "\n";

// ---------- 主采集序列（同一快照） ----------
const collectedAt = new Date().toISOString();
const collectedAtLocal = localISO();
const steps = [];

const view = await api(`https://api.bilibili.com/x/web-interface/view?bvid=${BVID}`, "view");
steps.push("view");
const ownerMid = view.owner.mid;
const aid = view.aid;

await sleep(GAP_MS);
const tagData = await api(`https://api.bilibili.com/x/tag/archive/tags?bvid=${BVID}`, "tags");
steps.push("tags");
await sleep(GAP_MS);
const relation = await api(`https://api.bilibili.com/x/relation/stat?vmid=${ownerMid}`, "relation");
steps.push("relation");

const pages = [];
for (const p of view.pages || []) {
  await sleep(GAP_MS);
  const pv2 = await api(`https://api.bilibili.com/x/player/v2?bvid=${BVID}&cid=${p.cid}`, `player-v2-p${p.page}`);
  steps.push(`player-v2-p${p.page}`);
  await sleep(GAP_MS);
  let online = null;
  try {
    online = await api(`https://api.bilibili.com/x/player/online/total?bvid=${BVID}&cid=${p.cid}`, `online-p${p.page}`);
    steps.push(`online-p${p.page}`);
  } catch (err) {
    online = { error: String(err.message || err) };
  }
  pages.push({
    page: p.page,
    isTargetPage: p.page === PAGE,
    cid: p.cid,
    part: p.part,
    duration: p.duration,
    dimension: p.dimension,
    firstFrame: p.first_frame || null,
    asrLanguage: p.asr_language || null,
    ctime: p.ctime,
    subtitleCount: (pv2.subtitle?.list || []).length,
    subtitleAllowSubmit: pv2.subtitle?.allow_submit ?? null,
    viewPointsCount: (pv2.view_points || []).length,
    interactionCount: (pv2.interaction || []).length,
    online,
  });
}

// 评论：默认不采集正文；这里在默认口径下只做一次「可见性探针」，
// 以便口径里如实说明为什么没有评论数据。加 --comments 才真正翻页取正文。
const commentProbe = {
  collected: false,
  reason: COLLECT_COMMENTS ? "" : "默认不采集评论（需要时加 --comments）",
  mode: COLLECT_COMMENTS ? COMMENT_MODE : null,
  pageSize: COLLECT_COMMENTS ? COMMENT_PAGE_SIZE : null,
  maxPages: COLLECT_COMMENTS ? COMMENT_MAX_PAGES : null,
  pagesFetched: 0,
  uniqueRpid: 0,
  allCount: null,
  isEnd: null,
  file: null,
  endpointsTried: [],
};
if (COLLECT_COMMENTS) {
  const seen = new Map();
  let offset = "";
  let allCount = null;
  let isEnd = null;
  for (let i = 1; i <= COMMENT_MAX_PAGES; i += 1) {
    await sleep(GAP_MS);
    const q = signWbi(
      {
        oid: aid,
        type: 1,
        mode: COMMENT_MODE,
        pagination_str: JSON.stringify({ offset }),
        plat: 1,
        web_location: 1315875,
        ps: COMMENT_PAGE_SIZE,
      },
      mixinKey,
    );
    let data;
    try {
      data = await api(`https://api.bilibili.com/x/v2/reply/wbi/main?${q}`, `reply-wbi-m${COMMENT_MODE}-p${i}`);
    } catch (err) {
      commentProbe.endpointsTried.push(`reply-wbi p${i}:fail ${String(err.message || err)}`);
      break;
    }
    const replies = data.replies || [];
    const top = data.top_replies || [];
    for (const x of [...top, ...replies]) {
      const key = x.rpid_str ?? String(x.rpid);
      if (!seen.has(key)) seen.set(key, x);
    }
    const cursor = data.cursor || {};
    allCount = cursor.all_count ?? allCount;
    isEnd = cursor.is_end ?? null;
    commentProbe.pagesFetched = i;
    commentProbe.endpointsTried.push(
      `reply-wbi p${i}:ok replies=${replies.length} top=${top.length} is_end=${cursor.is_end}`,
    );
    const next = cursor.pagination_reply?.next_offset ?? "";
    offset = next;
    if (cursor.is_end || !next) break;
  }
  const items = [...seen.values()].map((x) => ({
    rpid: x.rpid_str ?? String(x.rpid),
    root: x.root_str ?? String(x.root ?? 0),
    parent: x.parent_str ?? String(x.parent ?? 0),
    mid: String(x.mid ?? ""),
    member: x.member?.uname ?? null,
    ctime: x.ctime ?? null,
    ctimeISO: iso(x.ctime ?? 0),
    like: x.like ?? 0,
    replyCount: x.rcount ?? 0,
    message: x.content?.message ?? "",
  }));
  commentProbe.collected = commentProbe.pagesFetched > 0;
  commentProbe.uniqueRpid = seen.size;
  commentProbe.allCount = allCount;
  commentProbe.isEnd = isEnd;
  if (commentProbe.collected) {
    commentProbe.file = COMMENTS_REL;
    fs.writeFileSync(
      path.join(STATS, "comments.json"),
      JSON.stringify(
        {
          collectedAt,
          collectedAtLocal,
          bvid: BVID,
          aid,
          mode: COMMENT_MODE,
          pageSize: COMMENT_PAGE_SIZE,
          pagesFetched: commentProbe.pagesFetched,
          uniqueRpid: seen.size,
          allCount,
          isEnd,
          note: [
            "匿名 wbi 签名接口按 offset 分页采集，只含顶层评论（含置顶），未展开楼中楼。",
            "平台自报的 all_count 可能大于实际取回条数（已删除、仅自己可见、被折叠等）。",
          ],
          replies: items,
        },
        null,
        2,
      ) + "\n",
      "utf8",
    );
  } else if (!commentProbe.reason) {
    commentProbe.reason = "评论接口全部失败，未取到正文；详见 endpointsTried 与 raw/ 下的原始响应";
  }
}

// ---------- video-meta ----------
const minutesSincePublish = (Date.now() / 1000 - view.pubdate) / 60;
const videoMeta = {
  collectedAt,
  collectedAtLocal,
  bvid: BVID,
  targetPage: PAGE,
  aid,
  apiSteps: steps,
  anonymousOnly: true,
  cookieNote: "仅 buvid3/buvid4 设备指纹，未使用 SESSDATA 或任何登录态。",
  provenance: {
    config: cfg.rel(cfg.configPath),
    script: "scripts/fetch_meta.mjs",
    statsDir: cfg.rel(STATS),
    rawDir: RAW_REL,
  },
  title: view.title,
  desc: view.desc ?? "",
  dynamic: view.dynamic ?? "",
  owner: {
    mid: ownerMid,
    name: view.owner.name,
    face: view.owner.face,
    follower: relation.follower ?? null,
    following: relation.following ?? null,
  },
  pubdate: view.pubdate,
  pubdateISO: iso(view.pubdate),
  pubdateLocal: localISO(new Date(view.pubdate * 1000)),
  ctime: view.ctime,
  duration: view.duration,
  videos: view.videos,
  copyright: view.copyright,
  missionId: view.mission_id,
  tid: view.tid,
  tidV2: view.tid_v2,
  tname: view.tname,
  tnameV2: view.tname_v2,
  dimension: view.dimension,
  pic: view.pic,
  state: view.state,
  teenageMode: view.teenage_mode ?? null,
  rights: view.rights,
  stat: view.stat,
  statDerived: {
    interactionRate: view.stat.view ? (view.stat.like + view.stat.coin + view.stat.favorite) / view.stat.view : null,
    likePerView: view.stat.view ? view.stat.like / view.stat.view : null,
    favoritePerView: view.stat.view ? view.stat.favorite / view.stat.view : null,
    replyPerView: view.stat.view ? view.stat.reply / view.stat.view : null,
    coinPerLike: view.stat.like ? view.stat.coin / view.stat.like : null,
    sharePerLike: view.stat.like ? view.stat.share / view.stat.like : null,
    minutesSincePublish,
    viewPerMinuteSincePublish: minutesSincePublish ? view.stat.view / minutesSincePublish : null,
    caveat: "视频发布仅数十分钟，所有比率随时间快速变化，不具代表性，不可用于横向比较。",
  },
  subtitleAllowSubmit: view.subtitle?.allow_submit ?? null,
  subtitleListCount: (view.subtitle?.list || []).length,
  tags: (tagData || []).map((t) => ({ tagId: t.tag_id, tagName: t.tag_name, type: t.type, ctime: t.ctime })),
  pages,
  commentCollection: commentProbe,
  notes: [
    "所有平台计数均为累计值快照，仅代表 collectedAt 瞬间；本次未做时间序列采样。",
    "copyright=3 的语义 B 站未在公开文档中定义，原样记录不做解读。",
    "stat.danmaku=0 由三方证据一致确认：view.stat、x/v1/dm/list.so 空响应、用户 DownKyi 下载的弹幕 ASS（Dialogue 行数 0）。",
    COLLECT_COMMENTS
      ? "评论正文已按 --comments 采集（顶层评论 + 置顶，未展开楼中楼）；view.stat.reply 是平台自报计数，可能大于实际可取回条数。"
      : "评论正文默认不采集（需要时加 --comments）；view.stat.reply 仅是平台自报计数，无法拆分楼层与楼中楼。",
  ],
};
fs.writeFileSync(path.join(STATS, "video-meta.json"), JSON.stringify(videoMeta, null, 2) + "\n", "utf8");

const metaRows = [];
const push = (scope, metric, value, source, note) => metaRows.push([scope, metric, value, source, note]);
push("video", "bvid", BVID, "view", "");
push("video", "target_page", PAGE, "config.media", "本次分析的目标分P（来自配置）");
push("video", "aid", aid, "view", "");
push("video", "title", view.title, "view", "");
push("video", "owner_mid", ownerMid, "view", "");
push("video", "owner_name", view.owner.name, "view", "");
push("video", "owner_follower", relation.follower, "relation/stat", "采集瞬间");
push("video", "owner_following", relation.following, "relation/stat", "采集瞬间");
push("video", "pubdate", view.pubdate, "view", "Unix 秒");
push("video", "pubdate_local", videoMeta.pubdateLocal, "view", "本机时区 +08:00");
push("video", "duration_seconds", view.duration, "view", "各分P 时长之和");
push("video", "videos", view.videos, "view", "分P 数");
push("video", "copyright", view.copyright, "view", "语义未定义，原样记录");
push("video", "mission_id", view.mission_id, "view", "合集 id（未解析出可读合集）");
push("video", "tid", view.tid, "view", "分区 id");
push("video", "dimension", view.dimension, "view", "封面/首P 分辨率");
push("video", "view", view.stat.view, "view.stat", "累计播放");
push("video", "danmaku", view.stat.danmaku, "view.stat", "累计弹幕；三方证据均为 0");
push("video", "reply", view.stat.reply, "view.stat", "平台自报累计评论数（正文是否采集见 comments_collected）");
push("video", "favorite", view.stat.favorite, "view.stat", "累计收藏");
push("video", "coin", view.stat.coin, "view.stat", "累计投币");
push("video", "share", view.stat.share, "view.stat", "累计分享");
push("video", "like", view.stat.like, "view.stat", "累计点赞");
push("video", "dislike", view.stat.dislike, "view.stat", "累计不喜欢（B 站已基本停用）");
push("video", "now_rank", view.stat.now_rank, "view.stat", "0=未上榜");
push("video", "his_rank", view.stat.his_rank, "view.stat", "0=未上榜");
push("video", "interaction_rate", videoMeta.statDerived.interactionRate, "派生", "(点赞+投币+收藏)/播放");
push("video", "like_per_view", videoMeta.statDerived.likePerView, "派生", "点赞/播放");
push("video", "favorite_per_view", videoMeta.statDerived.favoritePerView, "派生", "收藏/播放");
push("video", "reply_per_view", videoMeta.statDerived.replyPerView, "派生", "平台自报评论/播放");
push("video", "coin_per_like", videoMeta.statDerived.coinPerLike, "派生", "投币/点赞");
push("video", "share_per_like", videoMeta.statDerived.sharePerLike, "派生", "分享/点赞");
push("video", "minutes_since_publish", minutesSincePublish, "派生", "采集时刻 − 发布时间");
push("video", "view_per_minute_since_publish", videoMeta.statDerived.viewPerMinuteSincePublish, "派生", "播放/发布以来分钟数；视频过新，不具代表性");
push("video", "tags", (tagData || []).map((t) => t.tag_name), "tag/archive/tags", `共 ${(tagData || []).length} 个`);
push("video", "comments_collected", commentProbe.collected, "config/--comments", commentProbe.reason || `mode=${COMMENT_MODE} pages=${commentProbe.pagesFetched}`);
push("video", "comments_fetched", commentProbe.collected ? commentProbe.uniqueRpid : null, "reply/wbi/main", "实际取回的顶层评论条数（未展开楼中楼）");
push("video", "raw_dir", RAW_REL, "config.paths", "各 API 原始响应目录");
for (const p of pages) {
  const s = `p${p.page}`;
  push(s, "is_target_page", p.isTargetPage, "view.pages", "是否 config.media.page 指定的分P");
  push(s, "part", p.part, "view.pages", "");
  push(s, "cid", p.cid, "view.pages", "");
  push(s, "duration_seconds", p.duration, "view.pages", "");
  push(s, "dimension", p.dimension, "view.pages", "原生分辨率");
  push(s, "subtitle_count", p.subtitleCount, "player/v2", "CC/AI 字幕轨数量");
  push(s, "subtitle_allow_submit", p.subtitleAllowSubmit, "player/v2", "是否允许投稿字幕");
  push(s, "view_points_count", p.viewPointsCount, "player/v2", "官方章节数，0=无章节");
  push(s, "online_total", p.online?.total ?? null, "player/online/total", "全站同时在看");
  push(s, "online_count", p.online?.count ?? null, "player/online/total", "本视频同时在看");
}
fs.writeFileSync(path.join(STATS, "video-meta.csv"), "\uFEFF" + toCsv(["scope","metric","value","source_api","note"], metaRows), "utf8");

console.log(`bvid=${BVID} targetPage=p${PAGE}`);
console.log(`config=${cfg.configPath}`);
console.log(`collectedAt=${collectedAtLocal}`);
console.log(`stat=`, view.stat);
console.log(`derived=`, videoMeta.statDerived);
console.log(`pages=`, pages.map((p) => `p${p.page}:${p.part}:${p.duration}s:sub${p.subtitleCount}:vp${p.viewPointsCount}:online${p.online?.total ?? "-"}${p.isTargetPage ? "*" : ""}`).join(" | "));
console.log(`tags=`, (tagData || []).map((t) => t.tag_name).join("/"));
console.log(
  COLLECT_COMMENTS
    ? `comments: collected=${commentProbe.collected} unique=${commentProbe.uniqueRpid} all_count=${commentProbe.allCount} file=${commentProbe.file ?? "-"}`
    : `comments: not collected (default); platform counter stat.reply=${view.stat.reply}; 需要时加 --comments`,
);
console.log(`out=`, cfg.rel(path.join(STATS, "video-meta.json")), cfg.rel(path.join(STATS, "video-meta.csv")), RAW_REL);
