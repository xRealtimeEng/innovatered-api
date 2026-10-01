# RED API (Batch 3)

Thin Flask + SQLAlchemy API shared by RedWeb and RPS for demo auth + contact.

## ADR — Ben override (Flask, not FastAPI)

**House stack preference:** FastAPI for new RED services.

**This service:** Ben explicitly chose **Flask + SQLAlchemy** for Batch 3 so the shared
users/auth demo ships quickly and Chieftain does not “correct” it back to FastAPI.
FastAPI remains the long-term preference for other services.

## Schema sketch

```
users
  id              INTEGER PK
  email           VARCHAR(320) UNIQUE NOT NULL
  password_hash   VARCHAR(255) NOT NULL
  name            VARCHAR(200) NULL
  created_at      DATETIME TZ

contact_messages
  id              INTEGER PK
  name            VARCHAR(200) NOT NULL
  email           VARCHAR(320) NOT NULL
  company         VARCHAR(200) NULL
  note            TEXT NOT NULL
  created_at      DATETIME TZ
```

`DATABASE_URL` defaults to `sqlite:///./red.db` (Postgres-ready via the same URL).

## Auth contract (RedWeb + RPS)

1. `POST /auth/register` `{email, password, name?}` → `{token, user}`
2. `POST /auth/login` `{email, password}` → `{token, user}`
3. Store `token` in **localStorage** key **`red_auth_token`**
4. Send `Authorization: Bearer <token>` on `GET /auth/me` and `POST /auth/logout`
5. `GET /auth/me` → `{user: {id, email, name, created_at}}`

## Run

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # optional
flask --app main run --host 0.0.0.0 --port 8000
# or: python main.py
```

Health: `http://127.0.0.1:8000/health`


DB ping: `http://127.0.0.1:8000/db/ping`

## Render free-tier deploy

The repository includes a root-level `render.yaml` blueprint and `backend/Procfile`.
In Render, the blueprint uses `backend/` as the service root, installs
`backend/requirements.txt`, and starts Gunicorn on Render's `$PORT`:

```text
gunicorn main:app --bind 0.0.0.0:$PORT
```

The free service is configured for `MAIL_MODE=log` and
`DATABASE_URL=sqlite:///./red.db`. SQLite is ephemeral on Render's free tier,
so this is suitable for the demo only; use a persistent Postgres database before
production. The shared demo account exists only when `SEED_TEST_PASSWORD` is set in
Render's environment. There is no default in code. On boot, the API creates
`demo@innovatered.local` or resets its password to that value; it never logs
the password.

## Contact / email

- `POST /contact` `{name, email, company?, note}` — validates, persists, attempts send
- `MAIL_MODE=log` (default): prints/logs the message; request still succeeds
- `MAIL_MODE=smtp`: uses `SMTP_*` + `CONTACT_TO` (default `ben.marum@innovatered.com`);
  SMTP failure is logged and does **not** fail the HTTP request

Ask Ceaser for an M365 test mailbox if real SMTP is needed later.

## CORS

Allows localhost Vite ports, `https://*.innovatered.pages.dev`,
`https://*.innovatered-rps.pages.dev`, and www/rps for later. Bearer tokens preferred
for cross-origin CF Pages frontends.
