(() => {
  "use strict";

  const EXECUTION_ROLE = Object.freeze({
    WRITER: "writer",
    QUESTION_ANSWER: "question_answer",
    REVIEWER: "reviewer",
    CHUNK_WRITER: "chunk_writer",
  });
  const TASK_STATE = Object.freeze({
    PENDING: "pending",
    RUNNING: "running",
  });

  const BLOCK_TYPE = Object.freeze({
    AGENT_ACTIVITY: "agent_activity",
    TASK_QUEUE: "task_queue",
    HTML: "html",
    MARKDOWN: "markdown",
    TEXT: "text",
    KEYVALUE: "keyvalue",
    TABLE: "table",
    CARDS: "cards",
    ROWS: "rows",
    TREE: "tree",
    LIST: "list",
    BUTTONS: "buttons",
    FORM: "form",
    LOG: "log",
    IMAGE: "image",
    VIDEO: "video",
    TABS: "tabs",
    PLAYER: "player",
    SWITCH: "switch",
    QA: "qa",
    PROGRESS: "progress",
    BADGE: "badge",
  });

  const QA_STATUS = Object.freeze({
    PENDING: "pending",
    DONE: "done",
    ERROR: "error",
  });

  const ASK_INTENSITY = Object.freeze({
    INSTANT: "instant",
    RESEARCH: "research",
  });

  const EXECUTION_BLOCK_KIND = Object.freeze({
    TEXT: "text",
    THINKING: "thinking",
    TOOL: "tool",
    REVIEW: "review",
    USER: "user",
  });

  const EXECUTION_BLOCK_STATUS = Object.freeze({
    STREAMING: "streaming",
    RUNNING: "running",
    COMPLETED: "completed",
    FAILED: "failed",
    CANCELLED: "cancelled",
  });

  const SIDEBAR_SIDE = Object.freeze({
    LEFT: "left",
    RIGHT: "right",
  });

  const HISTORY_MODE = Object.freeze({
    PUSH: "push",
    REPLACE: "replace",
  });

  const TOAST_KIND = Object.freeze({
    DEFAULT: "",
    OK: "ok",
    ERR: "err",
  });

  const SETTING_TYPE = Object.freeze({
    STRING: "string",
    TEXT: "text",
    INT: "int",
    FLOAT: "float",
    BOOL: "bool",
    SELECT: "select",
    SECRET: "secret",
    JSON: "json",
    ACTION: "action",
  });

  const FIELD_INPUT = Object.freeze({
    BUTTON: "button",
    PASSWORD: "password",
    TEXTAREA: "textarea",
    SELECT: "select",
    CHECKBOX: "checkbox",
    NUMBER: "number",
    TEXT: "text",
    SUBMIT: "submit",
  });

  const SELECTION_ACTION_KIND = Object.freeze({
    PROMPT: "prompt",
    ACTION: "action",
  });

  const WS_MESSAGE_TYPE = Object.freeze({
    REFRESH: "refresh",
    NOTIFY: "notify",
  });

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
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined) continue;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
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

  function toast(message, kind = TOAST_KIND.DEFAULT) {
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
    historyMode = HISTORY_MODE.REPLACE,
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
          for (const side of Object.values(SIDEBAR_SIDE)) {
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
        if (!quiet) toast(`加载状态失败：${err.message}`, TOAST_KIND.ERR);
      } finally {
        state.loading = null;
      }
    })();
    await state.loading;
  }

  function changeNavigation(callback, historyMode = HISTORY_MODE.PUSH) {
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
      }, HISTORY_MODE.REPLACE);
    } catch (err) {
      toast(`加载状态失败：${err.message}`, TOAST_KIND.ERR);
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

  function actionButton(label, action, value = undefined, cls = "btn btn-sm", disabled = false) {
    const attrs = { class: cls, dataset: { action } };
    if (value !== undefined) attrs.dataset.value = JSON.stringify(value);
    attrs.type = FIELD_INPUT.BUTTON;
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
            line.time ? el("span", { class: "ts", text: String(line.time) }) : null,
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
    const rowActions = Array.isArray(block.row_actions) ? block.row_actions : [];

    const colLabel = (c) => (typeof c === "string" ? c : (c.label ?? c.key ?? ""));
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
        if (typeof value === "object" && value !== null) value = JSON.stringify(value);
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
    if (!rows.length) wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
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
      type: FIELD_INPUT.BUTTON,
      text: node.expanded ? "▾" : "▸",
      title: canExpand ? (node.expanded ? "折叠" : "展开") : "",
    });
    caret.addEventListener("click", (event) => {
      event.stopPropagation();
      if (canExpand) runNodeAction(node.toggle);
    });
    row.appendChild(caret);
    if (node.icon) row.appendChild(el("span", { class: "tree-icon", text: node.icon }));
    row.appendChild(el("span", { class: "tree-label", text: node.label || node.id || "" }));
    if (node.subtitle) row.appendChild(el("span", { class: "tree-sub", text: node.subtitle }));
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
        item.subtitle ? el("div", { class: "row-sub", text: item.subtitle }) : null,
        item.meta ? el("div", { class: "row-meta", text: item.meta }) : null,
      ]);
      row.appendChild(main);
      if (item.action) {
        row.appendChild(
          actionButton(item.label || "打开", item.action, item.value, item.class || "btn btn-sm"),
        );
      }
      wrap.appendChild(row);
    }
    if (!wrap.children.length) wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
    return wrap;
  }

  function renderCards(block, existing = null) {
    const wrap = existing || el("div", { class: "cards" });
    const previous = new Map(Array.from(wrap.children, (card) => [card._cardKey, card]));
    const wanted = new Set();
    (block.items || []).forEach((item, index) => {
      const key = item.id || item.key || `card:${index}`;
      let card = previous.get(key);
      if (!card) {
        card = el("div", { class: "card" }, [el("div"), el("div"), el("div")]);
        card._cardKey = key;
      }
      const [head, body, actions] = card.children;
      head.replaceChildren();
      if (item.title) head.appendChild(el("div", { class: "card-title", text: item.title }));
      if (item.subtitle) head.appendChild(el("div", { class: "card-sub", text: item.subtitle }));
      if (item.meta) {
        const meta = el("div", { class: "card-meta" });
        for (const entry of Array.isArray(item.meta) ? item.meta : Object.entries(item.meta))
          meta.appendChild(
            el("span", {
              text: Array.isArray(entry) ? `${entry[0]}: ${entry[1]}` : String(entry),
            }),
          );
        head.appendChild(meta);
      }
      updateBlocks(body, item.blocks || []);
      actions.className = "card-actions";
      actions.replaceChildren();
      for (const action of item.actions || [])
        actions.appendChild(
          actionButton(
            action.label || "执行",
            action.action,
            action.value,
            action.class || "btn btn-sm",
          ),
        );
      wanted.add(card);
      if (wrap.children[index] !== card && !composingWithin(card))
        moveActivityNode(wrap, card, wrap.children[index] || null);
    });
    for (const card of Array.from(wrap.children))
      if (!wanted.has(card) && !composingWithin(card)) {
        destroyPlayers(card);
        card.remove();
      }
    if (!wrap.children.length) wrap.appendChild(el("div", { class: "empty", text: "暂无数据" }));
    wrap._deferred = Array.from(wrap.children).some(
      (card) => !wanted.has(card) || composingWithin(card) || card.children[1]?._deferred,
    );
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
      if (field.type === SETTING_TYPE.ACTION || field.input === FIELD_INPUT.BUTTON) {
        row.appendChild(
          actionButton(
            field.button_label || field.label || "执行",
            field.action,
            {},
            "btn btn-sm btn-primary",
          ),
        );
        if (field.description)
          row.appendChild(el("div", { class: "hint", text: field.description }));
        form.appendChild(row);
        continue;
      }
      const rawType = field.type || (field.secret ? SETTING_TYPE.SECRET : SETTING_TYPE.TEXT);
      const type = field.input || (field.secret ? FIELD_INPUT.PASSWORD : rawType);
      let input;
      if (type === FIELD_INPUT.TEXTAREA) {
        input = el("textarea", {
          class: "textarea",
          id,
          name: fieldName,
          placeholder: field.placeholder || "",
        });
        input.value = field.value ?? "";
      } else if (type === FIELD_INPUT.SELECT) {
        input = el("select", { class: "select", id, name: fieldName });
        const options = field.option_items || field.options || [];
        for (const option of options) {
          const value = typeof option === "object" ? option.value : option;
          const label = typeof option === "object" ? (option.label ?? option.value) : option;
          const opt = el("option", {
            value: String(value),
            text: String(label),
          });
          if (String(value) === String(field.value ?? field.default ?? "")) opt.selected = true;
          input.appendChild(opt);
        }
      } else if (type === FIELD_INPUT.CHECKBOX || rawType === SETTING_TYPE.BOOL) {
        input = el("input", {
          type: FIELD_INPUT.CHECKBOX,
          id,
          name: fieldName,
        });
        input.checked = Boolean(field.value);
        const label = row.querySelector("label");
        label.classList.add("checkbox");
        label.prepend(input);
      } else {
        input = el("input", {
          class: "input",
          id,
          name: fieldName,
          type:
            type === FIELD_INPUT.PASSWORD || field.secret
              ? FIELD_INPUT.PASSWORD
              : type === FIELD_INPUT.NUMBER ||
                  rawType === SETTING_TYPE.INT ||
                  rawType === SETTING_TYPE.FLOAT
                ? FIELD_INPUT.NUMBER
                : FIELD_INPUT.TEXT,
        });
        if (!field.secret && field.value !== undefined && field.value !== null) {
          input.value = field.value;
        }
        if (field.secret) {
          input.placeholder = field.is_set
            ? `已配置（${field.secret_length || "?"} 字符）；留空保持不变`
            : "未配置；粘贴新值";
        } else if (field.placeholder) {
          input.placeholder = field.placeholder;
        }
        if (rawType === SETTING_TYPE.INT || type === FIELD_INPUT.NUMBER) input.step = "1";
        if (rawType === SETTING_TYPE.FLOAT) input.step = "any";
      }
      input.dataset.fieldType = rawType;
      if (field.readonly) {
        input.readOnly = true;
        input.classList.add("input-readonly");
      }
      if (input.type !== FIELD_INPUT.CHECKBOX) row.appendChild(input);
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
      if (field.description) row.appendChild(el("div", { class: "hint", text: field.description }));
      form.appendChild(row);
    }
    const actions = el("div", { class: "form-actions" });
    const submit = el("button", {
      class: "btn btn-primary",
      type: FIELD_INPUT.SUBMIT,
      text: block.submit_label || submitSpec.label || "保存",
    });
    actions.appendChild(submit);
    form.appendChild(actions);
    return form;
  }

  function renderTabs(block, existing = null) {
    const wrap = existing || el("div", { class: "tabs" });
    if (!wrap._tabState) {
      const bar = el("div", { class: "tabs-bar" });
      wrap.appendChild(bar);
      wrap._tabState = {
        bar,
        panels: new Map(),
        buttons: new Map(),
        active: null,
      };
    }
    const data = wrap._tabState;
    const tabs = block.tabs || [];
    const key =
      block.id || block.key || `tabs:${tabs.map((tab) => tab.title || "").join("\u0001")}`;
    const tabKey = (tab, index) => tab.id || tab.key || tab.title || `tab:${index}`;
    const navigation = state.ui?.navigation;
    const requested = tabs.find(
      (tab) =>
        navigation?.doc &&
        tab.meta?.doc_key === navigation.doc &&
        tab.meta?.page_id === navigation.page,
    );
    const remembered = tabs.find((tab) => tab.title === state.blockTabs[key]);
    const selected = requested || remembered || tabs.find((tab) => tab.active) || tabs[0];
    let active = selected ? tabKey(selected, tabs.indexOf(selected)) : null;
    const focusedView = Array.from(agentViews.values()).find(
      (view) => wrap.contains(view.root) && agentDraft(view).composing,
    );
    if (focusedView && data.active !== active) active = data.active;
    const show = (target, remember = true) => {
      const index = tabs.findIndex((tab, index) => tabKey(tab, index) === target);
      if (index < 0) return;
      if (
        Array.from(data.panels.values()).some((panel) => !panel.hidden && composingWithin(panel))
      ) {
        data.deferredTarget = target;
        return;
      }
      for (const view of agentViews.values())
        if (wrap.contains(view.root)) {
          saveAgentEditor(view);
          if (document.activeElement === view.input) view.restoreFocus = true;
        }
      const changed = data.active !== target;
      data.active = target;
      if (remember) state.blockTabs[key] = tabs[index].title || "";
      for (const [id, panel] of data.panels) panel.hidden = id !== target;
      for (const [id, button] of data.buttons) button.classList.toggle("active", id === target);
      const panel = data.panels.get(target);
      for (const view of agentViews.values())
        if (changed && panel.contains(view.root) && view.input.getClientRects().length) {
          restoreAgentEditor(view, remember && Boolean(view.restoreFocus));
          if (remember) view.restoreFocus = false;
        }
      if (remember) typeset(panel);
    };
    data.show = show;
    const wanted = new Set();
    tabs.forEach((tab, index) => {
      const id = tabKey(tab, index);
      wanted.add(id);
      let panel = data.panels.get(id);
      let button = data.buttons.get(id);
      if (!panel) {
        panel = el("div", { class: "tabs-body" });
        button = el("button", { class: "tab-btn", type: FIELD_INPUT.BUTTON });
        data.panels.set(id, panel);
        data.buttons.set(id, button);
        wrap.appendChild(panel);
        data.bar.appendChild(button);
        panel.addEventListener("compositionend", () => {
          if (data.deferredTarget !== undefined) {
            const target = data.deferredTarget;
            delete data.deferredTarget;
            data.show(target);
          }
        });
        button.addEventListener("click", async () => {
          data.show(id);
          const meta = button._tab.meta || {};
          if (!meta.doc_key || !meta.page_id) return;
          try {
            await changeNavigation(() =>
              api("/api/ui/navigate", {
                method: "POST",
                body: JSON.stringify({ page: meta.page_id, doc: meta.doc_key }),
              }),
            );
          } catch (err) {
            toast(`打开页面失败：${err.message}`, TOAST_KIND.ERR);
          }
        });
      }
      button._tab = tab;
      button.textContent = tab.title || `Tab ${index + 1}`;
      const meta = tab.meta || {};
      Object.assign(panel.dataset, {
        docKey: meta.doc_key || "",
        docPath: meta.path || "",
        pageId: meta.page_id || "",
        owner: meta.owner || "",
        selectionActions: JSON.stringify(meta.selection_actions || []),
      });
      updateBlocks(panel, tab.blocks || []);
    });
    for (const [id, panel] of data.panels)
      if (!wanted.has(id) && !composingWithin(panel)) {
        destroyPlayers(panel);
        panel.remove();
        data.buttons.get(id).remove();
        data.panels.delete(id);
        data.buttons.delete(id);
      }
    if (!focusedView) show(active, false);
    wrap._deferred =
      Boolean(focusedView) || Array.from(data.panels.values()).some((panel) => panel._deferred);
    return wrap;
  }

  function playerKey(block) {
    return block.id || "player";
  }

  function attachStream(video, source, startAt) {
    const rawSrc = String(source.src || "");
    if (!rawSrc) return;
    // Relay every media URL through this server: the browser may be on a VPN
    // that cannot reach live.sjtu.edu.cn, while the server can.
    const src = relayedMediaUrl(rawSrc);
    if (rawSrc.includes(".flv") && window.mpegts && window.mpegts.isSupported()) {
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
      type: FIELD_INPUT.BUTTON,
      text: finished ? "已结束" : block.queued ? "提前处理" : processing ? "暂停处理" : "继续处理",
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

  function renderSwitch(block) {
    const wrap = el("div", { class: "block switch-block" });
    const input = el("input", {
      type: FIELD_INPUT.CHECKBOX,
      class: "switch-input",
    });
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
    if (block.hint) wrap.appendChild(el("div", { class: "hint", text: block.hint }));
    return wrap;
  }

  function qaPreview(text) {
    const flat = String(text || "")
      .replace(/\s+/g, " ")
      .trim();
    return flat.length > 90 ? `${flat.slice(0, 90)}…` : flat || "（提问）";
  }

  function qaLevelLabel(intensity) {
    if (intensity === ASK_INTENSITY.RESEARCH) return "研究";
    if (intensity === ASK_INTENSITY.INSTANT) return "快速";
    return "";
  }

  const qaLiveCache = new Map();

  function qaKey(path, id) {
    return JSON.stringify([path || "", id]);
  }

  function qaActivity(block) {
    return (
      qaLiveCache.get(qaKey(block.path, block.id))?.activity ||
      block.activity || { id: `qa-${block.id}`, agents: [] }
    );
  }

  function renderQA(block, existing = null) {
    const cacheKey = qaKey(block.path, block.id);
    const cached = qaLiveCache.get(cacheKey);
    if (block.status === QA_STATUS.PENDING && cached)
      block =
        cached.status && cached.status !== QA_STATUS.PENDING
          ? { ...block, ...cached }
          : {
              ...cached,
              ...block,
              activity: block.activity || cached.activity,
            };
    const pending = block.status === QA_STATUS.PENDING;
    const retained = retainedQaActivity(block);
    if (pending)
      qaLiveCache.set(cacheKey, {
        ...cached,
        ...block,
        activity: block.activity || qaActivity(block),
      });
    if (!pending && retained)
      qaLiveCache.set(cacheKey, {
        ...cached,
        ...block,
        activity: qaActivity(block),
      });
    if (existing) {
      if (pending) {
        const activity = qaActivity(block);
        const root = renderAgentActivity(activity);
        const view = activityView(activity);
        view.qa = cacheKey;
        const answer = existing.querySelector(".qa-answer");
        if (root.parentNode !== answer) answer.appendChild(root);
      } else finishQaBlock(existing, block);
      return existing;
    }
    const collapsed = Boolean(block.collapsed) && !pending && !retained;
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
      type: FIELD_INPUT.BUTTON,
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
    head.appendChild(el("span", { class: "qa-summary", text: qaPreview(block.question) }));
    const level = qaLevelLabel(block.intensity);
    if (level) head.appendChild(el("span", { class: "qa-level", text: level }));
    if (block.ts) head.appendChild(el("span", { class: "qa-time", text: block.ts }));
    if (pending) head.appendChild(el("span", { class: "qa-pending", text: "回答中…" }));
    head.appendChild(
      actionButton(
        "删除",
        block.delete_action || "reader.qa_delete",
        { path: block.path, id: block.id },
        "btn btn-sm btn-danger qa-delete",
      ),
    );

    const body = el("div", { class: "qa-body" });
    const answerBox = el("div", {
      class: `qa-answer ${pending ? "pending" : ""} ${block.status === QA_STATUS.ERROR ? "qa-failed" : ""}`,
    });
    if (pending || retained) {
      const activity = qaActivity(block);
      answerBox.appendChild(renderAgentActivity(activity));
      activityView(activity).qa = cacheKey;
    }
    if (!pending) {
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

  function qaNode(path, id) {
    return (
      $$(".qa").find(
        (node) => node.dataset.qaId === id && (!path || node.dataset.qaPath === path),
      ) || null
    );
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
    node.dataset.qaStatus = payload.status || QA_STATUS.DONE;
    const answerBox = node.querySelector(".qa-answer");
    const activity = node.querySelector(".agent-workflow");
    const view = Array.from(agentViews.values()).find((item) => item.root === activity);
    const retained = retainAgentView(view);
    if (answerBox) {
      answerBox.classList.remove("pending");
      answerBox.classList.toggle("qa-failed", payload.status === QA_STATUS.ERROR);
      let answer = answerBox.querySelector(".qa-text");
      if (!answer) {
        answer = el("div", { class: "qa-text markdown" });
        answerBox.appendChild(answer);
      }
      const html = payload.answer_html || escapeHtml(payload.answer || "（无内容）");
      if (answer._answerHtml !== html) {
        answer.innerHTML = html;
        answer._answerHtml = html;
        typeset(answer);
      }
      if (view && !retained) disposeAgentView(view.key);
    }
    const pending = node.querySelector(".qa-pending");
    if (pending) pending.remove();
    if (retained) {
      node.classList.remove("collapsed");
      const toggle = node.querySelector(".qa-toggle");
      if (toggle) toggle.textContent = "▾";
    } else if (payload.collapsed !== false) {
      node.classList.add("collapsed");
      const toggle = node.querySelector(".qa-toggle");
      if (toggle) toggle.textContent = "▸";
    }
  }

  function applyQaLive(payload) {
    if (!payload || !payload.id) return;
    const key = qaKey(payload.path, payload.id);
    const cached = { ...qaLiveCache.get(key), ...payload };
    const finished = payload.status && payload.status !== QA_STATUS.PENDING;
    qaLiveCache.set(key, cached);
    const node = qaNode(payload.path || "", payload.id);
    const update = () => {
      if (finished) {
        const activityId = payload.activity?.id || `qa-${payload.id}`;
        for (const viewKey of agentViews.keys()) {
          if (JSON.parse(viewKey)[1] === activityId && !retainAgentView(agentViews.get(viewKey)))
            disposeAgentView(viewKey);
        }
        if (node) finishQaBlock(node, payload);
      } else if (payload.activity && node?.dataset.qaStatus === QA_STATUS.PENDING) {
        const answerBox = node.querySelector(".qa-answer");
        if (answerBox) {
          const activity = renderAgentActivity(payload.activity);
          activityView(payload.activity).qa = key;
          if (activity.parentNode !== answerBox) answerBox.appendChild(activity);
        }
      }
    };
    if (node) withScrollAnchor(node, update);
    else update();
  }

  function renderBlocks(blocks) {
    const frag = document.createDocumentFragment();
    for (const block of blocks || []) {
      const node = renderBlock(block);
      if (node) {
        node._blockKey = stableBlockKey(block, frag.childNodes.length);
        node._block = block;
        node._blockSignature = JSON.stringify(block);
      }
      if (node) frag.appendChild(node);
    }
    return frag;
  }

  function stableBlockKey(block, index) {
    return block.type === BLOCK_TYPE.QA
      ? `qa:${qaKey(block.path, block.id)}`
      : `${block.type || BLOCK_TYPE.TEXT}:${block.id || block.key || index}`;
  }

  function composingWithin(node) {
    return Array.from(agentViews.values()).some(
      (view) => node.contains(view.root) && agentDraft(view).composing,
    );
  }

  function moveActivityNode(parent, node, before) {
    const focused = Array.from(agentViews.values()).find(
      (view) => node.contains(view.input) && document.activeElement === view.input,
    );
    if (focused) saveAgentEditor(focused);
    parent.insertBefore(node, before);
    if (focused && !agentDraft(focused).composing) restoreAgentEditor(focused, true);
  }

  function updateBlocks(parent, blocks) {
    const previous = new Map(Array.from(parent.children, (node) => [node._blockKey, node]));
    const wanted = new Set();
    let deferred = false;
    (blocks || []).forEach((block, index) => {
      const key = stableBlockKey(block, index);
      let node = previous.get(key);
      const signature = JSON.stringify(block);
      if (node) {
        if (block.type === BLOCK_TYPE.AGENT_ACTIVITY) renderAgentActivity(block);
        else if (block.type === BLOCK_TYPE.QA) renderQA(block, node);
        else if (block.type === BLOCK_TYPE.TABS) renderTabs(block, node.firstElementChild);
        else if (block.type === BLOCK_TYPE.CARDS) renderCards(block, node.firstElementChild);
        else if (node._blockSignature !== signature) {
          const focused = document.activeElement;
          if (
            !(node.contains(focused) && ["INPUT", "TEXTAREA", "SELECT"].includes(focused.tagName))
          ) {
            destroyPlayers(node);
            const replacement = renderBlock(block);
            if (replacement) {
              node.replaceWith(replacement);
              node = replacement;
            }
          } else deferred = true;
        }
      } else node = renderBlock(block);
      if (!node) return;
      node._blockKey = key;
      node._block = block;
      if (!deferred) node._blockSignature = signature;
      if (node.firstElementChild?._deferred) deferred = true;
      wanted.add(node);
      const current = parent.children[index];
      if (current !== node) {
        if (composingWithin(node) || (current && composingWithin(current))) deferred = true;
        else moveActivityNode(parent, node, current || null);
      }
    });
    for (const node of Array.from(parent.children)) {
      if (wanted.has(node)) continue;
      if (composingWithin(node)) deferred = true;
      else {
        destroyPlayers(node);
        node.remove();
      }
    }
    parent._deferred = deferred;
    return !deferred;
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
  const agentDrafts = new Map();
  const pageSurfaces = new Map();

  function agentDraft(view, target = view.target) {
    const key = JSON.stringify([view.key, target]);
    if (!agentDrafts.has(key))
      agentDrafts.set(key, {
        value: "",
        start: 0,
        end: 0,
        direction: "none",
        top: 0,
        left: 0,
        revision: 0,
        pending: false,
        composing: false,
      });
    return agentDrafts.get(key);
  }

  function saveAgentEditor(view) {
    if (view.target === undefined || view.editorSuspended || !view.input.getClientRects().length)
      return;
    const draft = agentDraft(view);
    const input = view.input;
    if (draft.value !== input.value) {
      draft.value = input.value;
      draft.revision += 1;
    }
    draft.start = input.selectionStart;
    draft.end = input.selectionEnd;
    draft.direction = input.selectionDirection;
    draft.top = input.scrollTop;
    draft.left = input.scrollLeft;
  }

  function restoreAgentEditor(view, focus = false) {
    const draft = agentDraft(view);
    if (draft.composing) return;
    if (view.input.value !== draft.value) view.input.value = draft.value;
    if (focus) view.input.focus({ preventScroll: true });
    view.input.setSelectionRange(draft.start, draft.end, draft.direction);
    view.input.scrollTop = draft.top;
    view.input.scrollLeft = draft.left;
    const target = view.target;
    const revision = draft.revision;
    const top = draft.top;
    const left = draft.left;
    view.editorRestoring = true;
    requestAnimationFrame(() => {
      if (
        view.target === target &&
        draft.revision === revision &&
        !draft.composing &&
        view.input.getClientRects().length
      ) {
        view.input.scrollTop = top;
        view.input.scrollLeft = left;
      }
      view.editorRestoring = false;
    });
    updateAgentSend(view);
  }

  function retainAgentView(view) {
    if (!view) return false;
    saveAgentEditor(view);
    return Array.from(view.targets.keys()).some((target) => {
      const draft = agentDraft(view, target);
      return draft.value.length > 0 || draft.pending || draft.composing;
    });
  }

  function updateAgentSend(view) {
    const pending = agentDraft(view).pending;
    view.send.disabled = pending;
    view.send.textContent = pending ? "发送中…" : "发送";
  }

  async function sendAgentMessage(view) {
    saveAgentEditor(view);
    const target = view.target;
    const draft = agentDraft(view, target);
    if (draft.pending || draft.composing || !draft.value.trim()) return;
    const sessionId = view.targets.get(target)?.session_id;
    if (!sessionId || !view.sendAction) {
      toast("会话记录不可用", TOAST_KIND.ERR);
      return;
    }
    const message = draft.value;
    const revision = draft.revision;
    const action = view.sendAction;
    const values = { ...view.sendValues, agent_session_id: sessionId, message };
    draft.pending = true;
    updateAgentSend(view);
    try {
      const result = await postAction(action, values, view.page, null, true);
      if (view.target === target) saveAgentEditor(view);
      if (result?.ok && draft.revision === revision && draft.composing)
        draft.clearRevision = revision;
      if (result?.ok && draft.revision === revision && !draft.composing) {
        draft.value = "";
        draft.start = draft.end = draft.top = draft.left = 0;
        draft.direction = "none";
        draft.revision += 1;
        if (view.target === target) restoreAgentEditor(view);
      }
    } finally {
      draft.pending = false;
      if (view.target === target) updateAgentSend(view);
      settleQaEditor(view);
    }
  }

  function activityView(block, page = state.ui.active_page) {
    return agentViews.get(JSON.stringify([page, block.id]));
  }

  function retainedQaActivity(block, page = state.ui.active_page) {
    const activity = qaActivity(block);
    return retainAgentView(activityView(activity, page));
  }

  function settleQaEditor(view) {
    if (!view.qa || retainAgentView(view)) return;
    const cached = qaLiveCache.get(view.qa);
    if (!cached?.status || cached.status === QA_STATUS.PENDING) return;
    const node = view.root.closest(".qa");
    if (node) finishQaBlock(node, cached);
    disposeAgentView(view.key);
  }

  function disposeAgentView(key) {
    const view = agentViews.get(key);
    view?.dispose();
    if (view)
      for (const target of view.targets.keys()) agentDrafts.delete(JSON.stringify([key, target]));
    agentViews.delete(key);
    agentSelections.delete(key);
  }

  function cleanupAgentViews() {
    const active = state.ui.active_page;
    const openPages = new Set(state.ui.open_pages || []);
    if (active) openPages.add(active);
    const pages = new Set((state.ui.pages || []).map((page) => page.id));
    const activeKeys = new Set();
    const qaKeys = new Set();
    const collect = (blocks, page) => {
      for (const block of blocks || []) {
        if (block.type === BLOCK_TYPE.AGENT_ACTIVITY)
          activeKeys.add(JSON.stringify([page, block.id]));
        if (block.type === BLOCK_TYPE.QA) {
          const key = qaKey(block.path, block.id);
          qaKeys.add(key);
          const cached = qaLiveCache.get(key);
          const retained = retainedQaActivity(block, page);
          if (block.status !== QA_STATUS.PENDING && !retained) qaLiveCache.delete(key);
          if (
            retained ||
            (block.status === QA_STATUS.PENDING &&
              (!cached?.status || cached.status === QA_STATUS.PENDING))
          )
            activeKeys.add(JSON.stringify([page, qaActivity(block).id]));
        }
        if (block.type === BLOCK_TYPE.TABS)
          for (const tab of block.tabs || []) collect(tab.blocks, page);
        if (block.type === BLOCK_TYPE.CARDS)
          for (const item of block.items || []) collect(item.blocks, page);
      }
    };
    for (const [page, blocks] of Object.entries(state.ui.page_content || {})) collect(blocks, page);
    for (const page of pageSurfaces.keys())
      if (!pages.has(page) || !openPages.has(page)) pageSurfaces.delete(page);
    for (const key of qaLiveCache.keys()) if (!qaKeys.has(key)) qaLiveCache.delete(key);
    for (const [key, view] of agentViews) {
      const [page] = JSON.parse(key);
      if (!pages.has(page) || !openPages.has(page) || (page === active && !activeKeys.has(key))) {
        disposeAgentView(key);
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
    return value >= 1000000 ? `${(value / 1000000).toFixed(1)}M` : `${(value / 1000).toFixed(1)}K`;
  }

  function executionToolTitle(block, args) {
    const name = (block.name || "tool").split(/[.:/]/).at(-1);
    const path = args.path || args.file_path || args.file || "";
    if (name === "read") {
      const range = args.offset !== undefined ? ` from line ${args.offset}` : "";
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
    const isRead = block.kind === EXECUTION_BLOCK_KIND.TOOL && name === "read";
    const text = executionText(block.text);
    const latestLine =
      text
        .trim()
        .split(/\n\s*\n/)
        .at(-1)
        ?.split("\n")[0] || "";
    const title =
      block.kind === EXECUTION_BLOCK_KIND.TOOL
        ? executionToolTitle(block, args)
        : block.kind === EXECUTION_BLOCK_KIND.THINKING
          ? `Thinking${latestLine ? `: ${latestLine.replaceAll("**", "")}` : ""}`
          : block.kind === EXECUTION_BLOCK_KIND.REVIEW
            ? `Review report${block.round !== undefined ? ` ${block.round}` : ""}`
            : `${block.kind === EXECUTION_BLOCK_KIND.USER ? "User" : "Text"}${text ? `: ${text.trim().split("\n")[0]}` : ""}`;
    row.className = `execution-block execution-${block.kind || EXECUTION_BLOCK_KIND.TEXT}`;
    row.dataset.status = block.status || EXECUTION_BLOCK_STATUS.COMPLETED;
    const header = el(isRead ? "div" : "summary", { class: "execution-header" }, [
      el("span", { class: "execution-title", text: title }),
      el("span", {
        class: "execution-status",
        text: block.status || EXECUTION_BLOCK_STATUS.COMPLETED,
      }),
      el("span", {
        class: "execution-seconds",
        text: executionNumber(block.seconds) ? `${block.seconds.toFixed(1)}s` : "",
      }),
    ]);
    if (isRead) {
      row.replaceChildren(header);
      return;
    }
    const body = el("div", { class: "execution-body" });
    if (block.kind === EXECUTION_BLOCK_KIND.TOOL) {
      body.append(
        el("h4", { text: "Arguments" }),
        el("pre", { text: executionText(block.arguments) || "Unavailable" }),
      );
      if (block.result !== undefined)
        body.append(el("h4", { text: "Result" }), el("pre", { text: executionText(block.result) }));
    } else if (block.kind === EXECUTION_BLOCK_KIND.REVIEW) {
      const report = executionArgs(block.report);
      const verdict = report.correct;
      if (typeof verdict === "boolean")
        body.appendChild(
          el("strong", {
            class: "execution-verdict",
            text: verdict ? "Correct" : "Incorrect",
          }),
        );
      if (block.error) body.appendChild(el("p", { text: block.error }));
      if (typeof verdict === "boolean") {
        if (report.explanation) body.appendChild(el("p", { text: report.explanation }));
      } else if (text) body.appendChild(el("pre", { text }));
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
    if (block.kind === EXECUTION_BLOCK_KIND.TOOL) {
      if (["grep", "glob"].includes(name) && block.result !== undefined) {
        const result = block.result;
        const output =
          result?.details?.displayContent ??
          (Array.isArray(result?.content)
            ? result.content
                .filter((part) => part.type === FIELD_INPUT.TEXT)
                .map((part) => part.text || "")
                .join("\n")
            : result);
        preview.textContent = executionText(output).split("\n").slice(0, 6).join("\n");
      } else if (
        ["write", "edit", "apply_patch"].includes(name) ||
        [EXECUTION_BLOCK_STATUS.STREAMING, EXECUTION_BLOCK_STATUS.RUNNING].includes(block.status)
      ) {
        const writing = ["write", "edit", "apply_patch"].includes(name);
        const content = executionText(
          writing ? (args.content ?? args.input ?? args.patch ?? block.arguments) : block.arguments,
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
        type: FIELD_INPUT.BUTTON,
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
      const input = el("textarea", {
        class: "execution-message",
        rows: "3",
        "aria-label": "发送给 Agent",
      });
      const send = el("button", {
        type: FIELD_INPUT.BUTTON,
        class: "btn btn-primary",
        text: "发送",
      });
      const composer = el("div", { class: "execution-composer" }, [input, send]);
      root.append(el("div", { class: "execution-bar" }, [select, metrics]), log, composer);
      view = {
        key,
        page: state.ui.active_page,
        input,
        send,
        targets: new Map(),
        labels: new Map(),
        root,
        select,
        menu,
        options: new Map(),
        metrics,
        log,
        rows: new Map(),
        positions: new Map(),
        agents: [],
        composer,
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
        (menu.querySelector('[aria-selected="true"]') || menu.firstElementChild)?.focus();
      });
      menu.addEventListener("click", (event) => {
        const option = event.target.closest("[data-target]");
        if (!option) return;
        if (agentDraft(view).composing) {
          view.deferredTarget = option.dataset.target;
          close();
          return;
        }
        agentSelections.set(key, option.dataset.target);
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
        } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? options.length - 1
                : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
          options[next]?.focus();
        }
      });
      const onPointerDown = (event) => {
        if (!menu.hidden && !menu.contains(event.target) && !select.contains(event.target)) close();
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
        if (!view.updating) view.positions.set(view.target, executionPosition(view));
      });
      for (const type of ["input", "select", "keyup", "scroll"])
        input.addEventListener(type, () => {
          if (type !== "scroll" || !view.editorRestoring) saveAgentEditor(view);
          if (type === "input") settleQaEditor(view);
        });
      input.addEventListener("focus", () => {
        view.wasFocused = true;
      });
      input.addEventListener("blur", (event) => {
        saveAgentEditor(view);
        if (
          event.relatedTarget &&
          $("#page-content").contains(event.relatedTarget) &&
          !event.relatedTarget.closest(".tab-btn")
        )
          view.wasFocused = false;
      });
      input.addEventListener("compositionstart", () => {
        agentDraft(view).composing = true;
      });
      input.addEventListener("compositionend", () => {
        agentDraft(view).composing = false;
        saveAgentEditor(view);
        const draft = agentDraft(view);
        if (draft.clearRevision === draft.revision) {
          draft.value = "";
          draft.start = draft.end = draft.top = draft.left = 0;
          draft.direction = "none";
          draft.revision += 1;
          restoreAgentEditor(view);
        }
        delete draft.clearRevision;
        if (view.deferredTarget !== undefined) {
          agentSelections.set(key, view.deferredTarget);
          delete view.deferredTarget;
          showAgentExecution(view, key);
        }
        state.pageKey = null;
        renderPageContent();
        settleQaEditor(view);
      });
      input.addEventListener("keydown", (event) => {
        if (
          event.key === "Enter" &&
          (event.shiftKey || event.ctrlKey) &&
          !event.isComposing &&
          event.keyCode !== 229 &&
          !agentDraft(view).composing
        ) {
          event.preventDefault();
          sendAgentMessage(view);
        }
      });
      send.addEventListener("click", () => sendAgentMessage(view));
      agentViews.set(key, view);
    }
    view.sendAction = block.send_action;
    view.sendValues = block.send_values || {};
    // No channel means the run is closed: keep the log, hide the composer.
    view.composer.hidden = !view.sendAction;
    view.agents = Array.isArray(block.agents) ? block.agents : [];
    const targets = new Map();
    view.agents.forEach((agent, index) => {
      const target = agent.session_id || `legacy:${agent.id || index}`;
      targets.set(target, agent);
      if (!view.labels.has(target)) {
        const label =
          agent.label ||
          (agent.role === EXECUTION_ROLE.WRITER
            ? "主写作"
            : agent.role === EXECUTION_ROLE.REVIEWER
              ? "笔记审阅"
              : agent.role) ||
          "Agent";
        const number =
          Array.from(view.labels.values()).filter((entry) => entry.base === label).length + 1;
        view.labels.set(target, { base: label, number });
      }
    });
    for (const [target, agent] of view.targets) {
      const draft = agentDraft(view, target);
      if (!targets.has(target) && (target === view.target || draft.value.length || draft.pending))
        targets.set(target, { ...agent, closed: true });
    }
    if (!targets.size) targets.set("legacy:unavailable", {});
    view.targets = targets;
    for (const [value, agent] of targets) {
      const label = view.labels.get(value);
      const duplicates =
        label &&
        Array.from(view.labels.values()).filter((entry) => entry.base === label.base).length > 1;
      const text = label
        ? `${label.base}${duplicates ? ` ${label.number}` : ""}${agent.role === EXECUTION_ROLE.REVIEWER ? (agent.review_passed ? "（已通过）" : "（未通过）") : ""}${agent.closed ? "（已关闭）" : ""}`
        : "查看会话";
      let option = view.options.get(value);
      if (!option) {
        option = el("button", {
          type: FIELD_INPUT.BUTTON,
          role: "option",
          dataset: { target: value },
        });
        view.options.set(value, option);
        view.menu.appendChild(option);
      }
      if (option.textContent !== text) option.textContent = text;
    }
    for (const [value, option] of view.options) {
      if (!targets.has(value)) {
        option.remove();
        view.options.delete(value);
        agentDrafts.delete(JSON.stringify([key, value]));
      }
    }
    showAgentExecution(view, key);
    return view.root;
  }

  function showAgentExecution(view, key) {
    const { select, metrics, log } = view;
    if (view.target !== undefined && log.isConnected)
      view.positions.set(view.target, executionPosition(view));
    view.updating = true;
    const preferred = agentSelections.get(key) ?? view.target;
    const target = view.targets.has(preferred)
      ? preferred
      : Array.from(view.targets).find(([, agent]) => agent.role === EXECUTION_ROLE.WRITER)?.[0] ||
        view.targets.keys().next().value;
    if (view.target !== target) {
      saveAgentEditor(view);
      view.target = target;
      restoreAgentEditor(view);
    }
    updateAgentSend(view);
    agentSelections.set(key, target);
    view.root.dataset.sessionId = view.targets.get(target)?.session_id || "";
    const label = `${view.options.get(target)?.textContent || "查看会话"} ▾`;
    if (select.textContent !== label) select.textContent = label;
    for (const [value, option] of view.options)
      option.setAttribute("aria-selected", String(value === target));
    const latest = view.targets.get(target) || {};
    const agents = [latest];
    const context =
      executionNumber(latest.context_tokens) &&
      executionNumber(latest.context_window) &&
      latest.context_window > 0
        ? `${((100 * latest.context_tokens) / latest.context_window).toFixed(1)}%/${executionTokens(latest.context_window)}`
        : "unavailable";
    const seconds = (value) => (executionNumber(value) ? `${value.toFixed(1)}s` : "unavailable");
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
          block.kind === EXECUTION_BLOCK_KIND.TOOL &&
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
    for (const child of Array.from(log.children)) if (!rows.includes(child)) child.remove();
    rows.forEach((row, index) => {
      if (log.children[index] !== row) log.insertBefore(row, log.children[index] || null);
    });
    const position = view.positions.get(target);
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
      if (view.target === target && view.revision === revision) {
        restore();
        view.positions.set(target, executionPosition(view));
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
    const pending = tasks.filter((task) => task.state === TASK_STATE.PENDING).length;
    $(".task-summary", root).textContent =
      `等待中 ${pending} 个，运行中 ${tasks.length - pending} 个`;
    $(".task-empty", root).hidden = tasks.length > 0;
    const list = $(".task-list", root);
    const rows = new Map(Array.from(list.children, (row) => [row.dataset.taskId, row]));
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
            type: FIELD_INPUT.BUTTON,
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
      $(".task-state", row).textContent = task.state === TASK_STATE.PENDING ? "等待中" : "运行中";
      const button = $(".task-cancel", row);
      button.hidden = !task.cancel?.action;
      button.setAttribute("aria-label", `停止任务：${task.label || task.id || "任务"}`);
      if (list.children[index] !== row) list.insertBefore(row, list.children[index] || null);
    });
    for (const row of rows.values()) row.remove();
    return root;
  }

  function renderBlock(block) {
    if (!block || typeof block !== "object") return null;
    const type = block.type || BLOCK_TYPE.TEXT;
    if (type === BLOCK_TYPE.AGENT_ACTIVITY) return renderAgentActivity(block);
    if (type === BLOCK_TYPE.TASK_QUEUE) return renderTaskQueue();
    if (type === BLOCK_TYPE.HTML) return el("div", { class: "block", html: block.html || "" });
    if (type === BLOCK_TYPE.MARKDOWN) {
      return el("div", {
        class: "block markdown",
        html: block.html || escapeHtml(block.text || ""),
      });
    }
    if (type === BLOCK_TYPE.TEXT) return el("pre", { class: "block pre", text: block.text ?? "" });
    if (type === BLOCK_TYPE.KEYVALUE) {
      const items = Array.isArray(block.items) ? block.items : Object.entries(block.items || {});
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
    if (type === BLOCK_TYPE.TABLE) return el("div", { class: "block" }, [renderTable(block)]);
    if (type === BLOCK_TYPE.CARDS) return el("div", { class: "block" }, [renderCards(block)]);
    if (type === BLOCK_TYPE.ROWS) return renderRows(block);
    if (type === BLOCK_TYPE.TREE) return el("div", { class: "block" }, [renderTree(block)]);
    if (type === BLOCK_TYPE.LIST) {
      const list = el("div", { class: "block btn-row" });
      for (const item of block.items || []) {
        if (typeof item === "string") list.appendChild(el("div", { text: item }));
        else if (item.action)
          list.appendChild(
            actionButton(
              item.text || item.label || "执行",
              item.action,
              item.value,
              item.class || "btn btn-sm",
            ),
          );
        else list.appendChild(el("div", { text: item.text || JSON.stringify(item) }));
      }
      return list;
    }
    if (type === BLOCK_TYPE.BUTTONS) {
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
    if (type === BLOCK_TYPE.FORM) return el("div", { class: "block" }, [renderForm(block)]);
    if (type === BLOCK_TYPE.LOG) return el("div", { class: "block" }, [renderLog(block)]);
    if (type === BLOCK_TYPE.IMAGE) {
      return el("div", { class: "block" }, [
        el("img", { class: "image", src: block.src, alt: block.alt || "" }),
      ]);
    }
    if (type === BLOCK_TYPE.VIDEO) {
      const video = el("video", { controls: "controls", src: block.src });
      return el("div", { class: "block" }, [video]);
    }
    if (type === BLOCK_TYPE.TABS) return el("div", { class: "block" }, [renderTabs(block)]);
    if (type === BLOCK_TYPE.PLAYER) return renderPlayer(block);
    if (type === BLOCK_TYPE.SWITCH) return renderSwitch(block);
    if (type === BLOCK_TYPE.QA) return renderQA(block);
    if (type === BLOCK_TYPE.PROGRESS) {
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
    if (type === BLOCK_TYPE.BADGE)
      return el("span", {
        class: `badge ${block.class || ""}`,
        text: block.text || "",
      });
    return el("pre", {
      class: "block pre",
      text: JSON.stringify(block, null, 2),
    });
  }

  function renderAll() {
    if (!state.ui) return;
    cleanupAgentViews();
    renderMenubar();
    renderSidebar(SIDEBAR_SIDE.LEFT);
    renderSidebar(SIDEBAR_SIDE.RIGHT);
    renderPageTabs();
    const pageChanged = renderPageContent();
    renderStatus();
    if (pageChanged) typeset();
  }

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
        type: FIELD_INPUT.BUTTON,
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
          const item = el("button", { class: "menu-item", type: FIELD_INPUT.BUTTON }, [
            el("span", {
              text: `${cmd.icon ? cmd.icon + " " : ""}${cmd.title}`,
            }),
            cmd.subtitle ? el("span", { class: "sub", text: cmd.subtitle }) : null,
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
      list.appendChild(el("div", { class: "palette-empty", text: "没有匹配的命令" }));
      return;
    }
    items.forEach((cmd, index) => {
      const row = el(
        "button",
        {
          class: `palette-item ${index === state.paletteIndex ? "active" : ""}`,
          type: FIELD_INPUT.BUTTON,
        },
        [
          cmd.category ? el("span", { class: "cat", text: cmd.category }) : null,
          el("span", { text: `${cmd.icon ? cmd.icon + " " : ""}${cmd.title}` }),
          cmd.subtitle ? el("span", { class: "sub", text: cmd.subtitle }) : null,
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

  const SIDEBAR_MIN = 200;
  const SIDEBAR_MAX = 560;

  function sidebarWidth(side) {
    const fallback = side === SIDEBAR_SIDE.LEFT ? 280 : 320;
    const stored = Number(localStorage.getItem(`lecture-agent.sidebar.${side}.w`));
    return Number.isFinite(stored) && stored > 0 ? clampWidth(stored) : fallback;
  }

  function clampWidth(value) {
    const hardMax = Math.min(SIDEBAR_MAX, Math.max(280, window.innerWidth * 0.45));
    return Math.round(Math.min(hardMax, Math.max(SIDEBAR_MIN, value)));
  }

  function applySidebarState() {
    const app = $("#app");
    if (!app) return;
    for (const side of Object.values(SIDEBAR_SIDE)) {
      const collapsed = localStorage.getItem(`lecture-agent.sidebar.${side}`) === "true";
      app.classList.toggle(`${side}-collapsed`, collapsed);
      app.style.setProperty(`--${side}-col`, collapsed ? "0px" : `${sidebarWidth(side)}px`);
    }
  }

  function setSidebarCollapsed(side, collapsed) {
    const app = $("#app");
    if (!app) return;
    app.classList.toggle(`${side}-collapsed`, collapsed);
    app.style.setProperty(`--${side}-col`, collapsed ? "0px" : `${sidebarWidth(side)}px`);
    localStorage.setItem(`lecture-agent.sidebar.${side}`, String(collapsed));
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
            side === SIDEBAR_SIDE.LEFT ? ev.clientX - rect.left : rect.right - ev.clientX;
          const value = `${clampWidth(width)}px`;
          app.style.setProperty(`--${side}-col`, value);
        };
        const stop = () => {
          handle.classList.remove("dragging");
          document.body.classList.remove("resizing");
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", stop);
          handle.removeEventListener("pointercancel", stop);
          const value = parseFloat(getComputedStyle(app).getPropertyValue(`--${side}-col`));
          if (Number.isFinite(value) && value > 0) {
            localStorage.setItem(`lecture-agent.sidebar.${side}.w`, String(Math.round(value)));
          }
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", stop);
        handle.addEventListener("pointercancel", stop);
      });
      handle.addEventListener("dblclick", () => {
        localStorage.removeItem(`lecture-agent.sidebar.${side}.w`);
        if (!$("#app").classList.contains(`${side}-collapsed`)) {
          $("#app").style.setProperty(`--${side}-col`, `${sidebarWidth(side)}px`);
        }
      });
    }
    window.addEventListener("resize", () => {
      const app = $("#app");
      if (!app) return;
      for (const side of Object.values(SIDEBAR_SIDE)) {
        if (app.classList.contains(`${side}-collapsed`)) continue;
        app.style.setProperty(`--${side}-col`, `${sidebarWidth(side)}px`);
      }
    });
  }

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
    if (!sel || sel.isCollapsed || sel.rangeCount === 0) return hideSelectionMenu();
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
        type: FIELD_INPUT.BUTTON,
        text: `${item.icon ? item.icon + " " : ""}${item.label || item.id}`,
      });
      btn.addEventListener("click", () => {
        hideSelectionMenu();
        if (item.kind === SELECTION_ACTION_KIND.PROMPT) {
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
    if (top < 8) top = Math.min(window.innerHeight - height - 10, rect.bottom + 8);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(top)}px`;
  }

  let askContext = null;

  function openAskDialog(item, ctx, selection) {
    askContext = { item, ctx, selection };
    const modal = $("#ask");
    if (!modal) return;
    const preview = $("#ask-selection");
    preview.textContent = selection.length > 800 ? `${selection.slice(0, 800)}…` : selection;
    const levels = $("#ask-intensity");
    if (levels) {
      levels.replaceChildren();
      const options =
        Array.isArray(item.intensities) && item.intensities.length
          ? item.intensities
          : [
              { value: ASK_INTENSITY.INSTANT, label: "快速" },
              { value: ASK_INTENSITY.RESEARCH, label: "研究" },
            ];
      for (const option of options) {
        const value = typeof option === "object" ? option.value : option;
        const label = typeof option === "object" ? (option.label ?? option.value) : option;
        levels.appendChild(el("option", { value: String(value), text: String(label) }));
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
      toast("请先输入问题", TOAST_KIND.ERR);
      return;
    }
    const { item, ctx, selection } = askContext;
    const intensity = ($("#ask-intensity") || {}).value || ASK_INTENSITY.INSTANT;
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
        context.recent ? el("span", { class: "context-tag", text: "最近一节" }) : null,
      ]),
    );
  }

  function renderSidebar(side) {
    const tabs = side === SIDEBAR_SIDE.LEFT ? state.ui.left_tabs : state.ui.right_tabs;
    const tabsEl = $(`#${side}-tabs`);
    const toolboxEl = $(`#${side}-toolbox`);
    const sidebarKey = JSON.stringify([
      tabs,
      state.activeTab[side],
      state.ui.active_page,
      side === SIDEBAR_SIDE.RIGHT ? state.ui.context : null,
    ]);
    if (toolboxEl.dataset.renderKey === sidebarKey) return;
    toolboxEl.dataset.renderKey = sidebarKey;
    const sidebarScroll = toolboxEl.scrollTop;
    if (side === SIDEBAR_SIDE.RIGHT) renderSidebarContext();
    const contextKnown = Object.prototype.hasOwnProperty.call(state.ui, "context");
    const context = side === SIDEBAR_SIDE.RIGHT ? state.ui.context : null;
    if (side === SIDEBAR_SIDE.RIGHT && contextKnown && !context) {
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
    tabsEl.hidden = (tabs || []).length <= 1;
    const active = pickTab(tabs || [], side);
    state.activeTab[side] = active;
    if (active) localStorage.setItem(`lecture-agent.tab.${side}`, active);
    for (const tab of tabs || []) {
      const btn = el(
        "button",
        {
          class: `side-tab ${tab.id === active ? "active" : ""}`,
          type: FIELD_INPUT.BUTTON,
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
      toolboxEl.appendChild(el("div", { class: "empty", text: "这个 tab 还没有工具" }));
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
          item.description ? el("div", { class: "toolbox-desc", text: item.description }) : null,
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
          renderBlocks(item.blocks.filter((block) => block.type !== BLOCK_TYPE.AGENT_ACTIVITY)),
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
              type: FIELD_INPUT.BUTTON,
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
    const previousPage = container.dataset.pageId;
    const samePage = previousPage === active;
    if (active === "ui:tasks" && samePage) {
      const queue = $(".task-queue", container);
      if (queue) {
        renderTaskQueue(queue);
        return false;
      }
    }
    const key = active
      ? `${active}\u0000${JSON.stringify(blocks || [], (k, v) => (k === "collapsed" ? undefined : v))}`
      : "__welcome__";
    if (samePage && state.pageKey === key) return false;
    if (!samePage && composingWithin(container)) return false;
    const focused = document.activeElement;
    const activityPage =
      blocks?.some(hasActivityBlock) || container.querySelector(".agent-workflow");
    if (
      samePage &&
      !activityPage &&
      container.contains(focused) &&
      ["INPUT", "TEXTAREA", "SELECT"].includes(focused?.tagName)
    )
      return false;
    const qaBefore = new Map();
    if (samePage) {
      const edge = container.getBoundingClientRect().top;
      for (const node of $$(".qa", container))
        qaBefore.set(qaKey(node.dataset.qaPath, node.dataset.qaId), {
          height: node.offsetHeight,
          above: node.getBoundingClientRect().bottom <= edge,
        });
    }
    const scrollTop = container.scrollTop;
    if (!samePage) {
      for (const view of agentViews.values()) {
        if (!container.contains(view.root)) continue;
        saveAgentEditor(view);
        view.positions.set(view.target, executionPosition(view));
        if (focused === view.input || view.wasFocused) view.restoreFocus = true;
        view.editorSuspended = true;
        view.close();
      }
      if (previousPage !== undefined && (state.ui.open_pages || []).includes(previousPage))
        pageSurfaces.set(previousPage, {
          nodes: Array.from(container.childNodes),
          scrollTop,
        });
      container.replaceChildren();
      container.dataset.pageId = active;
      const saved = pageSurfaces.get(active);
      if (saved) for (const node of saved.nodes) container.appendChild(node);
    }
    let complete = true;
    if (!active || !blocks) {
      if (!samePage || !container.children.length)
        container.replaceChildren(
          el("div", { class: "welcome" }, [
            el("div", { class: "welcome-mark", text: "∮" }),
            el("h2", { text: "选择一节课" }),
            el("p", {
              text: "左侧课程树里展开课程，选择录播或直播．按 Ctrl+P 打开命令面板．",
            }),
          ]),
        );
    } else complete = updateBlocks(container, blocks);
    state.pageKey = complete ? key : null;
    container.scrollTop = samePage ? scrollTop : pageSurfaces.get(active)?.scrollTop || 0;
    if (!samePage) {
      for (const view of agentViews.values()) {
        if (!container.contains(view.root)) continue;
        view.editorSuspended = false;
        if (view.input.getClientRects().length) {
          restoreAgentEditor(view, Boolean(view.restoreFocus));
          view.restoreFocus = false;
        }
      }
    }
    let adjust = 0;
    for (const node of $$(".qa", container)) {
      const before = qaBefore.get(qaKey(node.dataset.qaPath, node.dataset.qaId));
      if (before?.above) adjust += node.offsetHeight - before.height;
    }
    if (adjust) container.scrollTop += adjust;
    return true;
  }

  function hasActivityBlock(block) {
    return (
      block.type === BLOCK_TYPE.AGENT_ACTIVITY ||
      block.type === BLOCK_TYPE.QA ||
      (block.type === BLOCK_TYPE.TABS &&
        (block.tabs || []).some((tab) => (tab.blocks || []).some(hasActivityBlock))) ||
      (block.type === BLOCK_TYPE.CARDS &&
        (block.items || []).some((item) => (item.blocks || []).some(hasActivityBlock)))
    );
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
      item("转写", `${session.transcript_chars ?? 0} 字 / ${session.transcript_items ?? 0} 段`),
    );
    bar.appendChild(
      item("板书", `${session.visual_chars ?? 0} 字 / ${session.visual_items ?? 0} 段`),
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
          type: FIELD_INPUT.BUTTON,
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
      toast(`打开页面失败：${err.message}`, TOAST_KIND.ERR);
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
      toast(`关闭页面失败：${err.message}`, TOAST_KIND.ERR);
    }
  }

  function formValues(form) {
    const values = {};
    for (const field of Array.from(form.elements)) {
      if (!field.name || field.type === FIELD_INPUT.SUBMIT || field.type === FIELD_INPUT.BUTTON)
        continue;
      if (field.type === FIELD_INPUT.CHECKBOX) {
        values[field.name] = field.checked;
      } else if (field.dataset.fieldType === SETTING_TYPE.JSON) {
        try {
          values[field.name] = JSON.parse(field.value);
        } catch (_) {
          values[field.name] = field.value;
        }
      } else if (
        field.dataset.fieldType === SETTING_TYPE.INT ||
        field.dataset.fieldType === FIELD_INPUT.NUMBER
      ) {
        values[field.name] = field.value === "" ? null : Number.parseInt(field.value, 10);
      } else if (field.dataset.fieldType === SETTING_TYPE.FLOAT) {
        values[field.name] = field.value === "" ? null : Number.parseFloat(field.value);
      } else {
        values[field.name] = field.value;
      }
    }
    return values;
  }

  async function postAction(
    action,
    values = {},
    pageId = null,
    itemId = null,
    independent = false,
  ) {
    try {
      const request = async () => {
        const result = await api("/api/ui/action", {
          method: "POST",
          body: JSON.stringify({
            action,
            values,
            page_id: pageId || state.ui?.active_page || null,
            item_id: itemId || null,
          }),
        });
        if (result.message) toast(result.message, result.ok ? TOAST_KIND.OK : TOAST_KIND.ERR);
        if (result.open_page) {
          await api(`/api/ui/pages/${encodeURIComponent(result.open_page)}/open`, {
            method: "POST",
          });
        }
        return result;
      };
      if (!independent) return await changeNavigation(request);
      const result = await request();
      if (result?.refresh !== false || result?.open_page) await loadState({ quiet: true });
      return result;
    } catch (err) {
      toast(`操作失败：${err.message}`, TOAST_KIND.ERR);
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
      } catch (_) {}
    }
    const form = btn.closest("form");
    if (form) values = { ...formValues(form), ...values };
    postAction(action, values, btn.dataset.pageId || null, btn.dataset.itemId || null);
  });

  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-action]");
    if (!form) return;
    event.preventDefault();
    postAction(form.dataset.action, formValues(form));
  });

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
        if (data.type === WS_MESSAGE_TYPE.REFRESH) scheduleRefresh();
        else if (data.type === WS_MESSAGE_TYPE.NOTIFY && data.topic === "qa.live") {
          applyQaLive(data.payload || {});
        }
      } catch (_) {}
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
