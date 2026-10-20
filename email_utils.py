from email.message import EmailMessage
import os
import smtplib
import ssl


def send_password_reset_email(recipient, reset_url):
    host = os.environ.get("CAMPUSSHARE_SMTP_HOST", "").strip()
    sender = os.environ.get("CAMPUSSHARE_SMTP_FROM", "").strip()
    username = os.environ.get("CAMPUSSHARE_SMTP_USERNAME", "").strip()
    password = os.environ.get("CAMPUSSHARE_SMTP_PASSWORD", "")
    security = os.environ.get("CAMPUSSHARE_SMTP_SECURITY", "starttls").lower()

    if not host or not sender:
        raise RuntimeError(
            "CAMPUSSHARE_SMTP_HOST and CAMPUSSHARE_SMTP_FROM must be configured."
        )
    if bool(username) != bool(password):
        raise RuntimeError(
            "Configure both CAMPUSSHARE_SMTP_USERNAME and "
            "CAMPUSSHARE_SMTP_PASSWORD, or leave both empty."
        )
    if security not in {"starttls", "ssl"}:
        raise RuntimeError(
            "CAMPUSSHARE_SMTP_SECURITY must be 'starttls' or 'ssl'."
        )

    try:
        port = int(os.environ.get("CAMPUSSHARE_SMTP_PORT", "587"))
    except ValueError as error:
        raise RuntimeError("CAMPUSSHARE_SMTP_PORT must be a valid port.") from error
    if not 1 <= port <= 65535:
        raise RuntimeError("CAMPUSSHARE_SMTP_PORT must be between 1 and 65535.")

    message = EmailMessage()
    message["Subject"] = "Reset your CampusShare password"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "We received a request to reset your CampusShare password.\n\n"
        f"Use this link within 30 minutes:\n{reset_url}\n\n"
        "If you did not request a password reset, you can ignore this email."
    )

    timeout = 15
    if security == "ssl":
        with smtplib.SMTP_SSL(
            host,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
