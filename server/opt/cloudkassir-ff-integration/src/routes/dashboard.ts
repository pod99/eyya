import type { FastifyInstance } from "fastify";
import {
  adminSessionCookieHeader,
  isAdminAuthorized,
  isDashboardCredentialsValid
} from "./auth.js";

export async function registerDashboardRoutes(app: FastifyInstance) {
  app.get("/dashboard", async (request, reply) => {
    if (!isAdminAuthorized(request)) {
      return reply.type("text/html; charset=utf-8").send(renderLoginHtml());
    }

    return reply.header("Set-Cookie", adminSessionCookieHeader()).type("text/html; charset=utf-8").send(renderDashboardHtml());
  });

  app.post("/dashboard/login", async (request, reply) => {
    const body = request.body as Record<string, unknown> | undefined;
    const username = readFormString(body, "username");
    const password = readFormString(body, "password");

    if (!isDashboardCredentialsValid(username, password)) {
      return reply.code(401).type("text/html; charset=utf-8").send(renderLoginHtml("Неверный логин или пароль"));
    }

    return reply.header("Set-Cookie", adminSessionCookieHeader()).code(303).redirect("../dashboard");
  });
}

function readFormString(body: Record<string, unknown> | undefined, key: string): string {
  const value = body?.[key];
  if (Array.isArray(value)) {
    return String(value[0] ?? "");
  }
  return typeof value === "string" ? value : "";
}

function renderLoginHtml(error = ""): string {
  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CloudKassir FF Login</title>
  <style>
    @font-face{font-family:'Normalidad';src:url('https://static.tildacdn.com/tild6338-3936-4564-b838-346431666134/Normalidad-Wide-Regu.woff') format('woff');font-weight:400;font-style:normal}
    @font-face{font-family:'Normalidad';src:url('https://static.tildacdn.com/tild3263-3064-4535-a364-356137323665/Normalidad-Wide-Bold.woff') format('woff');font-weight:700;font-style:normal}
    :root{--paper:#f2f0f3;--card:#fff;--ink:#2f2f2f;--muted:#746169;--line:#e0dce3;--bad:#7a3f3f;--badb:#f3eaea}
    *{box-sizing:border-box} body{margin:0;min-height:100vh;background:var(--paper);color:var(--ink);font-family:'Normalidad',Arial,sans-serif;display:grid;place-items:center;padding:24px}
    .box{width:min(440px,100%);background:var(--card);border:1px solid var(--line);border-radius:3px;padding:30px}
    .eyebrow{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);margin-bottom:10px}
    h1{margin:0 0 24px;font-size:30px;line-height:1.05;text-transform:uppercase;letter-spacing:0}
    label{display:block;font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:14px 0 7px}
    input{width:100%;height:42px;border:1px solid var(--line);border-radius:3px;padding:9px 11px;font:inherit;background:#fff;color:var(--ink)}
    button{width:100%;height:42px;margin-top:20px;border:1px solid var(--ink);border-radius:3px;background:var(--ink);color:var(--paper);font:inherit;text-transform:uppercase;letter-spacing:.08em;cursor:pointer}
    .err{background:var(--badb);color:var(--bad);padding:10px 12px;border-radius:3px;margin-bottom:14px;font-size:12px}
  </style>
</head>
<body>
  <form class="box" method="post" action="dashboard/login">
    <div class="eyebrow">EYYA fiscal operations</div>
    <h1>Вход</h1>
    ${error ? `<div class="err">${escapeHtml(error)}</div>` : ""}
    <label for="username">Логин</label>
    <input id="username" name="username" autocomplete="username" autofocus />
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password" autocomplete="current-password" />
    <button type="submit">Войти</button>
  </form>
</body>
</html>`;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char] ?? char);
}

function renderDashboardHtml(): string {
  return `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CloudKassir FF Dashboard</title>
  <style>
    @font-face{font-family:'Normalidad';src:url('https://static.tildacdn.com/tild6464-6333-4335-b531-353837346562/Normalidad-Wide-Ligh.woff') format('woff');font-weight:300;font-style:normal}
    @font-face{font-family:'Normalidad';src:url('https://static.tildacdn.com/tild6338-3936-4564-b838-346431666134/Normalidad-Wide-Regu.woff') format('woff');font-weight:400;font-style:normal}
    @font-face{font-family:'Normalidad';src:url('https://static.tildacdn.com/tild3263-3064-4535-a364-356137323665/Normalidad-Wide-Bold.woff') format('woff');font-weight:700;font-style:normal}
    :root{
      --paper:#f2f0f3;--panel:#fff;--ink:#2f2f2f;--muted:#746169;--faint:#a8a8a8;
      --line:#e0dce3;--accent:#746169;--dark:#2f2f2f;--ok-c:#3d6b52;--ok-b:#e8f3ec;
      --warn-c:#7a5c1e;--warn-b:#f5edda;--bad-c:#7a3f3f;--bad-b:#f3eaea;--info-b:#eceaf0;
    }
    *{box-sizing:border-box}
    ::selection{background:var(--accent);color:#fff}
    body{
      margin:0;background:var(--paper);color:var(--ink);font-family:'Normalidad',Arial,sans-serif;
      font-weight:300;font-size:13px;line-height:1.65;-webkit-font-smoothing:antialiased;
    }
    button,input,select{font:inherit}
    button{cursor:pointer}
    .app{max-width:1440px;margin:0 auto;padding:42px 36px 72px}
    .masthead{
      display:grid;grid-template-columns:1fr auto;gap:28px;align-items:end;
      padding-bottom:28px;border-bottom:1px solid var(--line);margin-bottom:24px;
    }
    .eyebrow{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--accent);margin-bottom:10px}
    h1{margin:0;font-size:40px;line-height:1.05;font-weight:700;text-transform:uppercase;letter-spacing:0}
    h1 em{font-style:normal;color:var(--accent)}
    .meta{text-align:right;color:var(--muted);font-size:12px;line-height:1.9}
    .meta strong{color:var(--ink);font-weight:700}
    .topbar{display:flex;gap:10px;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap}
    .tabs{display:flex;gap:7px;flex-wrap:wrap}
    .tab,.btn,.icon-btn{
      border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:3px;
      padding:9px 11px;text-transform:uppercase;letter-spacing:.07em;font-size:10.5px;
      transition:border-color .15s,background .15s,color .15s,box-shadow .15s;
    }
    .tab:hover,.btn:hover,.icon-btn:hover{border-color:#ccc8d0;box-shadow:0 4px 16px rgba(116,97,105,.08)}
    .tab.active,.btn.primary{background:var(--dark);color:var(--paper);border-color:var(--dark)}
    .btn.danger{color:var(--bad-c);border-color:#e4c7c7;background:var(--bad-b)}
    .btn.small{padding:6px 8px;font-size:9.5px}
    .actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    .statusline{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}
    .view{display:none}
    .view.active{display:block}
    .metrics{display:grid;grid-template-columns:repeat(6,minmax(138px,1fr));gap:10px;margin-bottom:18px}
    .metric,.panel{
      background:var(--panel);border:1px solid var(--line);border-radius:3px;
      transition:border-color .2s,box-shadow .2s;
    }
    .metric:hover,.panel:hover{border-color:#ccc8d0;box-shadow:0 4px 16px rgba(116,97,105,.08)}
    .metric{min-height:102px;padding:17px 18px;display:flex;flex-direction:column;justify-content:space-between}
    .metric span{font-size:9.5px;color:var(--faint);letter-spacing:.16em;text-transform:uppercase}
    .metric strong{font-size:31px;line-height:1;font-weight:700}
    .metric.bad strong{color:var(--bad-c)}.metric.warn strong{color:var(--warn-c)}.metric.ok strong{color:var(--ok-c)}
    .split{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(340px,.65fr);gap:14px}
    .panel{margin-bottom:14px;overflow:hidden}
    .panel-head{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;border-bottom:1px solid var(--line)}
    .panel-title{display:flex;align-items:baseline;gap:12px}
    .panel-alpha{font-size:10px;font-weight:700;color:var(--accent);letter-spacing:.18em;text-transform:uppercase}
    h2{margin:0;font-size:15px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
    .filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
    input,select{
      min-height:34px;border:1px solid var(--line);border-radius:3px;background:#fff;color:var(--ink);
      padding:7px 9px;outline:none;min-width:150px;
    }
    input:focus,select:focus{border-color:var(--accent)}
    .table-wrap{width:100%;overflow:auto}
    table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
    th{font-size:9.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--faint);font-weight:700;background:#faf9fb;white-space:nowrap}
    td{color:var(--muted)}
    td strong{color:var(--ink);font-weight:700}
    code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;overflow-wrap:anywhere;color:var(--ink)}
    .badge{display:inline-flex;align-items:center;gap:5px;padding:3px 7px;border-radius:2px;font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;font-weight:400;white-space:nowrap}
    .bd{background:var(--ok-b);color:var(--ok-c)}.bw{background:var(--warn-b);color:var(--warn-c)}.bf{background:var(--bad-b);color:var(--bad-c)}.bi{background:var(--info-b);color:var(--muted)}
    .empty{padding:22px 16px;color:var(--faint);font-size:12px;text-transform:uppercase;letter-spacing:.07em}
    .kv{display:grid;grid-template-columns:minmax(150px,.35fr) minmax(0,1fr);border-top:1px solid var(--line)}
    .kv div{padding:10px 12px;border-bottom:1px solid var(--line)}
    .kv div:nth-child(odd){color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.12em;background:#faf9fb}
    .url-list{display:grid;gap:8px;padding:14px 16px}
    .url-row{display:grid;grid-template-columns:100px minmax(0,1fr) auto;gap:8px;align-items:center}
    .url-row code{display:block;background:#faf9fb;border:1px solid var(--line);padding:8px;border-radius:3px}
    .drawer{position:fixed;inset:0;background:rgba(47,47,47,.28);display:none;z-index:20}
    .drawer.open{display:block}
    .drawer-panel{position:absolute;right:0;top:0;width:min(720px,100vw);height:100%;background:var(--panel);border-left:1px solid var(--line);display:flex;flex-direction:column}
    .drawer-head{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:16px;border-bottom:1px solid var(--line)}
    .drawer pre{margin:0;padding:16px;overflow:auto;white-space:pre-wrap;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--ink)}
    .toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--dark);color:var(--paper);padding:10px 14px;border-radius:3px;font-size:12px;display:none;z-index:30}
    .toast.show{display:block}
    @media(max-width:1080px){.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.split{grid-template-columns:1fr}.masthead{grid-template-columns:1fr}.meta{text-align:left}}
    @media(max-width:680px){.app{padding:28px 16px 56px}h1{font-size:30px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.url-row{grid-template-columns:1fr}.filters input,.filters select{min-width:0;width:100%}}
  </style>
</head>
<body>
  <main class="app">
    <header class="masthead">
      <div>
        <div class="eyebrow">EYYA fiscal operations</div>
        <h1>CloudKassir <em>FF</em></h1>
      </div>
      <div class="meta">
        <div id="modeLine"><strong>...</strong></div>
        <div id="updatedLine">...</div>
      </div>
    </header>

    <div class="topbar">
      <nav class="tabs" id="tabs">
        <button class="tab active" data-view="overview">Сводка</button>
        <button class="tab" data-view="orders">Заказы</button>
        <button class="tab" data-view="receipts">Чеки</button>
        <button class="tab" data-view="returns">Возвраты</button>
        <button class="tab" data-view="webhooks">Webhook-и</button>
        <button class="tab" data-view="payments">Платежи</button>
        <button class="tab" data-view="kiz">KIZ-аудит</button>
        <button class="tab" data-view="ff">FF Platform</button>
        <button class="tab" data-view="cdek">СДЭК</button>
        <button class="tab" data-view="settings">Настройки</button>
      </nav>
      <div class="actions">
        <select id="exportType" aria-label="Тип CSV-отчета">
          <option value="orders">Заказы CSV</option>
          <option value="fiscal-receipts">Наши чеки CSV</option>
          <option value="imported-receipts">CloudKassir чеки CSV</option>
          <option value="returns">Возвраты CSV</option>
          <option value="transactions">Платежи CSV</option>
          <option value="webhook-events">Webhook-и CSV</option>
          <option value="mark-codes">KIZ CSV</option>
          <option value="shipments">FF отгрузки CSV</option>
          <option value="tracks">Трек-номера CSV</option>
          <option value="cdek">СДЭК отчет CSV</option>
        </select>
        <button class="btn" data-action="export-selected" type="button">Скачать</button>
        <span class="statusline" id="loadState">Готово</span>
        <button class="btn primary" id="refreshButton" type="button">Обновить</button>
      </div>
    </div>

    <section id="view-overview" class="view active">
      <div class="metrics" id="summaryMetrics"></div>
      <div class="split">
        <section class="panel">
          <div class="panel-head"><div class="panel-title"><span class="panel-alpha">01</span><h2>Последние ошибки</h2></div></div>
          <div id="recentErrors"></div>
        </section>
        <section class="panel">
          <div class="panel-head"><div class="panel-title"><span class="panel-alpha">02</span><h2>Статусы чеков</h2></div></div>
          <div id="receiptStatusBox"></div>
        </section>
      </div>
    </section>

    <section id="view-orders" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Заказы</h2></div>
          <div class="filters">
            <select id="ordersStatus">
              <option value="">Все статусы</option>
              <option value="paid">Оплаченные</option>
              <option value="pending_second">Ждут второй чек</option>
              <option value="error">С ошибками</option>
              <option value="without_shipment">Без отгрузки</option>
            </select>
            <input id="ordersSearch" placeholder="Invoice, email, трек" />
            <button class="btn" data-reload="orders" type="button">Найти</button>
          </div>
        </div>
        <div id="ordersTable"></div>
      </section>
    </section>

    <section id="view-receipts" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Наши чеки</h2></div>
          <div class="filters">
            <select id="receiptsKind">
              <option value="">Все типы</option>
              <option value="first_prepayment">Первый чек</option>
              <option value="second_fullpay">Второй чек</option>
              <option value="second_fullpay_return">Закрывающий возврат</option>
            </select>
            <select id="receiptsStatus">
              <option value="">Все статусы</option>
              <option value="dry_run">dry_run</option>
              <option value="sent">sent</option>
              <option value="queued">queued</option>
              <option value="processed">processed</option>
              <option value="error">error</option>
              <option value="sending">sending</option>
            </select>
            <input id="receiptsSearch" placeholder="Invoice, request, CloudKassir" />
            <button class="btn" data-reload="receipts" type="button">Найти</button>
          </div>
        </div>
        <div id="fiscalReceiptsTable"></div>
      </section>
      <section class="panel">
        <div class="panel-head"><div class="panel-title"><span class="panel-alpha">02</span><h2>Импорт из CloudKassir</h2></div></div>
        <div id="importedReceiptsTable"></div>
      </section>
    </section>

    <section id="view-returns" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Возвраты и закрывающие чеки</h2></div>
          <div class="filters">
            <select id="returnsStatus">
              <option value="">Все статусы</option>
              <option value="Processed">Processed</option>
              <option value="processed">processed</option>
              <option value="Error">Error</option>
              <option value="Queued">Queued</option>
            </select>
            <input id="returnsSearch" placeholder="Invoice, CloudKassir, ФП" />
            <button class="btn" data-reload="returns" type="button">Найти</button>
          </div>
        </div>
        <div id="returnsTable"></div>
      </section>
    </section>

    <section id="view-webhooks" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Webhook-события</h2></div>
          <div class="filters">
            <select id="webhooksType">
              <option value="">Все события</option>
              <option value="check">check</option><option value="pay">pay</option><option value="fail">fail</option>
              <option value="confirm">confirm</option><option value="refund">refund</option><option value="receipt">receipt</option>
              <option value="sbptoken">sbptoken</option><option value="kkt">kkt</option><option value="recurrent">recurrent</option><option value="cancel">cancel</option>
            </select>
            <input id="webhooksSearch" placeholder="Invoice, Transaction, payload" />
            <button class="btn" data-reload="webhooks" type="button">Найти</button>
          </div>
        </div>
        <div id="webhooksTable"></div>
      </section>
    </section>

    <section id="view-payments" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Транзакции</h2></div>
          <div class="filters">
            <select id="paymentsStatus">
              <option value="">Все статусы</option>
              <option value="Authorized">Authorized</option>
              <option value="Completed">Completed</option>
              <option value="Cancelled">Cancelled</option>
              <option value="Declined">Declined</option>
            </select>
            <input id="paymentsSearch" placeholder="Invoice, Transaction, email" />
            <button class="btn" data-reload="payments" type="button">Найти</button>
          </div>
        </div>
        <div id="paymentsTable"></div>
      </section>
    </section>

    <section id="view-kiz" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>KIZ-аудит</h2></div>
          <div class="filters">
            <select id="kizProblem">
              <option value="">Все коды</option>
              <option value="unlinked">Без позиции заказа</option>
              <option value="unused_after_sent">Не использованы после чека</option>
              <option value="unused">Активные неиспользованные</option>
              <option value="used">Использованные</option>
            </select>
            <input id="kizSearch" placeholder="KIZ, SKU, заказ, отгрузка" />
            <button class="btn" data-reload="kiz" type="button">Найти</button>
          </div>
        </div>
        <div id="kizTable"></div>
      </section>
    </section>

    <section id="view-ff" class="view">
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">01</span><h2>Отгрузки FF</h2></div>
          <div class="filters">
            <input id="ffStatus" placeholder="Статус" />
            <input id="ffSearch" placeholder="Отгрузка, заказ, трек, SKU" />
            <button class="btn" data-reload="ff" type="button">Найти</button>
          </div>
        </div>
        <div id="ffTable"></div>
      </section>
    </section>

    <section id="view-cdek" class="view">
      <div class="metrics" id="cdekMetrics"></div>
      <div class="split">
        <section class="panel">
          <div class="panel-head"><div class="panel-title"><span class="panel-alpha">01</span><h2>Статусы СДЭК</h2></div></div>
          <div id="cdekStatusTable"></div>
        </section>
        <section class="panel">
          <div class="panel-head"><div class="panel-title"><span class="panel-alpha">02</span><h2>SKU в заказах</h2></div></div>
          <div id="cdekSkuTable"></div>
        </section>
      </div>
      <section class="panel">
        <div class="panel-head">
          <div class="panel-title"><span class="panel-alpha">03</span><h2>СДЭК номера и состав</h2></div>
          <div class="filters">
            <input id="cdekStatus" placeholder="Статус CDEK" />
            <input id="cdekSearch" placeholder="Трек, заказ, ФИО, телефон, SKU" />
            <button class="btn" data-reload="cdek" type="button">Найти</button>
          </div>
        </div>
        <div id="cdekRowsTable"></div>
      </section>
    </section>

    <section id="view-settings" class="view">
      <div class="split">
        <section class="panel">
          <div class="panel-head"><div class="panel-title"><span class="panel-alpha">01</span><h2>Конфигурация</h2></div></div>
          <div id="settingsBox"></div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <div class="panel-title"><span class="panel-alpha">02</span><h2>Webhook URLs</h2></div>
            <button class="btn small" data-action="reveal-webhooks" type="button">Показать URL</button>
          </div>
          <div id="webhookUrls"></div>
        </section>
      </div>
    </section>
  </main>

  <div class="drawer" id="drawer">
    <div class="drawer-panel">
      <div class="drawer-head">
        <div><div class="eyebrow" id="drawerEyebrow">Детали</div><h2 id="drawerTitle">JSON</h2></div>
        <button class="btn" id="drawerClose" type="button">Закрыть</button>
      </div>
      <pre id="drawerBody"></pre>
    </div>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const state = { view: "overview", summary: null, detail: new Map(), seq: 1 };
    const metricLabels = [
      ["orders_paid", "Оплачено"],
      ["pending_second", "Ждут второй чек"],
      ["second_receipts", "Вторые чеки"],
      ["pending_second_returns", "Ждут возврат"],
      ["error", "Ошибки"],
      ["unlinked_active_marks", "KIZ без позиции"]
    ];

    function apiPrefix() {
      const marker = "/dashboard";
      const index = window.location.pathname.indexOf(marker);
      return index === -1 ? "" : window.location.pathname.slice(0, index);
    }
    function apiUrl(path) { return window.location.origin + apiPrefix() + path; }
    function qs(params) {
      const query = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && String(value).trim() !== "") query.set(key, String(value));
      });
      return query.toString() ? "?" + query.toString() : "";
    }
    async function api(path, options) {
      setLoading(true);
      try {
        const response = await fetch(apiUrl(path), Object.assign({ credentials: "same-origin" }, options || {}));
        const text = await response.text();
        const data = text ? JSON.parse(text) : null;
        if (!response.ok) throw new Error((data && data.error) || ("HTTP " + response.status));
        return data;
      } finally {
        setLoading(false);
      }
    }
    function setLoading(value) {
      document.getElementById("loadState").textContent = value ? "Загрузка" : "Готово";
    }
    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;" }[char]));
    }
    function fmtDate(value) {
      if (!value) return "";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString("ru-RU");
    }
    function money(kop, currency) {
      if (kop === null || kop === undefined) return "";
      return (Number(kop) / 100).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " " + (currency || "RUB");
    }
    function rub(kop) {
      if (kop === null || kop === undefined) return "";
      return (Number(kop) / 100).toLocaleString("ru-RU", { maximumFractionDigits: 0 }) + " ₽";
    }
    function badge(value) {
      const text = esc(value || "none");
      const normalized = String(value || "").toLowerCase();
      const tone = ["processed","sent","queued","completed","paid","dry_run","authorized","imported"].includes(normalized) ? "bd" :
        ["error","declined","fail","cancelled"].includes(normalized) ? "bf" :
        ["sending","pending","created"].includes(normalized) ? "bw" : "bi";
      return '<span class="badge ' + tone + '">' + text + '</span>';
    }
    function stash(value) {
      const id = "d" + state.seq++;
      state.detail.set(id, value);
      return id;
    }
    function detailButton(title, value) {
      const id = stash({ title, value });
      return '<button class="btn small" data-action="detail" data-detail-id="' + id + '" type="button">Детали</button>';
    }
    function safeHttpsUrl(value) {
      try {
        const url = new URL(String(value || ""));
        return url.protocol === "https:" ? url.href : "";
      } catch {
        return "";
      }
    }
    function safeLink(url, label) {
      const href = safeHttpsUrl(url);
      return href ? '<a href="' + esc(href) + '" target="_blank" rel="noreferrer">' + esc(label) + '</a>' : "";
    }
    function table(headers, rows) {
      if (!rows.length) return '<div class="empty">Нет данных</div>';
      return '<div class="table-wrap"><table><thead><tr>' + headers.map((header) => '<th>' + esc(header) + '</th>').join("") +
        '</tr></thead><tbody>' + rows.join("") + '</tbody></table></div>';
    }
    function toast(text) {
      const box = document.getElementById("toast");
      box.textContent = text;
      box.className = "toast show";
      setTimeout(() => { box.className = "toast"; }, 2400);
    }

    async function refreshSummary() {
      const summary = await api("/api/admin/summary");
      state.summary = summary;
      document.getElementById("modeLine").innerHTML = "<strong>" + (summary.dryRun ? "DRY RUN" : "LIVE") + "</strong>";
      document.getElementById("updatedLine").textContent = "Обновлено " + fmtDate(summary.generatedAt);
      renderSummary(summary);
      return summary;
    }
    function renderSummary(summary) {
      document.getElementById("summaryMetrics").innerHTML = metricLabels.map(([key, label]) => {
        const value = summary.counts[key] ?? 0;
        const tone = key === "error" && value > 0 ? " bad" : value > 0 ? " warn" : " ok";
        return '<article class="metric' + tone + '"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong></article>';
      }).join("");
      renderRecentErrors(summary.recentErrors || []);
      renderReceiptStatuses(summary.receiptStatuses || {});
    }
    function renderRecentErrors(rows) {
      document.getElementById("recentErrors").innerHTML = table(
        ["ID", "Заказ", "Тип", "Статус", "Invoice", "Ошибка", "Обновлено"],
        rows.map((row) => '<tr><td>' + row.id + '</td><td><code>' + esc(row.order?.externalId || "") + '</code></td><td>' + esc(row.kind) + '</td><td>' + badge(row.status) + '</td><td><code>' + esc(row.invoiceId || "") + '</code></td><td>' + esc(row.errorMessage || "") + '</td><td>' + fmtDate(row.updatedAt) + '</td></tr>')
      );
    }
    function renderReceiptStatuses(statuses) {
      const rows = Object.entries(statuses.all || {}).map(([status, count]) => '<tr><td>' + badge(status) + '</td><td><strong>' + count + '</strong></td></tr>');
      document.getElementById("receiptStatusBox").innerHTML = table(["Статус", "Чеков"], rows);
    }

    async function loadView(view) {
      state.view = view;
      document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === view));
      document.querySelectorAll(".view").forEach((node) => node.classList.toggle("active", node.id === "view-" + view));
      if (view === "overview") { await refreshSummary(); return; }
      if (view === "orders") return loadOrders();
      if (view === "receipts") return loadReceipts();
      if (view === "returns") return loadReturns();
      if (view === "webhooks") return loadWebhooks();
      if (view === "payments") return loadPayments();
      if (view === "kiz") return loadKiz();
      if (view === "ff") return loadFf();
      if (view === "cdek") return loadCdek();
      if (view === "settings") return loadSettings();
    }

    async function loadOrders() {
      const data = await api("/api/admin/orders" + qs({ status: value("ordersStatus"), search: value("ordersSearch"), take: 120 }));
      document.getElementById("ordersTable").innerHTML = table(["Заказ", "Сумма", "Оплата", "Чеки", "FF", "KIZ", "Клиент", "Действия"], data.rows.map((row) => {
        const first = row.firstReceipt ? badge(row.firstReceipt.status) : badge("нет");
        const second = row.secondReceipt ? badge(row.secondReceipt.status) : badge("нет");
        const shipment = row.latestShipment ? badge(row.latestShipment.status) + '<br><code>' + esc(row.skladDispatchId || "") + '</code>' : badge("нет");
        const actions = '<button class="btn small" data-action="sync-ff" data-external-id="' + esc(row.externalId) + '" type="button">Sync FF</button> ' +
          '<button class="btn small" data-action="second-receipt" data-external-id="' + esc(row.externalId) + '" type="button">2 чек</button> ' +
          detailButton("Заказ " + row.externalId, row);
        return '<tr><td><strong>' + esc(row.externalId) + '</strong><br><code>' + esc(row.deliveryTrackNumber || "") + '</code></td><td>' + money(row.amountKop, row.currency) + '</td><td>' + badge(row.paymentStatus) + '</td><td>1: ' + first + '<br>2: ' + second + '</td><td>' + shipment + '</td><td><strong>' + row.linkedMarks + "/" + row.markedRequired + '</strong><br>used ' + row.usedMarks + '</td><td>' + esc(row.customerEmail || "") + '<br>' + esc(row.customerPhone || "") + '</td><td>' + actions + '</td></tr>';
      }));
    }
    async function loadReceipts() {
      const data = await api("/api/admin/receipts" + qs({ kind: value("receiptsKind"), status: value("receiptsStatus"), search: value("receiptsSearch"), take: 100 }));
      document.getElementById("fiscalReceiptsTable").innerHTML = table(["ID", "Заказ", "Тип", "Статус", "Invoice", "CloudKassir", "Ошибка", "Действия"], data.fiscalReceipts.map((row) => {
        const actions = '<button class="btn small" data-action="refresh-receipt" data-receipt-id="' + row.id + '" type="button">Статус</button> ' + detailButton("Фискальный чек " + row.id, row);
        return '<tr><td>' + row.id + '</td><td><code>' + esc(row.order?.externalId || "") + '</code></td><td>' + esc(row.kind) + '</td><td>' + badge(row.status) + '</td><td><code>' + esc(row.invoiceId || "") + '</code></td><td><code>' + esc(row.cloudKassirId || "") + '</code></td><td>' + esc(row.errorMessage || "") + '</td><td>' + actions + '</td></tr>';
      }));
      document.getElementById("importedReceiptsTable").innerHTML = table(["CloudKassir", "Invoice", "Сумма", "ФД", "ФП", "URL", "Позиций", "Детали"], data.importedReceipts.map((row) => {
        const link = safeLink(row.url, "ОФД");
        return '<tr><td><code>' + esc(row.cloudKassirId) + '</code></td><td><code>' + esc(row.invoiceId || "") + '</code></td><td>' + money(row.amountKop, "RUB") + '</td><td>' + esc(row.documentNumber || "") + '</td><td>' + esc(row.fiscalSign || "") + '</td><td>' + link + '</td><td>' + (row.items || []).length + '</td><td>' + detailButton("Импортированный чек " + row.cloudKassirId, row) + '</td></tr>';
      }));
    }
    async function loadReturns() {
      const data = await api("/api/admin/returns" + qs({ status: value("returnsStatus"), search: value("returnsSearch"), take: 160 }));
      document.getElementById("returnsTable").innerHTML = table(["Возврат", "Заказ", "Сумма", "Второй чек", "Закрывающий возврат", "Причина", "Состав", "Действия"], data.rows.map((row) => {
        const order = row.order || {};
        const shipment = order.latestShipment || {};
        const items = (order.items || []).slice(0, 3).map((item) => esc(item.clientCode) + " x" + esc(item.quantity)).join("<br>");
        const second = row.secondReceipt ? badge(row.secondReceipt.status) + '<br><code>' + esc(row.secondReceipt.cloudKassirId || "") + '</code>' : badge("нет");
        const secondReturn = row.secondReturnReceipt ? badge(row.secondReturnReceipt.status) + '<br><code>' + esc(row.secondReturnReceipt.cloudKassirId || "") + '</code>' : badge("нет");
        const action = row.needsSecondReturn
          ? '<button class="btn small danger" data-action="second-return" data-return-id="' + esc(row.cloudKassirId) + '" type="button">Закрыть возврат</button> '
          : "";
        const ofd = safeLink(row.url, "ОФД");
        return '<tr><td><code>' + esc(row.cloudKassirId) + '</code><br>' + badge(row.status) + '<br>' + esc(row.dateTime || "") + '<br>' + ofd + '</td><td><strong>' + esc(row.invoiceId || "") + '</strong><br><code>' + esc(order.deliveryTrackNumber || shipment.trackNumber || shipment.cdekNumber || "") + '</code><br>FF <code>' + esc(order.skladDispatchId || "") + '</code></td><td>' + money(row.amountKop, "RUB") + '</td><td>' + second + '</td><td>' + secondReturn + '</td><td>' + (row.needsSecondReturn ? badge("нужно") : badge("ok")) + '<br>' + esc(row.reason) + '</td><td>' + items + '</td><td>' + action + detailButton("Возврат " + row.cloudKassirId, row.detail || row) + '</td></tr>';
      }));
    }
    async function loadWebhooks() {
      const data = await api("/api/admin/webhook-events" + qs({ eventType: value("webhooksType"), search: value("webhooksSearch"), take: 160 }));
      document.getElementById("webhooksTable").innerHTML = table(["ID", "Событие", "External", "Дата", "Payload"], data.rows.map((row) =>
        '<tr><td>' + row.id + '</td><td>' + badge(row.eventType) + '</td><td><code>' + esc(row.externalId || "") + '</code></td><td>' + fmtDate(row.createdAt) + '</td><td>' + detailButton("Webhook " + row.eventType + " #" + row.id, parseJson(row.payloadJson)) + '</td></tr>'
      ));
    }
    async function loadPayments() {
      const data = await api("/api/admin/transactions" + qs({ status: value("paymentsStatus"), search: value("paymentsSearch"), take: 140 }));
      document.getElementById("paymentsTable").innerHTML = table(["Transaction", "Invoice", "Сумма", "Статус", "Карта", "Причина", "Дата", "Детали"], data.rows.map((row) =>
        '<tr><td><code>' + esc(row.transactionId) + '</code></td><td><code>' + esc(row.invoiceId || "") + '</code></td><td>' + money(row.amountKop, row.currency) + '</td><td>' + badge(row.status) + '</td><td>' + esc(row.cardType || "") + ' ' + esc(row.cardFirstSix || "") + '...' + esc(row.cardLastFour || "") + '</td><td>' + esc(row.reason || row.reasonCode || "") + '</td><td>' + esc(row.paymentDate || "") + '</td><td>' + detailButton("Транзакция " + row.transactionId, parseJson(row.rawJson)) + '</td></tr>'
      ));
    }
    async function loadKiz() {
      const data = await api("/api/admin/mark-codes" + qs({ problem: value("kizProblem"), search: value("kizSearch"), take: 180 }));
      document.getElementById("kizTable").innerHTML = table(["ID", "KIZ", "Заказ", "SKU", "Отгрузка", "Статус", "Детали"], data.rows.map((row) => {
        const orderId = row.orderItem?.order?.externalId || row.shipmentProduct?.shipment?.order?.externalId || "";
        const shipment = row.shipmentProduct?.shipment;
        return '<tr><td>' + row.id + '</td><td><code>' + esc(shortMark(row.code)) + '</code></td><td><code>' + esc(orderId) + '</code></td><td>' + esc(row.orderItem?.clientCode || row.shipmentProduct?.clientCode || "") + '<br>' + esc(row.orderItem?.name || row.shipmentProduct?.name || "") + '</td><td><code>' + esc(shipment?.skladDispatchId || "") + '</code><br>' + badge(shipment?.status || "") + '</td><td>' + (row.isActive ? badge("active") : badge("inactive")) + '<br>' + (row.usedInSecondReceipt ? badge("used") : badge("unused")) + '</td><td>' + detailButton("KIZ " + row.id, row) + '</td></tr>';
      }));
    }
    async function loadFf() {
      const data = await api("/api/admin/shipments" + qs({ status: value("ffStatus"), search: value("ffSearch"), take: 120 }));
      document.getElementById("ffTable").innerHTML = table(["Отгрузка", "Заказ", "Статус", "Трек", "Получатель", "Товары", "KIZ", "Детали"], data.rows.map((row) => {
        const marks = (row.products || []).reduce((sum, product) => sum + (product.markCodes || []).length, 0);
        const products = (row.products || []).slice(0, 3).map((product) => esc(product.clientCode) + " x" + product.factQty).join("<br>");
        return '<tr><td><code>' + esc(row.skladDispatchId) + '</code></td><td><code>' + esc(row.order?.externalId || row.cdekOrderNumber || "") + '</code></td><td>FF ' + badge(row.status) + '<br>FF CDEK ' + badge(row.cdekStatus || "") + '<br>Delivery ' + badge(row.cdekDeliveryStatus || "") + '</td><td><code>' + esc(row.trackNumber || row.cdekNumber || "") + '</code></td><td>' + esc(row.cdekRecipientName || "") + '<br><code>' + esc(row.cdekRecipientPhone || "") + '</code></td><td>' + products + '</td><td><strong>' + marks + '</strong></td><td>' + detailButton("Отгрузка " + row.skladDispatchId, row) + '</td></tr>';
      }));
    }
    async function loadCdek() {
      const data = await api("/api/admin/cdek" + qs({ status: value("cdekStatus"), search: value("cdekSearch"), take: 140 }));
      const summary = data.summary || {};
      const metrics = [
        ["Треки", summary.tracks || 0, (summary.tracks || 0) + " заказов"],
        ["Синхронизировано", summary.synced || 0, "ожидают проверки " + (summary.waitingCheck || 0)],
        ["Доставлено", summary.delivered || 0, (summary.deliveredPercent || 0) + "%"],
        ["В пути", summary.inTransit || 0, "активные статусы"],
        ["Повторные", summary.repeatOrders || 0, (summary.duplicateGroups || 0) + " групп / " + rub(summary.repeatAmountKop || 0)],
        ["Сумма заказов", rub(summary.orderAmountKop || 0), "доставка " + rub(summary.deliveryAmountKop || 0)]
      ];
      document.getElementById("cdekMetrics").innerHTML = metrics.map(([label, value, note]) =>
        '<article class="metric"><span>' + esc(label) + '</span><strong>' + esc(value) + '</strong><span>' + esc(note) + '</span></article>'
      ).join("");
      const statusRows = (data.statusRows || []).map((row) =>
        '<tr><td>' + esc(row.label) + '</td><td><code>' + esc(row.code) + '</code></td><td><strong>' + esc(row.count) + '</strong></td><td>' + rub(row.orderAmountKop) + '</td><td>' + rub(row.deliveryAmountKop) + '</td></tr>'
      );
      statusRows.push('<tr><td><strong>Итого</strong></td><td></td><td><strong>' + esc(summary.tracks || 0) + '</strong></td><td><strong>' + rub(summary.orderAmountKop || 0) + '</strong></td><td><strong>' + rub(summary.deliveryAmountKop || 0) + '</strong></td></tr>');
      document.getElementById("cdekStatusTable").innerHTML = table(["Статус", "Код", "Заказы", "Сумма заказов", "Доставка"], statusRows);
      document.getElementById("cdekSkuTable").innerHTML = table(["SKU", "Товар", "Кол-во", "Мин. цена", "Макс. цена", "Сумма"], (data.skuRows || []).map((row) =>
        '<tr><td><code>' + esc(row.sku) + '</code></td><td>' + esc(row.name) + '</td><td><strong>' + esc(row.quantity) + '</strong></td><td>' + rub(row.minPriceKop) + '</td><td>' + rub(row.maxPriceKop) + '</td><td>' + rub(row.amountKop) + '</td></tr>'
      ));
      document.getElementById("cdekRowsTable").innerHTML = table(["Дата", "Заказ", "Тип", "ФИО", "Трек", "Статус", "Город", "План", "Доставлен", "Состав", "KIZ", "Сумма", "Детали"], (data.rows || []).map((row) => {
        const composition = esc(row.composition || "").replace(/\\n/g, "<br>");
        return '<tr><td>' + esc(row.date || "") + '</td><td><code>' + esc(row.orderExternalId || "") + '</code></td><td>' + badge(row.type) + '</td><td>' + esc(row.recipientName || "") + '<br><code>' + esc(row.recipientPhone || "") + '</code></td><td><code>' + esc(row.trackNumber || "") + '</code></td><td>' + esc(row.statusLabel || "") + '<br><code>' + esc(row.statusCode || "") + '</code></td><td>' + esc(row.city || "") + '</td><td>' + esc(row.plannedDeliveryDate || "") + '</td><td>' + esc(row.deliveredDate || "") + '</td><td>' + composition + '</td><td>' + esc(row.usedMarksCount || 0) + "/" + esc(row.marksCount || 0) + '</td><td>товары ' + rub(row.productAmountKop) + '<br>доставка ' + rub(row.deliveryAmountKop) + '<br><strong>' + rub(row.totalAmountKop) + '</strong></td><td>' + detailButton("СДЭК " + (row.trackNumber || row.skladDispatchId), row.detail || row) + '</td></tr>';
      }));
    }
    async function loadSettings() {
      const data = await api("/api/admin/settings");
      const configRows = [
        ["Режим", data.dryRun ? "DRY_RUN" : "LIVE"],
        ["ИНН", data.inn],
        ["Public ID", data.publicId],
        ["API CloudKassir", data.cloudKassirApiConfigured ? "configured" : "missing"],
        ["FF Platform", data.ffPlatformConfigured ? "configured" : "missing"],
        ["СНО", data.taxationSystem],
        ["Место расчета", data.calculationPlace],
        ["Worker tick", ms(data.workerPollIntervalMs)],
        ["External API timeout", ms(data.externalApiTimeoutMs)],
        ["CloudKassir poll", data.cloudKassirPollEnabled ? ms(data.cloudKassirPollIntervalMs) : "off"],
        ["Lookback", data.cloudKassirPollLookbackMinutes + " min"],
        ["CDEK API", data.cdek?.configured ? "configured" : "missing"],
        ["CDEK enrich", data.cdek?.enrichEnabled ? data.cdek.enrichBatchSize + " / " + ms(data.cdek.enrichIntervalMs) : "off"],
        ["FF scan", data.ffScan?.enabled ? "on" : "off"],
        ["FF scan mode", data.ffScan?.mode || ""],
        ["FF scan batch", data.ffScan ? data.ffScan.batchSize + " / " + ms(data.ffScan.intervalMs) : ""],
        ["FF scan window", data.ffScan?.mode === "cdek_period" ? ((data.ffScan.from || ("last " + data.ffScan.lookbackDays + " days")) + " → " + (data.ffScan.to || "today")) : ""],
        ["FF scan statuses", data.ffScan?.statuses || ""],
        ["FF scan next ID", data.ffScan?.mode === "id_range" ? (data.ffScan?.cursor?.nextSkladDispatchId ?? data.ffScan?.startId ?? "") : ""],
        ["FF scan found ID", data.ffScan?.cursor?.lastFoundId ?? ""],
        ["FF scan finished", data.ffScan?.cursor?.lastFinishedAt ? fmtDate(data.ffScan.cursor.lastFinishedAt) : ""]
      ];
      document.getElementById("settingsBox").innerHTML = '<div class="kv">' + configRows.map(([key, val]) => '<div>' + esc(key) + '</div><div><code>' + esc(val || "") + '</code></div>').join("") + '</div>';
      renderWebhookUrls(data.webhookUrlPaths || []);
    }
    function renderWebhookUrls(paths) {
      const base = window.location.origin + apiPrefix();
      document.getElementById("webhookUrls").innerHTML = '<div class="url-list">' + (paths || []).map((item) => {
        const url = base + item.path;
        return '<div class="url-row"><strong>' + esc(item.event) + '</strong><code>' + esc(url) + '</code><button class="btn small" data-action="copy" data-copy="' + esc(url) + '" type="button">Копия</button></div>';
      }).join("") + '</div>';
    }

    function value(id) { return document.getElementById(id)?.value || ""; }
    function parseJson(text) { try { return JSON.parse(text); } catch { return text; } }
    function shortMark(code) {
      const text = String(code || "");
      return text.length > 34 ? text.slice(0, 18) + "..." + text.slice(-10) : text;
    }
    function ms(value) {
      const number = Number(value || 0);
      if (number >= 60000) return Math.round(number / 60000) + " min";
      return number + " ms";
    }
    function openDrawer(title, value) {
      document.getElementById("drawerTitle").textContent = title;
      document.getElementById("drawerBody").textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
      document.getElementById("drawer").className = "drawer open";
    }
    async function doAction(button) {
      const action = button.dataset.action;
      if (action === "detail") {
        const item = state.detail.get(button.dataset.detailId);
        openDrawer(item.title, item.value);
        return;
      }
      if (action === "copy") {
        await navigator.clipboard.writeText(button.dataset.copy || "");
        toast("Скопировано");
        return;
      }
      if (action === "export-selected") {
        const exportType = value("exportType") || "orders";
        window.open(apiUrl("/api/admin/export/" + encodeURIComponent(exportType)), "_blank");
        return;
      }
      if (action === "reveal-webhooks") {
        const data = await api("/api/admin/webhook-urls/reveal", { method: "POST" });
        renderWebhookUrls(data.webhookUrlPaths || []);
        toast("Webhook URL показаны");
        return;
      }
      if (action === "sync-ff") {
        await api("/api/admin/orders/" + encodeURIComponent(button.dataset.externalId) + "/sync-ff", { method: "POST" });
        toast("FF синхронизирован");
        await loadOrders();
        return;
      }
      if (action === "second-receipt") {
        await api("/api/admin/orders/" + encodeURIComponent(button.dataset.externalId) + "/receipts/second", { method: "POST" });
        toast("Второй чек обработан");
        await loadOrders();
        return;
      }
      if (action === "second-return") {
        await api("/api/admin/returns/" + encodeURIComponent(button.dataset.returnId) + "/second-fullpay-return", { method: "POST" });
        toast("Закрывающий возврат обработан");
        await loadReturns();
        return;
      }
      if (action === "refresh-receipt") {
        await api("/api/admin/receipts/" + encodeURIComponent(button.dataset.receiptId) + "/refresh-status", { method: "POST" });
        toast("Статус обновлен");
        await loadReceipts();
      }
    }

    document.getElementById("tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-view]");
      if (button) loadView(button.dataset.view).catch(showError);
    });
    document.body.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-action]");
      if (actionButton) doAction(actionButton).catch(showError);
      const reloadButton = event.target.closest("[data-reload]");
      if (reloadButton) loadView(reloadButton.dataset.reload).catch(showError);
    });
    document.getElementById("refreshButton").addEventListener("click", () => loadView(state.view).catch(showError));
    document.getElementById("drawerClose").addEventListener("click", () => { document.getElementById("drawer").className = "drawer"; });
    document.getElementById("drawer").addEventListener("click", (event) => {
      if (event.target.id === "drawer") document.getElementById("drawer").className = "drawer";
    });
    function showError(error) {
      console.error(error);
      toast(String(error.message || error));
      setLoading(false);
    }
    refreshSummary().catch(showError);
  </script>
</body>
</html>`;
}
