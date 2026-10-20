import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("FLASK_SECRET_KEY", "test-only-campusshare-secret-key")

import app as campusshare
from database import connect_database


class RentalWorkflowTests(unittest.TestCase):
    @contextmanager
    def database_connection(self):
        connection = sqlite3.connect(campusshare.DATABASE)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database = campusshare.DATABASE
        self.original_csrf_enabled = campusshare.app.config.get(
            "WTF_CSRF_ENABLED", True
        )
        campusshare.DATABASE = os.path.join(self.temp_dir.name, "test.db")
        campusshare.app.config["TESTING"] = True
        campusshare.app.config["WTF_CSRF_ENABLED"] = False
        campusshare.init_db()

        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (name, email, student_id, password)
                VALUES ('Owner', 'owner@test.example', 'owner-1', 'test')
                """
            )
            connection.execute(
                """
                INSERT INTO users (name, email, student_id, password)
                VALUES ('Borrower', 'borrower@test.example', 'borrower-1', 'test')
                """
            )
            self.owner_id = connection.execute(
                "SELECT id FROM users WHERE student_id = 'owner-1'"
            ).fetchone()[0]
            self.borrower_id = connection.execute(
                "SELECT id FROM users WHERE student_id = 'borrower-1'"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO equipment (
                    owner_id, item_name, category, description, condition,
                    sharing_type, price, deposit, available_from,
                    available_until, location
                )
                VALUES (?, 'Camera', 'Electronics', 'Test camera', 'Good',
                        'Paid', 12, 20, '2026-01-01', '2027-12-31', 'Campus')
                """,
                (self.owner_id,),
            )
            self.equipment_id = connection.execute(
                "SELECT id FROM equipment"
            ).fetchone()[0]

        self.client = campusshare.app.test_client()

    def tearDown(self):
        campusshare.DATABASE = self.original_database
        campusshare.app.config["WTF_CSRF_ENABLED"] = self.original_csrf_enabled
        self.temp_dir.cleanup()

    def sign_in_as(self, user_id):
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = user_id

    def get_request(self, request_id):
        with self.database_connection() as connection:
            return connection.execute(
                """
                SELECT status, payment_status, total_price, late_fee_status
                FROM borrowing_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()

    def test_borrower_request_approval_and_demo_payment(self):
        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT user_id, message FROM notifications"
                ).fetchone(),
                (self.owner_id, "New borrowing request for Camera."),
            )

        with self.database_connection() as connection:
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]
        self.assertEqual(self.get_request(request_id)[:3], ("Pending", "Unpaid", 24.0))

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.get_request(request_id)[0], "Approved")
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT user_id, message FROM notifications ORDER BY id DESC"
                ).fetchone(),
                (self.borrower_id, "Your request for Camera was approved."),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get(f"/qr-payment/{request_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no real money is processed", response.data.lower())

        token = campusshare.serializer.dumps(
            {"request_id": request_id, "borrower_id": self.borrower_id},
            salt="qr-payment-salt",
        )
        response = self.client.post(f"/qr-payment/confirm/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Demo Payment Confirmed", response.data)
        self.assertEqual(self.get_request(request_id)[1], "Paid")

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/confirm-deposit/{request_id}")
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f"/requests/{request_id}/complete")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            status, late_fee, deposit_status = connection.execute(
                """
                SELECT status, late_fee, deposit_status
                FROM borrowing_requests WHERE id = ?
                """,
                (request_id,),
            ).fetchone()
        self.assertEqual(status, "Completed")
        self.assertEqual(late_fee, 0)
        self.assertEqual(deposit_status, "Held")

    def test_owner_can_reject_pending_request(self):
        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        self.assertEqual(response.status_code, 302)

        with self.database_connection() as connection:
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/requests/{request_id}/reject")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.get_request(request_id)[0], "Rejected")

    def test_only_borrower_can_pay_completed_late_fee(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date,
                    status, late_fee, late_fee_status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02',
                        'Completed', 24, 'Unpaid')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/pay-late-fee/{request_id}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.get_request(request_id)[3], "Unpaid")

        self.sign_in_as(self.borrower_id)
        response = self.client.post(f"/pay-late-fee/{request_id}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.get_request(request_id)[3], "Paid")

    def test_forgot_password_handles_unknown_and_registered_emails(self):
        with patch("routes.send_password_reset_email") as send_email:
            response = self.client.post(
                "/forgot-password",
                data={"email": "unknown@test.example"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"For privacy", response.data)
            send_email.assert_not_called()

            response = self.client.post(
                "/forgot-password",
                data={"email": "borrower@test.example"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"For privacy", response.data)
        send_email.assert_called_once()
        recipient, reset_url = send_email.call_args.args
        self.assertEqual(recipient, "borrower@test.example")
        self.assertIn("/reset-password/", reset_url)

    def test_password_reset_email_failure_is_logged_without_account_disclosure(self):
        with patch(
            "routes.send_password_reset_email",
            side_effect=RuntimeError("SMTP settings are missing"),
        ), self.assertLogs("app", level="ERROR") as captured_logs:
            registered_response = self.client.post(
                "/forgot-password",
                data={"email": "borrower@test.example"},
            )

        unknown_response = self.client.post(
            "/forgot-password",
            data={"email": "unknown@test.example"},
        )
        self.assertEqual(registered_response.status_code, 200)
        self.assertEqual(registered_response.data, unknown_response.data)
        self.assertTrue(
            any("Could not send a password reset email" in line
                for line in captured_logs.output)
        )

    def test_short_password_and_missing_csrf_token_are_rejected(self):
        response = self.client.get("/register")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="csrf_token"', response.data)

        response = self.client.post(
            "/register",
            data={
                "name": "New Student",
                "email": "new@buaa.edu.cn",
                "student_id": "new-student",
                "password": "short",
                "confirm_password": "short",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"12 characters", response.data)

        campusshare.app.config["WTF_CSRF_ENABLED"] = True
        response = self.client.post(
            "/request-equipment/1",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Form expired", response.data)

    def test_paid_rental_cannot_be_completed_before_payment(self):
        self.sign_in_as(self.borrower_id)
        self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        with self.database_connection() as connection:
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        self.client.post(f"/requests/{request_id}/approve")
        response = self.client.post(f"/requests/{request_id}/complete")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.get_request(request_id)[0], "Approved")

    def test_owner_can_record_and_settle_damage_within_deposit(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, deposit_status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02',
                        'Completed', 'Paid', 'Held')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/damage-status/{request_id}/damaged",
            data={"damage_report": "Cracked lens cover", "damage_charge": "25"},
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            f"/damage-status/{request_id}/damaged",
            data={"damage_report": "Cracked lens cover", "damage_charge": "15"},
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_status, damage_report, damage_charge
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Damaged", "Cracked lens cover", 15.0),
            )

        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deposit_status FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                "Settled",
            )

    def test_sqlite_foreign_keys_are_enabled_on_application_connections(self):
        connection = connect_database(campusshare.DATABASE)
        with self.assertRaises(sqlite3.IntegrityError):
            try:
                connection.execute(
                    """
                    INSERT INTO borrowing_requests (
                        equipment_id, borrower_id, start_date, end_date
                    )
                    VALUES (99999, 99999, '2026-11-01', '2026-11-02')
                    """
                )
            finally:
                connection.close()

    def test_past_rental_date_is_rejected_with_styled_error(self):
        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2020-01-01", "end_date": "2020-01-02"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"error-container", response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM borrowing_requests"
                ).fetchone()[0],
                0,
            )

    def test_non_owner_cannot_approve_and_foreign_key_blocks_history_delete(self):
        self.sign_in_as(self.borrower_id)
        self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        with self.database_connection() as connection:
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        response = self.client.post(f"/requests/{request_id}/approve")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"error-container", response.data)
        self.assertEqual(self.get_request(request_id)[0], "Pending")

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/delete-listing/{self.equipment_id}")
        self.assertEqual(response.status_code, 409)
        with self.database_connection() as connection:
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM equipment WHERE id = ?",
                    (self.equipment_id,),
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                1,
            )

    def test_browse_paginates_and_keeps_search_filters(self):
        with self.database_connection() as connection:
            connection.executemany(
                """
                INSERT INTO equipment (
                    owner_id, item_name, category, description, condition,
                    sharing_type, price, deposit, available_from,
                    available_until, location
                )
                VALUES (?, ?, 'Camera', 'Searchable camera', 'Good',
                        'Free', 0, 0, '2026-01-01', '2027-12-31', 'Campus')
                """,
                [
                    (self.owner_id, f"Search test item {index}")
                    for index in range(25)
                ],
            )

        first_page = self.client.get("/browse?q=Search+test")
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.data.count(b"View Details"), 24)
        self.assertIn(b"Page 1 of 2", first_page.data)

        second_page = self.client.get("/browse?q=Search+test&page=2")
        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.data.count(b"View Details"), 1)
        self.assertIn(b"Page 2 of 2", second_page.data)

    def test_invalid_listing_amount_returns_styled_validation_error(self):
        self.sign_in_as(self.owner_id)
        response = self.client.post(
            "/list-item",
            data={
                "item_name": "Tripod",
                "category": "Tripod",
                "description": "Test tripod",
                "condition": "Good",
                "sharing_type": "Paid",
                "price": "-1",
                "deposit": "0",
                "available_from": "2026-11-01",
                "available_until": "2026-11-02",
                "location": "Campus",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"error-container", response.data)

    def test_unknown_url_uses_styled_not_found_page(self):
        response = self.client.get("/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"error-container", response.data)


if __name__ == "__main__":
    unittest.main()
