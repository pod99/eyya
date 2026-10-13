const { Pool } = require("pg");

const days = Math.max(1, Number(process.env.CDEK_COMBO_LOOKBACK_DAYS || 14));
const exoPrice = Math.max(0, Number(process.env.CDEK_COMBO_EXO_PRICE || 5290));
const moscowTimeZone = "Europe/Moscow";
const isDaemon = process.argv.includes("--daemon");
const targetTrackingNumber = String(process.env.CDEK_COMBO_TRACKING_NUMBER || process.env.TRACKING_NUMBER || "").trim();
const cdekApiBase = process.env.CDEK_API_BASE || "https://api.cdek.ru/v2";
const cdekClientId = process.env.CDEK_CLIENT_ID || "";
const cdekClientSecret = process.env.CDEK_CLIENT_SECRET || "";
let cdekToken = null;

const pool = new Pool({
  host: process.env.PGHOST,
  port: Number(process.env.PGPORT || 5432),
  database: process.env.PGDATABASE,
  user: process.env.PGUSER,
  password: process.env.PGPASSWORD,
  max: 3,
});

const glosses = [
  {
    sku: "EY_07_tula",
    name: "Tula. Блеск-бальзам для губ с экзосомами и пептидами",
    patterns: [/тула/i, /tula/i, /ey_07_tula/i],
  },
  {
    sku: "EY_05_vologda",
    name: "Vologda. Блеск-бальзам для губ с экзосомами и пептидами",
    patterns: [/вологд/i, /vologda/i, /ey_05_vologda/i],
  },
  {
    sku: "EY_08_chelyabinsk",
    name: "Chelyabinsk. Блеск-бальзам для губ с экзосомами и пептидами",
    patterns: [/челябинск/i, /chelyabinsk/i, /ey_08_chelyabinsk/i],
  },
];

const exoItem = {
  sku: "EY_04_skinrise",
  name: "Exo System Box. Система ухода за кожей лица",
  weight: 500,
};

function normalizeSku(value) {
  const sku = String(value || "").trim().replace(/(?:_free)+\s*$/i, "");
  if (/^ey_04_skinrise-\d+$/i.test(sku)) return "EY_04_skinrise";
  return sku;
}

function sameMoney(left, right) {
  return Math.round(Number(left || 0) * 100) === Math.round(Number(right || 0) * 100);
}

function findGloss(item) {
  const text = `${item.sku || ""} ${item.ware_key || ""} ${item.name || ""}`;
  return glosses.find((gloss) => gloss.patterns.some((pattern) => pattern.test(text))) || null;
}

function hasCdekConfig() {
  return Boolean(cdekClientId && cdekClientSecret);
}

function isComboItem(item) {
  const sku = String(item.sku || item.ware_key || "");
  const name = String(item.name || "");
  return /ey_04_skinrise\s*\+/i.test(sku) ||
    (/ey_04_skinrise/i.test(sku) && /набор.*блеск/i.test(name)) ||
    /Набор:\s*Набор\s*\+\s*блеск/i.test(name);
}

function cleanExoBundleName(name) {
  return String(name || "")
    .replace(/\s*\(Набор:\s*Набор\s*\+\s*блеск\s*[^)]*\)/i, "")
    .replace(/\s*\(Набор:\s*Набор\)/i, "")
    .trim();
}

function itemSummary(item) {
  const amount = Number(item.amount || 0);
  const suffix = amount ? ` x ${amount}` : "";
  return `${String(item.name || "Товар").trim()}${suffix}`;
}

function latestStatus(entity) {
  const statuses = Array.isArray(entity?.statuses) ? entity.statuses.filter((status) => !status.deleted) : [];
  return statuses
    .slice()
    .sort((left, right) => new Date(right.date_time || 0) - new Date(left.date_time || 0))[0] || {};
}

function flattenCdekItems(entity) {
  return (entity?.packages || []).flatMap((pack) => (pack.items || []).map((item) => ({
    name: String(item.name || "").trim(),
    sku: normalizeSku(item.ware_key || ""),
    amount: Number(item.amount || 0),
    cost: Number(item.cost || 0),
    weight: Number(item.weight || 0),
  })));
}

function safeRaw(entity) {
  const copy = JSON.parse(JSON.stringify(entity || {}));
  delete copy.developer_key;
  delete copy.calls;
  return copy;
}

function hasRawComboCandidate(raw) {
  const packages = Array.isArray(raw?.packages) ? raw.packages : [];
  return packages.some((pack) => (pack.items || []).some((item) => {
    const sku = String(item.ware_key || "");
    const name = String(item.name || "");
    return /ey_04_skinrise\s*\+/i.test(sku) ||
      (/ey_04_skinrise/i.test(sku) && /\(Набор:/i.test(name)) ||
      /_free$/i.test(sku);
  }));
}

function sanitizePayment(payment) {
  const source = payment && typeof payment === "object" ? payment : { value: 0 };
  const result = { value: Number(source.value || 0) };
  if (source.vat_sum != null) result.vat_sum = Number(source.vat_sum || 0);
  if (source.vat_rate != null) result.vat_rate = source.vat_rate;
  return result;
}

function sanitizeCdekItem(item) {
  const result = {
    name: String(item.name || "Товар").trim(),
    ware_key: normalizeSku(item.ware_key || ""),
    payment: sanitizePayment(item.payment),
    cost: Number(item.cost || 0),
    weight: Number(item.weight || 0) || 1,
    amount: Number(item.amount || 1) || 1,
  };
  if (item.weight_gross != null) result.weight_gross = Number(item.weight_gross || result.weight);
  if (item.delivery_amount != null) result.delivery_amount = Number(item.delivery_amount || 0);
  if (item.marking) result.marking = item.marking;
  if (item.name_i18n) result.name_i18n = item.name_i18n;
  if (item.brand) result.brand = item.brand;
  if (item.country_code) result.country_code = item.country_code;
  if (item.material) result.material = item.material;
  return result;
}

function normalizeCdekItems(items) {
  let changed = false;
  const next = [];

  for (const source of Array.isArray(items) ? items : []) {
    const item = sanitizeCdekItem(source);
    const before = JSON.stringify(item);

    if (!isComboItem({ ...item, sku: item.ware_key })) {
      if (
        /ey_04_skinrise/i.test(item.ware_key) &&
        sameMoney(item.cost, exoPrice) &&
        /\(Набор:/i.test(String(item.name || ""))
      ) {
        const cleanedName = cleanExoBundleName(item.name);
        if (cleanedName && cleanedName !== item.name) {
          item.name = cleanedName;
          changed = true;
        }
      }
      if (JSON.stringify(item) !== before) changed = true;
      next.push(item);
      continue;
    }

    const gloss = findGloss({ ...item, sku: item.ware_key });
    if (!gloss) {
      if (JSON.stringify(item) !== before) changed = true;
      next.push(item);
      continue;
    }

    const comboCost = Number(item.cost || 0);
    const glossCost = Math.max(0, comboCost - exoPrice);
    next.push({
      ...item,
      name: exoItem.name,
      ware_key: exoItem.sku,
      cost: exoPrice,
      weight: exoItem.weight,
      weight_gross: exoItem.weight,
    });
    next.push({
      ...item,
      name: gloss.name,
      ware_key: gloss.sku,
      cost: glossCost,
      weight: 20,
      weight_gross: 20,
    });
    changed = true;
  }

  return { changed, items: next };
}

function cdekPackagePayload(pack, items) {
  const itemsWeight = items.reduce((total, item) => total + Number(item.weight || 0) * Number(item.amount || 1), 0);
  const result = {
    package_id: pack.package_id,
    number: pack.number,
    weight: Math.max(Number(pack.weight || 0), itemsWeight || 1),
    length: pack.length || 17,
    width: pack.width || 12,
    height: pack.height || 10,
    items,
  };
  if (pack.barcode) result.barcode = pack.barcode;
  if (pack.comment) result.comment = pack.comment;
  return result;
}

function normalizeCdekPackages(entity) {
  let changed = false;
  const packages = [];
  for (const pack of Array.isArray(entity?.packages) ? entity.packages : []) {
    const normalized = normalizeCdekItems(pack.items || []);
    const payload = cdekPackagePayload(pack, normalized.items);
    if (!pack.package_id) {
      return { changed: false, packages: [], reason: `package_id is empty for ${pack.number || "package"}` };
    }
    if (normalized.changed || Number(pack.weight || 0) !== payload.weight) changed = true;
    packages.push(payload);
  }
  return { changed, packages };
}

async function cdekRequest(path, options = {}) {
  const response = await fetch(`${cdekApiBase}${path}`, options);
  const text = await response.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  return { ok: response.ok, status: response.status, body };
}

async function getCdekToken() {
  if (cdekToken && cdekToken.expiresAt > Date.now() + 60_000) {
    return cdekToken.accessToken;
  }
  const result = await cdekRequest("/oauth/token", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "client_credentials",
      client_id: cdekClientId,
      client_secret: cdekClientSecret,
    }),
  });
  if (!result.ok || !result.body.access_token) {
    throw new Error(`CDEK auth failed: ${result.status} ${result.body.error || result.body.message || ""}`);
  }
  const expiresIn = Math.max(60, Number(result.body.expires_in || 3600));
  cdekToken = {
    accessToken: result.body.access_token,
    expiresAt: Date.now() + expiresIn * 1000,
  };
  return cdekToken.accessToken;
}

async function cdekAuthorizedRequest(path, options = {}) {
  let token = await getCdekToken();
  let result = await cdekRequest(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
  });
  if (result.status !== 401) return result;

  cdekToken = null;
  token = await getCdekToken();
  return cdekRequest(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
  });
}

async function fetchCdekOrder(trackingNumber) {
  const result = await cdekAuthorizedRequest(`/orders?cdek_number=${encodeURIComponent(trackingNumber)}`);
  if (!result.ok || !result.body.entity) {
    throw new Error(`CDEK fetch failed for ${trackingNumber}: ${result.status} ${result.body.message || result.body.error || ""}`);
  }
  return result.body.entity;
}

function responseErrors(body) {
  return (body.requests || []).flatMap((request) => request.errors || []).map((error) => error.message || error.code || JSON.stringify(error));
}

async function patchCdekOrderIfPossible(trackingNumber) {
  if (!hasCdekConfig()) return { action: "disabled" };

  const entity = await fetchCdekOrder(trackingNumber);
  if (!hasRawComboCandidate(entity)) {
    return { action: "already_normalized", entity };
  }

  const status = latestStatus(entity);
  if (!["CREATED", "ACCEPTED"].includes(status.code)) {
    return { action: "skipped", reason: `status ${status.code || "unknown"} is not editable`, entity };
  }

  const normalized = normalizeCdekPackages(entity);
  if (normalized.reason) return { action: "skipped", reason: normalized.reason, entity };
  if (!normalized.changed) return { action: "already_normalized", entity };

  const result = await cdekAuthorizedRequest("/orders", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uuid: entity.uuid, packages: normalized.packages }),
  });
  const errors = responseErrors(result.body);
  if (!result.ok || errors.length) {
    if (result.status === 400) {
      return { action: "skipped", reason: errors.join("; ") || `CDEK rejected update with ${result.status}`, entity };
    }
    throw new Error(`CDEK patch failed for ${trackingNumber}: ${result.status} ${errors.join("; ")}`);
  }

  await new Promise((resolve) => setTimeout(resolve, 2500));
  return { action: "patched", entity: await fetchCdekOrder(trackingNumber) };
}

function normalizeItems(items) {
  let changed = false;
  const next = [];

  for (const item of Array.isArray(items) ? items : []) {
    const normalized = { ...item };
    const sourceSku = normalized.sku ?? normalized.ware_key;
    const normalizedSku = normalizeSku(sourceSku);
    if (normalizedSku && normalized.sku !== normalizedSku) {
      normalized.sku = normalizedSku;
      changed = true;
    }
    if (normalized.ware_key && normalizeSku(normalized.ware_key) !== normalized.ware_key) {
      normalized.ware_key = normalizeSku(normalized.ware_key);
      changed = true;
    }

    if (!isComboItem(normalized)) {
      if (
        /ey_04_skinrise/i.test(String(normalized.sku || normalized.ware_key || "")) &&
        sameMoney(normalized.cost, exoPrice) &&
        /\(Набор:/i.test(String(normalized.name || ""))
      ) {
        const cleanedName = cleanExoBundleName(normalized.name);
        if (cleanedName && cleanedName !== normalized.name) {
          normalized.name = cleanedName;
          changed = true;
        }
      }
      next.push(normalized);
      continue;
    }

    const gloss = findGloss(normalized);
    if (!gloss) {
      next.push(normalized);
      continue;
    }

    const amount = Number(normalized.amount || 1) || 1;
    const comboCost = Number(normalized.cost || 0);
    const glossCost = Math.max(0, comboCost - exoPrice);
    const common = {
      amount,
      normalized_from_combo: true,
      combo_original_sku: sourceSku || null,
      combo_original_name: normalized.name || null,
      combo_original_cost: comboCost,
    };

    next.push({
      ...common,
      sku: exoItem.sku,
      name: exoItem.name,
      cost: exoPrice,
      weight: exoItem.weight,
    });
    next.push({
      ...common,
      sku: gloss.sku,
      name: gloss.name,
      cost: glossCost,
      weight: 20,
    });
    changed = true;
  }

  return { changed, items: next };
}

async function normalizeRecentCombos() {
  const lockResult = await pool.query("SELECT pg_try_advisory_lock(hashtext('eyya_cdek_combo_normalizer')) AS locked");
  if (!lockResult.rows[0]?.locked) {
    console.log(JSON.stringify({
      event: "cdek_combo_normalizer_skip_locked",
      targetTrackingNumber: targetTrackingNumber || null,
      at: new Date().toISOString(),
    }));
    return { checked: 0, updated: 0, skipped: 0, locked: true };
  }

  try {
  const { rows } = await pool.query(`
    WITH cdek AS (
      SELECT cs.tracking_number,
        cs.items,
        cs.raw_data,
        coalesce(sd.created_at, cs.status_at, cs.delivery_date::timestamptz, cs.planned_delivery_date::timestamptz, cs.last_synced_at)::date AS cdek_date
      FROM bitrix24.cdek_shipments cs
      LEFT JOIN LATERAL (
        SELECT min((s.value->>'date_time')::timestamptz) FILTER (WHERE s.value->>'code' IN ('CREATED','ACCEPTED')) AS created_at
        FROM jsonb_array_elements(coalesce(cs.raw_data->'statuses', '[]'::jsonb)) AS s(value)
      ) sd ON true
      WHERE cs.items IS NOT NULL
    )
    SELECT tracking_number, items, raw_data
    FROM cdek
    WHERE (
      ($2::text <> '' AND tracking_number = $2::text)
      OR (
        cdek_date >= ((now() AT TIME ZONE 'Europe/Moscow')::date - ($1::int * interval '1 day'))
        AND (
        EXISTS (
          SELECT 1
          FROM jsonb_array_elements(coalesce(items, '[]'::jsonb)) AS item(value)
          WHERE coalesce(item.value->>'sku', item.value->>'ware_key', '') ILIKE '%EY_04_skinrise +%'
             OR coalesce(item.value->>'sku', item.value->>'ware_key', '') ILIKE '%_free'
             OR coalesce(item.value->>'name', '') ILIKE '%Набор:%'
        )
        OR (
          raw_data::text ILIKE '%EY_04_skinrise%'
          AND raw_data::text ILIKE '%Набор:%'
          AND coalesce(raw_data->>'eyya_combo_cdek_patched_at', '') = ''
          AND coalesce(raw_data->>'eyya_combo_cdek_patch_skipped_at', '') = ''
        )
      )
      )
    )
    ORDER BY tracking_number
  `, [days, targetTrackingNumber]);

  let updated = 0;
  let skipped = 0;
  const samples = [];
  const cdekPatched = [];
  const cdekSkipped = [];

  for (const row of rows) {
    const normalized = normalizeItems(row.items);
    let items = normalized.items;
    let cdekResult = null;
    let rawPatchSql = "jsonb_set($5::jsonb, '{eyya_combo_normalized_at}', to_jsonb(now()), true)";

    if (hasCdekConfig() && (hasRawComboCandidate(row.raw_data) || (targetTrackingNumber && row.tracking_number === targetTrackingNumber))) {
      try {
        cdekResult = await patchCdekOrderIfPossible(row.tracking_number);
      } catch (error) {
        cdekResult = { action: "skipped", reason: String(error.message || error) };
      }
      if (["patched", "already_normalized"].includes(cdekResult.action) && cdekResult.entity) {
        items = flattenCdekItems(cdekResult.entity);
      }
      if (cdekResult.action === "patched") {
        cdekPatched.push(row.tracking_number);
        rawPatchSql = "jsonb_set(jsonb_set($5::jsonb, '{eyya_combo_normalized_at}', to_jsonb(now()), true), '{eyya_combo_cdek_patched_at}', to_jsonb(now()), true)";
      } else if (cdekResult.action === "skipped") {
        cdekSkipped.push({ trackingNumber: row.tracking_number, reason: cdekResult.reason });
        rawPatchSql = "jsonb_set(jsonb_set($5::jsonb, '{eyya_combo_normalized_at}', to_jsonb(now()), true), '{eyya_combo_cdek_patch_skipped_at}', to_jsonb(now()), true)";
      } else if (cdekResult.action === "already_normalized") {
        rawPatchSql = "jsonb_set($5::jsonb, '{eyya_combo_normalized_at}', to_jsonb(now()), true)";
      }
    }

    if (!normalized.changed && !cdekResult) {
      skipped += 1;
      continue;
    }

    const itemsCount = items.reduce((total, item) => total + Number(item.amount || 0), 0);
    const itemsSummary = items.map(itemSummary).join(" | ");
    await pool.query(`
      UPDATE bitrix24.cdek_shipments
      SET items = $2::jsonb,
          items_count = $3,
          items_summary = $4,
          raw_data = ${rawPatchSql}
      WHERE tracking_number = $1
    `, [
      row.tracking_number,
      JSON.stringify(items),
      itemsCount,
      itemsSummary,
      JSON.stringify(cdekResult?.entity ? safeRaw(cdekResult.entity) : row.raw_data || {}),
    ]);
    updated += 1;
    if (samples.length < 10) samples.push(row.tracking_number);
  }

  console.log(JSON.stringify({
    event: "cdek_combo_normalized",
    checked: rows.length,
    updated,
    skipped,
    samples,
    cdekPatched,
    cdekSkipped,
    lookbackDays: days,
    targetTrackingNumber: targetTrackingNumber || null,
    at: new Date().toISOString(),
  }));
  return { checked: rows.length, updated, skipped };
  } finally {
    await pool.query("SELECT pg_advisory_unlock(hashtext('eyya_cdek_combo_normalizer'))");
  }
}

function moscowHour() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: moscowTimeZone,
    hour: "2-digit",
    hour12: false,
  }).formatToParts(new Date());
  return Number(parts.find((part) => part.type === "hour")?.value || 0);
}

function intervalMs() {
  const hour = moscowHour();
  const frequentHours = hour >= 7 || hour < 1;
  return frequentHours ? 30 * 60 * 1000 : 2 * 60 * 60 * 1000;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runDaemon() {
  while (true) {
    try {
      await normalizeRecentCombos();
    } catch (error) {
      console.error(JSON.stringify({
        event: "cdek_combo_normalize_error",
        message: String(error.message || error),
        at: new Date().toISOString(),
      }));
      throw error;
    }
    const nextInterval = intervalMs();
    console.log(JSON.stringify({
      event: "cdek_combo_normalizer_sleep",
      minutes: Math.round(nextInterval / 60000),
      moscowHour: moscowHour(),
    }));
    await sleep(nextInterval);
  }
}

(isDaemon ? runDaemon() : normalizeRecentCombos())
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    await pool.end();
  });
