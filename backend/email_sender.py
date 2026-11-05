"""Send auth emails via SMTP (Resend / Gmail app password / generic SMTP)."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def _cfg():
    host = (os.getenv("SMTP_HOST") or "smtp.resend.com").strip()
    port = int(os.getenv("SMTP_PORT") or "465")
    user = (os.getenv("SMTP_USER") or os.getenv("SMTP_EMAIL") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_APP_PASSWORD") or "").strip()
    mail_from = (os.getenv("SMTP_FROM") or user).strip()
    return host, port, user, password, mail_from


def available() -> bool:
    _host, _port, user, password, _from = _cfg()
    return bool(user and password)


def send_email(to_email: str, subject: str, body: str) -> None:
    host, port, user, password, mail_from = _cfg()
    if not user or not password:
        # Dev fallback: never block local testing if SMTP is unset.
        print(f"[MAIL:DEV] to={to_email} subject={subject}\n{body}", flush=True)
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = to_email
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=25) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    print(f"[MAIL] sent to={to_email} subject={subject!r}", flush=True)


def send_auth_code(to_email: str, code: str, purpose: str) -> None:
    if purpose == "reset":
        subject = "АнтиГПТшник — код скидання пароля"
        body = (
            f"Ваш код для скидання пароля: {code}\n\n"
            "Код дійсний 15 хвилин. Якщо ви не просили скидання — ігноруйте цей лист.\n"
        )
    else:
        subject = "АнтиГПТшник — код підтвердження"
        body = (
            f"Ваш код підтвердження реєстрації: {code}\n\n"
            "Код дійсний 15 хвилин.\n"
        )
    send_email(to_email, subject, body)
