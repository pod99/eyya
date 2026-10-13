import json
import re
from typing import Any


MOJIBAKE_MARKERS = ("Р", "С", "Ð", "Ñ")


def fix_mojibake_text(value: str) -> str:
    if not any(marker in value for marker in MOJIBAKE_MARKERS):
        return value
    for source_encoding in ("cp1251", "latin1"):
        try:
            fixed = value.encode(source_encoding).decode("utf-8")
        except UnicodeError:
            continue
        if fixed != value and fixed.count("�") == 0:
            return fixed
    return value


def normalize_payload_text(value: Any) -> Any:
    if isinstance(value, str):
        return fix_mojibake_text(value)
    if isinstance(value, list):
        return [normalize_payload_text(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_payload_text(item) for key, item in value.items()}
    return value


def first_value(payload: dict[str, Any], keys: list[str]) -> str | None:
    lower_map = {str(key).lower(): value for key, value in payload.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value not in (None, ""):
            if isinstance(value, list):
                return str(value[0]) if value else None
            return str(value)
    return None


def nested_value(payload: dict[str, Any], path: list[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def first_nested_value(payload: dict[str, Any], paths: list[list[str]]) -> str | None:
    for path in paths:
        value = nested_value(payload, path)
        if value not in (None, ""):
            return str(value)
    return None


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D+", "", value)
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    return digits or None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().lower()
    return email or None


def normalize_amount(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return f"{float(value):.2f}"
    except (ValueError, TypeError):
        return str(value)


def normalize_products_text(products: list[dict[str, Any]] | None) -> str | None:
    if not products:
        return None
    lines = []
    for p in products:
        name = p.get("name", "?")
        sku = p.get("sku", "")
        try:
            qty = int(float(p.get("quantity", 1)))
        except (ValueError, TypeError):
            qty = 1
        try:
            price_val = float(p.get("price", p.get("amount", 0)))
        except (ValueError, TypeError):
            price_val = 0
        label = f"{name} ({sku})" if sku else name
        lines.append(f"{label} - {qty} x {int(price_val)} = {qty * int(price_val)}")
    return "\n".join(lines)


def normalize_products_json(products: list[dict[str, Any]] | None) -> str | None:
    if not products:
        return None
    clean = []
    for p in products:
        clean.append({
            "name": p.get("name", ""),
            "quantity": p.get("quantity", 1),
            "amount": p.get("amount", 0),
            "price": str(p.get("price", "")),
            "sku": p.get("sku", ""),
        })
    return json.dumps(clean, ensure_ascii=False)


def normalize_cookies(cookies: str | None, max_len: int = 2000) -> str | None:
    if not cookies:
        return None
    if len(cookies) <= max_len:
        return cookies
    return cookies[:max_len] + "..."


def extract_tilda_fields(payload: dict[str, Any]) -> dict[str, str | None]:
    payment = payload.get("payment") if isinstance(payload.get("payment"), dict) else {}
    products = payment.get("products") if isinstance(payment.get("products"), list) else None

    return {
        "form_id": first_value(payload, ["formid", "form_id", "tilda_form_id"]),
        "form_name": first_value(payload, ["formname", "form_name"]),
        "order_id": first_value(payload, ["orderid", "order_id", "tilda_orderid"])
        or first_nested_value(payload, [["payment", "orderid"], ["payment", "order_id"]]),
        "name": first_value(payload, ["name", "Name", "firstname", "first_name"]),
        "phone": normalize_phone(first_value(payload, ["phone", "Phone"])),
        "email": normalize_email(first_value(payload, ["email", "Email"])),
        "checkbox": first_value(payload, ["Checkbox", "checkbox"]),
        "page_url": first_value(payload, ["referer", "page", "page_url", "url"]),
        "payment_status": first_value(payload, ["payment_status"]),
        "payment_id": first_value(payload, ["payment_id", "paymentid"])
        or first_nested_value(payload, [["payment", "paymentid"], ["payment", "payment_id"]]),
        "transaction_id": first_value(payload, ["transaction_id", "transactionid"])
        or first_nested_value(payload, [["payment", "transactionid"], ["payment", "transaction_id"]]),
        "amount": normalize_amount(first_value(payload, ["amount", "sum", "price", "total"])
                                   or payment.get("amount")),
        "subtotal": normalize_amount(payment.get("subtotal")),
        "delivery": str(payment.get("delivery") or "") or None,
        "delivery_price": normalize_amount(payment.get("delivery_price")),
        "delivery_city": str(payment.get("delivery_city") or "") or None,
        "delivery_address": str(payment.get("delivery_address") or "") or None,
        "delivery_pickup_id": str(payment.get("delivery_pickup_id") or "") or None,
        "delivery_zip": str(payment.get("delivery_zip") or "") or None,
        "delivery_fio": str(payment.get("delivery_fio") or "") or None,
        "delivery_comment": str(payment.get("delivery_comment") or "") or None,
        "products": products,
        "products_text": normalize_products_text(products),
        "products_json": normalize_products_json(products),
        "cookies": normalize_cookies(first_value(payload, ["COOKIES", "cookies"])),
        "promocode": first_value(payload, ["promocode", "promo", "coupon"]),
        "client_id": first_value(payload, ["client_id", "clientid", "tildaspec-cookie"]),
        "utm_source": first_value(payload, ["utm_source"]),
        "utm_medium": first_value(payload, ["utm_medium"]),
        "utm_campaign": first_value(payload, ["utm_campaign"]),
        "utm_content": first_value(payload, ["utm_content"]),
        "utm_term": first_value(payload, ["utm_term"]),
    }
