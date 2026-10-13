const { Pool } = require("pg");

const apiBase = process.env.CDEK_API_BASE || "https://api.cdek.ru/v2";
const clientId = process.env.CDEK_CLIENT_ID;
const clientSecret = process.env.CDEK_CLIENT_SECRET;
const limit = Number(process.env.CDEK_SYNC_LIMIT || process.argv[2] || 0);
const staleHours = Number(process.env.CDEK_SYNC_STALE_HOURS || 12);
const concurrency = Math.max(1, Math.min(8, Number(process.env.CDEK_SYNC_CONCURRENCY || 3)));

const pool = new Pool({
  host: process.env.PGHOST,
  port: Number(process.env.PGPORT || 5432),
  database: process.env.PGDATABASE,
  user: process.env.PGUSER,
  password: process.env.PGPASSWORD,
  max: concurrency + 2,
});

function assertConfig() {
  if (!clientId || !clientSecret) {
    throw new Error("CDEK_CLIENT_ID and CDEK_CLIENT_SECRET are required");
  }
}

async function ensureTable() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bitrix24.cdek_tracking_seed (
      tracking_number text PRIMARY KEY,
      im_number text,
      order_created_at timestamptz,
      status_name text,
      is_deleted boolean,
      recipient_name text,
      recipient_city text,
      pickup_point text,
      source text NOT NULL DEFAULT 'cdek_journal',
      raw_data jsonb,
      imported_at timestamptz DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS cdek_tracking_seed_order_created_idx ON bitrix24.cdek_tracking_seed(order_created_at);

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

    CREATE TABLE IF NOT EXISTS bitrix24.cdek_shipments (
      tracking_number text PRIMARY KEY,
      cdek_number text,
      cdek_order_number text,
      status_code text,
      status_name text,
      status_city text,
      status_at timestamptz,
      planned_delivery_date date,
      delivery_date date,
      recipient_name text,
      recipient_phone text,
      recipient_email text,
      to_city text,
      to_address text,
      delivery_sum numeric,
      total_sum numeric,
      items_count integer,
      items_summary text,
      items jsonb,
      raw_data jsonb,
      last_synced_at timestamptz DEFAULT now(),
      sync_error text
    );
    CREATE INDEX IF NOT EXISTS cdek_shipments_status_idx ON bitrix24.cdek_shipments(status_code);
    CREATE INDEX IF NOT EXISTS cdek_shipments_delivery_date_idx ON bitrix24.cdek_shipments(delivery_date);
  `);
}

async function getToken() {
  const response = await fetch(`${apiBase}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: clientId,
      client_secret: clientSecret,
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.access_token) {
    throw new Error(`CDEK auth failed: ${response.status} ${body.error || body.message || ""}`);
  }
  return body.access_token;
}

function latestStatus(entity) {
  const statuses = Array.isArray(entity.statuses) ? entity.statuses.filter((s) => !s.deleted) : [];
  return statuses
    .slice()
    .sort((a, b) => new Date(b.date_time || 0) - new Date(a.date_time || 0))[0] || {};
}

function flattenItems(entity) {
  const packages = Array.isArray(entity.packages) ? entity.packages : [];
  return packages.flatMap((pack) => (pack.items || []).map((item) => ({
    name: item.name || "",
    sku: item.ware_key || "",
    amount: Number(item.amount || 0),
    cost: Number(item.cost || 0),
  })));
}

function safeRaw(entity) {
  const copy = JSON.parse(JSON.stringify(entity || {}));
  delete copy.developer_key;
  delete copy.calls;
  return copy;
}

function mapEntity(trackingNumber, entity) {
  const status = latestStatus(entity);
  const items = flattenItems(entity);
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
    raw: safeRaw(entity),
  };
}

async function fetchShipment(token, trackingNumber) {
  const url = `${apiBase}/orders?cdek_number=${encodeURIComponent(trackingNumber)}`;
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.entity) {
    throw new Error(`${response.status} ${body.message || body.error || "CDEK order not found"}`);
  }
  return mapEntity(trackingNumber, body.entity);
}

async function fetchShipmentByImNumber(token, orderNumber) {
  const url = `${apiBase}/orders?im_number=${encodeURIComponent(orderNumber)}`;
  const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || !body.entity?.cdek_number) {
    throw new Error(`${response.status} ${body.requests?.[0]?.errors?.[0]?.message || body.message || body.error || "CDEK order not found"}`);
  }
  return mapEntity(body.entity.cdek_number, body.entity);
}

async function upsertShipment(data) {
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

async function saveError(trackingNumber, error) {
  await pool.query(`
    INSERT INTO bitrix24.cdek_shipments (tracking_number, last_synced_at, sync_error)
    VALUES ($1, now(), $2)
    ON CONFLICT (tracking_number) DO UPDATE SET last_synced_at = now(), sync_error = excluded.sync_error
  `, [trackingNumber, String(error.message || error).slice(0, 500)]);
}

async function getTracks() {
  const limitClause = limit > 0 ? `LIMIT ${limit}` : "";
  const { rows } = await pool.query(`
    WITH tracks AS (
      SELECT DISTINCT nullif(tracking_number, '') AS tracking_number
      FROM bitrix24.cdek_tracking_seed
      WHERE nullif(tracking_number, '') IS NOT NULL
        AND coalesce(is_deleted, false) = false
      UNION
      SELECT DISTINCT nullif(tracking_number, '') AS tracking_number
      FROM bitrix24.bitrix_tracking_numbers
      WHERE nullif(tracking_number, '') IS NOT NULL
      UNION
      SELECT DISTINCT nullif(number_contract, '') AS tracking_number
      FROM bitrix24.ff_cdek_shipments
      WHERE nullif(number_contract, '') IS NOT NULL
      UNION
      SELECT DISTINCT nullif(delivery_performer_track_number, '') AS tracking_number
      FROM bitrix24.ff_cdek_shipments
      WHERE nullif(delivery_performer_track_number, '') IS NOT NULL
    )
    SELECT t.tracking_number
    FROM tracks t
    LEFT JOIN bitrix24.cdek_shipments cs ON cs.tracking_number = t.tracking_number
    LEFT JOIN bitrix24.cdek_deleted_tracks dt ON dt.tracking_number = t.tracking_number
    WHERE (
         cs.tracking_number IS NULL
      OR cs.last_synced_at < now() - ($1::int * interval '1 hour')
      OR (cs.sync_error IS NOT NULL AND cs.last_synced_at < now() - interval '2 hours')
    )
      AND dt.tracking_number IS NULL
    ORDER BY cs.last_synced_at NULLS FIRST, t.tracking_number
    ${limitClause}
  `, [staleHours]);
  return rows.map((row) => row.tracking_number);
}

async function getOrderNumbersWithoutTracks() {
  if (process.env.CDEK_SYNC_IM_FROM_SALES !== "1") {
    return [];
  }
  const limitClause = limit > 0 ? `LIMIT ${limit}` : "";
  const { rows } = await pool.query(`
    WITH orders AS (
      SELECT DISTINCT d.order_number
      FROM bitrix24.sales_orders_dashboard d
      LEFT JOIN bitrix24.cdek_shipments cs ON cs.cdek_order_number = d.order_number
      WHERE coalesce(d.order_number, '') <> ''
        AND nullif(d.raw_data->>'tracking_number', '') IS NULL
        AND cs.tracking_number IS NULL
    )
    SELECT order_number
    FROM orders
    ORDER BY order_number
    ${limitClause}
  `);
  return rows.map((row) => row.order_number);
}

async function refreshFreeOrders() {
  const staleFreeRows = await pool.query(`
    DELETE FROM bitrix24.cdek_free_orders
    WHERE NOT (
      lower(coalesce(im_number,'')) LIKE '7d-%'
      OR lower(coalesce(im_number,'')) LIKE 'dl-%'
      OR lower(coalesce(im_number,'')) LIKE 'pr-%'
      OR lower(coalesce(im_number,'')) LIKE 'mf%'
      OR lower(coalesce(im_number,'')) ~ '(^|-)01'
      OR coalesce(source,'') = 'manual_internal_transfer'
      OR (
        trim(coalesce(raw_data->>'client_order_number', im_number, '')) ~ '^[[:space:]–—−-]*$'
        AND lower(regexp_replace(trim(coalesce(recipient_name,'')), '[[:space:]]+', ' ', 'g')) ~ '^брагин никита([[:space:]]|$)'
      )
    )
  `);
  console.log(`free orders cleaned by im_number: ${staleFreeRows.rowCount}`);

  const { rowCount } = await pool.query(`
    WITH source_rows AS (
      SELECT
        coalesce(seed.tracking_number, cs.tracking_number) AS tracking_number,
        coalesce(nullif(cs.cdek_order_number, ''), nullif(cs.raw_data->>'number', '')) AS im_number,
        coalesce(seed.order_created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz, cs.last_synced_at) AS order_created_at,
        coalesce(seed.status_name, cs.status_name) AS status_name,
        coalesce(seed.recipient_name, cs.recipient_name) AS recipient_name,
        coalesce(seed.source, 'cdek_shipments') AS source,
        jsonb_set(
          coalesce(seed.raw_data, cs.raw_data, '{}'::jsonb),
          '{client_order_number}',
          to_jsonb(coalesce(cs.cdek_order_number, seed.im_number, '')),
          true
        ) AS raw_data,
        coalesce(cs.cdek_order_number, seed.im_number, '') AS client_order_number
      FROM bitrix24.cdek_shipments cs
      FULL JOIN bitrix24.cdek_tracking_seed seed ON seed.tracking_number = cs.tracking_number
      LEFT JOIN bitrix24.cdek_deleted_tracks dt ON dt.tracking_number = coalesce(seed.tracking_number, cs.tracking_number)
      WHERE coalesce(seed.tracking_number, cs.tracking_number) ~ '^10[0-9]{8,9}$'
        AND dt.tracking_number IS NULL
    ), classified AS (
      SELECT *,
        CASE
          WHEN lower(coalesce(im_number,'')) LIKE '7d-%' THEN 'Блогеры / бартер'
          WHEN lower(coalesce(im_number,'')) LIKE 'dl-%' THEN 'Клиентам без денег'
          WHEN lower(coalesce(im_number,'')) LIKE 'pr-%' THEN 'PR-рассылки'
          WHEN lower(coalesce(im_number,'')) LIKE 'mf%' OR lower(coalesce(im_number,'')) ~ '(^|-)01' THEN 'Внутренние / не клиенты'
          WHEN trim(coalesce(client_order_number,'')) ~ '^[[:space:]–—−-]*$'
            AND lower(regexp_replace(trim(coalesce(recipient_name,'')), '[[:space:]]+', ' ', 'g')) ~ '^брагин никита([[:space:]]|$)'
            THEN 'Внутренние / не клиенты'
          ELSE NULL
        END AS free_order_type,
        CASE
          WHEN lower(coalesce(im_number,'')) LIKE '7d-%' THEN 'Номер ИМ начинается с 7d-'
          WHEN lower(coalesce(im_number,'')) LIKE 'dl-%' THEN 'Номер ИМ начинается с dl-'
          WHEN lower(coalesce(im_number,'')) LIKE 'pr-%' THEN 'Номер ИМ начинается с pr-'
          WHEN lower(coalesce(im_number,'')) LIKE 'mf%' OR lower(coalesce(im_number,'')) ~ '(^|-)01' THEN 'Номер ИМ содержит mf или 01-маркер'
          WHEN trim(coalesce(client_order_number,'')) ~ '^[[:space:]–—−-]*$'
            AND lower(regexp_replace(trim(coalesce(recipient_name,'')), '[[:space:]]+', ' ', 'g')) ~ '^брагин никита([[:space:]]|$)'
            THEN 'Пустой/прочерк номер заказа и получатель Брагин Никита'
          ELSE NULL
        END AS free_order_reason
      FROM source_rows
    )
    INSERT INTO bitrix24.cdek_free_orders (
      tracking_number, im_number, order_created_at, status_name, recipient_name,
      source, raw_data, in_client_table, free_order_type, free_order_reason, imported_at, updated_at
    )
    SELECT
      tracking_number, im_number, order_created_at, status_name, recipient_name,
      source, raw_data, false, free_order_type, free_order_reason, now(), now()
    FROM classified
    WHERE free_order_type IS NOT NULL
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
  `);
  console.log(`free orders refreshed by im_number: ${rowCount}`);
}

async function worker(queue, token, state) {
  while (queue.length) {
    const trackingNumber = queue.shift();
    try {
      const shipment = await fetchShipment(token, trackingNumber);
      await upsertShipment(shipment);
      state.ok += 1;
    } catch (error) {
      await saveError(trackingNumber, error);
      state.errors += 1;
    }
    state.done += 1;
    if (state.done % 100 === 0 || state.done === state.total) {
      console.log(`synced ${state.done}/${state.total}, ok=${state.ok}, errors=${state.errors}`);
    }
  }
}

async function main() {
  assertConfig();
  await ensureTable();
  const tracks = await getTracks();
  const state = { total: tracks.length, done: 0, ok: 0, errors: 0 };
  console.log(`CDEK tracks to sync: ${tracks.length}`);
  const token = await getToken();
  if (tracks.length) {
    await Promise.all(Array.from({ length: concurrency }, () => worker(tracks, token, state)));
  }

  const orderNumbers = await getOrderNumbersWithoutTracks();
  const imState = { total: orderNumbers.length, done: 0, ok: 0, errors: 0 };
  console.log(`CDEK im_numbers to sync: ${orderNumbers.length}`);
  await Promise.all(Array.from({ length: concurrency }, async () => {
    while (orderNumbers.length) {
      const orderNumber = orderNumbers.shift();
      try {
        const shipment = await fetchShipmentByImNumber(token, orderNumber);
        await upsertShipment(shipment);
        imState.ok += 1;
      } catch {
        imState.errors += 1;
      }
      imState.done += 1;
      if (imState.done % 100 === 0 || imState.done === imState.total) {
        console.log(`im synced ${imState.done}/${imState.total}, ok=${imState.ok}, errors=${imState.errors}`);
      }
    }
  }));
  await refreshFreeOrders();
}

main()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    await pool.end();
  });
