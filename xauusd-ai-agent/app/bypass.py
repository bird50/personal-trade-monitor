"""Operator waivers for paper research. Off by default and stored as runtime state,
not in policy files, so policy hashes and the paper account are unchanged.

ai:   skip the AI call and the market-structure confirmation (the signal side is used).
risk: skip strategy filters and account rules; data integrity and sizing remain.
"""
import time

from .ai_analyzer import AnalysisResult

# Reason codes from structure.confirmation_blockers.
STRUCTURE_CODES = frozenset({"missing_structure", "structure_not_aligned_or_stale", "broken_pivot_level",
                             "entry_not_between_levels", "stop_or_target_conflicts_with_levels"})
# Strategy filters in RiskManager. Source, freshness, tick, symbol, AI-consistency and
# minimum-quantity checks are integrity checks and are never waived.
STRATEGY_CODES = frozenset({"stop_atr_out_of_range", "spread_atr_too_high", "net_reward_risk_too_low"})
OFF = {"ai": False, "risk": False}


def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS pipeline_bypass (
        id INTEGER PRIMARY KEY CHECK(id=1), ai INTEGER NOT NULL CHECK(ai IN (0,1)),
        risk INTEGER NOT NULL CHECK(risk IN (0,1)), updated_at REAL NOT NULL)""")
    conn.execute("INSERT OR IGNORE INTO pipeline_bypass VALUES(1,0,0,?)", (time.time(),))


def read(conn):
    row = conn.execute("SELECT ai,risk,updated_at FROM pipeline_bypass WHERE id=1").fetchone()
    return {"ai": bool(row[0]), "risk": bool(row[1]), "updated_at": row[2]}


def waivable(bypass):
    codes = set()
    if bypass.get("ai") or bypass.get("risk"):
        codes |= STRUCTURE_CODES
    if bypass.get("risk"):
        codes |= STRATEGY_CODES
    return codes


def still_allowed(report, current):
    """A bypassed approval may only fill while the waivers it relied on are still on."""
    used = report.get("bypass") or OFF
    if used.get("ai") and not current["ai"]:
        return False
    return set(report.get("waived_reason_codes") or ()) <= waivable(current)


class BypassStore:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            ensure(conn)

    def get(self):
        with self.db.connect() as conn:
            return read(conn)

    def set(self, ai=None, risk=None):
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = read(conn)
            target = {"ai": previous["ai"] if ai is None else ai, "risk": previous["risk"] if risk is None else risk}
            changed = [name for name in ("ai", "risk") if target[name] != previous[name]]
            if changed:
                conn.execute("UPDATE pipeline_bypass SET ai=?,risk=?,updated_at=? WHERE id=1",
                             (int(target["ai"]), int(target["risk"]), time.time()))
            current = read(conn)
        return {**current, "changed": changed}


class BypassAnalyzer:
    """Approves the deterministic side without calling a model or checking structure."""
    async def analyze(self, signal):
        return AnalysisResult(decision=signal.side, entry=signal.entry, sl=signal.sl, tp=signal.tp,
                              risk_reward=signal.risk_reward,
                              reasons=["AI bypassed by operator; deterministic signal side used"])
