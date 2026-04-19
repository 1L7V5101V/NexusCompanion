// ── CustomSelect ──────────────────────────────────────────────────────────

class CustomSelect {
  constructor(nativeSelect) {
    this._native = nativeSelect;
    this._open = false;
    this._build();
    document.addEventListener("click", () => this._close());
  }

  _build() {
    const wrapper = document.createElement("div");
    wrapper.className = "custom-select";

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";

    this._label = document.createElement("span");
    trigger.appendChild(this._label);

    const dropdown = document.createElement("div");
    dropdown.className = "cs-dropdown hidden";

    wrapper.appendChild(trigger);
    wrapper.appendChild(dropdown);

    this._native.parentNode.insertBefore(wrapper, this._native);
    this._native.style.display = "none";

    this._wrapper = wrapper;
    this._dropdown = dropdown;

    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      this._open ? this._close() : this._openDropdown();
    });

    this.refresh();
  }

  refresh() {
    const options = Array.from(this._native.options);
    const currentVal = this._native.value;

    this._dropdown.innerHTML = "";
    options.forEach((opt) => {
      const item = document.createElement("div");
      item.className = "cs-option";
      if (opt.value === currentVal) {
        item.classList.add("active");
      }
      item.textContent = opt.textContent;
      item.dataset.value = opt.value;
      item.addEventListener("click", (event) => {
        event.stopPropagation();
        this._select(opt.value);
      });
      this._dropdown.appendChild(item);
    });

    const selected = options.find((o) => o.value === currentVal);
    this._label.textContent = selected ? selected.textContent : (options[0]?.textContent || "");
  }

  _select(value) {
    this._native.value = value;
    this._native.dispatchEvent(new Event("change"));
    this.refresh();
    this._close();
  }

  _openDropdown() {
    this._dropdown.classList.remove("hidden");
    this._wrapper.classList.add("open");
    this._open = true;
  }

  _close() {
    this._dropdown.classList.add("hidden");
    this._wrapper.classList.remove("open");
    this._open = false;
  }
}

// ──────────────────────────────────────────────────────────────────────────

const state = {
  sessions: [],
  sessionMap: new Map(),
  activeSessionKey: null,
  activeMessage: null,
  selectedMessageIds: new Set(),
  sessionSearch: "",
  sessionChannel: "",
  messageSearch: "",
  messageRole: "",
  page: 1,
  pageSize: 25,
  totalMessages: 0,
  messages: [],
  modal: null,
  modalTarget: null,
};

const el = {};

document.addEventListener("DOMContentLoaded", () => {
  bindElements();
  bindEvents();
  el.roleCustomSelect = new CustomSelect(el.msgRoleFilter);
  el.channelCustomSelect = new CustomSelect(el.sessionChannelFilter);
  refreshAll();
});

function bindElements() {
  el.sessionList = document.getElementById("sessionList");
  el.sessionCountTitle = document.getElementById("sessionCountTitle");
  el.sessionSearch = document.getElementById("sessionSearch");
  el.sessionChannelFilter = document.getElementById("sessionChannelFilter");
  el.allMessagesButton = document.getElementById("allMessagesButton");
  el.allMessagesCount = document.getElementById("allMessagesCount");
  el.msgSearch = document.getElementById("msgSearch");
  el.msgRoleFilter = document.getElementById("msgRoleFilter");
  el.activeSessionChip = document.getElementById("activeSessionChip");
  el.activeSessionText = document.getElementById("activeSessionText");
  el.clearSessionFilter = document.getElementById("clearSessionFilter");
  el.batchBar = document.getElementById("batchBar");
  el.batchCount = document.getElementById("batchCount");
  el.batchDeleteButton = document.getElementById("batchDeleteButton");
  el.clearSelectionButton = document.getElementById("clearSelectionButton");
  el.selectAllCheckbox = document.getElementById("selectAllCheckbox");
  el.messageTable = document.getElementById("messageTable");
  el.messageMeta = document.getElementById("messageMeta");
  el.prevPageButton = document.getElementById("prevPageButton");
  el.nextPageButton = document.getElementById("nextPageButton");
  el.pageText = document.getElementById("pageText");
  el.detailPane = document.getElementById("detailPane");
  el.modalBackdrop = document.getElementById("modalBackdrop");
  el.modal = document.getElementById("modal");
}

function bindEvents() {
  el.sessionSearch.addEventListener("input", (event) => {
    state.sessionSearch = event.target.value.trim();
    loadSessions();
  });
  el.sessionChannelFilter.addEventListener("change", (event) => {
    state.sessionChannel = event.target.value;
    loadSessions();
  });
  el.msgSearch.addEventListener("input", (event) => {
    state.messageSearch = event.target.value.trim();
    state.page = 1;
    loadMessages();
  });
  el.msgRoleFilter.addEventListener("change", (event) => {
    state.messageRole = event.target.value;
    state.page = 1;
    loadMessages();
  });
  el.allMessagesButton.addEventListener("click", () => {
    state.activeSessionKey = null;
    state.page = 1;
    state.activeMessage = null;
    state.selectedMessageIds.clear();
    loadMessages();
    render();
  });
  el.clearSessionFilter.addEventListener("click", () => {
    state.activeSessionKey = null;
    state.page = 1;
    state.activeMessage = null;
    loadMessages();
    render();
  });
  el.batchDeleteButton.addEventListener("click", () => {
    openConfirmModal({
      title: "批量删除消息",
      text: `确定删除选中的 ${state.selectedMessageIds.size} 条消息吗？此操作不可撤销。`,
      danger: true,
      confirmText: "删除",
      onConfirm: async () => {
        await api("/api/dashboard/messages/batch-delete", {
          method: "POST",
          body: JSON.stringify({ ids: [...state.selectedMessageIds] }),
        });
        state.selectedMessageIds.clear();
        state.activeMessage = null;
        closeModal();
        await refreshAll();
      },
    });
  });
  el.clearSelectionButton.addEventListener("click", () => {
    state.selectedMessageIds.clear();
    render();
  });
  el.selectAllCheckbox.addEventListener("change", (event) => {
    if (event.target.checked) {
      state.messages.forEach((message) => state.selectedMessageIds.add(message.id));
    } else {
      state.messages.forEach((message) => state.selectedMessageIds.delete(message.id));
    }
    render();
  });
  el.prevPageButton.addEventListener("click", () => {
    if (state.page <= 1) {
      return;
    }
    state.page -= 1;
    loadMessages();
  });
  el.nextPageButton.addEventListener("click", () => {
    if (state.page >= pageCount()) {
      return;
    }
    state.page += 1;
    loadMessages();
  });
  el.modalBackdrop.addEventListener("click", closeModal);
}

async function refreshAll() {
  await loadSessions();
  await loadMessages();
  render();
}

async function loadSessions() {
  const params = new URLSearchParams();
  if (state.sessionSearch) {
    params.set("q", state.sessionSearch);
  }
  if (state.sessionChannel) {
    params.set("channel", state.sessionChannel);
  }
  params.set("page_size", "200");

  const payload = await api(`/api/dashboard/sessions?${params.toString()}`);
  state.sessions = payload.items;
  state.sessionMap = new Map(payload.items.map((session) => [session.key, session]));
  if (state.activeSessionKey && !state.sessionMap.has(state.activeSessionKey)) {
    state.activeSessionKey = null;
    state.activeMessage = null;
  }
  renderSessionFilters();
  renderSessions();
}

async function loadMessages() {
  const params = new URLSearchParams();
  if (state.activeSessionKey) {
    params.set("session_key", state.activeSessionKey);
  }
  if (state.messageSearch) {
    params.set("q", state.messageSearch);
  }
  if (state.messageRole) {
    params.set("role", state.messageRole);
  }
  params.set("page", String(state.page));
  params.set("page_size", String(state.pageSize));
  params.set("sort_order", "desc");

  const payload = await api(`/api/dashboard/messages?${params.toString()}`);
  state.messages = payload.items;
  state.totalMessages = payload.total;
  if (
    state.activeMessage &&
    !state.messages.find((message) => message.id === state.activeMessage.id)
  ) {
    state.activeMessage = null;
  }
  renderMessages();
  renderDetail();
}

function render() {
  renderSessions();
  renderMessages();
  renderDetail();
}

function renderSessionFilters() {
  const channels = [...new Set(state.sessions.map((session) => channelOf(session.key)))];
  const current = state.sessionChannel;
  el.sessionChannelFilter.innerHTML = '<option value="">全部 channel</option>';
  channels.forEach((channel) => {
    const option = document.createElement("option");
    option.value = channel;
    option.textContent = channel;
    if (channel === current) {
      option.selected = true;
    }
    el.sessionChannelFilter.appendChild(option);
  });
  el.channelCustomSelect?.refresh();
}

function renderSessions() {
  el.sessionCountTitle.textContent = `${state.sessions.length} 个会话`;
  el.allMessagesCount.textContent = String(totalSessionMessages());
  el.allMessagesButton.classList.toggle("active", !state.activeSessionKey);
  el.sessionList.innerHTML = "";

  state.sessions.forEach((session) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "session-item";
    if (session.key === state.activeSessionKey) {
      item.classList.add("active");
    }
    item.innerHTML = `
      <div class="session-main">
        <div>
          <div class="session-key">${escapeHtml(session.key)}</div>
          <div class="session-meta">
            <span class="channel-pill" style="${channelStyle(session.key)}">${escapeHtml(channelOf(session.key))}</span>
            <span>${escapeHtml(relativeTime(session.updated_at))}</span>
          </div>
        </div>
        <div class="session-count">${session.message_count}</div>
      </div>
    `;
    item.addEventListener("click", async () => {
      state.activeSessionKey = session.key;
      state.page = 1;
      state.activeMessage = null;
      state.selectedMessageIds.clear();
      await loadMessages();
      render();
    });
    el.sessionList.appendChild(item);
  });

  el.activeSessionChip.classList.toggle("hidden", !state.activeSessionKey);
  el.activeSessionText.textContent = state.activeSessionKey || "";
}

function renderMessages() {
  el.messageTable.innerHTML = "";
  const selectedOnPage = state.messages.filter((message) =>
    state.selectedMessageIds.has(message.id)
  ).length;
  el.selectAllCheckbox.checked =
    state.messages.length > 0 && selectedOnPage === state.messages.length;
  el.batchBar.classList.toggle("hidden", state.selectedMessageIds.size === 0);
  el.batchCount.textContent = `已选 ${state.selectedMessageIds.size} 条`;

  if (!state.messages.length) {
    el.messageTable.innerHTML = '<div class="empty-state">没有匹配的消息。</div>';
  }

  state.messages.forEach((message) => {
    const row = document.createElement("div");
    row.className = "table-row";
    if (state.activeMessage && state.activeMessage.id === message.id) {
      row.classList.add("active");
    }
    if (state.selectedMessageIds.has(message.id)) {
      row.classList.add("selected");
    }
    const sessionDisplay = formatSessionKeyForTable(message.session_key);
    row.innerHTML = `
      <label class="checkbox-cell"><input data-select-id="${escapeHtml(message.id)}" type="checkbox" ${state.selectedMessageIds.has(message.id) ? "checked" : ""}></label>
      <div class="mono cell-session" title="${escapeHtml(message.session_key)}">${escapeHtml(sessionDisplay)}</div>
      <div class="mono cell-seq" title="#${message.seq}">#${message.seq}</div>
      <div class="content-preview">${escapeHtml(stripMarkdown(message.content || ""))}</div>
      <div class="mono cell-time" title="${escapeHtml(message.timestamp)}">${escapeHtml(shortTs(message.timestamp))}</div>
      <div><span class="role-pill" style="${roleStyle(message.role)}">${escapeHtml(message.role)}</span></div>
      <div class="table-actions">
        <button class="icon-btn" data-edit-id="${escapeHtml(message.id)}" type="button">✎</button>
        <button class="icon-btn" data-delete-id="${escapeHtml(message.id)}" type="button">✕</button>
      </div>
    `;
    row.addEventListener("click", (event) => {
      if (event.target.closest("button") || event.target.closest("input")) {
        return;
      }
      state.activeMessage = message;
      renderDetail();
      renderMessages();
    });
    el.messageTable.appendChild(row);
  });

  el.messageTable.querySelectorAll("[data-select-id]").forEach((input) => {
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", (event) => {
      const messageId = event.target.getAttribute("data-select-id");
      if (event.target.checked) {
        state.selectedMessageIds.add(messageId);
      } else {
        state.selectedMessageIds.delete(messageId);
      }
      renderMessages();
    });
  });

  el.messageTable.querySelectorAll("[data-edit-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const message = state.messages.find(
        (item) => item.id === button.getAttribute("data-edit-id")
      );
      openMessageEditModal(message);
    });
  });

  el.messageTable.querySelectorAll("[data-delete-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const message = state.messages.find(
        (item) => item.id === button.getAttribute("data-delete-id")
      );
      openMessageDeleteModal(message);
    });
  });

  const sessionText = state.activeSessionKey ? ` · session: ${state.activeSessionKey}` : "";
  el.messageMeta.textContent = `共 ${state.totalMessages} 条${sessionText}`;
  el.pageText.textContent = `${state.page} / ${pageCount()}`;
  el.prevPageButton.disabled = state.page <= 1;
  el.nextPageButton.disabled = state.page >= pageCount();
}

function renderDetail() {
  if (!state.activeMessage) {
    el.detailPane.innerHTML = `
      <div class="detail-empty">
        <div class="detail-empty-title">消息详情</div>
        <div class="detail-empty-text">点开一条消息后，这里会显示完整内容、JSON 字段和所属 session 信息。</div>
      </div>
    `;
    return;
  }

  const session = state.sessionMap.get(state.activeMessage.session_key);
  el.detailPane.innerHTML = `
    <div class="detail-wrap">
      <div class="detail-toolbar">
        <div>
          <div class="detail-title">消息详情</div>
          <div class="detail-subtext">${escapeHtml(state.activeMessage.session_key)} · #${state.activeMessage.seq}</div>
        </div>
        <div class="table-actions">
          <button class="ghost" id="detailEditButton" type="button">编辑</button>
          <button class="danger-ghost" id="detailDeleteButton" type="button">删除</button>
        </div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Content</div>
        <div class="detail-content">${renderMarkdown(state.activeMessage.content)}</div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Fields</div>
        <div class="detail-grid">
          ${detailRow("id", `<code>${escapeHtml(state.activeMessage.id)}</code>`)}
          ${detailRow("session_key", `<code>${escapeHtml(state.activeMessage.session_key)}</code>`)}
          ${detailRow("seq", `<code>${state.activeMessage.seq}</code>`)}
          ${detailRow("role", escapeHtml(state.activeMessage.role))}
          ${detailRow("timestamp", `<code>${escapeHtml(state.activeMessage.timestamp)}</code>`)}
        </div>
      </div>

      ${
        state.activeMessage.tool_chain
          ? `<div class="detail-block"><div class="detail-label">Tool Chain</div>${jvPlaceholder(state.activeMessage.tool_chain)}</div>`
          : ""
      }
      ${
        extraOf(state.activeMessage)
          ? `<div class="detail-block"><div class="detail-label">Extra</div>${jvPlaceholder(extraOf(state.activeMessage))}</div>`
          : ""
      }
      ${
        session
          ? `
        <div class="detail-block">
          <div class="detail-label">Session</div>
          <div class="detail-grid">
            ${detailRow("key", `<code>${escapeHtml(session.key)}</code>`)}
            ${detailRow("message_count", String(session.message_count))}
            ${detailRow("updated_at", `<code>${escapeHtml(session.updated_at)}</code>`)}
          </div>
          ${jvPlaceholder(session.metadata || {})}
          <div class="modal-actions">
            <button class="ghost" id="sessionEditButton" type="button">编辑 Session</button>
            <button class="danger-ghost" id="sessionDeleteButton" type="button">删除 Session</button>
          </div>
        </div>
      `
          : ""
      }
    </div>
  `;

  document
    .getElementById("detailEditButton")
    .addEventListener("click", () => openMessageEditModal(state.activeMessage));
  document
    .getElementById("detailDeleteButton")
    .addEventListener("click", () => openMessageDeleteModal(state.activeMessage));

  if (session) {
    document
      .getElementById("sessionEditButton")
      .addEventListener("click", () => openSessionEditModal(session));
    document
      .getElementById("sessionDeleteButton")
      .addEventListener("click", () => openSessionDeleteModal(session));
  }

  attachJsonViewers(el.detailPane);
}

function openMessageEditModal(message) {
  const extra = extraOf(message);
  const toolChain = message.tool_chain || null;
  const html = `
    <div class="modal-title">编辑消息</div>
    <div class="modal-sub">直接修改原始 message 行。适合修正 content、role 和 JSON 字段。</div>
    <div class="form-grid">
      <label class="form-label">role
        <select id="modalRole">
          ${["user", "assistant", "system", "tool"]
            .map((role) => `<option value="${role}" ${message.role === role ? "selected" : ""}>${role}</option>`)
            .join("")}
        </select>
      </label>
      <label class="form-label">content
        <textarea id="modalContent" rows="8">${escapeHtml(message.content || "")}</textarea>
      </label>
      <label class="form-label">tool_chain JSON
        <textarea id="modalToolChain" rows="8">${escapeHtml(
          toolChain ? JSON.stringify(toolChain, null, 2) : ""
        )}</textarea>
      </label>
      <label class="form-label">extra JSON
        <textarea id="modalExtra" rows="8">${escapeHtml(
          extra ? JSON.stringify(extra, null, 2) : ""
        )}</textarea>
      </label>
    </div>
    <div class="modal-actions">
      <button class="ghost" id="modalCancel" type="button">取消</button>
      <button class="primary" id="modalSubmit" type="button">保存</button>
    </div>
  `;
  openModal(html, async () => {
    const payload = {
      role: document.getElementById("modalRole").value,
      content: document.getElementById("modalContent").value,
      tool_chain: parseJsonField("modalToolChain"),
      extra: parseJsonField("modalExtra"),
    };
    await api(`/api/dashboard/messages/${encodePath(message.id)}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    closeModal();
    await refreshAll();
  });
}

function openSessionEditModal(session) {
  const html = `
    <div class="modal-title">编辑 Session</div>
    <div class="modal-sub">这版只开放必要字段，避免手工改坏主键和创建时间。</div>
    <div class="form-grid">
      <label class="form-label">metadata JSON
        <textarea id="modalSessionMetadata" rows="10">${escapeHtml(JSON.stringify(session.metadata || {}, null, 2))}</textarea>
      </label>
      <label class="form-label">last_consolidated
        <input id="modalSessionConsolidated" type="number" value="${session.last_consolidated ?? 0}">
      </label>
      <label class="form-label">last_user_at
        <input id="modalSessionLastUser" type="text" value="${escapeHtml(session.last_user_at || "")}">
      </label>
      <label class="form-label">last_proactive_at
        <input id="modalSessionLastProactive" type="text" value="${escapeHtml(session.last_proactive_at || "")}">
      </label>
    </div>
    <div class="modal-actions">
      <button class="ghost" id="modalCancel" type="button">取消</button>
      <button class="primary" id="modalSubmit" type="button">保存</button>
    </div>
  `;
  openModal(html, async () => {
    await api(`/api/dashboard/sessions/${encodePath(session.key)}`, {
      method: "PATCH",
      body: JSON.stringify({
        metadata: parseJsonField("modalSessionMetadata"),
        last_consolidated: Number(document.getElementById("modalSessionConsolidated").value || 0),
        last_user_at: document.getElementById("modalSessionLastUser").value || null,
        last_proactive_at: document.getElementById("modalSessionLastProactive").value || null,
      }),
    });
    closeModal();
    await refreshAll();
  });
}

function openMessageDeleteModal(message) {
  openConfirmModal({
    title: "删除消息",
    text: `确定删除消息 #${message.seq} 吗？此操作不可撤销。`,
    danger: true,
    confirmText: "删除",
    onConfirm: async () => {
      await api(`/api/dashboard/messages/${encodePath(message.id)}`, {
        method: "DELETE",
      });
      if (state.activeMessage && state.activeMessage.id === message.id) {
        state.activeMessage = null;
      }
      closeModal();
      await refreshAll();
    },
  });
}

function openSessionDeleteModal(session) {
  openConfirmModal({
    title: "删除 Session",
    text: `确定删除 ${session.key} 吗？该 session 下所有消息会一起删除。`,
    danger: true,
    confirmText: "删除",
    onConfirm: async () => {
      await api(`/api/dashboard/sessions/${encodePath(session.key)}?cascade=true`, {
        method: "DELETE",
      });
      if (state.activeSessionKey === session.key) {
        state.activeSessionKey = null;
      }
      state.activeMessage = null;
      closeModal();
      await refreshAll();
    },
  });
}

function openConfirmModal({ title, text, confirmText, danger, onConfirm }) {
  const html = `
    <div class="modal-title">${escapeHtml(title)}</div>
    <div class="modal-sub">${escapeHtml(text)}</div>
    <div class="modal-actions">
      <button class="ghost" id="modalCancel" type="button">取消</button>
      <button class="${danger ? "danger-ghost" : "primary"}" id="modalSubmit" type="button">${escapeHtml(confirmText)}</button>
    </div>
  `;
  openModal(html, onConfirm);
}

function openModal(html, onSubmit) {
  el.modal.innerHTML = html;
  el.modal.classList.remove("hidden");
  el.modalBackdrop.classList.remove("hidden");
  document.getElementById("modalCancel").addEventListener("click", closeModal);
  document.getElementById("modalSubmit").addEventListener("click", onSubmit);
}

function closeModal() {
  el.modal.classList.add("hidden");
  el.modalBackdrop.classList.add("hidden");
  el.modal.innerHTML = "";
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
    },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    alert(payload.detail || `请求失败: ${response.status}`);
    throw new Error(payload.detail || `request failed: ${response.status}`);
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function pageCount() {
  return Math.max(1, Math.ceil(state.totalMessages / state.pageSize));
}

function totalSessionMessages() {
  return state.sessions.reduce((sum, session) => sum + (session.message_count || 0), 0);
}

function roleStyle(role) {
  const styles = {
    user: "background:#f3ddd2;color:#bc5c38;",
    assistant: "background:#ddf0e7;color:#2f7d62;",
    system: "background:#fbf0c9;color:#8b6b09;",
    tool: "background:#dceaf6;color:#276489;",
  };
  return styles[role] || "background:#ece6db;color:#6f6255;";
}

function channelStyle(key) {
  const styles = {
    telegram: "background:#dceaf6;color:#276489;",
    cli: "background:#ece6db;color:#6f6255;",
    qq: "background:#efe0f7;color:#74488d;",
    scheduler: "background:#fbf0c9;color:#8b6b09;",
  };
  return styles[channelOf(key)] || "background:#ece6db;color:#6f6255;";
}

function channelOf(key) {
  return String(key || "").split(":")[0] || "unknown";
}

function formatSessionKeyForTable(key) {
  const raw = String(key || "");
  const parts = raw.split(":");
  if (parts.length < 2) {
    return raw;
  }
  const channel = parts[0];
  const tail = parts.slice(1).join(":");
  if (tail.length <= 10) {
    return `${channel}:${tail}`;
  }
  return `${channel}:${tail.slice(0, 6)}...${tail.slice(-4)}`;
}

function relativeTime(value) {
  if (!value) {
    return "未更新";
  }
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return value;
  }
  const diff = Date.now() - time;
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < hour) {
    return `${Math.max(1, Math.round(diff / minute))} 分钟前`;
  }
  if (diff < day) {
    return `${Math.round(diff / hour)} 小时前`;
  }
  return `${Math.round(diff / day)} 天前`;
}

function shortTs(value) {
  if (!value) {
    return "-";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return `${date.getMonth() + 1}-${String(date.getDate()).padStart(2, "0")} ${String(
    date.getHours()
  ).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

// ── JSON tree viewer ──────────────────────────────────────────────────────

function _jnSpan(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

function _renderJNode(data, container, depth) {
  // Auto-parse nested JSON strings (e.g. result fields stored as escaped JSON)
  if (typeof data === "string") {
    const trimmed = data.trim();
    if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.length > 2) {
      try { data = JSON.parse(data); } catch {}
    }
  }

  if (data === null || data === undefined) {
    container.appendChild(_jnSpan("jt-null", "null"));
    return;
  }
  if (typeof data === "boolean") {
    container.appendChild(_jnSpan("jt-bool", String(data)));
    return;
  }
  if (typeof data === "number") {
    container.appendChild(_jnSpan("jt-num", String(data)));
    return;
  }
  if (typeof data === "string") {
    container.appendChild(_jnSpan("jt-str", JSON.stringify(data)));
    return;
  }

  const isArr = Array.isArray(data);
  const keys = isArr ? [...data.keys()] : Object.keys(data);

  if (keys.length === 0) {
    container.appendChild(_jnSpan("jt-null", isArr ? "[]" : "{}"));
    return;
  }

  const defaultOpen = depth < 1;
  const toggle = document.createElement("span");
  toggle.className = "jt-toggle";

  const updateToggleText = (open) => {
    toggle.textContent = open
      ? (isArr ? `▾ [${keys.length}]` : `▾ {${keys.length}}`)
      : (isArr ? `▸ [${keys.length}]` : `▸ {…}`);
  };
  updateToggleText(defaultOpen);
  container.appendChild(toggle);

  const children = document.createElement("div");
  children.className = "jt-children";
  if (!defaultOpen) children.style.display = "none";

  keys.forEach((k) => {
    const row = document.createElement("div");
    row.className = "jt-row";
    if (!isArr) {
      row.appendChild(_jnSpan("jt-key", String(k)));
      row.appendChild(_jnSpan("jt-colon", ": "));
    }
    _renderJNode(isArr ? data[k] : data[k], row, depth + 1);
    children.appendChild(row);
  });
  container.appendChild(children);

  toggle.addEventListener("click", () => {
    const nowOpen = children.style.display !== "none";
    children.style.display = nowOpen ? "none" : "";
    updateToggleText(!nowOpen);
  });
}

function makeJsonViewer(data) {
  const box = document.createElement("div");
  box.className = "json-tree";
  _renderJNode(data, box, 0);
  return box;
}

// Replace all <div data-jv="..."> placeholders with interactive viewers.
function attachJsonViewers(container) {
  container.querySelectorAll("[data-jv]").forEach((host) => {
    try {
      const raw = host.getAttribute("data-jv");
      const data = JSON.parse(decodeURIComponent(raw));
      host.replaceWith(makeJsonViewer(data));
    } catch {}
  });
}

function jvPlaceholder(data) {
  return `<div data-jv="${encodeURIComponent(JSON.stringify(data))}"></div>`;
}

// ──────────────────────────────────────────────────────────────────────────

function stripMarkdown(text) {
  return String(text ?? "")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/\*(.+?)\*/g, "$1")
    .replace(/__(.+?)__/g, "$1")
    .replace(/_(.+?)_/g, "$1")
    .replace(/~~(.+?)~~/g, "$1")
    .replace(/`{1,3}[\s\S]*?`{1,3}/g, "")
    .replace(/\[(.+?)\]\(.+?\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^>\s*/gm, "")
    .replace(/\n+/g, " ")
    .trim();
}

function renderMarkdown(text) {
  const raw = String(text ?? "").trim();
  if (!raw) {
    return '<span class="detail-subtext">empty</span>';
  }
  if (typeof marked !== "undefined") {
    return marked.parse(raw, { breaks: true, gfm: true });
  }
  return `<span style="white-space:pre-wrap">${escapeHtml(raw)}</span>`;
}

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function encodePath(value) {
  return encodeURIComponent(value).replaceAll("%2F", "/");
}

function parseJsonField(id) {
  const raw = document.getElementById(id).value.trim();
  if (!raw) {
    return null;
  }
  return JSON.parse(raw);
}

function detailRow(label, value) {
  return `<div class="detail-row"><div class="detail-label">${escapeHtml(label)}</div><div>${value}</div></div>`;
}

function extraOf(message) {
  const known = new Set(["id", "session_key", "seq", "role", "content", "timestamp", "tool_chain"]);
  const extra = {};
  Object.entries(message || {}).forEach(([key, value]) => {
    if (!known.has(key)) {
      extra[key] = value;
    }
  });
  return Object.keys(extra).length ? extra : null;
}
