"use strict";
/* ============================================================
   足球对冲系统 · 视图层（9 个视图）
   首页 / 玩法 / 赛事列表 / 单场详情 / 实时机会 / 购买清单 /
   追踪 / 历史 / 系统（含模拟市场控制台）
   ============================================================ */
(function () {
  const { S, MARKETS, PLATFORMS, PDEF } = App;
  const esc = App.esc;
  const fmtMoney = App.fmtMoney;
  const r2 = App.r2;

  function mName(m) { return m.home + " VS " + m.away; }
  const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  // 数据驱动动画缓存（用于增量高亮，避免每次重建从 0 开始）
  const metricCache = {};
  const posCache = new Set();
  const oddsCache = {};
  // 平台状态中文（内部枚举保留英文，展示用中文）
  const STATUS_TXT = { OPEN: "在售", SUSPENDED: "已暂停", CLOSED: "已关闭", UNAVAILABLE: "不可用" };
  const HEALTH_TXT = { OPEN: "正常", WARNING: "预警" };
  function mTime(m) {
    const d = m.kickoff;
    const now = new Date();
    const day = d.toDateString() === now.toDateString() ? "今天" : "明天";
    return `${day} ${App.hm(d)}`;
  }
  function marketDef(t) { return MARKETS.find(d => d.type === t); }
  function dateStr(d) {
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${mm}-${dd} ${App.hm(d)}`;
  }
  function countFor(type) {
    return S.matches.filter(m => m.markets[type]).length;
  }
  function positiveFor(type) {
    return Object.values(S.opps).filter(o => o && o.status === "POSITIVE" && o.def.type === type).length;
  }

  // 玩法卡片（可点）
  function marketCard(def, extra) {
    const n = countFor(def.type);
    const p = positiveFor(def.type);
    return `<button class="cat-card" onclick="App.openCat('${def.type}')">
      <div class="cat-name">${def.name}</div>
      <div class="cat-desc">${def.sels.map(s => s[1]).join(" / ")}</div>
      <div class="cat-stats">
        <span class="cat-n">${n} 场赛事</span>
        ${p > 0 ? `<span class="cat-p">${p} 个可研究</span>` : '<span class="cat-p dim">暂无可研究</span>'}
      </div>
      <div class="cat-go">进入 →</div>
    </button>`;
  }

  // ============================================================
  //  首页
  // ============================================================
  function home() {
    const pos = App.positiveOpps().sort((a, b) => b.roi - a.roi);

    // ---------- 大数字 ----------
    const changeCount = S.matches.reduce((s, m) =>
      s + Object.values(m.markets).reduce((a, mk) => a + mk.changes.length, 0), 0);
    const firstHome = !App.__homeDone;
    if (firstHome) App.__homeDone = true;
    const curMetrics = { m: S.matches.length, p: PLATFORMS.length, c: changeCount, o: pos.length };
    const mv = (to, cls, key) => {
      let extra = cls || "";
      if (!firstHome && metricCache[key] != null && metricCache[key] !== to) extra += " flash";
      return `<div class="mv ${extra}" ${firstHome ? `data-to="${to}"` : ""}>${firstHome ? 0 : to}</div>`;
    };
    const metrics = `<div class="metrics reveal d2">
      <div class="metric">${mv(S.matches.length, "", "m")}<div class="ml">比赛</div></div>
      <div class="metric">${mv(PLATFORMS.length, "", "p")}<div class="ml">数据源</div></div>
      <div class="metric">${mv(changeCount, "", "c")}<div class="ml">赔率变化</div></div>
      <div class="metric">${mv(pos.length, "good", "o")}<div class="ml">套利机会</div></div>
    </div>`;

    // ---------- 实时行情脉冲 ----------
    const pulse = `<div class="pulse-wrap reveal d3">
      <div class="sec-label"><h3>实时行情脉冲</h3><span>${S.dataMode === "real" ? "HG 行情驱动 · 真实数据" : "报价变动驱动市场脉搏 · 演示数据"}</span></div>
      <canvas id="pulse-canvas"></canvas>
    </div>`;

    // ---------- 实时报价流 ticker ----------
    const tickHtml = t => `<span class="tick ${t.pct >= 0 ? "up" : "down"}">
      <span class="tt">${esc(t.title)}</span>
      <span class="from">${t.old}</span>
      <span style="color:var(--muted2)">→</span>
      <span class="to">${t.now}</span>
      <span class="chg">${t.pct >= 0 ? "+" : ""}${t.pct.toFixed(1)}%</span>
      <span class="m-sub" style="font-size:10px">${PDEF[t.platform].name} · ${esc(t.label)}</span>
    </span>`;
    const tickItems = S.tickerFeed.slice(0, 12);
    const onePass = tickItems.map(tickHtml).join("");
    const tickTrack = tickItems.length
      ? `<div class="ticker-track">${onePass}${onePass}</div>`
      : `<div class="ticker-track"><span class="tick">等待报价变化…</span></div>`;
    const ticker = `<div class="ticker-wrap reveal d4">
      <div class="sec-label"><h3>市场动态</h3><span>报价变化实时流 · 悬停暂停</span></div>
      <div class="ticker">${tickTrack}</div>
    </div>`;

    // ---------- 最近机会 ----------
    const recent = pos.slice(0, 4).map(o => {
      const mid = o.key.split("|")[0];
      const m = S.matches.find(x => x.id === mid);
      const isNew = !posCache.has(o.key) && !firstHome;
      return `<div class="h-opp ${isNew ? "is-new" : ""}" onclick="App.openMatch('${mid}','${o.def.type}')">
        <span class="hcov">完整覆盖</span>
        <div class="hinfo">
          <div class="hname">${esc(mName(m))}</div>
          <div class="hsub">${esc(m.comp)} · ${mTime(m)} · ${o.def.name} · 报价版本 V${o.legs[0].version}</div>
        </div>
        <div class="hpnl"><b>+${o.roi.toFixed(2)}%</b><span>${fmtMoney(o.minPnl)}</span></div>
      </div>`;
    }).join("");
    const homeOpps = `<div class="home-opps reveal d5">
      <div class="sec-label"><h3>重点机会</h3>
        <span><button class="linkbtn" onclick="App.go('opp')">查看全部 →</button></span></div>
      ${recent || '<div class="empty"><div class="big">当前无正收益机会</div>报价变化后自动出现覆盖机会</div>'}
    </div>`;

    // ---------- 四个大型入口 ----------
    const portals = [
      { key: "play", tag: "市场行情", title: "浏览实时足球报价", desc: "按玩法浏览全部赛事，逐场对比多平台报价与水位变化。", color: "var(--cyan)", pc: "rgba(34,211,238,.14)", pcl: "rgba(34,211,238,.3)" },
      { key: "opp", tag: "机会监测", title: "发现定价偏差", desc: "扫描全部覆盖机会，按收益率排序并支持状态筛选。", color: "var(--violet)", pc: "rgba(168,85,247,.15)", pcl: "rgba(168,85,247,.3)" },
      { key: "history", tag: "数据中心", title: "溯源报价与版本", desc: "查看报价版本、机会生命周期与全部操作记录。", color: "var(--blue)", pc: "rgba(59,130,246,.14)", pcl: "rgba(59,130,246,.3)" },
      { key: "sys", tag: "系统监控", title: "监控采集与计算", desc: "数据源健康、延迟、返点与模拟市场控制台。", color: "var(--good)", pc: "rgba(52,211,153,.13)", pcl: "rgba(52,211,153,.3)" },
    ].map(p => `<div class="portal tilt" style="--pc-text:${p.color};--pc:${p.pc};--pc-line:${p.pcl}" onclick="App.go('${p.key}')">
      <span class="pt">${p.tag}</span>
      <h4>${p.title}</h4>
      <p class="pd">${p.desc}</p>
      <span class="par">进入 <span class="ar">→</span></span>
    </div>`).join("");
    const portalWrap = `<div class="reveal d6"><div class="sec-label" style="margin-top:8px"><h3>功能模块</h3></div>
      <div class="portals">${portals}</div></div>`;

    // ---------- Hero ----------
    const hero = `<div class="hero reveal d1">
      <span class="hero-kicker" data-depth="7"><span class="live-dot"></span>实时行情监测</span>
      <h1 data-depth="10">足球量化<br><span class="grad">市场研究</span></h1>
      <p data-depth="8">持续追踪多平台足球报价，实时计算覆盖关系、最优资金分配与对冲收益，输出可执行的购买方案。</p>
      <div class="hero-actions" data-depth="9">
        <button class="btn primary btn-lg" onclick="App.go('play')">查看市场行情</button>
        <button class="btn btn-lg" onclick="App.go('opp')">查看机会监测</button>
      </div>
      <div class="hero-meta">${S.dataMode === "real"
        ? `HG 实时数据 · 更新于 ${App.hms()} · ${S.matches.length} 场当前在售`
        : `演示数据 · 模拟市场 · 更新于 ${App.hms()} · 平均延迟 ${App.avgLat().toFixed(2)}s · ${S.matches.length} 场赛事`}</div>
    </div>`;

    // ---------- 早盘联赛树 ----------
    const LGS = S.real?.leagues || [];
    const lgTree = LGS.length ? (function(){
      const regs = [];
      const byRegion = {};
      LGS.forEach(lg => { (byRegion[lg.region] = byRegion[lg.region] || []).push(lg); });
      Object.keys(byRegion).forEach(r => regs.push({ name: r, leagues: byRegion[r] }));
      return regs.map(rg => `<details class="lg-region" style="margin:6px 0">
        <summary style="cursor:pointer;font-weight:600;color:var(--cyan);padding:6px;background:rgba(0,0,0,.2);border-radius:6px">${esc(rg.name)} <span style="opacity:.6">(${rg.leagues.length})</span></summary>
        <div style="padding-left:10px;display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">
          ${rg.leagues.map(lg => `<span style="border:1px solid rgba(255,255,255,.15);border-radius:4px;padding:3px 8px;font-size:12px;cursor:pointer" onclick="App.go('play')">${esc(lg.name)}<br><span style="opacity:.5;font-size:10px">id ${esc(lg.id)}</span></span>`).join("")}
        </div>
      </details>`).join("");
    })() : `<div class="empty" style="padding:20px;color:var(--muted)">早盘联赛数据未加载（等待 HG 会话）</div>`;
    const leagueSection = `<div class="reveal d7"><div class="sec-label"><h3>早盘联赛树</h3><span>${S.real?.leagueCount || 0} 个联赛 · ${S.real?.dataMode || ""} · 全部一次加载，无需逐个点击</span></div>
      <div class="lg-tree" style="max-height:360px;overflow:auto">${lgTree}</div></div>`;

    return hero + metrics + leagueSection + pulse + ticker + homeOpps + portalWrap +
      `<div class="home-foot">${S.dataMode === "real"
        ? "HG 实时数据 · 系统不提供下单，仅用于研究"
        : "演示数据 · 模拟市场引擎持续生成报价与机会，未接入真实平台"}</div>`;
  }

  // ============================================================
  //  玩法页
  // ============================================================
  function play() {
    const all = MARKETS.map(marketCard).join("");
    return `
      <div class="page-title">市场行情</div>
      <div class="page-sub">选择玩法查看对应赛事，单场玩法当前全部可演示。</div>
      <div class="cat-grid">${all}</div>
      <div class="page-title" style="margin-top:22px">组合玩法（规划中）</div>
      <div class="page-sub">多场组合在 P1 阶段演示，当前已提供 2串1 入口。</div>
      <div class="cat-grid">
        <a class="cat-card accent" href="/plans?demo=1" style="text-decoration:none">
          <div class="cat-name">2串1</div>
          <div class="cat-desc">两场比赛串关，可搭配外围对冲</div>
          <div class="cat-go">进入演示 →</div>
        </a>
        <button class="cat-card" disabled>
          <div class="cat-name">3串1</div>
          <div class="cat-desc">规划中 · 架构已预留</div>
        </button>
        <button class="cat-card" disabled>
          <div class="cat-name">4串1 / 复式</div>
          <div class="cat-desc">规划中 · 架构已预留</div>
        </button>
        <button class="cat-card" disabled>
          <div class="cat-name">复合 / 多场组合</div>
          <div class="cat-desc">规划中 · 统一框架承载</div>
        </button>
      </div>
      <div class="home-foot">演示数据 · 组合玩法复用同一套计算与展示框架</div>`;
  }

  // ============================================================
  //  赛事列表（玩法下）
  // ============================================================
  function cat() {
    const type = S.curCat;
    const def = marketDef(type);
    const q = S.catQ.trim().toLowerCase();
    let list = S.matches.filter(m => m.markets[type]);
    if (q) list = list.filter(m => (m.home + m.away + m.comp).toLowerCase().includes(q));
    list = list.map(m => ({ m, o: S.opps[m.id + "|" + type] }))
      .sort((a, b) => {
        const sa = a.o.status === "POSITIVE" ? 0 : 1, sb = b.o.status === "POSITIVE" ? 0 : 1;
        if (sa !== sb) return sa - sb;
        return a.m.kickoff - b.m.kickoff;
      });
    const cards = list.map(({ m, o }) => {
      const cls = o.status === "POSITIVE" ? "pos" : (o.status === "BREAK" ? "zero" : "neg");
      const st = o.status === "POSITIVE" ? "完整覆盖" : (o.status === "BREAK" ? "保本" : "暂无覆盖");
      const quotes = (o.legs || []).map(l =>
        `<span class="q"><b>${PDEF[l.p].name.replace("模拟平台 ", "平台")}</b> ${l.label} <span class="mono">@${l.odds}</span></span>`
      ).join("") || `<span class="q">等待报价</span>`;
      const sub = o.status === "POSITIVE"
        ? `总投入 ¥${o.total.toFixed(2)} · 最小收益 ${fmtMoney(o.minPnl)} · ROI ${o.roi.toFixed(2)}%`
        : (o.status === "DATA" ? "等待更多平台报价"
          : `组合返奖率 ${(100 / o.sum).toFixed(1)}% · 还差 ${((o.sum - 1) * 100).toFixed(1)}% 破线`);
      return `<div class="m-card ${cls}" style="cursor:pointer" onclick="App.openMatch('${m.id}','${type}')">
        <div class="m-head">
          <div class="m-name">${esc(mName(m))}</div>
          <div class="m-time">${mTime(m)} · ${esc(m.comp)} · ${m.id}</div>
        </div>
        <div class="m-quotes">${quotes}</div>
        <div class="m-foot">
          <span class="tag ${o.status === "POSITIVE" ? "good" : "warn"}">${st}</span>
          <span class="m-sub">${sub}</span>
          <span class="m-go">查看方案 →</span>
        </div>
      </div>`;
    }).join("");
    return `
      <div class="page-title">${def.name} · 今日赛事</div>
      <div class="page-sub">
        <button class="linkbtn" onclick="App.go('play')">← 返回玩法</button> · ${def.sels.map(s => s[1]).join(" / ")}
        ${def.note ? " · " + def.note : ""}
      </div>
      <div class="filter" style="margin-bottom:12px">
        <input placeholder="搜索球队 / 联赛" style="min-width:220px"
          value="${esc(S.catQ)}" oninput="App.setCatQ(this.value)" />
        <span class="m-sub">共 ${list.length} 场 · ${list.filter(x => x.o.status === "POSITIVE").length} 个正机会</span>
      </div>
      ${cards || '<div class="empty">没有匹配的赛事</div>'}
      <div class="home-foot">点击赛事卡片进入对冲详情：系统给出 在哪个平台买 / 买什么 / 买多少 / 赚多少</div>`;
  }

  // ============================================================
  //  单场详情（核心页）
  // ============================================================
  function match() {
    const id = S.curMatch, type = S.curMarket;
    const m = S.matches.find(x => x.id === id);
    const o = S.opps[id + "|" + type];
    const def = marketDef(type);
    if (!m || !o) {
      // P0 修复：比赛不存在时给出友好提示，而不是空白"数据不存在"
      const reason = !m ? "该场比赛已结束或数据已更新" : "该玩法暂无数据";
      return `<div class="empty" style="padding:60px 20px;text-align:center">
        <div style="font-size:48px;margin-bottom:16px">⚽</div>
        <div style="font-size:18px;margin-bottom:8px">${reason}</div>
        <div style="color:var(--muted);margin-bottom:24px">比赛ID：${esc(id)} · 玩法：${esc(type)}</div>
        <button class="btn primary" onclick="App.go('opp')">返回机会列表</button>
        <button class="btn" onclick="App.go('play')">返回赛事列表</button>
      </div>`;
    }

    // 玩法 tabs - show only markets this match actually has (dynamic)
    const availTypes = Object.keys(m.markets).filter(function(t) { return MARKETS.some(function(d) { return d.type === t; }); });
    const tabs = availTypes.map(function(t) {
      var d = MARKETS.find(function(dd) { return dd.type === t; }) || { type: t, name: t };
      return '<button class="mkt-tab ' + (t === type ? "on" : "") + '" onclick="App.switchMarket(\'' + t + '\')">' + d.name + '</button>';
    }).join("");

    // 01 赔率表（平台行 × 结果）
    const mk = m.markets[type];
    const headCells = def.sels.map(s => `<th style="text-align:center">${s[1]}</th>`).join("");
    const platRows = mk ? PLATFORMS.map(p => {
      const pf = mk.platforms[p];
      // P0 修复：某平台无该市场数据时不崩溃，显示空状态
      if (!pf || !pf.odds) {
        const emptyCells = def.sels.map(() => `<td style="text-align:center;color:var(--muted);padding:8px">-</td>`).join("");
        return `<tr>
          <td><b>${PDEF[p].name}</b><div class="m-sub" style="color:var(--muted)">暂无该玩法数据</div></td>
          ${emptyCells}
        </tr>`;
      }
      const cells = def.sels.map((s, i) => {
        const isBest = o.legs && o.legs[i] && o.legs[i].p === p;
        const oddsVal = pf.odds[i] != null ? pf.odds[i] : "-";
        return `<td style="text-align:center;${isBest ? "background:var(--good-bg)" : ""}">
          <span class="mono odds-cell" data-key="${id}|${type}|${p}|${i}" style="${isBest ? "color:var(--good);font-weight:700" : ""}">${oddsVal}</span>
          ${isBest ? '<div style="font-size:9.5px;color:var(--good)">最优</div>' : ""}
        </td>`;
      }).join("");
      return `<tr>
        <td><b>${PDEF[p].name}</b><div class="m-sub">${STATUS_TXT[pf.status] || pf.status || "未知"} · 报价版本 V${pf.version || 0}</div></td>
        ${cells}
      </tr>`;
    }).join("")
      : `<tr><td colspan="${def.sels.length + 1}" style="text-align:center;color:var(--muted);padding:24px">
           该玩法暂无真实数据（当前仅采集胜平负），不做伪造</td></tr>`;

    // 02 状态
    const sb = o.status === "POSITIVE"
      ? `<div class="status-banner sb-good"><span class="dot g"></span><span>完整覆盖 · 买齐后任意赛果都盈利（含返点）</span></div>`
      : o.status === "BREAK"
        ? `<div class="status-banner sb-warn"><span class="dot o"></span><span>接近保本 · 组合返奖率接近 100%，等待变盘</span></div>`
        : o.status === "DATA"
          ? `<div class="status-banner sb-gray"><span class="dot o"></span><span>等待数据 · ${esc(o.reason || "")}</span></div>`
          : `<div class="status-banner sb-bad"><span class="dot r"></span><span>暂无覆盖 · 当前报价硬买会亏</span></div>`;

    // 03 计算 stats
    const stats = o.status === "POSITIVE" ? `
      <div class="stat" style="padding:10px"><div class="l">总投入</div><div class="v" style="font-size:17px">¥${o.total.toFixed(2)}</div></div>
      <div class="stat" style="padding:10px"><div class="l">最差回收</div><div class="v" style="font-size:17px">¥${(o.total + o.minPnl).toFixed(2)}</div></div>
      <div class="stat" style="padding:10px"><div class="l">最小收益</div><div class="v v-pos" style="font-size:17px">${fmtMoney(o.minPnl)}</div></div>
      <div class="stat" style="padding:10px"><div class="l">最小收益率</div><div class="v v-pos" style="font-size:17px">${o.roi.toFixed(2)}%</div></div>
      <div class="stat" style="padding:10px"><div class="l">最大收益</div><div class="v" style="font-size:17px">${fmtMoney(o.maxPnl)}</div></div>`
      : o.status === "DATA" ? `
      <div class="stat" style="padding:10px"><div class="l">报价状态</div><div class="v" style="font-size:15px;color:var(--muted)">暂无真实数据</div></div>`
      : `
      <div class="stat" style="padding:10px"><div class="l">组合返奖率</div><div class="v" style="font-size:17px">${(100 / o.sum).toFixed(1)}%</div></div>
      <div class="stat" style="padding:10px"><div class="l">价差缺口</div><div class="v" style="font-size:17px;color:var(--bad)">${((o.sum - 1) * 100).toFixed(1)}%</div></div>
      <div class="stat" style="padding:10px"><div class="l">破线条件</div><div class="v" style="font-size:14px">组合返奖率&gt;100%</div></div>`;

    // 04 推荐方案
    const plan = o.status === "POSITIVE" ? `
      <div class="plan-box">
        ${o.legs.map((l, i) => `<div class="plan-row ${l.p === "C" ? "ext" : "tc"}">
          <span class="pr-idx">${i + 1}</span>
          <span class="pr-src">${PDEF[l.p].name}</span>
          <span class="pr-pick">${l.label}</span>
          <span class="pr-odds">@${l.odds}</span>
          <span class="pr-amt">¥${o.stake[i].toFixed(2)}</span>
        </div>`).join("")}
        <div class="plan-total">
          总投入 <b>¥${o.total.toFixed(2)}</b> · 理论回收 <b>¥${(o.total + o.minPnl).toFixed(2)}</b>
          · 最小收益 <b style="color:var(--good)">${fmtMoney(o.minPnl)}</b> · 返点已计入
        </div>
      </div>
      <div class="match-ops">
        <button class="btn primary" onclick="App.addToCart('${id}','${type}')">加入购买清单</button>
        <button class="btn" onclick="AppViews.exportOpportunityImage('${id}','${type}')">导出购买图</button>
        <button class="btn" onclick="App.addTrack('${id}','${type}')">开始追踪</button>
        <button class="btn" onclick="App.recalcMatch('${id}','${type}')">重新计算</button>
      </div>`
      : `
      <div class="plan-box" style="color:var(--muted)">
        当前报价下无可行方案：${o.status === "DATA" ? esc(o.reason) :
        `组合返奖率 ${(100 / o.sum).toFixed(1)}%，还差 ${((o.sum - 1) * 100).toFixed(1)}% 破线。报价变化后系统会自动重算，出现可盈利方案立即提示。`}
      </div>
      <div class="match-ops">
        <button class="btn" onclick="App.addTrack('${id}','${type}')">开始追踪（等变盘）</button>
        <button class="btn" onclick="App.recalcMatch('${id}','${type}')">重新计算</button>
      </div>`;

    // 05 折叠详情
    const chg = (mk && mk.changes) || [];
    const verLine = mk
      ? `赔率版本：${PLATFORMS.map(p => { const pf = mk.platforms[p]; return pf ? `V${pf.version || 0}` : "无"; }).join(" / ")}`
      : `赔率版本：暂无真实数据`;
    const timeLine = mk
      ? `计算时间：${App.hms(o.calcAt)} · 报价最后更新 ${App.hms(new Date(Math.max(...PLATFORMS.map(p => { const pf = mk.platforms[p]; return pf ? (pf.updatedAt || 0) : 0; }).filter(t => t > 0))))}`
      : `当前无该玩法真实报价`;
    const detail = `
      <details class="d-sec">
        <summary><span class="d-num" style="background:var(--brand)">i</span>报价变化与水位迭代<span class="d-sub">该场全部报价变动记录（时间/平台/结果/前值→后值）</span></summary>
        <div class="d-body">
          ${chg.length ? `<table class="tbl-mini"><thead><tr><th>时间</th><th>平台</th><th>结果</th><th>前值</th><th>后值</th><th>备注</th></tr></thead><tbody>
            ${chg.map(c => `<tr><td class="mono">${c.ts}</td><td>${PDEF[c.platform].name}</td><td>${def.sels[c.sel][1]}</td>
              <td class="mono">${c.old}</td><td class="mono">${c.now}</td><td>${c.note}</td></tr>`).join("")}
          </tbody></table>` : '<div class="m-sub">暂无报价变化</div>'}
        </div>
      </details>
      <details class="d-sec">
        <summary><span class="d-num" style="background:var(--brand)">v</span>版本 / 平台 / 规则详情</summary>
        <div class="d-body" style="font-size:12px;line-height:1.9">
          ${verLine}<br>
          结算规则：${def.name} 规则 v1（WIN / LOSE ${def.sels.length === 2 ? "/ PUSH" : ""} / VOID）<br>
          资金优化：词典序（先最大最小含返点净收益，再最小化实际投入）<br>
          ${timeLine}<br>
          平台限制：${PLATFORMS.map(p => `${PDEF[p].name} 最小${PDEF[p].min}/步长${PDEF[p].step}/最大${PDEF[p].max}/返点${(PDEF[p].rebate * 100).toFixed(1)}%`).join("；")}
        </div>
      </details>`;

    return `
      <div class="page-title">${esc(mName(m))}</div>
      <div class="page-sub">
        ${m.id} · ${esc(m.comp)} · 开赛 ${mTime(m)}
        · <button class="linkbtn" onclick="App.openCat('${type}')">← 返回赛事列表</button>
      </div>
      <div class="mkt-tabs">${tabs}</div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n1">01</span><span class="sec-title">当前赔率</span><span class="sec-sub">平台 × 结果 · 高亮为该结果最优报价</span></div>
        <div class="sec-body">
          <div class="fresh-row">
            <span class="fresh"><span class="dot g"></span>实时报价 · 采集新鲜度 刚刚</span>
            <span class="m-sub">报价 ${(() => { const a = App.lastWaterAgo(); return a == null ? "等待首次变化" : a === 0 ? "刚刚变化" : a + "s 前变化"; })()} · 时间过去不影响报价有效，仅在售/暂停/关闭状态改变才失效</span>
          </div>
          <table class="tbl-mini"><thead><tr><th>平台</th>${headCells}</tr></thead><tbody>${platRows}</tbody></table>
          <div class="m-sub">${S.dataMode === "real"
            ? "赔率来自 HG 行情，每次变盘版本 +1"
            : "赔率随模拟市场实时变化 · 每次变化版本 +1"}</div>
        </div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n2">02</span><span class="sec-title">机会状态</span></div>
        <div class="sec-body">${sb}</div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n3">03</span><span class="sec-title">系统计算</span><span class="sec-sub">覆盖 / 金额 / 收益（系统自动计算）</span></div>
        <div class="sec-body">
          <div class="stat-row" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr))">${stats}</div>
          ${o.status !== "DATA" ? `<div class="sec-tip">Σ(1/有效赔率)=${o.sum.toFixed(4)} · ${o.sum < 1 ? "Σ&lt;1 存在套利空间" : "Σ≥1 无套利（庄家抽水已包含）"}</div>` : ""}
        </div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n4">04</span><span class="sec-title">推荐购买方案</span><span class="sec-sub">系统给出"怎么买/买多少"</span></div>
        <div class="sec-body">${plan}</div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n5">05</span><span class="sec-title">详细信息</span><span class="sec-sub">默认折叠</span></div>
        <div class="sec-body">${detail}</div>
      </div>
      <div class="home-foot">${S.dataMode === "real"
        ? "HG 实时数据 · 系统不提供下单，仅用于研究"
        : "演示数据 · 购买前须在真实平台核对最新报价"}</div>`;
  }

  // ============================================================
  //  实时机会
  // ============================================================
  function opp() {
    const f = S.oppFilter;
    let list = Object.values(S.opps).filter(o => o && o.status !== "DATA");
    if (f.cat !== "ALL") list = list.filter(o => o.def.type === f.cat);
    if (f.status !== "ALL") {
      if (f.status === "POSITIVE") list = list.filter(o => o.status === "POSITIVE");
      else if (f.status === "BREAK") list = list.filter(o => o.status === "BREAK");
      else list = list.filter(o => o.status === "NO");
    }
    if (f.sort === "roi") list.sort((a, b) => b.roi - a.roi);
    else if (f.sort === "time") {
      list.sort((a, b) => {
        const ma = S.matches.find(x => x.id === a.key.split("|")[0]);
        const mb = S.matches.find(x => x.id === b.key.split("|")[0]);
        return ma.kickoff - mb.kickoff;
      });
    } else list.sort((a, b) => b.minPnl - a.minPnl);

    const cards = list.map(o => {
      const id = o.key.split("|")[0];
      const m = S.matches.find(x => x.id === id);
      const cls = o.status === "POSITIVE" ? "oc-pos" : o.status === "BREAK" ? "oc-zero" : "oc-neg";
      const stTag = o.status === "POSITIVE" ? '<span class="tag good">完整覆盖</span>'
        : o.status === "BREAK" ? '<span class="tag partial">保本</span>' : '<span class="tag neg">会亏</span>';
      const val = o.status === "POSITIVE"
        ? `<div class="opp-pnl pos">${o.roi.toFixed(2)}%</div><div class="opp-sub">${fmtMoney(o.minPnl)} · V${o.legs[0].version}</div>`
        : `<div class="opp-pnl neg">${((o.sum - 1) * 100).toFixed(1)}%</div><div class="opp-sub">缺口（距破线）</div>`;
      return `<div class="opp-card ${cls}">
        <span class="opp-ind"></span>
        <div class="opp-left">
          <div class="opp-tags">${stTag}<span class="tag anchor">${o.def.name}</span></div>
          <div class="opp-name">${esc(mName(m))}</div>
          <div class="opp-meta">${mTime(m)} · ${esc(m.comp)} · ${App.hms(o.calcAt)} 计算</div>
        </div>
        <div class="opp-right">
          ${val}
          <div class="opp-ops">
            <button class="op-btn op-primary" onclick="App.openMatch('${id}','${o.def.type}')">查看方案</button>
            ${o.status === "POSITIVE"
              ? `<button class="op-btn" onclick="App.addToCart('${id}','${o.def.type}')">加入清单</button>`
              : `<button class="op-btn" onclick="App.addTrack('${id}','${o.def.type}')">追踪变盘</button>`}
          </div>
        </div>
      </div>`;
    }).join("");

    const catOpts = ['<option value="ALL">全部玩法</option>'].concat(MARKETS.map(d =>
      `<option value="${d.type}" ${f.cat === d.type ? "selected" : ""}>${d.name}</option>`)).join("");
    return `
      <div class="status-banner sb-gray">
        <span class="dot ${S.settings.running ? "g" : "o"}"></span>
        <span><b>${S.settings.running ? "模拟市场运行中" : "已暂停"}</b> · 本会话：新发现 <b>${S.session.detected}</b> · 重算 <b>${S.session.recalced}</b> · 失效 <b>${S.session.invalidated}</b></span>
      </div>
      <div class="page-title">机会监测</div>
      <div class="page-sub">系统持续扫描全部赛事与玩法，报价变化自动重算，套利机会实时推送。</div>
      <div class="filter" style="margin-bottom:12px">
        <label>玩法</label><select onchange="App.setOppFilter('cat',this.value)">${catOpts}</select>
        <label>状态</label>
        <select onchange="App.setOppFilter('status',this.value)">
          ${["ALL:全部状态", "POSITIVE:完整覆盖", "BREAK:保本", "NO:会亏"].map(s => {
            const [v, l] = s.split(":");
            return `<option value="${v}" ${f.status === v ? "selected" : ""}>${l}</option>`;
          }).join("")}
        </select>
        <label>排序</label>
        <select onchange="App.setOppFilter('sort',this.value)">
          ${["roi:收益率", "pnl:收益额", "time:开赛时间"].map(s => {
            const [v, l] = s.split(":");
            return `<option value="${v}" ${f.sort === v ? "selected" : ""}>${l}</option>`;
          }).join("")}
        </select>
      </div>
      <div class="opp-list">${cards || '<div class="empty">没有匹配的机会</div>'}</div>
      <div class="home-foot">演示数据 · 机会由模拟市场引擎实时产生</div>`;
  }

  // ============================================================
  //  购买清单
  // ============================================================
  function cart() {
    const items = S.cart;
    const totalStake = items.reduce((s, c) => s + (c.status === "BOUGHT" ? 0 : c.snapshot.total), 0);
    const totalPnl = items.reduce((s, c) => s + (c.status === "BOUGHT" ? 0 : c.snapshot.minPnl), 0);
    const head = `<div class="cart-total">
      <span>共 <b>${items.length}</b> 笔（${items.filter(c => c.status === "BOUGHT").length} 已模拟购买）</span>
      <span>待购总投入 <b>¥${totalStake.toFixed(2)}</b></span>
      <span>最小收益合计 <b style="color:${totalPnl > 0 ? "var(--good)" : "var(--bad)"}">${fmtMoney(totalPnl)}</b></span>
      <span style="font-size:11px;color:var(--muted2)">模拟清单 · 不执行真实交易</span>
    </div>`;
    if (!items.length) {
      return head + `<div class="empty"><div class="big">购买清单为空</div>
        在赛事详情页点"加入购买清单"，方案会带着快照（报价/金额/版本）出现在这里。</div>`;
    }
    const rows = items.map((c, idx) => {
      const m = S.matches.find(x => x.id === c.matchId);
      const def = marketDef(c.marketType);
      const cur = S.opps[c.matchId + "|" + c.marketType];
      const map = {
        VALID: ["sb-good", "方案有效", "当前报价与加入时一致"],
        CHANGED: ["sb-warn", "赔率发生变化", c.note],
        INVALID: ["sb-bad", "方案失效", c.note],
        WAIT: ["sb-gray", "等待重新计算", c.note],
        BOUGHT: ["sb-gray", "已模拟购买", "仅模拟，未发生真实交易"],
      }[c.status] || ["sb-gray", c.status, ""];
      const curLine = cur && cur.status !== "DATA"
        ? `当前最新：收益率 ${cur.roi.toFixed(2)}% · 最小收益 ${fmtMoney(cur.minPnl)} · ${App.hms(cur.calcAt)}`
        : "等待重算";
      return `<div class="sec">
        <div class="sec-head">
          <span class="sec-num n1">${idx + 1}</span>
          <span class="sec-title">${esc((m && mName(m)) || c.matchId)}</span>
          <span class="sec-sub">${def.name} · 加入于 ${App.hms(new Date(c.createdAt))} · ${c.cid}</span>
        </div>
        <div class="sec-body">
          <div class="status-banner ${map[0]}"><span><b>${map[1]}</b> · ${map[2]}</span></div>
          <div class="plan-box" style="margin-bottom:8px">
            ${c.snapshot.legs.map((l, i) => `<div class="plan-row ${l.p === "C" ? "ext" : "tc"}">
              <span class="pr-idx">${i + 1}</span>
              <span class="pr-src">${PDEF[l.p].name}</span>
              <span class="pr-pick">${l.label}</span>
              <span class="pr-odds">@${l.odds}</span>
              <span class="pr-amt">¥${Number(c.snapshot.stake[i]).toFixed(2)}</span>
            </div>`).join("")}
            <div class="plan-total">快照：总投入 <b>¥${c.snapshot.total.toFixed(2)}</b> · 最小收益 <b>${fmtMoney(c.snapshot.minPnl)}</b> · 版本 V${c.snapshot.legs[0].version}
              <br><span class="m-sub">${curLine}</span></div>
          </div>
          <div class="match-ops">
            <button class="btn primary" onclick="AppViews.exportPurchaseImage('${c.cid}')">导出购买图</button>
            ${c.status === "CHANGED" ? `<button class="btn primary" onclick="App.refreshCartItem('${c.cid}')">按最新报价重算</button>` : ""}
            ${c.status === "BOUGHT" ? "" : `<button class="btn" onclick="App.simulateBuy('${c.cid}')">确认模拟购买</button>`}
            ${c.status === "BOUGHT" ? "" : `<button class="btn" onclick="App.addTrack('${c.matchId}','${c.marketType}')">开始追踪</button>`}
            <button class="btn" onclick="App.openMatch('${c.matchId}','${c.marketType}')">查看比赛</button>
            <button class="btn" onclick="App.removeCart('${c.cid}')">移除</button>
          </div>
        </div>
      </div>`;
    }).join("");
    return head + rows + `<div class="home-foot">清单条目保存报价快照，报价变化后自动标记并提示</div>`;
  }

  // ============================================================
  //  追踪
  // ============================================================
  function track() {
    if (!S.tracks.length) {
      return `<div class="page-title">追踪</div>
        <div class="empty"><div class="big">暂无追踪赛事</div>
        在赛事详情页点"开始追踪"，系统记录初始报价并持续对比水位变化。</div>`;
    }
    const cards = S.tracks.map((t, idx) => {
      const m = S.matches.find(x => x.id === t.matchId);
      const def = marketDef(t.marketType);
      const cur = S.opps[t.matchId + "|" + t.marketType];
      const mk = m.markets[t.marketType];
      const init = t.initial;
      const rows = init.legs.map((l, i) => {
        const cLeg = cur.legs ? cur.legs[i] : null;
        const curOdds = cLeg ? cLeg.odds : "—";
        const d = cLeg ? r2(Number(curOdds) - Number(l.odds)) : 0;
        const dir = d > 0 ? "color:var(--good)" : d < 0 ? "color:var(--bad)" : "color:var(--muted)";
        return `<tr>
          <td>${PDEF[l.p].name}</td>
          <td>${l.label}</td>
          <td class="mono" style="text-align:center">${l.odds}</td>
          <td class="mono" style="text-align:center;${dir};font-weight:700">${curOdds}</td>
          <td class="mono" style="text-align:center;${dir}">${d > 0 ? "+" : ""}${d}</td>
        </tr>`;
      }).join("");
      const initRoi = init.roi, curRoi = cur && cur.status !== "DATA" ? cur.roi : null;
      let state;
      if (cur && cur.status === "POSITIVE") {
        state = curRoi > initRoi ? ["sb-good", "机会改善"] : ["sb-warn", "机会维持/收窄"];
      } else if (cur && cur.status === "BREAK") state = ["sb-warn", "接近保本 · 等待变盘"];
      else state = ["sb-bad", "机会失效"];
      const chg = mk.changes.slice(0, 10);
      return `<div class="sec">
        <div class="sec-head">
          <span class="sec-num n2">${idx + 1}</span>
          <span class="sec-title">${esc(mName(m))}</span>
          <span class="sec-sub">${def.name} · 追踪于 ${App.hms(new Date(t.addedAt))} · ${t.tid}</span>
        </div>
        <div class="sec-body">
          <div class="status-banner ${state[0]}"><span class="dot ${state[0] === "sb-good" ? "g" : state[0] === "sb-bad" ? "r" : "o"}"></span><b>${state[1]}</b>
            · 原始收益率 ${initRoi.toFixed(2)}% → 当前 ${curRoi == null ? "—" : curRoi.toFixed(2) + "%"}</div>
          <table class="tbl-mini">
            <thead><tr><th>平台</th><th>结果</th><th style="text-align:center">初始赔率</th><th style="text-align:center">当前赔率</th><th style="text-align:center">水位变化</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          <details class="d-sec" style="margin-top:10px">
            <summary><span class="d-num" style="background:var(--brand)">w</span>赔率变化记录（水位迭代）</summary>
            <div class="d-body">
              ${chg.length ? chg.map(c => `<div class="evt ${Number(c.now) > Number(c.old) ? "good" : "bad"}">
                <span class="et">${c.ts}</span> ${PDEF[c.platform].name} · ${def.sels[c.sel][1]}：${c.old} → <b>${c.now}</b> ${c.note ? "· " + c.note : ""}</div>`).join("")
                : '<div class="m-sub">暂无变化</div>'}
            </div>
          </details>
          <div class="match-ops" style="margin-top:10px">
            <button class="btn primary" onclick="App.openMatch('${t.matchId}','${t.marketType}')">查看比赛</button>
            <button class="btn" onclick="App.removeTrack('${t.tid}')">停止追踪</button>
          </div>
        </div>
      </div>`;
    }).join("");
    return `<div class="page-title">追踪 · 水位迭代</div>
      <div class="page-sub">初始报价 vs 当前报价，逐腿记录水位变化，重算结果实时对比。</div>
      ${cards}`;
  }

  // ============================================================
  //  历史
  // ============================================================
  function history() {
    const f = S.histFilter;
    const kindMap = {
      DETECTED: ["good", "发现机会"], INVALIDATED: ["bad", "机会失效"],
      UPDATED: ["warn", "机会更新"], CART_ADD: ["", "加入清单"],
      CART_RECALC: ["", "清单重算"], TRACK_ADD: ["", "开始追踪"],
      BOUGHT: ["good", "模拟购买"], RECALC: ["", "手动重算"], REINIT: ["", "重新初始化"],
    };
    let list = S.events;
    if (f !== "ALL") list = list.filter(e => e.kind === f);
    const tl = list.map(e => {
      const km = kindMap[e.kind] || ["", e.kind];
      return `<div class="evt ${km[0]}">
        <span class="et">${e.tsStr}</span>
        <span class="tag ${km[0]}">${km[1]}</span>
        ${esc(e.text)}
        ${e.data && e.data.matchId ? `<button class="linkbtn" onclick="App.openMatch('${e.data.matchId}','${e.data.type}')">查看 →</button>` : ""}
      </div>`;
    }).join("");
    const opts = ["ALL:全部", "DETECTED:发现机会", "UPDATED:机会更新", "INVALIDATED:机会失效",
      "CART_ADD:加入清单", "CART_RECALC:清单重算", "TRACK_ADD:开始追踪", "BOUGHT:模拟购买", "REINIT:初始化"]
      .map(s => {
        const [v, l] = s.split(":");
        return `<option value="${v}" ${f === v ? "selected" : ""}>${l}</option>`;
      }).join("");
    return `<div class="page-title">数据中心</div>
      <div class="page-sub">原始数据与机会生命周期：记录每一次报价变化、机会产生/更新/失效、清单与追踪操作。</div>
      <div class="filter" style="margin-bottom:12px">
        <label>类型</label><select onchange="App.setHistFilter(this.value)">${opts}</select>
        <span class="m-sub">共 ${list.length} 条（持久化，刷新不丢）</span>
      </div>
      <div class="sec"><div class="sec-body"><div class="evt-timeline">${tl || '<div class="m-sub">暂无历史</div>'}</div></div></div>`;
  }

  // ============================================================
  //  研究分析
  // ============================================================
  function analyzeMarket(m, type) {
    const def = marketDef(type);
    const mk = m.markets[type];
    if (!mk || !mk.platforms) {
      const def = marketDef(type);
      return { def, mk: null, plats: [], best: def.sels.map(() => null), combRet: 0 };
    }
    const plats = PLATFORMS.flatMap(p => {
      const pf = PDEF[p];
      const platData = mk.platforms[p];
      if (!platData || !Array.isArray(platData.odds)) return [];
      const odds = platData.odds.map(Number);
      const imp = odds.map(o => 1 / o);
      const ret = imp.reduce((a, b) => a + b, 0);
      const devig = imp.map(x => x / ret);
      return { p, pf, odds, imp, ret, devig };
    });
    const best = def.sels.map((s, i) => {
      let b = null;
      plats.forEach(d => {
        const eff = d.odds[i] * (1 + d.pf.rebate);
        if (!b || eff > b.eff) b = { p: d.p, odds: d.odds[i], eff };
      });
      return b;
    });
    const combRet = best.reduce((s, b) => s + 1 / b.eff, 0);
    return { def, mk, plats, best, combRet };
  }

  function research() {
    if (!S.rMatch || !S.matches.find(x => x.id === S.rMatch)) {
      const fp = App.positiveOpps()[0];
      S.rMatch = fp ? fp.key.split("|")[0] : S.matches[0].id;
      S.rType = fp ? fp.def.type : "1X2";
    }
    const m = S.matches.find(x => x.id === S.rMatch);
    const type = S.rType;
    const A = analyzeMarket(m, type);
    const def = A.def;

    const matchOpts = S.matches.map(x =>
      `<option value="${x.id}" ${x.id === m.id ? "selected" : ""}>${x.home} VS ${x.away} · ${App.hm(x.kickoff)}</option>`).join("");
    const typeOpts = MARKETS.map(x =>
      `<option value="${x.type}" ${x.type === type ? "selected" : ""}>${x.name}</option>`).join("");

    const platRows = A.plats.map(d => {
      const retPct = (d.ret * 100).toFixed(2);
      const vig = ((1 - d.ret) * 100).toFixed(2);
      return `<tr>
        <td><b>${d.pf.name}</b><div class="m-sub">返点 ${(d.pf.rebate * 100).toFixed(1)}%</div></td>
        <td class="mono" style="text-align:center;color:${d.ret > 1 ? "var(--good)" : d.ret > 0.97 ? "var(--warn)" : "var(--muted)"}">${retPct}%</td>
        <td class="mono" style="text-align:center">${vig}%</td>
      </tr>`;
    }).join("");

    const combPct = (100 / A.combRet).toFixed(2);
    const combState = A.combRet < 1 ? ["sb-good", "组合返奖率 " + combPct + "% · 存在套利空间"]
      : A.combRet < 1.02 ? ["sb-warn", "组合返奖率 " + combPct + "% · 接近保本"]
        : ["sb-bad", "组合返奖率 " + combPct + "% · 无套利（硬买会亏）"];

    const devigBest = A.best.map(b => 1 / b.eff);
    const devigSum = devigBest.reduce((a, b) => a + b, 0);
    const probBars = def.sels.map((s, i) => {
      const pct = devigBest[i] / devigSum * 100;
      return `<div class="prob-bar">
        <span class="pb-l">${s[1]}</span>
        <div class="prob-track"><div class="prob-fill" style="width:${pct.toFixed(1)}%"></div></div>
        <span class="prob-val">${pct.toFixed(1)}%</span>
      </div>`;
    }).join("");

    const spreadRows = def.sels.map((s, i) => {
      const vals = A.plats.map(d => d.odds[i]);
      const hi = Math.max(...vals), lo = Math.min(...vals);
      const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
      const spread = ((hi - lo) / avg * 100).toFixed(1);
      return `<tr>
        <td>${s[1]}</td>
        ${A.plats.map(d => {
          const o = d.odds[i], isHi = o === hi, isLo = o === lo;
          return `<td class="mono" style="text-align:center;${isHi ? "color:var(--good);font-weight:700" : isLo ? "color:var(--bad)" : ""}">${o.toFixed(2)}</td>`;
        }).join("")}
        <td class="mono" style="text-align:right">${spread}%</td>
      </tr>`;
    }).join("");

    const trackSection = S.tracks.length
      ? S.tracks.map(t => {
        const tm = S.matches.find(x => x.id === t.matchId);
        return `<div class="h-opp" onclick="App.openMatch('${t.matchId}','${t.marketType}')">
          <span class="hcov">追踪中</span>
          <div class="hinfo"><div class="hname">${mName(tm)}</div>
          <div class="hsub">${marketDef(t.marketType).name} · 追踪于 ${App.hms(new Date(t.addedAt))}</div></div>
        </div>`;
      }).join("")
      : '<div class="empty"><div class="big">暂无追踪赛事</div>在比赛详情点"开始追踪"，即可在此查看水位演变</div>';

    const changeCount = S.matches.reduce((s, x) => s + Object.values(x.markets).reduce((a, mk) => a + mk.changes.length, 0), 0);
    const activity = Math.min(100, Math.round(changeCount / 8));

    return `
      <div class="page-title">研究分析</div>
      <div class="page-sub">隐含概率、去水概率、市场偏差与多源价差，用于研究定价效率（演示数据）。</div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n1">01</span><span class="sec-title">市场总览</span></div>
        <div class="sec-body">
          <div class="prob-bar">
            <span class="pb-l">市场活跃度</span>
            <div class="prob-track"><div class="prob-fill" style="width:${activity}%"></div></div>
            <span class="prob-val">${activity}%</span>
          </div>
          <div class="m-sub" style="margin-top:6px">数据来自累计报价变化（${changeCount} 次）· 共 ${S.matches.length} 场 · ${App.positiveOpps().length} 个套利机会</div>
        </div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n2">02</span><span class="sec-title">比赛分析</span>
          <span class="sec-sub">选择比赛与玩法</span></div>
        <div class="sec-body">
          <div class="rsel">
            <select onchange="App.setResearchMatch(this.value)">${matchOpts}</select>
            <select onchange="App.setResearchMarket(this.value)">${typeOpts}</select>
            ${mk && mk.line ? `<span style="margin-left:12px;font-weight:700;color:var(--gold);background:var(--badge-bg);padding:2px 10px;border-radius:4px;font-size:0.95em">盘口 <span style="color:var(--fg)">${esc(mk.line)}</span></span>` : ""}
          </div>
          <div class="status-banner ${combState[0]}">${combState[1]}</div>
          <div class="sec-label" style="margin-top:18px"><h3>各平台返奖率</h3></div>
          <table class="tbl-mini">
            <thead><tr><th>平台</th><th style="text-align:center">返奖率</th><th style="text-align:center">庄家抽水</th></tr></thead>
            <tbody>${platRows}</tbody>
          </table>
          <div class="sec-label" style="margin-top:18px"><h3>去水概率（最优组合）</h3></div>
          ${probBars}
          <div class="sec-label" style="margin-top:18px"><h3>多源价差</h3></div>
          <table class="tbl-mini">
            <thead><tr><th>结果</th>${PLATFORMS.map(p => `<th style="text-align:center">${PDEF[p].name.replace("模拟平台 ", "")}</th>`).join("")}<th style="text-align:right">价差</th></tr></thead>
            <tbody>${spreadRows}</tbody>
          </table>
        </div>
      </div>
      <div class="sec">
        <div class="sec-head"><span class="sec-num n3">03</span><span class="sec-title">水位演变</span></div>
        <div class="sec-body">${trackSection}</div>
      </div>
      <div class="home-foot">演示数据 · 概率与价差用于研究，不构成投注建议</div>`;
  }

  // ============================================================
  //  系统（数据源 / 规则 / 控制台）
  // ============================================================
  function sys() {
    const st = S.settings;
    const isReal = S.dataMode === "real";
    // 数据源
    const srcRows = isReal
      ? `<tr>
        <td><span class="dot ${S.real.status === "CONNECTED" ? "g" : "o"}"></span> <b>HG 数据源</b></td>
        <td class="mono">约 0.5s 采集</td>
        <td class="mono">0%</td>
        <td class="mono">2 / 2 / 20000</td>
        <td>${S.real.status === "CONNECTED"
          ? (S.real.stream ? "正常 · SSE 已连接" : "正常 · 等待推送")
          : "异常"}</td>
      </tr>`
      : PLATFORMS.map(p => {
        const d = PDEF[p];
        const h = S.health[p];
        const lat = h.lat.toFixed(2);
        const state = App.healthState(p);
        const dotCls = state === "OPEN" ? "g" : "o";
        return `<tr>
          <td><span class="dot ${dotCls}"></span> <b>${d.name}</b></td>
          <td class="mono">${lat}s</td>
          <td>${(d.rebate * 100).toFixed(1)}%</td>
          <td class="mono">${d.min} / ${d.step} / ${d.max}</td>
          <td>${HEALTH_TXT[state] || state}${state === "WARNING" ? "（延迟升高）" : ""}</td>
        </tr>`;
      }).join("");

    function segBtns(group, val, opts) {
      return `<div class="seg">${opts.map(([v, l]) =>
        `<button class="${String(val) === String(v) ? "on" : ""}" onclick="App.${group}(${isNaN(Number(v)) ? `'${v}'` : v})">${l}</button>`).join("")}</div>`;
    }

    const consoleSec = isReal
      ? (() => {
        const w = S.real.worker || {};
        const streamOK = S.real.stream;
        const hgh = S.real.hg_http || {};
        const hgEx = hgh.extra || {};
        const hgConnected = hgh.bootstrapped || hgh.status === "CONNECTED";
        const hgStateTxt = hgConnected
          ? '<span class="dot g"></span> 直连正常' + (hgh.lastError ? ' · 上次错误: ' + hgh.lastError : '')
          : (hgh.status === "WAITING_LOGIN"
            ? '<span class="dot o"></span> 等待登录（请在 Chrome crw066 页面重新登录）' + (hgh.lastError ? ' · ' + hgh.lastError : '')
            : '<span class="dot o"></span> 未连接');
        const hgCnt = hgEx.match_count != null ? hgEx.match_count : hgh.matchCount || "—";
        const hgLat = (hgEx.latency_p50_ms != null ? hgEx.latency_p50_ms.toFixed(0) + " / " + hgEx.latency_p95_ms.toFixed(0) + " / " + (hgEx.latency_p99_ms != null ? hgEx.latency_p99_ms.toFixed(0) : "-") + " ms" : "—");
        const hgDbTxt = (hgEx.last_db_commit_at != null ? "—" : "—");
        const hgEvt = hgEx.last_valid_event_at ? new Date(hgEx.last_valid_event_at).toLocaleTimeString() : "—";
        const hgDbAt = hgEx.last_db_commit_at ? new Date(hgEx.last_db_commit_at).toLocaleTimeString() : "—";
        const hgSync = hgh.last_sync_report ? JSON.stringify(hgh.last_sync_report) : "—";
        const hgErr = hgh.lastError || hgEx.last_error || "—";
        return `
      <div class="sec">
        <div class="sec-head"><span class="sec-num n3">03</span><span class="sec-title">实时链路状态</span>
          <span class="sec-sub">高频采集 + SSE 推送</span></div>
        <div class="sec-body">
          <table class="tbl-mini">
            <tbody>
              <tr><td>数据来源</td><td>${S.real.source || "HG"}</td></tr>
              <tr><td>实时推送连接</td><td>${streamOK
                ? '<span class="dot g"></span> SSE 已连接（变盘即推送）'
                : '<span class="dot o"></span> 未连接 / 自动重连中'}</td></tr>
              <tr><td>采集间隔</td><td class="mono">${w.base_interval != null ? w.base_interval + " 秒" : "—"}</td></tr>
              <tr><td>已轮询次数</td><td class="mono" data-w="poll_count">${w.poll_count != null ? w.poll_count : "—"}</td></tr>
              <tr><td>检测到变盘</td><td class="mono" data-w="change_count">${w.change_count != null ? w.change_count + " 次" : "—"}</td></tr>
              <tr><td>连续失败 / 退避</td><td class="mono" data-w="consecutive_fail">${w.consecutive_fail != null ? w.consecutive_fail : "—"}</td></tr>
              <tr><td>官方接口响应</td><td class="mono" data-w="fetch_ms">${w.last_fetch_ms != null ? w.last_fetch_ms + " ms" : "—"}</td></tr>
              <tr><td>变盘处理（入库）</td><td class="mono" data-w="process_ms">${w.last_process_ms != null ? w.last_process_ms + " ms" : "—"}</td></tr>
              <tr><td>推送延迟</td><td class="mono" data-w="latency">${S.real.pushLatencyMs != null ? S.real.pushLatencyMs + " ms" : "—"}</td></tr>
              <tr><td>后端生成时间</td><td class="mono">${S.real.generatedAt || "—"}</td></tr>
              <tr><td>前端最近拉取</td><td class="mono">${S.real.lastLoaded ? new Date(S.real.lastLoaded).toLocaleString() : "—"}</td></tr>
              <tr><td>当前市场范围</td><td>${S.real.marketNote || "仅胜平负"}</td></tr>
              <tr><td>当前在售场次</td><td class="mono">${S.matches.length}</td></tr>
              <tr><td colspan="2" style="border-top:1px dashed #333">HG 直连采集（http-direct）</td></tr>
              <tr><td>HG 连接状态</td><td>${hgStateTxt}</td></tr>
              <tr><td>HG 有效赛事</td><td class="mono">${hgCnt}</td></tr>
              <tr><td>HG 请求延迟 p50/p95/p99</td><td class="mono">${hgLat}</td></tr>
              <tr><td>HG 最近有效事件</td><td class="mono">${hgEvt}</td></tr>
              <tr><td>HG 最近 DB 提交</td><td class="mono">${hgDbAt}</td></tr>
              <tr><td>HG 同步报告</td><td class="mono">${hgSync}</td></tr>
              <tr><td>HG 错误</td><td class="mono">${hgErr}</td></tr>
            </tbody>
          </table>
          <div class="sec-tip">如需体验多平台对冲、多串玩法与购买图，请点击顶部「真实数据」切换到「演示模式」。</div>
        </div>
      </div>`;
      })()
      : `
      <div class="sec">
        <div class="sec-head"><span class="sec-num n3">03</span><span class="sec-title">模拟市场控制台</span>
          <span class="sec-sub">测试用：控制引擎并手动触发事件</span></div>
        <div class="sec-body">
          <div class="console-box">
            <div class="ctrl-group">
              <h4>模拟开关</h4>
              <div class="ctrl-row">
                <button class="btn ${st.running ? "primary" : ""}" onclick="App.toggleRun()">${st.running ? "运行中（点击暂停）" : "已暂停（点击运行）"}</button>
              </div>
            </div>
            <div class="ctrl-group">
              <h4>模拟速度</h4>
              <div class="ctrl-row">${segBtns("setSpeed", st.speed, [[1, "1x"], [2, "2x"], [5, "5x"], [10, "10x"]])}</div>
            </div>
            <div class="ctrl-group">
              <h4>赔率波动</h4>
              <div class="ctrl-row">${segBtns("setVol", st.vol, [["低", "低"], ["中", "中"], ["高", "高"]])}</div>
            </div>
            <div class="ctrl-group">
              <h4>赛事数量</h4>
              <div class="ctrl-row">${segBtns("setN", st.n, [[20, "20"], [30, "30"], [50, "50"]])}</div>
            </div>
          </div>
          <div class="ctrl-group" style="margin-top:12px">
            <h4>手动触发事件</h4>
            <div class="ctrl-row">
              <button class="btn primary" onclick="App.makeNew()">制造新机会</button>
              <button class="btn" onclick="App.makeInvalid()">制造机会失效</button>
              <button class="btn" onclick="App.triggerDrift()">触发赔率变化</button>
              <button class="btn" onclick="App.reinit()">重新初始化模拟数据</button>
            </div>
          </div>
        </div>
      </div>`;

    return `
      <div class="page-title">系统监控</div>
      <div class="page-sub">${isReal ? "数据源 · 规则版本 · 真实数据说明。" : "数据源 · 结算规则版本 · 模拟市场控制台（开发 / 演示 / 测试用）。"}</div>

      <div class="sec">
        <div class="sec-head"><span class="sec-num n1">01</span><span class="sec-title">数据源</span>
          <span class="sec-sub">${isReal ? "HG 行情数据（Real Adapter）" : "当前全部为模拟平台（Mock Adapter）"}</span></div>
        <div class="sec-body">
          <table class="tbl-mini">
            <thead><tr><th>平台</th><th>采集延迟</th><th>返点率</th><th>最小/步长/最大</th><th>状态</th></tr></thead>
            <tbody>${srcRows}</tbody>
          </table>
          <div class="sec-tip">
            ${isReal
              ? "数据链路：HG 接口 → 后端高频采集 → 变盘检测 → SSE 即时推送 → 前端秒级更新（另有 60 秒兜底轮询）。浏览器只访问本地后端，不直连任何平台。"
              : "接口预留：Mock Adapter 与未来 Real Adapter（TC 体彩等）输出同一结构，替换数据源后，标准化、匹配、计算、机会、清单、追踪全部无需重写。"}
          </div>
        </div>
      </div>

      <div class="sec">
        <div class="sec-head"><span class="sec-num n2">02</span><span class="sec-title">规则与版本</span></div>
        <div class="sec-body" style="font-size:12px;line-height:1.9">
          <table class="tbl-mini">
            <thead><tr><th>项</th><th>当前值</th></tr></thead>
            <tbody>
              <tr><td>结算规则</td><td>各玩法规则 v1：胜（WIN）/ 负（LOSE）/ 走水（PUSH）/ 取消（VOID）</td></tr>
              <tr><td>资金优化</td><td>词典序：先最大化含返点净收益，再最小化实际投入</td></tr>
              <tr><td>机会状态</td><td>盈利 / 保本 / 亏损 / 数据不足 · 生命周期：等待→发现→计算→覆盖→更新→失效</td></tr>
              <tr><td>重算触发</td><td>任一报价变化 → 以双方最新报价重算该场</td></tr>
              <tr><td>数据口径</td><td>报价版本 / 机会版本 / 方案快照全部保留</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      ${consoleSec}
      <div class="home-foot">${isReal
        ? "HG 实时数据 · 系统不提供下单，仅用于研究"
        : "全部为演示数据 · 平台统一命名“模拟平台 A/B/C”，不冒充真实接入"}</div>`;
  }

  // ============================================================
  //  购买确认弹窗内容
  // ============================================================
  function confirmCartBody(id, type, cur) {
    const def = marketDef(type);
    return `
      <div class="plan-box">
        ${cur.legs.map((l, i) => `<div class="plan-row ${l.p === "C" ? "ext" : "tc"}">
          <span class="pr-idx">${i + 1}</span>
          <span class="pr-src">${PDEF[l.p].name}</span>
          <span class="pr-pick">${l.label}</span>
          <span class="pr-odds">@${l.odds}</span>
          <span class="pr-amt">¥${cur.stake[i].toFixed(2)}</span>
        </div>`).join("")}
        <div class="plan-total">
          总投入 <b>¥${cur.total.toFixed(2)}</b><br>
          理论回收 <b>¥${(cur.total + cur.minPnl).toFixed(2)}</b>（最差情况）<br>
          最小收益 <b style="color:var(--good)">${fmtMoney(cur.minPnl)}（${cur.roi.toFixed(2)}%）</b><br>
          最大收益 <b>${fmtMoney(cur.maxPnl)}</b>
        </div>
      </div>
      <div class="match-ops">
        <button class="btn primary" onclick="App.confirmAddCart('${id}','${type}')">加入购买清单</button>
        <button class="btn" onclick="App.closeModal()">取消</button>
      </div>
      <div class="sec-tip">模拟购买方案：仅保存快照，不执行真实下单。</div>`;
  }

  // ============================================================
  //  导出购买方案为图片（方便照单购买）
  // ============================================================
  function drawOpportunityImage(m, snap, type) {
    const def = marketDef(type);
    const legs = snap.legs;
    const W = 760, pad = 34, rowH = 52, topH = 208, botH = 170;
    const H = topH + legs.length * rowH + botH;
    const dpr = 2;
    const cv = document.createElement("canvas");
    cv.width = W * dpr; cv.height = H * dpr;
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    const F = '"PingFang SC","Microsoft YaHei",sans-serif';
    const M = '"JetBrains Mono","IBM Plex Mono",Consolas,monospace';
    const hline = y => {
      g.strokeStyle = "rgba(255,255,255,.08)"; g.lineWidth = 1;
      g.beginPath(); g.moveTo(pad, y); g.lineTo(W - pad, y); g.stroke();
    };
    // 背景
    g.fillStyle = "#0B0E12"; g.fillRect(0, 0, W, H);
    // 顶部品牌
    g.font = `700 15px ${F}`; g.fillStyle = "#38e0c8";
    g.fillText("FX　足球量化市场研究", pad, 44);
    g.textAlign = "right";
    g.font = `400 12px ${M}`; g.fillStyle = "#5E6A7B";
    g.fillText("模拟购买清单", W - pad, 44);
    g.textAlign = "left";
    hline(64);
    // 比赛
    g.font = `800 33px ${F}`; g.fillStyle = "#F2F4F7";
    g.fillText(`${m.home} VS ${m.away}`, pad, 118);
    g.font = `400 15px ${M}`; g.fillStyle = "#8E98A7";
    g.fillText(`${m.comp}　·　开赛 ${dateStr(m.kickoff)}　·　玩法 ${def.name}`, pad, 152);
    // 状态徽章
    const st = "完整覆盖";
    g.font = `700 13px ${F}`;
    const bw = g.measureText(st).width + 26;
    g.fillStyle = "rgba(61,220,151,.12)";
    g.beginPath();
    g.roundRect ? g.roundRect(pad, 168, bw, 30, 8) : g.rect(pad, 168, bw, 30);
    g.fill();
    g.fillStyle = "#3ddc97";
    g.fillText(st, pad + 13, 188);
    hline(topH);
    // 投注腿
    legs.forEach((l, i) => {
      const cy = topH + i * rowH + rowH / 2;
      g.font = `700 17px ${F}`; g.fillStyle = "#F2F4F7";
      g.fillText(PDEF[l.p].name, pad, cy + 6);
      g.font = `400 15px ${F}`; g.fillStyle = "#8E98A7";
      g.fillText(l.label, pad + 208, cy + 6);
      g.font = `700 16px ${M}`; g.fillStyle = "#38e0c8";
      g.fillText("@" + l.odds, pad + 360, cy + 6);
      g.textAlign = "right";
      g.font = `800 21px ${M}`; g.fillStyle = "#F2F4F7";
      g.fillText("¥" + Number(snap.stake[i]).toFixed(0), W - pad, cy + 7);
      g.textAlign = "left";
    });
    const yb = topH + legs.length * rowH;
    hline(yb);
    // 汇总
    const by = yb + 36;
    g.font = `400 13px ${M}`; g.fillStyle = "#8E98A7";
    g.fillText("总投入", pad, by);
    g.font = `800 24px ${M}`; g.fillStyle = "#F2F4F7";
    g.fillText("¥" + snap.total.toFixed(2), pad, by + 34);
    g.font = `400 13px ${M}`; g.fillStyle = "#8E98A7";
    g.fillText("最小收益（含返点）", pad + 260, by);
    g.font = `800 24px ${M}`; g.fillStyle = "#3ddc97";
    g.fillText(fmtMoney(snap.minPnl), pad + 260, by + 34);
    g.textAlign = "right";
    g.font = `400 13px ${M}`; g.fillStyle = "#8E98A7";
    g.fillText("最小收益率", W - pad, by);
    g.font = `800 24px ${M}`; g.fillStyle = "#3ddc97";
    g.fillText("+" + snap.roi.toFixed(2) + "%", W - pad, by + 34);
    g.textAlign = "left";
    // 底部
    hline(H - 52);
    g.font = `400 12px ${M}`; g.fillStyle = "#5E6A7B";
    g.fillText("演示数据 · 仅模拟，不执行真实交易 · 购买前请核对最新报价", pad, H - 26);
    g.textAlign = "right";
    g.fillText("生成于 " + App.hms(), W - pad, H - 26);
    g.textAlign = "left";
    // 下载
    cv.toBlob(blob => {
      const url = URL.createObjectURL(blob);
      if (window.__EXPORT_PREVIEW) {
        // 预览自检模式：把购买图固定展示在页面，不触发下载
        let box = document.getElementById("export-preview");
        if (!box) {
          box = document.createElement("div");
          box.id = "export-preview";
          box.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.72);display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:30px 16px";
          document.body.appendChild(box);
          box.addEventListener("click", () => box.remove());
        }
        const img = document.createElement("img");
        img.src = url;
        img.style.cssText = "width:760px;max-width:100%;border-radius:16px;display:block;box-shadow:0 30px 80px rgba(0,0,0,.6)";
        box.innerHTML = ""; box.appendChild(img);
        return;
      }
      const a = document.createElement("a");
      a.href = url;
      a.download = `购买图_${m.home}VS${m.away}_${def.name}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    }, "image/png");
  }

  // 从购买清单导出
  function exportPurchaseImage(cid) {
    const c = S.cart.find(z => z.cid === cid);
    if (!c) return;
    drawOpportunityImage(S.matches.find(z => z.id === c.matchId), c.snapshot, c.marketType);
  }
  // 从比赛详情直接导出（需当前为正机会）
  function exportOpportunityImage(matchId, type) {
    const o = S.opps[matchId + "|" + type];
    if (!o || o.status !== "POSITIVE") return;
    drawOpportunityImage(S.matches.find(z => z.id === matchId), o, type);
  }

  // hash 路由（支持直达：#/cat/1X2、#/match/M003/1X2 等）
  function stateHash() {
    if (S.view === "cat") return "#/cat/" + S.curCat;
    if (S.view === "match") return "#/match/" + S.curMatch + "/" + S.curMarket;
    return "#/" + S.view;
  }
  function applyHash() {
    const h = (location.hash || "").replace(/^#\//, "");
    if (!h) return;
    const p = h.split("/");
    if (p[0] === "cat") { S.view = "cat"; S.curCat = p[1]; }
    else if (p[0] === "match") { S.view = "match"; S.curMatch = p[1]; S.curMarket = p[2]; }
    else if (["home", "play", "opp", "cart", "track", "research", "history", "sys"].includes(p[0])) S.view = p[0];
  }

  // ============================================================
  //  渲染分派
  // ============================================================
  // 视图归属主导航分组（cat/match 归 Markets）
  function navGroup(v) {
    if (["play", "cat", "match"].includes(v)) return "play";
    if (v === "track") return "research";
    return v;
  }
  // 大数字 count-up
  function runCountUp() {
    document.querySelectorAll("[data-to]").forEach(el => {
      const to = Number(el.dataset.to);
      const t0 = performance.now();
      const step = t => {
        const p = Math.min(1, (t - t0) / 900);
        el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3)));
        if (p < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    });
  }

  let lastView = null;
  // 系统页（真实模式）：收到 SSE 心跳后局部刷新数字，不整页重绘
  function updateSysLive(w) {
    if (S.view !== "sys" || S.dataMode !== "real" || !w) return;
    document.querySelectorAll("[data-w]").forEach(el => {
      const k = el.dataset.w;
      if (k === "latency") {
        if (S.real.pushLatencyMs != null) el.textContent = S.real.pushLatencyMs + " ms";
        return;
      }
      const v = w[k];
      if (v == null) return;
      if (k === "change_count") el.textContent = v + " 次";
      else if (k === "fetch_ms" || k === "process_ms") el.textContent = v + " ms";
      else el.textContent = v;
    });
  }

  function render() {
    const view = document.getElementById("view");
    if (!view) return;
    const switched = S.view !== lastView;
    const grp = navGroup(S.view);
    document.querySelectorAll(".nav-item[data-nav]").forEach(el => {
      el.classList.toggle("active", el.dataset.nav === grp);
    });
    const map = {
      home, play, cat, match, opp, cart, track, research, history, sys,
    };
    const fn = map[S.view] || home;

    // P0 修复：渲染异常兜底，防止整个页面白屏
    const safeRender = () => {
      try {
        return fn();
      } catch (e) {
        console.error("[P0] view render failed:", S.view, e);
        return `<div class="empty" style="padding:60px 20px;text-align:center">
          <div style="font-size:48px;margin-bottom:16px">⚠️</div>
          <div style="font-size:18px;margin-bottom:8px">页面渲染出错</div>
          <div style="color:var(--muted);margin-bottom:24px">错误信息：${esc(e.message || "未知错误")}</div>
          <button class="btn primary" onclick="App.go('home')">返回首页</button>
          <button class="btn" onclick="location.reload()">刷新页面</button>
        </div>`;
      }
    };

    // 替换内容并挂载（含计数、hash、高亮、缓存）
    const mount = () => {
      view.innerHTML = safeRender();
      // 淡入只在视图切换时播放；同视图的数据刷新（tick）不重播，避免闪烁
      if (!switched) view.querySelectorAll(".reveal").forEach(el => el.classList.remove("reveal"));
      App.updateTopbar();
      const h = stateHash();
      if (location.hash !== h) { try { history.replaceState(null, "", h); } catch (e) { } }
      if (switched) window.scrollTo({ top: 0 });
      runCountUp();
      // 数据驱动动画：赔率变化高亮（仅同视图数据刷新）
      view.querySelectorAll(".odds-cell").forEach(el => {
        const k = el.dataset.key, nv = parseFloat(el.textContent);
        if (!switched && oddsCache[k] != null && oddsCache[k] !== nv) {
          el.classList.add(nv > oddsCache[k] ? "odds-up" : "odds-down");
          setTimeout(() => el.classList.remove("odds-up", "odds-down"), 900);
        }
        oddsCache[k] = nv;
      });
      // 更新动画缓存
      metricCache.m = S.matches.length;
      metricCache.p = PLATFORMS.length;
      metricCache.c = S.matches.reduce((s, m) =>
        s + Object.values(m.markets).reduce((a, mk) => a + mk.changes.length, 0), 0);
      metricCache.o = App.positiveOpps().length;
      posCache.clear();
      App.positiveOpps().forEach(o => posCache.add(o.key));
      lastView = S.view;
    };

    if (switched && !reducedMotion) {
      // 旧视图淡出 → 替换 → 新视图淡入
      view.classList.add("view-leave");
      setTimeout(() => {
        view.classList.remove("view-leave");
        mount();
        view.classList.add("view-enter");
        requestAnimationFrame(() => requestAnimationFrame(() => view.classList.remove("view-enter")));
      }, 200);
    } else {
      mount();
    }
  }

  window.AppViews = { render, updateSysLive, confirmCartBody, exportPurchaseImage, exportOpportunityImage };
  // 首屏自动渲染（支持 hash 直达）
  try { applyHash(); render(); } catch (e) {}

  function renderHgTable() {
    var wrap = document.getElementById("hg-table-wrap");
    var tbody = document.getElementById("hg-tbody");
    var statusEl = document.getElementById("hg-status");
    var countEl = document.getElementById("hg-count");
    var timeEl = document.getElementById("hg-time");
    if (!wrap || !tbody) return;
    var hgMatches = S.matches.filter(function(m) { return m.hgOnly || (m.markets && m.markets["1X2"] && m.markets["1X2"].platforms && m.markets["1X2"].platforms.HG); });
    if (!hgMatches.length) { wrap.style.display = "none"; return; }
    wrap.style.display = "block";
    var hgStatus = S.real && S.real.hg ? S.real.hg.status : "CONNECTED";
    statusEl.textContent = hgStatus === "CONNECTED" ? "LIVE" : hgStatus;
    statusEl.className = "hg-status " + hgStatus;
    countEl.textContent = hgMatches.length + " 场比赛";
    var now = new Date();
    timeEl.textContent = App.hm(now) + ":" + String(now.getSeconds()).padStart(2,"0");
    var rows = "";
    hgMatches.forEach(function(m) {
      var mk = m.markets || {};
      var x1 = mk["1X2"];
      var hg = x1 && x1.platforms && x1.platforms.HG ? x1.platforms.HG.odds : [];
      function od(idx) { if (!hg || !hg[idx]) return "-"; return hg[idx]; }
      function om(mkO,k) { return mkO && mkO[k] ? (mkO[k].odds || "-") : "-"; }
      function ol(mkO) { return mkO && mkO.line ? mkO.line : "-"; }
      rows += "<tr>" +
        "<td>" + App.esc(m.kickoff_time||"") + "</td>" +
        "<td>" + App.esc((m.competition||m.comp||"").substring(0,14)) + "</td>" +
        "<td class=hg-home>" + App.esc(m.home) + "</td>" +
        "<td class=hg-away>" + App.esc(m.away) + "</td>" +
        "<td>" + App.esc((m.score_h||"")+"-"+(m.score_c||"")) + "</td>" +
        "<td class=hg-odds>" + od(0) + "</td><td class=hg-odds>" + od(1) + "</td><td class=hg-odds>" + od(2) + "</td>" +
        "<td class=hg-odds>" + om(mk["OU"],"OVER") + "</td><td class=hg-odds>" + om(mk["OU"],"UNDER") + "</td><td>" + ol(mk["OU"]) + "</td>" +
        "<td class=hg-odds>" + om(mk["EO"],"EVEN") + "</td><td class=hg-odds>" + om(mk["EO"],"ODD") + "</td>" +
        "<td class=hg-odds>" + om(mk["GL"],"OVER") + "</td><td class=hg-odds>" + om(mk["GL"],"UNDER") + "</td><td>" + ol(mk["GL"]) + "</td>" +
        "<td class=hg-odds>" + om(mk["CNROU"],"OVER") + "</td><td class=hg-odds>" + om(mk["CNROU"],"UNDER") + "</td><td>" + ol(mk["CNROU"]) + "</td>" +
        "<td class=hg-odds>" + om(mk["HTOU"],"OVER") + "</td><td class=hg-odds>" + om(mk["HTOU"],"UNDER") + "</td><td>" + ol(mk["HTOU"]) + "</td>" +
        "<td class=hg-odds>" + om(mk["RE"],"HOME") + "</td><td class=hg-odds>" + om(mk["RE"],"AWAY") + "</td><td>" + ol(mk["RE"]) + "</td>" +
        "</tr>";
    });
    tbody.innerHTML = rows;
  }

})();