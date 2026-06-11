# Twilio SMS & Voice Receiver

A Python [FastAPI](https://fastapi.tiangolo.com/) server that receives inbound SMS, OTP messages, and voice calls on your Twilio phone number. Twilio forwards each event to your webhooks; this app stores them in memory and exposes simple REST APIs to read messages, calls, and OTP codes.

## Features

- Webhook endpoint for Twilio inbound SMS
- Webhook endpoints for inbound voice calls, status updates, and voicemail recordings
- Automatic OTP extraction (4–8 digit codes) from message body
- REST APIs to list messages/calls and fetch the latest entry
- Sync fallback to pull messages and calls from the Twilio API if webhooks were missed
- Twilio request signature validation (when configured)

## Prerequisites

- Python 3.10+
- A [Twilio account](https://www.twilio.com/try-twilio) with a phone number that can receive SMS
- [ngrok](https://ngrok.com/) (or another tunnel) to expose your local server to the internet — Twilio webhooks require a public HTTPS URL

## Project structure

```
TwilioBackend/
├── app.py              # FastAPI application
├── requirements.txt    # Python dependencies
├── .env.example        # Environment variable template
├── .env                # Your local config (create this, not committed)
└── README.md
```

## Setup

### 1. Clone and create a virtual environment

```powershell
cd TwilioBackend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

On macOS/Linux:

```bash
cd TwilioBackend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy the example file and fill in your values:

```powershell
copy .env.example .env
```

Edit `.env`:

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Account SID from [Twilio Console](https://console.twilio.com) |
| `TWILIO_AUTH_TOKEN` | Auth Token from Twilio Console |
| `TWILIO_PHONE_NUMBER` | Your Twilio number in E.164 format (e.g. `+16184143472`) |
| `TWILIO_WEBHOOK_URL` | Public SMS webhook URL (set after starting ngrok — see below) |
| `TWILIO_VOICE_WEBHOOK_URL` | Public voice webhook URL (optional; derived from SMS URL if omitted) |
| `TWILIO_VOICE_GREETING` | Text spoken when answering a call |
| `TWILIO_VOICE_RECORD` | Record voicemail after greeting (`true`/`false`, default `true`) |
| `HOST` | Server bind address (default `0.0.0.0`) |
| `PORT` | Server port (default `8000`) |

### 3. Start the server

```powershell
python app.py
```

The API runs at `http://localhost:8000`. Interactive docs are at [http://localhost:8000/docs](http://localhost:8000/docs).

### 4. Expose the server with ngrok

In a **second terminal**:

```powershell
ngrok http 8000
```

Copy the HTTPS forwarding URL (e.g. `https://abc123.ngrok-free.dev`) and set in `.env`:

```
TWILIO_WEBHOOK_URL=https://abc123.ngrok-free.dev/webhook/sms
TWILIO_VOICE_WEBHOOK_URL=https://abc123.ngrok-free.dev/webhook/voice
```

Restart the app after updating `.env`. URLs must match **exactly** what you configure in Twilio (no trailing slash).

### 5. Configure Twilio webhooks

Twilio must know where to POST inbound messages and voice events.

#### Phone number — SMS

1. Open [Phone Numbers](https://console.twilio.com/us1/develop/phone-numbers/manage/incoming)
2. Select your number → **Messaging**
3. Under **A message comes in**:
   - **Webhook**
   - URL: `https://abc123.ngrok-free.dev/webhook/sms`
   - Method: **HTTP POST**

#### Phone number — Voice

1. Select your number → **Voice**
2. Under **A call comes in**:
   - **Webhook**
   - URL: `https://abc123.ngrok-free.dev/webhook/voice`
   - Method: **HTTP POST**
3. Under **Call status changes** (optional, recommended):
   - URL: `https://abc123.ngrok-free.dev/webhook/voice/status`
   - Method: **HTTP POST**

#### Messaging Service (if used)

If inbound SMS is routed through a Messaging Service (e.g. `TestSMS`):

1. Open [Messaging → Services](https://console.twilio.com/us1/develop/sms/services)
2. Select your service → **Integration**
3. Set **Incoming Messages** request URL to the same SMS webhook URL above

## API endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/webhook/sms` | Twilio inbound SMS webhook (primary) |
| `POST` | `/` | Alternate webhook path |
| `POST` | `/sms` | Alternate webhook path |
| `GET` | `/messages` | List recent messages (`?limit=20`) |
| `GET` | `/messages/latest` | Most recent message |
| `GET` | `/otp/latest` | Latest OTP from recent messages |
| `GET` / `POST` | `/messages/sync` | Pull inbound messages from Twilio API |
| `POST` | `/webhook/voice` | Twilio inbound voice webhook |
| `POST` | `/webhook/voice/status` | Call status updates (completed, busy, etc.) |
| `POST` | `/webhook/voice/recording` | Voicemail recording ready callback |
| `GET` | `/calls` | List recent calls (`?limit=20`) |
| `GET` | `/calls/latest` | Most recent inbound call |
| `GET` / `POST` | `/calls/sync` | Pull inbound calls from Twilio API |

### Example responses

**Latest message** — `GET /messages/latest`

```json
{
  "message": {
    "message_sid": "SMxxxxxxxx",
    "from": "+919779528344",
    "to": "+16184143472",
    "body": "Your verification code is 482910",
    "otp": "482910",
    "received_at": "2026-06-11T11:18:36+00:00"
  }
}
```

**Latest OTP** — `GET /otp/latest`

```json
{
  "otp": "482910",
  "from": "+919779528344",
  "body": "Your verification code is 482910",
  "received_at": "2026-06-11T11:18:36+00:00"
}
```

## Testing

### Quick test (webhook)

1. Ensure the server and ngrok are running.
2. Send an SMS from your phone to your Twilio number, e.g.:
   ```
   Your verification code is 123456
   ```
3. Check the server terminal for:
   ```
   [SMS] From +91... → +1...: Your verification code is 123456 | OTP: 123456
   ```
4. Open `http://localhost:8000/otp/latest` or use the Swagger UI at `/docs`.

### Sync missed messages

If a message appears in [Twilio Messaging Logs](https://console.twilio.com/us1/monitor/logs/sms) but not in the API (e.g. webhook misconfiguration):

```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:8000/messages/sync"
```

Or use **POST /messages/sync** in the Swagger UI.

### Quick test (voice)

1. Ensure the server and ngrok are running.
2. Call your Twilio number from a [verified phone](https://console.twilio.com/us1/develop/phone-numbers/manage/verified) (trial accounts).
3. You should hear the greeting and be prompted to leave a voicemail.
4. Check the server terminal for:
   ```
   [CALL] ringing From +91... → +1... (sid=CA...)
   ```
5. Open `http://localhost:8000/calls/latest` or use the Swagger UI at `/docs`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| Message in Twilio logs, empty `/messages` | Webhook not reaching the app | Check ngrok is running; verify webhook URL in Twilio |
| `403 Forbidden` on webhook | Signature validation failed | Ensure `TWILIO_WEBHOOK_URL` matches the exact URL in Twilio console |
| `405` on `POST /` | Webhook URL missing `/webhook/sms` path | Update Twilio to use `/webhook/sms`, or the app also accepts `POST /` and `POST /sms` |
| ngrok URL changed | Free ngrok URLs change on restart | Update `TWILIO_WEBHOOK_URL` and Twilio console |
| Trial account | Can only receive SMS/calls from verified numbers | [Verify your phone](https://console.twilio.com/us1/develop/phone-numbers/manage/verified) in Twilio Console |
| Call connects but no `/calls` entry | Voice webhook not configured | Set **A call comes in** to `/webhook/voice` in Twilio Console |

## Notes

- Messages and calls are stored **in memory** only (last 100 each). They are lost when the server restarts. Use `/messages/sync` or `/calls/sync` to reload from Twilio after a restart.
- OTP detection uses a simple regex for 4–8 digit numbers. It may match non-OTP numbers in some message formats.
- Never commit `.env` — it contains your Twilio credentials.

## Dependencies

- [FastAPI](https://fastapi.tiangolo.com/) — web framework
- [Uvicorn](https://www.uvicorn.org/) — ASGI server
- [Twilio Python SDK](https://github.com/twilio/twilio-python) — API client and webhook validation
- [python-dotenv](https://github.com/theskumar/python-dotenv) — environment variable loading
