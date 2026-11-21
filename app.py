import os
import re
import io
import hmac
import time
import secrets
import sqlite3
from functools import wraps

import bcrypt
import pyotp
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, flash, g, abort
)
from markupsafe import Markup

try:  # QR code is optional; the app still works (manual key) without it
    import qrcode
    import qrcode.image.svg
except ImportError:
    qrcode = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Absolute path: on hosts like PythonAnywhere the working directory is NOT your project folder
DATABASE = os.path.join(BASE_DIR, "users.db")

app = Flask(__name__)


def load_secret_key():
    """Use SECRET_KEY env var, otherwise create one once and keep it in a file,
    so sessions survive restarts."""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    path = os.path.join(BASE_DIR, "secret.key")
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Hosts like PythonAnywhere serve HTTPS, so Secure cookies are safe there.
    # Set COOKIE_SECURE=0 only for local http://127.0.0.1 testing.
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
    MAX_CONTENT_LENGTH=16 * 1024,
)

MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 300
PRE_2FA_TTL = 300
MAX_2FA_ATTEMPTS = 5
_failed = {}  # (ip, username) -> (count, first_fail_time)  (in-memory, per process)

# Valid dummy hash so login takes the same time whether or not the user exists
_DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(rounds=12))


# --- Database ---
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash BLOB NOT NULL,
                totp_secret TEXT,
                two_factor_enabled INTEGER DEFAULT 0
            )
        """)
        db.commit()


init_db()  # runs under Flask dev server AND under WSGI hosts


# --- CSRF protection (no extra library needed) ---
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, "Invalid or missing CSRF token. Go back, refresh the page and try again.")


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    return resp


# --- Validation ---
def validate_registration(username, email, password):
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", username):
        return "Username must be 3–20 characters (letters, numbers, underscores only)."
    if len(email) > 254 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return "Please enter a valid email address."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password.encode("utf-8")) > 72:
        return "Password must be at most 72 bytes long."  # bcrypt ignores anything beyond 72 bytes
    if not (re.search(r"[A-Z]", password) and re.search(r"[a-z]", password) and re.search(r"\d", password)):
        return "Password must include uppercase, lowercase, and at least one number."
    return None


# --- Helpers ---
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if user is None:  # also handles a deleted user with a stale session
            session.clear()
            flash("Please log in to access this page.", "error")
            return redirect(url_for("login"))
        g.user = user
        return f(*args, **kwargs)
    return decorated


def is_locked(key):
    entry = _failed.get(key)
    if not entry:
        return False
    count, first = entry
    if time.time() - first > LOCKOUT_SECONDS:
        _failed.pop(key, None)
        return False
    return count >= MAX_FAILED_LOGINS


def record_failure(key):
    count, first = _failed.get(key, (0, time.time()))
    _failed[key] = (count + 1, first)


def qr_svg(uri):
    if qrcode is None:
        return None
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    svg = svg[svg.find("<svg"):]  # strip the <?xml ...?> header
    return Markup(svg)


def finish_login(user):
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]


# --- Routes ---
@app.route("/")
def index():
    return redirect(url_for("dashboard" if "user_id" in session else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        error = validate_registration(username, email, password)
        if error:
            flash(error, "error")
            return render_template("register.html")

        password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)",
                (username, email, password_hash),
            )
            db.commit()
            flash("Account created successfully! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username or email is already registered.", "error")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        key = (request.remote_addr, username.lower())

        if is_locked(key):
            flash("Too many failed attempts. Try again in a few minutes.", "error")
            return render_template("login.html")

        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

        stored = user["password_hash"] if user else _DUMMY_HASH
        ok = bcrypt.checkpw(password.encode("utf-8"), stored)

        if user and ok:
            _failed.pop(key, None)
            if user["two_factor_enabled"] and user["totp_secret"]:
                session.clear()
                session["pre_2fa_user_id"] = user["id"]
                session["pre_2fa_time"] = time.time()
                session["2fa_attempts"] = 0
                return redirect(url_for("verify_2fa"))
            finish_login(user)
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))

        record_failure(key)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    pre_user_id = session.get("pre_2fa_user_id")
    if not pre_user_id or time.time() - session.get("pre_2fa_time", 0) > PRE_2FA_TTL:
        session.clear()
        flash("Session expired. Please log in again.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        if session.get("2fa_attempts", 0) >= MAX_2FA_ATTEMPTS:
            session.clear()
            flash("Too many wrong codes. Please log in again.", "error")
            return redirect(url_for("login"))

        user = get_db().execute("SELECT * FROM users WHERE id = ?", (pre_user_id,)).fetchone()
        if user is None or not user["totp_secret"]:
            session.clear()
            return redirect(url_for("login"))

        code = request.form.get("code", "").strip().replace(" ", "")
        # valid_window=1 tolerates ~30s of clock drift between phone and server
        if code.isdigit() and len(code) == 6 and pyotp.TOTP(user["totp_secret"]).verify(code, valid_window=1):
            finish_login(user)
            flash("2FA verified. Welcome back!", "success")
            return redirect(url_for("dashboard"))

        session["2fa_attempts"] = session.get("2fa_attempts", 0) + 1
        flash("Invalid or expired 2FA code.", "error")
    return render_template("verify_2fa.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", user=g.user)


@app.route("/setup-2fa", methods=["GET", "POST"])
@login_required
def setup_2fa():
    """Enable 2FA ONLY after the user proves their authenticator app works."""
    user = g.user
    if user["two_factor_enabled"]:
        flash("2FA is already enabled.", "success")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        secret = session.get("pending_totp_secret")
        code = request.form.get("code", "").strip().replace(" ", "")
        if secret and code.isdigit() and len(code) == 6 and pyotp.TOTP(secret).verify(code, valid_window=1):
            db = get_db()
            db.execute(
                "UPDATE users SET two_factor_enabled = 1, totp_secret = ? WHERE id = ?",
                (secret, user["id"]),
            )
            db.commit()
            session.pop("pending_totp_secret", None)
            flash("Two-Factor Authentication is now enabled.", "success")
            return redirect(url_for("dashboard"))
        flash("That code was wrong. Check the app and try again.", "error")

    if "pending_totp_secret" not in session:
        session["pending_totp_secret"] = pyotp.random_base32()
    secret = session["pending_totp_secret"]
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name="SecureAuthPython")
    return render_template("setup_2fa.html", secret=secret, qr=qr_svg(uri))


@app.route("/disable-2fa", methods=["POST"])
@login_required
def disable_2fa():
    user = g.user
    password = request.form.get("password", "")
    if not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"]):
        flash("Wrong password. 2FA was not disabled.", "error")
        return redirect(url_for("dashboard"))
    db = get_db()
    db.execute("UPDATE users SET two_factor_enabled = 0, totp_secret = NULL WHERE id = ?", (user["id"],))
    db.commit()
    flash("Two-Factor Authentication disabled.", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been securely logged out.", "success")
    return redirect(url_for("login"))


if __name__ == "__main__":
    # For local testing over plain http, run:  COOKIE_SECURE=0 python app.py
    app.run(debug=False)
