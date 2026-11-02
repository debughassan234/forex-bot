"""Tiny web server for the dashboard.

Serves the page only at  http://<server-ip>:8080/<secret-token>/  — every other
address gets "Not found", so the page is private to whoever has the link.
The token lives in .dashboard_token (created by dashboard_setup.sh).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dashboard

PORT = int(os.environ.get("DASH_PORT", "8080"))
TOKEN_FILE = os.path.join(dashboard.BOT_DIR, ".dashboard_token")


def load_token():
    try:
        tok = open(TOKEN_FILE).read().strip()
    except OSError:
        tok = ""
    if len(tok) < 16:
        sys.exit("No dashboard token — run: bash dashboard_setup.sh")
    return tok


TOKEN = load_token()
BASE = f"/{TOKEN}/"


class Handler(BaseHTTPRequestHandler):
    server_version = "fibdash"
    sys_version = ""

    def log_message(self, *args):        # never write the secret link into logs
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == BASE.rstrip("/"):
            self.send_response(301)
            self.send_header("Location", BASE)
            self.end_headers()
            return
        if path == BASE + "bg":
            try:
                data = open(dashboard.BG_FILE, "rb").read()
            except OSError:
                return self._send(404, "Not found", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=31536000")
            self.end_headers()
            self.wfile.write(data)
            return
        if path != BASE:
            return self._send(404, "Not found", "text/plain")
        try:
            self._send(200, dashboard.render())
        except Exception as e:
            self._send(500, f"Dashboard error: {e}", "text/plain")

    def do_POST(self):
        if self.path.split("?", 1)[0] == BASE + "bg":
            return self._upload_bg()
        if self.path.split("?", 1)[0] != BASE + "api":
            return self._send(404, "Not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not 0 < n <= 4096:
            return self._send(400, json.dumps({"ok": False, "msg": "Bad request"}), "application/json")
        try:
            body = json.loads(self.rfile.read(n))
            if not isinstance(body, dict):
                raise ValueError
            ok, msg = dashboard.apply_action(body)
        except Exception:
            ok, msg = False, "Bad request"
        self._send(200 if ok else 400, json.dumps({"ok": ok, "msg": msg}), "application/json")


    def _upload_bg(self):
        """Background photo from the page: already shrunk to a JPEG in the browser."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not 0 < n <= 8 * 1024 * 1024:
            return self._send(400, json.dumps({"ok": False, "msg": "Picture too large (max 8 MB)"}), "application/json")
        data = self.rfile.read(n)
        if not data.startswith(b"\xff\xd8\xff"):
            return self._send(400, json.dumps({"ok": False, "msg": "Not a JPEG picture"}), "application/json")
        os.makedirs(os.path.dirname(dashboard.BG_FILE), exist_ok=True)
        tmp = dashboard.BG_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dashboard.BG_FILE)
        self._send(200, json.dumps({"ok": True, "msg": "Background updated"}), "application/json")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dashboard on port {PORT}", flush=True)
    srv.serve_forever()
