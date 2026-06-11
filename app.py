"""
Twilio SMS webhook receiver — captures inbound SMS/OTP sent to your Twilio number.
"""

from __future__ import annotations

import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from twilio.request_validator import RequestValidator

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WEBHOOK_URL = os.getenv("TWILIO_WEBHOOK_URL", "").rstrip("/")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")

# Keep the last 100 received messages in memory
MAX_MESSAGES = 100
received_messages: deque[dict[str, Any]] = deque(maxlen=MAX_MESSAGES)

OTP_PATTERN = re.compile(r"\b\d{4,8}\b")

app = FastAPI(
    title="Twilio SMS Receiver",
    description="Webhook server to receive SMS and OTP messages on your Twilio number.",
)


def extract_otp(body: str) -> str | None:
    """Try to pull a 4–8 digit OTP code from the message body."""
    match = OTP_PATTERN.search(body)
    return match.group(0) if match else None


def get_validation_url(request: Request) -> str:
    """Build the public URL Twilio signed against (works behind ngrok)."""
    if TWILIO_WEBHOOK_URL:
        return TWILIO_WEBHOOK_URL

    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    return f"{proto}://{host}{request.url.path}".rstrip("/")


def validate_twilio_request(request: Request, form_data: dict[str, str]) -> None:
    """Verify the request actually came from Twilio."""
    if not TWILIO_AUTH_TOKEN:
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    url = get_validation_url(request)

    if not validator.validate(url, form_data, signature):
        print(f"[WARN] Twilio signature mismatch for URL: {url}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "twilio-sms-receiver"}


async def _handle_incoming_sms(request: Request) -> PlainTextResponse:
    """Process a Twilio inbound-SMS webhook payload."""
    form = await request.form()
    # Twilio signature must be validated against ALL posted fields, not a subset.
    form_data = {key: str(value) for key, value in form.items()}

    validate_twilio_request(request, form_data)

    message_sid = form_data.get("MessageSid", "")
    from_number = form_data.get("From", "")
    to_number = form_data.get("To", "")
    body = form_data.get("Body", "")

    otp = extract_otp(body)
    entry = {
        "message_sid": message_sid,
        "from": from_number,
        "to": to_number,
        "body": body,
        "otp": otp,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    received_messages.appendleft(entry)

    print(f"[SMS] From {from_number} → {to_number}: {body}" + (f" | OTP: {otp}" if otp else ""))

    return PlainTextResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


@app.post("/webhook/sms")
async def receive_sms(request: Request) -> PlainTextResponse:
    """
    Twilio calls this URL when someone sends an SMS to your Twilio number.
    Configure it under: Phone Numbers → your number → Messaging → 'A message comes in'.
    """
    return await _handle_incoming_sms(request)


@app.post("/")
async def receive_sms_root(request: Request) -> PlainTextResponse:
    """Accept webhooks misconfigured to POST to the root URL."""
    return await _handle_incoming_sms(request)


@app.post("/sms")
async def receive_sms_short(request: Request) -> PlainTextResponse:
    """Accept webhooks misconfigured to POST to /sms."""
    return await _handle_incoming_sms(request)


@app.post("/messages/sync")
def sync_messages_from_twilio(limit: int = 20) -> JSONResponse:
    """
    Pull recent inbound messages from Twilio API (useful if webhook was misconfigured).
    Requires TWILIO_PHONE_NUMBER in .env (e.g. +16184143472).
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")
    if not TWILIO_PHONE_NUMBER:
        raise HTTPException(status_code=400, detail="Set TWILIO_PHONE_NUMBER in .env")

    from twilio.rest import Client

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    twilio_messages = client.messages.list(to=TWILIO_PHONE_NUMBER, limit=limit)

    synced = 0
    for msg in twilio_messages:
        if msg.direction != "inbound":
            continue
        body = msg.body or ""
        entry = {
            "message_sid": msg.sid,
            "from": msg.from_,
            "to": msg.to,
            "body": body,
            "otp": extract_otp(body),
            "received_at": (
                msg.date_sent.replace(tzinfo=timezone.utc).isoformat()
                if msg.date_sent
                else datetime.now(timezone.utc).isoformat()
            ),
        }
        if not any(m["message_sid"] == entry["message_sid"] for m in received_messages):
            received_messages.appendleft(entry)
            synced += 1

    return JSONResponse(
        content={
            "synced": synced,
            "total_in_memory": len(received_messages),
            "messages": list(received_messages)[:limit],
        }
    )


@app.get("/messages")
def list_messages(limit: int = 20) -> JSONResponse:
    """List recently received messages (newest first)."""
    limit = max(1, min(limit, MAX_MESSAGES))
    return JSONResponse(content={"count": len(received_messages), "messages": list(received_messages)[:limit]})


@app.get("/messages/latest")
def latest_message() -> JSONResponse:
    """Return the most recent message — useful for polling OTP during testing."""
    if not received_messages:
        return JSONResponse(content={"message": None})
    return JSONResponse(content={"message": received_messages[0]})


@app.get("/otp/latest")
def latest_otp() -> JSONResponse:
    """Return the OTP from the most recent message that contained one."""
    for msg in received_messages:
        if msg["otp"]:
            return JSONResponse(
                content={
                    "otp": msg["otp"],
                    "from": msg["from"],
                    "body": msg["body"],
                    "received_at": msg["received_at"],
                }
            )
    return JSONResponse(content={"otp": None, "message": "No OTP found in recent messages"})


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host=host, port=port, reload=True)
