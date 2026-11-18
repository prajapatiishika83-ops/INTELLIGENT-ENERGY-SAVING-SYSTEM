"""Signup + OTP verification, forgot/reset password. Blueprint 'auth'."""
import re, hmac, hashlib, secrets, sqlite3
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, session, url_for, flash, current_app
from werkzeug.security import generate_password_hash, check_password_hash
import mailer

bp = Blueprint("auth", __name__); DB = "iess.db"
OTP_MINUTES, MAX_ATTEMPTS, COOLDOWN_SEC = 10, 5, 60
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$"); FMT = "%Y-%m-%d %H:%M:%S"
now = lambda: datetime.utcnow()

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_auth_tables():  # additive only: existing tables/data are untouched
    c = conn()
    c.execute("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, is_verified INTEGER DEFAULT 0, created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS otps(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL, purpose TEXT NOT NULL,
        otp_hash TEXT NOT NULL, expires_at TEXT NOT NULL, attempts INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, created_at TEXT)""")
    c.commit(); c.close()

def find_user(email):
    c = conn(); u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone(); c.close(); return u

def authenticate(ident, pw):
    u = find_user(ident.strip().lower())
    return u if u and u["is_verified"] and check_password_hash(u["password_hash"], pw) else None

def _hash(email, purpose, code):  # OTPs are stored as HMAC, never in plain text
    key = current_app.secret_key.encode()
    return hmac.new(key, f"{email}|{purpose}|{code}".encode(), hashlib.sha256).hexdigest()

def issue_otp(email, purpose):
    """Returns a new 6-digit code, or None if one was issued less than COOLDOWN_SEC ago."""
    c = conn(); last = c.execute("SELECT created_at FROM otps WHERE email=? AND purpose=? AND verified=0 ORDER BY id DESC LIMIT 1", (email, purpose)).fetchone()
    if last and (now() - datetime.strptime(last[0], FMT)).total_seconds() < COOLDOWN_SEC:
        c.close(); return None
    code = f"{secrets.randbelow(10**6):06d}"
    c.execute("DELETE FROM otps WHERE email=? AND purpose=?", (email, purpose))
    c.execute("INSERT INTO otps(email,purpose,otp_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
              (email, purpose, _hash(email, purpose, code), (now() + timedelta(minutes=OTP_MINUTES)).strftime(FMT), now().strftime(FMT)))
    c.commit(); c.close(); return code

def verify_otp(email, purpose, code):
    c = conn(); r = c.execute("SELECT * FROM otps WHERE email=? AND purpose=? AND verified=0 ORDER BY id DESC LIMIT 1", (email, purpose)).fetchone()
    ok = False
    if r and r["attempts"] < MAX_ATTEMPTS and now() <= datetime.strptime(r["expires_at"], FMT):
        c.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (r["id"],))
        ok = hmac.compare_digest(r["otp_hash"], _hash(email, purpose, code.strip()))
        if ok: c.execute("UPDATE otps SET verified=1 WHERE id=?", (r["id"],))
    c.commit(); c.close(); return ok

def pw_error(pw, pw2):
    if len(pw) < 8: return "Password must be at least 8 characters."
    if pw != pw2: return "Passwords do not match."

def _start_otp(email, purpose):
    session["otp_email"], session["otp_purpose"] = email, purpose
    return redirect(url_for("auth.verify"))

@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower()
        pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
        err = ("Enter your name." if len(name) < 2 else "Enter a valid email address." if not EMAIL_RE.match(email) else pw_error(pw, pw2))
        if err: flash(err); return render_template("signup.html", name=name, email=email)
        u = find_user(email)
        if u and u["is_verified"]:   # same response as a new signup: no account enumeration
            mailer.send_already_registered_email(email)
        else:
            c = conn()
            c.execute("INSERT INTO users(name,email,password_hash,is_verified,created_at) VALUES(?,?,?,0,?) "
                      "ON CONFLICT(email) DO UPDATE SET name=excluded.name, password_hash=excluded.password_hash",
                      (name, email, generate_password_hash(pw), now().strftime(FMT))); c.commit(); c.close()
            code = issue_otp(email, "signup")
            if code: mailer.send_otp_email(email, name, code, "signup", OTP_MINUTES)
        return _start_otp(email, "signup")
    return render_template("signup.html")

@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not EMAIL_RE.match(email): flash("Enter a valid email address."); return render_template("forgot_password.html")
        u = find_user(email)
        if u and u["is_verified"]:   # silently do nothing for unknown emails
            code = issue_otp(email, "reset")
            if code: mailer.send_otp_email(email, u["name"], code, "reset", OTP_MINUTES)
        return _start_otp(email, "reset")
    return render_template("forgot_password.html")

@bp.route("/verify-otp", methods=["GET", "POST"])
def verify():
    email, purpose = session.get("otp_email"), session.get("otp_purpose")
    if not email: return redirect(url_for("login"))
    if request.method == "POST":
        if verify_otp(email, purpose, request.form.get("otp", "")):
            session.pop("otp_email"); session.pop("otp_purpose")
            if purpose == "signup":
                c = conn(); c.execute("UPDATE users SET is_verified=1 WHERE email=?", (email,)); c.commit(); c.close()
                mailer.send_account_created_email(email, find_user(email)["name"])
                flash("Your account is successfully created. Please sign in.", "ok"); return redirect(url_for("login"))
            session["reset_email"] = email; return redirect(url_for("auth.reset"))
        flash("Invalid or expired code.")
    return render_template("verify_otp.html", email=email, minutes=OTP_MINUTES)

@bp.route("/resend-otp", methods=["POST"])
def resend():
    email, purpose = session.get("otp_email"), session.get("otp_purpose"); u = find_user(email) if email else None
    if u and ((purpose == "signup" and not u["is_verified"]) or (purpose == "reset" and u["is_verified"])):
        code = issue_otp(email, purpose)
        if code: mailer.send_otp_email(email, u["name"], code, purpose, OTP_MINUTES)
    flash("If the address is eligible, a new code was sent (one per minute).", "ok"); return redirect(url_for("auth.verify"))

@bp.route("/reset-password", methods=["GET", "POST"])
def reset():
    email = session.get("reset_email")
    if not email: return redirect(url_for("login"))
    if request.method == "POST":
        err = pw_error(request.form.get("password", ""), request.form.get("confirm", ""))
        if err: flash(err); return render_template("reset_password.html")
        c = conn(); c.execute("UPDATE users SET password_hash=? WHERE email=?", (generate_password_hash(request.form["password"]), email))
        c.execute("DELETE FROM otps WHERE email=?", (email,)); c.commit(); c.close()
        session.pop("reset_email"); flash("Password reset successfully.", "ok"); return redirect(url_for("login"))
    return render_template("reset_password.html")
