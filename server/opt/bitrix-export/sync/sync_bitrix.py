#!/usr/bin/env python3
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any

import psycopg2
import psycopg2.extras
import requests

WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/") + "/"
DATABASE_URL = os.environ["DATABASE_URL"]
PAGE_SIZE = 50
BATCH_SIZE = 50
REQUEST_DELAY = 0.15


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    return digits


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().lower()
    return email or None


def extract_multi_field(record: dict, field: str) -> str | None:
    values = record.get(field) or []
    if isinstance(values, list):
        for item in values:
            if isinstance(item, dict):
                val = item.get("VALUE")
                if val:
                    return str(val)
    return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_order_number(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"(?:№|заказ)\s*(\d+)(?:\b|-)", title, re.IGNORECASE)
    return match.group(1) if match else None


def flatten_bitrix_params(params: dict | None) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []

    def add(prefix: str, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                add(f"{prefix}[{key}]", nested)
            return
        if isinstance(value, (list, tuple)):
            for nested in value:
                add(f"{prefix}[]", nested)
            return
        result.append((prefix, value))

    for key, value in (params or {}).items():
        add(key, value)
    return result


class BitrixClient:
    def __init__(self, webhook: str):
        self.webhook = webhook
        self.session = requests.Session()

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(8):
            time.sleep(REQUEST_DELAY * (1 + attempt * 0.25))
            try:
                if method == "GET":
                    response = self.session.get(url, timeout=90, **kwargs)
                else:
                    response = self.session.post(url, timeout=120, **kwargs)
                response.raise_for_status()
                return response
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                last_error = exc
                wait = min(2 ** attempt, 30)
                log(f"retry {attempt + 1}/8 after {type(exc).__name__}, sleep {wait}s")
                time.sleep(wait)
        raise last_error  # type: ignore[misc]

    def call(self, method: str, params: dict | None = None) -> dict:
        response = self._request_with_retry("GET", self.webhook + method, params=flatten_bitrix_params(params))
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']} - {data.get('error_description')}")
        return data

    @staticmethod
    def extract_items(result: Any) -> list[dict]:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            if "tasks" in result:
                return result["tasks"]
            if "items" in result:
                return result["items"]
            return list(result.values())
        return []

    def list_all(self, method: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        params["start"] = 0
        all_items: list[dict] = []
        while True:
            data = self.call(method, params)
            items = self.extract_items(data.get("result", []))
            all_items.extend(items)
            next_start = data.get("next")
            total = data.get("total", "?")
            log(f"{method}: {len(all_items)}/{total}")
            if next_start is None:
                break
            params["start"] = next_start
        return all_items

    def batch(self, commands: dict[str, str]) -> dict[str, Any]:
        response = self._request_with_retry(
            "POST",
            self.webhook + "batch",
            json={"halt": 0, "cmd": commands},
        )
        data = response.json()
        if "error" in data:
            raise RuntimeError(f"batch: {data['error']} - {data.get('error_description')}")
        return data.get("result", {}).get("result", {})


class SyncService:
    def __init__(self, conn, client: BitrixClient):
        self.conn = conn
        self.client = client

    def init_schema(self) -> None:
        with open(os.path.join(os.path.dirname(__file__), "schema.sql"), "r", encoding="utf-8") as f:
            sql = f.read()
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    def start_run(self, entity_type: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bitrix_sync_runs (entity_type) VALUES (%s) RETURNING id",
                (entity_type,),
            )
            run_id = cur.fetchone()[0]
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: int, fetched: int, inserted: int, duplicates: int, errors: int, status: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bitrix_sync_runs
                SET finished_at = NOW(), fetched = %s, inserted = %s, duplicates = %s, errors = %s, status = %s
                WHERE id = %s
                """,
                (fetched, inserted, duplicates, errors, status, run_id),
            )
        self.conn.commit()

    def log_duplicate(self, entity_type: str, bitrix_id: int, reason: str, duplicate_of: int | None, dedup_key: str | None, raw: dict) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bitrix_duplicates (entity_type, bitrix_id, duplicate_of, reason, dedup_key, raw_data)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (entity_type, bitrix_id, duplicate_of, reason, dedup_key, json.dumps(raw, ensure_ascii=False)),
            )

    def log_review(
        self,
        entity_type: str,
        bitrix_id: int,
        similar_to: int,
        reason: str,
        match_key: str | None,
        details: dict,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bitrix_review_queue (entity_type, bitrix_id, similar_to, reason, match_key, details)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (entity_type, bitrix_id, similar_to, reason) DO NOTHING
                """,
                (
                    entity_type,
                    bitrix_id,
                    similar_to,
                    reason,
                    match_key,
                    json.dumps(details, ensure_ascii=False),
                ),
            )

    def find_contact_similar(self, phone: str | None, email: str | None, bitrix_id: int) -> tuple[int | None, str | None, dict | None]:
        with self.conn.cursor() as cur:
            if phone:
                cur.execute(
                    "SELECT id, name, last_name, phone_normalized, email_normalized FROM bitrix_contacts WHERE phone_normalized = %s AND id <> %s LIMIT 1",
                    (phone, bitrix_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0], "same_phone", {
                        "existing_name": f"{row[1] or ''} {row[2] or ''}".strip(),
                        "phone": row[3],
                        "email": row[4],
                    }
            if email:
                cur.execute(
                    "SELECT id, name, last_name, phone_normalized, email_normalized FROM bitrix_contacts WHERE email_normalized = %s AND id <> %s LIMIT 1",
                    (email, bitrix_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0], "same_email", {
                        "existing_name": f"{row[1] or ''} {row[2] or ''}".strip(),
                        "phone": row[3],
                        "email": row[4],
                    }
        return None, None, None

    def find_lead_similar(self, phone: str | None, email: str | None, bitrix_id: int) -> tuple[int | None, str | None, dict | None]:
        with self.conn.cursor() as cur:
            if phone:
                cur.execute(
                    "SELECT id, title, name, last_name FROM bitrix_leads WHERE phone_normalized = %s AND id <> %s LIMIT 1",
                    (phone, bitrix_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0], "same_phone", {
                        "existing_title": row[1],
                        "existing_name": f"{row[2] or ''} {row[3] or ''}".strip(),
                    }
            if email:
                cur.execute(
                    "SELECT id, title, name, last_name FROM bitrix_leads WHERE email_normalized = %s AND id <> %s LIMIT 1",
                    (email, bitrix_id),
                )
                row = cur.fetchone()
                if row:
                    return row[0], "same_email", {
                        "existing_title": row[1],
                        "existing_name": f"{row[2] or ''} {row[3] or ''}".strip(),
                    }
        return None, None, None

    def find_deal_duplicate(self, dedup_key: str) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM bitrix_deals WHERE dedup_key = %s AND order_number IS NOT NULL LIMIT 1",
                (dedup_key,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def find_deal_similar(self, order_number: str | None, products_signature: str, date_create: datetime | None, bitrix_id: int, title: str | None) -> list[tuple[int, str, dict]]:
        matches: list[tuple[int, str, dict]] = []
        date_part = date_create.date().isoformat() if date_create else None
        with self.conn.cursor() as cur:
            if order_number:
                cur.execute(
                    """
                    SELECT id, title, date_create::date, products_signature, opportunity
                    FROM bitrix_deals
                    WHERE order_number = %s AND id <> %s
                    LIMIT 5
                    """,
                    (order_number, bitrix_id),
                )
                for row in cur.fetchall():
                    other_date = row[2].isoformat() if row[2] else None
                    if other_date != date_part or row[3] != products_signature:
                        matches.append((
                            row[0],
                            "same_order_different_details",
                            {
                                "existing_title": row[1],
                                "existing_date": other_date,
                                "new_date": date_part,
                                "existing_products": row[3],
                                "new_products": products_signature,
                                "existing_opportunity": str(row[4]),
                            },
                        ))
            elif date_part and products_signature not in ("no-products", ""):
                cur.execute(
                    """
                    SELECT id, title, products_signature
                    FROM bitrix_deals
                    WHERE order_number IS NULL
                      AND date_create::date = %s::date
                      AND products_signature = %s
                      AND id <> %s
                    LIMIT 3
                    """,
                    (date_part, products_signature, bitrix_id),
                )
                for row in cur.fetchall():
                    matches.append((
                        row[0],
                        "same_day_same_products_no_order",
                        {
                            "existing_title": row[1],
                            "new_title": title,
                            "date": date_part,
                            "products": products_signature,
                        },
                    ))
        return matches

    def build_products_signature(self, product_rows: list[dict], fallback_comments: str | None) -> str:
        if product_rows:
            parts = []
            for row in sorted(product_rows, key=lambda x: str(x.get("PRODUCT_ID", ""))):
                parts.append(
                    f"{row.get('PRODUCT_ID')}:{row.get('PRODUCT_NAME', '')}:{row.get('QUANTITY', 0)}"
                )
            return "|".join(parts)
        if fallback_comments:
            return hashlib.md5(fallback_comments.encode("utf-8")).hexdigest()
        return "no-products"

    def build_deal_dedup_key(self, bitrix_id: int, order_number: str | None, products_signature: str, metric_date: datetime | None) -> str:
        if not order_number:
            return f"id:{bitrix_id}"
        date_part = metric_date.date().isoformat() if metric_date else "unknown-date"
        raw = f"{order_number}|{products_signature}|{date_part}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def sync_statuses(self) -> None:
        run_id = self.start_run("statuses")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.status.list")
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        cur.execute(
                            """
                            INSERT INTO bitrix_statuses (status_id, entity_id, name, sort, raw_data)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (status_id, entity_id) DO UPDATE SET
                                name = EXCLUDED.name,
                                sort = EXCLUDED.sort,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                item.get("STATUS_ID"),
                                item.get("ENTITY_ID"),
                                item.get("NAME"),
                                int(item.get("SORT") or 0),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_users(self) -> None:
        run_id = self.start_run("users")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("user.get", {"ACTIVE": True})
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        cur.execute(
                            """
                            INSERT INTO bitrix_users (id, name, last_name, email, active, raw_data)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                name = EXCLUDED.name,
                                last_name = EXCLUDED.last_name,
                                email = EXCLUDED.email,
                                active = EXCLUDED.active,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                int(item["ID"]),
                                item.get("NAME"),
                                item.get("LAST_NAME"),
                                item.get("EMAIL"),
                                item.get("ACTIVE") == True,
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_products(self) -> None:
        run_id = self.start_run("products")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.product.list", {"select": ["*"]})
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        cur.execute(
                            """
                            INSERT INTO bitrix_products (id, name, price, currency_id, active, raw_data)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                name = EXCLUDED.name,
                                price = EXCLUDED.price,
                                currency_id = EXCLUDED.currency_id,
                                active = EXCLUDED.active,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                int(item["ID"]),
                                item.get("NAME"),
                                item.get("PRICE"),
                                item.get("CURRENCY_ID"),
                                item.get("ACTIVE"),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_companies(self) -> None:
        run_id = self.start_run("companies")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.company.list", {"select": ["*"]})
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        phone = normalize_phone(extract_multi_field(item, "PHONE"))
                        email = normalize_email(extract_multi_field(item, "EMAIL"))
                        cur.execute(
                            """
                            INSERT INTO bitrix_companies (id, title, phone_normalized, email_normalized, date_create, date_modify, raw_data)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                title = EXCLUDED.title,
                                phone_normalized = EXCLUDED.phone_normalized,
                                email_normalized = EXCLUDED.email_normalized,
                                date_create = EXCLUDED.date_create,
                                date_modify = EXCLUDED.date_modify,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                int(item["ID"]),
                                item.get("TITLE"),
                                phone,
                                email,
                                parse_dt(item.get("DATE_CREATE")),
                                parse_dt(item.get("DATE_MODIFY")),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_contacts(self) -> None:
        run_id = self.start_run("contacts")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.contact.list", {"select": ["*"]})
            fetched = len(items)
            for item in items:
                try:
                    bitrix_id = int(item["ID"])
                    phone = normalize_phone(extract_multi_field(item, "PHONE"))
                    email = normalize_email(extract_multi_field(item, "EMAIL"))
                    similar_id, reason, details = self.find_contact_similar(phone, email, bitrix_id)
                    if similar_id:
                        details = details or {}
                        details["new_name"] = f"{item.get('NAME') or ''} {item.get('LAST_NAME') or ''}".strip()
                        self.log_review("contact", bitrix_id, similar_id, reason, phone or email, details)
                    with self.conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO bitrix_contacts (
                                id, name, last_name, second_name, phone_normalized, email_normalized,
                                company_id, assigned_by_id, date_create, date_modify, raw_data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                name = EXCLUDED.name,
                                last_name = EXCLUDED.last_name,
                                second_name = EXCLUDED.second_name,
                                phone_normalized = EXCLUDED.phone_normalized,
                                email_normalized = EXCLUDED.email_normalized,
                                company_id = EXCLUDED.company_id,
                                assigned_by_id = EXCLUDED.assigned_by_id,
                                date_create = EXCLUDED.date_create,
                                date_modify = EXCLUDED.date_modify,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                bitrix_id,
                                item.get("NAME"),
                                item.get("LAST_NAME"),
                                item.get("SECOND_NAME"),
                                phone,
                                email,
                                int(item["COMPANY_ID"]) if item.get("COMPANY_ID") else None,
                                int(item["ASSIGNED_BY_ID"]) if item.get("ASSIGNED_BY_ID") else None,
                                parse_dt(item.get("DATE_CREATE")),
                                parse_dt(item.get("DATE_MODIFY")),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                    inserted += 1
                except Exception:
                    errors += 1
                    self.conn.rollback()
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"contacts: fetched={fetched}, inserted={inserted}, duplicates={duplicates}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_leads(self) -> None:
        run_id = self.start_run("leads")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.lead.list", {"select": ["*", "UF_*", "UF_CRM_DELIVERYTRACK"]})
            fetched = len(items)
            for item in items:
                try:
                    bitrix_id = int(item["ID"])
                    phone = normalize_phone(extract_multi_field(item, "PHONE"))
                    email = normalize_email(extract_multi_field(item, "EMAIL"))
                    similar_id, reason, details = self.find_lead_similar(phone, email, bitrix_id)
                    if similar_id:
                        details = details or {}
                        details["new_title"] = item.get("TITLE")
                        details["new_name"] = f"{item.get('NAME') or ''} {item.get('LAST_NAME') or ''}".strip()
                        self.log_review("lead", bitrix_id, similar_id, reason, phone or email, details)
                    with self.conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO bitrix_leads (
                                id, title, name, last_name, phone_normalized, email_normalized,
                                status_id, source_id, origin_id, contact_id, opportunity, currency_id,
                                assigned_by_id, date_create, date_modify, raw_data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                title = EXCLUDED.title,
                                name = EXCLUDED.name,
                                last_name = EXCLUDED.last_name,
                                phone_normalized = EXCLUDED.phone_normalized,
                                email_normalized = EXCLUDED.email_normalized,
                                status_id = EXCLUDED.status_id,
                                source_id = EXCLUDED.source_id,
                                origin_id = EXCLUDED.origin_id,
                                contact_id = EXCLUDED.contact_id,
                                opportunity = EXCLUDED.opportunity,
                                currency_id = EXCLUDED.currency_id,
                                assigned_by_id = EXCLUDED.assigned_by_id,
                                date_create = EXCLUDED.date_create,
                                date_modify = EXCLUDED.date_modify,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                bitrix_id,
                                item.get("TITLE"),
                                item.get("NAME"),
                                item.get("LAST_NAME"),
                                phone,
                                email,
                                item.get("STATUS_ID"),
                                item.get("SOURCE_ID"),
                                item.get("ORIGIN_ID"),
                                int(item["CONTACT_ID"]) if item.get("CONTACT_ID") else None,
                                item.get("OPPORTUNITY"),
                                item.get("CURRENCY_ID"),
                                int(item["ASSIGNED_BY_ID"]) if item.get("ASSIGNED_BY_ID") else None,
                                parse_dt(item.get("DATE_CREATE")),
                                parse_dt(item.get("DATE_MODIFY")),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                    inserted += 1
                except Exception:
                    errors += 1
                    self.conn.rollback()
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"leads: fetched={fetched}, inserted={inserted}, duplicates={duplicates}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def fetch_deal_product_rows(self, deal_ids: list[int]) -> dict[int, list[dict]]:
        result: dict[int, list[dict]] = {deal_id: [] for deal_id in deal_ids}
        for i in range(0, len(deal_ids), BATCH_SIZE):
            chunk = deal_ids[i : i + BATCH_SIZE]
            if i and i % 1000 == 0:
                log(f"product rows: {i}/{len(deal_ids)}")
            commands = {f"d{deal_id}": f"crm.deal.productrows.get?id={deal_id}" for deal_id in chunk}
            batch_result = self.client.batch(commands)
            for deal_id in chunk:
                rows = batch_result.get(f"d{deal_id}")
                if isinstance(rows, list):
                    result[deal_id] = rows
        if deal_ids:
            log(f"product rows: {len(deal_ids)}/{len(deal_ids)}")
        return result

    def load_existing_deal_state(self, deal_ids: list[int]) -> dict[int, dict]:
        if not deal_ids:
            return {}
        state: dict[int, dict] = {}
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for i in range(0, len(deal_ids), 5000):
                chunk = deal_ids[i : i + 5000]
                cur.execute(
                    """
                    SELECT d.id, d.date_modify, d.products_signature, count(p.id) AS product_rows
                    FROM bitrix_deals d
                    LEFT JOIN bitrix_deal_products p ON p.deal_id = d.id
                    WHERE d.id = ANY(%s)
                    GROUP BY d.id, d.date_modify, d.products_signature
                    """,
                    (chunk,),
                )
                for row in cur.fetchall():
                    state[int(row["id"])] = dict(row)
        return state

    def sync_deals(self) -> None:
        run_id = self.start_run("deals")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.deal.list", {"select": ["*"]})
            fetched = len(items)
            deal_ids = [int(item["ID"]) for item in items]
            existing_state = self.load_existing_deal_state(deal_ids)
            changed_deal_ids: list[int] = []
            for item in items:
                bitrix_id = int(item["ID"])
                date_modify = parse_dt(item.get("DATE_MODIFY"))
                current = existing_state.get(bitrix_id)
                current_date_modify = current.get("date_modify") if current else None
                has_signature = bool(current and current.get("products_signature"))
                has_product_rows = bool(current and int(current.get("product_rows") or 0) > 0)
                if (
                    not current
                    or not has_signature
                    or not has_product_rows
                    or (date_modify and current_date_modify and date_modify > current_date_modify)
                ):
                    changed_deal_ids.append(bitrix_id)
            log(f"loading product rows for {len(changed_deal_ids)}/{len(deal_ids)} changed deals...")
            product_map = self.fetch_deal_product_rows(changed_deal_ids)
            changed_deal_set = set(changed_deal_ids)

            for item in items:
                try:
                    bitrix_id = int(item["ID"])
                    order_number = extract_order_number(item.get("TITLE"))
                    date_create = parse_dt(item.get("DATE_CREATE"))
                    date_modify = parse_dt(item.get("DATE_MODIFY"))
                    metric_date = parse_dt(item.get("BEGINDATE")) or date_create
                    product_rows = product_map.get(bitrix_id)
                    if product_rows is None and bitrix_id in existing_state:
                        products_signature = existing_state[bitrix_id].get("products_signature") or self.build_products_signature([], item.get("COMMENTS"))
                    else:
                        products_signature = self.build_products_signature(product_rows or [], item.get("COMMENTS"))
                    dedup_key = self.build_deal_dedup_key(bitrix_id, order_number, products_signature, metric_date)

                    if order_number:
                        dup_id = self.find_deal_duplicate(dedup_key)
                        if dup_id and dup_id != bitrix_id:
                            self.log_duplicate(
                                "deal",
                                bitrix_id,
                                "duplicate_by_order_products_date",
                                dup_id,
                                dedup_key,
                                item,
                            )
                            duplicates += 1
                            continue

                    with self.conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO bitrix_deals (
                                id, title, order_number, stage_id, category_id, opportunity, currency_id,
                                contact_id, lead_id, company_id, assigned_by_id, date_create, date_modify,
                                closed, products_signature, dedup_key, raw_data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                title = EXCLUDED.title,
                                order_number = EXCLUDED.order_number,
                                stage_id = EXCLUDED.stage_id,
                                category_id = EXCLUDED.category_id,
                                opportunity = EXCLUDED.opportunity,
                                currency_id = EXCLUDED.currency_id,
                                contact_id = EXCLUDED.contact_id,
                                lead_id = EXCLUDED.lead_id,
                                company_id = EXCLUDED.company_id,
                                assigned_by_id = EXCLUDED.assigned_by_id,
                                date_create = EXCLUDED.date_create,
                                date_modify = EXCLUDED.date_modify,
                                closed = EXCLUDED.closed,
                                products_signature = EXCLUDED.products_signature,
                                dedup_key = EXCLUDED.dedup_key,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                bitrix_id,
                                item.get("TITLE"),
                                order_number,
                                item.get("STAGE_ID"),
                                int(item["CATEGORY_ID"]) if item.get("CATEGORY_ID") else None,
                                item.get("OPPORTUNITY"),
                                item.get("CURRENCY_ID"),
                                int(item["CONTACT_ID"]) if item.get("CONTACT_ID") else None,
                                int(item["LEAD_ID"]) if item.get("LEAD_ID") else None,
                                int(item["COMPANY_ID"]) if item.get("COMPANY_ID") else None,
                                int(item["ASSIGNED_BY_ID"]) if item.get("ASSIGNED_BY_ID") else None,
                                date_create,
                                date_modify,
                                item.get("CLOSED"),
                                products_signature,
                                dedup_key,
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        if product_rows is not None:
                            cur.execute("DELETE FROM bitrix_deal_products WHERE deal_id = %s", (bitrix_id,))
                            for row in product_rows:
                                cur.execute(
                                    """
                                    INSERT INTO bitrix_deal_products (deal_id, product_id, product_name, quantity, price, raw_data)
                                    VALUES (%s, %s, %s, %s, %s, %s)
                                    """,
                                    (
                                        bitrix_id,
                                        int(row["PRODUCT_ID"]) if row.get("PRODUCT_ID") else None,
                                        row.get("PRODUCT_NAME"),
                                        row.get("QUANTITY"),
                                        row.get("PRICE"),
                                        json.dumps(row, ensure_ascii=False),
                                    ),
                                )
                    if bitrix_id in changed_deal_set:
                        for similar_id, reason, details in self.find_deal_similar(
                            order_number, products_signature, metric_date, bitrix_id, item.get("TITLE")
                        ):
                            self.log_review("deal", bitrix_id, similar_id, reason, order_number or details.get("date"), details)
                    inserted += 1
                except Exception:
                    errors += 1
                    self.conn.rollback()
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"deals: fetched={fetched}, inserted={inserted}, duplicates={duplicates}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_activities(self) -> None:
        run_id = self.start_run("activities")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("crm.activity.list", {"select": ["*"]})
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        cur.execute(
                            """
                            INSERT INTO bitrix_activities (
                                id, owner_type_id, owner_id, type_id, provider_id, subject,
                                responsible_id, completed, status, direction,
                                created_at, last_updated, deadline, raw_data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                owner_type_id = EXCLUDED.owner_type_id,
                                owner_id = EXCLUDED.owner_id,
                                type_id = EXCLUDED.type_id,
                                provider_id = EXCLUDED.provider_id,
                                subject = EXCLUDED.subject,
                                responsible_id = EXCLUDED.responsible_id,
                                completed = EXCLUDED.completed,
                                status = EXCLUDED.status,
                                direction = EXCLUDED.direction,
                                created_at = EXCLUDED.created_at,
                                last_updated = EXCLUDED.last_updated,
                                deadline = EXCLUDED.deadline,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                int(item["ID"]),
                                int(item["OWNER_TYPE_ID"]) if item.get("OWNER_TYPE_ID") else None,
                                int(item["OWNER_ID"]) if item.get("OWNER_ID") else None,
                                int(item["TYPE_ID"]) if item.get("TYPE_ID") else None,
                                item.get("PROVIDER_ID"),
                                item.get("SUBJECT"),
                                int(item["RESPONSIBLE_ID"]) if item.get("RESPONSIBLE_ID") else None,
                                item.get("COMPLETED"),
                                item.get("STATUS"),
                                item.get("DIRECTION"),
                                parse_dt(item.get("CREATED")),
                                parse_dt(item.get("LAST_UPDATED")),
                                parse_dt(item.get("DEADLINE")),
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"activities: fetched={fetched}, inserted={inserted}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_tasks(self) -> None:
        run_id = self.start_run("tasks")
        fetched = inserted = duplicates = errors = 0
        try:
            items = self.client.list_all("tasks.task.list")
            fetched = len(items)
            with self.conn.cursor() as cur:
                for item in items:
                    try:
                        task_id = int(item.get("id") or item.get("ID"))
                        cur.execute(
                            """
                            INSERT INTO bitrix_tasks (
                                id, title, status, priority, responsible_id, created_by,
                                created_date, closed_date, deadline, group_id, raw_data
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                title = EXCLUDED.title,
                                status = EXCLUDED.status,
                                priority = EXCLUDED.priority,
                                responsible_id = EXCLUDED.responsible_id,
                                created_by = EXCLUDED.created_by,
                                created_date = EXCLUDED.created_date,
                                closed_date = EXCLUDED.closed_date,
                                deadline = EXCLUDED.deadline,
                                group_id = EXCLUDED.group_id,
                                raw_data = EXCLUDED.raw_data,
                                synced_at = NOW()
                            """,
                            (
                                task_id,
                                item.get("title") or item.get("TITLE"),
                                int(item["status"]) if item.get("status") is not None else None,
                                int(item["priority"]) if item.get("priority") is not None else None,
                                int(item["responsibleId"]) if item.get("responsibleId") else None,
                                int(item["createdBy"]) if item.get("createdBy") else None,
                                parse_dt(item.get("createdDate") or item.get("CREATED_DATE")),
                                parse_dt(item.get("closedDate") or item.get("CLOSED_DATE")),
                                parse_dt(item.get("deadline") or item.get("DEADLINE")),
                                int(item["groupId"]) if item.get("groupId") else None,
                                json.dumps(item, ensure_ascii=False),
                            ),
                        )
                        inserted += 1
                    except Exception:
                        errors += 1
            self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"tasks: fetched={fetched}, inserted={inserted}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def sync_stage_history(self) -> None:
        run_id = self.start_run("stage_history")
        fetched = inserted = duplicates = errors = 0
        try:
            for entity_type_id, label in ((2, "deal"), (1, "lead")):
                log(f"stage history for {label} (entityTypeId={entity_type_id})")
                items = self.client.list_all(
                    "crm.stagehistory.list",
                    {"entityTypeId": entity_type_id},
                )
                fetched += len(items)
                with self.conn.cursor() as cur:
                    for item in items:
                        try:
                            cur.execute(
                                """
                                INSERT INTO bitrix_stage_history (
                                    id, entity_type_id, owner_id, type_id, stage_id,
                                    stage_semantic_id, category_id, created_time, raw_data
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (id, entity_type_id) DO UPDATE SET
                                    owner_id = EXCLUDED.owner_id,
                                    type_id = EXCLUDED.type_id,
                                    stage_id = EXCLUDED.stage_id,
                                    stage_semantic_id = EXCLUDED.stage_semantic_id,
                                    category_id = EXCLUDED.category_id,
                                    created_time = EXCLUDED.created_time,
                                    raw_data = EXCLUDED.raw_data,
                                    synced_at = NOW()
                                """,
                                (
                                    int(item["ID"]),
                                    entity_type_id,
                                    int(item["OWNER_ID"]) if item.get("OWNER_ID") else None,
                                    int(item["TYPE_ID"]) if item.get("TYPE_ID") else None,
                                    item.get("STAGE_ID"),
                                    item.get("STAGE_SEMANTIC_ID"),
                                    int(item["CATEGORY_ID"]) if item.get("CATEGORY_ID") else None,
                                    parse_dt(item.get("CREATED_TIME")),
                                    json.dumps(item, ensure_ascii=False),
                                ),
                            )
                            inserted += 1
                        except Exception:
                            errors += 1
                self.conn.commit()
            self.finish_run(run_id, fetched, inserted, duplicates, errors, "done")
            log(f"stage_history: fetched={fetched}, inserted={inserted}, errors={errors}")
        except Exception as exc:
            self.conn.rollback()
            self.finish_run(run_id, fetched, inserted, duplicates, errors + 1, f"failed: {exc}")
            raise

    def run_all(self, only: str | None = None) -> None:
        self.init_schema()
        steps = {
            "statuses": self.sync_statuses,
            "users": self.sync_users,
            "products": self.sync_products,
            "companies": self.sync_companies,
            "contacts": self.sync_contacts,
            "leads": self.sync_leads,
            "deals": self.sync_deals,
            "activities": self.sync_activities,
            "tasks": self.sync_tasks,
            "stage_history": self.sync_stage_history,
        }
        selected = [only] if only else list(steps.keys())
        for name in selected:
            if name not in steps:
                raise ValueError(f"Unknown entity: {name}")
            log(f"sync {name}")
            steps[name]()
        log("sync complete")


def main() -> int:
    if not WEBHOOK or WEBHOOK == "/":
        log("BITRIX_WEBHOOK is not set")
        return 1
    only = os.environ.get("SYNC_ONLY") or (sys.argv[1] if len(sys.argv) > 1 else None)
    client = BitrixClient(WEBHOOK)
    conn = psycopg2.connect(DATABASE_URL)
    try:
        SyncService(conn, client).run_all(only=only)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
