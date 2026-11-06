"""Odoo (or a proxy in front of it) answering an XML-RPC call with something that
isn't XML — an HTML error page, an empty body — is an Odoo error the run explains,
not an "Unexpected error: not well-formed (invalid token)"."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.integrations.odoo import OdooClient, OdooCredentials, OdooError


class _HtmlEverywhere(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        # Plain text is what raises "not well-formed (invalid token): line 1, column 0".
        body = b"upstream connect error or disconnect/reset before headers" if "object" in self.path else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def odoo_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HtmlEverywhere)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _client(url: str) -> OdooClient:
    client = OdooClient(OdooCredentials(url=url, db="db", username="u", api_key="k", uid=2, api="xmlrpc"))
    client._uid = 2
    return client


def test_a_plain_text_reply_from_the_object_endpoint_is_an_odoo_error(odoo_url):
    with pytest.raises(OdooError, match="isn't Odoo API data"):
        _client(odoo_url).execute("hr.attendance", "search", [[]], scope_to_company=False)


def test_an_empty_reply_to_sign_in_is_an_odoo_error(odoo_url):
    with pytest.raises(OdooError, match="isn't Odoo API data"):
        _client(odoo_url).authenticate()
