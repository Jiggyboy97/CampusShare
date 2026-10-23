import logging
import smtplib

from email_utils import send_notification_email


logger = logging.getLogger(__name__)


def create_notification(connection, user_id, message, link):
    connection.execute(
        """
        INSERT INTO notifications (user_id, message, link)
        VALUES (?, ?, ?)
        """,
        (user_id, message, link),
    )

    recipient = connection.execute(
        """
        SELECT email, email_verified, notification_emails_enabled
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if recipient is None or not recipient[1] or not recipient[2]:
        return

    try:
        send_notification_email(recipient[0], message)
    except (OSError, RuntimeError, smtplib.SMTPException):
        logger.error("Could not send an opted-in notification email.")
