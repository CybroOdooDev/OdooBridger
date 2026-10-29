"""A stand-in BioTime server for tests_load: 2 terminals and 199 punches
(some from terminals BioTime no longer lists), token login, an optional
delay on the punch list (TX_DELAY seconds) to make syncs slow.

    TX_DELAY=2 python tests_load/fake_biotime.py      # listens on 127.0.0.1:8799
"""
import json, time, os
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
NOW = datetime.utcnow() + timedelta(hours=5, minutes=30)
TERMS = [{"sn": "JHG3261700817", "alias": "A", "ip_address": "1.1.1.1", "area": {"area_name": "HQ"}},
         {"sn": "PYA8262101284", "alias": "B", "ip_address": "1.1.1.2", "area": {"area_name": "HQ"}}]
TX = [{"id": i, "emp_code": "100%d" % (i % 3), "punch_time": (NOW - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
       "upload_time": (NOW - timedelta(hours=i)).strftime("%Y-%m-%d %H:%M:%S"), "punch_state": "0",
       "terminal_sn": ["JHG3261700817", "OLDAUTO", "BIFACE1"][i % 3], "terminal_alias": "x"} for i in range(1, 200)]
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _send(self, body):
        b = json.dumps(body).encode(); self.send_response(200)
        self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0)); self._send({"token": "t"})
    def do_GET(self):
        p = urlparse(self.path).path
        if "terminals" in p: return self._send({"count": 2, "next": None, "data": TERMS})
        if "transactions" in p:
            tail = p.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                row = next((r for r in TX if r["id"] == int(tail)), None)
                if row is None:
                    self.send_response(404); self.send_header("content-length","0"); self.end_headers(); return
                return self._send(row)
            time.sleep(float(os.environ.get("TX_DELAY", "0")))
            return self._send({"count": len(TX), "next": None, "data": TX})
        return self._send({"count": 0, "next": None, "data": []})
ThreadingHTTPServer(("127.0.0.1", 8799), H).serve_forever()
