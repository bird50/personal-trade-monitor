"""Create local bridge credentials without storing MT5 account passwords."""
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
path = root / ".env.mt5"
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print(".env.mt5 exists; left unchanged.")
else:
    with os.fdopen(fd, "w") as file:
        file.write("MT5_BRIDGE_TOKEN=" + secrets.token_urlsafe(32) + "\n"
                   "MT5_SERVER=XMGlobal-MT5 7\n"
                   "MT5_SYMBOLS=GOLD,GOLD#,GOLDmicro,XAUUSD\n"
                   "MT5_DATABASE_PATH=data/mt5-market.db\n")
    print("Created .env.mt5 (0600).")
token = dotenv_values(path).get("MT5_BRIDGE_TOKEN", "")
if not token:
    raise SystemExit("MT5_BRIDGE_TOKEN is missing; token export skipped.")
export = root / "mt5/bridge-token.txt"
try:
    fd = os.open(export, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    if export.read_text().strip() != token:
        raise SystemExit("Existing token export differs; no files overwritten.")
else:
    with os.fdopen(fd, "w") as file:
        file.write(token + "\n")
print("EA token file ready: mt5/bridge-token.txt (keep private).")
