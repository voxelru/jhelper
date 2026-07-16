/**
 * Доска: рабочие дни в шапке, строки — сотрудники,
 * блоки — 3 строки (ключ / title / заказчик), подсказка при наведении.
 */

(function () {
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const btnRefresh = document.getElementById("btn-refresh");

  let settings = null;
  /** @type {{ rows: any[], meta: any } | null} */
  let model = null;
  let ppd = 36;

  /** @type {{ task: any, sourceRow: any } | null} */
  let dragState = null;

  const tooltipEl = document.createElement("div");
  tooltipEl.className = "task-tooltip";
  tooltipEl.hidden = true;
  document.body.appendChild(tooltipEl);

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.style.color = isError ? "#c62828" : "#546e7a";
  }

  function packRow(tasks) {
    let off = 0;
    for (const t of tasks) {
      t.startOffsetDays = Math.round(off * 10000) / 10000;
      t.durationDays = t.effortDays;
      off += t.durationDays;
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

  function removeTaskFromAllRows(task) {
    if (!model) return;
    for (const r of model.rows) {
      const i = r.tasks.indexOf(task);
      if (i >= 0) r.tasks.splice(i, 1);
    }
  }

  function insertTaskAtIndex(row, task, index) {
    removeTaskFromAllRows(task);
    let i = Math.max(0, Math.min(index, row.tasks.length));
    row.tasks.splice(i, 0, task);
    task.assigneeId = row.assigneeId;
    task.assigneeName = row.assigneeName;
    packRow(row.tasks);
  }

  function indexFromClientX(rowEl, clientX, tasks) {
    const track = rowEl.querySelector(".row-track");
    const rect = track.getBoundingClientRect();
    const x = clientX - rect.left;
    const dayFloat = x / ppd;
    let acc = 0;
    for (let i = 0; i < tasks.length; i++) {
      const mid = acc + tasks[i].durationDays / 2;
      if (dayFloat < mid) return i;
      acc += tasks[i].durationDays;
    }
    return tasks.length;
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

  function hideTooltip() {
    tooltipEl.hidden = true;
    tooltipEl.innerHTML = "";
  }

  function showTooltip(task, clientX, clientY) {
    const base = ((settings && settings.jiraBaseUrl) || "").replace(/\/$/, "");
    const { startIso, endIso } = resolveTaskDates(task);
    const keyLink = base
      ? `<a href="${escapeHtml(base)}/browse/${encodeURIComponent(task.key)}" target="_blank" rel="noopener noreferrer">${escapeHtml(task.key)}</a>`
      : escapeHtml(task.key);

    tooltipEl.innerHTML = `
      <div class="tt-row"><span class="tt-label">Задача</span><span class="tt-val">${keyLink}</span></div>
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
      if (dragState) return;
      showTooltip(task, e.clientX, e.clientY);
    });
    el.addEventListener("mousemove", (e) => {
      if (dragState || tooltipEl.hidden) return;
      showTooltip(task, e.clientX, e.clientY);
    });
    el.addEventListener("mouseleave", () => {
      hideTooltip();
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

      for (const t of row.tasks) {
        const el = document.createElement("div");
        el.className = "task";
        el.draggable = true;
        el.dataset.issueKey = t.key;
        el.style.background = t.color;
        el.style.left = `${t.startOffsetDays * ppd}px`;
        el.style.width = `${Math.max(4, t.durationDays * ppd - 2)}px`;

        const base = (settings.jiraBaseUrl || "").replace(/\/$/, "");
        const keyHtml = base
          ? `<a class="task-key" href="${escapeHtml(base)}/browse/${encodeURIComponent(t.key)}" target="_blank" rel="noopener noreferrer">${escapeHtml(t.key)}</a>`
          : `<span class="task-key">${escapeHtml(t.key)}</span>`;

        el.innerHTML = `
          <div class="task-line task-line-key">${keyHtml}</div>
          <div class="task-line task-line-title" title="${escapeHtml(t.summary || "")}">${escapeHtml(t.summary || "—")}</div>
          <div class="task-line task-line-customer" title="${escapeHtml(t.customer || "")}">${escapeHtml(t.customer || "—")}</div>
        `;
        wireTaskTooltip(el, t);
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

      const targetRow = row;
      const idx = indexFromClientX(rowEl, e.clientX, targetRow.tasks);

      insertTaskAtIndex(targetRow, draggedTask, idx);
      if (sourceRow !== targetRow) {
        packRow(sourceRow.tasks);
      }
      render();
      setStatus("Порядок изменён только в интерфейсе (в Jira не записывается).");
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
    rebuildAllPacks();
    render();
    setStatus(`Задач: ${countTasks(model)}`);
  }

  function countTasks(m) {
    return m.rows.reduce((a, r) => a + r.tasks.length, 0);
  }

  btnRefresh.addEventListener("click", () => {
    loadBoard().catch((e) => setStatus(String(e.message || e), true));
  });

  loadSettings()
    .then(() => loadBoard())
    .catch((e) => {
      boardEl.innerHTML = `<div class="error-banner">${escapeHtml(String(e.message || e))}</div>`;
      setStatus("", true);
    });
})();
