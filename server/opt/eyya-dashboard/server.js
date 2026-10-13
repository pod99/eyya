const path = require("node:path");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const Fastify = require("fastify");
const fastifyStatic = require("@fastify/static");
const { Pool } = require("pg");

const app = Fastify({ logger: true });
const authUser = process.env.DASHBOARD_USER || "";
const authPassword = process.env.DASHBOARD_PASSWORD || "";
const sessionSecret = process.env.DASHBOARD_SESSION_SECRET || authPassword || "eyya-dashboard-local";
const cdekApiBase = process.env.CDEK_API_BASE || "https://api.cdek.ru/v2";
const cdekClientId = process.env.CDEK_CLIENT_ID || "";
const cdekClientSecret = process.env.CDEK_CLIENT_SECRET || "";
const dashboardCacheTtlMs = Number(process.env.DASHBOARD_CACHE_TTL_MS || 90_000);
const dashboardCache = new Map();
const pool = new Pool({
  host: process.env.PGHOST,
  port: Number(process.env.PGPORT || 5432),
  database: process.env.PGDATABASE,
  user: process.env.PGUSER,
  password: process.env.PGPASSWORD,
  max: 8,
});

function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  return left.length === right.length && crypto.timingSafeEqual(left, right);
}

function parseCookies(header = "") {
  return Object.fromEntries(header.split(";").map((item) => {
    const [key, ...value] = item.trim().split("=");
    return [key, decodeURIComponent(value.join("=") || "")];
  }).filter(([key]) => key));
}

function signSession(user, expires) {
  const payload = Buffer.from(JSON.stringify({ user, expires }), "utf8").toString("base64url");
  const signature = crypto.createHmac("sha256", sessionSecret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function verifySession(cookie) {
  if (!cookie) return false;
  const parts = cookie.split(".");
  if (parts.length !== 2) return false;
  const [payload, signature] = parts;
  let session;
  try {
    session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  } catch {
    return false;
  }
  const { user, expires } = session;
  if (Number(expires) < Date.now()) return false;
  return safeEqual(signature, signSession(user, expires).split(".").pop()) && safeEqual(user, authUser);
}

function setSession(reply, user) {
  const maxAge = 60 * 60 * 12;
  const expires = Date.now() + maxAge * 1000;
  reply.header("Set-Cookie", [
    `eyya_dashboard_session=${encodeURIComponent(signSession(user, expires))}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Lax; Secure`,
  ]);
}

function clearSession(reply) {
  reply.header("Set-Cookie", "eyya_dashboard_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure");
}

app.addHook("onRequest", async (request, reply) => {
  if (!authUser || !authPassword) return;

  const url = request.url.split("?")[0];
  const publicPaths = new Set(["/login", "/login.html", "/login.css", "/login.js", "/api/login"]);
  if (publicPaths.has(url) || url.startsWith("/assets/")) return;

  const cookies = parseCookies(request.headers.cookie);
  if (verifySession(cookies.eyya_dashboard_session)) return;

  if (url.startsWith("/api/")) {
    return reply.code(401).send({ error: "auth_required" });
  }

  const prefix = request.headers["x-forwarded-prefix"] || "";
  return reply.redirect(`${prefix}/login`);
});

app.register(fastifyStatic, {
  root: path.join(__dirname, "public"),
  prefix: "/",
});

app.get("/login", async (_request, reply) => reply.sendFile("login.html"));

app.post("/api/login", async (request, reply) => {
  const { username = "", password = "" } = request.body || {};
  if (safeEqual(username, authUser) && safeEqual(password, authPassword)) {
    setSession(reply, username);
    return { ok: true };
  }
  return reply.code(401).send({ error: "invalid_credentials" });
});

app.post("/api/logout", async (_request, reply) => {
  clearSession(reply);
  return { ok: true };
});

const granularities = new Set(["day", "week", "month"]);
const sqlDatePart = (value) => (granularities.has(value) ? value : "week");
const asNull = (value) => (value === undefined || value === null || value === "" || value === "all" ? null : value);
const toDate = (value, fallback) => (/^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? value : fallback);
const cdekClientDeliverySql = (amount = "coalesce(nullif(actual_delivery_sum, 0), nullif(delivery_sum, 0), nullif(total_sum, 0), 0)") => `
  CASE
    WHEN cdek_date >= date '2026-04-21' AND cdek_date < date '2026-04-24' AND product_sum >= 3000 THEN 0
    WHEN cdek_date >= date '2026-04-24' AND product_sum >= 4500 THEN 0
    ELSE ${amount}
  END
`;
const cdekReturnSqlFor = (alias = "") => {
  const prefix = alias ? `${alias}.` : "";
  return `
  (
    coalesce(${prefix}raw_data->>'is_return', 'false') = 'true'
    OR coalesce(${prefix}raw_data->>'is_client_return', 'false') = 'true'
    OR coalesce(${prefix}raw_data->>'has_reverse_order', 'false') = 'true'
    OR coalesce(${prefix}raw_data->>'is_reverse', 'false') = 'true'
    OR ${prefix}status_code IN ('NOT_DELIVERED', 'RETURNED_TO_RECIPIENT_CITY_WAREHOUSE')
    OR lower(coalesce(${prefix}status_name,'')) LIKE '%возврат%'
    OR lower(coalesce(${prefix}status_name,'')) LIKE '%не вручен%'
    OR lower(coalesce(${prefix}status_name,'')) LIKE '%отмен%'
  )
`;
};
const cdekReturnSql = cdekReturnSqlFor();
const cdekDeliveredSqlFor = (alias = "") => {
  const prefix = alias ? `${alias}.` : "";
  return `(${prefix}status_code = 'DELIVERED' OR ${prefix}delivery_date IS NOT NULL)`;
};
const cdekDeliveredSql = cdekDeliveredSqlFor();
const cdekSuccessfulOrderSql = `(${cdekDeliveredSql} AND NOT ${cdekReturnSql})`;
const cdekPreviousSuccessfulOrdersSql = `
  count(*) FILTER (WHERE ${cdekSuccessfulOrderSql}) OVER (
    PARTITION BY client_key
    ORDER BY cdek_date, tracking_number
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
  )
`;

function parseTrackNumbers(value) {
  const text = Array.isArray(value) ? value.join("\n") : String(value || "");
  return [...new Set((text.match(/10\d{8,9}/g) || []))];
}

function latestCdekStatus(entity) {
  const statuses = Array.isArray(entity.statuses) ? entity.statuses.filter((status) => !status.deleted) : [];
  return statuses
    .slice()
    .sort((a, b) => new Date(b.date_time || 0) - new Date(a.date_time || 0))[0] || {};
}

function flattenCdekItems(entity) {
  const packages = Array.isArray(entity.packages) ? entity.packages : [];
  return packages.flatMap((pack) => (pack.items || []).map((item) => ({
    name: item.name || "",
    sku: item.ware_key || "",
    amount: Number(item.amount || 0),
    cost: Number(item.cost || 0),
  })));
}

function safeCdekRaw(entity) {
  const copy = JSON.parse(JSON.stringify(entity || {}));
  delete copy.developer_key;
  delete copy.calls;
  return copy;
}

function mapCdekEntity(trackingNumber, entity) {
  const status = latestCdekStatus(entity);
  const items = flattenCdekItems(entity);
  const recipient = entity.recipient || {};
  const phone = Array.isArray(recipient.phones) && recipient.phones[0] ? recipient.phones[0].number : null;
  const detail = entity.delivery_detail || {};
  return {
    trackingNumber,
    cdekNumber: entity.cdek_number || trackingNumber,
    cdekOrderNumber: entity.number || null,
    statusCode: status.code || null,
    statusName: status.name || null,
    statusCity: status.city || null,
    statusAt: status.date_time || null,
    plannedDeliveryDate: entity.planned_delivery_date || null,
    deliveryDate: entity.delivery_date || detail.date || null,
    recipientName: recipient.name || recipient.company || detail.recipient_name || null,
    recipientPhone: phone,
    recipientEmail: recipient.email || null,
    toCity: entity.to_location?.city || null,
    toAddress: entity.to_location?.address || null,
    deliverySum: detail.delivery_sum ?? null,
    totalSum: detail.total_sum ?? null,
    itemsCount: items.reduce((total, item) => total + Number(item.amount || 0), 0),
    itemsSummary: items.map((item) => `${item.name}${item.amount ? ` x ${item.amount}` : ""}`).join(" | "),
    items,
    raw: safeCdekRaw(entity),
  };
}

function isBlankOrderNumber(imNumber) {
  return /^[\s\-–—−]*$/u.test(String(imNumber || ""));
}

function normalizePersonName(name) {
  return String(name || "").trim().replace(/\s+/g, " ").toLowerCase();
}

function isBraginNikita(name) {
  return /^брагин никита(?:\s|$)/u.test(normalizePersonName(name));
}

function classifyFreeOrder(imNumber, recipientName = "", orderNumber = imNumber) {
  const value = String(imNumber || "").trim().toLowerCase();
  if (value.startsWith("7d-")) return { type: "Блогеры / бартер", reason: "Номер ИМ начинается с 7d-" };
  if (value.startsWith("dl-")) return { type: "Клиентам без денег", reason: "Номер ИМ начинается с dl-" };
  if (value.startsWith("pr-")) return { type: "PR-рассылки", reason: "Номер ИМ начинается с pr-" };
  if (value.startsWith("mf") || /(^|-)01/i.test(value)) return { type: "Внутренние / не клиенты", reason: "Номер ИМ содержит mf или 01-маркер" };
  if (isBlankOrderNumber(orderNumber) && isBraginNikita(recipientName)) {
    return { type: "Внутренние / не клиенты", reason: "Пустой/прочерк номер заказа и получатель Брагин Никита" };
  }
  return null;
}

async function ensureCdekAdminTables() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bitrix24.cdek_deleted_tracks (
      tracking_number text PRIMARY KEY,
      deleted_at timestamptz DEFAULT now(),
      deleted_by text,
      reason text
    );
    CREATE TABLE IF NOT EXISTS bitrix24.cdek_free_orders (
      tracking_number text PRIMARY KEY,
      im_number text,
      order_created_at timestamptz,
      status_name text,
      recipient_name text,
      source text,
      raw_data jsonb,
      in_client_table boolean DEFAULT false,
      free_order_type text NOT NULL,
      free_order_reason text,
      imported_at timestamptz DEFAULT now(),
      updated_at timestamptz DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS cdek_free_orders_type_idx ON bitrix24.cdek_free_orders(free_order_type);
    CREATE INDEX IF NOT EXISTS cdek_free_orders_created_idx ON bitrix24.cdek_free_orders(order_created_at);
  `);
}

async function getCdekToken() {
  if (!cdekClientId || !cdekClientSecret) {
    throw new Error("CDEK credentials are not configured");
  }
  const response = await fetch(`${cdekApiBase}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: cdekClientId,
      client_secret: cdekClientSecret,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.access_token) {
    throw new Error(`CDEK auth failed: ${response.status} ${body.error || body.message || ""}`);
  }
  return body.access_token;
}

async function fetchCdekShipment(token, trackingNumber) {
  const response = await fetch(`${cdekApiBase}/orders?cdek_number=${encodeURIComponent(trackingNumber)}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.entity) {
    throw new Error(`${response.status} ${body.message || body.error || "CDEK order not found"}`);
  }
  return mapCdekEntity(trackingNumber, body.entity);
}

async function upsertCdekShipment(data) {
  await pool.query(`
    INSERT INTO bitrix24.cdek_shipments (
      tracking_number, cdek_number, cdek_order_number, status_code, status_name, status_city, status_at,
      planned_delivery_date, delivery_date, recipient_name, recipient_phone, recipient_email, to_city, to_address,
      delivery_sum, total_sum, items_count, items_summary, items, raw_data, last_synced_at, sync_error
    ) VALUES (
      $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19::jsonb,$20::jsonb,now(),NULL
    )
    ON CONFLICT (tracking_number) DO UPDATE SET
      cdek_number = excluded.cdek_number,
      cdek_order_number = excluded.cdek_order_number,
      status_code = excluded.status_code,
      status_name = excluded.status_name,
      status_city = excluded.status_city,
      status_at = excluded.status_at,
      planned_delivery_date = excluded.planned_delivery_date,
      delivery_date = excluded.delivery_date,
      recipient_name = excluded.recipient_name,
      recipient_phone = excluded.recipient_phone,
      recipient_email = excluded.recipient_email,
      to_city = excluded.to_city,
      to_address = excluded.to_address,
      delivery_sum = excluded.delivery_sum,
      total_sum = excluded.total_sum,
      items_count = excluded.items_count,
      items_summary = excluded.items_summary,
      items = excluded.items,
      raw_data = excluded.raw_data,
      last_synced_at = now(),
      sync_error = NULL
  `, [
    data.trackingNumber, data.cdekNumber, data.cdekOrderNumber, data.statusCode, data.statusName, data.statusCity,
    data.statusAt, data.plannedDeliveryDate, data.deliveryDate, data.recipientName, data.recipientPhone,
    data.recipientEmail, data.toCity, data.toAddress, data.deliverySum, data.totalSum, data.itemsCount,
    data.itemsSummary, JSON.stringify(data.items), JSON.stringify(data.raw),
  ]);
}

async function placeCdekTrack(data) {
  const free = classifyFreeOrder(data.cdekOrderNumber || data.raw?.number, data.recipientName, data.cdekOrderNumber);
  await pool.query("DELETE FROM bitrix24.cdek_deleted_tracks WHERE tracking_number = $1", [data.trackingNumber]);
  await pool.query(`
    INSERT INTO bitrix24.cdek_tracking_seed (tracking_number, im_number, order_created_at, status_name, is_deleted, recipient_name, source, raw_data, imported_at)
    VALUES ($1, $2, coalesce($3::timestamptz, now()), $4, false, $5, 'dashboard_manual_add', $6::jsonb, now())
    ON CONFLICT (tracking_number) DO UPDATE SET
      im_number = excluded.im_number,
      order_created_at = excluded.order_created_at,
      status_name = excluded.status_name,
      is_deleted = false,
      recipient_name = excluded.recipient_name,
      source = excluded.source,
      raw_data = excluded.raw_data,
      imported_at = now()
  `, [
    data.trackingNumber,
    data.cdekOrderNumber,
    data.statusAt || data.deliveryDate || data.plannedDeliveryDate || null,
    data.statusName,
    data.recipientName,
    JSON.stringify({ source: "dashboard_manual_add", cdek_number: data.cdekNumber, number: data.cdekOrderNumber }),
  ]);

  if (free) {
    await pool.query(`
      INSERT INTO bitrix24.cdek_free_orders (
        tracking_number, im_number, order_created_at, status_name, recipient_name,
        source, raw_data, in_client_table, free_order_type, free_order_reason, imported_at, updated_at
      ) VALUES ($1,$2,$3,$4,$5,'dashboard_manual_add',$6::jsonb,false,$7,$8,now(),now())
      ON CONFLICT (tracking_number) DO UPDATE SET
        im_number = excluded.im_number,
        order_created_at = excluded.order_created_at,
        status_name = excluded.status_name,
        recipient_name = excluded.recipient_name,
        source = excluded.source,
        raw_data = excluded.raw_data,
        free_order_type = excluded.free_order_type,
        free_order_reason = excluded.free_order_reason,
        updated_at = now()
    `, [
      data.trackingNumber,
      data.cdekOrderNumber,
      data.statusAt || data.deliveryDate || data.plannedDeliveryDate || null,
      data.statusName,
      data.recipientName,
      JSON.stringify(data.raw),
      free.type,
      free.reason,
    ]);
  } else {
    await pool.query(
      "DELETE FROM bitrix24.cdek_free_orders WHERE tracking_number = $1 AND coalesce(source,'') <> 'manual_internal_transfer'",
      [data.trackingNumber],
    );
  }
  return { table: free ? "free" : "main", category: free?.type || null };
}

function offsetPlaceholders(sql, offset) {
  return sql.replace(/\$(\d+)/g, (_, n) => `$${Number(n) + offset}`);
}

function buildDealWhere(params, startIndex = 1) {
  const values = [];
  const where = [];
  const add = (condition, value) => {
    values.push(value);
    where.push(condition.replace("?", `$${startIndex + values.length - 1}`));
  };
  add("d.date_create_date >= ?::date", params.start);
  add("d.date_create_date < (?::date + interval '1 day')", params.end);
  if (params.source) add("coalesce(d.source_id, '') = ?", params.source);
  if (params.originator) add("coalesce(d.originator_id, '') = ?", params.originator);
  if (params.utmSource) add("coalesce(d.utm_source, '') = ?", params.utmSource);
  if (params.utmCampaign) add("coalesce(d.utm_campaign, '') = ?", params.utmCampaign);
  if (params.categoryId) add("d.category_id = ?::bigint", params.categoryId);
  if (params.assignedBy) add("d.assigned_by_id = ?::bigint", params.assignedBy);
  return { clause: where.join(" AND "), values };
}

function buildLeadWhere(params, startIndex = 1) {
  const values = [];
  const where = [];
  const add = (condition, value) => {
    values.push(value);
    where.push(condition.replace("?", `$${startIndex + values.length - 1}`));
  };
  add("l.date_create >= ?::date", params.start);
  add("l.date_create < (?::date + interval '1 day')", params.end);
  if (params.source) add("coalesce(l.source_id, '') = ?", params.source);
  if (params.utmSource) add("coalesce(l.utm_source, '') = ?", params.utmSource);
  if (params.utmCampaign) add("coalesce(l.utm_campaign, '') = ?", params.utmCampaign);
  if (params.assignedBy) add("l.assigned_by_id = ?::bigint", params.assignedBy);
  return { clause: where.join(" AND "), values };
}

async function one(sql, values = []) {
  const { rows } = await pool.query(sql, values);
  return rows[0] || {};
}

const opsHealthLogPath = process.env.OPS_HEALTH_LOG || "/var/log/eyya-business-healthcheck.log";

function parseHealthLine(line = "") {
  const fields = {};
  const matches = String(line).trim().matchAll(/([a-zA-Z][a-zA-Z0-9_]*)=([\s\S]*?)(?=\s+[a-zA-Z][a-zA-Z0-9_]*=|$)/g);
  for (const match of matches) {
    fields[match[1]] = match[2].trim();
  }
  if (fields.telegram) {
    try {
      fields.telegram = JSON.parse(fields.telegram);
    } catch {
      fields.telegram = { raw: fields.telegram };
    }
  }
  return fields;
}

function intValue(value) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : 0;
}

function opsLevel(value, warnWhen = (current) => current > 0) {
  return warnWhen(intValue(value)) ? "warn" : "ok";
}

function freshnessStatus(value, maxAgeHours) {
  if (!value || value === "—") return "warn";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "ok";
  return Date.now() - timestamp > maxAgeHours * 60 * 60 * 1000 ? "warn" : "ok";
}

function buildOpsHealth(fields, logMtime) {
  const telegram = fields.telegram && typeof fields.telegram === "object" ? fields.telegram : {};
  const sourceStatus = fields.status || "unknown";
  const telegramWaiting = intValue(telegram.waiting_over_30);
  const telegramFailed = intValue(telegram.failed);
  const telegramFailedRecent = intValue(telegram.failed_recent_24h);
  const telegramFailedOld = intValue(telegram.failed_older_7d);
  const telegramOutboxFailed = intValue(telegram.outbox_failed);
  const telegramOutboxFailedOld = intValue(telegram.outbox_failed_older_7d);
  const telegramInactive = Math.max(intValue(telegram.accounts) - intValue(telegram.active_accounts), 0);
  const cloudErrors = intValue(fields.cloudErrors);
  const cloudNotFound = intValue(fields.cloudNotFound);
  const cloudRetryLater = intValue(fields.cloudRetryLater);
  const n8nRestarts = intValue(fields.n8nRestarts);
  const status = [
    sourceStatus === "error" ? "error" : null,
    sourceStatus === "warn" || telegramWaiting || telegramFailedRecent || telegramOutboxFailed || telegramInactive || cloudErrors || cloudNotFound || cloudRetryLater || n8nRestarts ? "warn" : null,
  ].filter(Boolean)[0] || "ok";
  const dataStatus = freshnessStatus(fields.bitrixDealMax, 3) === "warn" || freshnessStatus(fields.cdekSynced, 6) === "warn" ? "warn" : "ok";

  return {
    ok: status !== "error",
    status,
    updatedAt: logMtime?.toISOString?.() || null,
    notes: fields.notes || "",
    cards: [
      {
        title: "Данные",
        status: sourceStatus === "error" ? "error" : dataStatus,
        primary: `витрина до ${fields.dashboardMax || "—"}`,
        secondary: dataStatus === "ok" ? "Свежесть данных в норме" : "Проверить свежесть Bitrix/CDEK",
        details: [
          { label: "Bitrix", value: fields.bitrixDealMax || "—", status: freshnessStatus(fields.bitrixDealMax, 3) },
          { label: "CDEK sync", value: fields.cdekSynced || "—", status: freshnessStatus(fields.cdekSynced, 6) },
          { label: "За 2 дня", value: fields.dashboardRecent2d || "—", status: "ok" },
        ],
      },
      {
        title: "Telegram",
        status: telegramWaiting || telegramFailedRecent || telegramOutboxFailed || telegramInactive ? "warn" : "ok",
        primary: telegramInactive ? `${telegramInactive} аккаунта требуют входа` : `${telegram.active_accounts ?? "—"}/${telegram.accounts ?? "—"} активны`,
        secondary: telegramWaiting ? `${telegramWaiting} диалогов ждут >30 мин` : "Свежая очередь без превышения",
        details: [
          { label: "Failed 24ч", value: telegramFailedRecent, status: opsLevel(telegramFailedRecent) },
          { label: "Failed история", value: `${telegramFailed} / ${telegramFailedOld} старше 7д`, status: telegramFailedRecent ? "warn" : "ok" },
          { label: "Outbox failed", value: telegramOutboxFailedOld ? `${telegramOutboxFailed} / ${telegramOutboxFailedOld} старше 7д` : telegramOutboxFailed, status: opsLevel(telegramOutboxFailed) },
          { label: "24ч", value: telegram.messages_24h ?? "—", status: "ok" },
        ],
      },
      {
        title: "CloudKassir",
        status: cloudRetryLater || cloudErrors || cloudNotFound ? "warn" : "ok",
        primary: cloudRetryLater ? `${cloudRetryLater} чеков ждут повтора` : "Повторные чеки закрыты",
        secondary: `ФФ завершён: ${fields.cloudFfFinished || "—"}`,
        details: [
          { label: "Errors", value: cloudErrors, status: opsLevel(cloudErrors) },
          { label: "Not found", value: cloudNotFound, status: opsLevel(cloudNotFound) },
          { label: "FF error", value: fields.cloudFfError || "—", status: fields.cloudFfError && fields.cloudFfError !== "no" ? "warn" : "ok" },
        ],
      },
      {
        title: "Автоматизации",
        status: n8nRestarts ? "warn" : "ok",
        primary: n8nRestarts ? `${n8nRestarts} новых рестартов` : "Без новых рестартов",
        secondary: `n8n запущен: ${fields.n8nStarted || "—"}`,
        details: [
          { label: "Healthcheck", value: sourceStatus, status },
          { label: "Лог", value: logMtime ? logMtime.toISOString() : "—", status: "ok" },
        ],
      },
    ],
  };
}

function stableQueryKey(query = {}) {
  return Object.keys(query)
    .sort()
    .filter((key) => key !== "refresh")
    .map((key) => `${key}=${query[key]}`)
    .join("&");
}

function getDashboardCache(key) {
  const cached = dashboardCache.get(key);
  if (!cached) return null;
  if (Date.now() - cached.createdAt > dashboardCacheTtlMs) {
    dashboardCache.delete(key);
    return null;
  }
  return cached;
}

function setDashboardCache(key, payload) {
  dashboardCache.set(key, { createdAt: Date.now(), payload });
  if (dashboardCache.size > 80) {
    const oldestKey = dashboardCache.keys().next().value;
    dashboardCache.delete(oldestKey);
  }
}

async function readOpsHealth() {
  const [content, stats] = await Promise.all([
    fs.readFile(opsHealthLogPath, "utf8"),
    fs.stat(opsHealthLogPath),
  ]);
  const line = content.trim().split(/\r?\n/).filter(Boolean).pop() || "";
  return buildOpsHealth(parseHealthLine(line), stats.mtime);
}

function trackingExpr(alias = "d") {
  return `nullif(${alias}.raw_data->>'tracking_number', '')`;
}

app.get("/api/filters", async () => {
  const [range, sources, utmSources, campaigns, categories, users] = await Promise.all([
    one("SELECT min(date_create_date)::date AS min_date, max(date_create_date)::date AS max_date FROM bitrix24.sales_orders_dashboard_fast"),
    pool.query(`SELECT coalesce(source_id,'') AS source_id, coalesce(originator_id,'') AS originator_id, count(*)::int AS cnt
                FROM bitrix24.sales_orders_dashboard_fast GROUP BY 1,2 ORDER BY cnt DESC LIMIT 80`),
    pool.query("SELECT coalesce(utm_source,'') AS value, count(*)::int AS cnt FROM bitrix24.sales_orders_dashboard_fast GROUP BY 1 ORDER BY cnt DESC LIMIT 80"),
    pool.query("SELECT coalesce(utm_campaign,'') AS value, count(*)::int AS cnt FROM bitrix24.sales_orders_dashboard_fast GROUP BY 1 ORDER BY cnt DESC LIMIT 120"),
    pool.query("SELECT DISTINCT category_id, 'Заказы' AS name FROM bitrix24.sales_orders_dashboard_fast WHERE category_id IS NOT NULL ORDER BY category_id"),
    pool.query("SELECT id, full_name FROM bitrix24.crm_user WHERE active IS DISTINCT FROM false ORDER BY full_name NULLS LAST LIMIT 200"),
  ]);
  return { range, sources: sources.rows, utmSources: utmSources.rows, campaigns: campaigns.rows, categories: categories.rows, users: users.rows };
});

app.get("/api/ops-health", async (request, reply) => {
  try {
    return await readOpsHealth();
  } catch (error) {
    request.log?.warn?.({ err: error }, "ops health read failed");
    return reply.code(503).send({
      ok: false,
      status: "error",
      message: "Не удалось прочитать операционный healthcheck.",
      updatedAt: null,
      cards: [],
    });
  }
});

app.post("/api/cdek/delete-track", async (request, reply) => {
  const tracks = parseTrackNumbers(request.body?.trackingNumber || request.body?.tracks);
  if (!tracks.length) {
    return reply.code(400).send({ error: "track_required" });
  }
  await ensureCdekAdminTables();
  const client = await pool.connect();
  try {
    const results = [];
    for (const trackingNumber of tracks) {
      await client.query("BEGIN");
      try {
        await client.query(`
          INSERT INTO bitrix24.cdek_deleted_tracks (tracking_number, deleted_by, reason, deleted_at)
          VALUES ($1, $2, 'dashboard manual delete', now())
          ON CONFLICT (tracking_number) DO UPDATE SET
            deleted_by = excluded.deleted_by,
            reason = excluded.reason,
            deleted_at = now()
        `, [trackingNumber, authUser || "dashboard"]);
        const deletions = {};
        deletions.free_orders = (await client.query("DELETE FROM bitrix24.cdek_free_orders WHERE tracking_number = $1", [trackingNumber])).rowCount;
        deletions.cdek_shipments = (await client.query("DELETE FROM bitrix24.cdek_shipments WHERE tracking_number = $1 OR cdek_number = $1", [trackingNumber])).rowCount;
        deletions.tracking_seed = (await client.query("DELETE FROM bitrix24.cdek_tracking_seed WHERE tracking_number = $1", [trackingNumber])).rowCount;
        deletions.bitrix_tracking = 0;
        deletions.ff_platform = (await client.query("DELETE FROM bitrix24.ff_cdek_shipments WHERE number_contract = $1 OR delivery_performer_track_number = $1", [trackingNumber])).rowCount;
        await client.query("COMMIT");
        results.push({ trackingNumber, deleted: deletions });
      } catch (error) {
        await client.query("ROLLBACK");
        throw error;
      }
    }
    return { ok: true, results };
  } finally {
    client.release();
  }
});

app.post("/api/cdek/add-track", async (request, reply) => {
  const tracks = parseTrackNumbers(request.body?.trackingNumber || request.body?.tracks);
  if (!tracks.length) {
    return reply.code(400).send({ error: "track_required" });
  }
  await ensureCdekAdminTables();
  try {
    const token = await getCdekToken();
    const results = [];
    for (const trackingNumber of tracks) {
      const shipment = await fetchCdekShipment(token, trackingNumber);
      await upsertCdekShipment(shipment);
      const placement = await placeCdekTrack(shipment);
      results.push({
        trackingNumber,
        imNumber: shipment.cdekOrderNumber,
        status: shipment.statusName,
        table: placement.table,
        category: placement.category,
      });
    }
    return { ok: true, results };
  } catch (error) {
    return reply.code(400).send({ error: "cdek_sync_failed", message: String(error.message || error) });
  }
});

app.get("/api/dashboard", async (request, reply) => {
  const startedAt = Date.now();
  const q = request.query || {};
  const cacheKey = stableQueryKey(q);
  if (q.refresh !== "1" && q.refresh !== "true") {
    const cached = getDashboardCache(cacheKey);
    if (cached) {
      reply.header("X-Dashboard-Cache", "hit");
      reply.header("X-Dashboard-Time-Ms", String(Date.now() - startedAt));
      return {
        ...cached.payload,
        cache: {
          status: "hit",
          ageMs: Date.now() - cached.createdAt,
          ttlMs: dashboardCacheTtlMs,
        },
      };
    }
  }
  reply.header("X-Dashboard-Cache", "miss");
  const mode = q.mode === "marketing" ? "marketing" : "client";
  const includeCdek = q.includeCdek === "1" || q.includeCdek === "true";
  const granularity = sqlDatePart(q.granularity);
  const today = new Date().toISOString().slice(0, 10);
  const defaultStart = new Date(Date.now() - 1000 * 60 * 60 * 24 * 2).toISOString().slice(0, 10);
  const params = {
    start: toDate(q.start, defaultStart),
    end: toDate(q.end, today),
    source: asNull(q.source),
    originator: asNull(q.originator),
    utmSource: asNull(q.utmSource),
    utmCampaign: asNull(q.utmCampaign),
    categoryId: asNull(q.categoryId),
    assignedBy: asNull(q.assignedBy),
  };

  const dealWhere = buildDealWhere(params, 1);
  const leadWhere = buildLeadWhere(params, 1);
  const shiftedLeadWhere = offsetPlaceholders(leadWhere.clause, dealWhere.values.length);
  const values = [...dealWhere.values, ...leadWhere.values];

  const overviewSql = `
    WITH filtered AS (
      SELECT d.*,
        (d.is_repeat_customer = 1) AS is_repeat_client,
        false AS is_based_on_deal,
        d.opportunity AS accounted_opportunity,
        coalesce(nullif(d.contact_email,''), right(regexp_replace(coalesce(d.contact_phone,''), '\\D', '', 'g'), 10), d.contact_name, d.order_number) AS client_key,
        coalesce((d.raw_data->>'customer_total_purchases')::int, 1) AS total_purchases
      FROM bitrix24.sales_orders_dashboard_fast d
      WHERE ${dealWhere.clause}
    ), leads AS (
      SELECT count(*)::int AS leads_count FROM bitrix24.crm_lead_metric_fast l WHERE ${shiftedLeadWhere}
    )
    SELECT
      count(*)::int AS deals_total,
      coalesce(sum(is_won),0)::int AS won_orders,
      coalesce(sum(CASE WHEN is_won=1 THEN accounted_opportunity ELSE 0 END),0)::float AS revenue,
      coalesce(avg(CASE WHEN is_won=1 AND NOT is_based_on_deal THEN opportunity END),0)::float AS avg_check,
      coalesce(sum(CASE WHEN is_won=1 AND is_repeat_client THEN 1 ELSE 0 END),0)::int AS repeat_orders,
      count(DISTINCT client_key) FILTER (WHERE total_purchases >= 2)::int AS repeat_clients_2plus,
      coalesce(sum(CASE WHEN is_won=1 AND is_repeat_client AND NOT is_based_on_deal THEN 1 ELSE 0 END),0)::int AS repeat_accounted_orders,
      coalesce(sum(CASE WHEN is_won=1 AND is_repeat_client THEN accounted_opportunity ELSE 0 END),0)::float AS repeat_revenue,
      coalesce(sum(CASE WHEN is_won=1 AND NOT is_repeat_client THEN 1 ELSE 0 END),0)::int AS new_orders,
      coalesce(sum(CASE WHEN is_won=1 AND NOT is_repeat_client AND NOT is_based_on_deal THEN 1 ELSE 0 END),0)::int AS new_accounted_orders,
      coalesce(sum(CASE WHEN is_won=1 AND NOT is_repeat_client THEN accounted_opportunity ELSE 0 END),0)::float AS new_revenue,
      coalesce(sum(CASE WHEN is_won=1 AND is_based_on_deal THEN 1 ELSE 0 END),0)::int AS based_on_deal_orders,
      coalesce(sum(CASE WHEN is_won=1 AND is_based_on_deal THEN opportunity ELSE 0 END),0)::float AS excluded_based_on_deal_amount,
      (SELECT leads_count FROM leads) AS leads_count
    FROM filtered`;

  const seriesSql = `
    WITH deals AS (
      SELECT date_trunc('${granularity}', d.date_create_date)::date AS period,
        d.*,
        (d.is_repeat_customer = 1) AS is_repeat_client,
        false AS is_based_on_deal,
        d.opportunity AS accounted_opportunity,
        coalesce(nullif(d.contact_email,''), right(regexp_replace(coalesce(d.contact_phone,''), '\\D', '', 'g'), 10), d.contact_name, d.order_number) AS client_key,
        coalesce((d.raw_data->>'customer_total_purchases')::int, 1) AS total_purchases
      FROM bitrix24.sales_orders_dashboard_fast d
      WHERE ${dealWhere.clause}
    ), leads AS (
      SELECT date_trunc('${granularity}', l.date_create)::date AS period, count(*)::int AS leads_count
      FROM bitrix24.crm_lead_metric_fast l WHERE ${shiftedLeadWhere}
      GROUP BY 1
    )
    SELECT d.period,
      count(*)::int AS deals_total,
      coalesce(sum(d.is_won),0)::int AS won_orders,
      coalesce(sum(CASE WHEN d.is_won=1 THEN d.accounted_opportunity ELSE 0 END),0)::float AS revenue,
      coalesce(sum(CASE WHEN d.is_won=1 AND d.is_repeat_client THEN 1 ELSE 0 END),0)::int AS repeat_orders,
      count(DISTINCT d.client_key) FILTER (WHERE d.total_purchases >= 2)::int AS repeat_clients_2plus,
      coalesce(sum(CASE WHEN d.is_won=1 AND d.is_repeat_client AND NOT d.is_based_on_deal THEN 1 ELSE 0 END),0)::int AS repeat_accounted_orders,
      coalesce(sum(CASE WHEN d.is_won=1 AND d.is_repeat_client THEN d.accounted_opportunity ELSE 0 END),0)::float AS repeat_revenue,
      coalesce(sum(CASE WHEN d.is_won=1 AND NOT d.is_repeat_client THEN 1 ELSE 0 END),0)::int AS new_orders,
      coalesce(sum(CASE WHEN d.is_won=1 AND NOT d.is_repeat_client AND NOT d.is_based_on_deal THEN 1 ELSE 0 END),0)::int AS new_accounted_orders,
      coalesce(sum(CASE WHEN d.is_won=1 AND NOT d.is_repeat_client THEN d.accounted_opportunity ELSE 0 END),0)::float AS new_revenue,
      coalesce(avg(CASE WHEN d.is_won=1 AND NOT d.is_based_on_deal THEN d.opportunity END),0)::float AS avg_check,
      coalesce(max(l.leads_count),0)::int AS leads_count
    FROM deals d
    LEFT JOIN leads l ON l.period = d.period
    GROUP BY d.period
    ORDER BY d.period`;

  const sourceSql = `
    SELECT coalesce(nullif(d.source_id,''),'Не указан') AS source,
      coalesce(nullif(d.originator_id,''),'') AS originator,
      count(*)::int AS deals,
      coalesce(sum(d.is_won),0)::int AS won,
      coalesce(sum(CASE WHEN d.is_won=1 THEN d.opportunity ELSE 0 END),0)::float AS revenue
    FROM bitrix24.sales_orders_dashboard_fast d
    WHERE ${dealWhere.clause}
    GROUP BY 1,2
    ORDER BY revenue DESC, deals DESC
    LIMIT 12`;

  const funnelSql = `
    SELECT d.stage_id, coalesce(d.stage_name, d.stage_id) AS stage_name, d.category_id,
      count(*)::int AS deals,
      coalesce(sum(d.opportunity),0)::float AS amount,
      coalesce(sum(d.is_won),0)::int AS won
    FROM bitrix24.sales_orders_dashboard_fast d
    WHERE ${dealWhere.clause}
    GROUP BY d.category_id, d.stage_id, d.stage_name
    ORDER BY d.category_id, deals DESC`;

  const managersSql = `
    SELECT coalesce(d.assigned_by_name, 'Не назначен') AS manager,
      count(*)::int AS deals,
      coalesce(sum(d.is_won),0)::int AS won,
      coalesce(sum(CASE WHEN d.is_won=1 THEN d.opportunity ELSE 0 END),0)::float AS revenue,
      round(100.0 * sum(d.is_won) / nullif(count(*),0), 1)::float AS win_rate
    FROM bitrix24.sales_orders_dashboard_fast d
    WHERE ${dealWhere.clause}
    GROUP BY 1
    ORDER BY revenue DESC, won DESC
    LIMIT 10`;

  const retentionSql = `
    SELECT date_trunc('${granularity}', d.date_create_date)::date AS period,
      count(*) FILTER (WHERE d.is_won=1 AND coalesce(d.is_repeat_customer,0)=0)::int AS new_clients,
      count(*) FILTER (WHERE d.is_won=1 AND coalesce(d.is_repeat_customer,0)=1)::int AS repeat_30,
      count(*) FILTER (WHERE d.is_won=1 AND coalesce(d.is_repeat_customer,0)=1)::int AS repeat_60
    FROM bitrix24.sales_orders_dashboard_fast d
    WHERE d.date_create_date >= $1::date AND d.date_create_date < ($2::date + interval '1 day')
    GROUP BY 1
    ORDER BY 1`;

  const supportSql = `
    SELECT coalesce(stage_name, stage_id) AS stage_name, stage_id, count(*)::int AS cnt
    FROM bitrix24.crm_deal_metric_fast d
    WHERE d.category_id = 4 AND d.date_create >= $1::date AND d.date_create < ($2::date + interval '1 day')
    GROUP BY 1,2
    ORDER BY cnt DESC`;

  const supportSeriesSql = `
    SELECT date_trunc('${granularity}', d.date_create)::date AS period,
      count(*) FILTER (WHERE d.stage_id = 'C4:WON' OR lower(coalesce(d.stage_name,'')) LIKE '%повтор%')::int AS repeat_purchase,
      count(*) FILTER (WHERE lower(coalesce(d.stage_name,'')) LIKE '%жалоб%')::int AS complaints,
      count(*) FILTER (WHERE lower(coalesce(d.stage_name,'')) LIKE '%нет контакт%')::int AS no_contact
    FROM bitrix24.crm_deal_metric_fast d
    WHERE d.category_id = 4 AND d.date_create >= $1::date AND d.date_create < ($2::date + interval '1 day')
    GROUP BY 1
    ORDER BY 1`;

  const linkedDealsSql = `
    SELECT
      count(*) FILTER (WHERE d.category_id = 4)::int AS support_deals,
      count(*) FILTER (WHERE d.category_id = 4 AND r.confidence >= 80)::int AS support_linked_to_order,
      count(*) FILTER (WHERE d.category_id = 4 AND r.relationship_source = 'order_number')::int AS support_linked_by_order_number,
      count(*) FILTER (WHERE d.category_id = 4 AND r.relationship_source = 'source_title_same_contact')::int AS support_linked_by_source_title,
      count(*) FILTER (WHERE d.category_id = 4 AND r.confidence < 80)::int AS support_probable_links,
      count(*) FILTER (WHERE d.category_id = 0)::int AS general_deals,
      count(*) FILTER (WHERE d.category_id = 0 AND r.confidence >= 80)::int AS general_linked_to_order
    FROM bitrix24.crm_deal_metric_fast d
    LEFT JOIN bitrix24.crm_deal_relationships_fast r
      ON r.child_deal_id = d.id
    WHERE d.date_create >= $1::date AND d.date_create < ($2::date + interval '1 day')`;

  const firstResponseSql = `
    WITH first_activity AS (
      SELECT l.id, min(a.created_at) AS first_activity_at
      FROM bitrix24.crm_lead_metric_fast l
      JOIN bitrix24.crm_activity_fast a ON a.owner_id = l.id AND (a.owner_type_id = 1 OR lower(coalesce(a.owner_type,'')) LIKE '%lead%')
      WHERE ${leadWhere.clause}
      GROUP BY l.id
    )
    SELECT round(avg(extract(epoch from (fa.first_activity_at - l.crm_date_create))/60.0),1)::float AS avg_minutes
    FROM first_activity fa
    JOIN bitrix24.crm_lead_metric_fast l ON l.id = fa.id
    WHERE fa.first_activity_at >= l.crm_date_create AND fa.first_activity_at <= l.crm_date_create + interval '14 days'`;

  const cdekSummarySql = `
    WITH cdek AS (
      SELECT cs.*,
        fo.free_order_type,
        fo.free_order_reason,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date,
        coalesce(
          nullif(CASE
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 11 AND left(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g'), 1) = '8'
              THEN '7' || substring(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g') from 2)
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 10
              THEN '7' || regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
            ELSE regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
          END, ''),
          nullif(lower(cs.recipient_email), ''),
          cs.cdek_order_number,
          cs.tracking_number
        ) AS client_key,
        coalesce(it.product_sum, 0) AS product_sum,
        coalesce(nullif(cs.raw_data->'delivery_recipient_cost'->>'value', '')::numeric, nullif(cs.raw_data->'delivery_detail'->>'payment_sum', '')::numeric, it.item_delivery_sum, 0) AS actual_delivery_sum
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT
          min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      LEFT JOIN LATERAL (
        SELECT
          coalesce(sum((i.value->>'cost')::numeric * coalesce((i.value->>'amount')::numeric, 1)), 0) AS product_sum,
          coalesce(sum(coalesce((i.value->>'delivery_amount')::numeric, 0)), 0) AS item_delivery_sum
        FROM jsonb_array_elements(coalesce(cs.items, '[]'::jsonb)) AS i(value)
      ) it ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    ), typed AS (
      SELECT *,
        ${cdekClientDeliverySql()} AS charged_delivery_sum,
        product_sum + ${cdekClientDeliverySql()} AS calculated_total,
        ${cdekReturnSql} AS is_return_or_cancel,
        ${cdekDeliveredSql} AS is_delivered,
        ${cdekPreviousSuccessfulOrdersSql} AS previous_successful_orders
      FROM cdek
      WHERE cdek_date IS NOT NULL
    )
    SELECT
      count(*)::int AS orders_total,
      count(tracking_number)::int AS with_tracking,
      count(status_code)::int AS synced,
      count(*) FILTER (WHERE sync_error IS NOT NULL)::int AS sync_errors,
      count(*) FILTER (WHERE status_code = 'DELIVERED' OR delivery_date IS NOT NULL)::int AS delivered,
      count(*) FILTER (WHERE tracking_number IS NOT NULL AND status_code IS NULL AND sync_error IS NULL)::int AS not_synced,
      count(*) FILTER (WHERE status_code IS NOT NULL AND status_code <> 'DELIVERED' AND delivery_date IS NULL)::int AS in_progress,
      coalesce(sum(calculated_total),0)::float AS order_revenue,
      coalesce(sum(charged_delivery_sum),0)::float AS delivery_sum,
      coalesce(sum(product_sum),0)::float AS cdek_total_sum,
      max(last_synced_at) AS last_synced_at
    FROM typed
    WHERE cdek_date >= $1::date AND cdek_date < ($2::date + interval '1 day')`;

  const cdekStatusesSql = `
    WITH cdek AS (
      SELECT cs.*,
        fo.free_order_type,
        fo.free_order_reason,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date,
        coalesce(it.product_sum, 0) AS product_sum,
        coalesce(nullif(cs.raw_data->'delivery_recipient_cost'->>'value', '')::numeric, nullif(cs.raw_data->'delivery_detail'->>'payment_sum', '')::numeric, it.item_delivery_sum, 0) AS actual_delivery_sum
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      LEFT JOIN LATERAL (
        SELECT
          coalesce(sum((i.value->>'cost')::numeric * coalesce((i.value->>'amount')::numeric, 1)), 0) AS product_sum,
          coalesce(sum(coalesce((i.value->>'delivery_amount')::numeric, 0)), 0) AS item_delivery_sum
        FROM jsonb_array_elements(coalesce(cs.items, '[]'::jsonb)) AS i(value)
      ) it ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    )
    SELECT coalesce(status_name, 'Не синхронизировано') AS status,
      coalesce(status_code, '') AS status_code,
      count(*)::int AS orders,
      coalesce(sum(${cdekClientDeliverySql()}),0)::float AS delivery_sum,
      coalesce(sum(total_sum),0)::float AS cdek_carrier_cost
    FROM cdek
    WHERE cdek_date >= $1::date AND cdek_date < ($2::date + interval '1 day')
    GROUP BY 1,2
    ORDER BY orders DESC, status`;

  const cdekRowsSql = `
    WITH cdek AS (
      SELECT cs.*,
        fo.free_order_type,
        fo.free_order_reason,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date,
        coalesce(
          nullif(CASE
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 11 AND left(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g'), 1) = '8'
              THEN '7' || substring(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g') from 2)
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 10
              THEN '7' || regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
            ELSE regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
          END, ''),
          nullif(lower(cs.recipient_email), ''),
          cs.cdek_order_number,
          cs.tracking_number
        ) AS client_key,
        coalesce(it.product_sum, 0) AS product_sum,
        coalesce(nullif(cs.raw_data->'delivery_recipient_cost'->>'value', '')::numeric, nullif(cs.raw_data->'delivery_detail'->>'payment_sum', '')::numeric, it.item_delivery_sum, 0) AS actual_delivery_sum,
        coalesce(it.items_summary_priced, cs.items_summary, '') AS items_summary_priced
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      LEFT JOIN LATERAL (
        SELECT
          coalesce(sum((i.value->>'cost')::numeric * coalesce((i.value->>'amount')::numeric, 1)), 0) AS product_sum,
          coalesce(sum(coalesce((i.value->>'delivery_amount')::numeric, 0)), 0) AS item_delivery_sum,
          string_agg(
            coalesce(i.value->>'name', 'Товар') || ' x ' || trim(to_char(coalesce((i.value->>'amount')::numeric, 1), 'FM999999990.##')) ||
            ' = ' || trim(to_char((i.value->>'cost')::numeric * coalesce((i.value->>'amount')::numeric, 1), 'FM999999990.00')) || ' ₽',
            ' | ' ORDER BY i.ordinality
          ) AS items_summary_priced
        FROM jsonb_array_elements(coalesce(cs.items, '[]'::jsonb)) WITH ORDINALITY AS i(value, ordinality)
      ) it ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    ), typed AS (
      SELECT *,
        ${cdekClientDeliverySql()} AS charged_delivery_sum,
        product_sum + ${cdekClientDeliverySql()} AS calculated_total,
        ${cdekReturnSql} AS is_return_or_cancel,
        ${cdekDeliveredSql} AS is_delivered,
        ${cdekPreviousSuccessfulOrdersSql} AS previous_successful_orders
      FROM cdek
      WHERE cdek_date IS NOT NULL
    )
    SELECT
      cdek_date AS date_create_date,
      cdek_order_number AS order_number,
      recipient_name AS client_name,
      CASE WHEN previous_successful_orders > 0 THEN 'Повторный' ELSE 'Первичный' END AS order_type,
      (previous_successful_orders > 0) AS is_repeat_order,
      calculated_total::float AS amount,
      product_sum::float AS product_sum,
      charged_delivery_sum::float AS charged_delivery_sum,
      tracking_number,
      coalesce(status_name, 'Не синхронизировано') AS status_name,
      status_code,
      status_at,
      to_city,
      to_address,
      planned_delivery_date,
      delivery_date,
      items_summary_priced || ' | Доставка = ' || trim(to_char(charged_delivery_sum, 'FM999999990.00')) || ' ₽' AS items_summary,
      coalesce(items_count,0)::int AS items_count,
      delivery_sum::float,
      total_sum::float,
      is_return_or_cancel,
      is_delivered,
      last_synced_at,
      sync_error,
      free_order_type,
      free_order_reason
    FROM typed
    WHERE cdek_date >= $1::date AND cdek_date < ($2::date + interval '1 day')
    ORDER BY cdek_date DESC, cdek_order_number DESC, tracking_number DESC`;

  const cdekFreeRowsSql = cdekRowsSql
    .replace(/WHERE cs\.raw_data IS NOT NULL\s+AND fo\.tracking_number IS NULL/g, "WHERE fo.tracking_number IS NOT NULL");

  const cdekItemsSql = `
    WITH cdek AS (
      SELECT cs.*,
        fo.free_order_type,
        fo.free_order_reason,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    ), items AS (
      SELECT
        coalesce(nullif(i.value->>'sku', ''), nullif(i.value->>'ware_key', ''), '') AS sku_raw,
        regexp_replace(
          regexp_replace(
            CASE
              WHEN lower(coalesce(i.value->>'sku', i.value->>'ware_key', '')) = 'ey_09refillbox' THEN 'ey_09_refillbox'
              ELSE lower(coalesce(i.value->>'sku', i.value->>'ware_key', ''))
            END,
            '(_old|_free)$', ''
          ),
          '[^a-z0-9]+', '_', 'g'
        ) AS sku_key,
        coalesce(i.value->>'name', 'Товар') AS item_name,
        coalesce((i.value->>'amount')::numeric, 1) AS qty,
        coalesce((i.value->>'cost')::numeric, 0) AS unit_price
      FROM cdek cs
      CROSS JOIN LATERAL jsonb_array_elements(coalesce(cs.items, '[]'::jsonb)) AS i(value)
      WHERE cs.cdek_date >= $1::date AND cs.cdek_date < ($2::date + interval '1 day')
    ), names AS (
      SELECT DISTINCT ON (sku_key)
        sku_key,
        sku_raw,
        item_name
      FROM (
        SELECT sku_key, sku_raw, item_name, sum(qty) AS qty
        FROM items
        GROUP BY sku_key, sku_raw, item_name
      ) n
      ORDER BY sku_key, qty DESC, length(item_name), item_name
    )
    SELECT
      upper(n.sku_raw) AS sku,
      n.item_name,
      sum(qty)::float AS quantity,
      min(unit_price)::float AS min_price,
      max(unit_price)::float AS max_price,
      sum(qty * unit_price)::float AS total_sum
    FROM items
    JOIN names n ON n.sku_key = items.sku_key
    GROUP BY items.sku_key, n.sku_raw, n.item_name
    ORDER BY total_sum DESC, quantity DESC, item_name
    LIMIT 100`;

  const cdekAnalyticsSql = `
    WITH cdek AS (
      SELECT cs.*,
        fo.free_order_type,
        fo.free_order_reason,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date,
        coalesce(
          nullif(CASE
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 11 AND left(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g'), 1) = '8'
              THEN '7' || substring(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g') from 2)
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 10
              THEN '7' || regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
            ELSE regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
          END, ''),
          nullif(lower(cs.recipient_email), ''),
          cs.cdek_order_number,
          cs.tracking_number
        ) AS client_key,
        coalesce(it.product_sum, 0) AS product_sum,
        coalesce(nullif(cs.raw_data->'delivery_recipient_cost'->>'value', '')::numeric, nullif(cs.raw_data->'delivery_detail'->>'payment_sum', '')::numeric, it.item_delivery_sum, 0) AS actual_delivery_sum
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      LEFT JOIN LATERAL (
        SELECT
          coalesce(sum((i.value->>'cost')::numeric * coalesce((i.value->>'amount')::numeric, 1)), 0) AS product_sum,
          coalesce(sum(coalesce((i.value->>'delivery_amount')::numeric, 0)), 0) AS item_delivery_sum
        FROM jsonb_array_elements(coalesce(cs.items, '[]'::jsonb)) AS i(value)
      ) it ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    ), typed AS (
      SELECT *,
        ${cdekClientDeliverySql()} AS charged_delivery_sum,
        product_sum + ${cdekClientDeliverySql()} AS calculated_total,
        ${cdekReturnSql} AS is_return_or_cancel,
        ${cdekDeliveredSql} AS is_delivered,
        ${cdekPreviousSuccessfulOrdersSql} AS previous_successful_orders
      FROM cdek
      WHERE cdek_date IS NOT NULL
    ), scoped AS (
      SELECT *
      FROM typed
      WHERE cdek_date >= $1::date AND cdek_date < ($2::date + interval '1 day')
    )
    SELECT
      count(*)::int AS orders_total,
      count(*) FILTER (WHERE previous_successful_orders > 0)::int AS repeat_orders,
      coalesce(sum(calculated_total) FILTER (WHERE previous_successful_orders > 0), 0)::float AS repeat_revenue,
      round(100.0 * count(*) FILTER (WHERE previous_successful_orders > 0) / nullif(count(*), 0), 1)::float AS repeat_rate,
      count(*) FILTER (WHERE previous_successful_orders > 0 AND is_return_or_cancel)::int AS repeat_returned_orders,
      coalesce(sum(calculated_total) FILTER (WHERE previous_successful_orders > 0 AND is_return_or_cancel), 0)::float AS repeat_returned_revenue,
      round(100.0 * count(*) FILTER (WHERE previous_successful_orders > 0 AND is_return_or_cancel) / nullif(count(*) FILTER (WHERE previous_successful_orders > 0), 0), 1)::float AS repeat_return_rate,
      coalesce(sum(product_sum), 0)::float AS product_revenue,
      coalesce(sum(charged_delivery_sum), 0)::float AS charged_delivery_sum,
      coalesce(sum(total_sum), 0)::float AS cdek_carrier_cost,
      coalesce(sum(calculated_total), 0)::float AS calculated_total,
      count(*) FILTER (WHERE is_delivered)::int AS delivered_orders,
      coalesce(sum(calculated_total) FILTER (WHERE is_delivered), 0)::float AS delivered_revenue,
      count(*) FILTER (WHERE NOT is_delivered AND NOT is_return_or_cancel)::int AS in_progress_orders,
      coalesce(sum(calculated_total) FILTER (WHERE NOT is_delivered AND NOT is_return_or_cancel), 0)::float AS in_progress_revenue,
      count(*) FILTER (WHERE is_return_or_cancel)::int AS returned_orders,
      coalesce(sum(calculated_total) FILTER (WHERE is_return_or_cancel), 0)::float AS returned_revenue,
      round(100.0 * count(*) FILTER (WHERE is_return_or_cancel) / nullif(count(*), 0), 1)::float AS return_rate,
      count(*) FILTER (WHERE coalesce(raw_data->>'is_return', 'false') = 'true' OR coalesce(raw_data->>'is_client_return', 'false') = 'true' OR coalesce(raw_data->>'has_reverse_order', 'false') = 'true' OR coalesce(raw_data->>'is_reverse', 'false') = 'true')::int AS cdek_return_flag_orders,
      max(last_synced_at) AS last_synced_at
    FROM scoped`;

  const cdekRetentionSql = `
    WITH cdek AS (
      SELECT cs.tracking_number,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz)::date AS cdek_date,
        coalesce(
          nullif(CASE
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 11 AND left(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g'), 1) = '8'
              THEN '7' || substring(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g') from 2)
            WHEN length(regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')) = 10
              THEN '7' || regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
            ELSE regexp_replace(coalesce(cs.recipient_phone,''), '\\D', '', 'g')
          END, ''),
          nullif(lower(cs.recipient_email), ''),
          cs.cdek_order_number,
          cs.tracking_number
        ) AS client_key,
        ${cdekReturnSqlFor("cs")} AS is_return_or_cancel,
        ${cdekDeliveredSqlFor("cs")} AS is_delivered
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN bitrix24.cdek_free_orders fo ON fo.tracking_number = cs.tracking_number
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      WHERE cs.raw_data IS NOT NULL
        AND fo.tracking_number IS NULL
    ), ordered AS (
      SELECT *,
        row_number() OVER (PARTITION BY client_key ORDER BY cdek_date, tracking_number) AS order_no,
        lead(cdek_date) OVER (PARTITION BY client_key ORDER BY cdek_date, tracking_number) AS next_order_date
      FROM cdek
      WHERE cdek_date IS NOT NULL
        AND is_delivered
        AND NOT is_return_or_cancel
    ), cohort AS (
      SELECT *
      FROM ordered
      WHERE order_no = 1
        AND cdek_date >= $1::date
        AND cdek_date < ($2::date + interval '1 day')
    )
    SELECT
      count(*)::int AS cohort_clients,
      count(*) FILTER (WHERE next_order_date <= cdek_date + interval '30 days')::int AS repeat_30_clients,
      count(*) FILTER (WHERE next_order_date <= cdek_date + interval '60 days')::int AS repeat_60_clients,
      count(*) FILTER (WHERE next_order_date <= cdek_date + interval '90 days')::int AS repeat_90_clients,
      round(100.0 * count(*) FILTER (WHERE next_order_date <= cdek_date + interval '30 days') / nullif(count(*), 0), 1)::float AS repeat_30_rate,
      round(100.0 * count(*) FILTER (WHERE next_order_date <= cdek_date + interval '60 days') / nullif(count(*), 0), 1)::float AS repeat_60_rate,
      round(100.0 * count(*) FILTER (WHERE next_order_date <= cdek_date + interval '90 days') / nullif(count(*), 0), 1)::float AS repeat_90_rate
    FROM cohort`;

  const [overview, series, sources, funnel, managers, retention, support, supportSeries, linkedDeals, firstResponse] = await Promise.all([
    pool.query(overviewSql, values),
    pool.query(seriesSql, values),
    pool.query(sourceSql, dealWhere.values),
    pool.query(funnelSql, dealWhere.values),
    pool.query(managersSql, dealWhere.values),
    pool.query(retentionSql, [params.start, params.end]),
    pool.query(supportSql, [params.start, params.end]),
    pool.query(supportSeriesSql, [params.start, params.end]),
    pool.query(linkedDealsSql, [params.start, params.end]),
    pool.query(firstResponseSql, leadWhere.values),
  ]);

  const cdek = {
    summary: {},
    statuses: [],
    rows: [],
    freeRows: [],
    items: [],
    analytics: {},
    retention: {},
  };

  if (includeCdek) {
    const [cdekSummary, cdekStatuses, cdekRows, cdekFreeRows, cdekItems, cdekAnalytics, cdekRetention] = await Promise.all([
      pool.query(cdekSummarySql, [params.start, params.end]),
      pool.query(cdekStatusesSql, [params.start, params.end]),
      pool.query(cdekRowsSql, [params.start, params.end]),
      pool.query(cdekFreeRowsSql, [params.start, params.end]),
      pool.query(cdekItemsSql, [params.start, params.end]),
      pool.query(cdekAnalyticsSql, [params.start, params.end]),
      pool.query(cdekRetentionSql, [params.start, params.end]),
    ]);
    cdek.summary = cdekSummary.rows[0] || {};
    cdek.statuses = cdekStatuses.rows;
    cdek.rows = cdekRows.rows;
    cdek.freeRows = cdekFreeRows.rows;
    cdek.items = cdekItems.rows;
    cdek.analytics = cdekAnalytics.rows[0] || {};
    cdek.retention = cdekRetention.rows[0] || {};
  }

  const row = overview.rows[0] || {};
  row.lead_to_order_conversion = row.leads_count ? Number((((row.won_orders || 0) / row.leads_count) * 100).toFixed(1)) : 0;
  row.repeat_avg_check = row.repeat_accounted_orders ? Number((row.repeat_revenue / row.repeat_accounted_orders).toFixed(0)) : 0;
  row.first_response_minutes = firstResponse.rows[0]?.avg_minutes || null;

  const payload = {
    mode,
    filters: params,
    overview: row,
    series: series.rows,
    sources: sources.rows,
    funnel: funnel.rows,
    managers: managers.rows,
    retention: retention.rows,
    support: support.rows,
    supportSeries: supportSeries.rows,
    linkedDeals: linkedDeals.rows[0] || {},
    cdek,
    generatedAt: new Date().toISOString(),
    cache: {
      status: "miss",
      ageMs: 0,
      ttlMs: dashboardCacheTtlMs,
    },
  };
  setDashboardCache(cacheKey, payload);
  reply.header("X-Dashboard-Time-Ms", String(Date.now() - startedAt));
  return payload;
});

app.get("/health", async () => ({ ok: true }));

const port = Number(process.env.PORT || 8010);
const host = process.env.HOST || "127.0.0.1";
app.listen({ host, port }).catch((err) => {
  app.log.error(err);
  process.exit(1);
});
