"""
Twilio SMS and voice webhook receiver — captures inbound SMS/OTP and phone calls.
"""

from __future__ import annotations

import os
import re
from html import escape
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WEBHOOK_URL = os.getenv("TWILIO_WEBHOOK_URL", "").rstrip("/")
TWILIO_VOICE_WEBHOOK_URL = os.getenv("TWILIO_VOICE_WEBHOOK_URL", "").rstrip("/")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
TWILIO_VOICE_GREETING = os.getenv(
    "TWILIO_VOICE_GREETING",
    "Thank you for calling. Please leave a message after the beep.",
)
TWILIO_VOICE_RECORD = os.getenv("TWILIO_VOICE_RECORD", "true").lower() in {"1", "true", "yes"}

MAX_ITEMS = 100
received_messages: deque[dict[str, Any]] = deque(maxlen=MAX_ITEMS)
received_calls: deque[dict[str, Any]] = deque(maxlen=MAX_ITEMS)

OTP_PATTERN = re.compile(r"\b\d{4,8}\b")


def build_app_description() -> str:
    base = (
        "Webhook server to receive inbound SMS, OTP messages, and voice calls "
        "on your Twilio phone number."
    )
    if TWILIO_PHONE_NUMBER:
        return f"{base}\n\n**Active Twilio number:** `{TWILIO_PHONE_NUMBER}`"
    return f"{base}\n\n**Active Twilio number:** _Not configured — set `TWILIO_PHONE_NUMBER` in `.env`_"


def get_public_base_url() -> str:
    """Derive ngrok/public base URL from configured SMS or voice webhook URLs."""
    for url in (TWILIO_WEBHOOK_URL, TWILIO_VOICE_WEBHOOK_URL, voice_webhook_url()):
        if url:
            parsed = urlparse(url)
            return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def voice_webhook_url() -> str:
    if TWILIO_VOICE_WEBHOOK_URL:
        return TWILIO_VOICE_WEBHOOK_URL
    if TWILIO_WEBHOOK_URL and "/webhook/" in TWILIO_WEBHOOK_URL:
        base = TWILIO_WEBHOOK_URL.rsplit("/webhook/", 1)[0]
        return f"{base}/webhook/voice"
    return ""


app = FastAPI(
    title="Twilio SMS & Voice Receiver",
    description=build_app_description(),
    docs_url=None,
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def extract_otp(body: str) -> str | None:
    """Try to pull a 4–8 digit OTP code from the message body."""
    match = OTP_PATTERN.search(body)
    return match.group(0) if match else None


def get_validation_url(request: Request, configured_url: str = "") -> str:
    """Build the public URL Twilio signed against (works behind ngrok)."""
    if configured_url:
        return configured_url.rstrip("/")

    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host", "")
    return f"{proto}://{host}{request.url.path}".rstrip("/")


def validate_twilio_request(
    request: Request,
    form_data: dict[str, str],
    configured_url: str = "",
) -> None:
    """Verify the request actually came from Twilio."""
    if not TWILIO_AUTH_TOKEN:
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        return

    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    url = get_validation_url(request, configured_url)

    if not validator.validate(url, form_data, signature):
        print(f"[WARN] Twilio signature mismatch for URL: {url}")
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def _upsert_call(entry: dict[str, Any]) -> None:
    """Insert or update a call record by call_sid."""
    call_sid = entry["call_sid"]
    for index, existing in enumerate(received_calls):
        if existing["call_sid"] == call_sid:
            merged = {**existing, **{k: v for k, v in entry.items() if v is not None and v != ""}}
            merged["updated_at"] = datetime.now(timezone.utc).isoformat()
            received_calls[index] = merged
            return

    entry.setdefault("received_at", datetime.now(timezone.utc).isoformat())
    entry["updated_at"] = entry["received_at"]
    received_calls.appendleft(entry)


def _call_entry_from_form(form_data: dict[str, str]) -> dict[str, Any]:
    duration = form_data.get("CallDuration") or form_data.get("RecordingDuration")
    return {
        "call_sid": form_data.get("CallSid", ""),
        "from": form_data.get("From", ""),
        "to": form_data.get("To", ""),
        "status": form_data.get("CallStatus", ""),
        "direction": form_data.get("Direction", ""),
        "duration": int(duration) if duration and duration.isdigit() else None,
        "recording_url": form_data.get("RecordingUrl", "") or None,
        "recording_sid": form_data.get("RecordingSid", "") or None,
    }


def build_incoming_call_twiml() -> str:
    """TwiML played when an inbound call is answered."""
    response = VoiceResponse()
    response.say(TWILIO_VOICE_GREETING, voice="alice")

    if TWILIO_VOICE_RECORD:
        base = get_public_base_url()
        record_kwargs: dict[str, Any] = {
            "timeout": 5,
            "max_length": 120,
            "play_beep": True,
        }
        if base:
            record_kwargs["recording_status_callback"] = f"{base}/webhook/voice/recording"
            record_kwargs["recording_status_callback_method"] = "POST"
        response.record(**record_kwargs)
    else:
        response.pause(length=1)
        response.hangup()

    return str(response)


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "twilio-sms-voice-receiver"}


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request) -> HTMLResponse:
    """Swagger UI with light/dark theme toggle."""
    root_path = request.scope.get("root_path", "").rstrip("/")
    openapi_url = f"{root_path}{app.openapi_url}"
    oauth2_redirect_url = app.swagger_ui_oauth2_redirect_url
    if oauth2_redirect_url:
        oauth2_redirect_url = f"{root_path}{oauth2_redirect_url}"

    response = get_swagger_ui_html(
        openapi_url=openapi_url,
        title=f"{app.title} - Docs",
        oauth2_redirect_url=oauth2_redirect_url,
        init_oauth=app.swagger_ui_init_oauth,
        swagger_ui_parameters=app.swagger_ui_parameters,
    )
    content = response.body.decode()
    phone_label = escape(TWILIO_PHONE_NUMBER) if TWILIO_PHONE_NUMBER else "Not configured"
    phone_class = "" if TWILIO_PHONE_NUMBER else " twilio-config-banner__number--missing"
    head_injection = (
        '<link rel="stylesheet" href="/static/docs-theme.css">\n'
        '<script src="/static/docs-theme.js" defer></script>\n'
    )
    body_injection = (
        '<div id="twilio-config-banner" class="twilio-config-banner">'
        '<span class="twilio-config-banner__label">SMS &amp; Voice on</span>'
        f'<span class="twilio-config-banner__number{phone_class}">{phone_label}</span>'
        "</div>\n"
    )
    content = content.replace("</head>", f"{head_injection}</head>", 1)
    content = content.replace("<body>", f"<body>{body_injection}", 1)
    return HTMLResponse(content)


async def _handle_incoming_sms(request: Request) -> PlainTextResponse:
    """Process a Twilio inbound-SMS webhook payload."""
    form = await request.form()
    # Twilio signature must be validated against ALL posted fields, not a subset.
    form_data = {key: str(value) for key, value in form.items()}

    validate_twilio_request(request, form_data, TWILIO_WEBHOOK_URL)

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


async def _parse_twilio_form(request: Request, configured_url: str) -> dict[str, str]:
    form = await request.form()
    form_data = {key: str(value) for key, value in form.items()}
    validate_twilio_request(request, form_data, configured_url)
    return form_data


async def _handle_incoming_call(request: Request) -> PlainTextResponse:
    """Answer an inbound voice call and optionally record a voicemail."""
    form_data = await _parse_twilio_form(request, voice_webhook_url())
    entry = _call_entry_from_form(form_data)
    _upsert_call(entry)

    print(
        f"[CALL] {entry['status']} From {entry['from']} → {entry['to']} "
        f"(sid={entry['call_sid']})"
    )

    return PlainTextResponse(content=build_incoming_call_twiml(), media_type="application/xml")


async def _handle_call_status(request: Request) -> PlainTextResponse:
    """Receive call status updates (completed, busy, no-answer, etc.)."""
    base = get_public_base_url()
    status_url = f"{base}/webhook/voice/status" if base else ""
    form_data = await _parse_twilio_form(request, status_url)
    entry = _call_entry_from_form(form_data)
    _upsert_call(entry)

    print(
        f"[CALL STATUS] {entry['status']} sid={entry['call_sid']} "
        f"duration={entry['duration']}s"
    )

    return PlainTextResponse(content="", status_code=200)


async def _handle_call_recording(request: Request) -> PlainTextResponse:
    """Receive recording metadata when voicemail finishes."""
    base = get_public_base_url()
    recording_url = f"{base}/webhook/voice/recording" if base else ""
    form_data = await _parse_twilio_form(request, recording_url)
    entry = _call_entry_from_form(form_data)
    _upsert_call(entry)

    print(f"[RECORDING] sid={entry['recording_sid']} url={entry['recording_url']}")

    return PlainTextResponse(content="", status_code=200)


@app.post("/webhook/voice")
async def receive_voice_call(request: Request) -> PlainTextResponse:
    """
    Twilio calls this URL when someone calls your Twilio number.
    Configure under: Phone Numbers → your number → Voice → 'A call comes in'.
    """
    return await _handle_incoming_call(request)


@app.post("/webhook/voice/status")
async def receive_voice_status(request: Request) -> PlainTextResponse:
    """
    Optional status callback for call lifecycle updates.
    Configure under: Phone Numbers → Voice → 'Call status changes'.
    """
    return await _handle_call_status(request)


@app.post("/webhook/voice/recording")
async def receive_voice_recording(request: Request) -> PlainTextResponse:
    """Callback when a voicemail recording is ready."""
    return await _handle_call_recording(request)


def sync_messages_from_twilio_api(limit: int = 20) -> int:
    """Pull inbound messages from Twilio into memory. Returns count of newly synced messages."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        return 0

    from twilio.rest import Client

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    twilio_messages = client.messages.list(to=TWILIO_PHONE_NUMBER, limit=limit)

    synced = 0
    for msg in twilio_messages:
        if not (msg.direction or "").startswith("inbound"):
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

    return synced


def ensure_messages_loaded(limit: int = 20) -> None:
    """Load from Twilio API when memory is empty (e.g. after server restart)."""
    if not received_messages:
        sync_messages_from_twilio_api(limit=limit)


@app.post("/messages/sync")
@app.get("/messages/sync")
def sync_messages_from_twilio(limit: int = 20) -> JSONResponse:
    """
    Pull recent inbound messages from Twilio API (useful if webhook was misconfigured).
    Requires TWILIO_PHONE_NUMBER in .env (e.g. +16184143472).
    """
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")
    if not TWILIO_PHONE_NUMBER:
        raise HTTPException(status_code=400, detail="Set TWILIO_PHONE_NUMBER in .env")

    synced = sync_messages_from_twilio_api(limit=limit)
    limit = max(1, min(limit, MAX_ITEMS))

    return JSONResponse(
        content={
            "synced": synced,
            "total_in_memory": len(received_messages),
            "messages": list(received_messages)[:limit],
        }
    )


def sync_calls_from_twilio_api(limit: int = 20) -> int:
    """Pull inbound calls from Twilio into memory. Returns count of newly synced calls."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_PHONE_NUMBER:
        return 0

    from twilio.rest import Client

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    twilio_calls = client.calls.list(to=TWILIO_PHONE_NUMBER, limit=limit)

    synced = 0
    for call in twilio_calls:
        if (call.direction or "") != "inbound":
            continue
        entry = {
            "call_sid": call.sid,
            "from": call.from_,
            "to": call.to,
            "status": call.status or "",
            "direction": call.direction or "",
            "duration": int(call.duration) if call.duration else None,
            "recording_url": None,
            "recording_sid": None,
            "received_at": (
                call.start_time.replace(tzinfo=timezone.utc).isoformat()
                if call.start_time
                else datetime.now(timezone.utc).isoformat()
            ),
        }
        if not any(c["call_sid"] == entry["call_sid"] for c in received_calls):
            entry["updated_at"] = entry["received_at"]
            received_calls.appendleft(entry)
            synced += 1

    return synced


def ensure_calls_loaded(limit: int = 20) -> None:
    """Load calls from Twilio API when memory is empty (e.g. after server restart)."""
    if not received_calls:
        sync_calls_from_twilio_api(limit=limit)


@app.post("/calls/sync")
@app.get("/calls/sync")
def sync_calls_from_twilio(limit: int = 20) -> JSONResponse:
    """Pull recent inbound calls from the Twilio API."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")
    if not TWILIO_PHONE_NUMBER:
        raise HTTPException(status_code=400, detail="Set TWILIO_PHONE_NUMBER in .env")

    synced = sync_calls_from_twilio_api(limit=limit)
    limit = max(1, min(limit, MAX_ITEMS))

    return JSONResponse(
        content={
            "synced": synced,
            "total_in_memory": len(received_calls),
            "calls": list(received_calls)[:limit],
        }
    )


@app.get("/calls")
def list_calls(limit: int = 20, sync: bool = True) -> JSONResponse:
    """List recently received calls (newest first). Auto-syncs from Twilio when empty."""
    limit = max(1, min(limit, MAX_ITEMS))
    if sync:
        ensure_calls_loaded(limit=limit)
    return JSONResponse(content={"count": len(received_calls), "calls": list(received_calls)[:limit]})


@app.get("/calls/latest")
def latest_call(sync: bool = True) -> JSONResponse:
    """Return the most recent inbound call."""
    if sync:
        ensure_calls_loaded()
    if not received_calls:
        return JSONResponse(content={"call": None})
    return JSONResponse(content={"call": received_calls[0]})


@app.get("/messages")
def list_messages(limit: int = 20, sync: bool = True) -> JSONResponse:
    """List recently received messages (newest first). Auto-syncs from Twilio when empty."""
    limit = max(1, min(limit, MAX_ITEMS))
    if sync:
        ensure_messages_loaded(limit=limit)
    return JSONResponse(content={"count": len(received_messages), "messages": list(received_messages)[:limit]})


@app.get("/messages/latest")
def latest_message(sync: bool = True) -> JSONResponse:
    """Return the most recent message — useful for polling OTP during testing."""
    if sync:
        ensure_messages_loaded()
    if not received_messages:
        return JSONResponse(content={"message": None})
    return JSONResponse(content={"message": received_messages[0]})


@app.get("/otp/latest")
def latest_otp(sync: bool = True) -> JSONResponse:
    """Return the OTP from the most recent message that contained one."""
    if sync:
        ensure_messages_loaded()
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
