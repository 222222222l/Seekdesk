"use strict";

window.BackendPicker = {
  async mount(panel, { api, esc, toast, active, onUse }) {
    let catalog,
      current = active,
      selected = active,
      generation = 0;
    const cache = new Map(),
      drafts = new Map();
    const field = (id) => panel.querySelector("#" + id);
    const options = (ids) =>
      ids
        .map((id) => '<option value="' + esc(id) + '">' + esc(id) + "</option>")
        .join("");
    function draft() {
      const d = {
        provider: selected,
        model: field("backend-model")?.value.trim() || "",
      };
      if (catalog[selected].kind === "api") {
        d.api_url = field("backend-url").value.trim();
        const key = field("backend-key").value;
        if (key || field("backend-clear")?.checked) d.api_key = key;
      }
      return d;
    }
    function controls() {
      ++generation;
      const item = catalog[selected],
        d = drafts.get(selected) || {};
      const base = d.api_url ?? (item.saved_url || item.base || "");
      const model = d.model ?? item.model;
      const saved = cache.get(JSON.stringify([selected, base])) || [];
      const isApi = item.kind === "api",
        isLocal = item.kind === "local";
      field("backend-fields").innerHTML =
        (isLocal
          ? ""
          : '<label class="field">可选模型<select id="backend-list"><option value="">选择扫描结果</option>' +
            options(saved) +
            "</select></label>" +
            '<label class="field">模型 ID<input id="backend-model" value="' +
            esc(model) +
            '" placeholder="' +
            (isApi ? "可手填供应商模型 ID" : "留空使用 CLI 默认模型") +
            '"></label>') +
        (isApi
          ? '<label class="field">API 基址<input type="url" id="backend-url" value="' +
            esc(base) +
            '"></label>' +
            '<label class="field">API Key<input id="backend-key" type="password" autocomplete="new-password" placeholder="' +
            (item.key_set
              ? "已保存，留空沿用同一基址的密钥"
              : "输入此渠道的密钥，本地服务可留空") +
            '"></label>' +
            (item.key_set
              ? '<label class="check"><input id="backend-clear" type="checkbox">移除此渠道密钥</label>'
              : "")
          : "");
      if (isApi && d.api_key) field("backend-key").value = d.api_key;
      field("backend-scan").hidden = isLocal;
      field("backend-note").textContent =
        item.kind === "cli"
          ? item.installed
            ? "本机已检测到 CLI"
            : "本机尚未安装此 CLI"
          : isApi
            ? "点击扫描会向所填接口读取目录。连接测试发送一次简短的测试请求。"
            : "使用本地关键词匹配，无需调用模型。";
      field("backend-use").disabled = item.kind === "cli" && !item.installed;
      if (!isLocal && saved.includes(model))
        field("backend-list").value = model;
    }
    async function scan() {
      const d = draft(),
        id = ++generation,
        target = selected,
        note = field("backend-note");
      note.textContent = "正在读取模型目录…";
      try {
        const r = await api("models/scan", d);
        if (id !== generation || !panel.isConnected) return;
        cache.set(JSON.stringify([target, d.api_url || ""]), r.models);
        field("backend-list").innerHTML =
          '<option value="">选择扫描结果</option>' + options(r.models);
        if (r.models.includes(field("backend-model").value))
          field("backend-list").value = field("backend-model").value;
        note.textContent = r.note + " · " + r.models.length + " 个模型";
      } catch (e) {
        if (id === generation)
          note.textContent = e.message + "。可以手填模型 ID。";
      }
    }
    async function load() {
      catalog = await api("models");
      if (!panel.isConnected) return;
      const groups = [
        ["local", "本地"],
        ["cli", "Agent CLI"],
        ["api", "模型 API"],
      ];
      const menu = groups
        .map(
          ([kind, label]) =>
            '<optgroup label="' +
            label +
            '">' +
            Object.entries(catalog)
              .filter(([, v]) => v.kind === kind)
              .map(
                ([k, v]) =>
                  '<option value="' +
                  esc(k) +
                  '" ' +
                  (k === selected ? "selected" : "") +
                  ">" +
                  esc(v.label) +
                  (kind === "cli"
                    ? v.installed
                      ? " · 已安装"
                      : " · 未检测到"
                    : "") +
                  "</option>",
              )
              .join("") +
            "</optgroup>",
        )
        .join("");
      panel.innerHTML =
        '<div class="section-head"><h2>匹配模型</h2><span class="badge blue" id="backend-active"></span></div>' +
        '<label class="field">运行后端<select id="backend-provider">' +
        menu +
        '</select></label><div class="formgrid section-title" id="backend-fields"></div>' +
        '<p class="hint" id="backend-note" role="status"></p><div class="actions"><button class="btn" type="button" id="backend-scan">扫描模型</button>' +
        '<button class="btn" type="button" id="backend-test">测试连接</button><button class="btn primary" type="button" id="backend-use">使用此后端和模型</button></div>';
      field("backend-active").textContent =
        "当前 · " +
        catalog[current].label +
        (catalog[current].model ? " / " + catalog[current].model : "");
      controls();
      field("backend-provider").onchange = (e) => {
        drafts.set(selected, draft());
        selected = e.target.value;
        controls();
        if (catalog[selected].kind === "cli" && catalog[selected].installed)
          scan();
      };
      field("backend-fields").onchange = (e) => {
        if (e.target.id === "backend-list" && e.target.value)
          field("backend-model").value = e.target.value;
        if (e.target.id === "backend-url") {
          ++generation;
          field("backend-list").innerHTML =
            '<option value="">请重新扫描此基址</option>';
        }
      };
      field("backend-scan").onclick = scan;
      for (const action of ["use", "test"])
        field("backend-" + action).onclick = async (e) => {
          const d = draft(),
            btn = e.currentTarget;
          btn.disabled = true;
          try {
            const result = await api("models/" + action, d);
            if (action === "test") {
              if (d.provider === selected)
                field("backend-note").textContent = result.note;
              toast(result.note);
            } else {
              current = d.provider;
              drafts.delete(current);
              await onUse();
              toast("模型已切换，下次巡查生效");
              await load();
            }
          } catch (err) {
            toast(err.message);
          } finally {
            btn.disabled = false;
          }
        };
      if (catalog[selected].kind === "cli" && catalog[selected].installed)
        scan();
    }
    try {
      await load();
    } catch (e) {
      panel.textContent = "模型目录读取失败：" + e.message;
    }
  },
};
