"""Tiny web server for the dashboard.

Two locks:
  1. a secret link  http://<server-ip>:<port>/<secret-token>/  (anything else: "Not found")
  2. a password, asked once per device, remembered for 90 days

The token is in .dashboard_token, the password hash in .dashboard_pass, logged-in
devices in .dashboard_sessions.json — all created by dashboard_setup.sh.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dashboard

PORT = int(os.environ.get("DASH_PORT", "8080"))
D = dashboard.BOT_DIR
TOKEN_FILE = os.path.join(D, ".dashboard_token")
PASS_FILE = os.path.join(D, ".dashboard_pass")
SESS_FILE = os.path.join(D, ".dashboard_sessions.json")
SESSION_DAYS = 90
COOKIE = "fibdash"
LOCK = threading.Lock()
FAILS = {}               # ip -> [count, first_fail_time]


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


# ---------------------------------------------------------------- password
def check_password(pw):
    """.dashboard_pass holds  pbkdf2_sha256$iterations$salt_hex$hash_hex"""
    try:
        algo, it, salt, want = open(PASS_FILE).read().strip().split("$")
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return algo == "pbkdf2_sha256" and hmac.compare_digest(got, want)
    except Exception:
        return False


def _sessions():
    try:
        s = json.load(open(SESS_FILE))
        return s if isinstance(s, dict) else {}
    except Exception:
        return {}


def _save_sessions(s):
    tmp = SESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, SESS_FILE)


def new_session():
    raw = secrets.token_urlsafe(32)
    with LOCK:
        s = {k: v for k, v in _sessions().items() if v > time.time()}
        s[hashlib.sha256(raw.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
        _save_sessions(s)
    return raw


def valid_session(raw):
    if not raw:
        return False
    exp = _sessions().get(hashlib.sha256(raw.encode()).hexdigest())
    return bool(exp and exp > time.time())


def end_session(raw):
    if not raw:
        return
    with LOCK:
        s = _sessions()
        s.pop(hashlib.sha256(raw.encode()).hexdigest(), None)
        _save_sessions(s)


def locked_out(ip):
    n, t0 = FAILS.get(ip, (0, 0))
    if time.time() - t0 > 900:           # failures forgotten after 15 minutes
        FAILS.pop(ip, None)
        return False
    return n >= 5


def note_fail(ip):
    n, t0 = FAILS.get(ip, (0, time.time()))
    FAILS[ip] = (n + 1, t0)


# ------------------------------------------------------------------ server
class Handler(BaseHTTPRequestHandler):
    server_version = "fibdash"
    sys_version = ""

    def log_message(self, *args):        # never write the secret link into logs
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", cookie=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, ok, msg, code=None, cookie=None):
        self._send(code or (200 if ok else 400), json.dumps({"ok": ok, "msg": msg}),
                   "application/json", cookie)

    def _file(self, path, cache):
        try:
            data = open(path, "rb").read()
        except OSError:
            return self._send(404, "Not found", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def _authed(self):
        return valid_session(self._session())

    def _body(self, limit):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if not 0 < n <= limit:
            return None
        return self.rfile.read(n)

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == BASE.rstrip("/"):
            self.send_response(301)
            self.send_header("Location", BASE)
            self.end_headers()
            return
        if path == BASE + "bg-default.jpg":   # stock picture, also used on the login page
            return self._file(os.path.join(D, "assets", "bg.jpg"), "public, max-age=604800")
        if not path.startswith(BASE):
            return self._send(404, "Not found", "text/plain")
        if not os.path.exists(PASS_FILE):
            return self._send(503, dashboard.login_page(
                "No password set yet — run in Termius: bash forex-bot/dashboard_setup.sh", form=False))
        if not self._authed():
            return self._send(401 if path != BASE else 200, dashboard.login_page())
        if path == BASE + "bg":
            return self._file(dashboard.BG_FILE, "private, max-age=31536000")
        if path != BASE:
            return self._send(404, "Not found", "text/plain")
        try:
            self._send(200, dashboard.render())
        except Exception as e:
            self._send(500, f"Dashboard error: {e}", "text/plain")

    # --------------------------------------------------------------- POST
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith(BASE):
            return self._send(404, "Not found", "text/plain")
        if path == BASE + "login":
            return self._login()
        if path == BASE + "logout":
            end_session(self._session())
            return self._json(True, "Logged out",
                              cookie=f"{COOKIE}=; Path={BASE}; Max-Age=0; HttpOnly; SameSite=Strict")
        if not self._authed():
            return self._json(False, "Please log in again", 401)
        if path == BASE + "bg":
            return self._upload_bg()
        if path != BASE + "api":
            return self._send(404, "Not found", "text/plain")
        raw = self._body(4096)
        if raw is None:
            return self._json(False, "Bad request")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            ok, msg = dashboard.apply_action(body)
        except Exception:
            ok, msg = False, "Bad request"
        self._json(ok, msg)

    def _login(self):
        ip = self.client_address[0]
        if locked_out(ip):
            return self._json(False, "Too many wrong tries — wait 15 minutes", 429)
        raw = self._body(1024)
        try:
            pw = json.loads(raw).get("password", "") if raw else ""
        except Exception:
            pw = ""
        if pw and check_password(pw):
            FAILS.pop(ip, None)
            sid = new_session()
            return self._json(True, "Welcome back", cookie=(
                f"{COOKIE}={sid}; Path={BASE}; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Strict"))
        note_fail(ip)
        time.sleep(1.0)                     # slows down guessing
        left = max(0, 5 - FAILS.get(ip, (0, 0))[0])
        self._json(False, "Wrong password" + (f" — {left} {'try' if left == 1 else 'tries'} left" if left else ""), 401)

    def _upload_bg(self):
        """Background photo from the page: already shrunk to a JPEG in the browser."""
        data = self._body(8 * 1024 * 1024)
        if data is None:
            return self._json(False, "Picture too large (max 8 MB)")
        if not data.startswith(b"\xff\xd8\xff"):
            return self._json(False, "Not a JPEG picture")
        os.makedirs(os.path.dirname(dashboard.BG_FILE), exist_ok=True)
        tmp = dashboard.BG_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dashboard.BG_FILE)
        self._json(True, "Background updated")


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"dashboard on port {PORT}", flush=True)
    srv.serve_forever()
