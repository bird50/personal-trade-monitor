"""Create local tokens once without printing or overwriting them."""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
template = (root / ".env.example").read_text()
template = template.replace("WEBHOOK_TOKEN=\n", f"WEBHOOK_TOKEN={secrets.token_urlsafe(32)}\n")
template = template.replace("API_TOKEN=\n", f"API_TOKEN={secrets.token_urlsafe(32)}\n")
try:
    fd = os.open(root / ".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists; left unchanged.")
with os.fdopen(fd, "w") as file:
    file.write(template)
print("Created .env with separate random tokens and permissions 0600.")
