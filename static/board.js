/**
 * Доска: рабочие дни в шапке, строки — сотрудники,
 * блоки — 3 строки (ключ / title / заказчик), подсказка при наведении.
 * Смена исполнителя (drag-and-drop) и изменение трудозатрат (растягивание
 * прямоугольника) копятся до нажатия «Сохранить в Jira».
 */

(function () {
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnUndo = document.getElementById("btn-undo");
  const btnClear = document.getElementById("btn-clear");
  const btnSave = document.getElementById("btn-save");
  const sprintFilterEl = document.getElementById("sprint-filter");

  let settings = null;
  /** @type {{ rows: any[], meta: any } | null} */
  let model = null;
  let ppd = 36;
  let pendingCount = 0;
  let selectedSprint = "";

  /** @type {{ task: any, sourceRow: any } | null} */
  let dragState = null;
  /** @type {{ task: any, row: any } | null} */
  let resizeState = null;

  const tooltipEl = document.createElement("div");
  tooltipEl.className = "task-tooltip";
  tooltipEl.hidden = true;
  document.body.appendChild(tooltipEl);

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.style.color = isError ? "#c62828" : "#546e7a";
  }

  function updateSaveButton() {
    const has = pendingCount > 0;
    btnSave.disabled = !has;
    btnSave.classList.toggle("has-pending", has);
    btnSave.textContent = has
      ? `Сохранить в Jira (${pendingCount})`
      : "Сохранить в Jira";
    btnUndo.disabled = !has;
    btnClear.disabled = !has;
  }

  function priorityRank(name) {
    const order = (settings && settings.priorities && settings.priorities.order) || [];
    const idx = order.indexOf(name);
    return idx === -1 ? order.length + 10 : idx;
  }

  function taskSortKey(t) {
    return [priorityRank(t.priority), t.key || ""];
  }

  function compareSortKeys(a, b) {
    if (a[0] !== b[0]) return a[0] - b[0];
    return a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0;
  }

  /**
   * Раскладывает задачи строки: с назначенным сроком (pinnedStartOffsetDays)
   * встают фиксированно на эту позицию, остальные заполняют свободные
   * промежутки подряд без зазоров, в порядке приоритета — как раньше.
   */
  function packRow(tasks) {
    const pinned = tasks.filter((t) => t.pinnedStartOffsetDays != null);
    const free = tasks.filter((t) => t.pinnedStartOffsetDays == null);
    pinned.sort((a, b) => {
      if (a.pinnedStartOffsetDays !== b.pinnedStartOffsetDays) {
        return a.pinnedStartOffsetDays - b.pinnedStartOffsetDays;
      }
      return compareSortKeys(taskSortKey(a), taskSortKey(b));
    });
    free.sort((a, b) => compareSortKeys(taskSortKey(a), taskSortKey(b)));

    const occupied = [];
    let cursor = 0;
    for (const t of pinned) {
      t.durationDays = t.effortDays;
      const start = Math.max(t.pinnedStartOffsetDays, cursor);
      t.startOffsetDays = Math.round(start * 10000) / 10000;
      const end = start + t.durationDays;
      occupied.push([start, end]);
      cursor = end;
    }

    for (const t of free) {
      t.durationDays = t.effortDays;
      let start = 0;
      for (;;) {
        const blocker = occupied.find(([s, e]) => s < start + t.durationDays && e > start);
        if (!blocker) break;
        start = blocker[1];
      }
      t.startOffsetDays = Math.round(start * 10000) / 10000;
      occupied.push([start, start + t.durationDays]);
      occupied.sort((a, b) => a[0] - b[0]);
    }
  }

  function totalTrackWidth() {
    if (!settings) return 800;
    return settings.workingDayCount * ppd;
  }

  function rebuildAllPacks() {
    if (!model) return;
    for (const row of model.rows) {
      packRow(row.tasks);
    }
  }

  function populateSprintFilter() {
    const sprints = (model && model.meta && model.meta.sprints) || [];
    const prev = selectedSprint;
    sprintFilterEl.innerHTML = '<option value="">Все спринты</option>';
    for (const name of sprints) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      sprintFilterEl.appendChild(opt);
    }
    selectedSprint = sprints.includes(prev) ? prev : "";
    sprintFilterEl.value = selectedSprint;
  }

  function removeTaskFromAllRows(task) {
    if (!model) return;
    for (const r of model.rows) {
      const i = r.tasks.indexOf(task);
      if (i >= 0) r.tasks.splice(i, 1);
    }
  }

  function offsetDaysFromClientX(rowEl, clientX) {
    const track = rowEl.querySelector(".row-track");
    const rect = track.getBoundingClientRect();
    const raw = (clientX - rect.left) / ppd;
    const maxOffset = Math.max(0, (settings.workingDayCount || 1) - 1);
    return Math.min(maxOffset, Math.max(0, Math.round(raw)));
  }

  function computeDatesForOffset(task, offsetDays) {
    return {
      startIso: boardDateAtOffset(offsetDays, false),
      endIso: boardDateAtOffset(offsetDays + task.durationDays, true),
    };
  }

  function formatDateRu(iso) {
    if (!iso) return "—";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return iso;
    return `${m[3]}.${m[2]}.${m[1]}`;
  }

  function boardDateAtOffset(offsetDays, forEnd) {
    const dates = (settings && settings.workingDates) || [];
    if (!dates.length) return null;
    let idx;
    if (forEnd) {
      idx = Math.ceil(offsetDays) - 1;
      if (offsetDays <= 0) idx = 0;
    } else {
      idx = Math.floor(offsetDays);
    }
    idx = Math.max(0, Math.min(dates.length - 1, idx));
    return dates[idx];
  }

  function resolveTaskDates(t) {
    const fields = (settings && settings.fields) || {};
    const startSrc = (fields.startDate && fields.startDate.source) || "board";
    const endSrc = (fields.endDate && fields.endDate.source) || "board";

    let startIso = null;
    let endIso = null;

    if (startSrc === "jira_field" && t.jiraStartDate) {
      startIso = t.jiraStartDate;
    } else {
      startIso = boardDateAtOffset(t.startOffsetDays, false);
    }

    if (endSrc === "jira_field" && t.jiraEndDate) {
      endIso = t.jiraEndDate;
    } else {
      endIso = boardDateAtOffset(t.startOffsetDays + t.durationDays, true);
    }

    return { startIso, endIso };
  }

  function formatEffort(days) {
    const n = Number(days);
    if (!Number.isFinite(n)) return "—";
    const rounded = Math.round(n * 100) / 100;
    return `${rounded} md`;
  }

  function missingFieldsText(task) {
    const fields = Array.isArray(task.missingFields) ? task.missingFields : [];
    return fields.length ? fields.join(", ") : "";
  }

  function hideTooltip() {
    tooltipEl.hidden = true;
    tooltipEl.innerHTML = "";
  }

  function showTooltip(task, clientX, clientY) {
    const base = ((settings && settings.jiraBaseUrl) || "").replace(/\/$/, "");
    const { startIso, endIso } = resolveTaskDates(task);
    const missingText = missingFieldsText(task);
    const keyLink = base
      ? `<a href="${escapeHtml(base)}/browse/${encodeURIComponent(task.key)}" target="_blank" rel="noopener noreferrer">${escapeHtml(task.key)}</a>`
      : escapeHtml(task.key);
    const pendingParts = [];
    if (task.pendingAssignee) pendingParts.push("исполнитель");
    if (task.pendingEffort) pendingParts.push("трудозатраты");
    if (task.pendingDates) pendingParts.push("даты");
    const pendingRow = pendingParts.length
      ? `<div class="tt-row"><span class="tt-label">Изменение</span><span class="tt-val">${escapeHtml(pendingParts.join(", "))} не сохранены в Jira</span></div>`
      : "";

    tooltipEl.innerHTML = `
      <div class="tt-row"><span class="tt-label">Задача</span><span class="tt-val">${keyLink}</span></div>
      ${pendingRow}
      ${missingText ? `<div class="tt-row"><span class="tt-label">Не заполнено</span><span class="tt-val">${escapeHtml(missingText)}</span></div>` : ""}
      <div class="tt-row"><span class="tt-label">Название</span><span class="tt-val">${escapeHtml(task.summary || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Заказчик</span><span class="tt-val">${escapeHtml(task.customer || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Начало</span><span class="tt-val">${escapeHtml(formatDateRu(startIso))}</span></div>
      <div class="tt-row"><span class="tt-label">Завершение</span><span class="tt-val">${escapeHtml(formatDateRu(endIso))}</span></div>
      <div class="tt-row"><span class="tt-label">Трудозатраты</span><span class="tt-val">${escapeHtml(formatEffort(task.effortDays))}</span></div>
    `;
    tooltipEl.hidden = false;

    const pad = 12;
    const tw = tooltipEl.offsetWidth;
    const th = tooltipEl.offsetHeight;
    let left = clientX + pad;
    let top = clientY + pad;
    if (left + tw > window.innerWidth - 8) left = clientX - tw - pad;
    if (top + th > window.innerHeight - 8) top = clientY - th - pad;
    tooltipEl.style.left = `${Math.max(8, left)}px`;
    tooltipEl.style.top = `${Math.max(8, top)}px`;
  }

  function wireTaskTooltip(el, task) {
    el.addEventListener("mouseenter", (e) => {
      if (dragState || resizeState) return;
      showTooltip(task, e.clientX, e.clientY);
    });
    el.addEventListener("mousemove", (e) => {
      if (dragState || resizeState || tooltipEl.hidden) return;
      showTooltip(task, e.clientX, e.clientY);
    });
    el.addEventListener("mouseleave", () => {
      hideTooltip();
    });
  }

  function wireTaskOpenInJira(el, task) {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".task-resize") || e.target.closest("a.task-key")) return;
      const base = ((settings && settings.jiraBaseUrl) || "").replace(/\/$/, "");
      if (!base) return;
      window.open(`${base}/browse/${encodeURIComponent(task.key)}`, "_blank", "noopener,noreferrer");
    });
  }

  function wireTaskResize(handleEl, task, row) {
    handleEl.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      hideTooltip();

      const startX = e.clientX;
      const startDuration = task.durationDays;
      const minDays = (settings && settings.minEffortWorkingDays) || 0.25;
      resizeState = { task, row };

      const onMove = (ev) => {
        const deltaDays = (ev.clientX - startX) / ppd;
        let next = Math.round((startDuration + deltaDays) * 4) / 4;
        next = Math.max(minDays, next);
        if (next !== task.durationDays) {
          task.durationDays = next;
          task.effortDays = next;
          packRow(row.tasks);
          render();
        }
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        resizeState = null;
        recordPendingEffort(task)
          .then(() => {
            setStatus(
              pendingCount
                ? `Трудозатраты изменены (накоплено изменений: ${pendingCount}). Нажмите «Сохранить в Jira».`
                : "Трудозатраты возвращены к значению из Jira; запись удалена."
            );
          })
          .catch((err) => setStatus(String(err.message || err), true));
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  function render() {
    boardEl.innerHTML = "";
    hideTooltip();
    if (!settings || !model) return;

    ppd = Number(settings.pixelsPerWorkingDay || 36);
    boardEl.style.setProperty("--ppd", `${ppd}px`);

    const inner = document.createElement("div");
    inner.className = "board-inner";

    const header = document.createElement("div");
    header.className = "board-header";

    const corner = document.createElement("div");
    corner.className = "corner";
    corner.textContent = "Сотрудник / дата";
    header.appendChild(corner);

    const timeline = document.createElement("div");
    timeline.className = "timeline";
    timeline.style.width = `${totalTrackWidth()}px`;

    for (const iso of settings.workingDates) {
      const d = new Date(iso + "T00:00:00");
      const cell = document.createElement("div");
      cell.className = "timeline-day";
      if (d.getDay() === 0 || d.getDay() === 6) {
        cell.classList.add("weekend");
      }
      const day = d.getDate();
      const mon = d.getMonth() + 1;
      cell.style.width = `${ppd}px`;
      cell.innerHTML = `<div>${day}.${mon}</div><div style="opacity:.7">${["Вс","Пн","Вт","Ср","Чт","Пт","Сб"][d.getDay()]}</div>`;
      timeline.appendChild(cell);
    }
    header.appendChild(timeline);
    inner.appendChild(header);

    for (const row of model.rows) {
      const visibleTasks = selectedSprint
        ? row.tasks.filter((t) => t.sprint === selectedSprint)
        : row.tasks;
      if (!visibleTasks.length) continue;

      const rowEl = document.createElement("div");
      rowEl.className = "board-row";
      rowEl.dataset.assigneeId = row.assigneeId || "";

      const label = document.createElement("div");
      label.className = "row-label";
      label.textContent = row.assigneeName || "—";
      rowEl.appendChild(label);

      const track = document.createElement("div");
      track.className = "row-track";
      track.style.width = `${totalTrackWidth()}px`;

      for (const t of visibleTasks) {
        const el = document.createElement("div");
        const isResizing = !!(resizeState && resizeState.task === t);
        el.className =
          "task" +
          (t.pendingAssignee || t.pendingEffort || t.pendingDates ? " pending" : "") +
          (isResizing ? " resizing" : "");
        el.draggable = true;
        el.dataset.issueKey = t.key;
        el.style.background = t.color;
        el.style.left = `${t.startOffsetDays * ppd}px`;
        el.style.width = `${Math.max(4, t.durationDays * ppd - 2)}px`;

        const base = (settings.jiraBaseUrl || "").replace(/\/$/, "");
        const keyHtml = base
          ? `<a class="task-key" href="${escapeHtml(base)}/browse/${encodeURIComponent(t.key)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t.key)}</a>`
          : `<span class="task-key">${escapeHtml(t.key)}</span>`;
        const statusHtml = `<span class="task-status">[${escapeHtml(t.status || "—")}]</span>`;
        const missingText = missingFieldsText(t);
        const missingHtml = missingText
          ? `<span class="task-missing" title="Не заполнено: ${escapeHtml(missingText)}">!</span>`
          : "";

        el.innerHTML = `
          <div class="task-line task-line-key">${missingHtml}${keyHtml} ${statusHtml}</div>
          <div class="task-line task-line-title" title="${escapeHtml(t.summary || "")}">${escapeHtml(t.summary || "—")}</div>
          <div class="task-line task-line-customer" title="${escapeHtml(t.customer || "")}">${escapeHtml(t.customer || "—")}</div>
        `;
        const resizeEl = document.createElement("div");
        resizeEl.className = "task-resize";
        resizeEl.title = "Изменить трудозатраты";
        el.appendChild(resizeEl);

        wireTaskTooltip(el, t);
        wireTaskResize(resizeEl, t, row);
        wireTaskOpenInJira(el, t);
        track.appendChild(el);
      }

      wireRowDragDrop(rowEl, track, row);
      rowEl.appendChild(track);
      inner.appendChild(rowEl);
    }

    boardEl.appendChild(inner);
    boardEl.setAttribute("aria-busy", "false");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function recordPendingAssignee(task) {
    const r = await fetch("/api/pending", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: task.key,
        assigneeId: task.assigneeId,
        assigneeName: task.assigneeName,
        originalAssigneeId: task.originalAssigneeId,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    pendingCount = data.count || 0;
    updateSaveButton();
    return data;
  }

  async function recordPendingEffort(task) {
    const r = await fetch("/api/pending/effort", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: task.key,
        effortDays: task.effortDays,
        originalEffortDays: task.originalEffortDays,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    task.pendingEffort = task.effortDays !== task.originalEffortDays;
    pendingCount = data.count || 0;
    updateSaveButton();
    render();
    return data;
  }

  function dateFieldWritable(name) {
    const fields = (settings && settings.fields) || {};
    const cfg = fields[name];
    return !!(cfg && cfg.source === "jira_field" && cfg.jiraFieldId);
  }

  async function recordPendingDates(task, startIso, endIso) {
    const startWritable = dateFieldWritable("startDate");
    const endWritable = dateFieldWritable("endDate");
    if (!startWritable && !endWritable) return null;

    const payload = { key: task.key };
    if (startWritable) {
      payload.startDate = startIso;
      payload.originalStartDate = task.originalJiraStartDate || null;
    }
    if (endWritable) {
      payload.endDate = endIso;
      payload.originalEndDate = task.originalJiraEndDate || null;
    }

    const r = await fetch("/api/pending/dates", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    task.pendingDates =
      (startWritable && startIso !== task.originalJiraStartDate) ||
      (endWritable && endIso !== task.originalJiraEndDate);
    pendingCount = data.count || 0;
    updateSaveButton();
    render();
    return data;
  }

  function wireRowDragDrop(rowEl, trackEl, row) {
    trackEl.addEventListener("dragstart", (e) => {
      if (e.target.closest("a.task-key")) {
        e.preventDefault();
        return;
      }
      const tEl = e.target.closest(".task");
      if (!tEl || !trackEl.contains(tEl)) return;
      const task = row.tasks.find((x) => x.key === tEl.dataset.issueKey);
      if (!task) return;
      hideTooltip();
      dragState = { task, sourceRow: row };
      tEl.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", task.key);
    });

    trackEl.addEventListener("dragend", (e) => {
      const tEl = e.target.closest(".task");
      if (tEl) tEl.classList.remove("dragging");
      document.querySelectorAll(".row-track.drag-target").forEach((x) => x.classList.remove("drag-target"));
      dragState = null;
    });

    trackEl.addEventListener("dragover", (e) => {
      if (!dragState) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      trackEl.classList.add("drag-target");
    });

    trackEl.addEventListener("dragleave", () => {
      trackEl.classList.remove("drag-target");
    });

    trackEl.addEventListener("drop", (e) => {
      e.preventDefault();
      trackEl.classList.remove("drag-target");
      if (!dragState) return;
      const { task: draggedTask, sourceRow } = dragState;
      const prevAssigneeId = draggedTask.assigneeId;
      const prevPinned = draggedTask.pinnedStartOffsetDays;

      const targetRow = row;
      const dropOffset = offsetDaysFromClientX(rowEl, e.clientX);

      removeTaskFromAllRows(draggedTask);
      targetRow.tasks.push(draggedTask);
      draggedTask.assigneeId = targetRow.assigneeId;
      draggedTask.assigneeName = targetRow.assigneeName;
      const originalAssignee = draggedTask.originalAssigneeId;
      draggedTask.pendingAssignee =
        originalAssignee != null && String(originalAssignee) !== String(targetRow.assigneeId);

      const { startIso, endIso } = computeDatesForOffset(draggedTask, dropOffset);
      draggedTask.pinnedStartOffsetDays = dropOffset;
      if (dateFieldWritable("startDate")) draggedTask.jiraStartDate = startIso;
      if (dateFieldWritable("endDate")) draggedTask.jiraEndDate = endIso;

      packRow(targetRow.tasks);
      if (sourceRow !== targetRow) packRow(sourceRow.tasks);
      render();

      const assigneeChanged = String(prevAssigneeId) !== String(targetRow.assigneeId);
      const datesChanged = prevPinned !== dropOffset;

      const persistPromises = [];
      if (assigneeChanged) persistPromises.push(recordPendingAssignee(draggedTask).then(() => true));
      if (datesChanged) persistPromises.push(recordPendingDates(draggedTask, startIso, endIso).then((d) => d !== null));

      if (!persistPromises.length) {
        setStatus("Положение не изменилось.");
        return;
      }
      Promise.all(persistPromises)
        .then((results) => {
          if (!results.some(Boolean)) {
            setStatus("Положение изменено только в интерфейсе (даты в Jira не настроены для записи).");
            return;
          }
          setStatus(
            pendingCount
              ? `Изменения накоплены (${pendingCount}). Нажмите «Сохранить в Jira».`
              : "Изменения возвращены к значениям из Jira; записи удалены."
          );
        })
        .catch((err) => setStatus(String(err.message || err), true));
    });
  }

  async function loadSettings() {
    const r = await fetch("/api/settings");
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    settings = data;
  }

  async function loadBoard() {
    setStatus("Загрузка…");
    boardEl.setAttribute("aria-busy", "true");
    const r = await fetch("/api/board");
    const data = await r.json();
    if (!r.ok) {
      boardEl.innerHTML = `<div class="error-banner">${escapeHtml(data.error || "Ошибка")}</div>`;
      setStatus("", true);
      return;
    }
    model = data;
    if (data.meta && data.meta.fields && settings) {
      settings.fields = data.meta.fields;
    }
    if (data.meta && data.meta.jiraBaseUrl && settings) {
      settings.jiraBaseUrl = data.meta.jiraBaseUrl;
    }
    pendingCount = (data.meta && data.meta.pendingCount) || 0;
    updateSaveButton();
    populateSprintFilter();
    rebuildAllPacks();
    render();
    const base = `Задач: ${countTasks(model)}`;
    setStatus(
      pendingCount
        ? `${base}. Несохранённых изменений: ${pendingCount}`
        : base
    );
  }

  function countTasks(m) {
    return m.rows.reduce((a, r) => a + r.tasks.length, 0);
  }

  async function saveToJira() {
    if (pendingCount <= 0) return;
    btnSave.disabled = true;
    setStatus("Сохранение в Jira…");
    try {
      const r = await fetch("/api/save", { method: "POST" });
      const data = await r.json();
      if (!r.ok && r.status !== 207) {
        throw new Error(data.error || r.statusText);
      }
      pendingCount = data.count || 0;
      updateSaveButton();
      const failed = Array.isArray(data.failed) ? data.failed : [];
      if (failed.length) {
        const keys = failed.map((f) => f.key).join(", ");
        setStatus(`Сохранено: ${data.saved || 0}. Ошибки: ${keys}`, true);
      } else {
        setStatus(`Сохранено в Jira: ${data.saved || 0}`);
      }
      await loadBoard();
    } catch (e) {
      updateSaveButton();
      setStatus(String(e.message || e), true);
    }
  }

  async function undoLastPending() {
    const r = await fetch("/api/pending/undo", { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  async function clearAllPending() {
    const r = await fetch("/api/pending/clear", { method: "POST" });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);
    return data;
  }

  btnRefresh.addEventListener("click", () => {
    loadBoard().catch((e) => setStatus(String(e.message || e), true));
  });

  btnUndo.addEventListener("click", () => {
    undoLastPending()
      .then(() => {
        setStatus("Последнее действие отменено.");
        return loadBoard();
      })
      .catch((e) => setStatus(String(e.message || e), true));
  });

  btnClear.addEventListener("click", () => {
    if (pendingCount > 0 && !window.confirm("Отменить все несохранённые изменения?")) return;
    clearAllPending()
      .then(() => {
        setStatus("Все несохранённые изменения отменены.");
        return loadBoard();
      })
      .catch((e) => setStatus(String(e.message || e), true));
  });

  btnSave.addEventListener("click", () => {
    saveToJira().catch((e) => setStatus(String(e.message || e), true));
  });

  sprintFilterEl.addEventListener("change", () => {
    selectedSprint = sprintFilterEl.value;
    render();
  });

  updateSaveButton();

  loadSettings()
    .then(() => loadBoard())
    .catch((e) => {
      boardEl.innerHTML = `<div class="error-banner">${escapeHtml(String(e.message || e))}</div>`;
      setStatus("", true);
    });
})();
