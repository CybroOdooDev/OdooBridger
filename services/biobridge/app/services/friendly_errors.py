"""Short, customer-facing wording for a failed connection test.

The technical text an integration raises ("HTTP 404 on /json/2/res.users/...")
is for us; the customer who clicked Test Connection needs to know what to check.
``friendly_odoo_message`` maps the failure to one short sentence and the full
detail goes to the server log.
"""

from __future__ import annotations

import re

from app.integrations.odoo import OdooAuthError, OdooError
from app.services.connections import UnsafeTargetError


def friendly_odoo_message(exc: Exception) -> str:
    text = str(exc)
    low = text.lower()

    if isinstance(exc, UnsafeTargetError):
        return ("Connection failed. This address is on a private network and can't be reached from the internet. "
                "Use the public address of your Odoo, with its port forwarded.")

    if isinstance(exc, OdooAuthError) or "rejected the credentials" in low or "refused the authentication" in low:
        if "no database named" in low:
            return "Connection failed. Check the Database name."
        return "Login failed. Check the Database, Login and API Key."
    if "must start with http" in low:
        return "Check the Server URL. It must start with https://"
    if "no path" in low or "just the server address" in low:
        return "Check the Server URL. Enter only the site address, with no path after it."
    if "cannot resolve the host" in low:
        return "Connection failed. The Server URL could not be found. Check it for typos."
    if "tls" in low or "ssl" in low or "certificate" in low:
        return "Connection failed. The secure connection could not be made. Check the Server URL."
    if "web page instead of" in low:
        return "Connection failed. The Server URL does not look like an Odoo site."
    if "timed out" in low or "connection refused" in low or "unreachable" in low:
        return "Connection failed. The site did not respond. Check that it is running."
    # A transport failure names its status and the call: "(HTTP 404 on /json/2/hr.attendance/create)".
    # Each status gets its own sentence; one catch-all here used to hide redirects, firewalls and
    # missing apps behind "check the URL", which the customer could not act on.
    m = re.search(r"http (\d{3}) on (/json/2/([\w.]+)/\w+|/xmlrpc/2/\w+)", low)
    status = int(m.group(1)) if m else None
    model = m.group(3) if m and m.group(3) else None
    if status in (301, 302, 307, 308):
        return "Connection failed. The Server URL redirects to another address. Enter the address it redirects to."
    if status == 401:
        return "Connection failed. The site asks for a separate password before Odoo (a protected site)."
    if status == 403:
        return ("Connection failed. A firewall or proxy in front of Odoo is blocking BioBridge. "
                "Allow Odoo's API (/json/2) from BioBridge's address.")
    if status == 404 and model and model not in ("res.users", "res.company"):
        app = {"hr.attendance": "Attendances", "hr.employee": "Employees"}.get(model)
        return (f"Connected to Odoo, but it has no {model} model. "
                + (f"Install the {app} app in this database." if app else "Check the apps installed in this database."))
    if status in (502, 503, 504) or "bad gateway" in low:
        return "Connection failed. The Odoo site is not responding. It may be inactive or restarting."
    if status == 404 or "no odoo api endpoint" in low or "could not reach odoo's api" in low:
        return "Connection failed. Check the Server URL, and that the Odoo site is active."
    if isinstance(exc, OdooError):
        # Already plain and actionable ("This Odoo user cannot see company id 7")
        # reads better as it is; only technical-looking text is replaced.
        technical = ("http ", "/json", "/xmlrpc", "traceback", "keyerror", "fault", "errno", "<html")
        if len(text) <= 160 and not any(t in low for t in technical):
            return text
        return "Connection failed. Check your Odoo details and that the site is active."
    return "Connection failed. Please try again."
