"""RED thin Flask API — health, auth, contact. SQLAlchemy 2.x + DATABASE_URL.

Ben override (Batch 3): Flask + SQLAlchemy for this demo shared users/auth path.
House long-term preference remains FastAPI for other services — see README ADR.
"""
from __future__ import annotations

import logging
import re
import os
import secrets
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from functools import wraps
from typing import Any

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request
from flask_cors import CORS
from sqlalchemy import DateTime, Integer, String, Text, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("red-api")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./red.db")
MAIL_MODE = os.getenv("MAIL_MODE", "log").strip().lower()  # log | smtp
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "noreply@innovatered.com")
CONTACT_TO = os.getenv("CONTACT_TO", "ben.marum@innovatered.com")
SEED_TEST_EMAIL = os.getenv("SEED_TEST_EMAIL", "demo@innovatered.local").strip().lower()
# No default: the demo account exists only when Render sets SEED_TEST_PASSWORD.
SEED_TEST_PASSWORD = os.getenv("SEED_TEST_PASSWORD", "").strip()

def _record_log(message: str, *args: Any, level: int = logging.INFO) -> None:
    """Log an auth/contact event to the private server log only."""
    log.log(level, message, *args)


# In-memory bearer tokens for staging demo (token -> user_id).
# Fine for single-process local/staging; replace with JWT/DB later.
_tokens: dict[str, int] = {}


def make_engine():
    connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
    return create_engine(DATABASE_URL, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ContactMessage(Base):
    __tablename__ = "contact_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    cors_origins = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "https://www.innovatered.com",
        "https://innovatered.com",
        "https://rps.innovatered.com",
        re.compile(r"https://.*\.innovatered\.pages\.dev"),
        re.compile(r"https://.*\.innovatered-rps\.pages\.dev"),
    ]
    CORS(
        app,
        origins=cors_origins,
        supports_credentials=True,
        allow_headers=["Authorization", "Content-Type"],
        methods=["GET", "POST", "OPTIONS"],
    )

    # Ensure schema and the shared demo login exist on boot.
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    _seed_demo_user()

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "red-api"})


    @app.get("/db/ping")
    def db_ping():
        with engine.connect() as conn:
            row = conn.execute(text("SELECT 1 AS ok")).mappings().one()
        return jsonify(
            {
                "ok": bool(row["ok"]),
                "database_url_scheme": DATABASE_URL.split(":", 1)[0],
            }
        )

    @app.post("/auth/register")
    def auth_register():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip() or None
        if not email or "@" not in email:
            return jsonify({"error": "valid email required"}), 400
        if len(password) < 8:
            return jsonify({"error": "password must be at least 8 characters"}), 400

        with SessionLocal() as db:
            existing = db.scalar(select(User).where(User.email == email))
            if existing:
                return jsonify({"error": "email already registered"}), 409
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                name=name,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            token = _issue_token(user.id)
            _record_log("auth register succeeded for %s", email)
            return jsonify({"token": token, "user": _user_json(user)}), 201

    @app.post("/auth/login")
    def auth_login():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        if not email or not password:
            return jsonify({"error": "email and password required"}), 400

        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.email == email))
            if not user or not check_password_hash(user.password_hash, password):
                _record_log("auth login failed for %s", email, level=logging.WARNING)
                return jsonify({"error": "invalid credentials"}), 401
            token = _issue_token(user.id)
            _record_log("auth login succeeded for %s", email)
            return jsonify({"token": token, "user": _user_json(user)})

    @app.post("/auth/logout")
    def auth_logout():
        token = _extract_token()
        if token and token in _tokens:
            del _tokens[token]
            _record_log("auth logout succeeded")
        return jsonify({"ok": True})

    @app.get("/auth/me")
    @require_auth
    def auth_me():
        with SessionLocal() as db:
            user = db.get(User, g.user_id)
            if not user:
                return jsonify({"error": "user not found"}), 401
            return jsonify({"user": _user_json(user)})

    @app.post("/contact")
    def contact():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        email = (data.get("email") or "").strip().lower()
        company = (data.get("company") or "").strip() or None
        note = (data.get("note") or "").strip()
        if not name or not email or "@" not in email or not note:
            return jsonify({"error": "name, email, and note are required"}), 400

        with SessionLocal() as db:
            msg = ContactMessage(name=name, email=email, company=company, note=note)
            db.add(msg)
            db.commit()
            db.refresh(msg)
            message_id = msg.id

        _record_log("contact received from %s", email)
        mail_status = _send_contact_mail(name=name, email=email, company=company, note=note)
        return jsonify(
            {
                "ok": True,
                "id": message_id,
                "mail": mail_status,
            }
        )

    return app


def _seed_demo_user() -> None:
    """Create the shared demo account, or reset its password, from SEED_TEST_PASSWORD.

    With no SEED_TEST_PASSWORD set, no demo account is created.
    """
    if len(SEED_TEST_PASSWORD) < 8:
        _record_log("demo login seed skipped; SEED_TEST_PASSWORD not set", level=logging.WARNING)
        return
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == SEED_TEST_EMAIL))
        if existing:
            existing.password_hash = generate_password_hash(SEED_TEST_PASSWORD)
            db.commit()
            _record_log("demo login seed reset password for existing demo user")
            return
        db.add(
            User(
                email=SEED_TEST_EMAIL,
                password_hash=generate_password_hash(SEED_TEST_PASSWORD),
                name="RED Demo",
            )
        )
        db.commit()
    _record_log("demo login seed ran for %s", SEED_TEST_EMAIL)


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _extract_token()
        if not token or token not in _tokens:
            return jsonify({"error": "unauthorized"}), 401
        g.user_id = _tokens[token]
        return fn(*args, **kwargs)

    return wrapper


def _extract_token() -> str | None:
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    data = request.get_json(silent=True) or {}
    return (data.get("token") or "").strip() or None


def _issue_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    _tokens[token] = user_id
    return token


def _user_json(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


def _send_contact_mail(
    *, name: str, email: str, company: str | None, note: str
) -> dict[str, Any]:
    subject = f"[RED contact] {name}"
    body = (
        f"Name: {name}\n"
        f"Email: {email}\n"
        f"Company: {company or '(none)'}\n"
        f"\n{note}\n"
    )
    if MAIL_MODE != "smtp":
        _record_log(
            "MAIL_MODE=log — contact message to %s\nFrom: %s <%s>\nSubject: %s\n%s",
            CONTACT_TO,
            name,
            email,
            subject,
            body,
        )
        return {"mode": "log", "sent": False, "logged": True}

    if not SMTP_HOST:
        _record_log("MAIL_MODE=smtp but SMTP_HOST missing — logged only", level=logging.WARNING)
        _record_log("Contact (smtp fallback log):\n%s", body)
        return {"mode": "smtp", "sent": False, "logged": True, "reason": "SMTP_HOST missing"}

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = CONTACT_TO
        msg["Reply-To"] = email
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
        return {"mode": "smtp", "sent": True}
    except Exception as exc:  # noqa: BLE001 — never fail contact solely on SMTP
        _record_log("SMTP send failed: %s", exc, level=logging.ERROR)
        _record_log("Contact (smtp error fallback log):\n%s", body)
        return {"mode": "smtp", "sent": False, "logged": True, "reason": str(exc)}


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
