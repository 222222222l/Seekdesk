"use strict";
const $ = (s, root = document) => root.querySelector(s);
const icons = {
  scan: '<path d="M4 9V5a1 1 0 0 1 1-1h4m6 0h4a1 1 0 0 1 1 1v4m0 6v4a1 1 0 0 1-1 1h-4m-6 0H5a1 1 0 0 1-1-1v-4"/><circle cx="12" cy="12" r="3"/><path d="m14 10 4-4"/>',
  layout:
    '<rect x="3" y="3" width="8" height="7" rx="1.4"/><rect x="14" y="3" width="7" height="11" rx="1.4"/><rect x="3" y="13" width="8" height="8" rx="1.4"/><path d="M14 18h7"/>',
  briefcase:
    '<rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12c5 3 13 3 18 0M10 14h4"/>',
  message:
    '<path d="M21 11a8 8 0 0 1-8 8H8l-5 3v-6a9 9 0 1 1 18-5Z"/><path d="M7 10h9m-9 4h6"/>',
  file: '<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8m-8 4h6"/>',
  layers: '<path d="m3 8 9-5 9 5-9 5zM3 12l9 5 9-5M3 16l9 5 9-5"/>',
  sliders:
    '<path d="M3 7h6m4 0h8M3 17h10m4 0h4"/><circle cx="11" cy="7" r="2"/><circle cx="15" cy="17" r="2"/>',
  archive:
    '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v11h14V9m-10 4h6"/>',
  panel: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  target:
    '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  upload: '<path d="M4 15v5h16v-5M12 16V3m-5 5 5-5 5 5"/>',
  search: '<circle cx="10" cy="10" r="6.5"/><path d="m15 15 6 6"/>',
  bell: '<path d="M4 17h16l-2-4V9a6 6 0 0 0-12 0v4zM10 20a2 2 0 0 0 4 0"/>',
  play: '<path d="m7 4 13 8-13 8z"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
};
const icon = (k) =>
  '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">' +
  (icons[k] || "") +
  "</svg>";
document
  .querySelectorAll("[data-icon]")
  .forEach((n) => n.insertAdjacentHTML("afterbegin", icon(n.dataset.icon)));
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        ch
      ],
  );
const titles = {
  overview: ["工作概览", "查看岗位巡查、简历匹配和沟通进度。"],
  jobs: ["岗位机会", "根据简历和岗位原文，核对匹配证据与投递材料。"],
  activity: ["沟通与通知", "查看 HR 会话、简历投递与每次巡查的执行记录。"],
  resume: ["我的简历", "上传用于岗位匹配和邀请投递的简历原件。"],
  sources: ["来源与账号", "登录招聘平台，管理企业招聘板与搜索来源。"],
  settings: ["模型与任务", "设置匹配模型、巡查频率和邮件通知。"],
};
const time = (stamp) =>
  stamp
    ? new Date(stamp * 1000).toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      })
    : "尚未运行";
let state,
  route = "",
  tab = "actions",
  query = "",
  kind = "all",
  page = 1,
  timer,
  refreshing = false;
const view = $("#view");
const badge = (label, cls = "") =>
  '<span class="badge ' + cls + '">' + esc(label) + "</span>";
const button = (label, act, i = "", cls = "") =>
  '<button type="button" class="btn ' +
  cls +
  '" data-act="' +
  act +
  '">' +
  (i ? icon(i) : "") +
  esc(label) +
  "</button>";
const link = (label, dest) =>
  '<a href="#' + dest + '">' + esc(label) + " →</a>";
const hdr = (title, side = "") =>
  '<div class="section-head"><h2>' + esc(title) + "</h2>" + side + "</div>";
function toast(text) {
  const n = $("#toast");
  n.hidden = false;
  n.textContent = text;
  clearTimeout(timer);
  timer = setTimeout(() => (n.hidden = true), 5000);
}
async function api(path, body) {
  const res = await fetch(
    "/api/" + path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await res.json();
  if (!res.ok) throw Error(data.error || "请求失败");
  return data;
}
function empty(i, title, desc, action = "") {
  return (
    '<div class="empty"><span class="tint">' +
    icon(i) +
    "</span><b>" +
    esc(title) +
    "</b><p>" +
    esc(desc) +
    "</p>" +
    action +
    "</div>"
  );
}
function sourceName(s) {
  return (
    state.platforms[s] ||
    (s.startsWith("greenhouse:")
      ? "Greenhouse"
      : s.startsWith("lever:")
        ? "Lever"
        : s)
  );
}
function leadLabel(mode) {
  return mode === "index"
    ? "搜索索引 · 待核实"
    : mode === "listing"
      ? "职位卡片 · 待核实"
      : "";
}
const matchLevels = {
  high: "高匹配",
  medium: "中匹配",
  low: "低匹配",
};
function matchLevel(score) {
  return score >= 80 ? "high" : score >= 50 ? "medium" : "low";
}
function scoreBadge(score) {
  const level = matchLevel(score);
  return badge(
    matchLevels[level] + " · " + score + " 分",
    level === "high" ? "good" : level === "medium" ? "blue" : "",
  );
}
function jobRow(j) {
  const status = [
    !j.active && "已下线",
    leadLabel(j.mode),
    j.mode === "preliminary" && "本地初评",
    j.pref_match === 0 && "偏好外",
  ].filter(Boolean);
  return (
    '<button class="job" data-job="' +
    esc(j.id) +
    '"><span class="company-avatar">' +
    esc(j.company.slice(0, 2).toUpperCase()) +
    '</span><span class="info"><strong>' +
    esc(j.title) +
    "</strong><small>" +
    esc(
      [j.company, j.location, sourceName(j.source), ...status]
        .filter(Boolean)
        .join(" · "),
    ) +
    "</small></span>" +
    (j.current ? scoreBadge(j.score) : badge("待重新匹配")) +
    "</button>"
  );
}
function runButton() {
  if (state.busy) return button("暂停巡查", "pause", "clock");
  return (
    '<button type="button" class="btn primary" data-act="run" ' +
    (state.busy ? "disabled" : "") +
    ">" +
    icon("play") +
    (state.busy ? "巡查中" : "立即巡查") +
    "</button>"
  );
}
function metric(i, title, n, color) {
  return (
    '<div class="metric"><div class="metric-top"><span class="tint ' +
    color +
    '">' +
    icon(i) +
    "</span>" +
    title +
    "</div><strong>" +
    n +
    "</strong></div>"
  );
}
function step(i, title, desc, to) {
  return (
    '<button class="step" data-go="' +
    to +
    '"><span>' +
    icon(i) +
    "</span><span><b>" +
    esc(title) +
    "</b><small>" +
    esc(desc) +
    "</small></span><em>›</em></button>"
  );
}
function sourceCount() {
  const c = state.settings;
  return (
    c.platforms.length +
    c.boards.split(/\s+/).filter(Boolean).length +
    c.sites.split(/\s+/).filter(Boolean).length +
    Number(c.brave_key_set) +
    Number(c.tavily_key_set) +
    Number(Boolean(state.search_plugins?.searxng?.enabled))
  );
}
function overview() {
  const c = state.settings,
    active = state.jobs.filter((j) => j.active),
    good = active.filter(
      (j) => j.current && j.pref_match && j.eligible && j.score >= c.threshold,
    );
  const a = state.contacted;
  const hero =
    '<div class="hero"><div class="hero-copy"><span class="hero-status">' +
    (state.busy ? "正在巡查" : c.scheduled ? "定时巡查已启用" : "巡查待命") +
    "</span><h2>岗位巡查雷达</h2><p>" +
    esc(
      state.busy
        ? state.phase
        : state.profile
          ? c.scheduled
            ? "下一次巡查 " + time(state.due)
            : "根据当前简历，从已启用的来源发现岗位。"
          : "上传简历，开始按你的经历和技能发现岗位。",
    ) +
    "</p>" +
    (state.profile ? runButton() : button("上传我的简历", "resume", "upload")) +
    '</div><div class="radar" aria-hidden="true"><div class="scope"><i></i><b class="blip a"></b><b class="blip b"></b><b class="blip c"></b></div></div></div>';
  const start =
    step(
      "file",
      "求职简历",
      state.profile ? state.profile.name : "上传 PDF、Word 或文字简历",
      "resume",
    ) +
    step("layers", "岗位来源", sourceCount() + " 个已配置来源", "sources") +
    step(
      "clock",
      "定时任务",
      c.scheduled ? "每 " + c.interval + " 分钟巡查一次" : "尚未启用定时巡查",
      "settings",
    );
  const latest = state.runs[0];
  return (
    hero +
    '<div class="stats">' +
    metric("briefcase", "已发现岗位", active.length, "") +
    metric("target", "达到匹配门槛", good.length, "teal") +
    metric("message", "已联系 HR", a, "amber") +
    '</div><div class="columns"><section class="panel">' +
    hdr("匹配机会", link("全部岗位", "jobs")) +
    (active.length
      ? active.slice(0, 5).map(jobRow).join("")
      : empty(
          "search",
          "还没有岗位机会",
          state.profile
            ? "运行一次巡查，查看匹配岗位和原文证据。"
            : "首次巡查完成后，这里会显示匹配度最高的岗位。",
          state.profile ? runButton() : link("设置岗位来源", "sources"),
        )) +
    '</section><div class="stack"><section class="panel">' +
    hdr("工作台设置") +
    '<div class="steps">' +
    start +
    "</div></section>" +
    (latest
      ? '<section class="panel">' +
        hdr("最近巡查", link("记录", "activity")) +
        badge(latest.status) +
        " <small>" +
        time(latest.start) +
        "</small></section>"
      : "") +
    "</div></div>"
  );
}
function jobs() {
  const visible = state.jobs.filter(
    (j) =>
      (kind === "all" ||
        (kind in matchLevels && j.current && matchLevel(j.score) === kind) ||
        (kind === "match"
          ? j.active &&
            j.current &&
            j.pref_match &&
            j.eligible &&
            j.score >= state.settings.threshold
          : kind === "inactive" && !j.active)) &&
      [j.title, j.company, j.location, j.source, sourceName(j.source)]
        .join(" ")
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  const pages = Math.max(1, Math.ceil(visible.length / 20));
  page = Math.min(page, pages);
  return (
    '<section class="panel">' +
    hdr("岗位清单", state.profile ? runButton() : link("上传简历", "resume")) +
    '<p class="hint">按当前简历的匹配分从高到低排列。待核实职位根据已有标题和摘要初评。</p>' +
    '<div class="filter"><input id="search" aria-label="搜索岗位" placeholder="搜索职位、公司或地区" value="' +
    esc(query) +
    '"><select id="job-filter" aria-label="匹配筛选">' +
    [
      ["all", "全部岗位"],
      ["high", "高匹配（80–100 分）"],
      ["medium", "中匹配（50–79 分）"],
      ["low", "低匹配（0–49 分）"],
      ["match", "达到门槛"],
      ["inactive", "已下线"],
    ]
      .map(
        ([k, t]) =>
          '<option value="' +
          k +
          '" ' +
          (kind === k ? "selected" : "") +
          ">" +
          t +
          "</option>",
      )
      .join("") +
    "</select></div>" +
    (visible.length
      ? visible
          .slice((page - 1) * 20, page * 20)
          .map(jobRow)
          .join("")
      : empty(
          "briefcase",
          "没有符合条件的岗位",
          "可以调整筛选，或启动下一轮巡查。",
        )) +
    (pages > 1
      ? '<div class="pagination">' +
        button("上一页", "prev") +
        page +
        " / " +
        pages +
        button("下一页", "next") +
        "</div>"
      : "") +
    "</section>"
  );
}
function activity() {
  const actions = state.actions
    .map(
      (a) =>
        '<article class="feed"><div>' +
        badge(a.state, a.state === "已完成" ? "good" : "warn") +
        "<b>" +
        esc(a.kind) +
        "</b></div><p>" +
        esc(a.company + " · " + a.title) +
        " · " +
        time(a.updated) +
        "</p><p>" +
        esc(a.detail) +
        '</p><button class="linkbtn" data-job="' +
        a.job +
        '">查看岗位与材料建议 →</button>' +
        (a.state === "待核对"
          ? '<button class="linkbtn" data-retry="' +
            a.id +
            '">已核实未发，重新排队</button>'
          : "") +
        "</article>",
    )
    .join("");
  const expandedRuns = new Set(
    Array.from(
      view.querySelectorAll("details[data-run][open]"),
      (el) => el.dataset.run,
    ),
  );
  const runs = state.runs
    .map(
      (r) =>
        '<details class="feed" data-run="' +
        r.id +
        '" ' +
        (expandedRuns.has(String(r.id)) ? "open" : "") +
        "><summary>" +
        badge(r.status, r.status === "完成" ? "good" : "warn") +
        "<b>" +
        esc(r.reason) +
        "巡查</b> · " +
        time(r.start) +
        "</summary>" +
        (!r.end && state.phase
          ? '<p class="hint">' + esc(state.phase) + "</p>"
          : "") +
        "<ul>" +
        r.reports
          .map(
            (s) =>
              "<li>" +
              esc(sourceName(s.source)) +
              " · " +
              esc(
                s.error ||
                  "读取 " +
                    s.count +
                    " 个岗位" +
                    (s.evaluated == null
                      ? ""
                      : "，已评估 " + s.evaluated + " 个") +
                    (s.leads ? "，待核实线索 " + s.leads + " 个" : "") +
                    (s.filtered
                      ? (s.retained
                          ? "，已保留偏好外岗位 "
                          : "，地点或关键词筛除 ") +
                        s.filtered +
                        " 个"
                      : ""),
              ) +
              (s.error && state.platforms[s.source]
                ? " " +
                  button("在专属浏览器核对搜索页", "searchpage-" + s.source)
                : "") +
              "</li>",
          )
          .join("") +
        "</ul></details>",
    )
    .join("");
  return (
    '<section class="panel"><div class="tabline">' +
    [
      ["actions", "沟通与通知"],
      ["runs", "巡查记录"],
    ]
      .map(
        ([k, t]) =>
          '<button class="tab ' +
          (k === tab ? "active" : "") +
          '" data-tab="' +
          k +
          '">' +
          t +
          "</button>",
      )
      .join("") +
    "</div>" +
    (tab === "actions"
      ? actions ||
        empty(
          "message",
          "暂无沟通记录",
          "打招呼、简历邀请、联系方式请求和材料通知会显示在这里。",
        )
      : runs || empty("clock", "尚未运行巡查", "可在工作概览发起首次巡查。")) +
    "</section>"
  );
}
function resume() {
  const p = state.profile;
  return (
    '<div class="stack"><section class="panel">' +
    hdr(p ? "当前简历" : "上传求职简历", p ? badge("已解析", "good") : "") +
    (p
      ? "<b>" +
        esc(p.name) +
        '</b><p class="hint">' +
        Math.ceil(p.bytes / 1024) +
        " KB · " +
        time(p.added) +
        '</p><div class="preview">' +
        esc(p.preview) +
        '</div><h3 class="section-title">更换简历</h3>'
      : "") +
    '<label class="upload" id="drop"><span class="tint">' +
    icon("upload") +
    '</span><b>选择或拖入简历文件</b><span class="hint">PDF、DOCX、TXT、MD · 最大 6 MB</span><input type="file" id="upload" accept=".pdf,.docx,.txt,.md"></label><p class="hint">工作台保留原件用于受邀投递。选用外部模型时，简历文本会发送给所选模型。</p></section><section class="panel">' +
    hdr("求职偏好") +
    '<form data-form="prefs"><div class="formgrid">' +
    field("keywords", "职位关键词", "text", "如 数据分析、产品经理") +
    field("location", "工作地点", "text", "如 上海，留空不限地区") +
    '</div><p class="hint">多个关键词用空格或逗号分隔，命中其一即可。留空时由模型从简历提取搜索词。</p>' +
    save("保存偏好") +
    "</form></section></div>"
  );
}
function field(k, label, type = "text", placeholder = "", wide = false) {
  const c = state.settings,
    v = type === "password" ? "" : c[k];
  const attrs =
    'name="' +
    k +
    '" placeholder="' +
    esc(
      type === "password" && c[k + "_set"] ? "已保存，留空保留" : placeholder,
    ) +
    '"';
  return (
    '<label class="field ' +
    (wide ? "full" : "") +
    '">' +
    esc(label) +
    (type === "textarea"
      ? '<textarea rows="3" ' + attrs + ">" + esc(v) + "</textarea>"
      : '<input type="' +
        type +
        '" ' +
        attrs +
        ' value="' +
        esc(v) +
        '" ' +
        (type === "password" ? 'autocomplete="new-password"' : "") +
        ">") +
    "</label>"
  );
}
const save = (label) =>
  '<div class="actions"><button class="btn primary" type="submit">' +
  esc(label) +
  "</button></div>";
const check = (k, label) =>
  '<label class="check"><input type="checkbox" name="' +
  k +
  '" ' +
  (state.settings[k] ? "checked" : "") +
  ">" +
  esc(label) +
  "</label>";
function searchPluginPanel() {
  const p = state.search_plugins?.searxng;
  if (!p) return "";
  const controls = p.mode
    ? '<p class="hint">' +
      esc(p.endpoint || p.project) +
      '</p><div class="actions">' +
      button("测试搜索", "sx-test") +
      button(
        p.enabled ? "停用巡查搜索" : "启用巡查搜索",
        p.enabled ? "sx-disable" : "sx-enable",
      ) +
      (p.mode !== "url"
        ? button("启动并启用", "sx-start") + button("停止服务", "sx-stop")
        : "") +
      button(p.mode === "managed" ? "卸载插件" : "移除接入", "sx-remove") +
      "</div>"
    : '<div class="formgrid"><label class="field">接入方式<select id="sx-mode"><option value="managed">安装独立实例（Docker）</option><option value="project" ' +
      (p.suggested_project ? "selected" : "") +
      '>导入本地 Compose 项目</option><option value="url">连接已运行的实例</option></select></label>' +
      '<label class="field" id="sx-project-field" ' +
      (p.suggested_project ? "" : "hidden") +
      '>本地项目目录<input id="sx-project" placeholder="包含 compose.yaml 的目录" value="' +
      esc(p.suggested_project) +
      '"></label>' +
      '<label class="field" id="sx-proxy-field" ' +
      (p.suggested_project ? "hidden" : "") +
      '>搜索出口代理（选填）<input id="sx-proxy" placeholder="容器可访问的 HTTP 或 SOCKS5h 地址"></label>' +
      '<label class="field" id="sx-url-field" hidden>本机实例地址<input id="sx-url" placeholder="http://127.0.0.1:8888"></label></div><div class="actions">' +
      button("安装并接入", "sx-install", "layers") +
      "</div>";
  return (
    '<section class="panel" id="search-plugin">' +
    hdr(
      "本地 SearXNG",
      badge(
        p.enabled ? "已加入巡查" : p.mode ? "已配置" : "可选插件",
        p.enabled ? "good" : "",
      ),
    ) +
    '<p class="hint">通过本机元搜索服务发现招聘官网。导入项目会启动其中的 searxng 服务。</p>' +
    controls +
    '<p class="hint" role="status" id="sx-note">' +
    esc(p.note) +
    "</p></section>"
  );
}
function sourcesPage() {
  const c = state.settings;
  return (
    '<form class="stack" data-form="sources"><section class="panel">' +
    hdr("招聘平台") +
    '<p class="hint">首次登录后关闭窗口，再勾选加入巡查。如果官网要求验证，可点「可见窗口复查」，在三分钟内完成官网提示并保持搜索页打开。复查只读取岗位。</p><div class="cards">' +
    Object.entries(state.platforms)
      .map(
        ([k, n]) =>
          '<div class="platform"><strong>' +
          esc(n) +
          "</strong><p>" +
          esc(state.logins[k] || "尚未打开登录窗口") +
          '</p><label class="check"><input type="checkbox" name="platform" value="' +
          k +
          '" ' +
          (c.platforms.includes(k) ? "checked" : "") +
          ">加入巡查</label>" +
          button("打开平台登录", "login-" + k, "arrow") +
          button("可见窗口复查", "preview-" + k) +
          "</div>",
      )
      .join("") +
    "</div></section>" +
    '<section class="panel">' +
    hdr("企业官网与招聘板") +
    '<div class="formgrid">' +
    field("boards", "公开招聘板", "textarea", "greenhouse:gitlab", true) +
    field(
      "sites",
      "企业招聘页 URL",
      "textarea",
      "每行一个招聘官网或职位详情 URL",
      true,
    ) +
    '</div><p class="hint">招聘板填写 greenhouse:企业标识 或 lever:企业标识，每行一个。官网支持 JobPosting 结构化职位。</p></section>' +
    '<section class="panel">' +
    hdr("聚合搜索引擎") +
    '<div class="formgrid">' +
    field("brave_key", "Brave Search API Key", "password") +
    field("tavily_key", "Tavily API Key", "password") +
    '</div><p class="hint">填写后加入巡查。引擎接收岗位搜索词，工作台读取结果页面中的职位原文。</p>' +
    clearKeys(["brave_key", "tavily_key"]) +
    "</section>" +
    save("保存岗位来源") +
    "</form>" +
    searchPluginPanel()
  );
}
function clearKeys(keys) {
  return keys
    .filter((k) => state.settings[k + "_set"])
    .map(
      (k) =>
        '<label class="check"><input type="checkbox" name="clear_' +
        k +
        '">移除已保存的 ' +
        esc(
          {
            brave_key: "Brave 密钥",
            tavily_key: "Tavily 密钥",
            api_key: "模型密钥",
            smtp_password: "邮箱授权码",
          }[k],
        ) +
        "</label>",
    )
    .join("");
}
function settings() {
  return (
    '<div class="stack"><section class="panel" id="backend-panel">正在扫描本机后端…</section>' +
    '<form class="stack" data-form="settings"><section class="panel">' +
    hdr("巡查与投递") +
    '<div class="formgrid">' +
    field("interval", "巡查间隔（分钟）", "number") +
    field("threshold", "匹配门槛（0–100）", "number") +
    field("daily_limit", "每日最多新联系 HR 数", "number") +
    '</div><div class="checks section-title">' +
    check("scheduled", "启用定时巡查，工作台服务运行期间生效") +
    check("outreach", "向达到门槛且有简历证据的平台岗位自动打招呼") +
    check(
      "resume_auto",
      "收到已联系 HR 的简历邀请时，发送首次联系时关联的简历原件",
    ) +
    "</div></section>" +
    '<section class="panel">' +
    hdr("求职提醒") +
    '<p class="hint">收到交换联系方式请求，或发现需要准备材料的匹配官网岗位时提醒你。</p>' +
    (state.desktop_available
      ? '<div class="actions">' +
        check("windows_notifications", "Windows 桌面通知") +
        button("试弹一条通知", "test-desktop") +
        '</div><p class="hint">无需邮箱，关闭网页后也能接收。请保持本机工作台服务运行。</p>'
      : "") +
    check("email_enabled", "邮件提醒") +
    '<fieldset id="mail-options" class="mail-options" ' +
    (state.settings.email_enabled ? "" : "hidden disabled") +
    ">" +
    '<label class="field">发件设置<select name="mail_mode"><option value="auto">快捷设置（QQ、163、126 邮箱）</option><option value="custom" ' +
    (state.settings.mail_mode === "custom" ? "selected" : "") +
    ">自定义 SMTP</option></select></label>" +
    '<p id="mail-guide" class="hint" ' +
    (state.settings.mail_mode === "custom" ? "hidden" : "") +
    ">使用自己的邮箱给自己发提醒。在邮箱官网开启 SMTP 服务并生成客户端授权码。</p>" +
    '<div class="formgrid">' +
    field("email", "接收通知的邮箱", "email") +
    field("smtp_password", "邮箱授权码", "password") +
    "</div>" +
    '<fieldset id="mail-custom" class="mail-options formgrid" ' +
    (state.settings.mail_mode === "custom" ? "" : "hidden disabled") +
    ">" +
    field("smtp_user", "发件邮箱 / SMTP 账号", "email") +
    field("smtp_host", "SMTP 服务器", "text", "smtp.example.com") +
    field("smtp_port", "SMTP 端口", "number") +
    "</fieldset>" +
    clearKeys(["smtp_password"]) +
    "</fieldset></section>" +
    save("保存模型与任务") +
    "</form></div>"
  );
}
function render() {
  route = location.hash.slice(1);
  if (!Object.hasOwn(titles, route)) route = "overview";
  $("#crumb").textContent = $("#heading").textContent = titles[route][0];
  $("#subtitle").textContent = titles[route][1];
  $("#dateline").textContent = new Date().toLocaleDateString("zh-CN", {
    month: "long",
    day: "numeric",
    weekday: "long",
  });
  document
    .querySelectorAll("nav a")
    .forEach((a) => a.classList.toggle("active", a.hash === "#" + route));
  view.innerHTML = {
    overview,
    jobs,
    activity,
    resume,
    sources: sourcesPage,
    settings,
  }[route]();
  if (route === "settings")
    BackendPicker.mount($("#backend-panel"), {
      api,
      esc,
      toast,
      active: state.settings.provider,
      onUse: () => refresh(false),
    });
}
async function refresh(draw = true) {
  state = await api("state");
  $("#online").textContent = "本地服务在线";
  $("#online").classList.remove("off");
  if (draw) render();
}
async function detail(id) {
  const j = await api("jobs/" + encodeURIComponent(id)),
    r = j.rating;
  $("#detail-body").innerHTML =
    "<h2>" +
    esc(j.title) +
    '</h2><p class="muted">' +
    esc(
      [j.company, j.location, sourceName(j.source)].filter(Boolean).join(" · "),
    ) +
    "</p>" +
    scoreBadge(r.score) +
    " " +
    badge(
      leadLabel(r.mode)
        ? leadLabel(r.mode) + " · 摘要初评"
        : r.mode === "local"
          ? "关键词匹配"
          : r.mode === "preliminary"
            ? "本地初评"
            : r.mode + " 模型",
    ) +
    (r.pref_match === false ? " " + badge("地点或关键词偏好外") : "") +
    (leadLabel(r.mode)
      ? [
          ["初评依据", r.reason],
          ["简历依据", r.evidence || "未找到可引用的简历原文"],
          [r.mode === "index" ? "搜索结果摘要" : "职位卡片内容", j.description],
        ]
      : [
          ["匹配判断", r.reason],
          ["简历依据", r.evidence || "未找到可引用的简历原文"],
          ["岗位依据", r.requirement || "暂无"],
          ["材料准备", r.materials],
          ["岗位原文", j.description],
        ]
    )
      .map(
        ([t, v]) =>
          '<h3 class="section-title">' +
          t +
          '</h3><div class="prose">' +
          esc(v) +
          "</div>",
      )
      .join("") +
    '<div class="actions"><a class="btn primary" target="_blank" rel="noopener noreferrer" href="' +
    esc(j.url) +
    '">打开原始岗位 ↗</a></div>';
  $("#detail").showModal();
}
async function upload(file) {
  if (!file) return;
  if (file.size > 6 * 1024 * 1024) throw Error("简历大小上限为 6 MB");
  const encoded = await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(Error("文件读取失败"));
    r.onload = () => resolve(r.result.split(",")[1]);
    r.readAsDataURL(file);
  });
  await api("resume", { name: file.name, data: encoded });
  await refresh();
  toast("简历已解析，可以开始巡查");
}
view.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.retry) {
      if (
        !window.confirm(
          "请先核对原平台或发件箱。确认上次未发送，并在下轮巡查中重试？",
        )
      )
        return;
      await api("retry", { id: b.dataset.retry });
      await refresh();
      toast("已重新排队");
      return;
    }
    if (b.dataset.go) {
      location.hash = b.dataset.go;
      return;
    }
    if (b.dataset.job) {
      await detail(b.dataset.job);
      return;
    }
    if (b.dataset.tab) {
      tab = b.dataset.tab;
      render();
      return;
    }
    const a = b.dataset.act;
    if (a === "test-desktop") {
      b.disabled = true;
      try {
        const r = await api("notifications/test-desktop", {});
        toast(r.note);
      } finally {
        b.disabled = false;
      }
      return;
    }
    if (a?.startsWith("sx-")) {
      const action = a.slice(3),
        panel = b.closest("section");
      if (
        action === "remove" &&
        !confirm(
          state.search_plugins.searxng.mode === "managed"
            ? "移除工作台创建的容器？配置和缓存会保留。"
            : "移除此实例的工作台接入？原项目会保留。",
        )
      )
        return;
      const data = { action };
      if (action === "install") {
        data.mode = $("#sx-mode").value;
        if (data.mode === "project") data.project = $("#sx-project").value;
        if (data.mode === "managed") data.proxy = $("#sx-proxy").value;
        if (data.mode === "url") data.endpoint = $("#sx-url").value;
      }
      panel.querySelectorAll("button").forEach((el) => (el.disabled = true));
      $("#sx-note").textContent = "正在处理，请稍候…";
      try {
        const r = await api("plugins/searxng", data);
        toast(r.note);
      } finally {
        await refresh(false);
        if (route === "sources" && $("#search-plugin"))
          $("#search-plugin").outerHTML = searchPluginPanel();
      }
      return;
    }
    if (a === "resume") {
      location.hash = "resume";
    }
    if (a === "run") {
      b.disabled = true;
      await api("run", {});
      await refresh();
      toast("巡查已启动");
    }
    if (a === "pause") {
      await api("pause", {});
      await refresh();
      toast("定时任务已暂停，当前操作结束后停止本轮巡查");
    }
    if (a?.startsWith("login-")) {
      await api("login", { platform: a.slice(6) });
      toast("正在打开专属登录窗口，请留意平台卡片上的状态");
    }
    if (a?.startsWith("preview-")) {
      await api("run", { platform: a.slice(8), interactive: true });
      tab = "runs";
      location.hash = "activity";
      await refresh();
      toast("正在打开官网搜索窗口，完成验证后请保持页面打开，等待读取");
    }
    if (a?.startsWith("searchpage-")) {
      await api("login", { platform: a.slice(11), purpose: "search" });
      toast("已请求打开本次官网搜索页，核对后关闭窗口即可再次复查");
    }
    if (a === "prev" || a === "next") {
      page = Math.max(1, page + (a === "next" ? 1 : -1));
      render();
    }
  } catch (err) {
    b.disabled = false;
    toast(err.message);
  }
});
view.addEventListener("input", (e) => {
  if (e.target.id === "search") {
    query = e.target.value;
    page = 1;
    const pos = e.target.selectionStart;
    render();
    const input = $("#search");
    input.focus();
    input.setSelectionRange(pos, pos);
  }
});
view.addEventListener("change", (e) => {
  if (e.target.name === "email_enabled") {
    const options = $("#mail-options");
    options.disabled = options.hidden = !e.target.checked;
  }
  if (e.target.name === "mail_mode") {
    const custom = e.target.value === "custom";
    $("#mail-custom").disabled = $("#mail-custom").hidden = !custom;
    $("#mail-guide").hidden = custom;
  }
  if (e.target.id === "sx-mode") {
    $("#sx-project-field").hidden = e.target.value !== "project";
    $("#sx-proxy-field").hidden = e.target.value !== "managed";
    $("#sx-url-field").hidden = e.target.value !== "url";
  }
  if (e.target.id === "upload")
    upload(e.target.files[0]).catch((err) => toast(err.message));
  if (e.target.id === "job-filter") {
    kind = e.target.value;
    page = 1;
    render();
  }
});
for (const name of ["dragover", "dragleave", "drop"])
  view.addEventListener(name, (e) => {
    const drop = e.target.closest("#drop");
    if (!drop) return;
    e.preventDefault();
    drop.classList.toggle("over", name === "dragover");
    if (name === "drop")
      upload(e.dataTransfer.files[0]).catch((err) => toast(err.message));
  });
view.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  if (!form.dataset.form) return;
  const data = {};
  for (const input of form.elements) {
    if (
      !input.name ||
      input.matches(":disabled") ||
      input.name.startsWith("clear_") ||
      input.name === "platform"
    )
      continue;
    if (input.type === "password" && !input.value) continue;
    data[input.name] =
      input.type === "checkbox"
        ? input.checked
        : input.type === "number"
          ? Number(input.value)
          : input.value;
  }
  for (const el of form.querySelectorAll(
    "[name^=clear_]:checked:not(:disabled)",
  ))
    data[el.name.slice(6)] = "";
  if (form.dataset.form === "sources")
    data.platforms = [...form.querySelectorAll("[name=platform]:checked")].map(
      (v) => v.value,
    );
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    await api("settings", data);
    await refresh();
    toast("设置已保存");
  } catch (err) {
    submit.disabled = false;
    toast(err.message);
  }
});
$("#close-detail").onclick = () => $("#detail").close();
window.addEventListener("hashchange", () => {
  if (state) {
    render();
    window.scrollTo(0, 0);
  }
});
refresh().catch((e) => {
  view.textContent = "无法读取工作台。请通过本机启动程序打开。";
  toast(e.message);
});
setInterval(async () => {
  if (refreshing || document.hidden || $("#detail").open) return;
  refreshing = true;
  try {
    const draw =
      ["overview", "activity"].includes(route) ||
      (route === "jobs" && state?.busy && !$("#search")?.matches(":focus"));
    await refresh(draw);
    if (route === "sources") {
      for (const [key, label] of Object.entries(state.logins)) {
        const b = view.querySelector('[data-act="login-' + key + '"]');
        if (b) b.closest(".platform").querySelector("p").textContent = label;
      }
    }
  } catch {
    $("#online").textContent = "服务离线";
    $("#online").classList.add("off");
  } finally {
    refreshing = false;
  }
}, 3500);
