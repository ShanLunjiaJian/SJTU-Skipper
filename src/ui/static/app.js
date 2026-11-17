(() => {
  "use strict";

  const state = {
    ui: null,
    activeTab: { left: null, right: null },
    blockTabs: {},
    pageKey: null,
    playerCache: {},
    paletteItems: [],
    paletteIndex: 0,
    loading: null,
    navigationVersion: 0,
    navigationPending: 0,
    navigationQueue: Promise.resolve(),
    lastLoad: 0,
    ws: null,
    wsRetry: 0,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k.startsWith("on") && typeof v === "function")
        node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined) continue;
      node.appendChild(
        typeof child === "string" ? document.createTextNode(child) : child,
      );
    }
    return node;
  }

  const escapeHtml = (s) =>
    String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );

  const fmtSeconds = (v) => {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return "0s";
    const m = Math.floor(n / 60);
    const s = Math.floor(n % 60);
    return m > 0 ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
  };

  const shortId = (id) => (id ? String(id).slice(0, 18) : "-");

  function toast(message, kind = "") {
    if (!message) return;
    const node = $("#toast");
    node.textContent = message;
    node.className = `toast show ${kind}`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
      node.className = "toast";
    }, 3200);
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...options,
    });
    if (resp.status === 401) {
      const next = location.pathname + location.search + location.hash;
      window.location.href = `/login?next=${encodeURIComponent(next)}`;
      throw new Error("未登录");
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try {
        detail = (await resp.json()).detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    const type = resp.headers.get("content-type") || "";
    return type.includes("application/json") ? resp.json() : resp.text();
  }

  function navigationFromUrl() {
    const params = new URL(window.location.href).searchParams;
    const navigation = { page: params.get("page") || "" };
    for (const key of ["class", "doc"]) {
      if (params.has(key)) navigation[key] = params.get(key);
    }
    return navigation;
  }

  function syncNavigation(navigation, historyMode) {
    if (!navigation) return;
    const url = new URL(window.location.href);
    for (const key of ["page", "class", "doc"]) {
      const value = navigation[key];
      if (value) url.searchParams.set(key, value);
      else url.searchParams.delete(key);
    }
    if (url.href !== window.location.href) {
      window.history[`${historyMode}State`](window.history.state, "", url);
    }
  }

  async function loadState({
    quiet = false,
    historyMode = "replace",
    navigationVersion = null,
  } = {}) {
    if (navigationVersion === null && state.navigationPending) return;
    if (state.loading) {
      await state.loading;
      if (navigationVersion === null) return;
    }
    const version = navigationVersion ?? state.navigationVersion;
    if (version !== state.navigationVersion) return;
    state.loading = (async () => {
      try {
        const ui = await api("/api/ui/state");
        if (version !== state.navigationVersion) return;
        if (navigationVersion === null && state.navigationPending) return;
        const previousPage = state.ui?.navigation?.page;
        state.ui = ui;
        state.lastLoad = Date.now();
        if (previousPage !== ui.navigation?.page) {
          for (const side of ["left", "right"]) {
            const tab = (ui[`${side}_tabs`] || []).find((item) =>
              (item.toolbox || []).some((tool) => tool.page === ui.active_page),
            );
            if (tab) state.activeTab[side] = tab.id;
          }
        }
        renderAll();
        syncNavigation(ui.navigation, historyMode);
        setConnection(true);
      } catch (err) {
        setConnection(false);
        if (!quiet) toast(`加载状态失败：${err.message}`, "err");
      } finally {
        state.loading = null;
      }
    })();
    await state.loading;
  }

  function changeNavigation(callback, historyMode = "push") {
    const version = state.navigationVersion;
    state.navigationPending += 1;
    const task = state.navigationQueue.then(async () => {
      try {
        const result = await callback();
        if (result?.refresh !== false || result?.open_page) {
          await loadState({
            quiet: true,
            historyMode,
            navigationVersion: version,
          });
        }
        return result;
      } finally {
        state.navigationPending -= 1;
      }
    });
    state.navigationQueue = task.catch(() => {});
    return task;
  }

  async function restoreNavigation(initial = false) {
    state.navigationVersion += 1;
    const navigation = navigationFromUrl();
    const hasPage = new URL(window.location.href).searchParams.has("page");
    try {
      await changeNavigation(async () => {
        if (!initial || hasPage) {
          await api("/api/ui/navigate", {
            method: "POST",
            body: JSON.stringify(navigation),
          });
        }
      }, "replace");
    } catch (err) {
      toast(`加载状态失败：${err.message}`, "err");
    }
  }

  function setConnection(ok) {
    const pill = $("#connection");
    pill.textContent = ok ? "已连接" : "连接断开";
    pill.className = `pill ${ok ? "pill-ok" : "pill-err"}`;
  }

  function typeset(node = $("#page-content")) {
    if (!node || !window.renderMathInElement) return;
    if (
      !node.textContent.includes("$") &&
      !node.textContent.includes("\\(") &&
      !node.textContent.includes("\\[")
    ) {
      return;
    }
    try {
      window.renderMathInElement(node, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "$", right: "$", display: false },
          { left: "\\(", right: "\\)", display: false },
        ],
        ignoredClasses: ["agent-workflow"],
        throwOnError: false,
        strict: false,
      });
    } catch (_) {
      /* one malformed formula must not stop the page */
    }
  }

  // ------------------------------------------------------------------
  // Blocks
  // ------------------------------------------------------------------
  function actionButton(
    label,
    action,
    value = undefined,
    cls = "btn btn-sm",
    disabled = false,
  ) {
    const attrs = { class: cls, dataset: { action } };
    if (value !== undefined) attrs.dataset.value = JSON.stringify(value);
    attrs.type = "button";
    const btn = el("button", { ...attrs, text: String(label) }, []);
    if (disabled) btn.disabled = true;
    return btn;
  }

  function renderLog(block) {
    const pre = el("pre", { class: "log" });
    const lines = Array.isArray(block.lines) ? block.lines : [];
    for (const line of lines) {
      if (typeof line === "string") {
        pre.appendChild(el("span", { class: "log-line", text: line }));
      } else {
        pre.appendChild(
          el("span", { class: "log-line" }, [
            line.time
              ? el("span", { class: "ts", text: String(line.time) })
              : null,
            document.createTextNode(String(line.text ?? line.message ?? "")),
          ]),
        );
      }
    }
    return pre;
  }

  function renderTable(block) {
    const wrap = el("div", { class: "table-wrap" });
    const table = el("table", { class: "data-table" });
    const columns = Array.isArray(block.columns) ? block.columns : [];
    const rows = Array.isArray(block.rows) ? block.rows : [];
    const rowActions = Array.isArray(block.row_actions)
      ? block.row_actions
      : [];

    const colLabel = (c) =>
      typeof c === "string" ? c : (c.label ?? c.key ?? "");
    const colKey = (c, i) => (typeof c === "string" ? c : (c.key ?? i));
    const thead = el("thead");
    const tr = el("tr");
    for (const c of columns) tr.appendChild(el("th", { text: colLabel(c) }));
    if (rowActions.length) tr.appendChild(el("th", { text: "操作" }));
    thead.appendChild(tr);
    table.appendChild(thead);

    const tbody = el("tbody");
    for (const row of rows) {
      const rowObj = Array.isArray(row)
        ? Object.fromEntries(columns.map((c, i) => [colKey(c, i), row[i]]))
        : row || {};
      const tr = el("tr");
      for (let i = 0; i < columns.length; i += 1) {
        const c = columns[i];
        const key = colKey(c, i);
        let value = Array.isArray(row) ? row[i] : rowObj[key];
        if (typeof value === "object" && value !== null)
          value = JSON.stringify(value);
        tr.appendChild(el("td", { text: value ?? "" }));
      }
      if (rowActions.length) {
        const td = el("td");
        for (const action of rowActions) {
          let value = action.value ? { ...action.value } : {};
          if (Array.isArray(action.keys)) {
            for (const key of action.keys) value[key] = rowObj[key];
          } else if (!action.value) {
            value = { ...rowObj };
          }
          td.appendChild(
            actionButton(
              action.label || "执行",
              action.action,
              value,
              action.class || "btn btn-sm",
            ),
          );
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    if (!rows.length)
      wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
    return wrap;
  }

  function renderTree(block) {
    const wrap = el("div", { class: "tree" });
    const nodes = Array.isArray(block.nodes) ? block.nodes : [];
    if (!nodes.length) {
      wrap.appendChild(
        el("div", {
          class: "tree-empty",
          text: block.empty_text || "暂无内容",
        }),
      );
      return wrap;
    }
    for (const node of nodes) wrap.appendChild(renderTreeNode(node));
    return wrap;
  }

  function renderTreeNode(node) {
    const box = el("div", { class: "tree-node" });
    const children = Array.isArray(node.children) ? node.children : [];
    const hasChildren = children.length > 0;
    const canExpand = hasChildren || !!node.toggle;
    const row = el("div", {
      class: `tree-row ${node.active ? "active" : ""} ${node.live ? "live" : ""}`,
    });
    const caret = el("button", {
      class: `tree-caret ${canExpand ? "" : "leaf"}`,
      type: "button",
      text: node.expanded ? "▾" : "▸",
      title: canExpand ? (node.expanded ? "折叠" : "展开") : "",
    });
    caret.addEventListener("click", (event) => {
      event.stopPropagation();
      if (canExpand) runNodeAction(node.toggle);
    });
    row.appendChild(caret);
    if (node.icon)
      row.appendChild(el("span", { class: "tree-icon", text: node.icon }));
    row.appendChild(
      el("span", { class: "tree-label", text: node.label || node.id || "" }),
    );
    if (node.subtitle)
      row.appendChild(el("span", { class: "tree-sub", text: node.subtitle }));
    if (node.badge) {
      row.appendChild(
        el("span", {
          class: `tree-badge ${node.badge_class || ""}`,
          text: node.badge,
        }),
      );
    }
    row.addEventListener("click", () => {
      if (node.action) runNodeAction(node.action);
      else if (canExpand) runNodeAction(node.toggle);
    });
    box.appendChild(row);
    if (hasChildren && node.expanded) {
      const kids = el("div", { class: "tree-children" });
      for (const child of children) kids.appendChild(renderTreeNode(child));
      box.appendChild(kids);
    }
    return box;
  }

  function runNodeAction(spec) {
    if (!spec || !spec.action) return;
    postAction(spec.action, spec.value || {});
  }

  function renderRows(block) {
    const wrap = el("div", { class: "row-list block" });
    for (const item of block.items || []) {
      const row = el("div", {
        class: `row-item ${item.active ? "active" : ""}`,
      });
      const main = el("div", { class: "row-main" }, [
        el("div", { class: "row-title", text: item.title || item.text || "" }),
        item.subtitle
          ? el("div", { class: "row-sub", text: item.subtitle })
          : null,
        item.meta ? el("div", { class: "row-meta", text: item.meta }) : null,
      ]);
      row.appendChild(main);
      if (item.action) {
        row.appendChild(
          actionButton(
            item.label || "打开",
            item.action,
            item.value,
            item.class || "btn btn-sm",
          ),
        );
      }
      wrap.appendChild(row);
    }
    if (!wrap.children.length)
      wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
    return wrap;
  }

  function renderCards(block) {
    const wrap = el("div", { class: "cards" });
    for (const item of block.items || []) {
      const card = el("div", { class: "card" });
      if (item.title)
        card.appendChild(el("div", { class: "card-title", text: item.title }));
      if (item.subtitle)
        card.appendChild(el("div", { class: "card-sub", text: item.subtitle }));
      if (item.meta) {
        const meta = el("div", { class: "card-meta" });
        const entries = Array.isArray(item.meta)
          ? item.meta
          : Object.entries(item.meta);
        for (const entry of entries) {
          const text = Array.isArray(entry)
            ? `${entry[0]}: ${entry[1]}`
            : String(entry);
          meta.appendChild(el("span", { text }));
        }
        card.appendChild(meta);
      }
      if (item.blocks) card.appendChild(renderBlocks(item.blocks));
      if (item.actions && item.actions.length) {
        const actions = el("div", { class: "card-actions" });
        for (const action of item.actions) {
          actions.appendChild(
            actionButton(
              action.label || "执行",
              action.action,
              action.value,
              action.class || "btn btn-sm",
            ),
          );
        }
        card.appendChild(actions);
      }
      wrap.appendChild(card);
    }
    if (!wrap.children.length)
      wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
    return wrap;
  }

  function renderForm(block) {
    const submitSpec = block.submit || {};
    const action = block.action || submitSpec.action || "";
    const form = el("form", { class: "form" });
    if (action) form.dataset.action = action;
    for (const field of block.fields || []) {
      const row = el("div", { class: "form-row" });
      const id = `f-${Math.random().toString(36).slice(2)}`;
      const fieldName = field.name || field.key;
      row.appendChild(el("label", { for: id, text: field.label || fieldName }));
      if (field.type === "action" || field.input === "button") {
        row.appendChild(
          actionButton(
            field.button_label || field.label || "执行",
            field.action,
            {},
            "btn btn-sm btn-primary",
          ),
        );
        if (field.description)
          row.appendChild(
            el("div", { class: "hint", text: field.description }),
          );
        form.appendChild(row);
        continue;
      }
      const rawType = field.type || (field.secret ? "secret" : "text");
      const type = field.input || (field.secret ? "password" : rawType);
      let input;
      if (type === "textarea") {
        input = el("textarea", {
          class: "textarea",
          id,
          name: fieldName,
          placeholder: field.placeholder || "",
        });
        input.value = field.value ?? "";
      } else if (type === "select") {
        input = el("select", { class: "select", id, name: fieldName });
        const options = field.option_items || field.options || [];
        for (const option of options) {
          const value = typeof option === "object" ? option.value : option;
          const label =
            typeof option === "object"
              ? (option.label ?? option.value)
              : option;
          const opt = el("option", {
            value: String(value),
            text: String(label),
          });
          if (String(value) === String(field.value ?? field.default ?? ""))
            opt.selected = true;
          input.appendChild(opt);
        }
      } else if (type === "checkbox" || rawType === "bool") {
        input = el("input", { type: "checkbox", id, name: fieldName });
        input.checked = Boolean(field.value);
      } else {
        input = el("input", {
          class: "input",
          id,
          name: fieldName,
          type:
            type === "password" || field.secret
              ? "password"
              : type === "number" || rawType === "int" || rawType === "float"
                ? "number"
                : "text",
        });
        if (
          !field.secret &&
          field.value !== undefined &&
          field.value !== null
        ) {
          input.value = field.value;
        }
        if (field.secret) {
          input.placeholder = field.is_set
            ? `已配置（${field.secret_length || "?"} 字符）；留空保持不变`
            : "未配置；粘贴新值";
        } else if (field.placeholder) {
          input.placeholder = field.placeholder;
        }
        if (rawType === "int" || type === "number") input.step = "1";
        if (rawType === "float") input.step = "any";
      }
      input.dataset.fieldType = rawType;
      if (field.readonly) {
        input.readOnly = true;
        input.classList.add("input-readonly");
      }
      row.appendChild(input);
      if (field.secret) {
        const status = el("div", {
          class: `form-status ${field.is_set ? "ok" : ""}`,
          text: field.is_set
            ? `已配置，${field.secret_length || "?"} 字符；真实值不会回传前端`
            : "未配置",
        });
        const line = el("div", { class: "form-inline" }, [status]);
        if (field.is_set) {
          line.appendChild(
            actionButton(
              "清除",
              "settings.clear_secret",
              { key: fieldName },
              "btn btn-sm btn-danger",
            ),
          );
        }
        row.appendChild(line);
      }
      if (field.description)
        row.appendChild(el("div", { class: "hint", text: field.description }));
      form.appendChild(row);
    }
    const actions = el("div", { class: "form-actions" });
    const submit = el("button", {
      class: "btn btn-primary",
      type: "submit",
      text: block.submit_label || submitSpec.label || "保存",
    });
    actions.appendChild(submit);
    form.appendChild(actions);
    return form;
  }

  function renderTabs(block) {
    const wrap = el("div", { class: "tabs" });
    const bar = el("div", { class: "tabs-bar" });
    const body = el("div", { class: "tabs-body" });
    const tabs = block.tabs || [];
    const key =
      block.id ||
      block.key ||
      `tabs:${tabs.map((t) => t.title || "").join("\u0001")}`;
    const savedTitle = state.blockTabs[key];
    const navigation = state.ui?.navigation;
    let active = tabs.findIndex(
      (t) =>
        navigation?.doc &&
        t.meta?.doc_key === navigation.doc &&
        t.meta?.page_id === navigation?.page,
    );
    if (active < 0)
      active = tabs.findIndex((t) => (t.title || "") === savedTitle);
    if (active < 0) active = tabs.findIndex((t) => t.active);
    if (active < 0) active = 0;
    // Selection-menu context of the visible tab: which document is displayed
    // and which menu items its owning plugin registered for this reader page.
    const applyMeta = (tab) => {
      const meta = (tab && tab.meta) || {};
      body.dataset.docKey = meta.doc_key || "";
      body.dataset.docPath = meta.path || "";
      body.dataset.pageId = meta.page_id || "";
      body.dataset.owner = meta.owner || "";
      body.dataset.selectionActions = JSON.stringify(
        meta.selection_actions || [],
      );
    };
    const show = (index, remember = true) => {
      active = index;
      if (remember) state.blockTabs[key] = tabs[index]?.title || "";
      $$(".tab-btn", bar).forEach((b, i) =>
        b.classList.toggle("active", i === active),
      );
      applyMeta(tabs[index]);
      body.replaceChildren(renderBlocks(tabs[index]?.blocks || []));
      if (
        remember &&
        (body.textContent.includes("$") || body.textContent.includes("\\("))
      ) {
        typeset(body);
      }
    };
    tabs.forEach((tab, i) => {
      const btn = el("button", {
        class: "tab-btn",
        type: "button",
        text: tab.title || `Tab ${i + 1}`,
      });
      btn.addEventListener("click", async () => {
        show(i);
        const meta = tab.meta || {};
        if (!meta.doc_key || !meta.page_id) return;
        try {
          await changeNavigation(() =>
            api("/api/ui/navigate", {
              method: "POST",
              body: JSON.stringify({ page: meta.page_id, doc: meta.doc_key }),
            }),
          );
        } catch (err) {
          toast(`打开页面失败：${err.message}`, "err");
        }
      });
      bar.appendChild(btn);
    });
    wrap.appendChild(bar);
    wrap.appendChild(body);
    if (tabs.length) show(active, false);
    return wrap;
  }

  // ------------------------------------------------------------------
  // Video player (class page)
  // ------------------------------------------------------------------
  function playerKey(block) {
    return block.id || "player";
  }

  function attachStream(video, source, startAt) {
    const rawSrc = String(source.src || "");
    if (!rawSrc) return;
    // Relay every media URL through this server: the browser may be on a VPN
    // that cannot reach live.sjtu.edu.cn, while the server can.
    const src = relayedMediaUrl(rawSrc);
    if (
      rawSrc.includes(".flv") &&
      window.mpegts &&
      window.mpegts.isSupported()
    ) {
      const player = window.mpegts.createPlayer({
        type: "flv",
        isLive: true,
        url: src,
      });
      player.attachMediaElement(video);
      player.load();
      video._mpegts = player;
      return;
    }
    const isHls = rawSrc.includes(".m3u8");
    const nativeHls = video.canPlayType("application/vnd.apple.mpegurl");
    if (isHls && !nativeHls && window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({
        startPosition: startAt > 1 ? startAt : -1,
        lowLatencyMode: true,
        maxBufferLength: 30,
      });
      hls.loadSource(src);
      hls.attachMedia(video);
      video._hls = hls;
      return;
    }
    video.src = src;
    if (startAt > 1) {
      video.addEventListener(
        "loadedmetadata",
        () => {
          try {
            video.currentTime = startAt;
          } catch (_) {}
        },
        { once: true },
      );
    }
  }

  // HLS playlists carry absolute segment URLs, so only progressive/FLV media is
  // relayed; those are the formats the SJTU recorder actually returns.
  function relayedMediaUrl(src) {
    if (!src) return src;
    if (String(src).includes(".m3u8")) return src;
    return `/api/media?src=${encodeURIComponent(src)}`;
  }

  function renderPlayer(block) {
    const key = playerKey(block);
    const cache = state.playerCache[key] || {};
    const wrap = el("div", { class: "player" });
    const grid = el("div", { class: "player-grid" });
    const videos = [];
    const sources = Array.isArray(block.sources) ? block.sources : [];

    for (const source of sources) {
      const saved = block.live ? {} : cache[source.key] || {};
      const pane = el("div", { class: "player-pane" });
      pane.appendChild(
        el("div", {
          class: "player-label",
          text: source.label || source.key || "",
        }),
      );
      const video = el("video", {
        class: "player-video",
        controls: "controls",
        playsinline: "",
        preload: "metadata",
      });
      video.muted = Boolean(source.muted);
      video.dataset.srcKey = source.key || "";
      attachStream(video, source, Number(saved.t || 0));
      const remember = () => {
        state.playerCache[key] = state.playerCache[key] || {};
        state.playerCache[key][source.key] = {
          t: Number.isFinite(video.currentTime) ? video.currentTime : 0,
        };
      };
      video.addEventListener("timeupdate", remember);
      video.addEventListener("pause", remember);
      video.addEventListener("seeked", remember);
      const hint = el("div", { class: "player-error", text: "" });
      video.addEventListener("error", () => {
        hint.textContent =
          video._mpegts || video._hls
            ? "视频加载失败：转发代理无法连上视频源"
            : "视频无法播放（服务器转发失败）";
      });
      videos.push(video);
      pane.appendChild(video);
      pane.appendChild(hint);
      grid.appendChild(pane);
    }

    if (block.screen_missing) {
      grid.appendChild(
        el("div", {
          class: "player-pane player-empty",
          text: "该课程没有屏幕流",
        }),
      );
    }
    wrap.appendChild(grid);

    const bar = el("div", { class: "player-actions" });
    const finished = Boolean(block.finished);
    const live = Boolean(block.live);
    let processing = Boolean(block.processing);

    const stateText = () => {
      if (finished) return "已结束";
      if (block.queued) return live ? "直播，排队中" : "排队中";
      if (live) return processing ? "直播，处理中" : "直播，已暂停处理";
      return processing ? "处理中" : "已暂停";
    };
    const stateEl = el("span", { class: "player-state", text: stateText() });
    const toggle = el("button", {
      class: "btn btn-sm btn-primary",
      type: "button",
      text: finished
        ? "已结束"
        : block.queued
          ? "提前处理"
          : processing
            ? "暂停处理"
            : "继续处理",
    });
    toggle.disabled = finished;

    const playAll = () => {
      for (const video of videos) {
        if (video.dataset.srcKey === "screen") video.muted = true;
        const tryPlay = () => {
          const p = video.play();
          if (p && p.catch) p.catch(() => {});
        };
        video.addEventListener("canplay", tryPlay, { once: true });
        tryPlay();
      }
    };
    const pauseAll = () => {
      for (const video of videos) {
        try {
          video.pause();
        } catch (_) {}
      }
    };

    if (!finished) {
      toggle.addEventListener("click", async () => {
        if (block.queued) {
          // Already waiting in line: the server moves it to the front; the
          // next poll reports whether it actually got the engine slot.
          await postAction(block.start_action, {});
          stateEl.textContent = stateText();
          toggle.textContent = "提前处理";
          return;
        }
        const action = processing ? block.pause_action : block.start_action;
        const result = await postAction(action, {});
        if (!result || result.ok === false) return;
        if (!processing && String(result.message || "").includes("准备")) {
          // Streams are still resolving; the server starts processing as soon
          // as they are ready and the page re-renders in the running state.
          stateEl.textContent = "准备中…";
          return;
        }
        processing = !processing;
        toggle.textContent = processing ? "暂停处理" : "继续处理";
        stateEl.textContent = stateText();
        if (processing) playAll();
        else if (!live) pauseAll();
      });
    }

    // Live video keeps playing while processing is paused; recordings only
    // play while processing is running.
    if (!finished && (live || processing)) {
      playAll();
    }

    bar.appendChild(stateEl);
    bar.appendChild(toggle);
    wrap.appendChild(bar);
    return wrap;
  }

  // ------------------------------------------------------------------
  // Switch + Q&A blocks
  // ------------------------------------------------------------------
  function renderSwitch(block) {
    const wrap = el("div", { class: "block switch-block" });
    const input = el("input", { type: "checkbox", class: "switch-input" });
    input.checked = Boolean(block.checked);
    const label = el("label", { class: "switch-label" }, [
      input,
      el("span", { class: "switch-track" }),
      el("span", { class: "switch-text", text: block.label || "" }),
    ]);
    input.addEventListener("change", () => {
      postAction(block.action, { enabled: input.checked });
    });
    wrap.appendChild(label);
    if (block.hint)
      wrap.appendChild(el("div", { class: "hint", text: block.hint }));
    return wrap;
  }

  function qaPreview(text) {
    const flat = String(text || "")
      .replace(/\s+/g, " ")
      .trim();
    return flat.length > 90 ? `${flat.slice(0, 90)}…` : flat || "（提问）";
  }

  function qaLevelLabel(intensity) {
    if (intensity === "research") return "研究";
    if (intensity === "instant") return "快速";
    return "";
  }

  function buildQaLive(block) {
    const live = el("div", {
      class: "qa-live",
      dataset: { qaId: block.id || "", qaPath: block.path || "" },
    });
    live.appendChild(el("div", { class: "qa-live-phase", text: "思考中…" }));
    live.appendChild(el("div", { class: "qa-live-tools" }));
    live.appendChild(el("div", { class: "qa-live-thinking" }));
    live.appendChild(el("div", { class: "qa-live-answer" }));
    return live;
  }

  function renderQA(block) {
    const pending = String(block.status || "") === "pending";
    const collapsed = Boolean(block.collapsed) && !pending;
    const wrap = el("section", {
      class: `qa ${collapsed ? "collapsed" : ""}`,
      dataset: {
        qaId: block.id || "",
        qaPath: block.path || "",
        qaStatus: block.status || "",
      },
    });
    const head = el("div", { class: "qa-head" });
    const toggle = el("button", {
      class: "qa-toggle",
      type: "button",
      text: collapsed ? "▸" : "▾",
      title: "折叠 / 展开",
    });
    toggle.addEventListener("click", () => {
      const next = !wrap.classList.contains("collapsed");
      wrap.classList.toggle("collapsed", next);
      toggle.textContent = next ? "▸" : "▾";
      postAction(block.collapse_action || "reader.qa_collapse", {
        path: block.path,
        id: block.id,
        collapsed: next,
      });
    });
    head.appendChild(toggle);
    head.appendChild(el("span", { class: "qa-badge qa-badge-q", text: "问" }));
    head.appendChild(
      el("span", { class: "qa-summary", text: qaPreview(block.question) }),
    );
    const level = qaLevelLabel(block.intensity);
    if (level) head.appendChild(el("span", { class: "qa-level", text: level }));
    if (block.ts)
      head.appendChild(el("span", { class: "qa-time", text: block.ts }));
    if (pending)
      head.appendChild(el("span", { class: "qa-pending", text: "回答中…" }));
    head.appendChild(
      actionButton(
        "删除",
        block.delete_action || "reader.qa_delete",
        { path: block.path, id: block.id },
        "btn btn-sm btn-danger qa-delete",
      ),
    );

    const body = el("div", { class: "qa-body" });
    body.appendChild(
      el("div", { class: "qa-msg qa-question" }, [
        el("div", { class: "qa-role", text: "你的提问" }),
        el("div", {
          class: "qa-text markdown",
          html: block.question_html || escapeHtml(block.question || ""),
        }),
      ]),
    );
    const answerBox = el("div", {
      class: `qa-msg qa-answer ${pending ? "pending" : ""} ${block.status === "error" ? "qa-failed" : ""}`,
    });
    answerBox.appendChild(el("div", { class: "qa-role", text: "Agent 回答" }));
    if (pending) {
      answerBox.appendChild(buildQaLive(block));
    } else {
      answerBox.appendChild(
        el("div", {
          class: "qa-text markdown",
          html: block.answer_html || escapeHtml(block.answer || "（无内容）"),
        }),
      );
    }
    body.appendChild(answerBox);

    wrap.appendChild(head);
    wrap.appendChild(body);
    return wrap;
  }

  // ------------------------------------------------------------------
  // Live Q&A patches (WebSocket notify, never a full-page refresh)
  // ------------------------------------------------------------------
  const _QA_PHASES = {
    thinking: "思考中…",
    tool: "检索 / 读取材料…",
    answering: "回答中…",
    done: "已完成",
    error: "失败",
  };

  function qaNode(path, id) {
    return (
      $$(".qa").find(
        (node) =>
          node.dataset.qaId === id && (!path || node.dataset.qaPath === path),
      ) || null
    );
  }

  function renderQaTools(node, tools) {
    const box = node.querySelector(".qa-live-tools");
    if (!box) return;
    box.replaceChildren();
    for (const tool of tools || []) {
      box.appendChild(
        el("span", {
          class: `qa-tool ${tool.done ? "done" : "busy"}`,
          text: `${tool.name || "?"} ${tool.detail || ""}`.trim(),
        }),
      );
    }
  }

  function withScrollAnchor(node, mutate) {
    const scroller = $("#page-content");
    const before = node.getBoundingClientRect();
    const heightBefore = node.offsetHeight;
    const scrollerTop = scroller ? scroller.getBoundingClientRect().top : 0;
    mutate();
    if (!scroller) return;
    const heightAfter = node.offsetHeight;
    if (before.bottom <= scrollerTop && heightAfter !== heightBefore) {
      // The block is above the viewport: keep the visible content where it is.
      scroller.scrollTop += heightAfter - heightBefore;
    }
  }

  function finishQaBlock(node, payload) {
    node.dataset.qaStatus = payload.status || "done";
    const answerBox = node.querySelector(".qa-answer");
    if (answerBox) {
      answerBox.classList.remove("pending");
      if (payload.status === "error") answerBox.classList.add("qa-failed");
      answerBox.replaceChildren(
        el("div", { class: "qa-role", text: "Agent 回答" }),
        el("div", {
          class: "qa-text markdown",
          html:
            payload.answer_html || escapeHtml(payload.answer || "（无内容）"),
        }),
      );
      typeset(answerBox);
    }
    const pending = node.querySelector(".qa-pending");
    if (pending) pending.remove();
    if (payload.collapsed !== false) {
      node.classList.add("collapsed");
      const toggle = node.querySelector(".qa-toggle");
      if (toggle) toggle.textContent = "▸";
    }
  }

  function applyQaLive(payload) {
    if (!payload || !payload.id) return;
    const node = qaNode(payload.path || "", payload.id);
    if (!node) return;
    withScrollAnchor(node, () => {
      const phase = node.querySelector(".qa-live-phase");
      if (phase && payload.phase) {
        const text = _QA_PHASES[payload.phase] || payload.phase;
        phase.textContent = payload.elapsed
          ? `${text}, ${payload.elapsed}s`
          : text;
      }
      if (Array.isArray(payload.tools)) renderQaTools(node, payload.tools);
      if (payload.reasoning_delta) {
        const box = node.querySelector(".qa-live-thinking");
        if (box) {
          box.appendChild(document.createTextNode(payload.reasoning_delta));
          box.scrollTop = box.scrollHeight;
        }
      }
      if (payload.text_delta) {
        const box = node.querySelector(".qa-live-answer");
        if (box) box.appendChild(document.createTextNode(payload.text_delta));
      }
      if (payload.status && payload.status !== "pending") {
        finishQaBlock(node, payload);
      }
    });
  }

  function renderBlocks(blocks) {
    const frag = document.createDocumentFragment();
    for (const block of blocks || []) {
      const node = renderBlock(block);
      if (node) frag.appendChild(node);
    }
    return frag;
  }

  function destroyPlayers(root) {
    if (!root) return;
    for (const video of $$("video", root)) {
      if (video._hls) {
        try {
          video._hls.destroy();
        } catch (_) {}
        video._hls = null;
      }
      if (video._mpegts) {
        try {
          video._mpegts.destroy();
        } catch (_) {}
        video._mpegts = null;
      }
    }
  }

  const agentSelections = new Map();
  const agentViews = new Map();

  function cleanupAgentViews() {
    const active = state.ui.active_page;
    const openPages = new Set(state.ui.open_pages || []);
    if (active) openPages.add(active);
    const pages = new Set((state.ui.pages || []).map((page) => page.id));
    const activeKeys = new Set();
    const collect = (blocks) => {
      for (const block of blocks || []) {
        if (block.type === "agent_activity")
          activeKeys.add(JSON.stringify([active, block.id]));
        if (block.type === "tabs")
          for (const tab of block.tabs || []) collect(tab.blocks);
        if (block.type === "cards")
          for (const item of block.items || []) collect(item.blocks);
      }
    };
    collect(state.ui.page_content?.[active]);
    for (const [key, view] of agentViews) {
      const [page] = JSON.parse(key);
      if (
        !pages.has(page) ||
        !openPages.has(page) ||
        (page === active && !activeKeys.has(key))
      ) {
        view.dispose();
        agentViews.delete(key);
        agentSelections.delete(key);
      } else if (page !== active) view.close();
    }
  }

  function executionText(value) {
    if (value === undefined || value === null) return "";
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  function executionArgs(value) {
    if (value && typeof value === "object") return value;
    try {
      const parsed = JSON.parse(value || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function executionNumber(value) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0;
  }

  function executionTokens(value) {
    return value >= 1000000
      ? `${(value / 1000000).toFixed(1)}M`
      : `${(value / 1000).toFixed(1)}K`;
  }

  function executionToolTitle(block, args) {
    const name = (block.name || "tool").split(/[.:/]/).at(-1);
    const path = args.path || args.file_path || args.file || "";
    if (name === "read") {
      const range =
        args.offset !== undefined ? ` from line ${args.offset}` : "";
      const count = args.limit !== undefined ? `, ${args.limit} lines` : "";
      const details = block.result?.details;
      const lines = details?.lineCount ?? details?.totalLines;
      return `read ${path || "path unavailable"}${range}${count}${executionNumber(lines) ? ` (${lines} lines)` : ""}`;
    }
    if (name === "wait") {
      const targets =
        args.targets ||
        args.ids ||
        args.handles ||
        block.result?.details?.jobs?.map((job) => job.name || job.id);
      return `wait: ${targets ? executionText(targets).replace(/\s+/g, " ") : args.i || "background work or peer messages"}`;
    }
    if (["grep", "glob"].includes(name))
      return `${name} ${args.pattern || path || ""}${args.pattern && path ? ` in ${path}` : ""}`;
    return `${name}${path ? ` ${path}` : args.i ? `: ${args.i}` : ""}`;
  }

  function renderExecutionBlock(block, row) {
    const args = executionArgs(block.arguments);
    const name = (block.name || "").split(/[.:/]/).at(-1);
    const isRead = block.kind === "tool" && name === "read";
    const text = executionText(block.text);
    const latestLine =
      text
        .trim()
        .split(/\n\s*\n/)
        .at(-1)
        ?.split("\n")[0] || "";
    const title =
      block.kind === "tool"
        ? executionToolTitle(block, args)
        : block.kind === "thinking"
          ? `Thinking${latestLine ? `: ${latestLine.replaceAll("**", "")}` : ""}`
          : block.kind === "review"
            ? "Review report"
            : `Text${text ? `: ${text.trim().split("\n")[0]}` : ""}`;
    row.className = `execution-block execution-${block.kind || "text"}`;
    row.dataset.status = block.status || "completed";
    const header = el(
      isRead ? "div" : "summary",
      { class: "execution-header" },
      [
        el("span", { class: "execution-title", text: title }),
        el("span", {
          class: "execution-status",
          text: block.status || "completed",
        }),
        el("span", {
          class: "execution-seconds",
          text: executionNumber(block.seconds)
            ? `${block.seconds.toFixed(1)}s`
            : "",
        }),
      ],
    );
    if (isRead) {
      row.replaceChildren(header);
      return;
    }
    const body = el("div", { class: "execution-body" });
    if (block.kind === "tool") {
      body.append(
        el("h4", { text: "Arguments" }),
        el("pre", { text: executionText(block.arguments) || "Unavailable" }),
      );
      if (block.result !== undefined)
        body.append(
          el("h4", { text: "Result" }),
          el("pre", { text: executionText(block.result) }),
        );
    } else if (block.kind === "review") {
      const report = executionArgs(block.report);
      const verdict = report.overall_correctness;
      if (verdict)
        body.appendChild(
          el("strong", { class: "execution-verdict", text: verdict }),
        );
      if (report.explanation || report.summary || text)
        body.appendChild(
          el("p", { text: report.explanation || report.summary || text }),
        );
      for (const finding of report.findings || []) {
        body.appendChild(
          el("article", { class: "execution-finding" }, [
            el("strong", {
              text: `${finding.priority !== undefined ? `P${finding.priority} ` : ""}${finding.title || "Finding"}`,
            }),
            el("p", { text: finding.body || finding.explanation || "" }),
            finding.file_path
              ? el("pre", {
                  text: `${finding.file_path}:${finding.line_start}-${finding.line_end}`,
                })
              : null,
          ]),
        );
      }
      if (!Object.keys(report).length && block.report)
        body.appendChild(el("pre", { text: executionText(block.report) }));
    } else body.appendChild(el("pre", { text }));
    const preview = el("pre", { class: "execution-preview", text: "" });
    if (block.kind === "tool") {
      if (["grep", "glob"].includes(name) && block.result !== undefined) {
        const result = block.result;
        const output =
          result?.details?.displayContent ??
          (Array.isArray(result?.content)
            ? result.content
                .filter((part) => part.type === "text")
                .map((part) => part.text || "")
                .join("\n")
            : result);
        preview.textContent = executionText(output)
          .split("\n")
          .slice(0, 6)
          .join("\n");
      } else if (
        ["write", "edit", "apply_patch"].includes(name) ||
        ["streaming", "running"].includes(block.status)
      ) {
        const writing = ["write", "edit", "apply_patch"].includes(name);
        const content = executionText(
          writing
            ? (args.content ?? args.input ?? args.patch ?? block.arguments)
            : block.arguments,
        );
        preview.textContent = writing
          ? content.split("\n").slice(-5).join("\n")
          : content.split("\n").slice(0, 5).join("\n");
      }
    }
    header.appendChild(preview);
    row.replaceChildren(header, body);
  }

  function executionPosition(view) {
    const { log } = view;
    const edge = log.getBoundingClientRect().top;
    const anchor = Array.from(log.children).find(
      (node) => node.getBoundingClientRect().bottom > edge,
    );
    return {
      top: log.scrollTop,
      bottom: log.scrollHeight - log.clientHeight - log.scrollTop <= 24,
      anchor,
      offset: anchor ? anchor.getBoundingClientRect().top - edge : 0,
    };
  }

  function renderAgentActivity(block) {
    const key = JSON.stringify([state.ui.active_page, block.id]);
    let view = agentViews.get(key);
    if (!view) {
      const root = el("section", { class: "block agent-workflow" });
      const select = el("button", {
        class: "execution-select",
        type: "button",
        "aria-label": "查看会话",
        "aria-haspopup": "listbox",
        "aria-expanded": "false",
      });
      const menu = el("div", {
        class: "execution-session-menu",
        role: "listbox",
        "aria-label": "选择会话",
        hidden: true,
      });
      document.body.appendChild(menu);
      const metrics = el("div", {
        class: "execution-metrics",
        "aria-label": "Execution metrics",
      });
      const log = el("div", {
        class: "execution-log",
        tabindex: "0",
        "aria-label": "Execution blocks",
      });
      root.append(
        el("div", { class: "execution-bar" }, [select, metrics]),
        log,
      );
      view = {
        root,
        select,
        menu,
        options: new Map(),
        metrics,
        log,
        rows: new Map(),
        positions: new Map(),
        agents: [],
      };
      const close = () => {
        menu.hidden = true;
        select.setAttribute("aria-expanded", "false");
      };
      view.close = close;
      select.addEventListener("click", () => {
        if (!menu.hidden) {
          close();
          return;
        }
        const rect = select.getBoundingClientRect();
        menu.style.left = `${rect.left}px`;
        menu.style.top = `${rect.bottom + 4}px`;
        menu.hidden = false;
        select.setAttribute("aria-expanded", "true");
        (
          menu.querySelector('[aria-selected="true"]') || menu.firstElementChild
        )?.focus();
      });
      menu.addEventListener("click", (event) => {
        const option = event.target.closest("[data-role]");
        if (!option) return;
        agentSelections.set(key, option.dataset.role);
        close();
        showAgentExecution(view, key);
        select.focus();
      });
      menu.addEventListener("keydown", (event) => {
        const options = Array.from(menu.children);
        const index = options.indexOf(document.activeElement);
        if (event.key === "Escape") {
          close();
          select.focus();
        } else if (
          ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)
        ) {
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? options.length - 1
                : (index +
                    (event.key === "ArrowDown" ? 1 : -1) +
                    options.length) %
                  options.length;
          options[next]?.focus();
        }
      });
      const onPointerDown = (event) => {
        if (
          !menu.hidden &&
          !menu.contains(event.target) &&
          !select.contains(event.target)
        )
          close();
      };
      document.addEventListener("pointerdown", onPointerDown);
      view.dispose = () => {
        close();
        document.removeEventListener("pointerdown", onPointerDown);
        menu.remove();
        root.remove();
        view.revision = (view.revision || 0) + 1;
      };
      log.addEventListener("scroll", () => {
        if (!view.updating)
          view.positions.set(view.role, executionPosition(view));
      });
      agentViews.set(key, view);
    }
    view.agents = Array.isArray(block.agents) ? block.agents : [];
    const roles = view.agents.some((agent) => agent.role === "main")
      ? new Map([
          ["main", "主写作"],
          ["note_review", "笔记审阅"],
        ])
      : new Map();
    for (const agent of view.agents)
      if (agent.role?.startsWith("chunk:")) roles.set(agent.role, agent.label);
    view.roles = roles;
    for (const [value, text] of roles) {
      let option = view.options.get(value);
      if (!option) {
        option = el("button", {
          type: "button",
          role: "option",
          dataset: { role: value },
        });
        view.options.set(value, option);
        view.menu.appendChild(option);
      }
      if (option.textContent !== text) option.textContent = text;
    }
    for (const [value, option] of view.options) {
      if (!roles.has(value)) {
        option.remove();
        view.options.delete(value);
      }
    }
    showAgentExecution(view, key);
    return view.root;
  }

  function showAgentExecution(view, key) {
    const { select, metrics, log } = view;
    if (view.role && log.isConnected)
      view.positions.set(view.role, executionPosition(view));
    view.updating = true;
    const preferred = agentSelections.get(key);
    const role =
      preferred && view.roles.has(preferred)
        ? preferred
        : view.agents.some((agent) => agent.role === "main")
          ? "main"
          : view.agents[0]?.role || "main";
    view.role = role;
    const label = `${view.roles.get(role) || "查看会话"} ▾`;
    if (select.textContent !== label) select.textContent = label;
    for (const [value, option] of view.options)
      option.setAttribute("aria-selected", String(value === role));
    const agents = view.agents.filter((agent) => agent.role === role);
    const latest = agents.at(-1) || {};
    const context =
      executionNumber(latest.context_tokens) &&
      executionNumber(latest.context_window) &&
      latest.context_window > 0
        ? `${((100 * latest.context_tokens) / latest.context_window).toFixed(1)}%/${executionTokens(latest.context_window)}`
        : "unavailable";
    const seconds = (value) =>
      executionNumber(value) ? `${value.toFixed(1)}s` : "unavailable";
    metrics.replaceChildren(
      ...[
        `${latest.model || "unavailable"}:${latest.thinking || "unavailable"}`,
        `上下文 ${context}`,
        `LLM ${seconds(latest.llm_seconds)}`,
        `工具调用 ${seconds(latest.tool_seconds)}`,
        executionNumber(latest.tokens_per_second)
          ? `${latest.tokens_per_second.toFixed(1)} tok/s`
          : "tok/s unavailable",
      ].map((text) => el("span", { text })),
    );
    const rows = [];
    for (const agent of agents) {
      for (const block of agent.blocks || []) {
        const id = JSON.stringify([agent.id, block.id]);
        const tag =
          block.kind === "tool" &&
          (block.name || "").split(/[.:/]/).at(-1) === "read"
            ? "div"
            : "details";
        let cached = view.rows.get(id);
        if (!cached || cached.node.localName !== tag) {
          if (cached) cached.node.remove();
          cached = { node: el(tag), signature: null };
          view.rows.set(id, cached);
        }
        const signature = JSON.stringify(block);
        if (cached.signature !== signature) {
          renderExecutionBlock(block, cached.node);
          cached.signature = signature;
        }
        rows.push(cached.node);
      }
    }
    if (!rows.length)
      rows.push(
        el("p", {
          class: "execution-empty",
          text: "Waiting for execution blocks",
        }),
      );
    for (const child of Array.from(log.children))
      if (!rows.includes(child)) child.remove();
    rows.forEach((row, index) => {
      if (log.children[index] !== row)
        log.insertBefore(row, log.children[index] || null);
    });
    const position = view.positions.get(role);
    const restore = () => {
      if (!position || position.bottom) log.scrollTop = log.scrollHeight;
      else if (position.anchor?.parentNode === log) {
        log.scrollTop +=
          position.anchor.getBoundingClientRect().top -
          log.getBoundingClientRect().top -
          position.offset;
      } else log.scrollTop = position.top;
    };
    restore();
    view.updating = false;
    const revision = (view.revision = (view.revision || 0) + 1);
    requestAnimationFrame(() => {
      if (view.role === role && view.revision === revision) {
        restore();
        view.positions.set(role, executionPosition(view));
      }
    });
  }

  function renderTaskQueue(existing = null) {
    const root =
      existing ||
      el("section", { class: "block task-queue" }, [
        el("h2", { class: "block-title", text: "任务队列" }),
        el("p", { class: "task-summary" }),
        el("p", {
          class: "empty task-empty",
          text: "还没有等待或运行中的任务．开始课程处理或生成笔记后，任务会出现在这里．",
        }),
        el("ul", { class: "task-list", "aria-label": "任务队列" }),
      ]);
    const tasks = state.ui.tasks || [];
    const pending = tasks.filter((task) => task.state === "pending").length;
    $(".task-summary", root).textContent =
      `等待中 ${pending} 个，运行中 ${tasks.length - pending} 个`;
    $(".task-empty", root).hidden = tasks.length > 0;
    const list = $(".task-list", root);
    const rows = new Map(
      Array.from(list.children, (row) => [row.dataset.taskId, row]),
    );
    tasks.forEach((task, index) => {
      const key = String(task.id ?? index);
      let row = rows.get(key);
      if (!row) {
        row = el("li", { class: "task-row", dataset: { taskId: key } }, [
          el("div", { class: "task-info" }, [
            el("h3", { class: "task-label" }),
            el("p", { class: "task-detail" }),
            el("p", { class: "task-session" }),
          ]),
          el("span", { class: "badge task-state" }),
          el("button", {
            type: "button",
            class: "btn btn-sm btn-danger task-cancel",
            text: "停止任务",
          }),
        ]);
        $(".task-cancel", row).addEventListener("click", async (event) => {
          const button = event.currentTarget;
          const cancel = row.task.cancel;
          if (!cancel?.action || button.disabled) return;
          button.disabled = true;
          try {
            await postAction(cancel.action, cancel.payload || {});
          } finally {
            button.disabled = false;
          }
        });
      }
      rows.delete(key);
      row.task = task;
      $(".task-label", row).textContent = task.label || "任务";
      $(".task-detail", row).textContent = task.detail || "";
      const session = $(".task-session", row);
      session.textContent = task.session_id ? `会话：${task.session_id}` : "";
      session.hidden = !task.session_id;
      $(".task-state", row).textContent =
        task.state === "pending" ? "等待中" : "运行中";
      const button = $(".task-cancel", row);
      button.hidden = !task.cancel?.action;
      button.setAttribute(
        "aria-label",
        `停止任务：${task.label || task.id || "任务"}`,
      );
      if (list.children[index] !== row)
        list.insertBefore(row, list.children[index] || null);
    });
    for (const row of rows.values()) row.remove();
    return root;
  }

  function renderBlock(block) {
    if (!block || typeof block !== "object") return null;
    const type = block.type || "text";
    if (type === "agent_activity") return renderAgentActivity(block);
    if (type === "task_queue") return renderTaskQueue();
    if (type === "html")
      return el("div", { class: "block", html: block.html || "" });
    if (type === "markdown") {
      return el("div", {
        class: "block markdown",
        html: block.html || escapeHtml(block.text || ""),
      });
    }
    if (type === "text")
      return el("pre", { class: "block pre", text: block.text ?? "" });
    if (type === "keyvalue") {
      const items = Array.isArray(block.items)
        ? block.items
        : Object.entries(block.items || {});
      const grid = el("div", { class: "kv-grid" });
      for (const [k, v] of items) {
        grid.appendChild(el("div", { class: "k", text: k }));
        grid.appendChild(
          el("div", {
            text: typeof v === "object" ? JSON.stringify(v) : String(v ?? ""),
          }),
        );
      }
      return el("div", { class: "block" }, [grid]);
    }
    if (type === "table")
      return el("div", { class: "block" }, [renderTable(block)]);
    if (type === "cards")
      return el("div", { class: "block" }, [renderCards(block)]);
    if (type === "rows") return renderRows(block);
    if (type === "tree")
      return el("div", { class: "block" }, [renderTree(block)]);
    if (type === "list") {
      const list = el("div", { class: "block btn-row" });
      for (const item of block.items || []) {
        if (typeof item === "string")
          list.appendChild(el("div", { text: item }));
        else if (item.action)
          list.appendChild(
            actionButton(
              item.text || item.label || "执行",
              item.action,
              item.value,
              item.class || "btn btn-sm",
            ),
          );
        else
          list.appendChild(
            el("div", { text: item.text || JSON.stringify(item) }),
          );
      }
      return list;
    }
    if (type === "buttons") {
      const row = el("div", { class: "block btn-row" });
      for (const b of block.buttons || []) {
        row.appendChild(
          actionButton(
            b.label || "执行",
            b.action,
            b.value,
            b.class || (b.primary ? "btn btn-primary" : "btn"),
            Boolean(b.disabled),
          ),
        );
      }
      return row;
    }
    if (type === "form")
      return el("div", { class: "block" }, [renderForm(block)]);
    if (type === "log")
      return el("div", { class: "block" }, [renderLog(block)]);
    if (type === "image") {
      return el("div", { class: "block" }, [
        el("img", { class: "image", src: block.src, alt: block.alt || "" }),
      ]);
    }
    if (type === "video") {
      const video = el("video", { controls: "controls", src: block.src });
      return el("div", { class: "block" }, [video]);
    }
    if (type === "tabs")
      return el("div", { class: "block" }, [renderTabs(block)]);
    if (type === "player") return renderPlayer(block);
    if (type === "switch") return renderSwitch(block);
    if (type === "qa") return renderQA(block);
    if (type === "progress") {
      const value = Number(block.value ?? 0);
      const pct = value <= 1 ? value * 100 : value;
      return el("div", { class: "block" }, [
        el("div", { class: "progress-label" }, [
          el("span", { text: block.label || "进度" }),
          el("span", { text: `${Math.round(pct)}%` }),
        ]),
        el("div", { class: "progress" }, [
          el("i", { style: `width:${Math.max(0, Math.min(100, pct))}%` }),
        ]),
      ]);
    }
    if (type === "badge")
      return el("span", {
        class: `badge ${block.class || ""}`,
        text: block.text || "",
      });
    return el("pre", {
      class: "block pre",
      text: JSON.stringify(block, null, 2),
    });
  }

  // ------------------------------------------------------------------
  // Layout
  // ------------------------------------------------------------------
  function renderAll() {
    if (!state.ui) return;
    cleanupAgentViews();
    renderMenubar();
    renderSidebar("left");
    renderSidebar("right");
    renderPageTabs();
    const pageChanged = renderPageContent();
    renderStatus();
    if (pageChanged) typeset();
  }

  // ------------------------------------------------------------------
  // Menubar + command palette
  // ------------------------------------------------------------------
  function commands() {
    return state.ui?.commands || [];
  }

  function closeMenus() {
    $$(".menu-wrap.open").forEach((wrap) => {
      wrap.classList.remove("open");
      const drop = $(".menu-drop", wrap);
      if (drop) drop.remove();
      const btn = $(".menu-btn", wrap);
      if (btn) btn.classList.remove("open");
    });
  }

  function runCommand(cmd) {
    closeMenus();
    closePalette();
    if (cmd.page) return openPage(cmd.page);
    if (cmd.action) return postAction(cmd.action, {});
    return null;
  }

  function renderMenubar() {
    const bar = $("#menubar");
    if (!bar) return;
    bar.replaceChildren();
    const groups = new Map();
    for (const cmd of commands()) {
      const name = cmd.category || "其他";
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push(cmd);
    }
    for (const [name, items] of groups) {
      const wrap = el("div", { class: "menu-wrap" });
      const btn = el("button", {
        class: "menu-btn",
        type: "button",
        text: name,
      });
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        const wasOpen = wrap.classList.contains("open");
        closeMenus();
        if (wasOpen) return;
        wrap.classList.add("open");
        btn.classList.add("open");
        const drop = el("div", { class: "menu-drop" });
        for (const cmd of items) {
          const item = el("button", { class: "menu-item", type: "button" }, [
            el("span", {
              text: `${cmd.icon ? cmd.icon + " " : ""}${cmd.title}`,
            }),
            cmd.subtitle
              ? el("span", { class: "sub", text: cmd.subtitle })
              : null,
          ]);
          item.addEventListener("click", (event) => {
            event.stopPropagation();
            runCommand(cmd);
          });
          drop.appendChild(item);
        }
        wrap.appendChild(drop);
      });
      wrap.appendChild(btn);
      bar.appendChild(wrap);
    }
  }

  function paletteMatches(query) {
    const q = String(query || "")
      .trim()
      .toLowerCase();
    const items = commands();
    if (!q) return items;
    return items.filter((cmd) => {
      const hay =
        `${cmd.title} ${cmd.subtitle || ""} ${cmd.category || ""} ${cmd.keywords || ""}`.toLowerCase();
      return q.split(/\s+/).every((part) => hay.includes(part));
    });
  }

  function renderPalette(query) {
    const list = $("#palette-list");
    if (!list) return;
    list.replaceChildren();
    const items = paletteMatches(query);
    state.paletteItems = items;
    if (state.paletteIndex >= items.length) state.paletteIndex = 0;
    if (!items.length) {
      list.appendChild(
        el("div", { class: "palette-empty", text: "没有匹配的命令" }),
      );
      return;
    }
    items.forEach((cmd, index) => {
      const row = el(
        "button",
        {
          class: `palette-item ${index === state.paletteIndex ? "active" : ""}`,
          type: "button",
        },
        [
          cmd.category
            ? el("span", { class: "cat", text: cmd.category })
            : null,
          el("span", { text: `${cmd.icon ? cmd.icon + " " : ""}${cmd.title}` }),
          cmd.subtitle
            ? el("span", { class: "sub", text: cmd.subtitle })
            : null,
        ],
      );
      row.addEventListener("click", () => runCommand(cmd));
      row.addEventListener("mouseenter", () => {
        state.paletteIndex = index;
        $$(".palette-item", list).forEach((node, i) =>
          node.classList.toggle("active", i === index),
        );
      });
      list.appendChild(row);
    });
  }

  function openPalette() {
    const palette = $("#palette");
    if (!palette) return;
    palette.hidden = false;
    const input = $("#palette-input");
    input.value = "";
    state.paletteIndex = 0;
    renderPalette("");
    input.focus();
  }

  function closePalette() {
    const palette = $("#palette");
    if (palette) palette.hidden = true;
    state.paletteIndex = 0;
  }

  function paletteOpen() {
    const palette = $("#palette");
    return palette && !palette.hidden;
  }

  document.addEventListener("keydown", (event) => {
    const key = event.key.toLowerCase();
    if ((event.ctrlKey || event.metaKey) && key === "p") {
      event.preventDefault();
      if (paletteOpen()) closePalette();
      else openPalette();
      return;
    }
    if (!paletteOpen()) return;
    const items = state.paletteItems || [];
    if (key === "escape") {
      event.preventDefault();
      closePalette();
    } else if (key === "arrowdown") {
      event.preventDefault();
      state.paletteIndex = Math.min(items.length - 1, state.paletteIndex + 1);
      renderPalette($("#palette-input").value);
      const active = $(".palette-item.active");
      if (active) active.scrollIntoView({ block: "nearest" });
    } else if (key === "arrowup") {
      event.preventDefault();
      state.paletteIndex = Math.max(0, state.paletteIndex - 1);
      renderPalette($("#palette-input").value);
      const active = $(".palette-item.active");
      if (active) active.scrollIntoView({ block: "nearest" });
    } else if (key === "enter") {
      event.preventDefault();
      const cmd = items[state.paletteIndex];
      if (cmd) runCommand(cmd);
    }
  });

  document.addEventListener("input", (event) => {
    if (event.target && event.target.id === "palette-input") {
      state.paletteIndex = 0;
      renderPalette(event.target.value);
    }
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest("#menubar")) closeMenus();
  });

  function bindPaletteTrigger() {
    const btn = $("#palette-btn");
    if (btn) btn.addEventListener("click", () => openPalette());
    const palette = $("#palette");
    if (palette) {
      palette.addEventListener("click", (event) => {
        if (event.target === palette) closePalette();
      });
    }
  }

  // ------------------------------------------------------------------
  // Collapsible, resizable sidebars
  // ------------------------------------------------------------------
  const SIDEBAR_MIN = 200;
  const SIDEBAR_MAX = 560;

  function sidebarWidth(side) {
    const fallback = side === "left" ? 280 : 320;
    const stored = Number(
      localStorage.getItem(`lecture-agent.sidebar.${side}.w`),
    );
    return Number.isFinite(stored) && stored > 0
      ? clampWidth(stored)
      : fallback;
  }

  function clampWidth(value) {
    const hardMax = Math.min(
      SIDEBAR_MAX,
      Math.max(280, window.innerWidth * 0.45),
    );
    return Math.round(Math.min(hardMax, Math.max(SIDEBAR_MIN, value)));
  }

  function applySidebarState() {
    const app = $("#app");
    if (!app) return;
    for (const side of ["left", "right"]) {
      const collapsed =
        localStorage.getItem(`lecture-agent.sidebar.${side}`) === "collapsed";
      app.classList.toggle(`${side}-collapsed`, collapsed);
      app.style.setProperty(
        `--${side}-col`,
        collapsed ? "0px" : `${sidebarWidth(side)}px`,
      );
    }
  }

  function setSidebarCollapsed(side, collapsed) {
    const app = $("#app");
    if (!app) return;
    app.classList.toggle(`${side}-collapsed`, collapsed);
    app.style.setProperty(
      `--${side}-col`,
      collapsed ? "0px" : `${sidebarWidth(side)}px`,
    );
    localStorage.setItem(
      `lecture-agent.sidebar.${side}`,
      collapsed ? "collapsed" : "open",
    );
  }

  function bindSidebarControls() {
    for (const btn of $$(".sidebar-close, .sidebar-restore")) {
      btn.addEventListener("click", () => {
        const side = btn.dataset.side;
        const collapsed = $("#app").classList.contains(`${side}-collapsed`);
        setSidebarCollapsed(side, !collapsed);
      });
    }
  }

  function bindSidebarResize() {
    for (const handle of $$(".resizer")) {
      const side = handle.dataset.side;
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        const app = $("#app");
        if (app.classList.contains(`${side}-collapsed`)) return;
        const workspace = $(".workspace");
        const rect = workspace.getBoundingClientRect();
        handle.setPointerCapture(event.pointerId);
        handle.classList.add("dragging");
        document.body.classList.add("resizing");
        const move = (ev) => {
          const width =
            side === "left" ? ev.clientX - rect.left : rect.right - ev.clientX;
          const value = `${clampWidth(width)}px`;
          app.style.setProperty(`--${side}-col`, value);
        };
        const stop = () => {
          handle.classList.remove("dragging");
          document.body.classList.remove("resizing");
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", stop);
          handle.removeEventListener("pointercancel", stop);
          const value = parseFloat(
            getComputedStyle(app).getPropertyValue(`--${side}-col`),
          );
          if (Number.isFinite(value) && value > 0) {
            localStorage.setItem(
              `lecture-agent.sidebar.${side}.w`,
              String(Math.round(value)),
            );
          }
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", stop);
        handle.addEventListener("pointercancel", stop);
      });
      // Double-click a gutter to reset that panel to its default width.
      handle.addEventListener("dblclick", () => {
        localStorage.removeItem(`lecture-agent.sidebar.${side}.w`);
        if (!$("#app").classList.contains(`${side}-collapsed`)) {
          $("#app").style.setProperty(
            `--${side}-col`,
            `${sidebarWidth(side)}px`,
          );
        }
      });
    }
    window.addEventListener("resize", () => {
      const app = $("#app");
      if (!app) return;
      for (const side of ["left", "right"]) {
        if (app.classList.contains(`${side}-collapsed`)) continue;
        app.style.setProperty(`--${side}-col`, `${sidebarWidth(side)}px`);
      }
    });
  }

  // ------------------------------------------------------------------
  // Selection menu (reader pages) + ask dialog
  // ------------------------------------------------------------------
  function hideSelectionMenu() {
    const menu = $("#selection-menu");
    if (!menu) return;
    menu.hidden = true;
    menu.replaceChildren();
  }

  function docContext(node) {
    const element = node && (node.nodeType === 1 ? node : node.parentElement);
    const body = element ? element.closest(".tabs-body") : null;
    if (!body) return null;
    let actions = [];
    try {
      actions = JSON.parse(body.dataset.selectionActions || "[]");
    } catch (_) {
      actions = [];
    }
    if (!actions.length) return null;
    return {
      actions,
      docKey: body.dataset.docKey || "",
      path: body.dataset.docPath || "",
      pageId: body.dataset.pageId || "",
    };
  }

  function showSelectionMenu() {
    const menu = $("#selection-menu");
    const page = $("#page-content");
    if (!menu || !page) return;
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || sel.rangeCount === 0)
      return hideSelectionMenu();
    const text = String(sel).trim();
    if (text.length < 2) return hideSelectionMenu();
    const node = sel.anchorNode;
    if (!node || !page.contains(node)) return hideSelectionMenu();
    const ctx = docContext(node);
    if (!ctx) return hideSelectionMenu();

    menu.replaceChildren();
    for (const item of ctx.actions) {
      const btn = el("button", {
        class: "selection-item",
        type: "button",
        text: `${item.icon ? item.icon + " " : ""}${item.label || item.id}`,
      });
      btn.addEventListener("click", () => {
        hideSelectionMenu();
        if (item.kind === "prompt") {
          openAskDialog(item, ctx, text);
          return;
        }
        postAction(item.action, {
          ...(item.value || {}),
          doc_key: ctx.docKey,
          path: ctx.path,
          page_id: ctx.pageId,
          selection: text,
        });
      });
      menu.appendChild(btn);
    }
    menu.hidden = false;
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const left = Math.min(
      window.innerWidth - width - 10,
      Math.max(10, rect.left + rect.width / 2 - width / 2),
    );
    let top = rect.top - height - 8;
    if (top < 8)
      top = Math.min(window.innerHeight - height - 10, rect.bottom + 8);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  let askContext = null;

  function openAskDialog(item, ctx, selection) {
    askContext = { item, ctx, selection };
    const modal = $("#ask");
    if (!modal) return;
    const preview = $("#ask-selection");
    preview.textContent =
      selection.length > 800 ? `${selection.slice(0, 800)}…` : selection;
    const levels = $("#ask-intensity");
    if (levels) {
      levels.replaceChildren();
      const options =
        Array.isArray(item.intensities) && item.intensities.length
          ? item.intensities
          : [
              { value: "instant", label: "快速" },
              { value: "research", label: "研究" },
            ];
      for (const option of options) {
        const value = typeof option === "object" ? option.value : option;
        const label =
          typeof option === "object" ? (option.label ?? option.value) : option;
        levels.appendChild(
          el("option", { value: String(value), text: String(label) }),
        );
      }
    }
    const input = $("#ask-input");
    input.value = "";
    modal.hidden = false;
    input.focus();
  }

  function closeAskDialog() {
    const modal = $("#ask");
    if (modal) modal.hidden = true;
    askContext = null;
  }

  async function sendAsk() {
    if (!askContext) return;
    const input = $("#ask-input");
    const question = (input.value || "").trim();
    if (!question) {
      toast("请先输入问题", "err");
      return;
    }
    const { item, ctx, selection } = askContext;
    const intensity = ($("#ask-intensity") || {}).value || "instant";
    closeAskDialog();
    await postAction(item.action, {
      ...(item.value || {}),
      doc_key: ctx.docKey,
      path: ctx.path,
      page_id: ctx.pageId,
      selection,
      question,
      intensity,
    });
  }

  function bindSelectionMenu() {
    document.addEventListener("mouseup", (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target && target.closest("#selection-menu")) return;
      if (target && target.closest("#ask")) return;
      window.setTimeout(showSelectionMenu, 0);
    });
    document.addEventListener("selectionchange", () => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed) hideSelectionMenu();
    });
    document.addEventListener("scroll", hideSelectionMenu, true);

    const menu = $("#selection-menu");
    if (menu) {
      // Keep the text selection alive while the menu is being clicked.
      menu.addEventListener("mousedown", (event) => event.preventDefault());
    }
  }

  function bindAskDialog() {
    const cancel = $("#ask-cancel");
    if (cancel) cancel.addEventListener("click", closeAskDialog);
    const close = $("#ask-close");
    if (close) close.addEventListener("click", closeAskDialog);
    const send = $("#ask-send");
    if (send) send.addEventListener("click", sendAsk);
    const input = $("#ask-input");
    if (input) {
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          sendAsk();
        } else if (event.key === "Escape") {
          event.preventDefault();
          closeAskDialog();
        }
      });
    }
    const modal = $("#ask");
    if (modal) {
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeAskDialog();
      });
    }
  }

  function pickTab(tabs, side) {
    const current = state.activeTab[side];
    if (current && tabs.some((t) => t.id === current)) return current;
    const saved = localStorage.getItem(`lecture-agent.tab.${side}`);
    if (saved && tabs.some((t) => t.id === saved)) return saved;
    return tabs[0]?.id || null;
  }

  function renderSidebarContext() {
    const box = $("#right-context");
    if (!box) return;
    const context = state.ui.context || null;
    if (!context) {
      box.hidden = true;
      box.replaceChildren();
      return;
    }
    box.hidden = false;
    box.replaceChildren(
      el("div", { class: "context-title", text: context.title || "" }),
      el("div", { class: "context-sub" }, [
        context.state ? el("span", { class: `dot ${context.state}` }) : null,
        el("span", { text: context.subtitle || "" }),
        context.recent
          ? el("span", { class: "context-tag", text: "最近一节" })
          : null,
      ]),
    );
  }

  function renderSidebar(side) {
    const tabs = side === "left" ? state.ui.left_tabs : state.ui.right_tabs;
    const tabsEl = $(`#${side}-tabs`);
    const toolboxEl = $(`#${side}-toolbox`);
    const sidebarKey = JSON.stringify([
      tabs,
      state.activeTab[side],
      state.ui.active_page,
      side === "right" ? state.ui.context : null,
    ]);
    if (toolboxEl.dataset.renderKey === sidebarKey) return;
    toolboxEl.dataset.renderKey = sidebarKey;
    const sidebarScroll = toolboxEl.scrollTop;
    if (side === "right") renderSidebarContext();
    const contextKnown = Object.prototype.hasOwnProperty.call(
      state.ui,
      "context",
    );
    const context = side === "right" ? state.ui.context : null;
    if (side === "right" && contextKnown && !context) {
      // Nothing is selected and there is no class history: the right panel has
      // nothing meaningful to show, so keep it out of the way instead of
      // displaying a stack of "未选择课程" widgets.
      tabsEl.replaceChildren();
      tabsEl.hidden = true;
      toolboxEl.replaceChildren(
        el("div", { class: "sidebar-empty" }, [
          el("b", { text: "还没有选择课程" }),
          el("span", { text: "从左侧课程树展开课程，点一节课开始。" }),
        ]),
      );
      return;
    }
    tabsEl.replaceChildren();
    // A single tab needs no tab strip: the left sidebar is just the course tree.
    tabsEl.hidden = (tabs || []).length <= 1;
    const active = pickTab(tabs || [], side);
    state.activeTab[side] = active;
    if (active) localStorage.setItem(`lecture-agent.tab.${side}`, active);
    for (const tab of tabs || []) {
      const btn = el(
        "button",
        {
          class: `side-tab ${tab.id === active ? "active" : ""}`,
          type: "button",
        },
        [
          tab.icon ? el("span", { class: "icon", text: tab.icon }) : null,
          el("span", { text: tab.title || tab.id }),
        ],
      );
      btn.addEventListener("click", () => {
        state.activeTab[side] = tab.id;
        localStorage.setItem(`lecture-agent.tab.${side}`, tab.id);
        renderSidebar(side);
        // Open the tab's primary page so switching tabs never leaves the
        // previous page looking like part of the new tab.
        const first = (tab.toolbox || [])[0];
        const pageId = first ? first.page || null : null;
        if (pageId && state.ui.active_page !== pageId) openPage(pageId);
      });
      tabsEl.appendChild(btn);
    }
    toolboxEl.replaceChildren();
    const current = (tabs || []).find((t) => t.id === active);
    if (!current || !(current.toolbox || []).length) {
      toolboxEl.appendChild(
        el("div", { class: "empty", text: "这个 tab 还没有工具" }),
      );
      return;
    }
    for (const item of current.toolbox) {
      const panel = el("section", {
        class: `toolbox-item ${item.title || item.description ? "" : "bare"}`,
      });
      const pageId = item.page || null;
      if (item.title || item.description) {
        const head = el("div", { class: "toolbox-head" }, [
          el("div", { class: "toolbox-title", text: item.title || item.id }),
          item.description
            ? el("div", { class: "toolbox-desc", text: item.description })
            : null,
        ]);
        if (pageId) {
          const open = actionButton(
            "打开页面",
            "ui.open_page",
            { page_id: pageId },
            "btn btn-sm btn-ghost",
          );
          open.style.float = "right";
          head.appendChild(open);
        }
        panel.appendChild(head);
      }
      const body = el("div", { class: "toolbox-body" });
      if (item.blocks && item.blocks.length) {
        body.appendChild(
          renderBlocks(
            item.blocks.filter((block) => block.type !== "agent_activity"),
          ),
        );
      } else {
        body.appendChild(
          el("div", {
            class: "empty",
            text: pageId ? "在页面中查看" : "暂无内容",
          }),
        );
      }
      panel.appendChild(body);
      toolboxEl.appendChild(panel);
    }
    toolboxEl.scrollTop = sidebarScroll;
  }

  function renderPageTabs() {
    const bar = $("#page-tabs");
    bar.replaceChildren();
    const openIds = state.ui.open_pages || [];
    const pages = (state.ui.pages || []).filter(
      (p) => openIds.includes(p.id) || p.id === state.ui.active_page,
    );
    for (const page of pages) {
      const active = page.id === state.ui.active_page;
      const tab = el("div", { class: `page-tab ${active ? "active" : ""}` }, [
        el("span", { text: page.title || page.id }),
        page.closable
          ? el("button", {
              class: "close",
              type: "button",
              text: "×",
              title: "关闭",
            })
          : null,
      ]);
      tab.addEventListener("click", async (event) => {
        if (event.target.classList.contains("close")) return;
        if (page.id !== state.ui.active_page) await openPage(page.id);
      });
      const close = $(".close", tab);
      if (close) {
        close.addEventListener("click", async (event) => {
          event.stopPropagation();
          await closePage(page.id);
        });
      }
      bar.appendChild(tab);
    }
  }

  function renderPageContent() {
    const container = $("#page-content");
    const active = state.ui.active_page || "";
    const blocks = active ? state.ui.page_content?.[active] : null;
    if (active === "ui:tasks" && container.dataset.pageId === active) {
      const queue = $(".task-queue", container);
      if (queue) {
        renderTaskQueue(queue);
        return false;
      }
    }
    // ``collapsed`` is client-side fold state persisted in the note; ignoring
    // it here keeps polling refreshes from re-rendering after a fold toggle.
    const key = active
      ? `${active}\u0000${JSON.stringify(blocks || [], (k, v) => (k === "collapsed" ? undefined : v))}`
      : "__welcome__";
    const samePage = container.dataset.pageId === active;
    // Page content is polled every few seconds; never re-render or re-run
    // KaTeX when the payload is identical.  This is what made the board and
    // notes pages feel like they were reloading constantly.
    if (samePage && state.pageKey === key) return false;
    if (
      samePage &&
      blocks?.length &&
      blocks.some((block) => block.type === "agent_activity") &&
      container.children.length === blocks.length
    ) {
      blocks.forEach((block, index) => {
        const current = container.children[index];
        if (block.type === "agent_activity") {
          const view = renderAgentActivity(block);
          if (view !== current) current.replaceWith(view);
        } else {
          const signature = JSON.stringify(block);
          if (current.dataset.blockSignature !== signature) {
            const node = renderBlock(block);
            if (node) {
              node.dataset.blockSignature = signature;
              current.replaceWith(node);
            }
          }
        }
      });
      state.pageKey = key;
      return true;
    }
    const scrollTop = samePage ? container.scrollTop : 0;
    const focused = document.activeElement;
    const editing =
      samePage &&
      focused &&
      container.contains(focused) &&
      !focused.classList.contains("execution-select") &&
      ["INPUT", "TEXTAREA", "SELECT"].includes(focused.tagName);
    if (editing) return false; // don't wipe out a form the user is typing in
    // Q&A blocks grow and shrink as answers stream in.  Record, for blocks
    // above the viewport, how tall they were, and put the scroll back so the
    // visible content does not move.
    const qaBefore = new Map();
    if (samePage) {
      const scrollerTop = container.getBoundingClientRect().top;
      for (const node of $$(".qa", container)) {
        qaBefore.set(`${node.dataset.qaPath}\u0000${node.dataset.qaId}`, {
          height: node.offsetHeight,
          above: node.getBoundingClientRect().bottom <= scrollerTop,
        });
      }
    }
    for (const view of agentViews.values()) {
      if (view.log.isConnected)
        view.positions.set(view.role, executionPosition(view));
    }
    destroyPlayers(container);
    container.replaceChildren();
    container.dataset.pageId = active;
    state.pageKey = key;
    if (!active || !blocks) {
      container.appendChild(
        el("div", { class: "welcome" }, [
          el("div", { class: "welcome-mark", text: "∮" }),
          el("h2", { text: "选择一节课" }),
          el("p", {
            text: "左侧课程树里展开课程，选择录播或直播。按 Ctrl+P 打开命令面板。",
          }),
        ]),
      );
      container.scrollTop = 0;
      return true;
    }
    container.appendChild(renderBlocks(blocks));
    container.scrollTop = samePage ? scrollTop : 0;
    if (samePage && qaBefore.size) {
      let adjust = 0;
      for (const node of $$(".qa", container)) {
        const prev = qaBefore.get(
          `${node.dataset.qaPath}\u0000${node.dataset.qaId}`,
        );
        if (prev && prev.above) adjust += node.offsetHeight - prev.height;
      }
      if (adjust) container.scrollTop += adjust;
    }
    return true;
  }

  function renderStatus() {
    const status = state.ui.status || {};
    const session = status.session || {};
    const runtime = status.runtime || {};
    const bar = $("#statusbar");
    bar.replaceChildren();

    const item = (label, value, cls = "") =>
      el("span", { class: "status-item" }, [
        cls ? el("span", { class: `dot ${cls}` }) : null,
        el("span", { text: label }),
        el("b", { text: String(value) }),
      ]);

    bar.appendChild(item("会话", shortId(session.session_id)));
    bar.appendChild(item("音频", fmtSeconds(session.audio_seconds)));
    bar.appendChild(item("视频", fmtSeconds(session.video_seconds)));
    bar.appendChild(
      item(
        "转写",
        `${session.transcript_chars ?? 0} 字 / ${session.transcript_items ?? 0} 段`,
      ),
    );
    bar.appendChild(
      item(
        "板书",
        `${session.visual_chars ?? 0} 字 / ${session.visual_items ?? 0} 段`,
      ),
    );
    bar.appendChild(
      item(
        "插件",
        (runtime.plugins_loaded || runtime.plugin_order || []).length || 0,
        runtime.started ? "ok" : "",
      ),
    );
    const tasks = state.ui.tasks || [];
    bar.appendChild(
      el(
        "button",
        {
          type: "button",
          class: "status-item status-task-link",
          "aria-label": `打开任务队列，共 ${tasks.length} 个任务`,
          onclick: () => openPage("ui:tasks"),
        },
        [
          tasks.length ? el("span", { class: "dot busy" }) : null,
          el("span", { text: "任务队列" }),
          el("b", { text: String(tasks.length) }),
        ],
      ),
    );

    const sessionPill = $("#session-pill");
    if (session.session_id) {
      sessionPill.textContent = `会话 ${shortId(session.session_id)}`;
      sessionPill.className = "pill pill-ok";
    } else {
      sessionPill.textContent = "无会话";
      sessionPill.className = "pill pill-muted";
    }
  }

  async function openPage(pageId) {
    try {
      await changeNavigation(() =>
        api(`/api/ui/pages/${encodeURIComponent(pageId)}/open`, {
          method: "POST",
        }),
      );
    } catch (err) {
      toast(`打开页面失败：${err.message}`, "err");
    }
  }

  async function closePage(pageId) {
    try {
      await changeNavigation(() =>
        api(`/api/ui/pages/${encodeURIComponent(pageId)}/close`, {
          method: "POST",
        }),
      );
    } catch (err) {
      toast(`关闭页面失败：${err.message}`, "err");
    }
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  function formValues(form) {
    const values = {};
    for (const field of Array.from(form.elements)) {
      if (!field.name || field.type === "submit" || field.type === "button")
        continue;
      if (field.type === "checkbox") {
        values[field.name] = field.checked;
      } else if (field.dataset.fieldType === "json") {
        try {
          values[field.name] = JSON.parse(field.value);
        } catch (_) {
          values[field.name] = field.value;
        }
      } else if (
        field.dataset.fieldType === "int" ||
        field.dataset.fieldType === "number"
      ) {
        values[field.name] =
          field.value === "" ? null : Number.parseInt(field.value, 10);
      } else if (field.dataset.fieldType === "float") {
        values[field.name] =
          field.value === "" ? null : Number.parseFloat(field.value);
      } else {
        values[field.name] = field.value;
      }
    }
    return values;
  }

  async function postAction(action, values = {}, pageId = null, itemId = null) {
    try {
      return await changeNavigation(async () => {
        const result = await api("/api/ui/action", {
          method: "POST",
          body: JSON.stringify({
            action,
            values,
            page_id: pageId || state.ui?.active_page || null,
            item_id: itemId || null,
          }),
        });
        if (result.message) toast(result.message, result.ok ? "ok" : "err");
        if (result.open_page) {
          await api(
            `/api/ui/pages/${encodeURIComponent(result.open_page)}/open`,
            {
              method: "POST",
            },
          );
        }
        return result;
      });
    } catch (err) {
      toast(`操作失败：${err.message}`, "err");
      return { ok: false };
    }
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-action]");
    if (!btn || btn.tagName === "FORM") return;
    event.preventDefault();
    const action = btn.dataset.action;
    if (!action) return;
    let values = {};
    for (const [key, value] of Object.entries(btn.dataset)) {
      if (["action", "value", "pageId", "itemId"].includes(key)) continue;
      values[key] = value;
    }
    if (btn.dataset.value) {
      try {
        values = { ...values, ...JSON.parse(btn.dataset.value) };
      } catch (_) {
        /* keep dataset */
      }
    }
    const form = btn.closest("form");
    if (form) values = { ...formValues(form), ...values };
    postAction(
      action,
      values,
      btn.dataset.pageId || null,
      btn.dataset.itemId || null,
    );
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-action]");
    if (!form) return;
    event.preventDefault();
    postAction(form.dataset.action, formValues(form));
  });

  // ------------------------------------------------------------------
  // WebSocket / polling
  // ------------------------------------------------------------------
  let refreshTimer = null;
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => loadState({ quiet: true }), 180);
  }

  function connectWs() {
    if (state.ws && state.ws.readyState <= 1) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    state.ws = ws;
    ws.addEventListener("open", () => {
      state.wsRetry = 0;
      setConnection(true);
      ws.send("hello");
    });
    ws.addEventListener("message", (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "refresh") scheduleRefresh();
        else if (data.type === "notify" && data.topic === "qa.live") {
          applyQaLive(data.payload || {});
        }
      } catch (_) {
        /* ignore */
      }
    });
    ws.addEventListener("close", () => {
      setConnection(false);
      state.ws = null;
      const delay = Math.min(8000, 800 * 2 ** state.wsRetry++);
      setTimeout(connectWs, delay);
    });
    ws.addEventListener("error", () => {
      try {
        ws.close();
      } catch (_) {}
    });
  }

  async function boot() {
    applySidebarState();
    bindSidebarControls();
    bindSidebarResize();
    bindSelectionMenu();
    bindAskDialog();
    bindPaletteTrigger();
    window.addEventListener("popstate", () => restoreNavigation());
    await restoreNavigation(true);
    connectWs();
    setInterval(() => {
      if (Date.now() - state.lastLoad > 2500) loadState({ quiet: true });
    }, 4000);
  }

  boot();
})();
