from typing import Any
from datetime import datetime, timezone
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class BitrixClient:
    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = (webhook_url or settings.bitrix_webhook_url or "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("BITRIX_WEBHOOK_URL is not configured")

        base = self.webhook_url.rstrip("/")
        url = f"{base}/{method}.json"
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(url, json=payload or {})
            response.raise_for_status()
            data = response.json()
        if "error" in data:
            description = data.get("error_description") or data["error"]
            raise RuntimeError(f"Bitrix API error: {description}")
        return data


def build_lead_fields(fields: dict[str, str | None], request_id: int | None = None) -> dict[str, Any]:
    order_id = fields.get("order_id") or "без номера"
    name = fields.get("name") or "Клиент Tilda"
    lead: dict[str, Any] = {
        "TITLE": f"Заказ Tilda {order_id}",
        "SOURCE_ID": "WEB",
        "ORIGINATOR_ID": "tilda",
        "ORIGIN_ID": order_id,
        "CURRENCY_ID": settings.default_currency,
        "IS_MANUAL_OPPORTUNITY": "Y",
    }

    if settings.responsible_user_id:
        lead["ASSIGNED_BY_ID"] = int(settings.responsible_user_id)

    if fields.get("page_url"):
        lead["SOURCE_DESCRIPTION"] = fields["page_url"]

    if fields.get("phone"):
        lead["PHONE"] = [{"VALUE": fields["phone"], "VALUE_TYPE": "WORK"}]
    if fields.get("email"):
        lead["EMAIL"] = [{"VALUE": fields["email"], "VALUE_TYPE": "WORK"}]
    if fields.get("amount"):
        lead["OPPORTUNITY"] = fields["amount"]

    if fields.get("delivery_city"):
        lead["ADDRESS_CITY"] = fields["delivery_city"]
    if fields.get("delivery_address"):
        lead["ADDRESS"] = fields["delivery_address"]
    if fields.get("delivery_zip"):
        lead["ADDRESS_POSTAL_CODE"] = fields["delivery_zip"]

    for utm_key in ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]:
        value = fields.get(utm_key)
        if value:
            lead[utm_key.upper()] = value

    comments_lines = [
        "Заявка из Tilda",
        f"Форма: {fields.get('form_name') or fields.get('form_id') or '-'}",
        f"orderid: {order_id}",
        f"Страница: {fields.get('page_url') or '-'}",
        f"Доставка: {fields.get('delivery') or '-'}",
    ]
    lead["COMMENTS"] = "\n".join(comments_lines)

    uf_map = {
        settings.bitrix_tilda_order_field: fields.get("order_id"),
        settings.bitrix_form_field: fields.get("form_id"),
        settings.bitrix_formname_field: fields.get("form_name"),
        settings.bitrix_amount_field: fields.get("amount"),
        settings.bitrix_subtotal_field: fields.get("subtotal"),
        settings.bitrix_delivery_field: fields.get("delivery"),
        settings.bitrix_delivery_price_field: fields.get("delivery_price"),
        settings.bitrix_delivery_city_field: fields.get("delivery_city"),
        settings.bitrix_delivery_address_field: fields.get("delivery_address"),
        settings.bitrix_delivery_pickup_id_field: fields.get("delivery_pickup_id"),
        settings.bitrix_delivery_zip_field: fields.get("delivery_zip"),
        settings.bitrix_delivery_fio_field: fields.get("delivery_fio"),
        settings.bitrix_delivery_comment_field: fields.get("delivery_comment"),
        settings.bitrix_product_field: fields.get("products_text"),
        settings.bitrix_products_json_field: fields.get("products_json"),
        settings.bitrix_cookies_field: fields.get("cookies"),
        settings.bitrix_checkbox_field: fields.get("checkbox"),
        settings.bitrix_request_id_field: str(request_id) if request_id else None,
        settings.bitrix_promocode_field: fields.get("promocode"),
    }

    payment_status = (fields.get("payment_status") or "").lower()
    payment_id = fields.get("payment_id")
    transaction_id = fields.get("transaction_id")

    if payment_status == "paid" and payment_id:
        uf_map[settings.bitrix_payment_id_field] = payment_id
    else:
        uf_map[settings.bitrix_payment_id_field] = ""

    if payment_status == "paid" and transaction_id:
        uf_map[settings.bitrix_transaction_id_field] = transaction_id
    else:
        uf_map[settings.bitrix_transaction_id_field] = ""

    uf_map[settings.bitrix_payment_status_field] = fields.get("payment_status") or "unknown"

    for key, value in uf_map.items():
        if key and value not in (None, ""):
            lead[key] = value

    return lead


def build_lead_update_fields(fields: dict[str, str | None], request_id: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {}

    if fields.get("name"):
        result["NAME"] = fields["name"]
    if settings.responsible_user_id:
        result["ASSIGNED_BY_ID"] = int(settings.responsible_user_id)

    uf_map = {
        settings.bitrix_tilda_order_field: fields.get("order_id"),
        settings.bitrix_form_field: fields.get("form_id"),
        settings.bitrix_formname_field: fields.get("form_name"),
        settings.bitrix_amount_field: fields.get("amount"),
        settings.bitrix_subtotal_field: fields.get("subtotal"),
        settings.bitrix_delivery_field: fields.get("delivery"),
        settings.bitrix_delivery_price_field: fields.get("delivery_price"),
        settings.bitrix_delivery_city_field: fields.get("delivery_city"),
        settings.bitrix_delivery_address_field: fields.get("delivery_address"),
        settings.bitrix_delivery_pickup_id_field: fields.get("delivery_pickup_id"),
        settings.bitrix_delivery_zip_field: fields.get("delivery_zip"),
        settings.bitrix_delivery_fio_field: fields.get("delivery_fio"),
        settings.bitrix_delivery_comment_field: fields.get("delivery_comment"),
        settings.bitrix_product_field: fields.get("products_text"),
        settings.bitrix_products_json_field: fields.get("products_json"),
        settings.bitrix_cookies_field: fields.get("cookies"),
        settings.bitrix_checkbox_field: fields.get("checkbox"),
        settings.bitrix_request_id_field: str(request_id) if request_id else None,
    }

    payment_status = (fields.get("payment_status") or "").lower()
    payment_id = fields.get("payment_id")
    transaction_id = fields.get("transaction_id")

    if payment_status == "paid" and payment_id:
        uf_map[settings.bitrix_payment_id_field] = payment_id
    else:
        uf_map[settings.bitrix_payment_id_field] = ""

    if payment_status == "paid" and transaction_id:
        uf_map[settings.bitrix_transaction_id_field] = transaction_id
    else:
        uf_map[settings.bitrix_transaction_id_field] = ""

    uf_map[settings.bitrix_payment_status_field] = fields.get("payment_status") or "unknown"

    for key, value in uf_map.items():
        if key and value not in (None, ""):
            result[key] = value

    if fields.get("page_url"):
        result["SOURCE_DESCRIPTION"] = fields["page_url"]

    if fields.get("amount"):
        result["OPPORTUNITY"] = fields["amount"]
        result["CURRENCY_ID"] = settings.default_currency
        result["IS_MANUAL_OPPORTUNITY"] = "Y"

    if fields.get("phone"):
        result["PHONE"] = [{"VALUE": fields["phone"], "VALUE_TYPE": "WORK"}]
    if fields.get("email"):
        result["EMAIL"] = [{"VALUE": fields["email"], "VALUE_TYPE": "WORK"}]

    if fields.get("delivery_city"):
        result["ADDRESS_CITY"] = fields["delivery_city"]
    if fields.get("delivery_address"):
        result["ADDRESS"] = fields["delivery_address"]
    if fields.get("delivery_zip"):
        result["ADDRESS_POSTAL_CODE"] = fields["delivery_zip"]

    for utm_key in ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]:
        if fields.get(utm_key):
            result[utm_key.upper()] = fields[utm_key]

    return result


def build_log_text(
    request_id: int | None,
    action: str,
    bitrix_lead_id: int | None,
    fields: dict[str, str | None],
    result: str,
    error: str | None = None,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        f"[{now}]",
        f"request_id={request_id or '?'}",
        "source=Tilda",
        f"order_id={fields.get('order_id') or '?'}",
        f"action={action}",
    ]
    if bitrix_lead_id:
        parts.append(f"bitrix_lead_id={bitrix_lead_id}")
    parts.append(f"payment_status={fields.get('payment_status') or 'unknown'}")
    parts.append(f"result={result}")
    if error:
        parts.append(f"error={error}")
    return "; ".join(parts)


PRODUCT_NAME_TO_ID: dict[str, int] = {}


async def _load_product_catalog(client: BitrixClient) -> dict[str, int]:
    if PRODUCT_NAME_TO_ID:
        return PRODUCT_NAME_TO_ID
    try:
        data = await client.call("crm.product.list", {
            "select": ["ID", "NAME", "SKU", "CATALOG_ID"],
            "start": 0,
        })
        for p in data.get("result", []):
            name = (p.get("NAME") or "").strip()
            pid = int(p["ID"])
            if name:
                PRODUCT_NAME_TO_ID[name.lower()] = pid
            sku = (p.get("SKU") or "").strip()
            if sku:
                PRODUCT_NAME_TO_ID[sku.lower()] = pid
    except Exception as e:
        logger.warning("Failed to load product catalog: %s", e)
    return PRODUCT_NAME_TO_ID


async def find_existing_lead(client: BitrixClient, fields: dict[str, str | None]) -> int | None:
    order_id = fields.get("order_id")
    if order_id:
        data = await client.call(
            "crm.lead.list",
            {
                "filter": {
                    "ORIGINATOR_ID": "tilda",
                    "ORIGIN_ID": order_id,
                },
                "select": ["ID", "ORIGINATOR_ID", "ORIGIN_ID"],
                "order": {"ID": "DESC"},
            },
        )
        result = data.get("result") or []
        if result:
            return int(result[0]["ID"])
    return None


async def upsert_lead(
    client: BitrixClient,
    fields: dict[str, str | None],
    request_id: int | None = None,
) -> tuple[str, int, dict[str, Any]]:
    lead_id = await find_existing_lead(client, fields)
    lead_fields = build_lead_fields(fields, request_id)

    if lead_id:
        update_fields = {k: v for k, v in lead_fields.items() if k not in ("ORIGINATOR_ID", "ORIGIN_ID", "ASSIGNED_BY_ID")}
        data = await client.call("crm.lead.update", {"id": lead_id, "fields": update_fields})
        return "updated", lead_id, data

    data = await client.call("crm.lead.add", {"fields": lead_fields})
    created_id = int(data["result"])
    return "created", created_id, data


async def fix_lead_fields(client: BitrixClient, lead_id: int, fields: dict[str, str | None], request_id: int | None = None) -> None:
    log_text = build_log_text(request_id, "fix", lead_id, fields, "success")

    updates = [
        {settings.log_field_code: log_text},
        {settings.bitrix_tilda_order_field: fields["order_id"]} if fields.get("order_id") else None,
        {settings.bitrix_form_field: fields["form_id"]} if fields.get("form_id") else None,
        {settings.bitrix_formname_field: fields["form_name"]} if fields.get("form_name") else None,
        {"NAME": fields["name"]} if fields.get("name") else None,
    ]

    for upd in updates:
        if not upd:
            continue
        try:
            await client.call("crm.lead.update", {"id": lead_id, "fields": upd})
        except Exception as e:
            logger.warning("fix_lead_fields %s failed: %s", list(upd.keys())[0], e)


async def find_existing_deal(client: BitrixClient, fields: dict[str, str | None]) -> int | None:
    order_id = fields.get("order_id")
    if not order_id:
        return None
    try:
        data = await client.call(
            "crm.deal.list",
            {
                "filter": {"TITLE": f"Заказ Tilda {order_id}"},
                "select": ["ID"],
                "order": {"ID": "DESC"},
            },
        )
        result = data.get("result") or []
        if result:
            return int(result[0]["ID"])
    except Exception:
        pass
    return None


async def create_deal(client: BitrixClient, lead_id: int, fields: dict[str, str | None]) -> int | None:
    order_id = fields.get("order_id") or "без номера"
    deal_fields: dict[str, Any] = {
        "TITLE": f"Заказ Tilda {order_id}",
        "LEAD_ID": lead_id,
        "STAGE_ID": "NEW",
        "CATEGORY_ID": 0,
        "CURRENCY_ID": settings.default_currency,
        "OPPORTUNITY": fields.get("amount") or "0",
        "IS_MANUAL_OPPORTUNITY": "Y",
        "SOURCE_ID": "WEB",
        "TYPE_ID": "SALE",
    }
    if settings.responsible_user_id:
        deal_fields["ASSIGNED_BY_ID"] = int(settings.responsible_user_id)

    if fields.get("delivery"):
        deal_fields["COMMENTS"] = f"Доставка: {fields['delivery']}\nГород: {fields.get('delivery_city', '-')}\nАдрес: {fields.get('delivery_address', '-')}"

    try:
        data = await client.call("crm.deal.add", {"fields": deal_fields})
        deal_id = int(data["result"])
        return deal_id
    except Exception as e:
        logger.warning("create_deal failed: %s", e)
        return None


async def set_deal_product_rows(client: BitrixClient, deal_id: int, fields: dict[str, str | None]) -> bool:
    catalog = await _load_product_catalog(client)
    rows = []

    products = fields.get("products")
    if products:
        for p in products:
            try:
                qty = int(float(p.get("quantity", 1)))
            except (ValueError, TypeError):
                qty = 1
            row: dict[str, Any] = {
                "PRODUCT_NAME": p.get("name", ""),
                "PRICE": str(p.get("price", p.get("amount", 0))),
                "QUANTITY": qty,
                "MEASURE_CODE": 796,
            }
            sku = (p.get("sku") or "").strip().lower()
            name = (p.get("name") or "").strip().lower()
            if sku in catalog:
                row["PRODUCT_ID"] = catalog[sku]
                row["CATALOG_ID"] = 24
            elif name in catalog:
                row["PRODUCT_ID"] = catalog[name]
                row["CATALOG_ID"] = 24
            rows.append(row)

    delivery_price = fields.get("delivery_price") or "0"
    delivery_name = "Доставка"
    delivery_id = catalog.get("доставка")
    delivery_row: dict[str, Any] = {
        "PRODUCT_NAME": delivery_name,
        "PRICE": str(delivery_price),
        "QUANTITY": 1,
        "MEASURE_CODE": 796,
    }
    if delivery_id:
        delivery_row["PRODUCT_ID"] = delivery_id
        delivery_row["CATALOG_ID"] = 24
    rows.append(delivery_row)

    if not rows:
        return False

    try:
        await client.call("crm.deal.productrows.set", {"id": deal_id, "rows": rows})
        return True
    except Exception as e:
        logger.warning("set_deal_product_rows failed: %s", e)
        return False


async def add_timeline_comment(client: BitrixClient, lead_id: int, fields: dict[str, str | None]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "Получена заявка Tilda",
        f"Дата: {now}",
        f"Форма: {fields.get('form_name') or fields.get('form_id') or '-'}",
        f"Страница: {fields.get('page_url') or '-'}",
        f"Order ID: {fields.get('order_id') or '-'}",
        f"Статус оплаты: {fields.get('payment_status') or '-'}",
        f"Доставка: {fields.get('delivery') or '-'}",
        f"Город: {fields.get('delivery_city') or '-'}",
        f"Адрес: {fields.get('delivery_address') or '-'}",
        f"Товары: {fields.get('products_text') or '-'}",
    ]
    return await client.call(
        "crm.timeline.comment.add",
        {"fields": {"ENTITY_ID": lead_id, "ENTITY_TYPE": "lead", "COMMENT": "\n".join(lines)}},
    )
