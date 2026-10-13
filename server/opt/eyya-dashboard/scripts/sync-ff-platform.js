const { Pool } = require("pg");

const apiBase = process.env.FF_PLATFORM_API_BASE || "https://ff-platform.ru/lk/1/client_api/v1";
const token = process.env.FF_PLATFORM_TOKEN;
const fromArg = process.env.FF_SYNC_FROM || process.argv[2];
const toArg = process.env.FF_SYNC_TO || process.argv[3];
const concurrency = Math.max(1, Math.min(6, Number(process.env.FF_SYNC_CONCURRENCY || 3)));
const statuses = (process.env.FF_SYNC_STATUSES || "sended,in_work,not_work").split(",").map((s) => s.trim()).filter(Boolean);

const pool = new Pool({
  host: process.env.PGHOST,
  port: Number(process.env.PGPORT || 5432),
  database: process.env.PGDATABASE,
  user: process.env.PGUSER,
  password: process.env.PGPASSWORD,
  max: concurrency + 2,
});

function assertConfig() {
  if (!token) throw new Error("FF_PLATFORM_TOKEN is required");
}

function formatDate(date) {
  const dd = String(date.getDate()).padStart(2, "0");
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const yyyy = date.getFullYear();
  return `${dd}-${mm}-${yyyy}`;
}

function defaultFrom() {
  const date = new Date();
  date.setDate(date.getDate() - 10);
  return formatDate(date);
}

async function ensureTable() {
  await pool.query(`
    CREATE TABLE IF NOT EXISTS bitrix24.ff_cdek_shipments (
      id bigint PRIMARY KEY,
      status text,
      type_contract text,
      number_contract text,
      date_created date,
      date_closed date,
      dispatch_status text,
      dispatch_date_created timestamptz,
      dispatch_date_at_work timestamptz,
      dispatch_date_at_otgrugen timestamptz,
      delivery_performer_track_number text,
      billing_costs jsonb,
      products jsonb,
      raw_list jsonb,
      raw_detail jsonb,
      raw_dispatch jsonb,
      last_synced_at timestamptz DEFAULT now(),
      sync_error text
    );
    CREATE INDEX IF NOT EXISTS ff_cdek_shipments_number_idx ON bitrix24.ff_cdek_shipments(number_contract);
    CREATE INDEX IF NOT EXISTS ff_cdek_shipments_track_idx ON bitrix24.ff_cdek_shipments(delivery_performer_track_number);
    CREATE INDEX IF NOT EXISTS ff_cdek_shipments_date_created_idx ON bitrix24.ff_cdek_shipments(date_created);
  `);
}

async function requestJson(path, params = {}) {
  const url = new URL(`${apiBase}/${path}`);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, value);
  });
  const response = await fetch(url, { headers: { token } });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`${response.status} ${body?.message || body?.error || response.statusText}`);
  }
  return body;
}

function parseDate(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{2})-(\d{2})-(\d{4})$/);
  if (!match) return null;
  return `${match[3]}-${match[2]}-${match[1]}`;
}

function parseTimestamp(value) {
  if (!value) return null;
  const match = String(value).match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2}):(\d{2})$/);
  if (!match) return null;
  return `${match[3]}-${match[2]}-${match[1]} ${match[4]}:${match[5]}:${match[6]}+03`;
}

async function upsertShipment(row, detail, dispatch, error = null) {
  const otgruzka = detail?.otgruzka || row || {};
  const products = Array.isArray(detail?.products) ? detail.products : [];
  await pool.query(`
    INSERT INTO bitrix24.ff_cdek_shipments (
      id, status, type_contract, number_contract, date_created, date_closed,
      dispatch_status, dispatch_date_created, dispatch_date_at_work, dispatch_date_at_otgrugen,
      delivery_performer_track_number, billing_costs, products, raw_list, raw_detail, raw_dispatch,
      last_synced_at, sync_error
    ) VALUES (
      $1,$2,$3,$4,$5::date,$6::date,$7,$8::timestamptz,$9::timestamptz,$10::timestamptz,
      $11,$12::jsonb,$13::jsonb,$14::jsonb,$15::jsonb,$16::jsonb,now(),$17
    )
    ON CONFLICT (id) DO UPDATE SET
      status = excluded.status,
      type_contract = excluded.type_contract,
      number_contract = excluded.number_contract,
      date_created = excluded.date_created,
      date_closed = excluded.date_closed,
      dispatch_status = excluded.dispatch_status,
      dispatch_date_created = excluded.dispatch_date_created,
      dispatch_date_at_work = excluded.dispatch_date_at_work,
      dispatch_date_at_otgrugen = excluded.dispatch_date_at_otgrugen,
      delivery_performer_track_number = excluded.delivery_performer_track_number,
      billing_costs = excluded.billing_costs,
      products = excluded.products,
      raw_list = excluded.raw_list,
      raw_detail = excluded.raw_detail,
      raw_dispatch = excluded.raw_dispatch,
      last_synced_at = now(),
      sync_error = excluded.sync_error
  `, [
    Number(row.id),
    otgruzka.status || row.status || null,
    otgruzka.type_contract || row.type_contract || null,
    otgruzka.number_contract || row.number_contract || null,
    parseDate(otgruzka.dateCreated || row.dateCreated),
    parseDate(otgruzka.dateClosed || row.dateClosed),
    dispatch?.status || null,
    parseTimestamp(dispatch?.date_created),
    parseTimestamp(dispatch?.date_at_work),
    parseTimestamp(dispatch?.date_at_otgrugen),
    dispatch?.delivery_performer_track_number || otgruzka.number_contract || row.number_contract || null,
    JSON.stringify(dispatch?.billing_costs || {}),
    JSON.stringify(products),
    JSON.stringify(row || {}),
    JSON.stringify(detail || {}),
    JSON.stringify(dispatch || {}),
    error ? String(error).slice(0, 500) : null,
  ]);
}

async function syncOne(row) {
  try {
    const [detail, dispatch] = await Promise.all([
      requestJson("cdek_otgruzki/detail", { id: row.id }),
      requestJson(`sklad_dispatch/${row.id}`),
    ]);
    await upsertShipment(row, detail, dispatch);
    return true;
  } catch (error) {
    await upsertShipment(row, null, null, error.message || error);
    return false;
  }
}

async function worker(queue, state) {
  while (queue.length) {
    const row = queue.shift();
    const ok = await syncOne(row);
    state.done += 1;
    state.ok += ok ? 1 : 0;
    state.errors += ok ? 0 : 1;
    if (state.done % 50 === 0 || state.done === state.total) {
      console.log(`ff synced ${state.done}/${state.total}, ok=${state.ok}, errors=${state.errors}`);
    }
  }
}

async function main() {
  assertConfig();
  await ensureTable();
  const from = fromArg || defaultFrom();
  const to = toArg || "";
  const byId = new Map();
  for (const status of statuses) {
    const rows = await requestJson("cdek_otgruzki", { from, to, status });
    if (Array.isArray(rows)) rows.forEach((row) => byId.set(Number(row.id), row));
  }
  const queue = Array.from(byId.values());
  console.log(`FF shipments to sync: ${queue.length}, from=${from}${to ? `, to=${to}` : ""}`);
  const state = { total: queue.length, done: 0, ok: 0, errors: 0 };
  await Promise.all(Array.from({ length: Math.min(concurrency, queue.length || 1) }, () => worker(queue, state)));
  await pool.end();
}

main().catch(async (error) => {
  console.error(error);
  await pool.end().catch(() => {});
  process.exit(1);
});
