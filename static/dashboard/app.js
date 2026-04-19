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

    const selected = options.find((option) => option.value === currentVal);
    this._label.textContent = selected
      ? selected.textContent
      : options[0]?.textContent || "";
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

const state = {
  viewMode: "sessions",
  navOpen: {
    sessions: true,
    memory: false,
  },
  sessions: [],
  sessionMap: new Map(),
  activeSessionKey: null,
  activeSession: null,
  selectedMessageIds: new Set(),
  sessionSearch: "",
  sessionChannel: "",
  messageSearch: "",
  messageRole: "",
  page: 1,
  pageSize: 25,
  totalMessages: 0,
  messages: [],
  activeMessage: null,
  memories: [],
  memoryMap: new Map(),
  memoryTypeCounts: [],
  activeMemoryId: null,
  activeMemoryDetail: null,
  activeMemorySimilar: [],
  selectedMemoryIds: new Set(),
  memorySearch: "",
  memoryType: "",
  memoryStatus: "",
  memoryScopeFilter: null,
  memoryPage: 1,
  memoryPageSize: 25,
  totalMemories: 0,
};

const el = {};

document.addEventListener("DOMContentLoaded", async () => {
  bindElements();
  bindEvents();
  initCustomSelects();
  await refreshAll();
});

window.dashboardSelectMemoryType = async (memoryType) => {
  state.viewMode = "memory";
  state.memoryType = memoryType || "";
  el.memoryTypeFilter.value = state.memoryType;
  el.memoryTypeCustomSelect.refresh();
  state.activeMemoryId = null;
  state.activeMemoryDetail = null;
  state.activeMemorySimilar = [];
  state.memoryPage = 1;
  await loadMemoriesAndSidebar();
  render();
};

function bindElements() {
  el.sessionList = document.getElementById("sessionList");
  el.sessionCountTitle = document.getElementById("sessionCountTitle");
  el.sessionSearch = document.getElementById("sessionSearch");
  el.sessionSidebarFilters = document.getElementById("sessionSidebarFilters");
  el.sessionChannelFilter = document.getElementById("sessionChannelFilter");
  el.allMessagesButton = document.getElementById("allMessagesButton");
  el.allMessagesCount = document.getElementById("allMessagesCount");
  el.allSessionsCount = document.getElementById("allSessionsCount");
  el.memoryTypeList = document.getElementById("memoryTypeList");
  el.memoryCountBadge = document.getElementById("memoryCountBadge");
  el.allMemoriesButton = document.getElementById("allMemoriesButton");
  el.allMemoriesCount = document.getElementById("allMemoriesCount");
  el.msgSearch = document.getElementById("msgSearch");
  el.msgRoleFilter = document.getElementById("msgRoleFilter");
  el.memorySearch = document.getElementById("memorySearch");
  el.memoryTypeFilter = document.getElementById("memoryTypeFilter");
  el.memoryStatusFilter = document.getElementById("memoryStatusFilter");
  el.messageFilters = document.getElementById("messageFilters");
  el.memoryFilters = document.getElementById("memoryFilters");
  el.activeSessionChip = document.getElementById("activeSessionChip");
  el.activeSessionText = document.getElementById("activeSessionText");
  el.clearSessionFilter = document.getElementById("clearSessionFilter");
  el.activeMemoryScopeChip = document.getElementById("activeMemoryScopeChip");
  el.activeMemoryScopeText = document.getElementById("activeMemoryScopeText");
  el.clearMemoryScopeFilter = document.getElementById("clearMemoryScopeFilter");
  el.batchBar = document.getElementById("batchBar");
  el.batchCount = document.getElementById("batchCount");
  el.batchDeleteButton = document.getElementById("batchDeleteButton");
  el.clearSelectionButton = document.getElementById("clearSelectionButton");
  el.selectAllCheckbox = null;
  el.tableHead = document.getElementById("tableHead");
  el.messageTable = document.getElementById("messageTable");
  el.messageMeta = document.getElementById("messageMeta");
  el.prevPageButton = document.getElementById("prevPageButton");
  el.nextPageButton = document.getElementById("nextPageButton");
  el.pageText = document.getElementById("pageText");
  el.detailPane = document.getElementById("detailPane");
  el.modalBackdrop = document.getElementById("modalBackdrop");
  el.modal = document.getElementById("modal");
  el.viewChipLabel = document.getElementById("viewChipLabel");
  el.sessionsNavGroup = document.getElementById("sessionsNavGroup");
  el.sessionsNavToggle = document.getElementById("sessionsNavToggle");
  el.sessionsNavBody = document.getElementById("sessionsNavBody");
  el.memoryNavGroup = document.getElementById("memoryNavGroup");
  el.memoryNavToggle = document.getElementById("memoryNavToggle");
  el.memoryNavBody = document.getElementById("memoryNavBody");
}

function initCustomSelects() {
  el.roleCustomSelect = new CustomSelect(el.msgRoleFilter);
  el.channelCustomSelect = new CustomSelect(el.sessionChannelFilter);
  el.memoryTypeCustomSelect = new CustomSelect(el.memoryTypeFilter);
  el.memoryStatusCustomSelect = new CustomSelect(el.memoryStatusFilter);
}

function bindEvents() {
  el.sessionSearch.addEventListener("input", async (event) => {
    state.sessionSearch = event.target.value.trim();
    await loadSessions();
    render();
  });

  el.sessionChannelFilter.addEventListener("change", async (event) => {
    state.sessionChannel = event.target.value;
    await loadSessions();
    render();
  });

  el.msgSearch.addEventListener("input", async (event) => {
    state.messageSearch = event.target.value.trim();
    state.page = 1;
    await loadMessages();
    render();
  });

  el.msgRoleFilter.addEventListener("change", async (event) => {
    state.messageRole = event.target.value;
    state.page = 1;
    await loadMessages();
    render();
  });

  el.memorySearch.addEventListener("input", async (event) => {
    state.memorySearch = event.target.value.trim();
    state.memoryPage = 1;
    await loadMemoriesAndSidebar();
    render();
  });

  el.memoryTypeFilter.addEventListener("change", async (event) => {
    state.memoryType = event.target.value;
    state.memoryPage = 1;
    await loadMemoriesAndSidebar();
    render();
  });

  el.memoryStatusFilter.addEventListener("change", async (event) => {
    state.memoryStatus = event.target.value;
    state.memoryPage = 1;
    await loadMemoriesAndSidebar();
    render();
  });

  el.allMessagesButton.addEventListener("click", async () => {
    state.viewMode = "sessions";
    state.activeSessionKey = null;
    state.activeSession = null;
    state.activeMessage = null;
    state.selectedMessageIds.clear();
    state.page = 1;
    await loadMessages();
    render();
  });

  el.allMemoriesButton.addEventListener("click", async () => {
    state.viewMode = "memory";
    state.memoryType = "";
    state.memoryTypeFilter.value = "";
    el.memoryTypeCustomSelect.refresh();
    state.activeMemoryId = null;
    state.activeMemoryDetail = null;
    state.activeMemorySimilar = [];
    state.selectedMemoryIds.clear();
    state.memoryPage = 1;
    await loadMemoriesAndSidebar();
    render();
  });

  el.clearSessionFilter.addEventListener("click", async () => {
    state.activeSessionKey = null;
    state.activeSession = null;
    state.activeMessage = null;
    state.page = 1;
    await loadMessages();
    render();
  });

  el.clearMemoryScopeFilter.addEventListener("click", async () => {
    state.memoryScopeFilter = null;
    state.activeMemoryId = null;
    state.activeMemoryDetail = null;
    state.activeMemorySimilar = [];
    state.memoryPage = 1;
    await loadMemoriesAndSidebar();
    render();
  });

  el.batchDeleteButton.addEventListener("click", () => {
    if (state.viewMode === "memory") {
      openConfirmModal({
        title: "批量删除记忆",
        text: `确定删除选中的 ${state.selectedMemoryIds.size} 条 memory 吗？此操作不可撤销。`,
        danger: true,
        confirmText: "删除",
        onConfirm: async () => {
          await api("/api/dashboard/memories/batch-delete", {
            method: "POST",
            body: JSON.stringify({ ids: [...state.selectedMemoryIds] }),
          });
          state.selectedMemoryIds.clear();
          state.activeMemoryId = null;
          state.activeMemoryDetail = null;
          state.activeMemorySimilar = [];
          closeModal();
          await refreshCurrentView();
        },
      });
      return;
    }

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
        await refreshCurrentView();
      },
    });
  });

  el.clearSelectionButton.addEventListener("click", () => {
    if (state.viewMode === "memory") {
      state.selectedMemoryIds.clear();
    } else {
      state.selectedMessageIds.clear();
    }
    render();
  });

  el.prevPageButton.addEventListener("click", async () => {
    if (state.viewMode === "memory") {
      if (state.memoryPage <= 1) {
        return;
      }
      state.memoryPage -= 1;
      await loadMemories();
      render();
      return;
    }

    if (state.page <= 1) {
      return;
    }
    state.page -= 1;
    await loadMessages();
    render();
  });

  el.nextPageButton.addEventListener("click", async () => {
    if (state.viewMode === "memory") {
      if (state.memoryPage >= pageCount()) {
        return;
      }
      state.memoryPage += 1;
      await loadMemories();
      render();
      return;
    }

    if (state.page >= pageCount()) {
      return;
    }
    state.page += 1;
    await loadMessages();
    render();
  });

  el.modalBackdrop.addEventListener("click", closeModal);
  el.sessionsNavToggle.addEventListener("click", async () => {
    await toggleNav("sessions");
  });
  el.memoryNavToggle.addEventListener("click", async () => {
    await toggleNav("memory");
  });
  el.memoryTypeList.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-memory-type]");
    if (!button) {
      return;
    }
    await window.dashboardSelectMemoryType(
      button.getAttribute("data-memory-type") || ""
    );
  });
}

async function toggleNav(kind) {
  if (state.viewMode !== kind) {
    state.viewMode = kind;
    state.navOpen[kind] = true;
    if (kind === "sessions") {
      await loadMessages();
    } else {
      await loadMemoriesAndSidebar();
    }
    render();
    return;
  }

  state.navOpen[kind] = !state.navOpen[kind];
  renderNav();
}

async function refreshAll() {
  await loadSessions();
  await loadMemorySidebar();
  await refreshCurrentView();
}

async function refreshCurrentView() {
  if (state.viewMode === "memory") {
    await loadMemories();
  } else {
    await loadMessages();
  }
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
  state.activeSession = state.activeSessionKey
    ? state.sessionMap.get(state.activeSessionKey) || null
    : null;

  if (state.activeSessionKey && !state.sessionMap.has(state.activeSessionKey)) {
    state.activeSessionKey = null;
    state.activeSession = null;
    state.activeMessage = null;
  }

  renderSessionFilters();
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
}

async function loadMemorySidebar() {
  const memoryTypes = ["procedure", "preference", "event", "profile"];
  const baseParams = new URLSearchParams();
  if (state.memorySearch) {
    baseParams.set("q", state.memorySearch);
  }
  if (state.memoryStatus) {
    baseParams.set("status", state.memoryStatus);
  }
  if (state.memoryScopeFilter?.channel) {
    baseParams.set("scope_channel", state.memoryScopeFilter.channel);
  }
  if (state.memoryScopeFilter?.chatId) {
    baseParams.set("scope_chat_id", state.memoryScopeFilter.chatId);
  }
  baseParams.set("page_size", "1");
  baseParams.set("sort_by", "updated_at");
  baseParams.set("sort_order", "desc");

  const requests = [];
  for (const memoryType of memoryTypes) {
    const params = new URLSearchParams(baseParams);
    params.set("memory_type", memoryType);
    const payload = await api(`/api/dashboard/memories?${params.toString()}`);
    requests.push({
      memory_type: memoryType,
      total: payload.total || 0,
    });
  }
  state.memoryTypeCounts = requests.filter((item) => item.total > 0);
  if (state.viewMode !== "memory") {
    state.totalMemories = requests.reduce((sum, item) => sum + item.total, 0);
  }
}

async function loadMemoriesAndSidebar() {
  await loadMemories();
  await loadMemorySidebar();
}

async function loadMemories() {
  const params = new URLSearchParams();
  if (state.memorySearch) {
    params.set("q", state.memorySearch);
  }
  if (state.memoryType) {
    params.set("memory_type", state.memoryType);
  }
  if (state.memoryStatus) {
    params.set("status", state.memoryStatus);
  }
  if (state.memoryScopeFilter?.channel) {
    params.set("scope_channel", state.memoryScopeFilter.channel);
  }
  if (state.memoryScopeFilter?.chatId) {
    params.set("scope_chat_id", state.memoryScopeFilter.chatId);
  }
  params.set("page", String(state.memoryPage));
  params.set("page_size", String(state.memoryPageSize));
  params.set("sort_by", "updated_at");
  params.set("sort_order", "desc");

  const payload = await api(`/api/dashboard/memories?${params.toString()}`);
  state.memories = payload.items;
  state.memoryMap = new Map(payload.items.map((item) => [item.id, item]));
  state.totalMemories = payload.total;

  if (state.activeMemoryId && !state.memoryMap.has(state.activeMemoryId)) {
    state.activeMemoryId = null;
    state.activeMemoryDetail = null;
    state.activeMemorySimilar = [];
  }
}

async function selectSession(session) {
  state.viewMode = "sessions";
  state.activeSessionKey = session.key;
  state.activeSession = session;
  state.activeMessage = null;
  state.selectedMessageIds.clear();
  state.page = 1;
  await loadMessages();
  render();
}

async function selectMemory(memoryId) {
  state.viewMode = "memory";
  state.activeMemoryId = memoryId;
  state.selectedMemoryIds.clear();
  await loadMemoryDetail(memoryId);
  render();
}

async function loadMemoryDetail(memoryId) {
  const [detail, similar] = await Promise.all([
    api(`/api/dashboard/memories/${encodePath(memoryId)}`),
    api(`/api/dashboard/memories/${encodePath(memoryId)}/similar?top_k=6`).catch(
      () => ({ items: [] })
    ),
  ]);
  state.activeMemoryDetail = detail;
  state.activeMemorySimilar = similar.items || [];
}

function render() {
  renderNav();
  renderSidebar();
  renderTopbar();
  renderTableHead();
  renderRows();
  renderDetail();
}

function renderNav() {
  el.sessionsNavGroup.classList.toggle("active", state.viewMode === "sessions");
  el.memoryNavGroup.classList.toggle("active", state.viewMode === "memory");
  toggleNavBody(el.sessionsNavBody, el.sessionsNavToggle, state.navOpen.sessions);
  toggleNavBody(el.memoryNavBody, el.memoryNavToggle, state.navOpen.memory);
}

function toggleNavBody(body, toggle, open) {
  body.classList.toggle("hidden", !open);
  const caret = toggle.querySelector(".nav-group-caret");
  caret.textContent = open ? "▾" : "▸";
}

function renderSidebar() {
  renderSessions();
  renderMemoryTypeList();
}

function renderTopbar() {
  const memoryMode = state.viewMode === "memory";
  el.messageFilters.classList.toggle("hidden", memoryMode);
  el.memoryFilters.classList.toggle("hidden", !memoryMode);
  el.sessionSidebarFilters.classList.toggle("hidden", memoryMode);
  el.viewChipLabel.textContent = memoryMode ? "memory" : "messages";
  el.batchDeleteButton.textContent = memoryMode ? "批量删除记忆" : "批量删除";
}

function renderSessionFilters() {
  const channels = [...new Set(state.sessions.map((session) => channelOf(session.key)))];
  const current = state.sessionChannel;
  el.sessionChannelFilter.innerHTML = '<option value="">全部 channel</option>';
  channels.forEach((channel) => {
    const option = document.createElement("option");
    option.value = channel;
    option.textContent = channel;
    option.selected = channel === current;
    el.sessionChannelFilter.appendChild(option);
  });
  el.channelCustomSelect.refresh();
}

function renderSessions() {
  el.sessionCountTitle.textContent =
    state.viewMode === "memory" ? `${state.totalMemories} 条记忆` : `${state.sessions.length} 个会话`;
  const total = totalSessionMessages();
  el.allMessagesCount.textContent = String(total);
  el.allSessionsCount.textContent = String(total);
  el.allMessagesButton.classList.toggle("active", state.viewMode === "sessions" && !state.activeSessionKey);
  el.sessionList.innerHTML = "";

  state.sessions.forEach((session) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "session-item";
    if (session.key === state.activeSessionKey && state.viewMode === "sessions") {
      item.classList.add("active");
    }
    item.innerHTML = `
      <div class="session-item-head">
        <div class="session-ident">
          <span class="channel-pill" style="${channelStyle(session.key)}">${escapeHtml(channelOf(session.key))}</span>
          <div class="session-key">${escapeHtml(session.key)}</div>
        </div>
        <div class="session-count">${session.message_count || 0}</div>
      </div>
      <div class="session-meta">
        <span>${escapeHtml(relativeTime(session.updated_at))}</span>
        <div class="session-actions">
          <button class="icon-btn" data-session-edit="${escapeHtml(session.key)}" type="button">✎</button>
          <button class="icon-btn" data-session-delete="${escapeHtml(session.key)}" type="button">✕</button>
        </div>
      </div>
    `;
    item.addEventListener("click", async (event) => {
      if (event.target.closest("button.icon-btn")) {
        return;
      }
      await selectSession(session);
    });
    el.sessionList.appendChild(item);
  });

  el.sessionList.querySelectorAll("[data-session-edit]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const session = state.sessionMap.get(button.getAttribute("data-session-edit"));
      if (session) {
        openSessionEditModal(session);
      }
    });
  });

  el.sessionList.querySelectorAll("[data-session-delete]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const session = state.sessionMap.get(button.getAttribute("data-session-delete"));
      if (session) {
        openSessionDeleteModal(session);
      }
    });
  });

  el.activeSessionChip.classList.toggle(
    "hidden",
    state.viewMode !== "sessions" || !state.activeSessionKey
  );
  el.activeSessionText.textContent = state.activeSessionKey || "";
}

function renderMemoryTypeList() {
  el.memoryCountBadge.textContent = String(state.totalMemories || 0);
  el.allMemoriesCount.textContent = String(state.totalMemories || 0);
  el.allMemoriesButton.classList.toggle(
    "active",
    state.viewMode === "memory" && !state.activeMemoryId && !state.memoryType
  );
  el.memoryTypeList.innerHTML = "";

  if (!state.memoryTypeCounts.length) {
    el.memoryTypeList.innerHTML = '<div class="empty-state">还没有可展示的 memory type。</div>';
  }

  state.memoryTypeCounts.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "memory-quick-item";
    button.dataset.memoryType = item.memory_type;
    button.setAttribute(
      "onclick",
      "window.dashboardSelectMemoryType(this.dataset.memoryType)"
    );
    if (state.viewMode === "memory" && state.memoryType === item.memory_type) {
      button.classList.add("active");
    }
    button.innerHTML = `
      <div class="memory-quick-head">
        <div class="memory-quick-main">
          <span class="type-pill" style="${memoryTypeStyle(item.memory_type)}">${escapeHtml(item.memory_type)}</span>
          <div class="memory-quick-title">${escapeHtml(memoryTypeHint(item.memory_type))}</div>
        </div>
        <span class="status-pill">${escapeHtml(String(item.total))}</span>
      </div>
      <div class="memory-quick-meta">
        <span>按 type 过滤</span>
        <span>${escapeHtml(String(item.total))} 条</span>
      </div>
    `;
    el.memoryTypeList.appendChild(button);
  });

  const scopeText = formatScopeChip();
  el.activeMemoryScopeChip.classList.toggle(
    "hidden",
    state.viewMode !== "memory" || !scopeText
  );
  el.activeMemoryScopeText.textContent = scopeText;
}

function memoryTypeHint(memoryType) {
  const hints = {
    procedure: "流程与做法",
    preference: "偏好与习惯",
    event: "事件与经历",
    profile: "长期画像",
  };
  return hints[memoryType] || memoryType;
}

function renderTableHead() {
  if (state.viewMode === "memory") {
    el.tableHead.className = "table-head mode-memory";
    el.tableHead.innerHTML = `
      <label class="checkbox-cell"><input id="selectAllCheckbox" type="checkbox"></label>
      <div>Type</div>
      <div>Summary</div>
      <div>Source</div>
      <div>Updated</div>
      <div>Status</div>
      <div></div>
    `;
  } else {
    el.tableHead.className = "table-head mode-messages";
    el.tableHead.innerHTML = `
      <label class="checkbox-cell"><input id="selectAllCheckbox" type="checkbox"></label>
      <div>Session Key</div>
      <div>Seq</div>
      <div>Content</div>
      <div>Timestamp</div>
      <div>Role</div>
      <div></div>
    `;
  }

  el.selectAllCheckbox = document.getElementById("selectAllCheckbox");
  el.selectAllCheckbox.addEventListener("change", (event) => {
    if (state.viewMode === "memory") {
      if (event.target.checked) {
        state.memories.forEach((item) => state.selectedMemoryIds.add(item.id));
      } else {
        state.memories.forEach((item) => state.selectedMemoryIds.delete(item.id));
      }
    } else if (event.target.checked) {
      state.messages.forEach((message) => state.selectedMessageIds.add(message.id));
    } else {
      state.messages.forEach((message) => state.selectedMessageIds.delete(message.id));
    }
    renderRows();
  });
}

function renderRows() {
  if (state.viewMode === "memory") {
    renderMemoryRows();
  } else {
    renderMessageRows();
  }
}

function renderMessageRows() {
  el.messageTable.innerHTML = "";
  const selectedOnPage = state.messages.filter((message) =>
    state.selectedMessageIds.has(message.id)
  ).length;
  el.selectAllCheckbox.checked =
    state.messages.length > 0 && selectedOnPage === state.messages.length;
  el.batchBar.classList.toggle("hidden", state.selectedMessageIds.size === 0);
  el.batchCount.textContent = `已选 ${state.selectedMessageIds.size} 条消息`;

  if (!state.messages.length) {
    el.messageTable.innerHTML = '<div class="empty-state">没有匹配的消息。</div>';
  }

  state.messages.forEach((message) => {
    const row = document.createElement("div");
    row.className = "table-row mode-messages";
    if (state.activeMessage?.id === message.id) {
      row.classList.add("active");
    }
    if (state.selectedMessageIds.has(message.id)) {
      row.classList.add("selected");
    }
    row.innerHTML = `
      <label class="checkbox-cell"><input data-select-id="${escapeHtml(message.id)}" type="checkbox" ${state.selectedMessageIds.has(message.id) ? "checked" : ""}></label>
      <div class="mono cell-session" title="${escapeHtml(message.session_key)}">${escapeHtml(formatSessionKeyForTable(message.session_key))}</div>
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
      renderRows();
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
      renderRows();
    });
  });

  el.messageTable.querySelectorAll("[data-edit-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const message = state.messages.find(
        (item) => item.id === button.getAttribute("data-edit-id")
      );
      if (message) {
        openMessageEditModal(message);
      }
    });
  });

  el.messageTable.querySelectorAll("[data-delete-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const message = state.messages.find(
        (item) => item.id === button.getAttribute("data-delete-id")
      );
      if (message) {
        openMessageDeleteModal(message);
      }
    });
  });

  const sessionText = state.activeSessionKey ? ` · session: ${state.activeSessionKey}` : "";
  el.messageMeta.textContent = `共 ${state.totalMessages} 条消息${sessionText}`;
  el.pageText.textContent = `${state.page} / ${pageCount()}`;
  el.prevPageButton.disabled = state.page <= 1;
  el.nextPageButton.disabled = state.page >= pageCount();
}

function renderMemoryRows() {
  el.messageTable.innerHTML = "";
  const selectedOnPage = state.memories.filter((item) =>
    state.selectedMemoryIds.has(item.id)
  ).length;
  el.selectAllCheckbox.checked =
    state.memories.length > 0 && selectedOnPage === state.memories.length;
  el.batchBar.classList.toggle("hidden", state.selectedMemoryIds.size === 0);
  el.batchCount.textContent = `已选 ${state.selectedMemoryIds.size} 条记忆`;

  if (!state.memories.length) {
    el.messageTable.innerHTML = '<div class="empty-state">没有匹配的 memory。</div>';
  }

  state.memories.forEach((item) => {
    const row = document.createElement("div");
    row.className = "table-row mode-memory";
    if (state.activeMemoryId === item.id) {
      row.classList.add("active");
    }
    if (state.selectedMemoryIds.has(item.id)) {
      row.classList.add("selected");
    }
    row.innerHTML = `
      <label class="checkbox-cell"><input data-memory-select-id="${escapeHtml(item.id)}" type="checkbox" ${state.selectedMemoryIds.has(item.id) ? "checked" : ""}></label>
      <div class="cell-type"><span class="type-pill" style="${memoryTypeStyle(item.memory_type)}">${escapeHtml(item.memory_type)}</span></div>
      <div class="content-preview">${escapeHtml(item.summary || "")}</div>
      <div class="mono cell-source" title="${escapeHtml(item.source_ref || "")}">${escapeHtml(shortSource(item.source_ref || "-"))}</div>
      <div class="mono cell-time" title="${escapeHtml(item.updated_at)}">${escapeHtml(shortTs(item.updated_at))}</div>
      <div class="cell-status"><span class="status-pill" style="${memoryStatusStyle(item.status)}">${escapeHtml(item.status)}</span></div>
      <div class="table-actions">
        <button class="icon-btn" data-memory-edit-id="${escapeHtml(item.id)}" type="button">✎</button>
        <button class="icon-btn" data-memory-delete-id="${escapeHtml(item.id)}" type="button">✕</button>
      </div>
    `;
    row.addEventListener("click", async (event) => {
      if (event.target.closest("button") || event.target.closest("input")) {
        return;
      }
      await selectMemory(item.id);
    });
    el.messageTable.appendChild(row);
  });

  el.messageTable.querySelectorAll("[data-memory-select-id]").forEach((input) => {
    input.addEventListener("click", (event) => event.stopPropagation());
    input.addEventListener("change", (event) => {
      const itemId = event.target.getAttribute("data-memory-select-id");
      if (event.target.checked) {
        state.selectedMemoryIds.add(itemId);
      } else {
        state.selectedMemoryIds.delete(itemId);
      }
      renderRows();
    });
  });

  el.messageTable.querySelectorAll("[data-memory-edit-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const item = state.memories.find(
        (memory) => memory.id === button.getAttribute("data-memory-edit-id")
      );
      if (item) {
        openMemoryEditModal(item);
      }
    });
  });

  el.messageTable.querySelectorAll("[data-memory-delete-id]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const item = state.memories.find(
        (memory) => memory.id === button.getAttribute("data-memory-delete-id")
      );
      if (item) {
        openMemoryDeleteModal(item);
      }
    });
  });

  const scopeText = formatScopeChip();
  el.messageMeta.textContent = scopeText
    ? `共 ${state.totalMemories} 条记忆 · scope: ${scopeText}`
    : `共 ${state.totalMemories} 条记忆`;
  el.pageText.textContent = `${state.memoryPage} / ${pageCount()}`;
  el.prevPageButton.disabled = state.memoryPage <= 1;
  el.nextPageButton.disabled = state.memoryPage >= pageCount();
}

function renderDetail() {
  if (state.viewMode === "memory") {
    renderMemoryDetail();
    return;
  }

  if (state.activeMessage) {
    renderMessageDetail();
    return;
  }

  if (state.activeSession) {
    renderSessionDetail();
    return;
  }

  el.detailPane.innerHTML = `
    <div class="detail-empty">
      <div class="detail-empty-title">详情</div>
      <div class="detail-empty-text">点开消息、session 或 memory 后，这里会显示完整内容、字段和 JSON 信息。</div>
    </div>
  `;
}

function renderMessageDetail() {
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
                ${detailRow("message_count", String(session.message_count || 0))}
                ${detailRow("updated_at", `<code>${escapeHtml(session.updated_at || "")}</code>`)}
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

function renderSessionDetail() {
  const session = state.activeSession;
  el.detailPane.innerHTML = `
    <div class="detail-wrap">
      <div class="detail-toolbar">
        <div>
          <div class="detail-title">Session 详情</div>
          <div class="detail-subtext">${escapeHtml(session.key)}</div>
        </div>
        <div class="table-actions">
          <button class="ghost" id="sessionDetailEditButton" type="button">编辑</button>
          <button class="danger-ghost" id="sessionDetailDeleteButton" type="button">删除</button>
        </div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Fields</div>
        <div class="detail-grid">
          ${detailRow("key", `<code>${escapeHtml(session.key)}</code>`)}
          ${detailRow("message_count", String(session.message_count || 0))}
          ${detailRow("updated_at", `<code>${escapeHtml(session.updated_at || "")}</code>`)}
          ${detailRow("last_user_at", `<code>${escapeHtml(session.last_user_at || "")}</code>`)}
          ${detailRow("last_proactive_at", `<code>${escapeHtml(session.last_proactive_at || "")}</code>`)}
          ${detailRow("last_consolidated", `<code>${escapeHtml(String(session.last_consolidated ?? ""))}</code>`)}
        </div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Metadata</div>
        ${jvPlaceholder(session.metadata || {})}
      </div>
    </div>
  `;

  document
    .getElementById("sessionDetailEditButton")
    .addEventListener("click", () => openSessionEditModal(session));
  document
    .getElementById("sessionDetailDeleteButton")
    .addEventListener("click", () => openSessionDeleteModal(session));
  attachJsonViewers(el.detailPane);
}

function renderMemoryDetail() {
  if (!state.activeMemoryDetail) {
    el.detailPane.innerHTML = `
      <div class="detail-empty">
        <div class="detail-empty-title">Memory 详情</div>
        <div class="detail-empty-text">点开一条 memory 后，这里会显示摘要、scope、extra_json 和相似记忆。</div>
      </div>
    `;
    return;
  }

  const item = state.activeMemoryDetail;
  el.detailPane.innerHTML = `
    <div class="detail-wrap">
      <div class="detail-toolbar">
        <div>
          <div class="detail-title">Memory 详情</div>
          <div class="detail-subtext">${escapeHtml(item.id)} · ${escapeHtml(item.memory_type)}</div>
        </div>
        <div class="table-actions">
          <button class="ghost" id="memoryEditButton" type="button">编辑</button>
          <button class="danger-ghost" id="memoryDeleteButton" type="button">删除</button>
        </div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Summary</div>
        <div class="detail-content">${escapeHtml(item.summary || "")}</div>
      </div>

      <div class="detail-block">
        <div class="detail-label">Fields</div>
        <div class="detail-grid">
          ${detailRow("id", `<code>${escapeHtml(item.id)}</code>`)}
          ${detailRow("memory_type", `<span class="type-pill" style="${memoryTypeStyle(item.memory_type)}">${escapeHtml(item.memory_type)}</span>`)}
          ${detailRow("status", `<span class="status-pill" style="${memoryStatusStyle(item.status)}">${escapeHtml(item.status)}</span>`)}
          ${detailRow("source_ref", `<code>${escapeHtml(item.source_ref || "")}</code>`)}
          ${detailRow("happened_at", `<code>${escapeHtml(item.happened_at || "")}</code>`)}
          ${detailRow("created_at", `<code>${escapeHtml(item.created_at || "")}</code>`)}
          ${detailRow("updated_at", `<code>${escapeHtml(item.updated_at || "")}</code>`)}
          ${detailRow("reinforcement", `<code>${escapeHtml(String(item.reinforcement ?? 0))}</code>`)}
          ${detailRow("emotional_weight", `<code>${escapeHtml(String(item.emotional_weight ?? 0))}</code>`)}
          ${detailRow("has_embedding", String(Boolean(item.has_embedding)))}
          ${detailRow("embedding_dim", `<code>${escapeHtml(String(item.embedding_dim ?? 0))}</code>`)}
          ${detailRow("scope_channel", `<code>${escapeHtml(item.extra_json?.scope_channel || "")}</code>`)}
          ${detailRow("scope_chat_id", `<code>${escapeHtml(item.extra_json?.scope_chat_id || "")}</code>`)}
        </div>
        ${
          item.extra_json?.scope_channel
            ? `
              <div class="modal-actions">
                <button class="ghost" id="memoryScopeFilterButton" type="button">按这个 scope 过滤</button>
              </div>
            `
            : ""
        }
      </div>

      <div class="detail-block">
        <div class="detail-label">Extra JSON</div>
        ${jvPlaceholder(item.extra_json || {})}
      </div>

      <div class="detail-block">
        <div class="detail-label">Similar Memories</div>
        ${
          state.activeMemorySimilar.length
            ? `<div class="detail-similar-list">${state.activeMemorySimilar
                .map(
                  (similar) => `
                    <button class="similar-item" data-similar-id="${escapeHtml(similar.id)}" type="button">
                      <div class="similar-head">
                        <span class="type-pill" style="${memoryTypeStyle(similar.memory_type)}">${escapeHtml(similar.memory_type)}</span>
                        <span class="similar-score">score ${escapeHtml(String(similar.score ?? "-"))}</span>
                      </div>
                      <div class="similar-summary">${escapeHtml(similar.summary || "")}</div>
                    </button>
                  `
                )
                .join("")}</div>`
            : '<div class="muted-text">没有可用的相似记忆。</div>'
        }
      </div>
    </div>
  `;

  document
    .getElementById("memoryEditButton")
    .addEventListener("click", () => openMemoryEditModal(item));
  document
    .getElementById("memoryDeleteButton")
    .addEventListener("click", () => openMemoryDeleteModal(item));
  if (item.extra_json?.scope_channel) {
    document
      .getElementById("memoryScopeFilterButton")
      .addEventListener("click", async () => {
        state.memoryScopeFilter = {
          channel: item.extra_json.scope_channel || "",
          chatId: item.extra_json.scope_chat_id || "",
        };
        state.memoryPage = 1;
        await loadMemoriesAndSidebar();
        render();
      });
  }

  el.detailPane.querySelectorAll("[data-similar-id]").forEach((button) => {
    button.addEventListener("click", async () => {
      await selectMemory(button.getAttribute("data-similar-id"));
    });
  });

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
            .map(
              (role) => `<option value="${role}" ${message.role === role ? "selected" : ""}>${role}</option>`
            )
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
    await refreshCurrentView();
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
        last_consolidated: Number(
          document.getElementById("modalSessionConsolidated").value || 0
        ),
        last_user_at: document.getElementById("modalSessionLastUser").value || null,
        last_proactive_at:
          document.getElementById("modalSessionLastProactive").value || null,
      }),
    });
    closeModal();
    await loadSessions();
    await refreshCurrentView();
  });
}

function openMemoryEditModal(item) {
  const extraJson = state.activeMemoryDetail?.id === item.id
    ? state.activeMemoryDetail.extra_json || {}
    : {
        scope_channel: item.scope_channel || "",
        scope_chat_id: item.scope_chat_id || "",
      };
  const html = `
    <div class="modal-title">编辑 Memory</div>
    <div class="modal-sub">保留现有 memory 结构，只开放摘要外的安全字段。</div>
    <div class="form-grid">
      <label class="form-label">status
        <select id="modalMemoryStatus">
          <option value="active" ${item.status === "active" ? "selected" : ""}>active</option>
          <option value="superseded" ${item.status === "superseded" ? "selected" : ""}>superseded</option>
        </select>
      </label>
      <label class="form-label">source_ref
        <input id="modalMemorySourceRef" type="text" value="${escapeHtml(item.source_ref || "")}">
      </label>
      <label class="form-label">happened_at
        <input id="modalMemoryHappenedAt" type="text" value="${escapeHtml(item.happened_at || "")}">
      </label>
      <label class="form-label">emotional_weight
        <input id="modalMemoryWeight" type="number" min="0" max="10" value="${escapeHtml(String(item.emotional_weight ?? 0))}">
      </label>
      <label class="form-label">extra_json
        <textarea id="modalMemoryExtra" rows="10">${escapeHtml(JSON.stringify(extraJson, null, 2))}</textarea>
      </label>
    </div>
    <div class="modal-actions">
      <button class="ghost" id="modalCancel" type="button">取消</button>
      <button class="primary" id="modalSubmit" type="button">保存</button>
    </div>
  `;
  openModal(html, async () => {
    await api(`/api/dashboard/memories/${encodePath(item.id)}`, {
      method: "PATCH",
      body: JSON.stringify({
        status: document.getElementById("modalMemoryStatus").value,
        source_ref: document.getElementById("modalMemorySourceRef").value || null,
        happened_at: document.getElementById("modalMemoryHappenedAt").value || null,
        emotional_weight: Number(document.getElementById("modalMemoryWeight").value || 0),
        extra_json: parseJsonField("modalMemoryExtra") || {},
      }),
    });
    closeModal();
    await loadMemoriesAndSidebar();
    if (state.activeMemoryId === item.id) {
      await loadMemoryDetail(item.id);
    }
    render();
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
      if (state.activeMessage?.id === message.id) {
        state.activeMessage = null;
      }
      closeModal();
      await refreshCurrentView();
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
        state.activeSession = null;
      }
      state.activeMessage = null;
      closeModal();
      await loadSessions();
      await refreshCurrentView();
    },
  });
}

function openMemoryDeleteModal(item) {
  openConfirmModal({
    title: "删除 Memory",
    text: `确定删除这条 memory 吗？此操作不可撤销。`,
    danger: true,
    confirmText: "删除",
    onConfirm: async () => {
      await api(`/api/dashboard/memories/${encodePath(item.id)}`, {
        method: "DELETE",
      });
      if (state.activeMemoryId === item.id) {
        state.activeMemoryId = null;
        state.activeMemoryDetail = null;
        state.activeMemorySimilar = [];
      }
      closeModal();
      await loadMemoriesAndSidebar();
      render();
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
  if (state.viewMode === "memory") {
    return Math.max(1, Math.ceil(state.totalMemories / state.memoryPageSize));
  }
  return Math.max(1, Math.ceil(state.totalMessages / state.pageSize));
}

function totalSessionMessages() {
  return state.sessions.reduce((sum, session) => sum + (session.message_count || 0), 0);
}

function roleStyle(role) {
  const styles = {
    user: "background:var(--accent-soft);color:var(--accent);",
    assistant: "background:var(--green-soft);color:var(--green);",
    system: "background:var(--yellow-soft);color:#8b6b09;",
    tool: "background:var(--blue-soft);color:#276489;",
  };
  return styles[role] || "background:#ece6db;color:var(--text-soft);";
}

function channelStyle(key) {
  const styles = {
    telegram: "background:var(--blue-soft);color:#276489;",
    cli: "background:#ece6db;color:var(--text-soft);",
    qq: "background:#efe0f7;color:#74488d;",
    scheduler: "background:var(--yellow-soft);color:#8b6b09;",
  };
  return styles[channelOf(key)] || "background:#ece6db;color:var(--text-soft);";
}

function memoryTypeStyle(memoryType) {
  const styles = {
    procedure: "background:var(--blue-soft);color:#276489;",
    preference: "background:var(--accent-soft);color:var(--accent);",
    event: "background:var(--green-soft);color:var(--green);",
    profile: "background:#ece6db;color:var(--text-soft);",
  };
  return styles[memoryType] || "background:#ece6db;color:var(--text-soft);";
}

function memoryStatusStyle(status) {
  if (status === "superseded") {
    return "background:var(--yellow-soft);color:#8b6b09;";
  }
  return "background:var(--green-soft);color:var(--green);";
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

function shortSource(value) {
  const raw = String(value || "");
  if (raw.length <= 24) {
    return raw;
  }
  return `${raw.slice(0, 12)}...${raw.slice(-8)}`;
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

function formatScopeChip() {
  if (!state.memoryScopeFilter?.channel) {
    return "";
  }
  return `${state.memoryScopeFilter.channel}:${state.memoryScopeFilter.chatId || ""}`;
}

function _jnSpan(cls, text) {
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = text;
  return span;
}

function _renderJNode(data, container, depth) {
  if (typeof data === "string") {
    const trimmed = data.trim();
    if ((trimmed.startsWith("{") || trimmed.startsWith("[")) && trimmed.length > 2) {
      try {
        data = JSON.parse(data);
      } catch {}
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

  const defaultOpen = depth < 3;
  const toggle = document.createElement("span");
  toggle.className = "jt-toggle";

  const updateToggleText = (open) => {
    toggle.textContent = open
      ? isArr
        ? `▾ [${keys.length}]`
        : `▾ {${keys.length}}`
      : isArr
        ? `▸ [${keys.length}]`
        : "▸ {…}";
  };
  updateToggleText(defaultOpen);
  container.appendChild(toggle);

  const children = document.createElement("div");
  children.className = "jt-children";
  if (!defaultOpen) {
    children.style.display = "none";
  }

  keys.forEach((key) => {
    const row = document.createElement("div");
    row.className = "jt-row";
    if (!isArr) {
      row.appendChild(_jnSpan("jt-key", String(key)));
      row.appendChild(_jnSpan("jt-colon", ": "));
    }
    _renderJNode(isArr ? data[key] : data[key], row, depth + 1);
    children.appendChild(row);
  });

  container.appendChild(children);
  toggle.addEventListener("click", () => {
    const isOpen = children.style.display !== "none";
    children.style.display = isOpen ? "none" : "";
    updateToggleText(!isOpen);
  });
}

function makeJsonViewer(data) {
  const box = document.createElement("div");
  box.className = "json-tree";
  _renderJNode(data, box, 0);
  return box;
}

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
  return `<div class="detail-row"><div class="detail-row-label">${escapeHtml(label)}</div><div class="detail-row-val">${value}</div></div>`;
}

function extraOf(message) {
  const known = new Set([
    "id",
    "session_key",
    "seq",
    "role",
    "content",
    "timestamp",
    "tool_chain",
  ]);
  const extra = {};
  Object.entries(message || {}).forEach(([key, value]) => {
    if (!known.has(key)) {
      extra[key] = value;
    }
  });
  return Object.keys(extra).length ? extra : null;
}
