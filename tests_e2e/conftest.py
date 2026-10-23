"""Run the OdooBridger core and BioBridge (platform mode) in one process.

Each app reads DATABASE_URL when its package is first imported, so the core is
imported with its own database, then BioBridge with another. Service-to-service
HTTP is routed in-process: the core's provisioning client goes to BioBridge's
app, BioBridge's calls to the core (public key, code exchange) go to the core's.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "core"), str(ROOT / "services" / "biobridge")]
for _var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_var, None)

_tmp = tempfile.mkdtemp(prefix="ob-e2e-")
SERVICE_SECRET = "e2e-biometric-secret"

# --- the core first ----------------------------------------------------------
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/core.db",
    "RUN_BACKGROUND_JOBS": "false",
    "PUBLIC_BASE_URL": "http://core.test",
    "BIOMETRIC_CLIENT_SECRET": SERVICE_SECRET,
    "BIOMETRIC_BASE_URL": "https://biometric.test",
    "BIOMETRIC_INTERNAL_URL": "http://biobridge.test",
    "STAFF_EMAIL": "staff@odoobridger.io",
    "STAFF_PASSWORD": "Staff-Password-1",
})
import obcore.main  # noqa: E402,F401

# --- then BioBridge, in platform mode -----------------------------------------
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/biobridge.db",
    "SCHEDULER_MODE": "off",
    "PLATFORM_MODE": "true",
    "PLATFORM_CORE_URL": "http://core.test",
    "PLATFORM_CLIENT_SECRET": SERVICE_SECRET,
    "STAFF_EMAIL": "",
    "STAFF_PASSWORD": "",
})
import app.main  # noqa: E402,F401
