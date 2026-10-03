"""Local, private dev dashboard for red-api.

Shows the database (tables, columns, row counts, stats), live connections,
recent request traffic, users, contact messages and the API's routes.

It is off unless DASHBOARD_ENABLED=1, and it only answers requests coming
from this machine (127.0.0.1 / ::1), so it stays local while we build.
"""
from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, Flask, abort, g, jsonify, request, send_file
from sqlalchemy import DateTime, Float, Integer, String, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

STARTED_AT = datetime.now(timezone.utc)
LOCAL_ADDRS = {"127.0.0.1", "::1", "localhost"}
HIDDEN_COLUMNS = {"password_hash"}
PREFIX = "/_dash"


def init_dashboard(app: Flask, *, engine: Engine, Base: Any, SessionLocal: sessionmaker,
                   tokens: dict[str, int], user_model: Any, contact_model: Any) -> None:
    """Attach request logging and (if enabled) the dashboard to the app."""

    class RequestLog(Base):
        __tablename__ = "request_log"

        id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
        ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True,
                                             default=lambda: datetime.now(timezone.utc))
        method: Mapped[str] = mapped_column(String(10), nullable=False)
        path: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
        endpoint: Mapped[str | None] = mapped_column(String(200), nullable=True)
        status: Mapped[int] = mapped_column(Integer, nullable=False)
        duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
        ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
        user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
        user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    RequestLog.__table__.create(bind=engine, checkfirst=True)

    # ---- request logging (every API call except the dashboard itself) ----
    @app.before_request
    def _start_timer():
        g._t0 = time.perf_counter()

    @app.after_request
    def _log_request(resp):
        try:
            if (request.path.startswith(PREFIX) or request.method == "OPTIONS"
                    or request.headers.get("User-Agent") == "red-dashboard-check"):
                return resp
            ms = (time.perf_counter() - getattr(g, "_t0", time.perf_counter())) * 1000
            with SessionLocal() as db:
                db.add(RequestLog(method=request.method, path=request.path[:500],
                                  endpoint=request.endpoint, status=resp.status_code,
                                  duration_ms=round(ms, 2),
                                  ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
                                  user_agent=(request.user_agent.string or "")[:300],
                                  user_id=getattr(g, "user_id", None)))
                db.commit()
        except Exception:  # noqa: BLE001 - logging must never break a request
            app.logger.exception("request_log write failed")
        return resp

    if os.getenv("DASHBOARD_ENABLED", "0") != "1":
        return

    bp = Blueprint("dash", __name__, url_prefix=PREFIX)
    dialect = engine.dialect.name

    @bp.before_request
    def _local_only():
        if (request.remote_addr or "") not in LOCAL_ADDRS:
            abort(404)

    def q(sql: str, **params) -> list[dict[str, Any]]:
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(text(sql), params).mappings()]

    def jsonable(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, (bytes, memoryview)):
            return f"<{len(v)} bytes>"
        if isinstance(v, timedelta):
            return v.total_seconds()
        return v

    def rows(rs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: jsonable(v) for k, v in r.items()} for r in rs]

    @bp.get("/")
    def page():
        return send_file(Path(__file__).with_name("dashboard.html"))

    @bp.get("/api/overview")
    def overview():
        t0 = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        ping_ms = round((time.perf_counter() - t0) * 1000, 2)
        info: dict[str, Any] = {
            "service": "red-api",
            "started_at": STARTED_AT.isoformat(),
            "uptime_s": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
            "python": platform.python_version(),
            "host": platform.node(),
            "pid": os.getpid(),
            "db_dialect": dialect,
            "db_driver": engine.dialect.driver,
            "db_target": engine.url.render_as_string(hide_password=True),
            "db_ping_ms": ping_ms,
            "pool": engine.pool.status(),
            "active_tokens": len(tokens),
        }
        if dialect == "postgresql":
            r = q("""SELECT version() AS version, current_database() AS db,
                     pg_size_pretty(pg_database_size(current_database())) AS size,
                     pg_database_size(current_database()) AS size_bytes,
                     (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()) AS connections,
                     (SELECT setting::int FROM pg_settings WHERE name='max_connections') AS max_connections,
                     d.xact_commit, d.xact_rollback, d.blks_hit, d.blks_read, d.deadlocks,
                     d.tup_inserted, d.tup_updated, d.tup_deleted, d.stats_reset
                     FROM pg_stat_database d WHERE d.datname = current_database()""")[0]
            hit, read = r.pop("blks_hit") or 0, r.pop("blks_read") or 0
            r["cache_hit_pct"] = round(100 * hit / (hit + read), 2) if hit + read else None
            info["postgres"] = {k: jsonable(v) for k, v in r.items()}
        elif dialect == "sqlite":
            db_file = engine.url.database or ""
            info["sqlite"] = {"file": db_file,
                              "size_bytes": os.path.getsize(db_file) if db_file and os.path.exists(db_file) else None,
                              "version": q("select sqlite_version() as v")[0]["v"]}
        with SessionLocal() as db:
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            info["counts"] = {
                "users": db.scalar(select(func.count()).select_from(user_model)),
                "contact_messages": db.scalar(select(func.count()).select_from(contact_model)),
                "requests_24h": db.scalar(select(func.count()).where(RequestLog.ts >= since)),
                "errors_24h": db.scalar(select(func.count()).where(RequestLog.ts >= since, RequestLog.status >= 400)),
            }
        return jsonify(info)

    @bp.get("/api/tables")
    def tables():
        insp = inspect(engine)
        stats: dict[str, dict[str, Any]] = {}
        if dialect == "postgresql":
            for r in q("""SELECT relname, n_live_tup, n_dead_tup, seq_scan, idx_scan,
                          n_tup_ins, n_tup_upd, n_tup_del, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
                          pg_total_relation_size(relid) AS total_bytes,
                          pg_size_pretty(pg_total_relation_size(relid)) AS total_size
                          FROM pg_stat_user_tables"""):
                stats[r.pop("relname")] = {k: jsonable(v) for k, v in r.items()}
        out = []
        for name in sorted(insp.get_table_names()):
            pk = set(insp.get_pk_constraint(name).get("constrained_columns") or [])
            with engine.connect() as conn:
                count = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar()
            out.append({
                "name": name,
                "rows": count,
                "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c["nullable"],
                             "pk": c["name"] in pk, "default": jsonable(c.get("default"))}
                            for c in insp.get_columns(name)],
                "indexes": [{"name": i["name"], "columns": i["column_names"], "unique": i["unique"]}
                            for i in insp.get_indexes(name)],
                "foreign_keys": [{"columns": f["constrained_columns"], "references": f"{f['referred_table']}({', '.join(f['referred_columns'])})"}
                                 for f in insp.get_foreign_keys(name)],
                "stats": stats.get(name),
            })
        return jsonify(out)

    @bp.get("/api/table/<name>")
    def table_rows(name: str):
        insp = inspect(engine)
        if name not in insp.get_table_names():
            abort(404)
        limit = max(1, min(int(request.args.get("limit", 50)), 500))
        cols = [c["name"] for c in insp.get_columns(name)]
        order = "id" if "id" in cols else cols[0]
        rs = q(f'SELECT * FROM "{name}" ORDER BY "{order}" DESC LIMIT :n', n=limit)
        for r in rs:
            for h in HIDDEN_COLUMNS & r.keys():
                r[h] = "(hidden)"
        return jsonify({"columns": cols, "rows": rows(rs)})

    @bp.get("/api/connections")
    def connections():
        if dialect != "postgresql":
            return jsonify({"supported": False, "note": f"Live connection list needs Postgres (now {dialect})."})
        rs = q("""SELECT pid, usename AS user, application_name AS app, client_addr::text AS client,
                  backend_type, state, wait_event_type, wait_event,
                  backend_start, xact_start, query_start, state_change,
                  left(query, 300) AS query, pid = pg_backend_pid() AS is_this_dashboard
                  FROM pg_stat_activity WHERE datname = current_database() ORDER BY backend_start""")
        locks = q("""SELECT l.locktype, l.mode, l.granted, l.pid, c.relname
                     FROM pg_locks l LEFT JOIN pg_class c ON c.oid = l.relation
                     WHERE l.database = (SELECT oid FROM pg_database WHERE datname = current_database())""")
        return jsonify({"supported": True, "connections": rows(rs), "locks": rows(locks), "pool": engine.pool.status()})

    @bp.get("/api/traffic")
    def traffic():
        minutes = max(5, min(int(request.args.get("minutes", 60)), 1440))
        now = datetime.now(timezone.utc)
        since = now - timedelta(minutes=minutes)
        with SessionLocal() as db:
            recent = db.scalars(select(RequestLog).order_by(RequestLog.id.desc()).limit(200)).all()
            window = db.execute(select(RequestLog.ts, RequestLog.method, RequestLog.path, RequestLog.status,
                                       RequestLog.duration_ms, RequestLog.ip)
                                .where(RequestLog.ts >= since)).all()
        buckets = [0] * minutes
        errs = [0] * minutes
        routes: dict[str, dict[str, Any]] = {}
        clients: dict[str, int] = {}
        for ts, method, path, status, ms, ip in window:
            ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            i = min(minutes - 1, int((ts - since).total_seconds() // 60))
            buckets[i] += 1
            if status >= 400:
                errs[i] += 1
            k = f"{method} {path}"
            r = routes.setdefault(k, {"route": k, "count": 0, "errors": 0, "ms": []})
            r["count"] += 1
            r["errors"] += status >= 400
            r["ms"].append(ms)
            clients[ip or "?"] = clients.get(ip or "?", 0) + 1
        route_list = []
        for r in routes.values():
            ms = sorted(r.pop("ms"))
            r["avg_ms"] = round(sum(ms) / len(ms), 1)
            r["p95_ms"] = round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1)
            route_list.append(r)
        route_list.sort(key=lambda r: -r["count"])
        return jsonify({
            "minutes": minutes, "per_minute": buckets, "errors_per_minute": errs,
            "routes": route_list,
            "clients": sorted(({"ip": k, "count": v} for k, v in clients.items()), key=lambda c: -c["count"]),
            "recent": [{"ts": r.ts.isoformat(), "method": r.method, "path": r.path, "status": r.status,
                        "ms": r.duration_ms, "ip": r.ip, "user_agent": r.user_agent, "user_id": r.user_id}
                       for r in recent],
        })

    @bp.get("/api/users")
    def users():
        with SessionLocal() as db:
            us = db.scalars(select(user_model).order_by(user_model.id.desc())).all()
            logins = dict(db.execute(select(RequestLog.ip, func.max(RequestLog.ts))
                                     .where(RequestLog.path == "/auth/login", RequestLog.status == 200)
                                     .group_by(RequestLog.ip)).all())
            by_user = dict(db.execute(select(RequestLog.user_id, func.max(RequestLog.ts))
                                      .where(RequestLog.user_id.is_not(None)).group_by(RequestLog.user_id)).all())
            contacts = db.scalars(select(contact_model).order_by(contact_model.id.desc()).limit(25)).all()
        signed_in = {}
        for uid in tokens.values():
            signed_in[uid] = signed_in.get(uid, 0) + 1
        return jsonify({
            "users": [{"id": u.id, "email": u.email, "name": u.name,
                       "created_at": jsonable(u.created_at),
                       "last_seen": jsonable(by_user.get(u.id)),
                       "active_sessions": signed_in.get(u.id, 0)} for u in us],
            "recent_login_ips": [{"ip": k, "last": jsonable(v)} for k, v in logins.items()],
            "contact_messages": [{"id": c.id, "name": c.name, "email": c.email, "company": c.company,
                                  "note": c.note[:200], "created_at": jsonable(c.created_at)} for c in contacts],
        })

    @bp.get("/api/routes")
    def routes_list():
        out = []
        for rule in app.url_map.iter_rules():
            if rule.rule.startswith(PREFIX) or rule.endpoint == "static":
                continue
            out.append({"rule": rule.rule, "methods": sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"}),
                        "endpoint": rule.endpoint})
        return jsonify(sorted(out, key=lambda r: r["rule"]))

    @bp.get("/api/checks")
    def checks():
        client = app.test_client()
        results = []
        for path in ("/health", "/db/ping"):
            t0 = time.perf_counter()
            resp = client.get(path, environ_base={"REMOTE_ADDR": "127.0.0.1"},
                              headers={"User-Agent": "red-dashboard-check"})
            results.append({"path": path, "status": resp.status_code,
                            "ms": round((time.perf_counter() - t0) * 1000, 1), "body": resp.get_json(silent=True)})
        return jsonify(results)

    app.register_blueprint(bp)
