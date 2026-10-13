import asyncio
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.bitrix import (
    BitrixClient,
    add_timeline_comment,
    create_deal,
    find_existing_deal,
    fix_lead_fields,
    set_deal_product_rows,
    upsert_lead,
)
from app.config import settings
from app.db import get_db, init_db
from app.models import IntegrationLog, RetryQueue, TildaRequest
from app.normalization import extract_tilda_fields, normalize_payload_text

logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

RETRY_INTERVALS = [
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=1),
    timedelta(hours=1),
]


def utf8_json(data: dict[str, Any], status_code: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        content=json.dumps(data, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


@app.on_event("startup")
def startup() -> None:
    init_db()
    asyncio.get_event_loop().create_task(_retry_worker())


def log_event(
    db: Session,
    request_id: int | None,
    action: str,
    status: str,
    rest_method: str | None = None,
    payload: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    db.add(
        IntegrationLog(
            request_id=request_id,
            action=action,
            rest_method=rest_method,
            status=status,
            payload=payload,
            response=response,
            error=error,
        )
    )
    db.commit()


async def parse_payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            return {}
        payload = body if isinstance(body, dict) else {"payload": body}
        return normalize_payload_text(payload)
    try:
        form = await request.form()
    except Exception:
        return {}
    return normalize_payload_text({key: value for key, value in form.multi_items()})


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "bitrix_configured": BitrixClient().enabled,
        "webhook_url": f"{settings.public_base_url.rstrip('/')}/api/tilda/orders",
    }


def require_inspect_token(token: str | None) -> None:
    if settings.inspect_token and token != settings.inspect_token:
        raise HTTPException(status_code=403, detail="Invalid inspect token")


@app.get("/api/tilda/latest")
def latest_tilda_request(
    token: str | None = None, db: Session = Depends(get_db)
) -> Response:
    require_inspect_token(token)
    headers = {"Cache-Control": "no-store, max-age=0"}
    record = db.query(TildaRequest).order_by(TildaRequest.id.desc()).first()
    if not record:
        return utf8_json({"ok": True, "latest": None, "fields": []}, headers=headers)
    raw_payload = normalize_payload_text(record.raw_payload)
    return utf8_json(
        {
            "ok": True,
            "latest": {
                "id": record.id,
                "status": record.status,
                "tilda_form_id": record.tilda_form_id,
                "tilda_order_id": record.tilda_order_id,
                "phone": record.phone,
                "email": record.email,
                "raw_payload": raw_payload,
                "created_at": record.created_at.isoformat(),
            },
            "fields": sorted(raw_payload.keys()),
        },
        headers=headers,
    )


@app.get("/api/tilda/requests")
def list_requests(
    token: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> Response:
    require_inspect_token(token)
    q = db.query(TildaRequest).order_by(TildaRequest.id.desc())
    if status:
        q = q.filter(TildaRequest.status == status)
    records = q.limit(limit).all()
    return utf8_json({
        "ok": True,
        "count": len(records),
        "items": [
            {
                "id": r.id,
                "tilda_order_id": r.tilda_order_id,
                "phone": r.phone,
                "email": r.email,
                "status": r.status,
                "bitrix_entity_id": r.bitrix_entity_id,
                "error": r.error,
                "retry_count": r.retry_count,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    })


@app.post("/api/tilda/orders")
async def tilda_orders(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    x_secret = request.headers.get("x-webhook-secret", "")
    if settings.bitrix_webhook_secret and not hmac.compare_digest(x_secret, settings.bitrix_webhook_secret):
        return JSONResponse({"ok": False, "status": "error", "error": "Invalid secret"}, status_code=403)

    payload = await parse_payload(request)
    if not payload:
        return JSONResponse({"ok": False, "status": "error", "error": "Empty or invalid payload"}, status_code=400)
    fields = extract_tilda_fields(payload)

    record = TildaRequest(
        tilda_form_id=fields.get("form_id"),
        tilda_form_name=fields.get("form_name"),
        tilda_order_id=fields.get("order_id"),
        phone=fields.get("phone"),
        email=fields.get("email"),
        status="received",
        raw_payload=payload,
        normalized_payload=fields,
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    log_event(db, record.id, "receive", "ok", payload=fields)

    errors = []
    if not fields.get("order_id"):
        errors.append("payment.orderid is required")
    if not fields.get("phone") and not fields.get("email"):
        errors.append("phone or email is required")
    if not fields.get("form_id") and not fields.get("form_name"):
        errors.append("formid or formname is required")

    if errors:
        record.status = "validation_error"
        record.error = "; ".join(errors)
        db.commit()
        log_event(db, record.id, "validate", "error", error=record.error)
        return JSONResponse({"ok": False, "status": "validation_error", "error": "; ".join(errors)}, status_code=422)

    client = BitrixClient()
    if not client.enabled:
        record.status = "pending_bitrix_config"
        db.commit()
        log_event(db, record.id, "bitrix_skip", "pending", payload=fields, error="BITRIX_WEBHOOK_URL is empty")
        return JSONResponse(
            {
                "ok": True,
                "request_id": record.id,
                "status": record.status,
                "message": "Request saved. Configure BITRIX_WEBHOOK_URL to enable CRM sync.",
            }
        )

    try:
        action, lead_id, response = await upsert_lead(client, fields, request_id=record.id)
        record.status = action
        record.bitrix_entity_type = "lead"
        record.bitrix_entity_id = str(lead_id)
        db.commit()
        log_event(db, record.id, f"lead.{action}", "ok", rest_method=f"crm.lead.{action}", payload=fields, response=response)

        await asyncio.sleep(1)
        await fix_lead_fields(client, lead_id, fields, record.id)
        log_event(db, record.id, "lead.fix_fields", "ok", rest_method="crm.lead.update")

        deal_id = await find_existing_deal(client, fields)
        if not deal_id:
            deal_id = await create_deal(client, lead_id, fields)
        if deal_id:
            log_event(db, record.id, "deal.created", "ok", rest_method="crm.deal.add", response={"deal_id": deal_id})
            ok = await set_deal_product_rows(client, deal_id, fields)
            log_event(db, record.id, "deal.productrows", "ok" if ok else "skip", rest_method="crm.deal.productrows.set")

        comment_response = await add_timeline_comment(client, lead_id, fields)
        log_event(
            db,
            record.id,
            "timeline_comment",
            "ok",
            rest_method="crm.timeline.comment.add",
            payload={"lead_id": lead_id},
            response=comment_response,
        )
        return JSONResponse({"ok": True, "request_id": record.id, "lead_id": lead_id, "deal_id": deal_id, "action": action})
    except Exception as exc:
        record.status = "bitrix_error"
        record.error = str(exc)
        record.retry_count = 0
        db.commit()
        log_event(db, record.id, "bitrix_sync", "error", payload=fields, error=str(exc))

        _enqueue_retry(db, record, fields)
        return JSONResponse({"ok": False, "request_id": record.id, "error": str(exc)}, status_code=502)


def _enqueue_retry(db: Session, record: TildaRequest, fields: dict[str, Any]) -> None:
    interval = RETRY_INTERVALS[min(record.retry_count, len(RETRY_INTERVALS) - 1)]
    db.add(RetryQueue(
        request_id=record.id,
        payload=fields,
        retry_count=record.retry_count,
        status="pending",
        next_retry_at=datetime.now(timezone.utc) + interval,
    ))
    db.commit()


async def _process_retry(retry: RetryQueue, db: Session) -> None:
    record = db.query(TildaRequest).filter(TildaRequest.id == retry.request_id).first()
    if not record:
        retry.status = "failed"
        db.commit()
        return

    client = BitrixClient()
    if not client.enabled:
        retry.status = "failed"
        retry.last_error = "BITRIX_WEBHOOK_URL is empty"
        db.commit()
        return

    try:
        action, lead_id, response = await upsert_lead(client, retry.payload, request_id=record.id)
        record.status = action
        record.bitrix_entity_type = "lead"
        record.bitrix_entity_id = str(lead_id)
        record.retry_count = retry.retry_count + 1
        db.commit()

        await fix_lead_fields(client, lead_id, retry.payload, record.id)

        deal_id = await create_deal(client, lead_id, retry.payload)
        if deal_id and retry.payload.get("products"):
            await set_deal_product_rows(client, deal_id, retry.payload)

        await add_timeline_comment(client, lead_id, retry.payload)
        retry.status = "done"
        db.commit()
        log_event(db, record.id, f"lead.{action}_retry", "ok", rest_method=f"crm.lead.{action}", response=response)
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        retry.last_error = str(exc)
        retry.retry_count += 1
        record.retry_count = retry.retry_count
        record.last_retry_at = datetime.now(timezone.utc)
        db.commit()

        if retry.retry_count >= len(RETRY_INTERVALS):
            retry.status = "failed"
            record.status = "failed"
            record.error = f"Retries exhausted. Last error: {exc}"
            db.commit()
            log_event(db, record.id, "retry_failed", "error", error=str(exc))
        else:
            interval = RETRY_INTERVALS[min(retry.retry_count, len(RETRY_INTERVALS) - 1)]
            retry.next_retry_at = datetime.now(timezone.utc) + interval
            db.commit()


async def _retry_worker() -> None:
    from app.db import SessionLocal
    while True:
        await asyncio.sleep(60)
        try:
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                pending = (
                    db.query(RetryQueue)
                    .filter(RetryQueue.status == "pending", RetryQueue.next_retry_at <= now)
                    .order_by(RetryQueue.next_retry_at)
                    .limit(5)
                    .all()
                )
                for retry in pending:
                    retry.status = "processing"
                    db.commit()
                    await _process_retry(retry, db)
            finally:
                db.close()
        except Exception as exc:
            logger.error("Retry worker error: %s", exc)
