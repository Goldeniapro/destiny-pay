"""Persistent memory of each business (SQLite).

One file, WAL mode, one lock: simple and safe for a single-instance web service.
Everything a business owner tells Destiny Pay lives here: contacts, invoices it created,
bills to pay, money in and out, reminders, remembered facts, the activity log, pending
confirmations and alerts for the UI.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, lang TEXT NOT NULL, tz TEXT NOT NULL, currency TEXT NOT NULL,
  demo INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'customer', notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
  UNIQUE(ws, name COLLATE NOCASE));
CREATE TABLE IF NOT EXISTS invoices(
  id TEXT PRIMARY KEY, ws TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  total REAL NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'USD', due_date TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL, refreshed_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, payee TEXT NOT NULL, payee_email TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '', amount REAL NOT NULL, currency TEXT NOT NULL, due_date TEXT NOT NULL,
  recurrence TEXT NOT NULL DEFAULT 'none', status TEXT NOT NULL DEFAULT 'pending', method TEXT NOT NULL DEFAULT '',
  paid_at REAL, payout_batch_id TEXT NOT NULL DEFAULT '', alerted INTEGER NOT NULL DEFAULT 0, anchor_day INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS movements(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, kind TEXT NOT NULL, description TEXT NOT NULL,
  amount REAL NOT NULL, currency TEXT NOT NULL, method TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
  contact TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS movements_ref ON movements(ws, ref) WHERE ref != '';
CREATE TABLE IF NOT EXISTS reminders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, text TEXT NOT NULL, due_at REAL NOT NULL,
  kind TEXT NOT NULL DEFAULT 'owner_note', invoice_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
  result TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, fired_at REAL);
CREATE TABLE IF NOT EXISTS facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, fact TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ws TEXT NOT NULL, at REAL NOT NULL, key TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS confirmations(
  id TEXT PRIMARY KEY, ws TEXT NOT NULL, conversation TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, target TEXT NOT NULL,
  details TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS payouts(
  sender_batch_id TEXT PRIMARY KEY, ws TEXT NOT NULL, bill_id INTEGER NOT NULL, status TEXT NOT NULL,
  batch_id TEXT NOT NULL DEFAULT '', amount REAL NOT NULL DEFAULT 0, checked_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  followed_up INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS fake_paypal(id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS invoices_ws ON invoices(ws);
CREATE INDEX IF NOT EXISTS bills_ws ON bills(ws);
CREATE INDEX IF NOT EXISTS movements_ws ON movements(ws, at);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
CREATE INDEX IF NOT EXISTS activity_ws ON activity(ws, id);
CREATE INDEX IF NOT EXISTS alerts_ws ON alerts(ws, id);
"""


class Store:
    def __init__(self, path: str | None = None) -> None:
        path = path or os.environ.get("DP_DB_PATH", "data/destiny_pay.db")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.executescript(SCHEMA)
            for table, column, decl in (("bills", "anchor_day", "INTEGER NOT NULL DEFAULT 0"), ("payouts", "amount", "REAL NOT NULL DEFAULT 0"),
                                        ("payouts", "checked_at", "REAL NOT NULL DEFAULT 0"), ("payouts", "followed_up", "INTEGER NOT NULL DEFAULT 0")):
                cols = {r[1] for r in self._db.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in cols:  # older database: add the column in place
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # --- low level -----------------------------------------------------------
    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # --- workspaces ----------------------------------------------------------
    def create_workspace(self, ws: str, name: str, lang: str, tz: str, currency: str, demo: bool) -> None:
        now = time.time()
        self.x("INSERT INTO workspaces(id,name,lang,tz,currency,demo,created_at,last_seen) VALUES(?,?,?,?,?,?,?,?)",
               (ws, name, lang, tz, currency, int(demo), now, now))

    def workspace(self, ws: str) -> dict | None:
        return self.one("SELECT * FROM workspaces WHERE id=?", (ws,))

    def touch(self, ws: str) -> None:
        self.x("UPDATE workspaces SET last_seen=? WHERE id=?", (time.time(), ws))

    def active_workspaces(self, within: float) -> list[str]:
        return [r["id"] for r in self.q("SELECT id FROM workspaces WHERE last_seen>?", (time.time() - within,))]

    def count_recent_workspaces(self, within: float) -> int:
        return self.one("SELECT COUNT(*) n FROM workspaces WHERE created_at>?", (time.time() - within,))["n"]

    def purge(self, idle_seconds: float, keep_activity: int = 300, keep_alerts: int = 200) -> int:
        """Delete businesses nobody opened for a while (nothing pending on PayPal or in reminders) and trim logs.

        A public demo creates a business per visitor: without this the disk fills up."""
        cutoff, now = time.time() - idle_seconds, time.time()
        stale = [r["id"] for r in self.q(
            "SELECT id FROM workspaces w WHERE last_seen<? "
            "AND NOT EXISTS(SELECT 1 FROM payouts p WHERE p.ws=w.id AND p.status NOT IN ('done','failed')) "
            "AND NOT EXISTS(SELECT 1 FROM reminders r WHERE r.ws=w.id AND r.status='pending' AND r.due_at>?) LIMIT 500", (cutoff, now))]
        with self._lock:
            for ws in stale:
                self._db.execute("DELETE FROM fake_paypal WHERE id IN (SELECT id FROM invoices WHERE ws=?)", (ws,))
                for table in ("contacts", "invoices", "bills", "movements", "reminders", "facts", "activity", "alerts", "confirmations", "payouts"):
                    self._db.execute(f"DELETE FROM {table} WHERE ws=?", (ws,))
                self._db.execute("DELETE FROM workspaces WHERE id=?", (ws,))
            for ws in [r["ws"] for r in self.q("SELECT ws FROM activity GROUP BY ws HAVING COUNT(*)>?", (keep_activity,))]:
                self._db.execute("DELETE FROM activity WHERE ws=? AND id NOT IN (SELECT id FROM activity WHERE ws=? ORDER BY id DESC LIMIT ?)", (ws, ws, keep_activity))
            for ws in [r["ws"] for r in self.q("SELECT ws FROM alerts GROUP BY ws HAVING COUNT(*)>?", (keep_alerts,))]:
                self._db.execute("DELETE FROM alerts WHERE ws=? AND id NOT IN (SELECT id FROM alerts WHERE ws=? ORDER BY id DESC LIMIT ?)", (ws, ws, keep_alerts))
            self._db.execute("DELETE FROM confirmations WHERE status!='pending' AND created_at<?", (now - 7 * 86400,))
        return len(stale)

    # --- activity and alerts -------------------------------------------------
    def log(self, ws: str, kind: str, text: str, data: dict | None = None) -> None:
        self.x("INSERT INTO activity(ws,at,kind,text,data) VALUES(?,?,?,?,?)",
               (ws, time.time(), kind, text[:500], json.dumps(data or {}, ensure_ascii=False)))

    def alert(self, ws: str, key: str, **params: Any) -> None:
        self.x("INSERT INTO alerts(ws,at,key,params) VALUES(?,?,?,?)", (ws, time.time(), key, json.dumps(params, ensure_ascii=False)))

    def alerts_since(self, ws: str, since_id: int) -> list[dict]:
        rows = self.q("SELECT * FROM alerts WHERE ws=? AND id>? ORDER BY id LIMIT 20", (ws, since_id))
        for r in rows:
            r["params"] = json.loads(r["params"])
        return rows

    def last_alert_id(self, ws: str) -> int:
        return self.one("SELECT COALESCE(MAX(id),0) m FROM alerts WHERE ws=?", (ws,))["m"]

    # --- fake PayPal persistence (demo mode) ---------------------------------
    def fake_get(self, key: str) -> dict | None:
        row = self.one("SELECT data FROM fake_paypal WHERE id=?", (key,))
        return json.loads(row["data"]) if row else None

    def fake_put(self, key: str, data: dict) -> None:
        self.x("INSERT INTO fake_paypal(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
               (key, json.dumps(data, ensure_ascii=False)))
