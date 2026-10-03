"""Local, private dev dashboard for red-api.

Shows the database (tables, columns, row counts, stats), live connections,
recent request traffic, users, contact messages and the API's routes.

It is off unless DASHBOARD_ENABLED=1, and it only answers requests coming
from this machine (127.0.0.1 / ::1), so it stays local while we build.
"""
from __future__ import annotations

import inspect as pyinspect
import os
import re
import sys
import platform
import threading
import time
from collections import deque
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

    # ---- live samples for the graphs (in memory, every 2 s, last 30 min) ----
    _live_lock = threading.Lock()
    _live: dict[str, Any] = {"req": 0, "err": 0, "ms_sum": 0.0, "ms_max": 0.0,
                             "bytes_in": 0, "bytes_out": 0, "status": {}}
    samples: deque[dict[str, Any]] = deque(maxlen=900)
    is_pg = engine.dialect.name == "postgresql"

    def _net_bytes() -> tuple[int, int]:
        rx = tx = 0
        try:
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                f = data.split()
                rx += int(f[0]); tx += int(f[8])
        except Exception:
            pass
        return rx, tx

    def _cpu() -> tuple[int, int]:
        try:
            f = [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:]]
            return sum(f), f[3] + (f[4] if len(f) > 4 else 0)
        except Exception:
            return 0, 0

    def _sampler() -> None:
        prev_net, prev_cpu, prev_t = _net_bytes(), _cpu(), time.time()
        prev_db: dict[str, int] | None = None
        while True:
            time.sleep(2)
            now = time.time(); dt = max(0.001, now - prev_t); prev_t = now
            with _live_lock:
                snap = dict(_live); snap["status"] = dict(_live["status"])
                for k in ("req", "err", "bytes_in", "bytes_out"):
                    _live[k] = 0
                _live["ms_sum"] = 0.0; _live["ms_max"] = 0.0; _live["status"] = {}
            net = _net_bytes(); cpu = _cpu()
            tot, idle = cpu[0] - prev_cpu[0], cpu[1] - prev_cpu[1]
            sample: dict[str, Any] = {
                "t": int(now * 1000),
                "rps": round(snap["req"] / dt, 2), "eps": round(snap["err"] / dt, 2),
                "avg_ms": round(snap["ms_sum"] / snap["req"], 2) if snap["req"] else 0,
                "max_ms": round(snap["ms_max"], 2),
                "app_in_bps": round(snap["bytes_in"] / dt), "app_out_bps": round(snap["bytes_out"] / dt),
                "net_rx_bps": round((net[0] - prev_net[0]) / dt), "net_tx_bps": round((net[1] - prev_net[1]) / dt),
                "cpu_pct": round(100 * (1 - idle / tot), 1) if tot > 0 else 0,
                "load1": round(os.getloadavg()[0], 2), "status": snap["status"],
            }
            try:
                sample["rss_mb"] = round(int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
            except Exception:
                sample["rss_mb"] = None
            prev_net, prev_cpu = net, cpu
            try:
                t0 = time.perf_counter()
                with engine.connect() as c:
                    c.execute(text("SELECT 1"))
                    sample["db_ping_ms"] = round((time.perf_counter() - t0) * 1000, 2)
                    if is_pg:
                        st = c.execute(text("""SELECT count(*) FILTER (WHERE state='active') AS active,
                                  count(*) FILTER (WHERE state LIKE 'idle%') AS idle, count(*) AS total
                                  FROM pg_stat_activity WHERE datname = current_database()""")).mappings().one()
                        sample.update(db_active=st["active"], db_idle=st["idle"], db_total=st["total"])
                        d = c.execute(text("""SELECT xact_commit, xact_rollback, tup_returned, tup_fetched,
                                  tup_inserted + tup_updated + tup_deleted AS tup_written, blks_hit, blks_read
                                  FROM pg_stat_database WHERE datname = current_database()""")).mappings().one()
                        d = {k: int(v or 0) for k, v in d.items()}
                        if prev_db:
                            sample["db_tps"] = round((d["xact_commit"] + d["xact_rollback"] - prev_db["xact_commit"] - prev_db["xact_rollback"]) / dt, 2)
                            sample["db_rows_read_ps"] = round((d["tup_returned"] - prev_db["tup_returned"]) / dt, 1)
                            sample["db_rows_written_ps"] = round((d["tup_written"] - prev_db["tup_written"]) / dt, 2)
                            hit = d["blks_hit"] - prev_db["blks_hit"]; rd = d["blks_read"] - prev_db["blks_read"]
                            sample["db_cache_hit_pct"] = round(100 * hit / (hit + rd), 1) if hit + rd else 100.0
                        prev_db = d
            except Exception as e:  # keep sampling even if the DB blips
                sample["db_error"] = str(e)[:200]
            sample["sessions"] = len(tokens)
            samples.append(sample)

    if os.environ.get("DASHBOARD_ENABLED") == "1":
        threading.Thread(target=_sampler, name="dash-sampler", daemon=True).start()

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
            with _live_lock:
                _live["req"] += 1
                _live["err"] += resp.status_code >= 400
                _live["ms_sum"] += ms
                _live["ms_max"] = max(_live["ms_max"], ms)
                _live["bytes_in"] += request.content_length or 0
                _live["bytes_out"] += resp.calculate_content_length() or 0
                _live["status"][str(resp.status_code)[0] + "xx"] = _live["status"].get(str(resp.status_code)[0] + "xx", 0) + 1
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


    # ---------------- database map, data profile, API map, integrations ----------------
    def _model_tables() -> dict[str, str]:
        """Model class name -> table name for every ORM model on Base."""
        out: dict[str, str] = {}
        for m in Base.registry.mappers:
            t = getattr(m.class_, "__tablename__", None)
            if t:
                out[m.class_.__name__] = t
        return out

    def _singular(name: str) -> str:
        return name[:-1] if name.endswith("s") else name

    @bp.get("/api/dbmap")
    def dbmap():
        insp = inspect(engine)
        names = sorted(insp.get_table_names())
        by_singular = {_singular(n): n for n in names}
        tables, rels = [], []
        sizes: dict[str, dict[str, Any]] = {}
        idx_use: dict[str, dict[str, Any]] = {}
        databases: list[dict[str, Any]] = []
        if is_pg:
            for r in q("""SELECT relname, n_live_tup, pg_total_relation_size(relid) AS total,
                                 pg_relation_size(relid) AS data, pg_indexes_size(relid) AS idx
                          FROM pg_stat_user_tables"""):
                sizes[r["relname"]] = r
            for r in q("""SELECT relname, indexrelname, idx_scan, pg_relation_size(indexrelid) AS size
                          FROM pg_stat_user_indexes"""):
                idx_use[r["indexrelname"]] = r
            databases = rows(q("""SELECT d.datname AS name, pg_database_size(d.datname) AS size_bytes,
                                     pg_size_pretty(pg_database_size(d.datname)) AS size,
                                     (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname) AS connections,
                                     d.datname = current_database() AS is_current
                                  FROM pg_database d WHERE NOT d.datistemplate ORDER BY 1"""))
        else:
            databases = [{"name": engine.url.database or "sqlite", "size_bytes": None, "size": None,
                          "connections": None, "is_current": True}]
        total_rows = 0
        with engine.connect() as conn:
            for n in names:
                cols = insp.get_columns(n)
                pk = set(insp.get_pk_constraint(n).get("constrained_columns") or [])
                uniques = {c for u in insp.get_unique_constraints(n) for c in u["column_names"]}
                indexes = []
                for i in insp.get_indexes(n):
                    if i.get("unique"):
                        uniques.update(c for c in i["column_names"] if c)
                    u = idx_use.get(i["name"], {})
                    indexes.append({"name": i["name"], "columns": [c for c in i["column_names"] if c],
                                    "unique": bool(i.get("unique")), "scans": u.get("idx_scan"),
                                    "size_bytes": u.get("size")})
                if is_pg:
                    for iname, u in idx_use.items():
                        if u["relname"] == n and iname.endswith("_pkey"):
                            indexes.insert(0, {"name": iname, "columns": sorted(pk), "unique": True,
                                               "scans": u["idx_scan"], "size_bytes": u["size"], "primary": True})
                count = conn.execute(text(f'SELECT count(*) FROM "{n}"')).scalar() or 0
                total_rows += count
                for fk in insp.get_foreign_keys(n):
                    rels.append({"from": n, "from_cols": fk["constrained_columns"], "to": fk["referred_table"],
                                 "to_cols": fk["referred_columns"], "kind": "foreign key"})
                declared = {c for fk in insp.get_foreign_keys(n) for c in fk["constrained_columns"]}
                for c in cols:
                    m = re.fullmatch(r"(\w+)_id", c["name"])
                    if m and c["name"] not in declared:
                        target = by_singular.get(m.group(1)) or (m.group(1) if m.group(1) in names else None)
                        if target and target != n:
                            rels.append({"from": n, "from_cols": [c["name"]], "to": target, "to_cols": ["id"],
                                         "kind": "inferred"})
                sz = sizes.get(n, {})
                tables.append({
                    "name": n, "rows": count,
                    "size_bytes": sz.get("total"), "data_bytes": sz.get("data"), "index_bytes": sz.get("idx"),
                    "model": next((k for k, v in _model_tables().items() if v == n), None),
                    "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": bool(c["nullable"]),
                                 "pk": c["name"] in pk, "unique": c["name"] in uniques,
                                 "default": str(c.get("default")) if c.get("default") is not None else None,
                                 "hidden": c["name"] in HIDDEN_COLUMNS} for c in cols],
                    "indexes": indexes,
                })
        return jsonify({"dialect": dialect, "database": engine.url.database, "databases": databases,
                        "tables": tables, "relationships": rels, "total_rows": total_rows,
                        "total_bytes": sum((t["size_bytes"] or 0) for t in tables) if is_pg else None})

    @bp.get("/api/profile/<name>")
    def profile(name: str):
        insp = inspect(engine)
        if name not in insp.get_table_names():
            abort(404)
        cols = insp.get_columns(name)
        out = []
        with engine.connect() as conn:
            total = conn.execute(text(f'SELECT count(*) FROM "{name}"')).scalar() or 0
            ts_col = None
            for c in cols:
                cn, tname = c["name"], str(c["type"]).upper()
                info: dict[str, Any] = {"name": cn, "type": str(c["type"])}
                if cn in HIDDEN_COLUMNS:
                    info["hidden"] = True
                    out.append(info)
                    continue
                qc = f'"{cn}"'
                r = conn.execute(text(f'SELECT count({qc}) AS filled, count(DISTINCT {qc}) AS distinct_n FROM "{name}"')).mappings().one()
                info["nulls"] = total - r["filled"]
                info["distinct"] = r["distinct_n"]
                if any(k in tname for k in ("INT", "FLOAT", "DOUBLE", "NUMERIC", "REAL", "DATE", "TIME")):
                    mm = conn.execute(text(f'SELECT min({qc}) AS mn, max({qc}) AS mx FROM "{name}"')).mappings().one()
                    info["min"], info["max"] = jsonable(mm["mn"]), jsonable(mm["mx"])
                    if any(k in tname for k in ("INT", "FLOAT", "DOUBLE", "NUMERIC", "REAL")) and not c["name"] == "id":
                        info["avg"] = jsonable(conn.execute(text(f'SELECT avg({qc}) FROM "{name}"')).scalar())
                        if info["avg"] is not None:
                            info["avg"] = round(float(info["avg"]), 2)
                if "TIME" in tname or "DATE" in tname:
                    ts_col = ts_col or cn
                if r["distinct_n"] and r["distinct_n"] <= max(25, total // 2) and "TEXT" not in tname:
                    top = conn.execute(text(f'SELECT {qc} AS v, count(*) AS n FROM "{name}" WHERE {qc} IS NOT NULL '
                                            f'GROUP BY {qc} ORDER BY n DESC LIMIT 5')).mappings().all()
                    info["top"] = [{"value": jsonable(t["v"]), "count": t["n"]} for t in top]
                out.append(info)
            growth = []
            if ts_col:
                since = datetime.now(timezone.utc) - timedelta(days=30)
                vals = conn.execute(text(f'SELECT "{ts_col}" FROM "{name}" WHERE "{ts_col}" >= :s'), {"s": since}).scalars().all()
                days: dict[str, int] = {}
                for v in vals:
                    if v is not None:
                        d = (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).date().isoformat()
                        days[d] = days.get(d, 0) + 1
                for i in range(30, -1, -1):
                    d = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
                    growth.append({"day": d, "count": days.get(d, 0)})
        return jsonify({"table": name, "rows": total, "columns": out, "time_column": ts_col, "growth": growth})

    @bp.get("/api/apimap")
    def apimap():
        models = _model_tables()
        ep_counts: dict[str, dict[str, Any]] = {}
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        with SessionLocal() as db:
            for ep, n, errs, avg in db.execute(
                    select(RequestLog.endpoint, func.count(), func.sum((RequestLog.status >= 400).cast(Integer)),
                           func.avg(RequestLog.duration_ms))
                    .where(RequestLog.ts >= since).group_by(RequestLog.endpoint)).all():
                ep_counts[ep or ""] = {"calls_24h": n, "errors_24h": int(errs or 0), "avg_ms": round(float(avg or 0), 1)}
        out = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static" or rule.rule.startswith(PREFIX):
                continue
            fn = app.view_functions.get(rule.endpoint)
            reads, writes, notes = set(), set(), []
            auth = False
            try:
                inner = fn
                while hasattr(inner, "__wrapped__"):
                    auth = auth or inner.__name__ == "wrapper"
                    inner = inner.__wrapped__
                src = pyinspect.getsource(inner)
                auth = auth or "require_auth" in pyinspect.getsource(fn) or "@require_auth" in src
                for cls, table in models.items():
                    if re.search(rf"\b{cls}\b", src):
                        if re.search(rf"db\.add\(\s*{cls}\b|=\s*{cls}\(", src) or ("db.add(" in src and re.search(rf"{cls}\(", src)):
                            writes.add(table)
                        if re.search(rf"select\(\s*{cls}\b|db\.get\(\s*{cls}\b|query\(\s*{cls}\b", src):
                            reads.add(table)
                        if table not in reads and table not in writes:
                            reads.add(table)
                if re.search(r"_tokens\b|_issue_token", src):
                    notes.append("in-memory sessions")
                if "SELECT 1" in src:
                    notes.append("database ping")
                if re.search(r"_send_contact_mail|smtplib|SMTP", src):
                    notes.append("contact email step")
            except (OSError, TypeError):
                pass
            methods = sorted(m for m in rule.methods if m not in {"HEAD", "OPTIONS"})
            out.append({"rule": rule.rule, "methods": methods, "endpoint": rule.endpoint,
                        "reads": sorted(reads), "writes": sorted(writes | {"request_log"}),
                        "auth": auth, "notes": notes, **ep_counts.get(rule.endpoint, {"calls_24h": 0, "errors_24h": 0, "avg_ms": None})})
        out.sort(key=lambda r: r["rule"])
        return jsonify({"routes": out, "models": models})

    SECRET_HINT = re.compile(r"PASSWORD|SECRET|TOKEN|KEY", re.I)

    @bp.get("/api/integrations")
    def integrations():
        mod = sys.modules.get(app.import_name)
        try:
            src = pyinspect.getsource(mod) if mod else ""
        except (OSError, TypeError):
            src = ""
        env = []
        for name in sorted(set(re.findall(r'os\.(?:getenv|environ\.get)\(\s*"([A-Z0-9_]+)"', src)) | {"DASHBOARD_ENABLED"}):
            val = os.environ.get(name)
            shown = None
            if val is not None:
                if SECRET_HINT.search(name):
                    shown = "set (hidden)"
                elif name == "DATABASE_URL":
                    shown = engine.url.render_as_string(hide_password=True)
                else:
                    shown = val
            env.append({"name": name, "set": val is not None, "value": shown})
        cors = []
        m = re.search(r"cors_origins\s*=\s*\[(.*?)\]", src, re.S)
        if m:
            cors = re.findall(r'"([^"]+)"', m.group(1))
        mail_mode = os.environ.get("MAIL_MODE", "log")
        t0 = time.perf_counter()
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            db_ok, db_ms = True, round((time.perf_counter() - t0) * 1000, 2)
        except Exception:
            db_ok, db_ms = False, None
        items = [
            {"name": "Database", "kind": f"{dialect} via {engine.dialect.driver}", "status": "connected" if db_ok else "down",
             "detail": engine.url.render_as_string(hide_password=True), "ping_ms": db_ms},
            {"name": "Email (contact form)", "kind": "SMTP" if mail_mode == "smtp" else "log only",
             "status": "configured" if mail_mode != "smtp" or os.environ.get("SMTP_HOST") else "missing SMTP_HOST",
             "detail": (f"SMTP {os.environ.get('SMTP_HOST')}" if mail_mode == "smtp" else "Messages are saved to contact_messages and written to the log, not emailed.")},
            {"name": "Sessions", "kind": "in-memory tokens", "status": f"{len(tokens)} active",
             "detail": "Tokens live in the API process and reset when it restarts."},
            {"name": "Browser apps allowed (CORS)", "kind": f"{len(cors)} origins", "status": "configured" if cors else "none",
             "detail": ", ".join(cors)},
            {"name": "Request log", "kind": "request_log table", "status": "recording",
             "detail": "Every API call except this dashboard."},
        ]
        return jsonify({"integrations": items, "env": env, "cors": cors})

    @bp.get("/api/live")
    def live():
        since = int(request.args.get("since", 0))
        return jsonify({"interval_s": 2, "samples": [x for x in samples if x["t"] > since]})

    app.register_blueprint(bp)
