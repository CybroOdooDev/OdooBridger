#!/usr/bin/env python3
"""Bring the core's database to the latest schema (also done at every start).

    python3 tools/db_upgrade.py            # apply anything pending
    python3 tools/db_upgrade.py --check    # report only; exit 1 if behind

See obcore/migrations.py for how a database that predates migrations is adopted.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcore import migrations  # noqa: E402
from obcore.config import settings  # noqa: E402
from obcore.db import engine  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only report; exit 1 if the database is behind.")
    args = parser.parse_args()
    print(f"database: {settings.database_url.split('@')[-1]}")
    head = migrations.head_revision()
    if args.check:
        if migrations.is_legacy(engine):
            print("Not under migrations yet; the next start (or this tool without --check) adopts it.")
            return 1
        now = migrations.current_revision(engine)
        print(f"at {now or 'nothing'}, latest is {head}")
        return 0 if now == head else 1
    revision = migrations.upgrade(engine)
    print(f"database is at {revision} (latest: {head})")
    return 0 if revision == head else 1


if __name__ == "__main__":
    sys.exit(main())
