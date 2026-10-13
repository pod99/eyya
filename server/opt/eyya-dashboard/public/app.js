let mode = "client";
let dashboardTab = "bitrix";
let charts = {};
const form = document.querySelector("#filters");
const loading = document.querySelector("#loading");
const applyButton = form.querySelector(".apply");
const trackToolStatus = document.querySelector("#trackToolStatus");
const apiPath = (path) => new URL(path.replace(/^\//, ""), window.location.href).toString();

const money = (value) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value || 0)) + " ₽";
const count = (value) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value || 0));
const percent = (value) => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
const dateInput = (value) => String(value || "").slice(0, 10);
const dateLabel = (value) => new Date(value).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "2-digit" });
const sum = (rows, key) => rows.reduce((total, row) => total + Number(row[key] || 0), 0);
const html = (value) => String(value ?? "—").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const multiline = (value) => html(value).replace(/\n/g, "<br>");
const colors = {
  client: {
    revenue: "#8b7a81",
    orders: "rgba(178, 203, 184, 0.82)",
    repeat: "rgba(174, 196, 218, 0.82)",
    newClients: "rgba(207, 190, 203, 0.82)",
    repeat30: "rgba(178, 203, 184, 0.82)",
    repeat60: "rgba(224, 209, 172, 0.84)",
  },
  marketing: {
    revenue: "#a97986",
    orders: "rgba(226, 195, 204, 0.84)",
    repeat: "rgba(207, 179, 190, 0.84)",
    newClients: "rgba(190, 207, 218, 0.82)",
    repeat30: "rgba(226, 195, 204, 0.84)",
    repeat60: "rgba(224, 209, 172, 0.84)",
  },
  sources: ["#b2cbb8", "#e2c3cc", "#e0d1ac", "#aec4da", "#cfbecb", "#c7d1bf", "#dbc1b5", "#c8c4d2"],
};
const chartStyle = {
  grid: { color: "rgba(116, 97, 105, 0.14)", borderColor: "rgba(116, 97, 105, 0.2)" },
  ticks: { color: "#746169" },
  legend: { labels: { color: "#746169", boxWidth: 18, boxHeight: 10, usePointStyle: true } },
};

function option(select, value, label) {
  const el = document.createElement("option");
  el.value = value ?? "";
  el.textContent = label;
  select.appendChild(el);
}

function fillSelect(name, rows, mapper) {
  const select = form.elements[name];
  select.innerHTML = "";
  option(select, "all", "Все");
  rows.forEach((row) => {
    const mapped = mapper(row);
    option(select, mapped.value, mapped.label);
  });
}

async function init() {
  initDashboardTabs();
  initTrackTools();
  initCollapsibleTables();
  const filters = await fetch(apiPath("api/filters")).then((r) => r.json());
  const minDate = dateInput(filters.range.min_date);
  const maxDate = dateInput(filters.range.max_date);
  const recentStart = maxDate ? new Date(`${maxDate}T00:00:00Z`) : null;
  if (recentStart) recentStart.setUTCDate(recentStart.getUTCDate() - 2);
  const defaultStart = recentStart ? recentStart.toISOString().slice(0, 10) : minDate;
  form.elements.start.value = minDate && defaultStart < minDate ? minDate : defaultStart;
  form.elements.end.value = maxDate;
  fillSelect("source", filters.sources, (r) => ({ value: r.source_id, label: r.source_id || "Не указан" }));
  fillSelect("originator", filters.sources.filter((r) => r.originator_id), (r) => ({ value: r.originator_id, label: r.originator_id }));
  fillSelect("utmSource", filters.utmSources, (r) => ({ value: r.value, label: r.value || "Не указан" }));
  fillSelect("utmCampaign", filters.campaigns, (r) => ({ value: r.value, label: r.value || "Не указан" }));
  fillSelect("categoryId", filters.categories, (r) => ({ value: r.category_id, label: r.name }));
  fillSelect("assignedBy", filters.users.filter((r) => r.full_name), (r) => ({ value: r.id, label: r.full_name }));
  await loadDashboard();
}

function updateHeader() {
  const kicker = document.querySelector("#kicker");
  const title = document.querySelector("#title");
  document.body.classList.toggle("marketing", dashboardTab === "bitrix" && mode === "marketing");
  if (dashboardTab === "cdek") {
    if (kicker) kicker.textContent = "Логистика и отправления";
    title.textContent = "Доставка СДЭК";
    return;
  }
  if (kicker) kicker.textContent = mode === "marketing" ? "Метрики по маркетингу" : "Метрики клиентского отдела";
  title.textContent = mode === "marketing" ? "Дашборд по маркетингу" : "Дашборд клиентского отдела";
}

function initDashboardTabs() {
  const buttons = document.querySelectorAll(".tab[data-dashboard-tab]");
  const panels = document.querySelectorAll("[data-dashboard-panel]");
  buttons.forEach((button) => {
    button.addEventListener("click", async () => {
      const nextMode = button.dataset.mode || mode;
      const nextDashboardTab = button.dataset.dashboardTab || "bitrix";
      const shouldReload = nextDashboardTab !== dashboardTab || (nextDashboardTab === "bitrix" && nextMode !== mode);
      dashboardTab = button.dataset.dashboardTab || "bitrix";
      mode = nextMode;
      buttons.forEach((tab) => tab.classList.toggle("active", tab === button));
      panels.forEach((panel) => panel.classList.toggle("active", panel.dataset.dashboardPanel === dashboardTab));
      updateHeader();
      requestAnimationFrame(() => {
        Object.values(charts).forEach((chart) => chart?.resize?.());
        fitKpiValues();
      });
      if (shouldReload) await loadDashboard();
    });
  });
}

function initCollapsibleTables() {
  const titles = {
    periodTable: "Таблица по периодам",
    sourceTable: "Источники",
    funnelTable: "Воронка",
    linkedDealsTable: "Сопровождение",
    retentionTable: "Retention",
    cdekFinanceTable: "Показатели",
    cdekStatusTable: "Статусы",
    cdekItemTable: "SKU",
    cdekTable: "ДАТА CDEK",
    cdekRepeatTable: "Повторные заказы",
    cdekRepeatReturnedTable: "Повторные возвраты / отмены",
    cdekFreeFinanceTable: "Итоги бесплатных",
    cdekFreeStatusTable: "Категории и статусы",
    cdekFreeTable: "Бесплатные / неклиентские отправления",
    managerTable: "Менеджеры",
  };
  document.querySelectorAll(".table-wrap").forEach((wrap) => {
    if (wrap.closest(".collapsible-table")) return;
    const table = wrap.querySelector("table");
    const title = titles[table?.id] || "Таблица";
    const shell = document.createElement("section");
    shell.className = "collapsible-table";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "collapse-toggle";
    button.setAttribute("aria-expanded", "true");
    button.innerHTML = `<span>${html(title)}</span><i aria-hidden="true"></i>`;
    wrap.parentNode.insertBefore(shell, wrap);
    shell.appendChild(button);
    shell.appendChild(wrap);
    button.addEventListener("click", () => {
      const isCollapsed = shell.classList.toggle("is-collapsed");
      button.setAttribute("aria-expanded", String(!isCollapsed));
    });
  });
}

function setTrackToolStatus(message, state = "") {
  if (!trackToolStatus) return;
  trackToolStatus.textContent = message || "";
  trackToolStatus.dataset.state = state;
}

function trackActionSummary(action, data) {
  if (action === "add") {
    return (data.results || []).map((row) => {
      const table = row.table === "free" ? `бесплатные: ${row.category}` : "основная";
      return `${row.trackingNumber} -> ${table}`;
    }).join("; ");
  }
  return (data.results || []).map((row) => {
    const deleted = Object.values(row.deleted || {}).reduce((total, value) => total + Number(value || 0), 0);
    return `${row.trackingNumber} удален (${count(deleted)} записей)`;
  }).join("; ");
}

function initTrackTools() {
  const deleteInput = document.querySelector("#deleteTrackInput");
  const deleteButton = document.querySelector("#deleteTrackButton");
  const addInput = document.querySelector("#addTrackInput");
  const addButton = document.querySelector("#addTrackButton");
  const exportButton = document.querySelector("#exportCdekButton");
  const run = async (action, input, button) => {
    const trackingNumber = input.value.trim();
    if (!trackingNumber) {
      setTrackToolStatus("Введите трек-номер.", "error");
      return;
    }
    button.disabled = true;
    setTrackToolStatus(action === "add" ? "Загружаем данные из CDEK..." : "Удаляем трек-номер...", "loading");
    try {
      const response = await fetch(apiPath(`api/cdek/${action === "add" ? "add-track" : "delete-track"}`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trackingNumber }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.message || data.error || "Ошибка операции");
      }
      input.value = "";
      setTrackToolStatus(trackActionSummary(action, data), "ok");
      await loadDashboard();
    } catch (error) {
      setTrackToolStatus(String(error.message || error), "error");
    } finally {
      button.disabled = false;
    }
  };
  deleteButton?.addEventListener("click", () => run("delete", deleteInput, deleteButton));
  addButton?.addEventListener("click", () => run("add", addInput, addButton));
  exportButton?.addEventListener("click", exportCdekTables);
  deleteInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") run("delete", deleteInput, deleteButton);
  });
  addInput?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") run("add", addInput, addButton);
  });
}

function xml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;")
    .replace(/\r?\n/g, "&#10;");
}

function tableRows(tableId) {
  const table = document.querySelector(`#${tableId}`);
  if (!table) return [];
  return [...table.querySelectorAll("tr")].map((tr) => (
    [...tr.children].map((cell) => cell.innerText.trim())
  )).filter((row) => row.some(Boolean));
}

function worksheetXml(name, rows) {
  const safeName = name.replace(/[\\/?*[\]:]/g, " ").slice(0, 31);
  const body = (rows.length ? rows : [["Нет данных"]]).map((row, rowIndex) => `
    <Row>${row.map((cell) => `
      <Cell${rowIndex === 0 ? ' ss:StyleID="Header"' : ""}><Data ss:Type="String">${xml(cell)}</Data></Cell>
    `).join("")}</Row>
  `).join("");
  return `
    <Worksheet ss:Name="${xml(safeName)}">
      <Table>${body}</Table>
      <WorksheetOptions xmlns="urn:schemas-microsoft-com:office:excel">
        <FreezePanes/>
        <FrozenNoSplit/>
        <SplitHorizontal>1</SplitHorizontal>
        <TopRowBottomPane>1</TopRowBottomPane>
        <ActivePane>2</ActivePane>
      </WorksheetOptions>
    </Worksheet>
  `;
}

function exportCdekTables() {
  const sheets = [
    ["Показатель", tableRows("cdekFinanceTable")],
    ["Статус", tableRows("cdekStatusTable")],
    ["SKU", tableRows("cdekItemTable")],
    ["ДАТА CDEK", tableRows("cdekTable")],
    ["Повторные", tableRows("cdekRepeatTable")],
    ["Повторные возвраты", tableRows("cdekRepeatReturnedTable")],
    ["Бесплатные неклиентские", tableRows("cdekFreeTable")],
  ];
  const workbook = `<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
  xmlns:o="urn:schemas-microsoft-com:office:office"
  xmlns:x="urn:schemas-microsoft-com:office:excel"
  xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
  <Styles>
    <Style ss:ID="Default" ss:Name="Normal"><Alignment ss:Vertical="Top" ss:WrapText="1"/></Style>
    <Style ss:ID="Header"><Font ss:Bold="1"/><Interior ss:Color="#EDE7EC" ss:Pattern="Solid"/></Style>
  </Styles>
  ${sheets.map(([name, rows]) => worksheetXml(name, rows)).join("")}
</Workbook>`;
  const blob = new Blob([workbook], { type: "application/vnd.ms-excel;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10);
  link.href = url;
  link.download = `eyya-cdek-${stamp}.xls`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setTrackToolStatus("Excel-файл сформирован: 5 листов.", "ok");
}

function params() {
  const data = new FormData(form);
  const params = new URLSearchParams({ mode });
  for (const [key, value] of data.entries()) params.set(key, value);
  if (dashboardTab === "cdek") params.set("includeCdek", "1");
  return params;
}

async function loadDashboard() {
  setLoading(true);
  updateHeader();
  try {
    await loadOpsHealth();
    const response = await fetch(apiPath(`api/dashboard?${params().toString()}`));
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.message || data.error || "Ошибка загрузки");
    renderKpis(data.overview);
    renderTrend(data.series);
    renderSources(data.sources);
    renderFunnel(data.funnel, data.support);
    renderRetention(data.retention);
    renderManagers(data.managers);
    renderCdek(data.cdek || {});
    renderTables(data);
    document.querySelector("#generatedAt").textContent = new Date(data.generatedAt).toLocaleString("ru-RU");
  } finally {
    setLoading(false);
  }
}

async function loadOpsHealth() {
  const container = document.querySelector("#opsHealth");
  if (!container) return;
  try {
    const response = await fetch(apiPath("api/ops-health"));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.status === "error") throw new Error(data.message || "Диагностика недоступна");
    renderOpsHealth(data);
  } catch (error) {
    container.innerHTML = `
      <div class="ops-health-head">
        <div>
          <h2>Операционный контроль</h2>
          <p>Не удалось прочитать последнюю проверку процессов.</p>
        </div>
        <span class="ops-badge is-error">ошибка</span>
      </div>
    `;
  }
}

function healthStatusLabel(status) {
  return status === "ok" ? "норма" : status === "warn" ? "внимание" : "ошибка";
}

function formatHealthDate(value) {
  if (!value || value === "—") return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? html(value) : date.toLocaleString("ru-RU");
}

function renderOpsHealth(data) {
  const container = document.querySelector("#opsHealth");
  if (!container) return;
  const cards = data.cards || [];
  const updated = data.updatedAt ? new Date(data.updatedAt).toLocaleString("ru-RU") : "—";
  container.innerHTML = `
    <div class="ops-health-head">
      <div>
        <h2>Операционный контроль</h2>
        <p>Последняя проверка: ${html(updated)}</p>
      </div>
      <span class="ops-badge is-${html(data.status || "warn")}">${healthStatusLabel(data.status)}</span>
    </div>
    <div class="ops-health-grid">
      ${cards.map((card) => `
        <article class="ops-card is-${html(card.status || "ok")}">
          <header>
            <span>${html(card.title)}</span>
            <b>${healthStatusLabel(card.status)}</b>
          </header>
          <strong>${html(card.primary)}</strong>
          <p>${html(card.secondary)}</p>
          <dl>
            ${(card.details || []).map((item) => `
              <div class="is-${html(item.status || "ok")}">
                <dt>${html(item.label)}</dt>
                <dd>${formatHealthDate(item.value)}</dd>
              </div>
            `).join("")}
          </dl>
        </article>
      `).join("")}
    </div>
    ${data.notes ? `<p class="ops-notes">${html(data.notes)}</p>` : ""}
  `;
}

function setLoading(state) {
  document.body.classList.toggle("is-loading", state);
  loading.classList.toggle("hidden", !state);
  applyButton.disabled = state;
  applyButton.textContent = state ? "Загружаем..." : "Обновить";
}

function renderKpis(k) {
  const items = mode === "marketing"
    ? [
        ["Выручка", money(k.revenue), `${count(k.won_orders)} оплат`],
        ["Заказы сайта", count(k.won_orders), `CV лид → оплата ${percent(k.lead_to_order_conversion)}`],
        ["Средний чек", money(k.avg_check), "по выигранным сделкам"],
        ["Повторные клиенты", money(k.repeat_revenue), `${count(k.repeat_orders)} заказов`],
        ["Новые клиенты", money(k.new_revenue), `${count(k.new_orders)} заказов`],
        ["Лиды", count(k.leads_count), "по дате начала"],
      ]
    : [
        ["Доход с повторных продаж", money(k.repeat_revenue), `${count(k.repeat_orders)} заказов`],
        ["Повторные заказы", count(k.repeat_orders), `средний чек ${money(k.repeat_avg_check)}`],
        ["Конверсия лид → заказ", percent(k.lead_to_order_conversion), `${count(k.leads_count)} лидов`],
        ["Время первого ответа", k.first_response_minutes == null ? "—" : `${count(k.first_response_minutes)} мин`, "по активностям Bitrix"],
        ["Новые клиенты", count(k.new_orders), money(k.new_revenue)],
        ["Общая выручка", money(k.revenue), `${count(k.won_orders)} оплат`],
      ];
  document.querySelector("#kpis").innerHTML = items.map(([label, value, hint]) => `<article class="kpi"><span>${label}</span><strong>${value}</strong><em>${hint}</em></article>`).join("");
  fitKpiValues();
  requestAnimationFrame(fitKpiValues);
}

function fitKpiValues() {
  document.querySelectorAll(".kpi").forEach((card) => {
    const value = card.querySelector("strong");
    if (!value) return;

    value.style.setProperty("--kpi-value-size", "");
    let size = Math.min(36, Math.max(23, Number.parseFloat(getComputedStyle(value).fontSize) || 36));
    value.style.setProperty("--kpi-value-size", `${size}px`);

    const fits = () => value.scrollWidth <= value.clientWidth + 1 && card.scrollHeight <= card.clientHeight + 1;
    while (!fits() && size > 12) {
      size -= 1;
      value.style.setProperty("--kpi-value-size", `${size}px`);
    }
  });
}

function chart(id, config) {
  charts[id]?.destroy();
  charts[id] = new Chart(document.getElementById(id), config);
}

function renderTrend(rows) {
  const labels = rows.map((r) => dateLabel(r.period));
  const palette = colors[mode];
  chart("trendChart", {
    type: "bar",
    data: {
      labels,
      datasets: [
        { type: "line", label: "Выручка", data: rows.map((r) => r.revenue), borderColor: palette.revenue, backgroundColor: "transparent", pointBackgroundColor: "#fbfafb", pointBorderColor: palette.revenue, borderWidth: 2, yAxisID: "y1", tension: 0.3 },
        { label: "Оплаты", data: rows.map((r) => r.won_orders), backgroundColor: palette.orders, borderRadius: 2, yAxisID: "y" },
        { label: "Повторные", data: rows.map((r) => r.repeat_orders), backgroundColor: palette.repeat, borderRadius: 2, yAxisID: "y" },
      ],
    },
    options: { responsive: true, interaction: { mode: "index" }, plugins: { legend: chartStyle.legend }, scales: { x: { grid: chartStyle.grid, ticks: chartStyle.ticks }, y: { beginAtZero: true, grid: chartStyle.grid, ticks: chartStyle.ticks }, y1: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false }, ticks: chartStyle.ticks } } },
  });
}

function renderSources(rows) {
  chart("sourceChart", {
    type: "doughnut",
    data: {
      labels: rows.map((r) => [r.source, r.originator].filter(Boolean).join(" / ")),
      datasets: [{ data: rows.map((r) => r.revenue), backgroundColor: colors.sources, borderColor: "#fbfafb", borderWidth: 2 }],
    },
    options: { plugins: { legend: { position: "bottom", ...chartStyle.legend } } },
  });
}

function renderFunnel(funnel, support) {
  const rows = mode === "client" ? support : funnel.slice(0, 8);
  const max = Math.max(...rows.map((r) => r.cnt || r.deals || 0), 1);
  document.querySelector("#funnel").innerHTML = rows.map((r) => {
    const value = r.cnt || r.deals || 0;
    const name = r.stage_name || r.stage_id;
    return `<div class="barrow"><header><strong>${name}</strong><span>${count(value)}</span></header><div class="track"><div class="fill" style="width:${Math.max(4, value / max * 100)}%"></div></div></div>`;
  }).join("");
}

function renderRetention(rows) {
  const palette = colors[mode];
  chart("retentionChart", {
    type: "bar",
    data: {
      labels: rows.map((r) => dateLabel(r.period)),
      datasets: [
        { label: "Новые", data: rows.map((r) => r.new_clients), backgroundColor: palette.newClients, borderRadius: 2 },
        { label: "Повтор 30 дней", data: rows.map((r) => r.repeat_30), backgroundColor: palette.repeat30, borderRadius: 2 },
        { label: "Повтор 60 дней", data: rows.map((r) => r.repeat_60), backgroundColor: palette.repeat60, borderRadius: 2 },
      ],
    },
    options: { responsive: true, scales: { x: { grid: chartStyle.grid, ticks: chartStyle.ticks }, y: { beginAtZero: true, grid: chartStyle.grid, ticks: chartStyle.ticks } }, plugins: { legend: { position: "bottom", ...chartStyle.legend } } },
  });
}

function renderManagers(rows) {
  document.querySelector("#managers").innerHTML = rows.map((r) => `
    <div class="row">
      <div><strong>${r.manager}</strong><br><span>${count(r.won)} оплат из ${count(r.deals)} сделок · ${percent(r.win_rate)}</span></div>
      <strong>${money(r.revenue)}</strong>
    </div>`).join("");
}

function setTable(id, headers, rows) {
  const table = document.querySelector(`#${id}`);
  const bodyRows = rows.filter((row) => !row?.isTotal);
  const totalRows = rows.filter((row) => row?.isTotal);
  const cells = (row, tag) => row.map((cell) => `<${tag}>${multiline(cell)}</${tag}>`).join("");
  const rowClass = (row) => row?.rowClass ? ` class="${html(row.rowClass)}"` : "";
  table.innerHTML = `
    <thead><tr>${headers.map((h) => `<th>${html(h)}</th>`).join("")}</tr></thead>
    <tbody>${bodyRows.map((row) => `<tr${rowClass(row)}>${cells(row, "td")}</tr>`).join("")}</tbody>
    ${totalRows.length ? `<tfoot>${totalRows.map((row) => `<tr${rowClass(row)}>${cells(row, "td")}</tr>`).join("")}</tfoot>` : ""}
  `;
}

function summarizeDeliveryRows(rows) {
  const deliveredRows = rows.filter((row) => row.is_delivered);
  const returnedRows = rows.filter((row) => row.is_return_or_cancel);
  const inProgressRows = rows.filter((row) => !row.is_delivered && !row.is_return_or_cancel);
  const total = rows.length;
  const delivered = deliveredRows.length;
  const returned = returnedRows.length;
  const repeat = rows.filter((row) => row.is_repeat_order || row.order_type === "Повторный").length;
  const inProgress = inProgressRows.length;
  const cdekCost = sum(rows, "total_sum");
  return {
    total,
    delivered,
    returned,
    repeat,
    inProgress,
    productSum: sum(rows, "product_sum"),
    clientDelivery: sum(rows, "charged_delivery_sum"),
    amount: sum(rows, "amount"),
    cdekCost,
    deliveredAmount: sum(deliveredRows, "amount"),
    returnedAmount: sum(returnedRows, "amount"),
    inProgressAmount: sum(inProgressRows, "amount"),
    deliveredCdekCost: sum(deliveredRows, "total_sum"),
    returnedCdekCost: sum(returnedRows, "total_sum"),
    inProgressCdekCost: sum(inProgressRows, "total_sum"),
    avgCdekCost: total ? cdekCost / total : 0,
    deliveredRate: total ? delivered / total * 100 : 0,
    returnedRate: total ? returned / total * 100 : 0,
  };
}

function groupRows(rows, keyFn) {
  const map = new Map();
  rows.forEach((row) => {
    const key = keyFn(row) || "Не указано";
    const item = map.get(key) || { label: key, orders: 0, productSum: 0, amount: 0, cdekCost: 0 };
    item.orders += 1;
    item.productSum += Number(row.product_sum || 0);
    item.amount += Number(row.amount || 0);
    item.cdekCost += Number(row.total_sum || 0);
    map.set(key, item);
  });
  return [...map.values()].sort((a, b) => b.orders - a.orders || b.cdekCost - a.cdekCost);
}

function renderDeliveryCards(id, cards) {
  const target = document.querySelector(`#${id}`);
  if (!target) return;
  target.innerHTML = cards.map(([label, value, hint]) => `
    <div class="delivery-card"><span>${html(label)}</span><strong>${html(value)}</strong><em>${html(hint)}</em></div>
  `).join("");
}

function renderBars(rows, max, mode = "status") {
  const capped = rows.slice(0, 8);
  return capped.map((row, index) => {
    const width = max ? Math.max(4, row.orders / max * 100) : 0;
    return `
      <div class="mini-bar ${mode}-${index % 4}">
        <header><strong>${html(row.label)}</strong><span>${count(row.orders)} · ${money(row.cdekCost)}</span></header>
        <div class="mini-track"><div class="mini-fill" style="width:${width}%"></div></div>
      </div>
    `;
  }).join("");
}

function renderDeliveryInsights(id, sections) {
  const target = document.querySelector(`#${id}`);
  if (!target) return;
  target.innerHTML = sections.map((section) => {
    const max = Math.max(...section.rows.map((row) => row.orders), 1);
    return `
      <div class="insight-panel">
        <header><h4>${html(section.title)}</h4><span>${html(section.hint || "")}</span></header>
        <div class="mini-bars">${renderBars(section.rows, max, section.mode)}</div>
      </div>
    `;
  }).join("");
}

function renderCdek(cdek) {
  const summary = cdek.summary || {};
  const analytics = cdek.analytics || {};
  const retention = cdek.retention || {};
  const mainRows = cdek.rows || [];
  const repeatRows = mainRows.filter((row) => row.is_repeat_order || row.order_type === "Повторный");
  const repeatReturnedRows = repeatRows.filter((row) => row.is_return_or_cancel);
  const updated = summary.last_synced_at ? new Date(summary.last_synced_at).toLocaleString("ru-RU") : "—";
  const cards = [
    ["Отгрузки", count(analytics.orders_total ?? summary.orders_total), `${count(summary.with_tracking)} треков`],
    ["Получено", money(analytics.delivered_revenue), `${count(analytics.delivered_orders)} доставлено`],
    ["Возвраты/отмены", money(analytics.returned_revenue), `${count(analytics.returned_orders)} · ${percent(analytics.return_rate)}`],
    ["Повторные", money(analytics.repeat_revenue), `${count(analytics.repeat_orders)} · ${percent(analytics.repeat_rate)}`],
    ["Повторные возвраты/отмены", money(analytics.repeat_returned_revenue), `${count(analytics.repeat_returned_orders)} · ${percent(analytics.repeat_return_rate)}`],
    ["Доставка CDEK", money(analytics.cdek_carrier_cost), `клиентская ${money(analytics.charged_delivery_sum)}`],
    ["Повтор через 30 дней", percent(retention.repeat_30_rate), `${count(retention.repeat_30_clients)} из ${count(retention.cohort_clients)}`],
    ["Повтор через 60 дней", percent(retention.repeat_60_rate), `${count(retention.repeat_60_clients)} из ${count(retention.cohort_clients)}`],
    ["Повтор через 90 дней", percent(retention.repeat_90_rate), `${count(retention.repeat_90_clients)} из ${count(retention.cohort_clients)}`],
    ["Синхронизировано", count(summary.synced), `обновлено ${updated}`],
    ["В пути", money(analytics.in_progress_revenue), `${count(analytics.in_progress_orders)} заказов`],
  ];
  renderDeliveryCards("cdekSummary", cards);

  renderDeliveryInsights("cdekMainInsights", [
    {
      title: "Основная доставка по статусам",
      hint: "заказы / стоимость CDEK",
      mode: "status",
      rows: (cdek.statuses || []).map((row) => ({
        label: row.status,
        orders: Number(row.orders || 0),
        cdekCost: Number(row.cdek_carrier_cost || 0),
      })),
    },
    {
      title: "Итоги основной доставки",
      hint: `${count(analytics.orders_total)} отправлений`,
      mode: "metric",
      rows: [
        { label: "Получено", orders: Number(analytics.delivered_orders || 0), cdekCost: Number(analytics.delivered_revenue || 0) },
        { label: "В пути / в работе", orders: Number(analytics.in_progress_orders || 0), cdekCost: Number(analytics.in_progress_revenue || 0) },
        { label: "Возвраты / не вручено", orders: Number(analytics.returned_orders || 0), cdekCost: Number(analytics.returned_revenue || 0) },
        { label: "Повторные", orders: Number(analytics.repeat_orders || 0), cdekCost: Number(analytics.repeat_revenue || 0) },
        { label: "Повторные возвраты", orders: Number(analytics.repeat_returned_orders || 0), cdekCost: Number(analytics.repeat_returned_revenue || 0) },
      ],
    },
  ]);

  setTable("cdekFinanceTable", ["Показатель", "Кол-во", "Сумма", "Доля"], [
    ["Все отгрузки", count(analytics.orders_total), money(analytics.calculated_total), "100%"],
    ["Получено", count(analytics.delivered_orders), money(analytics.delivered_revenue), percent(analytics.orders_total ? (analytics.delivered_orders / analytics.orders_total) * 100 : 0)],
    ["В пути / в работе", count(analytics.in_progress_orders), money(analytics.in_progress_revenue), percent(analytics.orders_total ? (analytics.in_progress_orders / analytics.orders_total) * 100 : 0)],
    ["Возвраты / отмены / не вручено", count(analytics.returned_orders), money(analytics.returned_revenue), percent(analytics.return_rate)],
    ["Повторные заказы", count(analytics.repeat_orders), money(analytics.repeat_revenue), percent(analytics.repeat_rate)],
    ["Повторные возвраты / отмены", count(analytics.repeat_returned_orders), money(analytics.repeat_returned_revenue), percent(analytics.repeat_return_rate)],
    ["Клиентская доставка из CDEK", "", money(analytics.charged_delivery_sum), ""],
    ["Стоимость услуг CDEK", "", money(analytics.cdek_carrier_cost), ""],
    ["Retention 30 дней", count(retention.repeat_30_clients), "", percent(retention.repeat_30_rate)],
    ["Retention 60 дней", count(retention.repeat_60_clients), "", percent(retention.repeat_60_rate)],
    ["Retention 90 дней", count(retention.repeat_90_clients), "", percent(retention.repeat_90_rate)],
  ]);

  setTable("cdekStatusTable", ["Статус", "Код", "Заказы", "Доставка клиента", "Стоимость CDEK"], [
    ...(cdek.statuses || []).map((row) => [row.status, row.status_code || "—", count(row.orders), money(row.delivery_sum), money(row.cdek_carrier_cost)]),
    Object.assign(["Итого", "", count(sum(cdek.statuses || [], "orders")), money(sum(cdek.statuses || [], "delivery_sum")), money(sum(cdek.statuses || [], "cdek_carrier_cost"))], { isTotal: true }),
  ]);

  setTable("cdekItemTable", ["SKU", "Товар", "Кол-во", "Мин. цена", "Макс. цена", "Сумма"], [
    ...(cdek.items || []).map((row) => [
      row.sku || "—",
      row.item_name,
      count(row.quantity),
      money(row.min_price),
      money(row.max_price),
      money(row.total_sum),
    ]),
    Object.assign(["Итого", "", count(sum(cdek.items || [], "quantity")), "", "", money(sum(cdek.items || [], "total_sum"))], { isTotal: true }),
  ]);

  const deliveryRow = (row, includeCategory = false) => Object.assign([
      dateLabel(row.date_create_date),
      row.order_number,
      row.order_type,
      row.client_name,
      row.tracking_number,
      row.sync_error ? `Ошибка: ${row.sync_error}` : row.status_name,
      row.to_city || "—",
      row.planned_delivery_date ? dateLabel(row.planned_delivery_date) : "—",
      row.delivery_date ? dateLabel(row.delivery_date) : "—",
      (row.items_summary || "—").replace(/\s+\|\s+/g, "\n"),
      money(row.product_sum),
      money(row.charged_delivery_sum),
      money(row.amount),
      money(row.delivery_sum),
      ...(includeCategory ? [row.free_order_type || "—"] : []),
    ], { rowClass: [row.is_return_or_cancel ? "is-return-order" : "", row.is_repeat_order || row.order_type === "Повторный" ? "is-repeat-order" : ""].filter(Boolean).join(" ") });

  setTable("cdekTable", ["Дата CDEK", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "Товары", "Доставка", "Сумма", "Стоимость CDEK"], [
    ...mainRows.map((row) => deliveryRow(row)),
  ]);

  setTable("cdekRepeatTable", ["Дата CDEK", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "Товары", "Доставка", "Сумма", "Стоимость CDEK"], [
    ...repeatRows.map((row) => deliveryRow(row)),
  ]);

  setTable("cdekRepeatReturnedTable", ["Дата CDEK", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "Товары", "Доставка", "Сумма", "Стоимость CDEK"], [
    ...repeatReturnedRows.map((row) => deliveryRow(row)),
  ]);

  setTable("cdekFreeTable", ["Дата CDEK", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "Товары", "Доставка", "Сумма", "Стоимость CDEK", "Категория"], [
    ...(cdek.freeRows || []).map((row) => deliveryRow(row, true)),
  ]);

  const freeRows = cdek.freeRows || [];
  const free = summarizeDeliveryRows(freeRows);
  const freeByCategory = groupRows(freeRows, (row) => row.free_order_type);
  const freeByStatus = groupRows(freeRows, (row) => row.status_name || (row.sync_error ? "Ошибка CDEK" : "Не синхронизировано"));

  renderDeliveryCards("cdekFreeSummary", [
    ["Отправления", count(free.total), `${count(free.delivered)} доставлено`],
    ["Товары", money(free.productSum), `${money(free.amount)} сумма`],
    ["Стоимость CDEK", money(free.cdekCost), `средняя ${money(free.avgCdekCost)}`],
    ["Возвраты / не вручено", count(free.returned), percent(free.returnedRate)],
    ["В пути", count(free.inProgress), `${count(free.repeat)} повторных`],
  ]);

  renderDeliveryInsights("cdekFreeInsights", [
    { title: "Бесплатные по категориям", hint: "заказы / стоимость CDEK", mode: "category", rows: freeByCategory },
    { title: "Бесплатные по статусам", hint: "заказы / стоимость CDEK", mode: "status", rows: freeByStatus },
  ]);

  setTable("cdekFreeFinanceTable", ["Показатель", "Кол-во", "Сумма", "Доля"], [
    ["Все бесплатные / неклиентские", count(free.total), money(free.amount), "100%"],
    ["Доставлено", count(free.delivered), money(free.deliveredAmount), percent(free.deliveredRate)],
    ["В пути / в работе", count(free.inProgress), money(free.inProgressAmount), percent(free.total ? free.inProgress / free.total * 100 : 0)],
    ["Возвраты / не вручено", count(free.returned), money(free.returnedAmount), percent(free.returnedRate)],
    ["Стоимость услуг CDEK", "", money(free.cdekCost), ""],
    ["Средняя стоимость CDEK", "", money(free.avgCdekCost), ""],
  ]);

  setTable("cdekFreeStatusTable", ["Категория / статус", "Заказы", "Товары", "Стоимость CDEK"], [
    ...freeByCategory.map((row) => [row.label, count(row.orders), money(row.productSum), money(row.cdekCost)]),
    Object.assign(["Итого", count(free.total), money(free.productSum), money(free.cdekCost)], { isTotal: true }),
    ...freeByStatus.map((row) => [`Статус: ${row.label}`, count(row.orders), money(row.productSum), money(row.cdekCost)]),
  ]);
}

function renderTables(data) {
  const periods = data.series.map((r) => dateLabel(r.period));
  const total = data.overview || {};
  const totalLeads = sum(data.series, "leads_count");
  const totalWon = sum(data.series, "won_orders");
  const avgFirstResponse = total.first_response_minutes == null ? "—" : count(total.first_response_minutes);
  const periodRows = mode === "marketing" ? [
    ["ПРОДАЖИ", "", ...data.series.flatMap((r) => [count(r.won_orders), money(r.revenue)]), count(total.won_orders), money(total.revenue)],
    ["Выручка (сайт)", "зак. / руб.", ...data.series.flatMap((r) => [count(r.won_orders), money(r.revenue)]), count(total.won_orders), money(total.revenue)],
    ["Средний чек (сайт)", "руб.", ...data.series.flatMap((r) => ["", money(r.avg_check)]), "", money(total.avg_check)],
    ["новые клиенты", "зак. / руб.", ...data.series.flatMap((r) => [count(r.new_orders), money(r.new_revenue)]), count(total.new_orders), money(total.new_revenue)],
    ["повторные заказы", "зак. / руб.", ...data.series.flatMap((r) => [count(r.repeat_orders), money(r.repeat_revenue)]), count(total.repeat_orders), money(total.repeat_revenue)],
    ["ТРАФИК — ВОРОНКА", "", ...data.series.flatMap(() => ["", ""]), "", ""],
    ["Уровень 5 — ЛИДЫ", "чел.", ...data.series.flatMap((r) => [count(r.leads_count), ""]), count(totalLeads), ""],
    ["CV лид → оплата", "%", ...data.series.flatMap((r) => ["", percent(r.leads_count ? (r.won_orders / r.leads_count) * 100 : 0)]), "", percent(totalLeads ? (totalWon / totalLeads) * 100 : 0)],
    ["Уровень 6 — ОПЛАТЫ", "чел.", ...data.series.flatMap((r) => [count(r.won_orders), ""]), count(totalWon), ""],
  ] : [
    ["доход с повторных продаж", "руб.", ...data.series.map((r) => money(r.repeat_revenue)), money(total.repeat_revenue)],
    ["кол-во повторных заказов", "шт.", ...data.series.map((r) => count(r.repeat_orders)), count(total.repeat_orders)],
    ["средний чек повторных заказов", "руб.", ...data.series.map((r) => money(r.repeat_accounted_orders ? r.repeat_revenue / r.repeat_accounted_orders : 0)), money(total.repeat_avg_check)],
    ["конверсия из лидов в заказ ( на сайте )", "%", ...data.series.map((r) => percent(r.leads_count ? (r.won_orders / r.leads_count) * 100 : 0)), percent(total.lead_to_order_conversion)],
    ["время первого ответа, минута", "мин.", ...data.series.map((r) => r.first_response_minutes == null ? "—" : count(r.first_response_minutes)), avgFirstResponse],
  ];
  const periodHeaders = mode === "marketing"
    ? ["Показатель", "Ед.", ...periods.flatMap((p) => [`${p} кол-во`, `${p} значение`]), "Итого кол-во", "Итого значение"]
    : ["Показатель", "Ед.", ...periods, "Итого"];
  setTable("periodTable", periodHeaders, periodRows);

  setTable("sourceTable", ["Источник", "Сделки", "Оплаты", "Выручка"], [
    ...data.sources.map((r) => [
    [r.source, r.originator].filter(Boolean).join(" / "),
    count(r.deals),
    count(r.won),
    money(r.revenue),
    ]),
    Object.assign(["Итого", count(sum(data.sources, "deals")), count(sum(data.sources, "won")), money(sum(data.sources, "revenue"))], { isTotal: true }),
  ]);

  const funnelRows = mode === "client" ? data.support : data.funnel;
  setTable("funnelTable", ["Этап", "Воронка", "Кол-во", "Сумма"], [
    ...funnelRows.map((r) => [
    r.stage_name || r.stage_id,
    r.category_id ? `#${r.category_id}` : "Сопровождение",
    count(r.cnt || r.deals),
    r.amount === undefined ? "—" : money(r.amount),
    ]),
    Object.assign(["Итого", "", count(funnelRows.reduce((total, r) => total + Number(r.cnt || r.deals || 0), 0)), mode === "client" ? "—" : money(sum(funnelRows, "amount"))], { isTotal: true }),
  ]);

  const retentionRows = data.retention.map((r) => {
    const support = (data.supportSeries || []).find((s) => dateLabel(s.period) === dateLabel(r.period)) || {};
    return [
      dateLabel(r.period),
      count(r.new_clients),
      percent(r.new_clients ? (r.repeat_30 / r.new_clients) * 100 : 0),
      percent(r.new_clients ? (r.repeat_60 / r.new_clients) * 100 : 0),
      count(support.complaints),
      count(support.no_contact),
    ];
  });
  setTable("retentionTable", ["Период", "новые клиенты", "Повторная покупка 30 дней (%)", "Повторная покупка 60 дней (%)", "Жалобы (кол-во)", "Нет контакта (кол-во)"], [
    ...retentionRows,
    Object.assign([
      "Итого",
      count(sum(data.retention, "new_clients")),
      percent(sum(data.retention, "new_clients") ? (sum(data.retention, "repeat_30") / sum(data.retention, "new_clients")) * 100 : 0),
      percent(sum(data.retention, "new_clients") ? (sum(data.retention, "repeat_60") / sum(data.retention, "new_clients")) * 100 : 0),
      count(sum(data.supportSeries || [], "complaints")),
      count(sum(data.supportSeries || [], "no_contact")),
    ], { isTotal: true }),
  ]);

  const linked = data.linkedDeals || {};
  const overview = data.overview || {};
  setTable("linkedDealsTable", ["Связи сделок", "Значение"], [
    ["Сделки сопровождения", count(linked.support_deals)],
    ["Связано уверенно", count(linked.support_linked_to_order)],
    ["по номеру заказа", count(linked.support_linked_by_order_number)],
    ["по исходной сделке из названия", count(linked.support_linked_by_source_title)],
    ["вероятные совпадения", count(linked.support_probable_links)],
    ["сумма исключена из продаж", money(overview.excluded_based_on_deal_amount)],
  ]);

  setTable("managerTable", ["Менеджер", "Сделки", "Оплаты", "Конверсия", "Выручка"], [
    ...data.managers.map((r) => [
    r.manager,
    count(r.deals),
    count(r.won),
    percent(r.win_rate),
    money(r.revenue),
    ]),
    Object.assign(["Итого", count(sum(data.managers, "deals")), count(sum(data.managers, "won")), percent(sum(data.managers, "deals") ? (sum(data.managers, "won") / sum(data.managers, "deals")) * 100 : 0), money(sum(data.managers, "revenue"))], { isTotal: true }),
  ]);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  await loadDashboard();
});

window.addEventListener("resize", fitKpiValues);
document.fonts?.ready.then(fitKpiValues);

init().catch((err) => {
  console.error(err);
  document.querySelector("main").insertAdjacentHTML("afterbegin", `<div class="panel"><strong>Ошибка загрузки</strong><p>${err.message}</p></div>`);
});
