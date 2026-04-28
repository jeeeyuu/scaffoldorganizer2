const API = "http://127.0.0.1:8765";
const DEFAULT_STATUS_POLL_MS = 2000;
const FETCH_TIMEOUT_MS = 60_000;
const FILTER_DEBOUNCE_MS = 180;
const FEEDBACK_AUTO_CLEAR_MS = 30_000;

const state = {
  activeTab: "inbox",
  selectedItemIds: new Set(),
  currentItems: [],
  currentSessions: [],
  filter: "",
  statusTimer: null,
  statusPollMs: DEFAULT_STATUS_POLL_MS,
  filterTimer: null,
  // When set, the card for this item renders in edit mode instead of view.
  // Only one card is editable at a time — pressing Edit elsewhere swaps.
  editingItemId: null,
  // Worklog draft lives in memory until the user clicks Save or Save&Export.
  // `savedId` is null while the textarea holds an unsaved draft.
  worklogDraft: {
    logDate: null,
    title: null,
    contextSummary: null,
    savedId: null,
  },
};

const $ = (id) => document.getElementById(id);

async function request(path, options = {}) {
  // Hard timeout so a hung LLM call (or lost backend) doesn't leave buttons
  // disabled forever.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(`${API}${path}`, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...options,
    });
    if (!response.ok) {
      let detail = await response.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch (_) {
        /* plain text */
      }
      throw new Error(`${response.status} ${detail}`.trim());
    }
    return response.json();
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error(`Request timed out after ${FETCH_TIMEOUT_MS / 1000}s: ${path}`);
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
  }
}

let _feedbackTimer = null;

function setFeedback(text, level = "info") {
  const el = $("feedback");
  el.textContent = text;
  el.dataset.level = level;

  // Any previous auto-clear timer is now stale — a new feedback line
  // replaces it. Busy messages represent active work and should remain
  // visible until the op finishes and calls setFeedback again; empty
  // strings don't need to be cleared either.
  if (_feedbackTimer) {
    clearTimeout(_feedbackTimer);
    _feedbackTimer = null;
  }
  if (!text || level === "busy") return;

  _feedbackTimer = setTimeout(() => {
    // Only clear if the text is still the one we queued — a later
    // setFeedback will have reset the timer above, so this branch only
    // runs when the message truly has been idle for the full interval.
    el.textContent = "";
    el.dataset.level = "info";
    _feedbackTimer = null;
  }, FEEDBACK_AUTO_CLEAR_MS);
}

async function withBusy(button, fn) {
  if (button?.dataset.busy === "1") return;
  if (button) {
    button.dataset.busy = "1";
    button.disabled = true;
    button.classList.add("busy");
  }
  try {
    return await fn();
  } finally {
    if (button) {
      button.dataset.busy = "";
      button.disabled = false;
      button.classList.remove("busy");
    }
  }
}

function tabQuery() {
  if (state.activeTab === "inbox") return "/items?status=inbox";
  // Active merges todo (planned) + doing (in progress) so the user can see
  // both when deciding what to do next. Long-term is excluded — it has its
  // own tab. The backend sort already puts doing rows on top, and the card
  // CSS highlights them with a green border + shadow.
  if (state.activeTab === "active") return "/items?status=todo,doing&exclude_horizon=long_term";
  if (state.activeTab === "longterm") return "/items?horizon=long_term";
  if (state.activeTab === "done") return "/items?status=done";
  return "/items";
}

async function refreshStatus() {
  try {
    const status = await request("/status");
    $("backendStatus").textContent = `Backend: ${status.backend}`;
    $("telegramStatus").textContent = `Telegram: ${status.telegram}`;
    $("aiStatus").textContent = `AI: ${status.ai}`;
    $("backendStatus").className = "dot active";

    const telegramClass = status.telegram === "Active"
      ? "active"
      : status.telegram === "Disabled"
      ? "muted"
      : "idle";
    $("telegramStatus").className = `dot ${telegramClass}`;
    $("telegramStatus").title = status.telegram_error || "";

    // AI dot now distinguishes Ready (green) / Error (red) / fallback (amber).
    // The tooltip carries the raw error string so the user can diagnose
    // why "Processed locally" keeps appearing.
    const aiClass = status.ai === "Ready"
      ? "active"
      : status.ai === "Error"
      ? "error"
      : "idle";
    $("aiStatus").className = `dot ${aiClass}`;
    $("aiStatus").title = status.ai_error || "";
  } catch (_error) {
    $("backendStatus").textContent = "Backend: Error";
    $("backendStatus").className = "dot error";
  }
}

function applyFilter(items) {
  const query = state.filter.trim().toLowerCase();
  if (!query) return items;
  return items.filter((item) => {
    const haystack = [item.title, item.project, item.content, ...(item.tags || [])]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function renderItems(items) {
  state.currentItems = items;
  const filtered = applyFilter(items);
  const view = $("itemsView");
  view.innerHTML = "";
  for (const item of filtered) {
    const card = document.createElement("article");
    const priority = Number(item.priority) || 3;
    card.className = `item-card status-${item.status} priority-${priority}`;
    card.dataset.itemId = String(item.id);
    card.innerHTML = state.editingItemId === item.id
      ? renderItemEditMode(item)
      : renderItemViewMode(item, priority);
    view.appendChild(card);
  }
  const hidden = items.length - filtered.length;
  $("itemCount").textContent = hidden > 0
    ? `${filtered.length} shown / ${items.length} total (${hidden} filtered)`
    : `${filtered.length} item${filtered.length === 1 ? "" : "s"}`;
}

function renderItemViewMode(item, priority) {
  const checked = state.selectedItemIds.has(item.id) ? "checked" : "";
  const content = item.content && item.content !== item.title
    ? `<p class="card-body">${escapeHtml(item.content)}</p>`
    : "";
  // Doing items get a loud "DOING" pill in the header so the active
  // state is unmistakable next to a sea of todo cards on the same tab.
  const doingPill = item.status === "doing"
    ? `<span class="status-pill" title="진행 중">DOING</span>`
    : "";
  return `
    <header class="card-head">
      <input type="checkbox" data-select="${item.id}" ${checked} />
      <h3 title="#${item.id} · ${escapeHtml(item.item_type)} · ${escapeHtml(item.horizon)}">${escapeHtml(item.title)}</h3>
      ${doingPill}
      <span class="priority-chip p${priority}" title="${priorityLabel(priority)}">P${priority}</span>
    </header>
    ${content}
    <footer class="card-actions">
      ${statusButtons(item)}
      <button class="icon-btn" data-action="edit" data-id="${item.id}" title="Edit">✎</button>
    </footer>
  `;
}

function renderItemEditMode(item) {
  const priority = Number(item.priority) || 3;
  const priorityBtns = [1, 2, 3, 4, 5]
    .map((p) => `<button type="button" class="priority-btn${p === priority ? " active" : ""}" data-priority="${p}">P${p}</button>`)
    .join("");
  return `
    <header class="card-head">
      <input class="edit-title" type="text" value="${escapeHtml(item.title)}" data-id="${item.id}" maxlength="120" />
    </header>
    <div class="edit-priority-row">
      <input type="hidden" class="edit-priority" value="${priority}" />
      ${priorityBtns}
    </div>
    <textarea class="edit-content" data-id="${item.id}" rows="3">${escapeHtml(item.content || "")}</textarea>
    <footer class="card-actions">
      <button data-action="save-edit" data-id="${item.id}">Save</button>
      <button data-action="cancel-edit" data-id="${item.id}">Cancel</button>
      <button class="danger" data-action="hard-delete" data-id="${item.id}" title="Permanently delete (no restore)">✕ Delete</button>
    </footer>
  `;
}

function statusButtons(item) {
  const id = item.id;
  const s = item.status;
  const btns = [];
  // Only show the transitions that make sense from the current state.
  // Keeps the card compact by not listing the status the item is already in.
  if (s === "inbox")    btns.push(`<button data-status="todo" data-id="${id}" title="Move to Active">→ Active</button>`);
  if (s === "archived") btns.push(`<button data-status="todo" data-id="${id}" title="Restore to Active">Restore</button>`);
  if (s === "todo" || s === "inbox") btns.push(`<button data-status="doing" data-id="${id}">Doing</button>`);
  if (s === "doing")    btns.push(`<button data-status="todo" data-id="${id}" title="Pause back to Active">Pause</button>`);
  if (s !== "done" && s !== "archived") btns.push(`<button data-status="done" data-id="${id}">Done</button>`);
  if (s !== "archived") btns.push(`<button data-status="archived" data-id="${id}" title="Archive">🗑</button>`);
  return btns.join("");
}

async function refreshItems() {
  const items = await request(tabQuery());
  renderItems(items);
}

async function refreshSessions() {
  const sessions = await request("/sessions");
  state.currentSessions = sessions;
  const list = $("sessionsList");
  list.innerHTML = "";
  for (const session of sessions) {
    const div = document.createElement("div");
    div.className = "list-entry";
    div.dataset.sessionId = String(session.id);
    div.innerHTML = `
      <strong>${escapeHtml(session.title)}</strong>
      <div class="meta">${escapeHtml(session.updated_at || "")}</div>
      <div class="card-actions">
        <button data-session-load="${session.id}">Load</button>
        <button data-session-export="${session.id}">Export</button>
      </div>
    `;
    list.appendChild(div);
  }
}

async function refreshWorklogs() {
  const worklogs = await request("/worklogs");
  const list = $("worklogsList");
  list.innerHTML = "";
  for (const log of worklogs) {
    const div = document.createElement("div");
    div.className = "list-entry";
    div.innerHTML = `<strong>${escapeHtml(log.title)}</strong><div class="meta">${escapeHtml(log.created_at || "")}</div>`;
    div.addEventListener("click", () => {
      $("worklogDraft").value = log.content_md;
    });
    list.appendChild(div);
  }
}

async function refresh() {
  try {
    await refreshStatus();
    if (state.activeTab === "sessions") await refreshSessions();
    else if (state.activeTab === "worklogs") await refreshWorklogs();
    else await refreshItems();
  } catch (error) {
    setFeedback(error.message, "error");
  }
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
  });
  $("itemsView").classList.toggle("hidden", tab === "sessions" || tab === "worklogs");
  const toolbar = document.querySelector(".panel-toolbar");
  if (toolbar) toolbar.classList.toggle("hidden", tab === "sessions" || tab === "worklogs");
  $("sessionView").classList.toggle("hidden", tab !== "sessions");
  $("worklogView").classList.toggle("hidden", tab !== "worklogs");
  refresh();
}

async function loadSession(sessionId) {
  const session = await request(`/sessions/${sessionId}`);
  $("brainDump").value = session.raw_text || "";
  $("structuredOutput").value = session.structured_text || "";
  $("sessionTitle").value = session.title || "";
  setFeedback(`Loaded session #${sessionId}`, "success");
}

async function resetApp() {
  // Hard reset: wipe every item (long-term included) AND clear editor
  // state. This is destructive so it goes behind a confirm() prompt.
  const confirmed = window.confirm(
    "Reset: this deletes ALL items (including long-term) from the database and clears the editor. Continue?",
  );
  if (!confirmed) return;
  const result = await request("/items/reset", { method: "POST", body: "{}" });
  $("brainDump").value = "";
  $("structuredOutput").value = "";
  $("sessionTitle").value = "";
  $("worklogDraft").value = "";
  state.selectedItemIds.clear();
  state.worklogDraft = { logDate: null, title: null, contextSummary: null, savedId: null };
  updateWorklogStatus("");
  setFeedback(`Reset complete — deleted ${result.deleted} item${result.deleted === 1 ? "" : "s"}`, "success");
  await refresh();
}

function updateWorklogStatus(text) {
  const el = $("worklogStatus");
  if (el) el.textContent = text;
}

async function sendCommand() {
  const text = $("commandInput").value.trim();
  if (!text) return;
  const selectedIds = Array.from(state.selectedItemIds);
  const payload = {
    text,
    selected_item_ids: selectedIds,
    selected_items: state.currentItems.filter((item) => state.selectedItemIds.has(item.id)),
    active_session: null,
    ui_context: { active_tab: state.activeTab, filters: {} },
  };
  setFeedback("Processing command…", "busy");
  const result = await request("/chat/command", { method: "POST", body: JSON.stringify(payload) });
  $("commandInput").value = "";
  state.selectedItemIds.clear();
  setFeedback(result.router?.user_feedback || "Command processed", "info");
  await refresh();
}

async function changeItemStatus(id, status) {
  await request(`/items/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
  await refresh();
}

const _PRIORITY_LABELS = {
  1: "P1 · Critical",
  2: "P2 · High",
  3: "P3 · Normal",
  4: "P4 · Low",
  5: "P5 · Someday",
};

function priorityLabel(priority) {
  return _PRIORITY_LABELS[priority] || `P${priority}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function startStatusPolling() {
  if (state.statusTimer) return;
  state.statusTimer = setInterval(refreshStatus, state.statusPollMs);
}

function stopStatusPolling() {
  if (!state.statusTimer) return;
  clearInterval(state.statusTimer);
  state.statusTimer = null;
}

async function loadUiConfig() {
  try {
    const config = await request("/config/ui");
    if (Number.isFinite(config.ui_refresh_interval_ms)) {
      state.statusPollMs = Math.max(500, Number(config.ui_refresh_interval_ms));
    }
    if (config.app_name) {
      document.title = config.app_name;
    }
  } catch (_error) {
    // Config fetch failure is non-fatal — fall back to defaults.
  }
}

// -------- Event wiring --------

document.addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  if (target.matches(".tabs button")) switchTab(target.dataset.tab);
  // Priority toggle buttons in edit mode
  if (target.classList.contains("priority-btn") && target.dataset.priority) {
    const card = target.closest(".item-card");
    if (!card) return;
    const newPriority = Number(target.dataset.priority);
    const hiddenInput = card.querySelector(".edit-priority");
    if (hiddenInput) hiddenInput.value = String(newPriority);
    card.querySelectorAll(".priority-btn").forEach((btn) => {
      btn.classList.toggle("active", Number(btn.dataset.priority) === newPriority);
    });
    return;
  }
  if (target.dataset.status && target.dataset.id) {
    await withBusy(target, async () => {
      try {
        await changeItemStatus(target.dataset.id, target.dataset.status);
      } catch (error) {
        setFeedback(error.message, "error");
      }
    });
  }
  if (target.dataset.sessionLoad) {
    await withBusy(target, async () => {
      try {
        await loadSession(Number(target.dataset.sessionLoad));
      } catch (error) {
        setFeedback(error.message, "error");
      }
    });
  }
  if (target.dataset.sessionExport) {
    await withBusy(target, async () => {
      try {
        const result = await request(`/sessions/${target.dataset.sessionExport}/export`, { method: "POST", body: "{}" });
        setFeedback(`Exported: ${result.path}`, "success");
      } catch (error) {
        setFeedback(error.message, "error");
      }
    });
  }
  // Inline item edit — Edit / Save / Cancel all live on the card footer.
  if (target.dataset.action === "edit") {
    state.editingItemId = Number(target.dataset.id);
    renderItems(state.currentItems);
    const card = document.querySelector(`[data-item-id="${state.editingItemId}"]`);
    card?.querySelector(".edit-title")?.focus();
    return;
  }
  if (target.dataset.action === "cancel-edit") {
    state.editingItemId = null;
    renderItems(state.currentItems);
    return;
  }
  if (target.dataset.action === "hard-delete") {
    const id = Number(target.dataset.id);
    if (!window.confirm("This permanently deletes the item. Continue?")) return;
    await withBusy(target, async () => {
      try {
        await request(`/items/${id}`, { method: "DELETE" });
        state.editingItemId = null;
        state.selectedItemIds.delete(id);
        setFeedback(`Deleted #${id}`, "success");
        await refresh();
      } catch (error) {
        setFeedback(error.message, "error");
      }
    });
    return;
  }
  if (target.dataset.action === "save-edit") {
    const id = Number(target.dataset.id);
    const card = document.querySelector(`[data-item-id="${id}"]`);
    if (!card) return;
    const title = card.querySelector(".edit-title").value.trim();
    const content = card.querySelector(".edit-content").value;
    const priority = Number(card.querySelector(".edit-priority").value) || 3;
    if (!title) {
      setFeedback("Title cannot be empty", "error");
      return;
    }
    await withBusy(target, async () => {
      try {
        await request(`/items/${id}`, {
          method: "PATCH",
          body: JSON.stringify({ title, content, priority }),
        });
        state.editingItemId = null;
        setFeedback(`Updated #${id}`, "success");
        await refresh();
      } catch (error) {
        setFeedback(error.message, "error");
      }
    });
    return;
  }
});

// Ctrl/Cmd+Enter inside edit inputs saves; Esc cancels.
document.addEventListener("keydown", (event) => {
  if (state.editingItemId === null) return;
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const inEditField = target.matches(".edit-title, .edit-content, .edit-priority");
  if (!inEditField) return;
  if (event.key === "Escape") {
    state.editingItemId = null;
    renderItems(state.currentItems);
  } else if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    const card = target.closest(".item-card");
    card?.querySelector('[data-action="save-edit"]')?.click();
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement)) return;
  if (target.dataset.select) {
    const id = Number(target.dataset.select);
    if (target.checked) state.selectedItemIds.add(id);
    else state.selectedItemIds.delete(id);
  }
});

$("sendCommand").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      await sendCommand();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

$("commandInput").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    $("sendCommand").click();
  }
});

$("filterInput").addEventListener("input", (event) => {
  const value = event.target.value;
  if (state.filterTimer) clearTimeout(state.filterTimer);
  state.filterTimer = setTimeout(() => {
    state.filter = value;
    renderItems(state.currentItems);
  }, FILTER_DEBOUNCE_MS);
});

$("resetApp").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      await resetApp();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

// --- Session picker: toolbar Load button opens a modal that lists saved
// sessions. 0 → notify; 1 → load immediately; 2+ → popup to pick. ---

function openSessionPicker(sessions) {
  const list = $("sessionPickerList");
  list.innerHTML = "";
  for (const session of sessions) {
    const row = document.createElement("div");
    row.className = "list-entry";
    row.dataset.pickerLoad = String(session.id);
    row.innerHTML = `
      <strong>${escapeHtml(session.title)}</strong>
      <div class="meta">#${session.id} · ${escapeHtml(session.updated_at || "")}</div>
    `;
    list.appendChild(row);
  }
  $("sessionPicker").classList.remove("hidden");
}

function closeSessionPicker() {
  $("sessionPicker").classList.add("hidden");
  $("sessionPickerList").innerHTML = "";
}

$("loadSession").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      const sessions = await request("/sessions");
      if (sessions.length === 0) {
        setFeedback("No saved sessions yet.", "info");
        return;
      }
      if (sessions.length === 1) {
        await loadSession(sessions[0].id);
        return;
      }
      openSessionPicker(sessions);
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

$("sessionPickerClose").addEventListener("click", () => closeSessionPicker());
$("sessionPicker").addEventListener("click", (event) => {
  // Click on scrim (not the card) closes the modal.
  if (event.target === $("sessionPicker")) closeSessionPicker();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("sessionPicker").classList.contains("hidden")) {
    closeSessionPicker();
  }
});
$("sessionPickerList").addEventListener("click", async (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const row = target.closest("[data-picker-load]");
  if (!row) return;
  const id = Number(row.dataset.pickerLoad);
  try {
    await loadSession(id);
    closeSessionPicker();
  } catch (error) {
    setFeedback(error.message, "error");
  }
});

$("exportMarkdown").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      const result = await request("/export/markdown", { method: "POST", body: "{}" });
      setFeedback(`Exported: ${result.path}`, "success");
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

$("saveSession").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      await request("/sessions/save", {
        method: "POST",
        body: JSON.stringify({
          title: $("sessionTitle").value || "Manual session",
          raw_text: $("brainDump").value,
          structured_text: $("structuredOutput").value || "",
        }),
      });
      setFeedback("Session saved", "success");
      await refresh();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

$("structureBrainDump").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      setFeedback("Structuring brain dump…", "busy");
      const result = await request("/brain-dump/structure", {
        method: "POST",
        body: JSON.stringify({
          title: $("sessionTitle").value || "Brain dump",
          raw_text: $("brainDump").value,
        }),
      });
      $("structuredOutput").value = result.structured?.structured_text || "";
      const n = (result.items || []).length;
      const note = result.structured?.used_fallback ? " (local fallback)" : "";
      setFeedback(`Session #${result.session.id} + ${n} item${n === 1 ? "" : "s"} created${note}`, "success");
      await refresh();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

// --- Worklog: generate draft → preview → save / save&export ---

$("generateWorklog").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      setFeedback("Generating work log draft…", "busy");
      const result = await request("/worklog/generate", { method: "POST", body: "{}" });
      $("worklogDraft").value = result.content_md;
      state.worklogDraft = {
        logDate: result.log_date,
        title: result.title,
        contextSummary: result.context_summary || {},
        savedId: null,
      };
      updateWorklogStatus("Draft — not saved");
      const note = result.used_fallback ? " (local fallback)" : "";
      setFeedback(`Draft ready${note} — review, then Save or Save & Export`, "success");
      switchTab("worklogs");
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

async function persistWorklogDraft() {
  if (!state.worklogDraft.logDate) {
    throw new Error("No draft to save. Click 'Work Log' first to generate one.");
  }
  const result = await request("/worklog/save", {
    method: "POST",
    body: JSON.stringify({
      log_date: state.worklogDraft.logDate,
      title: state.worklogDraft.title || `Work Log ${state.worklogDraft.logDate}`,
      content_md: $("worklogDraft").value,
      context_summary: state.worklogDraft.contextSummary || {},
    }),
  });
  state.worklogDraft.savedId = result.id;
  updateWorklogStatus(`Saved #${result.id}`);
  return result;
}

$("saveWorklog").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      const saved = await persistWorklogDraft();
      setFeedback(`Saved work log #${saved.id}`, "success");
      await refresh();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

$("exportWorklog").addEventListener("click", (event) => {
  const button = event.currentTarget;
  withBusy(button, async () => {
    try {
      // Save first if this draft hasn't been persisted yet.
      if (!state.worklogDraft.savedId) {
        await persistWorklogDraft();
      }
      const result = await request(`/worklogs/${state.worklogDraft.savedId}/export`, {
        method: "POST",
        body: "{}",
      });
      setFeedback(`Exported: ${result.path}`, "success");
      await refresh();
    } catch (error) {
      setFeedback(error.message, "error");
    }
  });
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopStatusPolling();
  else {
    refreshStatus();
    startStatusPolling();
  }
});

(async function init() {
  await loadUiConfig();
  await refresh();
  startStatusPolling();
})();
