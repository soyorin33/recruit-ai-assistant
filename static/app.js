/* 招聘助手 · 前端演示台（原生 JS，无构建步骤） */
(() => {
  "use strict";

  const STAGES = ["简历筛选", "初试", "复试", "Offer", "已入职"];
  const OPERATOR = "HR小王";
  const EXAMPLES = [
    ["新候选人建档", "张三 Java后端 BOSS来的，一面过了，明天下午3点二面，面试官陈晨"],
    ["多轮·改时间", "把他的二面改到后天上午10点"],
    ["多轮·换面试官", "他的面试官换成林峰"],
    ["多轮·追问", "他现在什么情况？"],
    ["一句话更新多人", "王芳二面挂了，算法基础薄弱；赵磊接了offer"],
    ["入职", "孙浩今天入职了"],
    ["带手机号建档", "李雷 前端 内推 13800138000 约了后天上午10点一面 面试官林峰"],
    ["触发人工确认", "张三一面挂了"],
    ["问数", "本周各岗位进了几个复试？"],
    ["问漏斗", "招聘漏斗怎么样？"],
    ["查人", "赵磊怎么样了"],
    ["日报", "发一下今天的日报"],
    ["📄 示例简历", "__resume__"],
  ];

  const S = { status: null, dash: null, cands: [], msgs: [], openPending: new Set(), prevUpdated: null, charts: {} };
  const $ = (id) => document.getElementById(id);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  /* ---------- 安全的 DOM 构造 ---------- */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
      ...opts,
      body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
    });
    if (res.status === 401) { location.href = "/login"; throw new Error("未登录"); }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.json();
  }

  function toast(text) {
    const t = $("toast");
    t.textContent = text;
    t.classList.add("show");
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove("show"), 2200);
  }

  const shortTime = (s) => (s ? s.slice(5, 16) : "");
  const shortT = (v) => (/^\d{4}-\d{2}-\d{2} /.test(String(v)) ? String(v).slice(5, 16) : v);
  const TOOL_LABEL = { guard: "🛡️ 校验拦截", record_candidates: "记录候选人", search_candidates: "查找候选人", get_candidate: "查看详情", query_data: "统计查询", daily_report: "生成日报" };
  const CARD_FIELDS = { stage: "阶段", status: "状态", position: "岗位", channel: "渠道", interview_time: "面试时间", interviewer: "面试官" };
  const isDark = () => document.documentElement.dataset.theme === "dark" || (document.documentElement.dataset.theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
  const stageColor = (s) => css(`--st${Math.max(0, STAGES.indexOf(s))}`);

  /* ======================= 状态栏 ======================= */
  function pushChip(st) {
    const p = st.push, last = p.last || {};
    const times = `每天 ${p.alert_time} 跟进提醒、${p.report_time} 日报`;
    let on, label, title;
    if (st.aibot_ws.enabled && p.chats > 0) {
      on = st.aibot_ws.state === "已连接";
      label = `日报推送：招聘助手 → ${p.chats} 个群`;
      title = `${times}，由招聘助手主动推送` + (p.webhook ? "；Webhook 群机器人作为备用" : "");
    } else if (st.aibot_ws.enabled) {
      on = false; label = p.webhook ? "日报推送：Webhook" : "日报推送：待绑定群";
      title = "在企微群里 @招聘助手 说一句话，这个群就会收到每日日报和跟进提醒";
    } else {
      on = p.webhook; label = p.webhook ? "日报推送：Webhook" : "日报推送：未配置";
      title = `${times}推送到企微群`;
    }
    if (last.error) title += `\n上次推送：${last.error}`;
    else if (last.at) title += `\n上次推送：${last.at}，经${last.via}`;
    return chip(on, label, title);
  }

  function tdocsChip(t) {
    const label = !t.enabled ? "腾讯文档：未配置" : t.last_error ? "腾讯文档：同步异常" : "腾讯文档：已接入";
    const title = t.last_error || (t.enabled ? `候选人变更实时写入腾讯文档智能表格（官方 MCP），本次启动以来已同步 ${t.synced} 次，点击打开表格` : "配置 TDOCS_TOKEN 并运行 scripts.tdocs_setup 后启用");
    const inner = [h("span", { class: "dot" + (t.enabled && !t.last_error ? " on" : "") }), label];
    return t.enabled && t.url
      ? h("a", { class: "chip", title, href: t.url, target: "_blank", rel: "noopener" }, ...inner)
      : h("span", { class: "chip", title }, ...inner);
  }

  async function loadStatus() {
    S.status = await api("/api/status");
    const st = S.status;
    const chip = (on, label, title) => h("span", { class: "chip", title }, h("span", { class: "dot" + (on ? " on" : "") }), label);
    $("chips").replaceChildren(
      h("span", { class: "chip", title: "Server-Sent Events 实时推送" }, h("span", { class: "dot live", id: "live-dot" }), "实时同步"),
      chip(true, st.llm.enabled ? `AI 引擎：${st.llm.model}` : "AI 引擎：规则引擎（演示模式）",
        st.llm.enabled ? st.llm.base_url : "未配置 LLM_API_KEY，自动降级为本地规则引擎；配置后即切换为大模型"),
      chip(st.wecom_app, st.wecom_app ? "企微应用：已接入" : "企微应用：未接入", "接收 HR 在企业微信中发给招聘助手的消息"),
      chip(st.smartsheet.enabled, st.smartsheet.enabled ? `智能表格：已同步 ${st.smartsheet.synced}` : "智能表格：未配置", st.smartsheet.last_error || "候选人变更自动写入企微智能表格"),
      pushChip(st),
      tdocsChip(st.tdocs),
      st.aibot_ws.enabled
        ? chip(st.aibot_ws.state === "已连接", `群 @ 助手：${st.aibot_ws.state}`,
          st.aibot_ws.last_error || `企微智能机器人长连接，已收到 ${st.aibot_ws.received} 条消息；在群里 @招聘助手 即可记录 / 查询`)
        : chip(st.aibot, st.aibot ? "群 @ 助手：已接入" : "群 @ 助手：未配置", "企微智能机器人：在群里 @招聘助手 即可记录 / 查询"),
      chip(st.vision.enabled, st.vision.enabled ? `截图识别：${st.vision.model}` : "截图识别：未配置", "配置 VISION_MODEL 后可识别简历截图、聊天截图"),
      chip(st.auth_enabled, st.auth_enabled ? "访问：已加口令" : "访问：未加口令（仅限本机）",
        st.auth_enabled ? "看板与接口需要登录" : "部署到公网前请设置 ACCESS_TOKEN"),
    );
    $("btn-reset").classList.toggle("hidden", !st.demo_mode);
    $("btn-logout").classList.toggle("hidden", !st.auth_enabled);
  }

  /* ======================= 聊天 ======================= */
  function renderExamples() {
    $("examples").replaceChildren(...EXAMPLES.map(([label, text]) =>
      h("button", { class: "ex", title: text === "__resume__" ? "上传一份示例简历" : text, onclick: () => (text === "__resume__" ? sendSampleResume() : send(text)) }, label === "📄 示例简历" ? label : `${label}：${text}`)));
  }

  async function loadMessages() {
    const [m, p] = await Promise.all([api("/api/messages"), api("/api/pending")]);
    S.openPending = new Set(p.items.map((x) => x.id));
    const box = $("msgs");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    const changed = m.items.length !== S.msgs.length || (m.items.at(-1) || {}).id !== (S.msgs.at(-1) || {}).id;
    S.msgs = m.items;
    box.querySelectorAll(".mini-chart").forEach((el) => echarts?.getInstanceByDom(el)?.dispose()); // 释放旧图表实例
    box.replaceChildren(...S.msgs.map(renderMsg));
    if (S.typing) box.append(typingEl()); // 处理中重绘列表时保留「正在…」提示
    if (changed || atBottom) box.scrollTop = box.scrollHeight;
  }

  function renderMsg(m) {
    const isBot = m.role === "bot";
    const who = isBot ? "招聘助手" : m.operator || "HR";
    const meta = h("div", { class: "msg-meta" }, `${who} · ${m.ts.slice(11, 16)}`,
      m.source === "wecom" ? h("span", { class: "src-tag" }, "来自企业微信") : null);
    const bubble = h("div", { class: "bubble" }, (isBot && m.payload?.web_text) || m.text);
    if (isBot) decorateBot(bubble, m.payload || {});
    return h("div", { class: "msg " + (isBot ? "bot" : "user") },
      h("div", { class: "avatar" }, isBot ? "AI" : who.replace("HR", "").slice(-2) || "HR"),
      h("div", { class: "msg-body" }, meta, bubble));
  }

  function decorateBot(bubble, p) {
    // 待确认卡片
    for (const a of p.actions || []) {
      if (a.type !== "pending") continue;
      const open = S.openPending.has(a.pending_id);
      bubble.append(h("div", { class: "card pending" },
        h("div", { class: "card-title" }, "⚠️ 需要人工确认", h("span", { class: "msg-meta" }, `#${a.pending_id}`)),
        ...a.plans.map((pl) => h("div", { class: "change-row" }, h("b", {}, pl.op === "create" ? `新建 ${pl.name}` : pl.name), "　",
          Object.entries(pl.changes).filter(([k]) => CARD_FIELDS[k])
            .map(([k, [o, n]]) => `${CARD_FIELDS[k]} ${o ? shortT(o) + " → " : ""}${n == null ? "清空" : shortT(n)}`).join("；"))),
        h("ul", { class: "reasons" }, ...a.reasons.map((r) => h("li", {}, r))),
        h("div", { class: "card-actions" },
          h("button", { class: "btn sm primary", disabled: !open, onclick: () => resolvePending(a.pending_id, true) }, open ? "确认写入" : "已处理"),
          open ? h("button", { class: "btn sm", onclick: () => resolvePending(a.pending_id, false) }, "取消") : null)));
    }
    // 问数结果：表格 + 小图
    if (p.kind === "query" && p.table && p.table.rows.length) {
      const t = p.table;
      bubble.append(h("table", { class: "mini-table tnum" },
        h("thead", {}, h("tr", {}, ...t.columns.map((c) => h("th", {}, c)))),
        h("tbody", {}, ...t.rows.slice(0, 12).map((r) => h("tr", {}, ...r.map((c) => h("td", {}, c)))))));
      if (p.chart && p.chart.type === "bar" && window.echarts) {
        const el = h("div", { class: "mini-chart" });
        bubble.append(el);
        requestAnimationFrame(() => miniBar(el, p.chart));
      }
    }
    // 智能体推理过程：每一步调用了什么工具、得到什么结果
    if (p.agent) {
      const ag = p.agent;
      const bits = [`${ag.steps.length} 步`, ag.model, ag.latency_ms && `${(ag.latency_ms / 1000).toFixed(1)}s`, ag.tokens && `${ag.tokens} tokens`].filter(Boolean);
      bubble.append(h("details", { class: "ai" }, h("summary", {}, `🧠 智能体推理 · ${bits.join(" · ")}`),
        ag.steps.length ? h("ol", { class: "steps" }, ...ag.steps.map((s) => h("li", {},
          h("b", {}, TOOL_LABEL[s.tool] || s.tool), "　", s.summary,
          h("code", {}, JSON.stringify(s.args))))) : h("div", {}, "未调用工具（直接回复 / 反问）")));
    }
    // AI 解析详情：展示大模型 / 规则引擎的结构化输出，体现「AI 做了什么」
    const ex = p.extraction;
    if (ex) {
      const engine = ex.engine === "llm" ? `大模型 ${ex.llm_meta?.model || ""}` : "规则引擎";
      const bits = [engine, ex.coreference && `指代 ${ex.coreference}`, ex.intent && `意图 ${ex.intent}`, ex.confidence != null && `置信度 ${Number(ex.confidence).toFixed(2)}`,
        ex.llm_meta?.latency_ms && `${ex.llm_meta.latency_ms}ms`].filter(Boolean);
      const shown = { ...ex };
      if (p.query) shown.query_plan = p.query.plan;
      bubble.append(h("details", { class: "ai" }, h("summary", {}, `🧠 AI 解析 · ${bits.join(" · ")}`),
        ex.fallback_reason ? h("div", {}, `⚠ 大模型不可用已自动降级：${ex.fallback_reason}`) : null,
        h("pre", {}, JSON.stringify(shown, null, 2))));
    }
  }

  function miniBar(el, chart) {
    const c = echarts.init(el, null, { renderer: "svg" });
    c.setOption({
      animation: false, // 消息列表会随实时推送重绘，避免反复播放动画
      grid: { left: 4, right: 30, top: 6, bottom: 4, containLabel: true },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: { type: "value", show: false },
      yAxis: { type: "category", inverse: true, data: chart.labels, axisTick: { show: false }, axisLine: { show: false }, axisLabel: { color: css("--text-2"), fontSize: 11 } },
      series: [{ type: "bar", data: chart.values, barWidth: 12, itemStyle: { color: css("--s1"), borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: css("--text-2"), fontSize: 11 } }],
    });
  }

  function typingEl() {
    return h("div", { class: "typing", id: "typing" }, S.typingText || "招聘助手正在理解消息…");
  }

  function showTyping(on) {
    S.typing = on;
    if (on) S.typingText = "";
    const t = document.getElementById("typing");
    if (on && !t) {
      $("msgs").append(typingEl());
      $("msgs").scrollTop = $("msgs").scrollHeight;
    } else if (!on && t) t.remove();
  }

  async function send(text) {
    text = (text ?? $("input").value).trim();
    if (!text) return;
    $("input").value = "";
    // 乐观渲染用户消息
    $("msgs").append(renderMsg({ role: "user", operator: OPERATOR, text, ts: new Date().toTimeString().slice(0, 8).padStart(19, " "), payload: {} }));
    showTyping(true);
    $("btn-send").disabled = true;
    try {
      await api("/api/messages", { method: "POST", body: { text, operator: OPERATOR } });
    } catch (e) {
      toast("发送失败：" + e.message);
    } finally {
      $("btn-send").disabled = false;
      showTyping(false);
      await refreshAll();
    }
  }

  async function uploadFile(file) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("operator", OPERATOR);
    showTyping(true);
    try {
      await api("/api/resume", { method: "POST", body: fd });
    } catch (e) {
      toast("上传失败：" + e.message);
    } finally {
      showTyping(false);
      await refreshAll();
    }
  }

  async function sendSampleResume() {
    const text = await fetch("/static/samples/resume_demo.txt").then((r) => r.text());
    await uploadFile(new File([text], "简历-陈思宇-后端开发.txt", { type: "text/plain" }));
  }

  async function resolvePending(id, confirm) {
    try {
      await api(`/api/pending/${id}`, { method: "POST", body: { confirm, operator: OPERATOR } });
    } catch (e) {
      toast(e.message);
    }
    await refreshAll();
  }

  /* ======================= 看板 ======================= */
  async function loadDashboard() {
    S.dash = await api("/api/dashboard");
    renderKpis();
    renderFunnel();
    renderTrend();
    renderPosition();
    renderChannel();
    renderAlerts();
  }

  function renderKpis() {
    const k = S.dash.kpis;
    const tile = (label, value, sub, cls = "") => h("div", { class: "kpi " + cls },
      h("div", { class: "label" }, label), h("div", { class: "value tnum" }, value), h("div", { class: "sub" }, sub));
    $("kpis").replaceChildren(
      tile("在途候选人", k.active, "状态为进行中"),
      tile("本周新增简历", k.new_week, "周一至今"),
      tile("本周面试", k.interviews_week, "已约 / 已完成场次"),
      tile("本月 Offer", k.offers_month, `本月入职 ${k.hired_month} 人`),
      tile("待跟进预警", k.alerts, k.alerts ? "超时未推进 / 漏填反馈" : "全部按时跟进", k.alerts ? "warn" : ""),
      tile("AI 自动记录", k.auto_records_30d, `近 30 天 · 约省 ${k.hours_saved_30d} 小时录入`, "ai"),
    );
    const n = k.alerts;
    $("alert-count").textContent = n;
    $("alert-count").classList.toggle("hidden", !n);
  }

  function renderFunnel() {
    const fl = S.dash.funnel;
    const max = Math.max(1, fl[0].count);
    $("funnel").replaceChildren(...fl.map((x, i) => h("div", { class: "f-row" },
      h("div", { class: "f-label" }, x.stage),
      h("div", { class: "f-track" },
        h("div", { class: "f-bar tnum", style: `width:${Math.max(6, (x.count / max) * 100)}%;background:${stageColor(x.stage)};color:${i < 1 && !isDark() ? "#0d366b" : "#fff"}`, title: `${x.stage}：${x.count} 人` }, x.count)),
      h("div", { class: "f-rate tnum" }, i === 0 ? "—" : h("span", {}, "转化 ", h("b", {}, `${x.rate}%`))))));
  }

  function baseAxis() {
    return {
      axisLine: { lineStyle: { color: css("--border") } },
      axisTick: { show: false },
      axisLabel: { color: css("--text-3"), fontSize: 11 },
      splitLine: { lineStyle: { color: css("--surface-3") } },
    };
  }

  function chart(id) {
    const el = $(id);
    if (!window.echarts || !el) return null;
    if (!S.charts[id]) S.charts[id] = echarts.init(el, null, { renderer: "svg" });
    return S.charts[id];
  }

  const tooltip = (extra = {}) => ({
    backgroundColor: css("--surface"), borderColor: css("--border"), textStyle: { color: css("--text"), fontSize: 12 },
    extraCssText: "box-shadow:0 4px 14px rgba(0,0,0,.12);border-radius:8px;", ...extra,
  });

  function renderTrend() {
    const c = chart("chart-trend");
    if (!c) return;
    const t = S.dash.trend;
    const ax = baseAxis();
    const endLabel = (name) => ({ show: true, formatter: name, color: css("--text-2"), fontSize: 11 });
    c.setOption({
      grid: { left: 8, right: 64, top: 28, bottom: 4, containLabel: true },
      legend: { top: 0, left: 0, itemWidth: 14, itemHeight: 3, textStyle: { color: css("--text-2"), fontSize: 12 } },
      tooltip: tooltip({ trigger: "axis", axisPointer: { type: "line", lineStyle: { color: css("--text-3") } } }),
      xAxis: { type: "category", data: t.labels, boundaryGap: false, ...ax, splitLine: { show: false } },
      yAxis: { type: "value", minInterval: 1, ...ax, axisLine: { show: false } },
      series: [
        { name: "AI 自动记录", type: "line", data: t.records, smooth: 0.25, symbol: "circle", symbolSize: 7, showSymbol: false,
          lineStyle: { width: 2, color: css("--s1") }, itemStyle: { color: css("--s1"), borderColor: css("--surface"), borderWidth: 2 },
          areaStyle: { color: css("--s1"), opacity: 0.08 }, endLabel: endLabel("AI 记录") },
        { name: "新增简历", type: "line", data: t.new, smooth: 0.25, symbol: "circle", symbolSize: 7, showSymbol: false,
          lineStyle: { width: 2, color: css("--s2") }, itemStyle: { color: css("--s2"), borderColor: css("--surface"), borderWidth: 2 },
          endLabel: endLabel("新增简历") },
      ],
    }, true);
  }

  function renderPosition() {
    const order = STAGES.slice(0, 4);
    $("stage-legend").replaceChildren(...order.map((s) => h("span", {}, h("i", { style: `background:${stageColor(s)}` }), s)));
    const c = chart("chart-position");
    if (!c) return;
    const rows = S.dash.by_position.filter((p) => p.active > 0).sort((a, b) => b.active - a.active);
    const ax = baseAxis();
    c.setOption({
      grid: { left: 4, right: 44, top: 4, bottom: 4, containLabel: true },
      tooltip: tooltip({ trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(127,127,127,.08)" } },
        formatter: (ps) => `<b>${ps[0].name}</b><br>` + ps.filter((p) => p.value).map((p) => `${p.marker}${p.seriesName}：${p.value} 人`).join("<br>") }),
      xAxis: { type: "value", minInterval: 1, ...ax, axisLine: { show: false } },
      yAxis: { type: "category", inverse: true, data: rows.map((r) => r.position), ...ax, axisLabel: { color: css("--text-2"), fontSize: 12 } },
      series: [...order.map((s) => ({
        name: s, type: "bar", stack: "a", barWidth: 16, data: rows.map((r) => r.by_stage[s] || null),
        itemStyle: { color: stageColor(s), borderColor: css("--surface"), borderWidth: 1 },
      })), {
        // 不可见的「合计」系列，只用于在条形末端标注在途总人数
        name: "合计", type: "bar", stack: "a", data: rows.map(() => 0), tooltip: { show: false },
        label: { show: true, position: "right", color: css("--text-2"), fontSize: 11, formatter: (p) => `${rows[p.dataIndex].active} 人` },
      }],
    }, true);
  }

  function renderChannel() {
    const c = chart("chart-channel");
    if (!c) return;
    const rows = S.dash.by_channel;
    const ax = baseAxis();
    c.setOption({
      grid: { left: 4, right: 96, top: 4, bottom: 4, containLabel: true },
      tooltip: tooltip({ trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(127,127,127,.08)" } },
        formatter: (ps) => { const r = rows[ps[0].dataIndex]; return `<b>${r.channel}</b><br>候选人 ${r.total} 人<br>进入复试及以上 ${r.advanced} 人（${r.advance_rate}%）<br>Offer 及以上 ${r.offer} 人`; } }),
      xAxis: { type: "value", minInterval: 1, ...ax, axisLine: { show: false } },
      yAxis: { type: "category", inverse: true, data: rows.map((r) => r.channel), ...ax, axisLabel: { color: css("--text-2"), fontSize: 12 } },
      series: [{ type: "bar", data: rows.map((r) => r.total), barWidth: 14, itemStyle: { color: css("--s1"), borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: "right", color: css("--text-2"), fontSize: 11, formatter: (p) => `${p.value} 人 · ${rows[p.dataIndex].advance_rate}%` } }],
    }, true);
  }

  /* ======================= 候选人总表 ======================= */
  async function loadCandidates() {
    const data = await api("/api/candidates");
    const prev = S.prevUpdated;
    S.cands = data.items;
    S.prevUpdated = Object.fromEntries(S.cands.map((c) => [c.id, c.updated_at]));
    S.flash = new Set(prev ? S.cands.filter((c) => prev[c.id] !== c.updated_at).map((c) => c.id) : []);
    $("cand-count").textContent = S.cands.length;
    fillFilters();
    renderSheet();
    if (S.flash.size) {
      const names = S.cands.filter((c) => S.flash.has(c.id)).map((c) => c.name).join("、");
      toast(`已实时同步：${names}`);
    }
  }

  function fillFilters() {
    const fill = (id, values) => {
      const sel = $(id), cur = sel.value, first = sel.options[0].textContent;
      sel.replaceChildren(h("option", { value: "" }, first), ...values.map((v) => h("option", { value: v }, v)));
      sel.value = values.includes(cur) ? cur : "";
    };
    fill("f-position", [...new Set(S.cands.map((c) => c.position).filter(Boolean))]);
    fill("f-stage", STAGES);
    fill("f-status", ["进行中", "已淘汰", "已放弃", "已入职"]);
  }

  function renderSheet() {
    const q = $("f-q").value.trim(), pos = $("f-position").value, stg = $("f-stage").value, sts = $("f-status").value;
    const rows = S.cands.filter((c) => (!pos || c.position === pos) && (!stg || c.stage === stg) && (!sts || c.status === sts)
      && (!q || [c.name, c.interviewer, c.feedback, c.resume_summary, c.channel].some((v) => v && v.includes(q))));
    $("f-count").textContent = `${rows.length} / ${S.cands.length} 人`;
    $("sheet-body").replaceChildren(...rows.map((c) => h("tr", { class: S.flash?.has(c.id) ? "flash" : "", onclick: () => openDrawer(c.id) },
      h("td", { class: "nowrap" }, h("b", {}, c.name)),
      h("td", { class: "nowrap" }, c.position || "—"),
      h("td", { class: "nowrap" }, c.channel || "—"),
      h("td", {}, h("span", { class: "stage" }, h("i", { style: `background:${stageColor(c.stage)}` }), c.stage)),
      h("td", {}, h("span", { class: `status s-${c.status}` }, c.status)),
      h("td", { class: "nowrap" }, c.interviewer || "—"),
      h("td", { class: "nowrap tnum" }, shortTime(c.interview_time) || "—"),
      h("td", {}, matchBadge(c.match_score, c.match_detail)),
      h("td", { class: "fb", title: c.feedback || c.resume_summary || "" }, c.feedback || c.resume_summary || "—"),
      h("td", { class: "nowrap" }, c.hr_owner || "—"),
      h("td", { class: "nowrap tnum" }, shortTime(c.updated_at)))));
  }

  function parseMatch(detail) {
    try { return detail ? JSON.parse(detail) : null; } catch { return null; }
  }

  function matchBadge(score, detail) {
    if (score == null) return "—";
    const m = parseMatch(detail) || {};
    const cls = score >= 75 ? "s-已入职" : score >= 55 ? "s-进行中" : "s-已淘汰";
    return h("span", { class: `status ${cls} tnum`, title: m.level || "" }, `${score} 分`);
  }

  function matchSection(c) {
    const m = parseMatch(c.match_detail);
    if (!m) return null;
    const list = (title, items) => items && items.length ? [h("dt", {}, title), h("dd", {}, items.join("、"))] : [];
    return h("div", { class: "match-box" },
      h("div", { class: "match-head" }, h("b", {}, "🎯 岗位匹配度"), matchBadge(c.match_score, c.match_detail),
        h("span", { class: "msg-meta" }, `${m.level} · ${m.engine === "llm" ? "大模型评估" : "规则评估"}`)),
      h("dl", { class: "kv" }, ...list("命中", m.matched), ...list("缺失", m.missing), ...list("亮点", m.highlights), ...list("风险", m.risks)),
      m.questions?.length ? h("div", {}, h("div", { class: "msg-meta" }, "建议面试问题"),
        h("ol", { class: "steps" }, ...m.questions.map((q) => h("li", {}, q)))) : null);
  }

  async function openDrawer(id) {
    const { candidate: c, events } = await api(`/api/candidates/${id}`);
    const close = () => $("drawer-root").replaceChildren();
    const kv = [["应聘岗位", c.position], ["渠道", c.channel], ["当前阶段", c.stage], ["状态", c.status], ["手机", c.phone],
      ["邮箱", c.email], ["面试官", c.interviewer], ["面试时间", c.interview_time], ["面试评价", c.feedback],
      ["简历摘要", c.resume_summary], ["负责HR", c.hr_owner], ["建档时间", c.created_at]].filter(([, v]) => v);
    const LBL = { match_score: "匹配度", stage: "阶段", status: "状态", position: "岗位", channel: "渠道", interviewer: "面试官", interview_time: "面试时间", feedback: "评价", name: "姓名", resume_summary: "简历摘要", hr_owner: "负责HR", email: "邮箱" };
    $("drawer-root").replaceChildren(
      h("div", { class: "drawer-mask", onclick: close }),
      h("aside", { class: "drawer", role: "dialog", "aria-label": `${c.name} 详情` },
        h("div", { class: "drawer-head" }, h("h2", {}, c.name), h("span", { class: `status s-${c.status}` }, c.status),
          h("button", { class: "btn sm", style: "margin-left:auto", onclick: close }, "关闭")),
        h("div", { class: "drawer-body" },
          h("dl", { class: "kv" }, ...kv.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
          matchSection(c),
          h("h3", { style: "font-size:14px;margin:0 0 10px" }, "变更记录（可追溯到原始消息）"),
          h("div", { class: "timeline" }, ...events.map((e) => h("div", { class: "tl-item" },
            h("div", { class: "tl-time tnum" }, `${e.ts.slice(5, 16)} · ${e.operator || ""} · ${{ llm: "大模型", agent: "智能体", vision: "截图识别", rules: "规则引擎" }[e.engine] || "系统"}${e.source === "wecom" ? " · 企业微信" : ""}`),
            h("div", {}, e.action === "create" ? "建档" : Object.entries(e.changes).filter(([k]) => LBL[k]).map(([k, [o, n]]) => `${LBL[k]}：${o ? o + " → " : ""}${n ?? "清空"}`).join("；")),
            e.raw_text ? h("div", { class: "tl-raw" }, "💬 ", e.raw_text) : null)))))
    );
  }

  /* ======================= 跟进提醒 ======================= */
  function renderAlerts() {
    const a = S.dash.alerts;
    const group = (title, items, sev, render) => h("div", { class: "alert-group" },
      h("h3", {}, title, h("span", { class: "chip tnum" }, items.length)),
      items.length ? items.map((x) => h("div", { class: "alert-item", onclick: () => openDrawer(x.id) }, h("span", { class: "sev " + sev[0] }, sev[1]), ...render(x)))
        : h("div", { class: "empty" }, "暂无"));
    $("alerts").replaceChildren(
      group("面试后未填反馈", a.feedback_missing, ["crit", "漏填反馈"], (x) => [h("b", {}, x.name), `${x.position || ""} · ${x.stage}，请面试官 ${x.interviewer || "—"} 补充评价`, h("span", { class: "when tnum" }, `已过 ${x.hours} 小时`)]),
      group(`超过 ${S.status?.stale_days || 3} 天无进展`, a.stale, ["warn", "超时"], (x) => [h("b", {}, x.name), `${x.position || ""} · ${x.stage}，负责人 ${x.hr_owner || "—"}`, h("span", { class: "when tnum" }, `${x.days} 天未更新`)]),
      group("即将进行的面试（今明两天）", a.upcoming, ["info", "待面试"], (x) => [h("b", {}, x.name), `${x.position || ""} · ${x.stage}，面试官 ${x.interviewer || "待定"}`, h("span", { class: "when tnum" }, shortTime(x.interview_time))]),
    );
  }

  /* ======================= 刷新 & 实时推送 ======================= */
  let refreshing = null;
  async function refreshAll() {
    if (refreshing) return refreshing;
    refreshing = Promise.all([loadMessages(), loadDashboard(), loadCandidates(), loadStatus()])
      .catch((e) => console.error(e)).finally(() => (refreshing = null));
    return refreshing;
  }

  let pendingRefresh = null;
  function connectStream() {
    const es = new EventSource("/api/stream");
    S.es = es;
    S.lastBeat = Date.now();
    es.onmessage = (e) => {
      const data = JSON.parse(e.data || "{}");
      S.lastBeat = Date.now();
      const mine = (data.progress || []).filter((p) => p.operator === OPERATOR).at(-1);
      const typing = document.getElementById("typing");
      if (mine) S.typingText = mine.text;
      if (mine && typing) typing.textContent = mine.text; // 智能体每一步实时显示
      if ((data.kinds || []).every((k) => k === "progress" || k === "ping")) return; // 进度 / 心跳无需刷新数据
      clearTimeout(pendingRefresh);
      pendingRefresh = setTimeout(refreshAll, 150); // 合并短时间内的多次变更
    };
    es.onopen = () => { S.lastBeat = Date.now(); $("live-dot")?.classList.add("live"); };
    es.onerror = () => $("live-dot")?.classList.remove("live");
  }

  // 看门狗：代理 / 网关可能悄悄掐断长连接，超过 40 秒没收到任何事件（含 15 秒一次的心跳）就重连并补一次刷新
  setInterval(() => {
    if (S.es && Date.now() - S.lastBeat > 40000) {
      S.es.close();
      connectStream();
      refreshAll();
    }
  }, 10000);

  /* ======================= 绑定事件 ======================= */
  function bind() {
    $("btn-send").onclick = () => send();
    $("input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    });
    $("btn-upload").onclick = () => $("file").click();
    $("file").onchange = (e) => { const f = e.target.files[0]; if (f) uploadFile(f); e.target.value = ""; };
    $("btn-report").onclick = async () => {
      showTyping(true);
      const r = await api("/api/report", { method: "POST" }).catch((e) => toast(e.message));
      showTyping(false);
      if (r) toast(r.pushed ? "日报已生成，并已推送到企微群" : "日报已生成（在企微群里 @招聘助手 一次，之后即可自动推送到该群）");
      refreshAll();
    };
    $("btn-reset").onclick = async () => {
      if (!confirm("恢复为初始演示数据？当前演示中新增的记录会被清空。")) return;
      await api("/api/reset", { method: "POST" });
      S.prevUpdated = null; S.msgs = [];
      await refreshAll();
      toast("已重置演示数据");
    };
    $("btn-logout").onclick = async () => {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    };
    $("btn-push-alerts").onclick = async () => {
      const r = await api("/api/alerts/push", { method: "POST" }).catch((e) => toast(e.message));
      if (r) toast(r.pushed ? "提醒已推送到企微群" : r.markdown ? "提醒内容已生成（招聘助手还没绑定群，暂未推送）" : "当前没有需要提醒的事项");
    };
    document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
      ["dash", "table", "alerts"].forEach((n) => $("tab-" + n).classList.toggle("hidden", n !== t.dataset.tab));
      if (t.dataset.tab === "dash") Object.values(S.charts).forEach((c) => c.resize());
    }));
    ["f-q", "f-position", "f-stage", "f-status"].forEach((id) => $(id).addEventListener("input", renderSheet));
    window.addEventListener("resize", () => Object.values(S.charts).forEach((c) => c.resize()));
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => loadDashboard());
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("drawer-root").replaceChildren(); });
  }

  async function init() {
    bind();
    renderExamples();
    await refreshAll();
    connectStream();
  }
  init();
})();
