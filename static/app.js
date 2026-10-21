"use strict";
/* ============================================================
   足球对冲系统 · 模拟市场引擎 + 统一状态 + 对冲计算
   全部为演示数据（Mock Adapter）。未来可替换为真实数据源
   TC / HG / ODDSAPI（Real Adapter），下游计算与展示不变。
   ============================================================ */

// P0 修复：全局异常捕获，防止静默失败
window.addEventListener("error", (event) => {
  console.error("[P0 Global Error]", {
    message: event.message,
    filename: event.filename,
    lineno: event.lineno,
    colno: event.colno,
  });
});
window.addEventListener("unhandledrejection", (event) => {
  console.error("[P0 Unhandled Promise Rejection]", event.reason);
});

(function () {
  // ---------- 平台 ----------
  const PLATFORMS = ["A", "B", "C"]; // 模拟平台 A / B / C
  const PDEF = {
    A: { name: "模拟平台 A", min: 10, step: 1, max: 20000, rebate: 0.008 },
    B: { name: "模拟平台 B", min: 10, step: 1, max: 15000, rebate: 0.005 },
    C: { name: "模拟平台 C", min: 2, step: 2, max: 10000, rebate: 0.003 },
    TC: { name: "竞彩(HG替代)", min: 2, step: 2, max: 20000, rebate: 0 },
    HG: { name: "HG", min: 1, step: 1, max: 999999, rebate: 0 },
    DAFABET: { name: "DAFABET", min: 1, step: 1, max: 999999, rebate: 0 },
  };
  // ---------- 玩法（纯数据驱动：所有玩法从后端抓取数据中自动发现）----------
  // 类型码 → 中文名（仅用于展示，不限制玩法范围）
  const _TYPE_NAMES = {
    "1X2":"胜平负", "AH":"让球", "OU":"大小球", "EO":"单双",
    "GL":"进球线", "HT1X2":"半场胜平负", "HTOU":"半场大小球",
    "CNROU":"角球大小", "SCORE":"是否得分", "SHOTS":"射门数",
    "TOTS":"总射门", "PEN":"点球胜平负", "RE":"亚盘", "HOU":"半场亚盘",
    "TG":"总进球", "BTTS":"双方进球", "CS":"比分", "HTFT":"半全场",
    "HM":"让球胜平负(3-way)", "HR":"让球(半场)"
  };
  // 选项码 → 中文名
  var _SEL_NAMES = {
    "HOME":"主胜", "DRAW":"平局", "AWAY":"客胜",
    "OVER":"大球", "UNDER":"小球",
    "EVEN":"单", "ODD":"双",
    "YES":"是", "NO":"否"
  };
  function _selName(code) { return _SEL_NAMES[code] || code; }
  function _typeName(type) { return _TYPE_NAMES[type] || type; }

  // 玩法注册表（空启动，全由数据填充）
  var _REG = {};
  var MARKETS = [];

  // 从比赛数据中注册一个玩法类型
  function _ensureMarketDef(type, sampleMk) {
    if (_REG[type]) return _REG[type];
    var selCodes = [];
    if (sampleMk) {
      // 排除元数据 key
      selCodes = Object.keys(sampleMk).filter(function(k) {
        return k !== "line" && k !== "code" && k !== "type"
            && k !== "changes" && k !== "marketBase"
            && k !== "platforms" && k !== "selections"
            && k !== "backendQuotes" && k !== "multiLine";
      });
    }
    // 如果 sampleMk 没有直接选项，尝试 platforms
    if (selCodes.length === 0 && sampleMk && sampleMk.platforms) {
      var pfKeys = Object.keys(sampleMk.platforms);
      if (pfKeys.length > 0) {
        var pf0 = sampleMk.platforms[pfKeys[0]];
        if (pf0 && pf0.odds) {
          selCodes = pf0.odds.map(function(_, i) { return "SEL" + i; });
        }
      }
    }
    if (selCodes.length === 0) selCodes = ["HOME", "AWAY"]; // 终极兜底
    var sels = selCodes.map(function(c) { return [c, _selName(c)]; });
    var def = { type: type, name: _typeName(type), sels: sels };
    _REG[type] = def;
    // 同步 MARKETS 数组
    MARKETS.length = 0;
    Object.values(_REG).forEach(function(d) { MARKETS.push(d); });
    return def;
  }

  // 扫描全部比赛，注册所有发现的玩法类型
  function _scanAndRegisterMarkets() {
    var count = 0;
    S.matches.forEach(function(m) {
      Object.keys(m.markets || {}).forEach(function(t) {
        if (!_REG[t]) {
          _ensureMarketDef(t, m.markets[t]);
          count++;
        }
      });
    });
    return count;
  }  // ---------- 队名池 ----------
  const TEAMS = [
    "阿森纳", "切尔西", "利物浦", "曼城", "曼联", "热刺", "拜仁", "多特蒙德", "莱比锡",
    "皇马", "巴萨", "马竞", "塞维利亚", "尤文图斯", "AC米兰", "国米", "那不勒斯", "罗马",
    "巴黎圣日耳曼", "里昂", "马赛", "本菲卡", "波尔图", "阿贾克斯", "埃因霍温", "凯尔特人",
    "流浪者", "布鲁日", "费内巴切", "加拉塔萨雷", "帕纳辛奈科斯", "奥林匹亚科斯",
    "萨格勒布迪纳摩", "贝尔格莱德红星", "哥本哈根", "莫尔德", "博德闪耀", "马尔默",
    "川崎前锋", "横滨水手", "浦和红钻", "蔚山现代", "全北现代", "水原三星", "墨尔本城",
    "洛杉矶FC", "弗拉门戈", "河床", "博卡青年", "蒙特雷",
  ];
  const COMPS = ["英超", "西甲", "德甲", "意甲", "法甲", "欧冠", "欧联", "欧国联", "日职", "韩K", "英冠", "荷甲", "葡超", "苏超"];
  // 初始可套利模板（固定 id，保证刷新后仍有可演示机会）
  const SEED = [
    ["M003", "1X2"], ["M007", "1X2"], ["M011", "1X2"], ["M015", "1X2"],
    ["M005", "AH"], ["M009", "OU"], ["M013", "BTTS"],
  ];

  // ---------- 全局状态 ----------
  const S = {
    matches: [],
    opps: {},                       // key = matchId|type -> 最新计算结果
    events: [],                     // 历史事件
    cart: [],                       // 购买清单
    tracks: [],                     // 追踪
    settings: { running: true, speed: 1, vol: "中", n: 50 },
    dataMode: "real",               // real=HG 真实数据 / demo=模拟演示
    real: {
      source: null, status: null, generatedAt: null, lastLoaded: null, marketNote: null,
      stream: false, worker: null, lastEventAt: null, pushLatencyMs: null,
      hg: { status: "WAITING_CDP", matchCount: 0, matchedToTc: 0, generatedAt: null },
    },
    view: "home",
    curCat: null, curMatch: null, curMarket: null,
    rMatch: null, rType: "1X2",
    catQ: "",
    oppFilter: { cat: "ALL", status: "ALL", sort: "roi" },
    histFilter: "ALL",
    session: { detected: 0, recalced: 0, invalidated: 0 },
    initDone: false,
    // 数据源健康（采集延迟持续游走；状态由延迟决定）
    health: {
      A: { lat: 0.75, ok: 0 },
      B: { lat: 0.95, ok: 0 },
      C: { lat: 1.15, ok: 0 },
    },
    lastQuoteAt: null,              // 最近一次报价漂移时间
    tickerFeed: [],                 // 最近报价变化（首页 ticker 数据源）
  };

  // ---------- 持久化 ----------
  const LS = {
    get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { } },
  };
  function persist() {
    LS.set("fh_cart", S.cart);
    LS.set("fh_tracks", S.tracks);
    LS.set("fh_events", S.events);
  }

  // ---------- 工具 ----------
  const r2 = v => Math.round(v * 100) / 100;
  const ri = n => Math.floor(Math.random() * n);
  function pick(arr) { return arr[ri(arr.length)]; }
  function pad(n) { return String(n).padStart(2, "0"); }
  function hms(d) { d = d || new Date(); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); }
  function hm(d) { d = d || new Date(); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function fmtMoney(v) { return (v >= 0 ? "+" : "-") + "¥" + Math.abs(v).toFixed(2); }
  function esc(x) { return String(x == null ? "" : x).replace(/[&<>"]/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[s])); }
  function stepFloor(v, p) {
    const d = PDEF[p];
    let s = Math.floor(v / d.step) * d.step;
    if (s > d.max) s = d.max;
    return s;
  }

  // ---------- 主题（ThemeProvider） ----------
  const theme = {
    get() {
      // 支持 URL 参数 ?theme=dark|light（便于测试与分享主题链接）
      try {
        const u = new URLSearchParams(location.search).get("theme");
        if (u === "light" || u === "dark") return u;
      } catch (e) { }
      let t = null;
      try { t = localStorage.getItem("fh_theme"); } catch (e) { }
      if (t === "light" || t === "dark") return t;
      return (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
    },
    apply(t) {
      document.documentElement.setAttribute("data-theme", t);
      const btn = document.getElementById("theme-toggle");
      if (btn) btn.textContent = t === "dark" ? "日" : "夜";
    },
    toggle() {
      const cur = document.documentElement.getAttribute("data-theme") || "dark";
      const next = cur === "dark" ? "light" : "dark";
      const root = document.documentElement;
      root.classList.add("theme-transition");
      try { localStorage.setItem("fh_theme", next); } catch (e) { }
      theme.apply(next);
      setTimeout(() => root.classList.remove("theme-transition"), 520);
    },
  };

  // ============================================================
  //  模拟市场引擎
  // ============================================================
  // 实时行情脉冲（市场脉搏数据，随每次报价变化增长）
  let pulseData = Array.from({ length: 64 }, () => (Math.random() - 0.5) * 3);
  function pulsePush(v) {
    pulseData.push(Math.max(-6, Math.min(6, v)));
    if (pulseData.length > 72) pulseData.shift();
  }
  function fairOdds(n, ret) {
    // ret=目标返奖率（玩家期望返还比例）；odds = ret/p，使 Σ1/odds = 1/ret
    const w = Array.from({ length: n }, () => 0.3 + Math.random() * 1.3);
    const sum = w.reduce((a, b) => a + b, 0);
    return w.map(x => String(r2(ret / (x / sum))));
  }

  function buildMatch(id, idx) {
    const kickoff = new Date(Date.now() + (2 + idx * 0.7) * 3600000);
    const m = {
      id, idx,
      home: "HOME", away: "AWAY",
      comp: pick(COMPS),
      kickoff,
      status: "SCHEDULED",
      markets: {},
    };
    // 市场基线：各平台围绕同一公平赔率，但对不同结果有方向性定价优势（模拟真实报价：跨平台才有价差）
    MARKETS.forEach(def => {
      const base = fairOdds(def.sels.length, 0.925 + Math.random() * 0.045);
      const n = def.sels.length;
      const platforms = {};
      PLATFORMS.forEach((p, pi) => {
        // 平台-结果级优势因子，相位错开（A/B/C 各自在不同结果上占优），因子围绕 1 保持平台整体抽水
        const phase = pi * (Math.PI * 2 / PLATFORMS.length);
        const odds = base.map((b, i) => {
          const dir = Math.cos((i / Math.max(1, n - 1)) * Math.PI * 2 + phase);
          const f = 1 + dir * 0.032 + (Math.random() - 0.5) * 0.02;
          return String(r2(Number(b) * f));
        });
        platforms[p] = { odds: [...odds], base: [...odds], status: "OPEN", version: 1, updatedAt: Date.now() };
      });
      m.markets[def.type] = { type: def.type, platforms, changes: [], marketBase: base };
    });
    return m;
  }

  function assignTeams() {
    const pool = [...TEAMS];
    for (let i = pool.length - 1; i > 0; i--) {
      const j = ri(i + 1);
      [pool[i], pool[j]] = [pool[j], pool[i]];
    }
    S.matches.forEach((m, i) => {
      m.home = pool[(i * 2) % pool.length];
      m.away = pool[(i * 2 + 1) % pool.length];
      if (m.home === m.away) m.away = pool[(i * 2 + 3) % pool.length];
    });
  }

  function initEngine(keepSeed) {
    const n = S.settings.n;
    S.matches = [];
    S.opps = {};
    for (let i = 0; i < n; i++) S.matches.push(buildMatch("M" + String(i + 1).padStart(3, "0"), i));
    // id 形如 M001
    assignTeams();
    // 注入初始套利
    SEED.forEach(([id, type]) => {
      if (S.matches.find(m => m.id === id)) injectArbitrage(id, type);
    });
    // 首次全量计算（不 toast）
    S.matches.forEach(m => {
      MARKETS.forEach(def => {
        S.opps[m.id + "|" + def.type] = calc(m, def.type);
      });
    });
    seedTicker();
    S.initDone = true;
  }

  // ============================================================
  //  数据模式（REAL HG 真实数据 / DEMO 模拟演示）
  // ============================================================
  const DEMO_PLATFORMS = ["A", "B", "C"];
  const REAL_PLATFORMS = ["TC", "HG", "DAFABET"];
  function applyPlatforms() {
    const want = S.dataMode === "real" ? REAL_PLATFORMS : DEMO_PLATFORMS;
    PLATFORMS.length = 0;
    want.forEach(p => PLATFORMS.push(p));
    updateModeChip();
  }
  function updateModeChip() {
    const chip = document.getElementById("mode-chip");
    const txt = document.getElementById("mode-txt");
    if (!chip) return;
    chip.classList.toggle("demo", S.dataMode !== "real");
    if (txt) txt.textContent = S.dataMode === "real" ? "真实数据" : "演示模式";
  }
  function resolveDataMode() {
    // URL ?mode=demo|real 优先，其次 localStorage fh_mode，默认 real
    try {
      const u = new URLSearchParams(location.search).get("mode");
      if (u === "demo" || u === "real") return u;
    } catch (e) { }
    let m = null;
    try { m = localStorage.getItem("fh_mode"); } catch (e) { }
    if (m === "demo" || m === "real") return m;
    return "real";
  }

  // HG-first: build match from HG format (quotes + markets direct)
  function parseHGDate(s) {
  if (!s) return new Date();
  // "10-20 15:00:00"
  var m = s.match(/^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})$/);
  if (m) { var now = new Date(); return new Date(now.getFullYear(), parseInt(m[1])-1, parseInt(m[2]), parseInt(m[3]), parseInt(m[4]), parseInt(m[5])); }
  // "10-08 08:30p"
  m = s.match(/^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})([ap])$/i);
  if (m) { var now = new Date(); var h = parseInt(m[3]); if (m[5].toLowerCase()==='p'&&h<12) h+=12; if (m[5].toLowerCase()==='a'&&h===12) h=0; return new Date(now.getFullYear(), parseInt(m[1])-1, parseInt(m[2]), h, parseInt(m[4])); }
  return new Date(s);
}

function buildRealMatch(fm) {
    const q = fm.quotes || {};
    const odds = [q.HOME?.odds, q.DRAW?.odds, q.AWAY?.odds].map(Number);
    if (odds.some(isNaN)) return null;  // skip matches without valid 1X2
    const updatedAt = fm.updated_at ? new Date(fm.updated_at).getTime() : Date.now();
    // HG is the primary platform
    const platforms = {
      HG: { odds: odds.map(String), base: odds.map(String), status: "OPEN",
            version: 1, updatedAt },
    };
    // Also attach TC quotes if available (from matched TC data)
    const hq = fm.hg_quotes || fm.tc_quotes || {};
    if (hq.HOME?.odds && hq.DRAW?.odds && hq.AWAY?.odds) {
      const ho = [hq.HOME.odds, hq.DRAW.odds, hq.AWAY.odds].map(Number);
      const hat = fm.hg_updated_at ? new Date(fm.hg_updated_at).getTime() : updatedAt;
      platforms.TC = { odds: ho.map(String), base: ho.map(String), status: "OPEN",
        version: Math.max(hq.HOME.quote_version || 0, hq.DRAW.quote_version || 0, hq.AWAY.quote_version || 0) || 1,
        updatedAt: hat };
    }
    // V4.2: Build all market types from backend fm.markets (DYNAMIC - accepts any type)
    const allMarkets = {};
    const backendMarkets = fm.markets || {};
    // Expanded type map; unknown types pass through as-is
    const mtNameMap = {
      "MATCH_RESULT": "1X2", "ASIAN_HANDICAP": "AH", "OVER_UNDER": "OU",
      "EVEN_ODD": "EO", "GOAL_LINE": "GL", "HALF_TIME_1X2": "HT1X2",
      "HALF_TIME_OU": "HTOU", "CORNER_OU": "CNROU", "SCORE_YN": "SCORE",
      "SHOTS_YN": "SHOTS", "TOTAL_SHOTS_YN": "TOTS", "PENALTY_1X2": "PEN",
      "RACE_EVEN": "RE", "HALF_TIME_AH": "HOU"
    };
    for (const [mtDb, qdata] of Object.entries(backendMarkets)) {
      const mtType = mtNameMap[mtDb] || mtDb;
      const selOdds = {};
      for (const [code, info] of Object.entries(qdata)) {
        if (code === "line" || code === "code" || code === "type") continue;
        selOdds[code] = { odds: String(info.odds || info), source_status: info.source_status || "OPEN" };
      }
      // For 1X2, preserve the existing platforms structure
      if (mtType === "1X2") {
        allMarkets["1X2"] = { type: "1X2", platforms, changes: [], marketBase: odds.map(String), backendQuotes: selOdds };
      } else if (selOdds && Object.keys(selOdds).length > 0) {
        allMarkets[mtType] = { type: mtType, selections: selOdds, changes: [], marketBase: [] };
      }
    }
    // Ensure 1X2 always exists
    if (!allMarkets["1X2"]) {
      allMarkets["1X2"] = { type: "1X2", platforms, changes: [], marketBase: odds.map(String) };
    }
    return {
      id: fm.match_id, idx: 0,
      home: fm.home, away: fm.away,
      comp: fm.competition || "",
      kickoff: parseHGDate(fm.kickoff_time_utc || fm.kickoff_time),
      status: "SCHEDULED", real: true,
      tcReturnRate: fm.tc_return_rate_pct, tcMargin: fm.tc_margin_pct,
      markets: allMarkets,
    };
  }

  function normTeam(v) {
    return String(v || "").toLowerCase()
      .replace(/&/g, " and ")
      .replace(/\b(fc|cf|sc|afc|club)\b/g, " ")
      .replace(/[^a-z0-9\u4e00-\u9fff]/g, "");
  }

  function mergeDafabetMulti(feed) {
    // V4.2: merge DAFABET AH/OU markets into matching TC matches
    const dm = feed.dafabet_multi;
    if (!dm || !Array.isArray(dm) || !dm.length) return;
    const dmMap = new Map();
    dm.forEach(d => {
      const k = normTeam(d.home) + "|" + normTeam(d.away);
      if (k !== "|") dmMap.set(k, d);
    });
    let merged = 0;
    S.matches.forEach(m => {
      const x = dmMap.get(normTeam(m.home) + "|" + normTeam(m.away));
      if (!x || !x.markets) return;
      // Add DAFABET AH/OU markets with platform-style structure
      for (const [mt, info] of Object.entries(x.markets)) {
        if (m.markets[mt]) continue;
        const sels = info.selections || {};
        const codes = Object.keys(sels);
        const odds = codes.map(c => String(sels[c].odds || ""));
        const base = codes.map(c => String(sels[c].odds || ""));
        m.markets[mt] = {
          type: mt,
          line: info.line || null,
          source: "DAFABET",
          platforms: {
            DAFABET: {
              odds: odds,
              base: base,
              status: "OPEN",
              version: 1,
              updatedAt: Date.now()
            }
          },
          changes: [],
          marketBase: base
        };
        merged++;
      }
    });
    if (merged > 0) console.log("[V4.2] Merged " + merged + " DAFABET AH/OU markets into matches");
  }

  function mergeHGFeed(feed) {
    if (!feed || !Array.isArray(feed.hg_matches || feed.matches)) return;
    const hm = new Map();
    (feed.hg_matches || feed.matches).forEach(m => {
      const k = normTeam(m.home) + "|" + normTeam(m.away);
      if (k !== "|") hm.set(k, m);
    });
    let matched = 0;
    const matchedHgIds = new Set();
    S.matches.forEach(m => {
      const x = hm.get(normTeam(m.home) + "|" + normTeam(m.away));
      if (!x) return;
      matchedHgIds.add(x.match_id);
      const market = m.markets["1X2"];
      if (!market) return;
      if (x?.quotes?.HOME?.odds && x?.quotes?.DRAW?.odds && x?.quotes?.AWAY?.odds) {
        const ho = [x.quotes.HOME.odds, x.quotes.DRAW.odds, x.quotes.AWAY.odds].map(String);
        const updatedAt = x.updated_at ? new Date(x.updated_at).getTime() : Date.now();
        const old = market.platforms.HG;
        market.platforms.HG = { odds: ho, base: old?.base || ho.slice(), status: "OPEN",
          version: Math.max(x.quotes.HOME.quote_version || 0, x.quotes.DRAW.quote_version || 0, x.quotes.AWAY.quote_version || 0) || 1,
          updatedAt };
        [0,1,2].forEach((i) => {
          if (old?.odds?.[i] != null && old.odds[i] !== ho[i]) {
            market.changes.unshift({ts:hms(), platform:"HG", sel:i, old:old.odds[i], now:ho[i], note:"HG 实时变盘"});
          }
        });
        market.changes = market.changes.slice(0, 40);
        matched += 1;
      }
      // V5: Add AH/OU/EO markets from HG to matched TC matches
      var xMk = x.markets || {};
      addHGMarkets(m, xMk);
    });
    // V5: Add unmatched HG-only matches (with full markets)
    var existingHgIds = new Set(S.matches.map(m => m.id));
    (feed.hg_matches || feed.matches).forEach(x => {
      if (matchedHgIds.has(x.match_id)) return;
      const q = x.quotes || {};
      const odds = [q.HOME?.odds, q.DRAW?.odds, q.AWAY?.odds].map(Number);
      if (odds.some(isNaN)) return;
      const updatedAt = x.updated_at ? new Date(x.updated_at).getTime() : Date.now();
      const isStale = x.stale === true || x.cache === true;
      var mk = {};
      mk["1X2"] = { type: "1X2",
        platforms: { HG: { odds: odds.map(String), base: odds.map(String),
          status: isStale ? "CLOSED" : "OPEN", version: 1, updatedAt } },
        changes: [], marketBase: odds.map(String) };
      const xMk = x.markets || {};
      addHGMarketsRaw(mk, xMk, isStale, updatedAt);
      if (existingHgIds.has("HG-" + x.match_id)) return;
      S.matches.push({
        id: "HG-" + x.match_id, idx: 0,
        home: x.home, away: x.away,
        comp: x.competition || "",
        kickoff: x.kickoff_time_utc ? new Date(x.kickoff_time_utc) : new Date(),
        status: isStale ? "STALE" : "SCHEDULED",
        real: true, hgOnly: true, stale: isStale,
        markets: mk,
      });
    });
    S.real.hg = {
      status: feed.source_status || "CONNECTED",
      matchCount: feed.match_count || 0,
      matchedToTc: feed?.sources?.HG?.matched_to_tc ?? matched,
      generatedAt: feed.generated_at || null,
    };
    // Auto-register new market types from HG data
    _scanAndRegisterMarkets();
    // V5: Recalculate ALL markets, not just 1X2
    S.opps = {};
    MARKETS.forEach(def => {
      S.matches.forEach(m => {
        S.opps[m.id + "|" + def.type] = calc(m, def.type);
      });
    });
    if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
  }
  // V5 helper: add HG markets (AH/OU/EO) to an existing match
  function addHGMarkets(m, hmMk) {
    var mk = m.markets;
    addHGMarketsRaw(mk, hmMk, m.stale, Date.now());
  }
  function addHGMarketsRaw(mk, hmMk, isStale, updatedAt) {
    var s = isStale ? "CLOSED" : "OPEN";
    // AH line 1 (HR)
    var ahArr = Array.isArray(hmMk["AH"]) ? hmMk["AH"] : (hmMk["AH"] ? [hmMk["AH"]] : []);
    var ahFirst = ahArr[0];
    if (ahFirst) {
      var ahOdds = [parseFloat(ahFirst.HOME?.odds || 0), parseFloat(ahFirst.AWAY?.odds || 0)];
      mk["AH"] = { type: "AH", line: ahFirst.line || "",
        platforms: { HG: { odds: ahOdds.map(String), base: ahOdds.map(String),
          status: s, version: 1, updatedAt } },
        changes: [], marketBase: ahOdds.map(String) };
    }
    // OU
    var ou = hmMk["OU"];
    if (ou && ou.OVER && ou.UNDER) {
      var ouOdds = [parseFloat(ou.OVER.odds || 0), parseFloat(ou.UNDER.odds || 0)];
      mk["OU"] = { type: "OU", line: ou.line || "",
        platforms: { HG: { odds: ouOdds.map(String), base: ouOdds.map(String),
          status: s, version: 1, updatedAt } },
        changes: [], marketBase: ouOdds.map(String) };
    }
    // EO (Even/Odd)
    var eo = hmMk["EO"];
    if (eo && eo.EVEN && eo.ODD) {
      var eoOdds = [parseFloat(eo.EVEN.odds || 0), parseFloat(eo.ODD.odds || 0)];
      mk["EO"] = { type: "EO", line: eo.line || "",
        platforms: { HG: { odds: eoOdds.map(String), base: eoOdds.map(String),
          status: s, version: 1, updatedAt } },
        changes: [], marketBase: eoOdds.map(String) };
    }
    var gl = hmMk["GL"]; if (gl && gl.OVER && gl.UNDER) { var glOdds = [parseFloat(gl.OVER.odds || 0), parseFloat(gl.UNDER.odds || 0)]; mk["GL"] = { type: "GL", line: gl.line || "", platforms: { HG: { odds: glOdds.map(String), base: glOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: glOdds.map(String) }; }
    var ht1x2 = hmMk["HT1X2"]; if (ht1x2 && ht1x2.HOME && ht1x2.DRAW && ht1x2.AWAY) { var ht1x2Odds = [parseFloat(ht1x2.HOME.odds || 0), parseFloat(ht1x2.DRAW.odds || 0), parseFloat(ht1x2.AWAY.odds || 0)]; mk["HT1X2"] = { type: "HT1X2", platforms: { HG: { odds: ht1x2Odds.map(String), base: ht1x2Odds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: ht1x2Odds.map(String) }; }
    var htou = hmMk["HTOU"]; if (htou && htou.OVER && htou.UNDER) { var htouOdds = [parseFloat(htou.OVER.odds || 0), parseFloat(htou.UNDER.odds || 0)]; mk["HTOU"] = { type: "HTOU", line: htou.line || "", platforms: { HG: { odds: htouOdds.map(String), base: htouOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: htouOdds.map(String) }; }
    var cnrou = hmMk["CNROU"]; if (cnrou && cnrou.OVER && cnrou.UNDER) { var cnrouOdds = [parseFloat(cnrou.OVER.odds || 0), parseFloat(cnrou.UNDER.odds || 0)]; mk["CNROU"] = { type: "CNROU", line: cnrou.line || "", platforms: { HG: { odds: cnrouOdds.map(String), base: cnrouOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: cnrouOdds.map(String) }; }
    var re = hmMk["RE"]; if (re && re.HOME && re.AWAY) { var reOdds = [parseFloat(re.HOME.odds || 0), parseFloat(re.AWAY.odds || 0)]; mk["RE"] = { type: "RE", line: re.line || "", platforms: { HG: { odds: reOdds.map(String), base: reOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: reOdds.map(String) }; }
    var pen = hmMk["PEN"]; if (pen && pen.HOME && pen.DRAW && pen.AWAY) { var penOdds = [parseFloat(pen.HOME.odds || 0), parseFloat(pen.DRAW.odds || 0), parseFloat(pen.AWAY.odds || 0)]; mk["PEN"] = { type: "PEN", line: "", platforms: { HG: { odds: penOdds.map(String), base: penOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: penOdds.map(String) }; }
    var yns = ["SCORE","SHOTS","TOTS"]; for (var yi = 0; yi < yns.length; yi++) { var t = yns[yi]; var yn = hmMk[t]; if (yn && yn.YES && yn.NO) { var ynOdds = [parseFloat(yn.YES.odds || 0), parseFloat(yn.NO.odds || 0)]; mk[t] = { type: t, line: "", platforms: { HG: { odds: ynOdds.map(String), base: ynOdds.map(String), status: s, version: 1, updatedAt } }, changes: [], marketBase: ynOdds.map(String) }; } }
  }

  let realLoading = false;
  function applyRealFeed(feed) {
      if (!feed || !Array.isArray(feed.matches)) throw new Error("invalid real feed");
      // 旧赔率（变化检测）
      const prevOdds = {};
      S.matches.forEach(m => {
        const mk = m.markets["1X2"];
        if (mk && mk.platforms.HG) {
          mk.platforms.HG.odds.forEach((o, i) => { prevOdds[m.id + "|" + i] = Number(o); });
        }
      });
      const newMatches = feed.matches.map(buildRealMatch).filter(Boolean);
      // 迁移旧 changes 并为真实变盘生成记录
      newMatches.forEach(m => {
        const old = S.matches.find(x => x.id === m.id);
        const mk = m.markets["1X2"];
        if (old && old.markets["1X2"]) {
          mk.changes = old.markets["1X2"].changes || [];
          const newPf = mk.platforms.HG;
          ["HOME", "DRAW", "AWAY"].forEach((code, i) => {
            const po = prevOdds[m.id + "|" + i], nv = Number(newPf.odds[i]);
            if (po != null && po !== nv) {
              mk.changes.unshift({ ts: hms(), platform: "HG", sel: i,
                old: String(po), now: String(nv), note: "HG 行情变盘" });
              if (mk.changes.length > 40) mk.changes.pop();
            }
          });
        }
      });
      // HG-only matches (no TC counterpart) — render independently
      if (Array.isArray(feed.hg_matches)) {
        const tcIds = new Set(newMatches.map(m => m.id));
        feed.hg_matches.forEach(hm => {
          if (tcIds.has(String(hm.match_id))) return;
          const q = hm.quotes || {};
          const odds = [q.HOME?.odds, q.DRAW?.odds, q.AWAY?.odds].map(Number);
          if (odds.some(isNaN)) return;
          const updatedAt = hm.updated_at ? new Date(hm.updated_at).getTime() : Date.now();
          // P0 修复：stale/cache 数据标记为过期，不参与实时计算
          const isStale = hm.stale === true || hm.cache === true;
          var mk = {};
          // 1X2
          mk["1X2"] = { type: "1X2",
            platforms: { HG: { odds: odds.map(String), base: odds.map(String),
              status: isStale ? "CLOSED" : "OPEN", version: 1, updatedAt } },
            changes: [], marketBase: odds.map(String) };
          // Use addHGMarketsRaw for ALL remaining market types from HG data
          var hmMk = hm.markets || {};
          addHGMarketsRaw(mk, hmMk, isStale, updatedAt);
          // Also handle AH array format
          var ahData = hmMk["AH"];
          if (ahData && !mk["AH"]) {
            var ahArr = Array.isArray(ahData) ? ahData : [ahData];
            var ahFirst = ahArr[0];
            if (ahFirst) {
              var ahOdds = [parseFloat(ahFirst.HOME && ahFirst.HOME.odds || 0), parseFloat(ahFirst.AWAY && ahFirst.AWAY.odds || 0)];
              mk["AH"] = { type: "AH", line: ahFirst.line || "",
                platforms: { HG: { odds: ahOdds.map(String), base: ahOdds.map(String),
                  status: isStale ? "CLOSED" : "OPEN", version: 1, updatedAt } },
                changes: [], marketBase: ahOdds.map(String) };
            }
          }
          newMatches.push({
            id: "HG-" + hm.match_id, idx: 0,
            home: hm.home, away: hm.away,
            comp: hm.competition || "",
            kickoff: parseHGDate(hm.kickoff_time_utc || hm.kickoff_time),
            status: isStale ? "STALE" : "SCHEDULED",
            real: true, hgOnly: true,
            stale: isStale,
            markets: mk,
          });
        });
      }
      S.matches = newMatches;
      // Auto-register any new market types found in the data
      _scanAndRegisterMarkets();
      // V4.2: 全玩法实时计算 (all discovered markets)
      S.opps = {};
      MARKETS.forEach(def => {
        S.matches.forEach(m => {
          S.opps[m.id + "|" + def.type] = calc(m, def.type);
        });
      });
      S.real.source = feed.source;
      S.real.status = feed.source_status;
      S.real.generatedAt = feed.generated_at;
      S.real.lastLoaded = Date.now();
      S.real.marketNote = feed.market_note;
      S.real.leagues = Array.isArray(feed.leagues) ? feed.leagues : [];
      S.real.leagueCount = feed.league_count || S.real.leagues.length;
      seedRealTicker();
      if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
  }

  async function loadRealFeed() {
    if (realLoading) return;
    realLoading = true;
    try {
      const resp = await fetch("/api/v1/real/feed", { cache: "no-store" });
      if (!resp.ok) throw new Error("feed HTTP " + resp.status);
      const feed = await resp.json();
      applyRealFeed(feed);
      if (feed.sources?.HG) {
        S.real.hg = {
          status: feed.sources.HG.status,
          matchCount: feed.sources.HG.match_count || 0,
          matchedToTc: feed.sources.HG.matched_to_tc || 0,
          generatedAt: feed.sources.HG.generated_at || null,
        };
        S.real.hg_http = {
          status: feed.sources.HG.status,
          transport: "http-direct",
          matchCount: feed.sources.HG.match_count || 0,
          generatedAt: feed.sources.HG.generated_at || null,
          extra: feed.sources.HG.extra || {},
        };
      }
      mergeHGFeed(feed);
      mergeDafabetMulti(feed);
      try { if (typeof renderHgTable === "function") renderHgTable(); } catch(e) {}
    } catch (e) {
      S.real.status = "ERROR";
      if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
    } finally {
      realLoading = false;
    }
  }

  function seedRealTicker() {
    const recs = [];
    S.matches.forEach(m => {
      const mk = m.markets["1X2"];
      (mk.changes || []).slice(0, 3).forEach(c => {
        const def = MARKETS.find(d => d.type === "1X2");
        const pf = mk.platforms[c.platform] || mk.platforms.HG;
        recs.push({
          ts: pf.updatedAt, matchId: m.id, type: "1X2",
          title: m.home + " vs " + m.away,
          platform: c.platform, label: def.sels[c.sel][1],
          old: c.old, now: c.now,
          pct: ((Number(c.now) - Number(c.old)) / Number(c.old)) * 100,
        });
      });
    });
    if (recs.length) {
      recs.sort((a, b) => b.ts - a.ts);
      S.tickerFeed = recs.slice(0, 30);
    } else {
      // 首次无变化：用当前真实报价构造静态展示
      const t = [];
      const labels = ["主胜", "平局", "客胜"];
      S.matches.forEach(m => {
        var mk1x2 = m.markets && m.markets["1X2"]; var pf = mk1x2 && mk1x2.platforms && (mk1x2.platforms.HG || mk1x2.platforms.TC) || {};
        if (!pf) return;
        [0, 1, 2].forEach(i => {
          t.push({
            ts: pf.updatedAt, matchId: m.id, type: "1X2",
            title: m.home + " vs " + m.away,
            platform: (mk1x2 && mk1x2.platforms && mk1x2.platforms.TC) ? "TC" : "HG", label: labels[i],
            old: pf.odds[i], now: pf.odds[i], pct: 0,
          });
        });
      });
      S.tickerFeed = t.slice(0, 30);
    }
  }

  // ---- SSE 实时推送（1s 级端到端）----
  let realEventSource = null;
  function ensureRealStream() {
    if (realEventSource != null) return;
    try {
      const es = new EventSource("/api/v1/stream");
      realEventSource = es;
      es.addEventListener("connected", ev => {
        try {
          const d = JSON.parse(ev.data);
          S.real.stream = true;
          S.real.worker = d.worker;
          if (d.hg) {
            S.real.hg = { status: d.hg.connected ? "CONNECTED" : (d.hg.enabled ? "WAITING_CDP" : "DISABLED"),
              matchCount: d.hg.cache_matches || 0, matchedToTc: 0, generatedAt: d.hg.cache_generated_at || null };
          }
          if (d.hg_http) {
            S.real.hg_http = {
              status: d.hg_http.bootstrapped ? "CONNECTED" : (d.hg_http.enabled ? "WAITING_LOGIN" : "DISABLED"),
              bootstrapped: d.hg_http.bootstrapped,
              lastError: d.hg_http.last_error || null,
              matchCount: d.hg_http.cache_matches || 0,
            };
          }
          S.real.lastEventAt = Date.now();
        } catch (e) { }
        if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
      });
      es.addEventListener("update", ev => {
        let d;
        try { d = JSON.parse(ev.data); } catch (e) { return; }
        S.real.lastEventAt = Date.now();
        if (d.worker) S.real.worker = d.worker;
        if (d.server_time) {
          const st = new Date(d.server_time).getTime();
          if (!isNaN(st)) S.real.pushLatencyMs = Math.max(0, Date.now() - st);
        }
        if (d.event === "quotes_changed") {
          if (d.feed) {
            try { applyRealFeed(d.feed); } catch (e) { S.real.status = "ERROR"; }
          } else {
            loadRealFeed();
          }
          if (window.AppViews && window.AppViews.updateSysLive) window.AppViews.updateSysLive(d.worker);
        } else if (d.event === "hg_quotes_changed") {
          if (d.feed) {
            try { mergeHGFeed(d.feed); } catch (e) { S.real.status = "PARTIAL"; }
          }
          S.real.hg = {
            status: d.feed?.source_status || "CONNECTED",
            matchCount: d.feed?.match_count || 0,
            matchedToTc: S.matches.filter(m => !!m.markets?.["1X2"]?.platforms?.HG).length,
            generatedAt: d.feed?.generated_at || null,
          };
          S.real.lastEventAt = Date.now();
          if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
        } else if (d.event === "heartbeat") {
          if (window.AppViews && window.AppViews.updateSysLive)
            window.AppViews.updateSysLive(d.worker);
        } else if (d.event === "source_status") {
          if (d.status === "ERROR") S.real.status = "ERROR";
          if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
        }
      });
      es.onopen = () => { S.real.stream = true; };
      es.onerror = () => {
        S.real.stream = false;         // EventSource 自动重连
        if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
      };
    } catch (e) {
      S.real.stream = false;
    }
  }
  function closeRealStream() {
    if (realEventSource != null) {
      realEventSource.close();
      realEventSource = null;
    }
    S.real.stream = false;
  }

  // 兜底轮询：仅用于 SSE 断线/异常；正常情况下实时更新完全靠 SSE。
  let realTimer = null;
  function ensureRealTimer() {
    if (realTimer == null) {
      realTimer = setInterval(() => {
        if (!document.hidden && S.dataMode === "real" && !S.real.stream) loadRealFeed();
      }, 2000);
    }
  }

  // 构造开盘首报（让首页 ticker 初始有内容）
  function seedTicker() {
    const recs = [];
    const shuffled = [...S.matches].sort(() => Math.random() - 0.5).slice(0, 10);
    shuffled.forEach(m => {
      const types = Object.keys(m.markets);
      const type = types[Math.floor(Math.random() * types.length)];
      const def = MARKETS.find(d => d.type === type);
      if (!def || !def.sels || !def.sels.length) return;
      const mk = m.markets[type];
      const p = PLATFORMS[Math.floor(Math.random() * PLATFORMS.length)];
      const si = Math.floor(Math.random() * def.sels.length);
      const now = Number(mk.platforms[p].odds[si]);
      const old = r2(now - (Math.random() - 0.4) * 0.09);
      recs.push({
        ts: Date.now() - Math.floor(Math.random() * 90000),
        matchId: m.id, type,
        title: m.home + " vs " + m.away,
        platform: p, label: def.sels[si][1],
        old: String(old), now: String(now),
        pct: ((now - old) / old) * 100,
      });
    });
    recs.sort((a, b) => b.ts - a.ts);
    S.tickerFeed = recs;
  }

  // 改变报价（版本+1 + 记录）
  function setOdds(mk, p, si, nv, note) {
    const pf = mk.platforms[p];
    const old = Number(pf.odds[si]);
    nv = r2(nv);
    if (nv === old) return false;
    pf.odds = pf.odds.map((o, i) => i === si ? String(nv) : o);
    pf.version++;
    pf.updatedAt = Date.now();
    mk.changes.unshift({ ts: hms(), platform: p, sel: si, old: String(old), now: String(nv), note: note || "" });
    if (mk.changes.length > 40) mk.changes.pop();
    // 首页实时 ticker 数据源
    const m = S.matches.find(mm => mm.markets[mk.type] === mk);
    if (m) {
      const def = MARKETS.find(d => d.type === mk.type);
      if (!def || !def.sels) return true;
      const t = {
        ts: Date.now(), matchId: m.id, type: mk.type,
        title: m.home + " vs " + m.away,
        platform: p, label: def.sels[si][1],
        old: String(old), now: String(nv),
        pct: ((nv - old) / old) * 100,
      };
      S.tickerFeed.unshift(t);
      if (S.tickerFeed.length > 30) S.tickerFeed.pop();
      pulsePush(t.pct);
    }
    return true;
  }

  // 注入套利（在某平台抬高某腿，使组合 Σ<1；自动选抬升系数最小的腿）
  function injectArbitrage(matchId, type) {
    const m = S.matches.find(x => x.id === matchId);
    if (!m) return false;
    const def = MARKETS.find(d => d.type === type);
    const mk = m.markets[type];
    const bestFor = i => Math.max(...PLATFORMS.map(pp => Number(mk.platforms[pp].odds[i])));
    // 当前最优平台
    const topPlat = i => {
      let p = PLATFORMS[0], v = -1;
      PLATFORMS.forEach(pp => {
        const o = Number(mk.platforms[pp].odds[i]);
        if (o > v) { v = o; p = pp; }
      });
      return p;
    };
    let best = null;
    def.sels.forEach((_, si) => {
      const others = def.sels.map((_, i) => i).filter(i => i !== si);
      const sumOthers = others.reduce((s, i) => s + 1 / bestFor(i), 0);
      const denom = 1 - sumOthers - 0.015; // 安全垫（目标 Σ≈0.985）
      if (denom <= 0.02) return;
      const need = r2(1 / denom);
      const cur = bestFor(si);
      const factor = need / cur;
      if (!best || factor < best.factor) best = { si, need, factor, cur };
    });
    if (!best) return false;
    return setOdds(mk, topPlat(best.si), best.si, best.need, "报价偏离·价差");
  }

  // 收回偏离（制造失效）
  function withdrawArbitrage(matchId, type) {
    const m = S.matches.find(x => x.id === matchId);
    if (!m) return false;
    const mk = m.markets[type];
    const def = MARKETS.find(d => d.type === type);
    let any = false;
    PLATFORMS.forEach(p => {
      const pf = mk.platforms[p];
      def.sels.forEach((_, si) => {
        const cur = Number(pf.odds[si]), base = Number(pf.base[si]);
        if (cur > base * 1.06) {
          if (setOdds(mk, p, si, r2(cur * 0.93), "偏离回收")) any = true;
        }
      });
    });
    return any;
  }

  // 一次漂移（带均值回归）
  function driftOnce() {
    const volN = { "低": 1, "中": 3, "高": 6 }[S.settings.vol];
    const changed = new Set();
    for (let k = 0; k < volN; k++) {
      const m = pick(S.matches);
      if (!m) continue;
      const type = pick(Object.keys(m.markets));
      const mk = m.markets[type];
      const def = MARKETS.find(d => d.type === type);
      if (!def || !def.sels || !def.sels.length) continue;
      const p = pick(PLATFORMS);
      const si = ri(def.sels.length);
      const pf = mk.platforms[p];
      const cur = Number(pf.odds[si]);
      const base = Number(pf.base[si]);
      const big = Math.random() < 0.07;
      const amp = (big ? 0.12 : 0.03);
      let delta = (Math.random() * 2 - 1) * amp * cur + (base - cur) * 0.22;
      const nv = r2(Math.max(1.02, Math.min(40, cur + delta)));
      if (nv !== cur && setOdds(mk, p, si, nv, big ? "大幅波动" : "")) changed.add(m.id);
    }
    return changed;
  }

  // ============================================================
  //  对冲计算（真实前端算法）
  // ============================================================
  function calc(m, type) {
    var def = MARKETS.find(d => d.type === type);
    const mk = m.markets[type];
    if (!mk) return { status: "DATA", reason: "等待该玩法开盘" };
    // If market not in registry, build def from data
    if (!def) {
      var codes = Object.keys(mk).filter(function(k) { return k !== "line" && k !== "type" && k !== "changes" && k !== "marketBase" && k !== "platforms" && k !== "selections" && k !== "backendQuotes"; });
      if (codes.length === 0) {
        // Try platforms
        var pf = mk.platforms && mk.platforms.HG;
        if (pf && pf.odds) { codes = pf.odds.map(function(_, i) { return "SEL" + i; }); }
      }
      if (codes.length === 0) return { status: "DATA", reason: "无法识别玩法选项" };
      def = { type: type, name: type, sels: codes.map(function(c) { return [c, c]; }) };
    }
    // Build legs from actual market data
    var sels = def.sels;
    // Check if this market uses platforms or selections structure
    var usePlatforms = mk.platforms && Object.keys(mk.platforms).length > 0;
    var legs;
    if (usePlatforms) {
      legs = sels.map((s, i) => {
        let best = null;
        PLATFORMS.forEach(p => {
          const pf = mk.platforms[p];
          if (!pf || pf.status !== "OPEN") return;
          const odds = Number(pf.odds[i]);
          const eff = odds * (1 + PDEF[p].rebate);
          if (!best || eff > best.eff) best = { p, odds, eff, code: s[0], label: s[1], version: pf.version, updatedAt: pf.updatedAt };
        });
        return best;
      });
    } else {
      // Use selections structure (backend-loaded markets)
      var sels2 = mk.selections || {};
      legs = sels.map(function(s) {
        var info = sels2[s[0]];
        if (!info) return null;
        return { p: "TC", odds: Number(info.odds), eff: Number(info.odds), code: s[0], label: s[1], version: 1, updatedAt: Date.now() };
      });
    }
    if (!legs || legs.some(function(x) { return !x; })) return { status: "DATA", reason: "等待更多平台报价", def };
    const sum = legs.reduce((s, l) => s + 1 / l.eff, 0);
    // 分配（目标：每状态回收相等，含输腿返水）
    const B = 1000;
    const C = B / sum;
    let stake = legs.map(l => {
      let s = C / l.eff;
      s = stepFloor(s, l.p);
      if (s < PDEF[l.p].min) s = PDEF[l.p].min;
      return s;
    });
    // 迭代平衡（精确模型下让各状态回收趋同）
    function exactPnl(stake) {
      const total = stake.reduce((a, b) => a + b, 0);
      return legs.map((l, i) => {
        let cash = stake[i] * l.odds;
        legs.forEach((lj, j) => { if (j !== i) cash += stake[j] * PDEF[lj.p].rebate; });
        return cash - total;
      });
    }
    for (let it = 0; it < 8; it++) {
      const pnl = exactPnl(stake);
      const spread = Math.max(...pnl) - Math.min(...pnl);
      const total = stake.reduce((a, b) => a + b, 0);
      if (spread < total * 0.004) break;
      legs.forEach((l, i) => {
        const total = stake.reduce((a, b) => a + b, 0);
        let reb = 0;
        legs.forEach((lj, j) => { if (j !== i) reb += stake[j] * PDEF[lj.p].rebate; });
        const cur = stake[i] * l.odds + reb;
        const nv = stepFloor(stake[i] * (C / cur), l.p);
        if (nv >= PDEF[l.p].min && nv <= PDEF[l.p].max) stake[i] = nv;
      });
    }
    const total = stake.reduce((a, b) => a + b, 0);
    const pnl = exactPnl(stake);
    const minPnl = Math.min(...pnl), maxPnl = Math.max(...pnl);
    const status = minPnl > 0.5 ? "POSITIVE" : (minPnl > -0.5 ? "BREAK" : "NO");
    return {
      def, status, legs, stake, total, pnl, minPnl, maxPnl, sum,
      roi: (minPnl / total) * 100,
      maxRoi: (maxPnl / total) * 100,
      calcAt: new Date(),
      key: m.id + "|" + type,
    };
  }

  // ============================================================
  //  重算后处理：生命周期 / 清单 / 追踪
  // ============================================================
  function addEvent(kind, text, data) {
    const e = { ts: Date.now(), tsStr: hms(), kind, text, data: data || null };
    S.events.unshift(e);
    if (S.events.length > 250) S.events.pop();
    persist();
  }

  function afterRecalc(ids) {
    ids.forEach(id => {
      const m = S.matches.find(x => x.id === id);
      if (!m) return;
      MARKETS.forEach(def => {
        const key = id + "|" + def.type;
        const prev = S.opps[key];
        const cur = calc(m, def.type);
        S.opps[key] = cur;
        if (!S.initDone) return;
        const wasPos = prev && prev.status === "POSITIVE";
        const isPos = cur.status === "POSITIVE";
        if (prev && !wasPos && isPos) {
          S.session.detected++;
          addEvent("DETECTED", `发现新机会：${m.home} VS ${m.away}（${def.name}）收益率 ${cur.roi.toFixed(2)}%`, { matchId: id, type: def.type });
          toast("pos", "发现新机会", `${m.home} VS ${m.away} · ${def.name} · ${cur.roi.toFixed(2)}%`, () => App.openMatch(id, type));
        } else if (prev && wasPos && !isPos) {
          S.session.invalidated++;
          addEvent("INVALIDATED", `机会失效：${m.home} VS ${m.away}（${def.name}）`, { matchId: id, type: def.type });
          toast("neg", "机会失效", `${m.home} VS ${m.away} · ${def.name}`, () => App.openMatch(id, type));
        } else if (prev && wasPos && isPos && Math.abs(prev.minPnl - cur.minPnl) > 1) {
          S.session.recalced++;
          addEvent("UPDATED", `机会更新：${m.home} VS ${m.away}（${def.name}）收益率 ${cur.roi.toFixed(2)}%`, { matchId: id, type: def.type });
        }
        syncCart(id, def.type, cur);
      });
    });
  }

  // 清单状态同步
  function syncCart(id, type, cur) {
    S.cart.forEach(c => {
      if (c.matchId !== id || c.marketType !== type) return;
      let changed = false;
      if (cur.status === "DATA") { c.status = "WAIT"; c.note = "等待重新计算"; }
      else if (cur.legs.length !== c.snapshot.legs.length) changed = true;
      else {
        cur.legs.forEach((l, i) => {
          const sp = c.snapshot.legs[i];
          if (l.p !== sp.p || Number(l.odds) !== Number(sp.odds)) changed = true;
        });
      }
      if (!changed) {
        if (c.status !== "BOUGHT") { c.status = "VALID"; c.note = ""; }
      } else if (cur.status === "POSITIVE") {
        if (c.status !== "BOUGHT") {
          c.status = "CHANGED"; c.note = "原方案报价已变化，建议按最新报价重算";
          if (!c.notifiedChanged) {
            c.notifiedChanged = true;
            toast("warn", "原购买方案发生变化", `${cur.def ? "" : ""}${id} · ${type} · 当前收益率 ${cur.roi ? cur.roi.toFixed(2) : "—"}%`, () => App.openCart());
          }
        }
      } else {
        if (c.status !== "BOUGHT") {
          c.status = "INVALID"; c.note = "原方案机会已失效";
          if (!c.notifiedInvalid) {
            c.notifiedInvalid = true;
            toast("neg", "购买方案失效", `${id} · ${type}`, () => App.openCart());
          }
        }
      }
      persist();
    });
  }

  // ============================================================
  //  Toast
  // ============================================================
  function toast(kind, title, text, onClick) {
    const wrap = document.getElementById("toast-wrap");
    if (!wrap) return;
    const el = document.createElement("div");
    el.className = "toast " + (kind === "pos" ? "pos" : kind === "neg" ? "neg" : "warn");
    el.innerHTML = `<span class="tx">&times;</span><div class="tt">${esc(title)}</div><div class="td">${esc(text)}</div>`;
    const t = setTimeout(() => remove(), 6500);
    function remove() {
      clearTimeout(t);
      el.classList.add("out");
      setTimeout(() => el.remove(), 260);
    }
    el.addEventListener("click", e => {
      if (e.target.classList.contains("tx")) { remove(); return; }
      if (onClick) onClick();
      remove();
    });
    while (wrap.children.length > 5) wrap.firstChild.remove();
    wrap.appendChild(el);
  }

  // ============================================================
  //  Modal
  // ============================================================
  function openModal(title, sub, body) {
    document.getElementById("m-title").textContent = title;
    document.getElementById("m-sub").innerHTML = sub || "";
    document.getElementById("m-body").innerHTML = body;
    document.getElementById("modal").classList.add("show");
  }
  function closeModal() {
    document.getElementById("modal").classList.remove("show");
  }

  // ============================================================
  //  主循环（每秒）
  // ============================================================
  function tick() {
    if (S.dataMode === "real") { updateTopbar(); return; }
    // 数据源健康：延迟向均值回归游走，偶发尖峰
    PLATFORMS.forEach((p, i) => {
      const h = S.health[p];
      const target = 0.6 + i * 0.25;
      let lat = h.lat + (target - h.lat) * 0.12 + (Math.random() - 0.5) * 0.18;
      if (Math.random() < 0.03) lat += 0.6 + Math.random() * 0.9;   // 偶发尖峰
      h.lat = Math.max(0.1, Math.min(4, lat));
      if (h.lat < 1.5) h.ok++;
    });
    const rate = { 1: 0.4, 2: 0.85, 5: 2, 10: 4 }[S.settings.speed] || 0.4;
    const base = Math.floor(rate);
    const extra = Math.random() < rate - base ? 1 : 0;
    const times = base + extra;
    const changed = new Set();
    for (let i = 0; i < times; i++) {
      driftOnce().forEach(id => changed.add(id));
    }
    if (changed.size) {
      S.lastQuoteAt = Date.now();
      afterRecalc(changed);
      if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
    }
    updateTopbar();
  }
  function setText(id, v) { const el = document.getElementById(id); if (el) el.textContent = v; }
  function updateTopbar() {
    const pos = positiveOpps();
    setText("top-opp", pos.length);
    setText("top-track", S.tracks.length);
    setText("top-cart", S.cart.length);
  }
  function positiveOpps() {
    return Object.values(S.opps).filter(o => o && o.status === "POSITIVE");
  }
  // 数据源健康状态
  function healthState(p) { return S.health[p].lat < 1.5 ? "OPEN" : "WARNING"; }
  function avgLat() { return PLATFORMS.reduce((a, p) => a + S.health[p].lat, 0) / PLATFORMS.length; }
  // 最近一次报价漂移距今秒数
  function lastWaterAgo() {
    if (!S.lastQuoteAt) return null;
    return Math.max(0, Math.round((Date.now() - S.lastQuoteAt) / 1000));
  }

  // ============================================================
  //  对外操作
  // ============================================================
  function serializeSnapshot(cur) {
    return {
      legs: cur.legs.map(l => ({ p: l.p, code: l.code, label: l.label, odds: String(l.odds), version: l.version })),
      stake: cur.stake.map(String),
      total: cur.total,
      minPnl: cur.minPnl,
      roi: cur.roi,
      tsStr: hms(cur.calcAt),
    };
  }

  const App = {
    S, MARKETS, PLATFORMS, PDEF, hms, hm, esc, fmtMoney,
    r2, pick,
    openModal, closeModal, toast,
    toggleTheme: theme.toggle,
    setDataMode(m) {
      if (m !== "real" && m !== "demo") return;
      S.dataMode = m;
      try { localStorage.setItem("fh_mode", m); } catch (e) { }
      applyPlatforms();
      S.view = "home";
      if (m === "real") {
        loadRealFeed();
        ensureRealTimer();
        ensureRealStream();
      } else {
        closeRealStream();
        initEngine();
        window.AppViews.render();
      }
    },
    toggleDataMode() {
      this.setDataMode(S.dataMode === "real" ? "demo" : "real");
    },
    calc, positiveOpps, updateTopbar,
    healthState, avgLat, lastWaterAgo,
    injectArbitrage, withdrawArbitrage, driftOnce,
    go(view) {
      S.view = view;
      try {
        window.AppViews.render();
      } catch (e) {
        console.error("[P0] go render failed:", view, e);
      }
    },
    openCat(type) {
      S.curCat = type; S.view = "cat";
      try {
        window.AppViews.render();
      } catch (e) {
        console.error("[P0] openCat render failed:", type, e);
      }
    },
    openMatch(id, type) {
      if (!id || !type) {
        console.warn("[P0] openMatch called with invalid params:", id, type);
        toast("warn", "参数缺失", "比赛ID或玩法类型为空");
        return;
      }
      // P0 修复：防重复点击锁
      if (S._navLock && S._navLock.id === id && S._navLock.type === type && Date.now() - S._navLock.ts < 300) {
        return;
      }
      S._navLock = { id, type, ts: Date.now() };
      S.curMatch = id; S.curMarket = type; S.view = "match";
      try {
        window.AppViews.render();
      } catch (e) {
        console.error("[P0] render match view failed:", e);
        toast("error", "页面渲染失败", "请刷新重试");
      }
    },
    switchMarket(type) {
      S.curMarket = type;
      window.AppViews.render();
    },
    setResearchMatch(id) {
      S.rMatch = id; window.AppViews.render();
    },
    setResearchMarket(t) {
      S.rType = t; window.AppViews.render();
    },
    openCart() {
      S.view = "cart";
      window.AppViews.render();
    },
    searchRes: [],
    openSearch() {
      document.getElementById("search-panel").classList.add("open");
      const inp = document.getElementById("search-input");
      if (inp) inp.focus();
    },
    closeSearch() {
      document.getElementById("search-panel").classList.remove("open");
      const inp = document.getElementById("search-input");
      if (inp) inp.value = "";
      this.searchRes = [];
    },
    runSearch(q) {
      q = (q || "").trim().toLowerCase();
      const panel = document.getElementById("search-panel");
      const box = document.getElementById("search-results");
      const res = [];
      if (!q) { panel.classList.remove("open"); box.innerHTML = ""; return; }
      panel.classList.add("open");
      S.matches.forEach(m => {
        if ((m.home + " " + m.away + " " + m.comp + " " + m.id).toLowerCase().includes(q)) {
          res.push({ t: "比赛", n: m.home + " VS " + m.away, s: m.comp + " · " + hm(m.kickoff) + " · " + m.id, mid: m.id, type: "1X2" });
        }
      });
      Object.values(S.opps).forEach(o => {
        if (o && o.status === "POSITIVE" && (o.key + " " + o.def.name).toLowerCase().includes(q)) {
          const mid = o.key.split("|")[0];
          const m = S.matches.find(x => x.id === mid);
          res.push({ t: "套利机会", n: m.home + " VS " + m.away, s: o.def.name + " · 净收益 " + o.roi.toFixed(2) + "%", mid, type: o.def.type });
        }
      });
      if ("数据源 平台 健康 source 延迟".includes(q)) {
        res.push({ t: "数据源", n: "模拟平台 A/B/C 健康与延迟", s: "前往系统监控", sys: true });
      }
      this.searchRes = res;
      box.innerHTML = res.length ? res.slice(0, 30).map((r, i) =>
        `<div class="sp-item" onclick="App.pickSearch(${i})"><span class="sp-type">${r.t}</span>
          <span class="sp-name">${esc(r.n)}</span><span class="sp-sub">${esc(r.s)}</span></div>`).join("")
        : '<div class="sp-item"><span class="sp-name">无匹配结果</span></div>';
    },
    pickSearch(i) {
      const r = this.searchRes[i];
      if (!r) return;
      if (r.sys) { this.closeSearch(); this.go("sys"); }
      else { this.closeSearch(); this.openMatch(r.mid, r.type); }
    },
    addToCart(id, type) {
      const cur = S.opps[id + "|" + type];
      if (!cur || cur.status !== "POSITIVE") {
        toast("warn", "当前无可行方案", "报价变化后出现机会会自动提示");
        return;
      }
      if (S.cart.find(c => c.matchId === id && c.marketType === type)) {
        toast("warn", "已在购买清单", id + " · " + type);
        return;
      }
      const body = AppViews.confirmCartBody(id, type, cur);
      openModal("确认模拟购买方案", `${id} · ${MARKETS.find(d => d.type === type).name} · 全部为模拟数据，不执行真实下单`, body);
    },
    confirmAddCart(id, type) {
      const cur = S.opps[id + "|" + type];
      const cid = "C" + Date.now().toString(36).toUpperCase();
      S.cart.push({
        cid, matchId: id, marketType: type,
        snapshot: serializeSnapshot(cur),
        status: "VALID", note: "", createdAt: Date.now(),
      });
      persist();
      closeModal();
      addEvent("CART_ADD", `加入购买清单：${id}（${type}）投入 ¥${cur.total.toFixed(2)} 最小收益 ${cur.minPnl.toFixed(2)}`, { matchId: id, type });
      S.view = "cart";
      window.AppViews.render();
    },
    refreshCartItem(cid) {
      const c = S.cart.find(x => x.cid === cid);
      if (!c) return;
      const cur = S.opps[c.matchId + "|" + c.marketType];
      if (!cur || cur.status === "DATA") { toast("warn", "等待重算", ""); return; }
      if (cur.status !== "POSITIVE") { toast("neg", "当前已无机会", ""); return; }
      c.snapshot = serializeSnapshot(cur);
      c.status = "VALID"; c.note = "";
      c.notifiedChanged = false;
      persist();
      addEvent("CART_RECALC", `清单按最新报价重算：${c.matchId}（${c.marketType}）`, { matchId: c.matchId, type: c.marketType });
      window.AppViews.render();
      toast("pos", "已按最新报价重算", c.matchId + " · " + c.marketType);
    },
    removeCart(cid) {
      S.cart = S.cart.filter(x => x.cid !== cid);
      persist();
      window.AppViews.render();
    },
    simulateBuy(cid) {
      const c = S.cart.find(x => x.cid === cid);
      if (!c) return;
      c.status = "BOUGHT";
      persist();
      addEvent("BOUGHT", `模拟购买完成：${c.matchId}（${c.marketType}），仅模拟，未发生真实交易`, { matchId: c.matchId, type: c.marketType });
      toast("pos", "模拟购买完成", c.matchId + " · 未发生真实交易");
      window.AppViews.render();
    },
    addTrack(id, type) {
      if (S.tracks.find(t => t.matchId === id && t.marketType === type)) {
        toast("warn", "已在追踪", id + " · " + type);
        return;
      }
      const cur = S.opps[id + "|" + type];
      S.tracks.push({
        tid: "T" + Date.now().toString(36).toUpperCase(),
        matchId: id, marketType: type,
        addedAt: Date.now(),
        initial: cur ? serializeSnapshot(cur) : null,
      });
      persist();
      addEvent("TRACK_ADD", `开始追踪：${id}（${type}）`, { matchId: id, type });
      S.view = "track";
      window.AppViews.render();
    },
    removeTrack(tid) {
      S.tracks = S.tracks.filter(t => t.tid !== tid);
      persist();
      window.AppViews.render();
    },
    recalcMatch(id, type) {
      const m = S.matches.find(x => x.id === id);
      if (!m) return;
      S.opps[id + "|" + type] = calc(m, type);
      addEvent("RECALC", `手动重算：${id}（${type}）`, { matchId: id, type });
      window.AppViews.render();
      toast("warn", "已重新计算", id + " · " + type);
    },
    setCatQ(v) {
      S.catQ = v;
      window.AppViews.render();
    },
    setOppFilter(k, v) {
      S.oppFilter[k] = v;
      window.AppViews.render();
    },
    setHistFilter(v) {
      S.histFilter = v;
      window.AppViews.render();
    },
    // 控制台
    toggleRun() {
      S.settings.running = !S.settings.running;
      document.getElementById("engine-status").textContent = S.settings.running ? "模拟市场运行中" : "模拟市场已暂停";
      document.getElementById("dot-engine").className = "dot " + (S.settings.running ? "g" : "o");
      window.AppViews.render();
    },
    setSpeed(v) { S.settings.speed = Number(v); window.AppViews.render(); },
    setVol(v) { S.settings.vol = v; window.AppViews.render(); },
    setN(v) {
      S.settings.n = Number(v);
      initEngine();
      addEvent("REINIT", `模拟市场重新初始化：${S.settings.n} 场`);
      window.AppViews.render();
    },
    makeNew() {
      const keys = Object.keys(S.opps).filter(k => S.opps[k].status !== "POSITIVE");
      for (let i = keys.length - 1; i > 0; i--) {
        const j = ri(i + 1);
        [keys[i], keys[j]] = [keys[j], keys[i]];
      }
      for (const key of keys) {
        const [id, type] = key.split("|");
        if (injectArbitrage(id, type)) {
          afterRecalc(new Set([id]));
          window.AppViews.render();
          return;
        }
      }
      toast("warn", "没有可制造的场", "当前几乎全部已覆盖");
    },
    makeInvalid() {
      const pos = positiveOpps();
      if (!pos.length) { toast("warn", "当前无正机会可失效", ""); return; }
      const o = pick(pos);
      withdrawArbitrage(o.legs[0] && o.key.split("|")[0], o.key.split("|")[1]);
      afterRecalc(new Set([o.key.split("|")[0]]));
      window.AppViews.render();
    },
    triggerDrift() {
      const changed = new Set();
      driftOnce().forEach(id => changed.add(id));
      driftOnce().forEach(id => changed.add(id));
      if (changed.size) { S.lastQuoteAt = Date.now(); afterRecalc(changed); window.AppViews.render(); }
    },
    reinit() {
      initEngine();
      addEvent("REINIT", "模拟市场重新初始化");
      window.AppViews.render();
    },
  };

  window.App = App;

  // ---------- 启动 ----------
  theme.apply(theme.get());
  S.cart = LS.get("fh_cart", []);
  S.tracks = LS.get("fh_tracks", []);
  S.events = LS.get("fh_events", []);
  S.dataMode = resolveDataMode();
  applyPlatforms();
  if (S.dataMode === "real") {
    loadRealFeed();
    ensureRealTimer();
    ensureRealStream();
  } else {
    initEngine();
  }

  // 侧栏
  document.querySelectorAll(".side-item[data-view]").forEach(el => {
    el.addEventListener("click", () => {
      document.querySelectorAll(".side-item").forEach(x => x.classList.remove("active"));
      el.classList.add("active");
      S.view = el.dataset.view;
      if (window.AppViews) { window.AppViews.render(); try { renderHgTable(); } catch(e) {} }
    });
  });
  // 时钟
  setInterval(() => {
    setText("clock", hms());
  }, 1000);
  // 主循环（页面不可见时暂停）
  setInterval(() => {
    if (S.settings.running && !document.hidden) tick();
  }, 1000);
  updateTopbar();

  // ---------- 汉堡菜单 ----------
  const hamburger = document.getElementById("hamburger");
  const mobileMenu = document.getElementById("mobile-menu");
  if (hamburger) {
    hamburger.addEventListener("click", () => {
      mobileMenu.classList.toggle("open");
    });
    mobileMenu.querySelectorAll(".nav-item").forEach(el => {
      el.addEventListener("click", () => mobileMenu.classList.remove("open"));
    });
  }

  // ---------- 导航滚动收缩 + 滚动视差 ----------
  const navbar = document.getElementById("navbar");
  window.addEventListener("scroll", () => {
    const y = window.scrollY;
    navbar.classList.toggle("shrink", y > 40);
    const hero = document.querySelector(".hero");
    if (hero) {
      hero.style.transform = `translateY(${y * 0.32}px) scale(${1 + y * 0.0001})`;
      hero.style.opacity = Math.max(0, 1 - y / 460);
    }
  }, { passive: true });

  // ---------- 背景：粒子 + 视差（轻量、rAF） ----------
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const canvas = document.getElementById("bg-canvas");
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, particles = [];
  const mx = { x: 0, y: 0, tx: 0, ty: 0 };

  function sizeCanvas() {
    W = canvas.width = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  function initParticles() {
    const n = Math.min(46, Math.round(W / 34));
    particles = Array.from({ length: n }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.22,
      vy: (Math.random() - 0.5) * 0.22,
      r: Math.random() * 1.6 + 0.6,
      a: Math.random() * 0.45 + 0.2,
    }));
  }
  sizeCanvas();
  initParticles();
  window.addEventListener("resize", () => { sizeCanvas(); initParticles(); });

  if (!reducedMotion) {
    window.addEventListener("pointermove", e => {
      mx.tx = (e.clientX / W - 0.5) * 2;
      mx.ty = (e.clientY / H - 0.5) * 2;
    }, { passive: true });
  }

  // 视差目标（背景层 1-3 / 市场层 4-8）
  const parTargets = [
    { el: document.querySelector(".bg-grid"), s: 2 },
    { el: document.querySelector(".glow.g1"), s: 6 },
    { el: document.querySelector(".glow.g2"), s: 9 },
    { el: document.querySelector(".glow.g3"), s: 5 },
  ];

  // 实时行情脉冲曲线（画布随 home 视图重建，需每次动态获取）
  // 实时行情脉冲曲线（画布随 home 视图重建，context 每次动态获取，避免绑到旧画布）
  function drawPulse() {
    const pulseCanvas = document.getElementById("pulse-canvas");
    if (!pulseCanvas) return;
    const w = pulseCanvas.clientWidth, h = pulseCanvas.clientHeight;
    if (!w) return;
    const dpr = window.devicePixelRatio || 1;
    if (pulseCanvas.width !== Math.round(w * dpr)) { pulseCanvas.width = w * dpr; pulseCanvas.height = h * dpr; }
    const pctx = pulseCanvas.getContext("2d");
    pctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    pctx.clearRect(0, 0, w, h);
    const mid = h / 2;
    pctx.strokeStyle = "rgba(128,128,140,.22)";
    pctx.lineWidth = 1;
    pctx.beginPath(); pctx.moveTo(0, mid); pctx.lineTo(w, mid); pctx.stroke();
    const n = pulseData.length, step = w / (n - 1);
    const lineY = i => mid - pulseData[i] * 7;
    // 填充
    pctx.beginPath();
    pctx.moveTo(0, mid);
    for (let i = 0; i < n; i++) pctx.lineTo(i * step, lineY(i));
    pctx.lineTo(w, mid); pctx.closePath();
    pctx.fillStyle = "currentColor"; pctx.globalAlpha = 0.1; pctx.fill();
    pctx.globalAlpha = 1;
    // 曲线
    pctx.beginPath();
    for (let i = 0; i < n; i++) { const x = i * step, y = lineY(i); i ? pctx.lineTo(x, y) : pctx.moveTo(x, y); }
    pctx.strokeStyle = "currentColor"; pctx.lineWidth = 1.8; pctx.stroke();
    // 末端点
    pctx.beginPath(); pctx.arc(w, lineY(n - 1), 3, 0, 6.28);
    pctx.fillStyle = "currentColor"; pctx.fill();
  }

  function frame() {
    if (!document.hidden) {
      mx.x += (mx.tx - mx.x) * 0.06;
      mx.y += (mx.ty - mx.y) * 0.06;
      parTargets.forEach(t => {
        if (t.el) t.el.style.transform =
          `translate3d(${-(mx.x * t.s).toFixed(2)}px,${-(mx.y * t.s).toFixed(2)}px,0)`;
      });
      // 前景层（标题/数据 8-16）
      document.querySelectorAll("[data-depth]").forEach(el => {
        const d = parseFloat(el.dataset.depth);
        el.style.transform = `translate3d(${-(mx.x * d).toFixed(2)}px,${-(mx.y * d).toFixed(2)}px,0)`;
      });
      ctx.clearRect(0, 0, W, H);
      particles.forEach(p => {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0) p.x = W; if (p.x > W) p.x = 0;
        if (p.y < 0) p.y = H; if (p.y > H) p.y = 0;
        ctx.beginPath();
        ctx.arc(p.x - mx.x * 3, p.y - mx.y * 3, p.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(148,180,255,${p.a})`;
        ctx.fill();
      });
      drawPulse();
    }
    requestAnimationFrame(frame);
  }
  if (!reducedMotion) requestAnimationFrame(frame);
  else {
    // 静态粒子
    particles.forEach(p => { ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.28); ctx.fillStyle = `rgba(148,180,255,${p.a})`; ctx.fill(); });
    drawPulse();
  }

  // 轻量 3D 倾斜（仅模块入口，≤3deg）
  if (!reducedMotion) {
    document.addEventListener("pointermove", e => {
      const card = e.target.closest(".tilt");
      if (!card) return;
      const r = card.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width - 0.5;
      const py = (e.clientY - r.top) / r.height - 0.5;
      card.style.transform = `perspective(820px) rotateX(${(-py * 3).toFixed(2)}deg) rotateY(${(px * 3).toFixed(2)}deg) translateY(-3px)`;
    }, { passive: true });
    document.addEventListener("pointerout", e => {
      const card = e.target.closest(".tilt");
      if (card && e.relatedTarget && !card.contains(e.relatedTarget)) card.style.transform = "";
    }, { passive: true });
  }
  // 隐藏自检：?exportpreview=1 自动生成首个正机会的购买图并预览（不下载）
  try {
    const params = new URLSearchParams(location.search);
    if (params.get("exportpreview")) {
      window.__EXPORT_PREVIEW = true;
      setTimeout(() => {
        const pos = App.positiveOpps();
        if (pos.length) {
          const mid = pos[0].key.split("|")[0], type = pos[0].def.type;
          window.AppViews.exportOpportunityImage(mid, type);
        }
      }, 3500);
    }
    // 隐藏自检：?autocart=1 自动加入首个正机会到购买清单
    if (params.get("autocart")) {
      setTimeout(() => {
        const pos = App.positiveOpps();
        if (pos.length) {
          const mid = pos[0].key.split("|")[0], type = pos[0].def.type;
          App.confirmAddCart(mid, type);
        }
      }, 2500);
    }
    // 隐藏自检：?autotrack=1 自动追踪首个正机会
    if (params.get("autotrack")) {
      setTimeout(() => {
        const pos = App.positiveOpps();
        if (pos.length) {
          const mid = pos[0].key.split("|")[0], type = pos[0].def.type;
          App.addTrack(mid, type);
        }
      }, 2500);
    }
  } catch (e) { }
})();
