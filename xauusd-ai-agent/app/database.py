import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import Signal


class SignalConflict(Exception):
    """An existing signal ID was reused with a different payload."""


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("Database schema is newer than this application")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY,
                    signal_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
                    bar_close_ms INTEGER NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received' CHECK (status = 'received'),
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS signals_bar_close ON signals(bar_close_ms)")
            # Additive migration: preserve original signal rows and their received status.
            db.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    signal_record_id INTEGER PRIMARY KEY REFERENCES signals(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','processing','completed','failed','expired')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    lease_until REAL,
                    lease_token TEXT,
                    model TEXT,
                    prompt_version TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    updated_at REAL NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS analyses_queue ON analyses(status, next_attempt_at)")
            columns={row[1] for row in db.execute('PRAGMA table_info(analyses)')}
            if 'provider' not in columns:
                db.execute("ALTER TABLE analyses ADD COLUMN provider TEXT NOT NULL DEFAULT 'lm_studio'")
            db.execute("INSERT OR IGNORE INTO analyses(signal_record_id, updated_at) SELECT id, ? FROM signals", (time.time(),))
            db.execute("""CREATE TABLE IF NOT EXISTS risk_evaluations (
                signal_record_id INTEGER PRIMARY KEY REFERENCES signals(id),
                report_json TEXT NOT NULL
            )""")
            db.execute("PRAGMA user_version=2")

    def health(self):
        with self.connect() as db:
            db.execute("SELECT id FROM signals LIMIT 1").fetchone()

    def insert(self, signal: Signal):
        with self.connect() as db:
            return self.insert_in_transaction(db, signal)

    def insert_in_transaction(self, db, signal: Signal):
        """Allow source checkpoint and queued signal to commit together."""
        payload = json.dumps(signal.model_dump(exclude={"source"} if signal.source is None else set()), sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        received_at = datetime.now(timezone.utc).isoformat()
        row = db.execute("""
                INSERT INTO signals
                    (signal_id, symbol, side, bar_close_ms, received_at, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO NOTHING RETURNING id
            """, (signal.signal_id, signal.symbol, signal.side, signal.bar_close_ms, received_at, payload, digest)).fetchone()
        if row is not None:
            db.execute("INSERT INTO analyses(signal_record_id, updated_at) VALUES (?, ?)", (row["id"], time.time()))
            return row["id"], True
        existing = db.execute("SELECT id, payload_sha256 FROM signals WHERE signal_id = ?", (signal.signal_id,)).fetchone()
        if existing["payload_sha256"] != digest:
            raise SignalConflict()
        return existing["id"], False

    @staticmethod
    def decode(row):
        return {
            "id": row["id"], "signal_id": row["signal_id"],
            "received_at": row["received_at"], "status": row["status"],
            "payload": json.loads(row["payload_json"]),
        }

    def list_signals(self, after_id: int, limit: int):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM signals WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [self.decode(row) for row in rows]

    def get_signal(self, record_id: int):
        with self.connect() as db:
            row = db.execute("SELECT * FROM signals WHERE id = ?", (record_id,)).fetchone()
        return None if row is None else self.decode(row)

    def get_analysis(self, record_id: int, policy_sha256: str | None = None):
        with self.connect() as db:
            row = db.execute("SELECT * FROM analyses WHERE signal_record_id = ?", (record_id,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"]) if row["result_json"] else None
        risk = self.get_risk(record_id)
        policy_matches = risk is not None and (policy_sha256 is None or risk["policy_sha256"] == policy_sha256)
        risk_valid = policy_matches and risk["status"] == "passed" and time.time() <= risk["expires_at"]
        effective = result["decision"] if row["status"] == "completed" and result and risk_valid else "WAIT"
        return {
            "signal_record_id": row["signal_record_id"], "status": row["status"],
            "attempts": row["attempts"], "model": row["model"], "prompt_version": row["prompt_version"],
            "provider":row['provider'],
            "result": result, "error_code": row["error_code"], "updated_at": row["updated_at"],
            "ai_decision": result["decision"] if result else "WAIT",
            "risk_status": ("not_evaluated" if risk is None else "policy_changed" if not policy_matches else "expired" if time.time() > risk["expires_at"] else risk["status"]),
            "effective_decision": effective,
            "confirmed_quantity": risk["approved_quantity"] if effective in ("BUY", "SELL") else None,
            "mode": "paper_simulation",
        }

    def claim_analysis(self, model: str, prompt_version: str, timeout: int, max_attempts: int, max_age: int,provider:str='lm_studio'):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE analyses SET status='expired', error_code='stale_signal', updated_at=?
                WHERE (status='pending' OR (status='processing' AND lease_until <= ?))
                AND signal_record_id IN (SELECT id FROM signals WHERE bar_close_ms < ?)""",
                (now, now, (now - max_age) * 1000))
            db.execute("""UPDATE analyses SET status='failed', error_code='attempts_exhausted', updated_at=?
                WHERE attempts >= ? AND (status='pending' OR (status='processing' AND lease_until <= ?))""",
                (now, max_attempts, now))
            row = db.execute("""SELECT a.signal_record_id, a.attempts, s.payload_json
                FROM analyses a JOIN signals s ON s.id=a.signal_record_id
                WHERE (a.status='pending' AND a.next_attempt_at <= ?)
                   OR (a.status='processing' AND a.lease_until <= ?)
                ORDER BY a.signal_record_id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("""UPDATE analyses SET status='processing', attempts=attempts+1,
                lease_until=?, lease_token=?, model=?, prompt_version=?, updated_at=?,provider=?
                WHERE signal_record_id=?""",
                (now + timeout + 15, token, model, prompt_version, now,provider,row["signal_record_id"]))
            return {"id": row["signal_record_id"], "attempt": row["attempts"] + 1,
                    "lease_token": token, "payload": json.loads(row["payload_json"])}

    def finish_analysis(self, job: dict, status: str, result: dict | None, error_code: str | None, retry_delay: int = 0, risk_report: dict | None = None):
        now = time.time()
        with self.connect() as db:
            cursor = db.execute("""UPDATE analyses SET status=?, result_json=?, error_code=?,
                next_attempt_at=?, lease_until=NULL, lease_token=NULL, updated_at=?
                WHERE signal_record_id=? AND status='processing' AND lease_token=?""",
                (status, json.dumps(result, allow_nan=False) if result is not None else None,
                 error_code, now + retry_delay, now, job["id"], job["lease_token"]))
            if cursor.rowcount == 1:
                # Persist AI + risk atomically under the same lease guard.
                if risk_report is not None and status == "completed":
                    db.execute("INSERT INTO risk_evaluations(signal_record_id, report_json) VALUES (?, ?) ON CONFLICT(signal_record_id) DO UPDATE SET report_json=excluded.report_json",
                               (job["id"], json.dumps(risk_report, allow_nan=False)))
                else:
                    db.execute("DELETE FROM risk_evaluations WHERE signal_record_id=?", (job["id"],))
                return True
            return False

    def get_risk(self, record_id: int):
        with self.connect() as db:
            row = db.execute("SELECT report_json FROM risk_evaluations WHERE signal_record_id=?", (record_id,)).fetchone()
        return json.loads(row["report_json"]) if row else None
