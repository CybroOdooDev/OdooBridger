#!/usr/bin/env python3
"""Create deploy/.env with fresh random secrets (never overwrites an existing one).

    python3 deploy/make-env.py                     # then: docker compose -f deploy/docker-compose.yml up -d --build
    python3 deploy/make-env.py --staff you@x.com   # also sets the staff login (a password is generated)
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

here = Path(__file__).parent
target, example = here / ".env", here / ".env.example"

p = argparse.ArgumentParser()
p.add_argument("--staff", help="staff email for the /staff consoles", default="staff@odoobridger.local")
p.add_argument("--force", action="store_true", help="replace an existing .env")
args = p.parse_args()

if target.exists() and not args.force:
    sys.exit(f"{target} already exists; edit it, or run with --force to replace it.")

gen = lambda: secrets.token_urlsafe(48)
values = {k: gen() for k in ("POSTGRES_PASSWORD", "CORE_JWT_SECRET", "CORE_MASTER_KEY",
                             "BIOBRIDGE_JWT_SECRET", "BIOBRIDGE_MASTER_KEY", "BIOMETRIC_CLIENT_SECRET")}
values["POSTGRES_PASSWORD"] = secrets.token_hex(16)
values["STAFF_EMAIL"] = args.staff
values["STAFF_PASSWORD"] = secrets.token_urlsafe(12)

text = example.read_text()
for key, val in values.items():
    text = re.sub(rf"^{key}=.*$", lambda m: f"{key}={val}", text, flags=re.M)
target.write_text(text)
target.chmod(0o600)
print(f"Wrote {target}")
print(f"Staff login: {values['STAFF_EMAIL']} / {values['STAFF_PASSWORD']}")
