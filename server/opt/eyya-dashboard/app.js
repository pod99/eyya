let mode = "client";
let charts = {};
const form = document.querySelector("#filters");
const loading = document.querySelector("#loading");
const applyButton = form.querySelector(".apply");
const apiPath = (path) => new URL(path.replace(/^\//, ""), window.location.href).toString();

const money = (value) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value || 0)) + " ₽";
const count = (value) => new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(Number(value || 0));
const percent = (value) => `${Number(value || 0).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
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
  const filters = await fetch(apiPath("api/filters")).then((r) => r.json());
  form.elements.start.value = filters.range.min_date || "";
  form.elements.end.value = filters.range.max_date || "";
  fillSelect("source", filters.sources, (r) => ({ value: r.source_id, label: r.source_id || "Не указан" }));
  fillSelect("originator", filters.sources.filter((r) => r.originator_id), (r) => ({ value: r.originator_id, label: r.originator_id }));
  fillSelect("utmSource", filters.utmSources, (r) => ({ value: r.value, label: r.value || "Не указан" }));
  fillSelect("utmCampaign", filters.campaigns, (r) => ({ value: r.value, label: r.value || "Не указан" }));
  fillSelect("categoryId", filters.categories, (r) => ({ value: r.category_id, label: r.name }));
  fillSelect("assignedBy", filters.users.filter((r) => r.full_name), (r) => ({ value: r.id, label: r.full_name }));
  await loadDashboard();
}

function params() {
  const data = new FormData(form);
  const params = new URLSearchParams({ mode });
  for (const [key, value] of data.entries()) params.set(key, value);
  return params;
}

async function loadDashboard() {
  setLoading(true);
  document.body.classList.toggle("marketing", mode === "marketing");
  document.querySelector("#kicker").textContent = mode === "marketing" ? "Метрики по маркетингу" : "Метрики клиентского отдела";
  document.querySelector("#title").textContent = mode === "marketing" ? "Дашборд по маркетингу" : "Дашборд клиентского отдела";
  try {
    const data = await fetch(apiPath(`api/dashboard?${params().toString()}`)).then((r) => r.json());
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

function renderCdek(cdek) {
  const summary = cdek.summary || {};
  const updated = summary.last_synced_at ? new Date(summary.last_synced_at).toLocaleString("ru-RU") : "—";
  const cards = [
    ["Треки", count(summary.with_tracking), `${count(summary.orders_total)} заказов`],
    ["Синхронизировано", count(summary.synced), `обновлено ${updated}`],
    ["Доставлено", count(summary.delivered), `${percent(summary.with_tracking ? (summary.delivered / summary.with_tracking) * 100 : 0)}`],
    ["В пути", count(summary.in_progress), `${count(summary.not_synced)} ждут проверки`],
    ["Сумма заказов", money(summary.order_revenue), `доставка ${money(summary.delivery_sum)}`],
  ];
  document.querySelector("#cdekSummary").innerHTML = cards.map(([label, value, hint]) => `
    <div class="delivery-card"><span>${html(label)}</span><strong>${html(value)}</strong><em>${html(hint)}</em></div>
  `).join("");

  setTable("cdekStatusTable", ["Статус", "Код", "Заказы", "Сумма доставки"], [
    ...(cdek.statuses || []).map((row) => [row.status, row.status_code || "—", count(row.orders), money(row.delivery_sum)]),
    Object.assign(["Итого", "", count(sum(cdek.statuses || [], "orders")), money(sum(cdek.statuses || [], "delivery_sum"))], { isTotal: true }),
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

  setTable("cdekTable", ["Дата CDEK", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "Товары", "Доставка", "Сумма"], [
    ...(cdek.rows || []).map((row) => Object.assign([
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
    ], { rowClass: row.order_type === "Повторный" ? "is-repeat-order" : "" })),
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

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", async () => {
    mode = tab.dataset.mode;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    await loadDashboard();
  });
});

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
