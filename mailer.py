"""Reusable email service. All settings come from environment variables (or a .env file)."""
import os, smtplib, ssl
from email.message import EmailMessage
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass

def _send(to, subject, body):
    server, user, pw = os.environ.get("MAIL_SERVER"), os.environ.get("MAIL_USERNAME"), os.environ.get("MAIL_PASSWORD")
    if not (server and user and pw):  # DEV fallback: print instead of sending
        print(f"\n--- MAIL NOT CONFIGURED: printing email instead ---\nTo: {to}\nSubject: {subject}\n{body}\n---\n")
        return True
    port = int(os.environ.get("MAIL_PORT", "587")); tls = os.environ.get("MAIL_USE_TLS", "true").lower() == "true"
    msg = EmailMessage(); msg["From"] = os.environ.get("MAIL_FROM", user); msg["To"] = to; msg["Subject"] = subject
    msg.set_content(body)
    try:
        if tls:  # STARTTLS, normally port 587
            with smtplib.SMTP(server, port, timeout=15) as s:
                s.starttls(context=ssl.create_default_context()); s.login(user, pw); s.send_message(msg)
        else:    # implicit SSL, normally port 465
            with smtplib.SMTP_SSL(server, port, context=ssl.create_default_context(), timeout=15) as s:
                s.login(user, pw); s.send_message(msg)
        return True
    except Exception as e:
        print("Email send failed:", e); return False

def send_otp_email(to, name, otp, purpose, minutes=10):
    what = "verify your new IESS account" if purpose == "signup" else "reset your IESS password"
    return _send(to, f"Your IESS verification code: {otp}",
                 f"Hello {name or ''},\n\nUse this code to {what}:\n\n    {otp}\n\nIt expires in {minutes} minutes. "
                 "If you did not request it, ignore this email.\n\nIESS - Intelligent Energy Saving System")

def send_account_created_email(to, name):
    return _send(to, "Your IESS account is ready", f"Hello {name or ''},\n\nYour account is successfully created.\n\nYou can now sign in to IESS.")

def send_already_registered_email(to):
    return _send(to, "IESS sign-up attempt", "Someone tried to create an IESS account with this email, but an account already exists.\n"
                 "If it was you, sign in or use 'Forgot Password?'. Otherwise, ignore this email.")
