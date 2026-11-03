from app.integrations.odoo import OdooAuthError, OdooError
from app.services.friendly_errors import friendly_odoo_message as friendly


def test_the_screenshot_error_becomes_one_short_sentence():
    raw = OdooError("Could not reach Odoo's API at https://x.easyinstance.com: there is no Odoo API "
                    "endpoint there. The URL is wrong — most often it has a path on the end. "
                    "(HTTP 404 on /json/2/res.users/context_get)")
    msg = friendly(raw)
    assert "Server URL" in msg and "active" in msg
    assert "404" not in msg and "json/2" not in msg and len(msg) < 90


def test_each_common_failure_says_what_to_check():
    assert "Login failed" in friendly(OdooAuthError("Odoo rejected the credentials. The API reached Odoo"))
    assert "Database" in friendly(OdooAuthError("Odoo has no database named 'x'."))
    assert "could not be found" in friendly(OdooError("Cannot resolve the host in https://x: boom"))
    assert "did not respond" in friendly(OdooError("Timed out connecting to https://x. A firewall"))
    assert "did not respond" in friendly(OdooError("Connection refused by https://x. Nothing is listening there."))
    assert "inactive" in friendly(OdooError("... (HTTP 502 on /json/2/x/y)"))
    assert "secure connection" in friendly(OdooError("TLS failed for https://x: bad"))
    assert "no path" in friendly(OdooError("The Odoo URL must be just the server address, with no path.")).lower()


def test_unknown_errors_never_leak_internals():
    msg = friendly(OdooError("Traceback KeyError: 'foo' at line 12"))
    assert "Traceback" not in msg and msg.startswith("Connection failed")


def test_each_http_status_keeps_its_own_cause():
    def raw(status, call="/json/2/res.users/context_get"):
        return OdooError(f"Could not reach Odoo's API at https://x: hint (HTTP {status} on {call})")
    assert "redirects" in friendly(raw(301))
    assert "firewall" in friendly(raw(403))
    assert "not responding" in friendly(raw(502))
    assert "Server URL" in friendly(raw(404))
    missing = friendly(raw(404, "/json/2/hr.attendance/create"))
    assert "hr.attendance" in missing and "Attendances" in missing


def test_no_database_selected_points_at_the_database_name():
    import httpx
    from app.integrations.odoo import _json2_error
    resp = httpx.Response(404, text="<!DOCTYPE html><title>404 Not Found</title><p>No database is selected "
                          "and the requested URL was not found in the server-wide controllers.</p>")
    err = _json2_error("https://x", "res.users", "context_get", "bio_bridge_v19", resp)
    assert isinstance(err, OdooAuthError) and "bio_bridge_v19" in str(err)
    assert friendly(err) == "Connection failed. Check the Database name."
