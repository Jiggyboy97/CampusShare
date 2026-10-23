import unittest
from unittest.mock import patch

from email_utils import (
    send_notification_email,
    send_password_reset_email,
    send_verification_email,
)


class PasswordResetEmailTests(unittest.TestCase):
    def test_sends_reset_email_over_starttls(self):
        settings = {
            "CAMPUSSHARE_SMTP_HOST": "smtp.test.example",
            "CAMPUSSHARE_SMTP_PORT": "587",
            "CAMPUSSHARE_SMTP_SECURITY": "starttls",
            "CAMPUSSHARE_SMTP_FROM": "campusshare@test.example",
            "CAMPUSSHARE_SMTP_USERNAME": "smtp-user",
            "CAMPUSSHARE_SMTP_PASSWORD": "smtp-password",
        }
        with patch.dict("os.environ", settings, clear=False):
            with patch("email_utils.smtplib.SMTP") as smtp_factory:
                smtp = smtp_factory.return_value.__enter__.return_value
                send_password_reset_email(
                    "student@test.example",
                    "https://campusshare.test/reset-password/token",
                )

        smtp_factory.assert_called_once_with(
            "smtp.test.example",
            587,
            timeout=15,
        )
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("smtp-user", "smtp-password")
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "student@test.example")
        self.assertIn(
            "https://campusshare.test/reset-password/token",
            message.get_content(),
        )

    def test_requires_smtp_host_and_sender(self):
        with patch.dict(
            "os.environ",
            {
                "CAMPUSSHARE_SMTP_HOST": "",
                "CAMPUSSHARE_SMTP_FROM": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(
                RuntimeError, "CAMPUSSHARE_SMTP_HOST"
            ):
                send_password_reset_email(
                    "student@test.example",
                    "https://campusshare.test/reset-password/token",
                )

    def test_sends_email_verification_message(self):
        settings = {
            "CAMPUSSHARE_SMTP_HOST": "smtp.test.example",
            "CAMPUSSHARE_SMTP_PORT": "587",
            "CAMPUSSHARE_SMTP_SECURITY": "starttls",
            "CAMPUSSHARE_SMTP_FROM": "campusshare@test.example",
            "CAMPUSSHARE_SMTP_USERNAME": "",
            "CAMPUSSHARE_SMTP_PASSWORD": "",
        }
        verification_url = "https://campusshare.test/verify-email/token"
        with patch.dict("os.environ", settings, clear=False):
            with patch("email_utils.smtplib.SMTP") as smtp_factory:
                smtp = smtp_factory.return_value.__enter__.return_value
                send_verification_email("student@test.example", verification_url)

        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "student@test.example")
        self.assertIn("Verify your CampusShare email", message["Subject"])
        self.assertIn(verification_url, message.get_content())

    def test_sends_notification_email(self):
        settings = {
            "CAMPUSSHARE_SMTP_HOST": "smtp.test.example",
            "CAMPUSSHARE_SMTP_PORT": "587",
            "CAMPUSSHARE_SMTP_SECURITY": "starttls",
            "CAMPUSSHARE_SMTP_FROM": "campusshare@test.example",
            "CAMPUSSHARE_SMTP_USERNAME": "",
            "CAMPUSSHARE_SMTP_PASSWORD": "",
        }
        notification_text = "Your rental request was approved."
        with patch.dict("os.environ", settings, clear=False):
            with patch("email_utils.smtplib.SMTP") as smtp_factory:
                smtp = smtp_factory.return_value.__enter__.return_value
                send_notification_email(
                    "student@test.example",
                    notification_text,
                )

        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message["To"], "student@test.example")
        self.assertEqual(message["Subject"], "CampusShare notification")
        self.assertIn(notification_text, message.get_content())


if __name__ == "__main__":
    unittest.main()
