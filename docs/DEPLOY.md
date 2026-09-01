# Deploying

A webhook needs a **publicly reachable HTTPS URL**. That is the only real
constraint — everything else is a normal FastAPI app.

## Option A — local + tunnel (use this while building)

```bash
.venv/bin/uvicorn backend.main:app --reload --port 8000
cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
```

Paste the printed `https://…` URL + `/webhooks/razorpay` into the Razorpay
dashboard.

⚠️ **Quick tunnels mint a new URL on every restart**, so you re-register the
webhook in Razorpay each time. Two ways out:
- `ngrok http 8000 --domain=your-reserved.ngrok-free.app` (free reserved domain)
- a named cloudflared tunnel (`cloudflared tunnel create`)

Do one of these early. Re-pasting URLs during a demo is how demos die.

## Option B — hosted (use this for the demo)

Any container host works. `render.yaml`, Railway, and Fly all take the same
start command:

```
uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

Set `RAZORPAY_WEBHOOK_SECRET`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` as
environment variables — never commit `.env`.

⚠️ **Free tiers sleep after inactivity.** A cold start takes 30–60s, and
Razorpay will retry a webhook that times out — but the first event of your demo
arriving a minute late is not a good look. Either hit `/health` a few minutes
before presenting, or use a paid instance for demo day.

⚠️ **The app needs a reachable MySQL.** Locally: your installed instance.
Hosted: the platform's managed MySQL add-on, with `MYSQL_*` env vars pointed at
it. Migrations run automatically at startup, so a fresh database self-builds.

## Demo-day resilience

Venue wifi is hostile and the tunnel is the fragile link. The synthetic path
runs entirely locally, so it can carry the whole demo if the network dies:

```bash
curl -X POST 'localhost:8000/admin/generate?count=500&seed=42'
```

Rehearse once with wifi off.
