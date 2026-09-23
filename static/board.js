/**
 * Доска: рабочие дни выбранного диапазона в шапке, строки — сотрудники,
 * блоки — 3 строки (ключ / title / заказчик), подсказка при наведении.
 * Смена исполнителя (drag-and-drop между строками) и положение задачи
 * (перетаскивание по датам, растягивание прямоугольника) копятся до нажатия
 * «Сохранить в Jira». Любое перемещение или изменение размера пишет в файл
 * изменений сразу три параметра: дату начала, дату окончания и
 * продолжительность.
 */

(function () {
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnUndo = document.getElementById("btn-undo");
  const btnClear = document.getElementById("btn-clear");
  const btnSave = document.getElementById("btn-save");
  const sprintFilterEl = document.getElementById("sprint-filter");
  const customerFilterEl = document.getElementById("customer-filter");
  const searchInputEl = document.getElementById("search-input");
  const rangeFromEl = document.getElementById("range-from");
  const rangeToEl = document.getElementById("range-to");
  const btnRangeReset = document.getElementById("btn-range-reset");

  let settings = null;
  /** @type {{ rows: any[], meta: any } | null} */
  let model = null;
  let ppd = 36;
  let pendingCount = 0;
  let selectedSprint = "";
  let selectedCustomer = "";
  let searchQuery = "";
  /** Фильтр по датам: какие календарные дни горизонта показывать на шкале (ISO, включительно). */
  let rangeFrom = "";
  let rangeTo = "";

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
   * Раскладывает задачи строки без автоматического заполнения промежутков:
   * полностью заполненные задачи (isComplete — есть дата начала, окончания
   * и трудозатраты) встают строго на свой диапазон дат. Остальные — в
   * очередь после самой поздней из них, подряд без зазоров, в порядке
   * приоритета. Между полностью заполненными задачами намеренно остаются
   * пустые промежутки, куда можно вручную перетащить любую задачу.
   */
  function packRow(tasks) {
    const complete = tasks.filter((t) => t.isComplete);
    const incomplete = tasks.filter((t) => !t.isComplete);
    complete.sort((a, b) => {
      if (a.dateRangeStartOffsetDays !== b.dateRangeStartOffsetDays) {
        return a.dateRangeStartOffsetDays - b.dateRangeStartOffsetDays;
      }
      return compareSortKeys(taskSortKey(a), taskSortKey(b));
    });
    incomplete.sort((a, b) => compareSortKeys(taskSortKey(a), taskSortKey(b)));

    // cursor = null, пока не размещена первая задача: смещение может быть и
    // отрицательным (задача началась раньше выбранного окна).
    let cursor = null;
    for (const t of complete) {
      t.durationDays = t.dateRangeDurationDays;
      const start =
        cursor === null ? t.dateRangeStartOffsetDays : Math.max(t.dateRangeStartOffsetDays, cursor);
      t.startOffsetDays = Math.round(start * 10000) / 10000;
      cursor = start + t.durationDays;
    }

    // Задачи без дат не раскладываем задним числом: очередь начинается не
    // раньше сегодняшнего дня (todayOffsetDays — его смещение от начала окна).
    const todayOffset = Number((settings && settings.todayOffsetDays) || 0);
    cursor = cursor === null ? todayOffset : Math.max(cursor, todayOffset);
    for (const t of incomplete) {
      t.durationDays = t.effortDays;
      t.startOffsetDays = Math.round(cursor * 10000) / 10000;
      cursor += t.durationDays;
    }
  }

  /**
   * Видимое окно шкалы: часть рабочих дней горизонта, попадающая в фильтр по
   * датам. startIndex/endIndex — границы окна в тех же единицах, в которых
   * задачи хранят startOffsetDays (номер рабочего дня от начала горизонта),
   * endIndex не включается.
   */
  function visibleWindow() {
    const dates = (settings && settings.workingDates) || [];
    let startIndex = 0;
    let endIndex = dates.length;
    if (rangeFrom) {
      while (startIndex < endIndex && dates[startIndex] < rangeFrom) startIndex++;
    }
    if (rangeTo) {
      while (endIndex > startIndex && dates[endIndex - 1] > rangeTo) endIndex--;
    }
    return { startIndex, endIndex, dates: dates.slice(startIndex, endIndex) };
  }

  /**
   * Положение блока задачи внутри видимого окна. Задача, выходящая за границу
   * диапазона, отображается только своей попадающей в него частью
   * (clipLeft/clipRight помечают обрезанные края). null — задача целиком вне
   * окна и не показывается.
   */
  function taskViewBox(task, view) {
    const taskStart = task.startOffsetDays;
    const taskEnd = task.startOffsetDays + task.durationDays;
    const from = Math.max(taskStart, view.startIndex);
    const to = Math.min(taskEnd, view.endIndex);
    if (to <= from) return null;
    return {
      left: (from - view.startIndex) * ppd,
      width: Math.max(4, (to - from) * ppd - 2),
      clipLeft: taskStart < view.startIndex,
      clipRight: taskEnd > view.endIndex,
    };
  }

  function trackWidth(view) {
    if (!settings) return 800;
    return Math.max(1, view.dates.length) * ppd;
  }

  function rebuildAllPacks() {
    if (!model) return;
    for (const row of model.rows) {
      packRow(row.tasks);
    }
  }

  function populateFilterSelect(selectEl, values, currentValue, allLabel) {
    selectEl.innerHTML = "";
    const allOpt = document.createElement("option");
    allOpt.value = "";
    allOpt.textContent = allLabel;
    selectEl.appendChild(allOpt);
    for (const name of values) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      selectEl.appendChild(opt);
    }
    const next = values.includes(currentValue) ? currentValue : "";
    selectEl.value = next;
    return next;
  }

  function populateSprintFilter() {
    const sprints = (model && model.meta && model.meta.sprints) || [];
    selectedSprint = populateFilterSelect(sprintFilterEl, sprints, selectedSprint, "Все спринты");
  }

  function populateCustomerFilter() {
    const customers = (model && model.meta && model.meta.customers) || [];
    selectedCustomer = populateFilterSelect(customerFilterEl, customers, selectedCustomer, "Все бизнес-партнёры");
  }

  function syncRangeInputs() {
    rangeFromEl.value = rangeFrom;
    rangeToEl.value = rangeTo;
  }

  /** Query-строка окна шкалы для /api/settings и /api/board. */
  function rangeQuery() {
    const params = new URLSearchParams();
    if (rangeFrom) params.set("from", rangeFrom);
    if (rangeTo) params.set("to", rangeTo);
    const q = params.toString();
    return q ? `?${q}` : "";
  }

  /**
   * Подхватывает окно, которое реально построил сервер: он может подрезать
   * слишком широкий интервал (maxWindowWorkingDays) или достроить вторую
   * границу, если заполнена только одна.
   */
  function adoptServerWindow(meta) {
    if (!meta) return;
    if (meta.planningStart) rangeFrom = meta.planningStart;
    if (meta.planningEnd) rangeTo = meta.planningEnd;
    syncRangeInputs();
  }

  /** Диапазон по умолчанию — горизонт планирования от сегодняшнего дня. */
  function resetDateRange() {
    rangeFrom = "";
    rangeTo = "";
    syncRangeInputs();
  }

  function taskMatchesFilters(t) {
    if (selectedSprint && t.sprint !== selectedSprint) return false;
    if (selectedCustomer && t.customer !== selectedCustomer) return false;
    if (searchQuery) {
      const key = (t.key || "").toLowerCase();
      const summary = (t.summary || "").toLowerCase();
      if (!key.includes(searchQuery) && !summary.includes(searchQuery)) return false;
    }
    return true;
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
    const view = visibleWindow();
    // Координата внутри трека — это смещение от начала видимого окна, а не от
    // начала горизонта планирования.
    const raw = view.startIndex + (clientX - rect.left) / ppd;
    const maxOffset = Math.max(0, (settings.workingDayCount || 1) - 1);
    return Math.min(maxOffset, Math.max(0, Math.round(raw)));
  }

  function datesForRange(offsetDays, durationDays) {
    return {
      startIso: boardDateAtOffset(offsetDays, false),
      endIso: boardDateAtOffset(offsetDays + durationDays, true),
    };
  }

  /**
   * Текущее положение задачи на доске тремя параметрами: дата начала, дата
   * окончания и продолжительность (ширина прямоугольника в рабочих днях).
   * Это единственный источник значений, которые уходят в файл изменений.
   */
  function taskPlacement(task) {
    const duration = Math.round(Number(task.durationDays) * 10000) / 10000;
    const { startIso, endIso } = datesForRange(task.startOffsetDays, duration);
    return { startIso, endIso, durationDays: duration };
  }

  /**
   * Ставит задачу в точный диапазон: от offsetDays на durationDays рабочих
   * дней. Единая точка для перетаскивания и растягивания — после неё все три
   * параметра задачи (дата начала, дата окончания, продолжительность)
   * согласованы между собой и с тем, что уйдёт в файл изменений.
   */
  function applyPlacement(task, offsetDays, durationDays) {
    const minDays = (settings && settings.minEffortWorkingDays) || 0.25;
    const duration = Math.max(minDays, Math.round(Number(durationDays) * 10000) / 10000);
    task.startOffsetDays = Math.round(Number(offsetDays) * 10000) / 10000;
    task.durationDays = duration;
    task.effortDays = duration;
    task.isComplete = true;
    task.dateRangeStartOffsetDays = task.startOffsetDays;
    task.dateRangeDurationDays = duration;
    const { startIso, endIso } = taskPlacement(task);
    task.jiraStartDate = startIso;
    task.jiraEndDate = endIso;
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
    let startIso = null;
    let endIso = null;

    if (t.jiraStartDate) {
      startIso = t.jiraStartDate;
    } else {
      startIso = boardDateAtOffset(t.startOffsetDays, false);
    }

    if (t.jiraEndDate) {
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
    if (task.pendingPlacement) pendingParts.push("даты и продолжительность");
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
      // Левый край при растягивании остаётся на месте: задача занимает точный
      // диапазон от него на новую продолжительность.
      const anchorOffset = task.startOffsetDays;
      const minDays = (settings && settings.minEffortWorkingDays) || 0.25;
      let changed = false;
      resizeState = { task, row };

      const onMove = (ev) => {
        const deltaDays = (ev.clientX - startX) / ppd;
        let next = Math.round((startDuration + deltaDays) * 4) / 4;
        next = Math.max(minDays, next);
        if (next !== task.durationDays) {
          applyPlacement(task, anchorOffset, next);
          changed = true;
          packRow(row.tasks);
          render();
        }
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        resizeState = null;
        if (!changed) {
          render();
          return;
        }

        recordPendingPlacement(task)
          .then(() => {
            setStatus(
              pendingCount
                ? `Изменения накоплены (${pendingCount}). Нажмите «Сохранить в Jira».`
                : "Изменения возвращены к значениям из Jira; записи удалены."
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

    const view = visibleWindow();
    if (!view.dates.length) {
      boardEl.innerHTML = `<div class="empty-banner">В выбранном диапазоне дат нет рабочих дней.</div>`;
      boardEl.setAttribute("aria-busy", "false");
      return;
    }

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
    timeline.style.width = `${trackWidth(view)}px`;

    for (const iso of view.dates) {
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
      // Задача попадает на доску, если проходит фильтры и хотя бы частью
      // попадает в выбранный диапазон дат.
      const visibleTasks = [];
      for (const t of row.tasks) {
        if (!taskMatchesFilters(t)) continue;
        const box = taskViewBox(t, view);
        if (box) visibleTasks.push({ task: t, box });
      }
      if (!visibleTasks.length) continue;

      const rowEl = document.createElement("div");
      rowEl.className = "board-row";
      rowEl.dataset.assigneeId = row.assigneeId || "";

      // Рядом с сотрудником — суммарная оценка задач, попавших в текущие
      // фильтры (спринт, бизнес-партнёр, поиск, окно дат).
      const rowEffort = visibleTasks.reduce((sum, { task }) => {
        const n = Number(task.effortDays);
        return sum + (Number.isFinite(n) ? n : 0);
      }, 0);

      const label = document.createElement("div");
      label.className = "row-label";
      const nameEl = document.createElement("span");
      nameEl.className = "row-name";
      nameEl.textContent = row.assigneeName || "—";
      const totalEl = document.createElement("span");
      totalEl.className = "row-total";
      totalEl.textContent = formatEffort(rowEffort);
      totalEl.title = `Суммарная оценка показанных задач (${visibleTasks.length}) с учётом фильтров`;
      label.append(nameEl, totalEl);
      rowEl.appendChild(label);

      const track = document.createElement("div");
      track.className = "row-track";
      track.style.width = `${trackWidth(view)}px`;

      for (const { task: t, box } of visibleTasks) {
        const el = document.createElement("div");
        const isResizing = !!(resizeState && resizeState.task === t);
        el.className =
          "task" +
          (t.pendingAssignee || t.pendingPlacement ? " pending" : "") +
          (isResizing ? " resizing" : "") +
          (box.clipLeft ? " clip-left" : "") +
          (box.clipRight ? " clip-right" : "");
        el.draggable = true;
        el.dataset.issueKey = t.key;
        el.style.background = t.color;
        el.style.left = `${box.left}px`;
        el.style.width = `${box.width}px`;

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
        // Задачу, обрезанную границей окна, тянуть за край нельзя: её
        // настоящие даты лежат за пределами видимой шкалы.
        if (!box.clipLeft && !box.clipRight) {
          const resizeEl = document.createElement("div");
          resizeEl.className = "task-resize";
          resizeEl.title = "Изменить продолжительность";
          el.appendChild(resizeEl);
          wireTaskResize(resizeEl, t, row);
        }

        wireTaskTooltip(el, t);
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

  /**
   * Пишет в файл изменений положение задачи — всегда все три параметра сразу:
   * дата начала, дата окончания и продолжительность. Если задача вернулась
   * ровно к исходным значениям из Jira, сервер удаляет запись целиком.
   */
  async function recordPendingPlacement(task) {
    const { startIso, endIso, durationDays } = taskPlacement(task);
    const originalStart = task.originalJiraStartDate || null;
    const originalEnd = task.originalJiraEndDate || null;
    const originalEffort = task.originalEffortDays;

    const r = await fetch("/api/pending/placement", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: task.key,
        startDate: startIso,
        endDate: endIso,
        effortDays: durationDays,
        originalStartDate: originalStart,
        originalEndDate: originalEnd,
        originalEffortDays: originalEffort,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);

    // Раскладка строки могла сдвинуть задачу вправо от точки отпускания —
    // храним в задаче ровно то, что записано в файл изменений.
    task.jiraStartDate = startIso;
    task.jiraEndDate = endIso;
    task.pendingPlacement =
      startIso !== originalStart || endIso !== originalEnd || durationDays !== originalEffort;
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

      const targetRow = row;
      const dropOffset = offsetDaysFromClientX(rowEl, e.clientX);

      removeTaskFromAllRows(draggedTask);
      targetRow.tasks.push(draggedTask);
      draggedTask.assigneeId = targetRow.assigneeId;
      draggedTask.assigneeName = targetRow.assigneeName;
      const originalAssignee = draggedTask.originalAssigneeId;
      draggedTask.pendingAssignee =
        originalAssignee != null && String(originalAssignee) !== String(targetRow.assigneeId);

      // Перетаскивание — явное указание позиции: задача занимает диапазон от
      // dropOffset и на текущую ширину вперёд и перестаёт зависеть от
      // расчётной очереди.
      applyPlacement(draggedTask, dropOffset, draggedTask.durationDays);

      packRow(targetRow.tasks);
      if (sourceRow !== targetRow) packRow(sourceRow.tasks);
      render();

      // Положение пишем всегда — перемещение задаёт все три параметра сразу
      // (дата начала, дата окончания, продолжительность). Записи идут строго
      // по очереди: сервер читает и перезаписывает один и тот же файл.
      const assigneeChanged = String(prevAssigneeId) !== String(targetRow.assigneeId);
      recordPendingPlacement(draggedTask)
        .then(() => (assigneeChanged ? recordPendingAssignee(draggedTask) : null))
        .then(() => {
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
    const r = await fetch(`/api/board${rangeQuery()}`);
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
    // Шкалу берём из ответа: окно строит сервер, и оно могло быть подрезано.
    if (data.meta && settings) {
      if (Array.isArray(data.meta.workingDates)) {
        settings.workingDates = data.meta.workingDates;
        settings.workingDayCount = data.meta.workingDates.length;
      }
      if (data.meta.planningStart) settings.planningStart = data.meta.planningStart;
      if (data.meta.planningEnd) settings.planningEnd = data.meta.planningEnd;
      settings.todayOffsetDays = Number(data.meta.todayOffsetDays || 0);
    }
    pendingCount = (data.meta && data.meta.pendingCount) || 0;
    adoptServerWindow(data.meta);
    updateSaveButton();
    populateSprintFilter();
    populateCustomerFilter();
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

  customerFilterEl.addEventListener("change", () => {
    selectedCustomer = customerFilterEl.value;
    render();
  });

  searchInputEl.addEventListener("input", () => {
    searchQuery = searchInputEl.value.trim().toLowerCase();
    render();
  });

  // Даты в фильтре ничем не ограничены — окно можно увести и в прошлое.
  // Шкалу на новый интервал строит сервер, поэтому доску перезапрашиваем.
  function reloadForRange() {
    syncRangeInputs();
    loadBoard().catch((e) => setStatus(String(e.message || e), true));
  }

  rangeFromEl.addEventListener("change", () => {
    rangeFrom = rangeFromEl.value || "";
    if (rangeFrom && rangeTo && rangeFrom > rangeTo) rangeTo = rangeFrom;
    reloadForRange();
  });

  rangeToEl.addEventListener("change", () => {
    rangeTo = rangeToEl.value || "";
    if (rangeFrom && rangeTo && rangeTo < rangeFrom) rangeFrom = rangeTo;
    reloadForRange();
  });

  btnRangeReset.addEventListener("click", () => {
    resetDateRange();
    reloadForRange();
  });

  updateSaveButton();

  loadSettings()
    .then(() => loadBoard())
    .catch((e) => {
      boardEl.innerHTML = `<div class="error-banner">${escapeHtml(String(e.message || e))}</div>`;
      setStatus("", true);
    });
})();
