"""Pin the current config/paper-xm.json + config/risk-xm-paper.json policy to the Paper account.

The account stores the policy hash it was funded under; after an intentional policy edit the
summary shows policy_matches=false and new entries stay blocked until an operator adopts it:

  .venv/bin/python scripts/adopt_paper_policy.py                      # show what would change
  .venv/bin/python scripts/adopt_paper_policy.py --reason "..." --confirm

Balance, ledger, positions and tick cursor are unchanged; the previous policy is kept in the
paper_policy_history table. Refused while a position is open. Back up data/xm-trading.db first.
The running XM service picks up the new account hash without a restart only if it already
loaded the same config files; otherwise restart it after adopting.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from app.paper_trading import PaperEngine, PaperPolicy  # noqa: E402
from app.risk_manager import RiskManager, RiskPolicy  # noqa: E402
from app.xm_main import xm_settings  # noqa: E402


def changed_keys(old: dict, new: dict, prefix=""):
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            out += changed_keys(a, b, f"{prefix}{key}.")
        elif a != b:
            out.append({"key": prefix + key, "account": a, "config": b})
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reason", default="")
    parser.add_argument("--confirm", action="store_true", help="write the change (default is a dry run)")
    args = parser.parse_args()
    config = xm_settings()
    if not config.database_path.is_file():
        raise SystemExit(f"Database not found: {config.database_path}")
    with sqlite3.connect(f"file:{config.database_path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT policy_hash, policy_json FROM paper_account WHERE id=1").fetchone()
    if row is None:
        raise SystemExit("Paper account is not funded yet")
    risk = RiskManager(RiskPolicy.load(config.risk_policy_path))
    paper_policy = PaperPolicy.load(ROOT / "config/paper-xm.json")
    combined = {"paper": paper_policy.model_dump(mode="json"), "risk": risk.snapshot}
    report = {"account_policy_hash": row[0], "changes": changed_keys(json.loads(row[1]), combined)}
    if not args.confirm:
        print(json.dumps(report | {"dry_run": True}, indent=2))
        return
    if not args.reason.strip():
        raise SystemExit("--reason is required with --confirm")
    engine = PaperEngine(Database(config.database_path), paper_policy, risk)
    try:
        result = engine.adopt_policy(args.reason)
    except ValueError as error:
        raise SystemExit(str(error))
    print(json.dumps(report | result, indent=2))


if __name__ == "__main__":
    main()
