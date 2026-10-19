/**
 * Списание времени: дни выбранного окна в шапке, строки — сотрудники, в
 * клетке дня — задачи, по которым сотрудник в этот день списал время.
 * Высота блока — списанные часы (полный рабочий день — вся высота строки),
 * но не меньше одной строки текста; если блоки дня не помещаются, строка
 * растёт вниз. Страница только читает ворклоги и ничего не пишет в Jira.
 */

(function () {
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const summaryEl = document.getElementById("summary");
  const btnRefresh = document.getElementById("btn-refresh");
  const searchInputEl = document.getElementById("search-input");
  const rangeFromEl = document.getElementById("range-from");
  const rangeToEl = document.getElementById("range-to");
  const btnRangePrev = document.getElementById("btn-range-prev");
  const btnRangeNext = document.getElementById("btn-range-next");
  const btnRangeReset = document.getElementById("btn-range-reset");

  /** @type {{ rows: any[], issues: Record<string, any>, days: any[], meta: any } | null} */
  let model = null;
  let searchQuery = "";
  let rangeFrom = "";
  let rangeTo = "";

  // Геометрия блоков: отступ сверху/снизу в строке, зазор между блоками,
  // отступ блока от границ столбца дня и минимальная высота блока (px).
  const ENTRY_INSET = 4;
  const STACK_GAP = 2;
  const COLUMN_PAD = 2;
  const MIN_ENTRY_HEIGHT = 16;
  // С этой высоты в блоке есть место для второй строки — заголовка задачи.
  const TWO_LINE_HEIGHT = 30;

  const tooltipEl = document.createElement("div");
  tooltipEl.className = "task-tooltip";
  tooltipEl.hidden = true;
  document.body.appendChild(tooltipEl);

  function setStatus(text, isError) {
    statusEl.textContent = text || "";
    statusEl.style.color = isError ? "#c62828" : "#546e7a";
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatDateRu(iso) {
    const m = String(iso || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? `${m[3]}.${m[2]}.${m[1]}` : iso || "—";
  }

  const HOURS_FORMAT = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });

  /** Секунды -> «4 ч», «1,5 ч». */
  function formatHours(seconds) {
    return `${HOURS_FORMAT.format((Number(seconds) || 0) / 3600)} ч`;
  }

  function isoAddDays(iso, days) {
    const d = new Date(`${iso}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }

  function daysBetween(fromIso, toIso) {
    return Math.round((new Date(`${toIso}T00:00:00Z`) - new Date(`${fromIso}T00:00:00Z`)) / 86400000);
  }

  function jiraIssueUrl(key) {
    const base = ((model && model.meta && model.meta.jiraBaseUrl) || "").replace(/\/$/, "");
    return base ? `${base}/browse/${encodeURIComponent(key)}` : "";
  }

  function issueInfo(key) {
    return (model && model.issues && model.issues[key]) || { key, summary: key };
  }

  /**
   * Запись проходит поиск, если запрос есть в номере или заголовке задачи.
   * Совпадение с именем сотрудника показывает все его записи.
   */
  function entryMatches(entry, row) {
    if (!searchQuery) return true;
    if ((row.authorName || "").toLowerCase().includes(searchQuery)) return true;
    const issue = issueInfo(entry.key);
    return (
      entry.key.toLowerCase().includes(searchQuery) ||
      (issue.summary || "").toLowerCase().includes(searchQuery)
    );
  }

  function hideTooltip() {
    tooltipEl.hidden = true;
    tooltipEl.innerHTML = "";
  }

  function showTooltip(entry, row, dayTotal, clientX, clientY) {
    const issue = issueInfo(entry.key);
    const url = jiraIssueUrl(entry.key);
    const keyHtml = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(entry.key)}</a>`
      : escapeHtml(entry.key);
    const comments = (entry.comments || []).slice(0, 5);
    const commentsHtml = comments.length
      ? `<div class="tt-row"><span class="tt-label">Комментарий</span><span class="tt-val">${comments.map(escapeHtml).join("<br>")}</span></div>`
      : "";
    tooltipEl.innerHTML = `
      <div class="tt-row"><span class="tt-label">Задача</span><span class="tt-val">${keyHtml}</span></div>
      <div class="tt-row"><span class="tt-label">Тип</span><span class="tt-val">${escapeHtml(issue.issueType || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Статус</span><span class="tt-val">${escapeHtml(issue.status || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Название</span><span class="tt-val">${escapeHtml(issue.summary || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Заказчик</span><span class="tt-val">${escapeHtml(issue.customer || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Сотрудник</span><span class="tt-val">${escapeHtml(row.authorName || "—")}</span></div>
      <div class="tt-row"><span class="tt-label">Дата</span><span class="tt-val">${escapeHtml(formatDateRu(entry.date))}</span></div>
      <div class="tt-row"><span class="tt-label">Списано</span><span class="tt-val">${escapeHtml(formatHours(entry.seconds))}</span></div>
      <div class="tt-row"><span class="tt-label">Всего за день</span><span class="tt-val">${escapeHtml(formatHours(dayTotal))}</span></div>
      ${commentsHtml}
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

  /** Подсветка всех дней, в которые списывали время на ту же задачу. */
  function highlightIssue(key, on) {
    boardEl.querySelectorAll(".wl-entry").forEach((el) => {
      el.classList.toggle("related", on && el.dataset.issueKey === key);
    });
  }

  function wireEntry(el, entry, row, dayTotal) {
    el.addEventListener("mouseenter", (e) => {
      highlightIssue(entry.key, true);
      showTooltip(entry, row, dayTotal, e.clientX, e.clientY);
    });
    el.addEventListener("mousemove", (e) => showTooltip(entry, row, dayTotal, e.clientX, e.clientY));
    el.addEventListener("mouseleave", () => {
      highlightIssue(entry.key, false);
      hideTooltip();
    });
    el.addEventListener("click", () => {
      const url = jiraIssueUrl(entry.key);
      if (url) window.open(url, "_blank", "noopener");
    });
  }

  function render() {
    boardEl.innerHTML = "";
    hideTooltip();
    summaryEl.textContent = "";
    if (!model) return;

    const meta = model.meta || {};
    const ppd = Number(meta.pixelsPerDay || 120);
    const hoursPerDay = Number(meta.hoursPerDay || 8);
    const fullDaySeconds = hoursPerDay * 3600;
    boardEl.style.setProperty("--ppd", `${ppd}px`);

    const days = model.days || [];
    if (!days.length) {
      boardEl.innerHTML = `<div class="empty-banner">В выбранном диапазоне дат нет рабочих дней.</div>`;
      boardEl.setAttribute("aria-busy", "false");
      return;
    }
    if (!model.rows.length) {
      boardEl.innerHTML = `<div class="empty-banner">За ${escapeHtml(formatDateRu(meta.from))} — ${escapeHtml(formatDateRu(meta.to))} никто не списывал время.</div>`;
      boardEl.setAttribute("aria-busy", "false");
      summaryEl.textContent = "Списано: 0 ч";
      return;
    }
    const dayIndex = new Map(days.map((d, i) => [d.date, i]));
    const width = days.length * ppd;

    // Записи, прошедшие поиск, — по ним считаются и суммы в шапке.
    const visibleByRow = model.rows.map((row) => row.entries.filter((e) => entryMatches(e, row)));
    const totalsByDay = new Map();
    for (const entries of visibleByRow) {
      for (const e of entries) totalsByDay.set(e.date, (totalsByDay.get(e.date) || 0) + e.seconds);
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
    timeline.style.width = `${width}px`;
    for (const day of days) {
      const d = new Date(`${day.date}T00:00:00`);
      const cell = document.createElement("div");
      cell.className =
        "timeline-day" + (day.weekend ? " wl-weekend" : "") + (day.date === meta.today ? " wl-today" : "");
      cell.style.width = `${ppd}px`;
      const total = totalsByDay.get(day.date) || 0;
      cell.innerHTML =
        `<div>${d.getDate()}.${d.getMonth() + 1}</div>` +
        `<div style="opacity:.7">${["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"][d.getDay()]}</div>` +
        (total ? `<div class="wl-day-total">${escapeHtml(formatHours(total))}</div>` : "");
      cell.title = total ? `Списано за день всеми сотрудниками: ${formatHours(total)}` : "";
      timeline.appendChild(cell);
    }
    header.appendChild(timeline);
    inner.appendChild(header);

    const baseRowHeight =
      parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row-height")) || 72;
    // Полный рабочий день занимает всю высоту строки.
    const unit = baseRowHeight - 2 * ENTRY_INSET;

    let shownSeconds = 0;
    const shownIssues = new Set();
    let shownPeople = 0;

    model.rows.forEach((row, rowIndex) => {
      const entries = visibleByRow[rowIndex];
      const rowSeconds = entries.reduce((sum, e) => sum + e.seconds, 0);
      shownSeconds += rowSeconds;
      if (entries.length) shownPeople++;
      entries.forEach((e) => shownIssues.add(e.key));

      const rowEl = document.createElement("div");
      rowEl.className = "board-row" + (entries.length ? "" : " wl-faded");

      const label = document.createElement("div");
      label.className = "row-label";
      const nameEl = document.createElement("span");
      nameEl.className = "row-name";
      nameEl.textContent = row.authorName || "—";
      nameEl.title = row.authorName || "";
      const totalEl = document.createElement("span");
      totalEl.className = "row-total";
      totalEl.textContent = formatHours(rowSeconds);
      totalEl.title = `Списано за выбранные дни${searchQuery ? " с учётом поиска" : ""}`;
      label.append(nameEl, totalEl);
      rowEl.appendChild(label);

      const track = document.createElement("div");
      track.className = "row-track";
      track.style.width = `${width}px`;

      days.forEach((day, i) => {
        if (!day.weekend) return;
        const col = document.createElement("div");
        col.className = "wl-weekend-col";
        col.style.left = `${i * ppd}px`;
        col.style.width = `${ppd}px`;
        track.appendChild(col);
      });

      // Записи одного дня стоят друг под другом, самые большие — сверху.
      const byDay = new Map();
      for (const e of entries) {
        if (!dayIndex.has(e.date)) continue;
        if (!byDay.has(e.date)) byDay.set(e.date, []);
        byDay.get(e.date).push(e);
      }

      let stackBottom = 0;
      for (const [date, dayEntries] of byDay) {
        dayEntries.sort((a, b) => b.seconds - a.seconds || (a.key < b.key ? -1 : 1));
        const dayTotal = dayEntries.reduce((sum, e) => sum + e.seconds, 0);
        const left = dayIndex.get(date) * ppd + COLUMN_PAD;
        let top = 0;
        for (const entry of dayEntries) {
          const height = Math.max(
            MIN_ENTRY_HEIGHT,
            (entry.seconds / fullDaySeconds) * (unit + STACK_GAP) - STACK_GAP
          );
          const issue = issueInfo(entry.key);
          const el = document.createElement("div");
          el.className = "wl-entry";
          el.dataset.issueKey = entry.key;
          el.style.background = issue.color || "#78909c";
          el.style.left = `${left}px`;
          el.style.width = `${ppd - 2 * COLUMN_PAD - 1}px`;
          el.style.top = `${ENTRY_INSET + top}px`;
          el.style.height = `${height}px`;
          el.innerHTML =
            `<div class="wl-line wl-line-head"><span class="wl-key">${escapeHtml(entry.key)}</span>` +
            `<span class="wl-hours">${escapeHtml(formatHours(entry.seconds))}</span></div>` +
            (height >= TWO_LINE_HEIGHT
              ? `<div class="wl-line wl-title">${escapeHtml(issue.summary || "")}</div>`
              : "");
          wireEntry(el, entry, row, dayTotal);
          track.appendChild(el);
          top += height + STACK_GAP;
        }
        stackBottom = Math.max(stackBottom, top - STACK_GAP);

        // Больше рабочего дня за день — красная черта по нижнему краю клетки.
        if (dayTotal > fullDaySeconds + 1) {
          const mark = document.createElement("div");
          mark.className = "overload-day";
          mark.style.left = `${dayIndex.get(date) * ppd}px`;
          mark.style.width = `${ppd}px`;
          mark.title = `Списано ${formatHours(dayTotal)} — больше рабочего дня (${HOURS_FORMAT.format(hoursPerDay)} ч)`;
          track.appendChild(mark);
        }
      }

      // Не поместившиеся по высоте блоки раздвигают строку вниз.
      const rowHeight = Math.max(baseRowHeight, stackBottom + 2 * ENTRY_INSET);
      rowEl.style.minHeight = `${rowHeight}px`;
      track.style.minHeight = `${rowHeight}px`;

      rowEl.appendChild(track);
      inner.appendChild(rowEl);
    });

    const todayIndex = dayIndex.has(meta.today) ? dayIndex.get(meta.today) : -1;
    if (todayIndex >= 0) {
      const todayEl = document.createElement("div");
      todayEl.className = "today-line";
      // Линия — по левому краю столбца, а не посередине: середину занимают блоки.
      todayEl.style.left = `calc(var(--label-width) + ${todayIndex * ppd}px)`;
      todayEl.style.width = "2px";
      todayEl.title = `Сегодня, ${formatDateRu(meta.today)}`;
      inner.appendChild(todayEl);
    }

    boardEl.appendChild(inner);
    boardEl.setAttribute("aria-busy", "false");
    summaryEl.textContent =
      `Списано: ${formatHours(shownSeconds)} · сотрудников: ${shownPeople} · задач: ${shownIssues.size}`;
  }

  function syncRangeInputs() {
    rangeFromEl.value = rangeFrom;
    rangeToEl.value = rangeTo;
  }

  /** Окно дат — в адресе страницы, чтобы ссылкой можно было поделиться. */
  function syncUrl() {
    const params = new URLSearchParams(window.location.search);
    if (rangeFrom) params.set("from", rangeFrom);
    else params.delete("from");
    if (rangeTo) params.set("to", rangeTo);
    else params.delete("to");
    const q = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${q ? `?${q}` : ""}`);
  }

  async function loadWorklog() {
    setStatus("Загрузка…");
    boardEl.setAttribute("aria-busy", "true");
    const params = new URLSearchParams();
    if (rangeFrom) params.set("from", rangeFrom);
    if (rangeTo) params.set("to", rangeTo);
    const q = params.toString();
    const r = await fetch(`/api/worklog${q ? `?${q}` : ""}`);
    const data = await r.json();
    if (!r.ok) {
      model = null;
      boardEl.innerHTML = `<div class="error-banner">${escapeHtml(data.error || "Ошибка")}</div>`;
      boardEl.setAttribute("aria-busy", "false");
      setStatus("", true);
      return;
    }
    model = data;
    // Окно строит сервер: он достраивает пустую границу и подрезает слишком широкое.
    rangeFrom = data.meta.from;
    rangeTo = data.meta.to;
    syncRangeInputs();
    render();
    setStatus("");
  }

  function reload() {
    syncUrl();
    loadWorklog().catch((e) => setStatus(String(e.message || e), true));
  }

  /** Сдвиг окна на его собственную длину: две недели — на две недели. */
  function shiftRange(direction) {
    if (!rangeFrom || !rangeTo) return;
    const length = daysBetween(rangeFrom, rangeTo) + 1;
    rangeFrom = isoAddDays(rangeFrom, direction * length);
    rangeTo = isoAddDays(rangeTo, direction * length);
    reload();
  }

  btnRefresh.addEventListener("click", reload);

  searchInputEl.addEventListener("input", () => {
    searchQuery = searchInputEl.value.trim().toLowerCase();
    render();
  });

  rangeFromEl.addEventListener("change", () => {
    rangeFrom = rangeFromEl.value || "";
    if (rangeFrom && rangeTo && rangeFrom > rangeTo) rangeTo = rangeFrom;
    reload();
  });

  rangeToEl.addEventListener("change", () => {
    rangeTo = rangeToEl.value || "";
    if (rangeFrom && rangeTo && rangeTo < rangeFrom) rangeFrom = rangeTo;
    reload();
  });

  btnRangePrev.addEventListener("click", () => shiftRange(-1));
  btnRangeNext.addEventListener("click", () => shiftRange(1));
  btnRangeReset.addEventListener("click", () => {
    rangeFrom = "";
    rangeTo = "";
    reload();
  });

  const initial = new URLSearchParams(window.location.search);
  rangeFrom = initial.get("from") || "";
  rangeTo = initial.get("to") || "";
  syncRangeInputs();
  loadWorklog().catch((e) => {
    boardEl.innerHTML = `<div class="error-banner">${escapeHtml(String(e.message || e))}</div>`;
    setStatus("", true);
  });
})();
