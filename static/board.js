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
  const summaryEl = document.getElementById("summary");
  const btnRefresh = document.getElementById("btn-refresh");
  const btnUndo = document.getElementById("btn-undo");
  const btnClear = document.getElementById("btn-clear");
  const btnSave = document.getElementById("btn-save");
  const sprintFilterEl = document.getElementById("sprint-filter");
  const customerFilterEl = document.getElementById("customer-filter");
  const labelFilterEl = document.getElementById("label-filter");
  const searchInputEl = document.getElementById("search-input");
  const rangeFromEl = document.getElementById("range-from");
  const rangeToEl = document.getElementById("range-to");
  const btnRangeReset = document.getElementById("btn-range-reset");

  let settings = null;
  /** @type {{ rows: any[], meta: any } | null} */
  let model = null;
  let ppd = 36;
  let pendingCount = 0;
  // Значение пункта «Не определено»: совпасть с настоящим значением поля Jira
  // оно не может, поэтому отличить «не выбрано» от «пусто» получается однозначно.
  const UNSET_VALUE = "\u0000не-определено";

  let selectedSprint = "";
  let selectedCustomer = "";
  let selectedLabel = "";
  let searchQuery = "";
  /** Фильтр по датам: какие календарные дни горизонта показывать на шкале (ISO, включительно). */
  let rangeFrom = "";
  let rangeTo = "";

  // Геометрия блоков внутри строки: отступ сверху/снизу, зазор между
  // задачами, стоящими друг под другом, и минимальная высота блока (px).
  const TASK_INSET = 4;
  const STACK_GAP = 2;
  const MIN_TASK_HEIGHT = 14;
  // Шаг трудозатрат — четверть рабочего дня; сроки всегда в целых днях.
  const EFFORT_STEP = 0.25;

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

  /**
   * Сводка в шапке: сколько задач и трудозатрат сейчас на доске. Пока фильтры
   * что-то скрывают, рядом показывается и общее число загруженных задач.
   */
  function updateSummary(shownTasks, shownEffort) {
    if (!model) {
      summaryEl.textContent = "";
      return;
    }
    const total = countTasks(model);
    const tasksText = shownTasks === total ? `Задач: ${total}` : `Задач: ${shownTasks} из ${total}`;
    summaryEl.textContent = `${tasksText} · ${formatEffort(shownEffort)}`;
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
   * и трудозатраты) встают строго на свой диапазон дат — в том числе когда
   * диапазоны пересекаются: такие задачи идут параллельно, по вертикали их
   * расставляет layoutStack при отрисовке. Остальные — в очередь после самой
   * поздней из них, подряд без зазоров, в порядке приоритета; срок такой
   * задачи — её трудозатраты, округлённые вверх до целого дня. Между
   * полностью заполненными задачами намеренно остаются пустые промежутки,
   * куда можно вручную перетащить любую задачу.
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
      t.startOffsetDays = Math.round(t.dateRangeStartOffsetDays * 10000) / 10000;
      const end = t.startOffsetDays + t.durationDays;
      cursor = cursor === null ? end : Math.max(cursor, end);
    }

    // Задачи без дат не раскладываем задним числом: очередь начинается не
    // раньше сегодняшнего дня (todayOffsetDays — его смещение от начала окна).
    const todayOffset = Number((settings && settings.todayOffsetDays) || 0);
    cursor = cursor === null ? todayOffset : Math.max(cursor, todayOffset);
    for (const t of incomplete) {
      t.durationDays = Math.max(1, Math.ceil(Number(t.effortDays) - 1e-9));
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

  /** Трудозатраты кратны четверти дня и не меньше её. */
  function roundEffort(days) {
    const n = Number(days);
    if (!Number.isFinite(n)) return EFFORT_STEP;
    return Math.max(EFFORT_STEP, Math.round(n / EFFORT_STEP) * EFFORT_STEP);
  }

  /** Загрузка сотрудника задачей: доля рабочего дня, которую она занимает. */
  function taskLoad(task) {
    const duration = Number(task.durationDays) || 1;
    return roundEffort(task.effortDays) / duration;
  }

  /**
   * Раскладывает задачи строки по вертикали. Длина блока — срок задачи в
   * днях, высота — загрузка (трудозатраты / срок), так что площадь блока
   * равна трудозатратам; unit — высота блока при загрузке 100%. Задачи,
   * пересекающиеся по времени, ставятся друг под другом: каждая занимает
   * самое верхнее свободное место. Проставляет top/height (px) и возвращает
   * нижнюю границу самого низкого блока — строка растёт, если суммарная
   * загрузка в какой-то день больше 100%.
   */
  function layoutStack(items, unit) {
    const EPS = 1e-6;
    const sorted = items
      .slice()
      .sort((a, b) => a.task.startOffsetDays - b.task.startOffsetDays || (a.task.key < b.task.key ? -1 : 1));
    const placed = [];
    let maxBottom = 0;

    for (const item of sorted) {
      const start = item.task.startOffsetDays;
      const end = start + item.task.durationDays;
      // Зазор между блоками входит в высоту: две задачи по 50% вместе с
      // зазором занимают ровно unit. Слишком низкий блок нельзя ни прочитать,
      // ни схватить мышью, поэтому высота ограничена снизу.
      const height = Math.max(MIN_TASK_HEIGHT, taskLoad(item.task) * (unit + STACK_GAP) - STACK_GAP);
      const overlapping = placed.filter((p) => p.start < end - EPS && start < p.end - EPS);
      const candidates = [0, ...overlapping.map((p) => p.top + p.height + STACK_GAP)].sort((x, y) => x - y);
      const top = candidates.find((y) =>
        overlapping.every((p) => y + height + STACK_GAP <= p.top + EPS || y >= p.top + p.height + STACK_GAP - EPS)
      );
      item.top = top;
      item.height = height;
      placed.push({ start, end, top, height });
      maxBottom = Math.max(maxBottom, top + height);
    }
    return maxBottom;
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

    // Отдельный пункт для задач, у которых поле не заполнено.
    const unsetOpt = document.createElement("option");
    unsetOpt.value = UNSET_VALUE;
    unsetOpt.textContent = "Не определено";
    selectEl.appendChild(unsetOpt);

    for (const name of values) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      selectEl.appendChild(opt);
    }
    const next =
      currentValue === UNSET_VALUE || values.includes(currentValue) ? currentValue : "";
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

  function populateLabelFilter() {
    const labels = (model && model.meta && model.meta.labels) || [];
    selectedLabel = populateFilterSelect(labelFilterEl, labels, selectedLabel, "Все метки");
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

  /** Совпадение значения поля задачи с выбранным пунктом фильтра. */
  function matchesValueFilter(selected, value) {
    if (!selected) return true;
    if (selected === UNSET_VALUE) return !value;
    return value === selected;
  }

  /** То же для поля-списка (метки): «Не определено» — список пуст. */
  function matchesListFilter(selected, values) {
    if (!selected) return true;
    const list = Array.isArray(values) ? values : [];
    if (selected === UNSET_VALUE) return !list.length;
    return list.includes(selected);
  }

  function taskMatchesFilters(t) {
    if (!matchesValueFilter(selectedSprint, t.sprint)) return false;
    if (!matchesValueFilter(selectedCustomer, t.customer)) return false;
    if (!matchesListFilter(selectedLabel, t.labels)) return false;
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
   * окончания (длина прямоугольника в целых рабочих днях) и трудозатраты
   * (его площадь, кратная четверти дня). Это единственный источник значений,
   * которые уходят в файл изменений.
   */
  function taskPlacement(task) {
    const duration = Math.max(1, Math.round(Number(task.durationDays)));
    const { startIso, endIso } = datesForRange(task.startOffsetDays, duration);
    return { startIso, endIso, durationDays: duration, effortDays: roundEffort(task.effortDays) };
  }

  /**
   * Ставит задачу в точный диапазон: от offsetDays на durationDays целых
   * рабочих дней. Единая точка для перетаскивания и растягивания — после неё
   * даты задачи согласованы с её положением и с тем, что уйдёт в файл
   * изменений. Трудозатраты при этом не меняются: та же работа просто
   * распределяется по другому числу дней, и блок становится ниже или выше.
   */
  function applyPlacement(task, offsetDays, durationDays) {
    const duration = Math.max(1, Math.round(Number(durationDays)));
    task.startOffsetDays = Math.round(Number(offsetDays));
    task.durationDays = duration;
    task.effortDays = roundEffort(task.effortDays);
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

  /** «50% — 1 md за 2 раб. дн.»: чем объясняется высота блока. */
  function formatLoad(task) {
    const duration = Math.max(1, Math.round(Number(task.durationDays) || 1));
    return `${Math.round(taskLoad(task) * 100)}% — ${formatEffort(task.effortDays)} за ${duration} раб. дн.`;
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
    if (task.pendingPlacement) pendingParts.push("даты и трудозатраты");
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
      <div class="tt-row"><span class="tt-label">Загрузка</span><span class="tt-val">${escapeHtml(formatLoad(task))}</span></div>
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
      // диапазон от него на новый срок — в целых днях. Трудозатраты те же,
      // поэтому блок становится длиннее и ниже (или короче и выше).
      const anchorOffset = task.startOffsetDays;
      let changed = false;
      resizeState = { task, row };

      const onMove = (ev) => {
        const deltaDays = (ev.clientX - startX) / ppd;
        const next = Math.max(1, Math.round(startDuration + deltaDays));
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
        finishResize(task, changed);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  /**
   * Изменение трудозатрат: нижний край блока тянется по вертикали. Срок не
   * меняется, поэтому высота (загрузка) прямо задаёт площадь — трудозатраты,
   * с шагом в четверть дня. unit — высота блока при загрузке 100%.
   */
  function wireTaskEffortResize(handleEl, task, row, startHeight, unit) {
    handleEl.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      hideTooltip();

      const startY = e.clientY;
      let changed = false;
      resizeState = { task, row };

      const onMove = (ev) => {
        const height = startHeight + (ev.clientY - startY);
        const load = (height + STACK_GAP) / (unit + STACK_GAP);
        const next = roundEffort(load * task.durationDays);
        if (next !== task.effortDays) {
          task.effortDays = next;
          changed = true;
          render();
        }
      };

      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        finishResize(task, changed);
      };

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }

  /** Общее завершение растягивания: записать изменение или просто перерисовать. */
  function finishResize(task, changed) {
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
  }

  function render() {
    boardEl.innerHTML = "";
    hideTooltip();
    updateSummary(0, 0);
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

    let shownTasks = 0;
    let shownEffort = 0;
    const baseRowHeight =
      parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row-height")) || 72;

    for (const row of model.rows) {
      // Задача попадает на доску, если проходит фильтры и хотя бы частью
      // попадает в выбранный диапазон дат. Строка сотрудника остаётся и без
      // задач — состав и порядок строк не должны прыгать при смене фильтров.
      const visibleTasks = [];
      for (const t of row.tasks) {
        if (!taskMatchesFilters(t)) continue;
        const box = taskViewBox(t, view);
        if (box) visibleTasks.push({ task: t, box });
      }

      const rowEl = document.createElement("div");
      rowEl.className = "board-row" + (visibleTasks.length ? "" : " no-tasks");
      rowEl.dataset.assigneeId = row.assigneeId || "";

      // Рядом с сотрудником — суммарная оценка задач, попавших в текущие
      // фильтры (спринт, бизнес-партнёр, поиск, окно дат).
      const rowEffort = visibleTasks.reduce((sum, { task }) => {
        const n = Number(task.effortDays);
        return sum + (Number.isFinite(n) ? n : 0);
      }, 0);
      shownTasks += visibleTasks.length;
      shownEffort += rowEffort;

      const label = document.createElement("div");
      label.className = "row-label";
      const nameEl = document.createElement("span");
      nameEl.className = "row-name";
      nameEl.textContent = row.assigneeName || "—";
      const totalEl = document.createElement("span");
      totalEl.className = "row-total";
      totalEl.textContent = formatEffort(rowEffort);
      totalEl.title = `Суммарная оценка показанных задач (${visibleTasks.length} из ${row.tasks.length}) с учётом фильтров`;
      label.append(nameEl, totalEl);
      rowEl.appendChild(label);

      const track = document.createElement("div");
      track.className = "row-track";
      track.style.width = `${trackWidth(view)}px`;

      // Высота блока — загрузка сотрудника задачей, площадь — трудозатраты.
      // Строка растёт, когда задачи, идущие параллельно, не помещаются в неё.
      const unit = baseRowHeight - 2 * TASK_INSET;
      const stackBottom = layoutStack(visibleTasks, unit);
      track.style.minHeight = `${Math.max(baseRowHeight, stackBottom + 2 * TASK_INSET)}px`;

      for (const { task: t, box, top, height } of visibleTasks) {
        const el = document.createElement("div");
        const isResizing = !!(resizeState && resizeState.task === t);
        el.className =
          "task" +
          (height < 26 ? " tiny" : height < 48 ? " short" : "") +
          (t.pendingAssignee || t.pendingPlacement ? " pending" : "") +
          (isResizing ? " resizing" : "") +
          (box.clipLeft ? " clip-left" : "") +
          (box.clipRight ? " clip-right" : "");
        el.draggable = true;
        el.dataset.issueKey = t.key;
        el.style.background = t.color;
        el.style.left = `${box.left}px`;
        el.style.width = `${box.width}px`;
        el.style.top = `${TASK_INSET + top}px`;
        el.style.height = `${height}px`;

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
          resizeEl.title = "Изменить срок (трудозатраты не меняются)";
          el.appendChild(resizeEl);
          wireTaskResize(resizeEl, t, row);
        }
        const effortEl = document.createElement("div");
        effortEl.className = "task-resize-effort";
        effortEl.title = "Изменить трудозатраты (срок не меняется)";
        el.appendChild(effortEl);
        wireTaskEffortResize(effortEl, t, row, height, unit);

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
    updateSummary(shownTasks, shownEffort);
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
    const { startIso, endIso, effortDays } = taskPlacement(task);
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
        effortDays,
        originalStartDate: originalStart,
        originalEndDate: originalEnd,
        originalEffortDays: originalEffort,
      }),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || r.statusText);

    // Храним в задаче ровно то, что записано в файл изменений.
    task.jiraStartDate = startIso;
    task.jiraEndDate = endIso;
    task.effortDays = effortDays;
    task.pendingPlacement =
      startIso !== originalStart || endIso !== originalEnd || effortDays !== originalEffort;
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
      // расчётной очереди. Если там уже есть задачи, они пойдут параллельно.
      // Срок и трудозатраты задачи при переносе не меняются.
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
    populateLabelFilter();
    rebuildAllPacks();
    render();
    setStatus(pendingCount ? `Несохранённых изменений: ${pendingCount}` : "");
  }

  function countTasks(m) {
    return m.rows.reduce((a, r) => a + r.tasks.length, 0);
  }

  const FIELD_LABELS = {
    assignee: "исполнитель",
    effort: "трудозатраты",
    startDate: "дата начала",
    endDate: "дата окончания",
  };

  /** Короткое описание полей, которые сервер не стал писать в Jira, с причиной. */
  function describeSkipped(skipped) {
    const byReason = new Map();
    for (const s of skipped) {
      const label = FIELD_LABELS[s.field] || s.field;
      const reason = s.reason || "причина не указана";
      const key = `${label} — ${reason}`;
      byReason.set(key, (byReason.get(key) || 0) + 1);
    }
    return Array.from(byReason.entries())
      .map(([text, count]) => (count > 1 ? `${text} (задач: ${count})` : text))
      .join("; ");
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
      const skipped = Array.isArray(data.skipped) ? data.skipped : [];
      const skippedNote = skipped.length ? ` Не записано в Jira: ${describeSkipped(skipped)}.` : "";
      // Итог сохранения показываем после перезагрузки доски, иначе её
      // собственный статус тут же затрёт сообщение об ошибках и пропусках.
      await loadBoard();
      if (failed.length) {
        const keys = failed.map((f) => f.key).join(", ");
        setStatus(`Сохранено: ${data.saved || 0}. Ошибки: ${keys}.${skippedNote}`, true);
      } else if (skipped.length) {
        setStatus(`Сохранено в Jira: ${data.saved || 0}.${skippedNote}`, true);
      } else {
        setStatus(`Сохранено в Jira: ${data.saved || 0}`);
      }
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
      .then(() => loadBoard())
      .then(() => setStatus("Последнее действие отменено."))
      .catch((e) => setStatus(String(e.message || e), true));
  });

  btnClear.addEventListener("click", () => {
    if (pendingCount > 0 && !window.confirm("Отменить все несохранённые изменения?")) return;
    clearAllPending()
      .then(() => loadBoard())
      .then(() => setStatus("Все несохранённые изменения отменены."))
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

  labelFilterEl.addEventListener("change", () => {
    selectedLabel = labelFilterEl.value;
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
