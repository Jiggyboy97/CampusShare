import io
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from contextlib import contextmanager
from unittest.mock import patch

os.environ.setdefault("FLASK_SECRET_KEY", "test-only-campusshare-secret-key")

import app as campusshare
from database import connect_database
from rental_reminders import create_due_return_reminders
from werkzeug.security import check_password_hash, generate_password_hash


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
        self.original_upload_folder = campusshare.app.config["UPLOAD_FOLDER"]
        campusshare.DATABASE = os.path.join(self.temp_dir.name, "test.db")
        campusshare.app.config["UPLOAD_FOLDER"] = os.path.join(
            self.temp_dir.name,
            "uploads",
        )
        campusshare.app.config["TESTING"] = True
        campusshare.app.config["WTF_CSRF_ENABLED"] = False
        campusshare.init_db()

        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (
                    name, email, student_id, password, email_verified
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (
                    "Owner",
                    "owner@test.example",
                    "owner-1",
                    generate_password_hash("test-password-long"),
                ),
            )
            connection.execute(
                """
                INSERT INTO users (
                    name, email, student_id, password, email_verified
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (
                    "Borrower",
                    "borrower@test.example",
                    "borrower-1",
                    generate_password_hash("test-password-long"),
                ),
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
        campusshare.app.config["UPLOAD_FOLDER"] = self.original_upload_folder
        campusshare.app.config["WTF_CSRF_ENABLED"] = self.original_csrf_enabled
        self.temp_dir.cleanup()

    def sign_in_as(self, user_id):
        with self.client.session_transaction() as user_session:
            user_session["user_id"] = user_id
            user_session["user_name"] = "Test Student"

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

    def listing_form_data(self, **overrides):
        today = date.today()
        data = {
            "item_name": "Tripod",
            "category": "Tripod",
            "description": "A sturdy camera tripod.",
            "condition": "Good",
            "sharing_type": "Paid",
            "price_unit": "day",
            "price": "10",
            "deposit": "20",
            "available_from": today.isoformat(),
            "available_until": (today + timedelta(days=30)).isoformat(),
            "location": "Campus",
            "pickup_time_start": "09:00",
            "return_time_end": "18:00",
        }
        data.update(overrides)
        return data

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
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, message
                    FROM notifications
                    WHERE message = 'Borrower paid for the rental of Camera.'
                    """
                ).fetchone(),
                (self.owner_id, "Borrower paid for the rental of Camera."),
            )

        response = self.client.post(f"/qr-payment/confirm/{token}")
        self.assertEqual(response.status_code, 200)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM notifications
                    WHERE message = 'Borrower paid for the rental of Camera.'
                    """
                ).fetchone()[0],
                1,
            )

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
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, message
                    FROM notifications
                    WHERE message = 'Your rental of Camera has been marked returned.'
                    """
                ).fetchone(),
                (
                    self.borrower_id,
                    "Your rental of Camera has been marked returned.",
                ),
            )

    def test_hourly_request_rounds_up_and_requires_one_hour_minimum(self):
        self.sign_in_as(self.borrower_id)
        rental_date = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE equipment
                SET price_unit = 'hour', price = 5,
                    pickup_time_start = '09:00', return_time_end = '18:00'
                WHERE id = ?
                """,
                (self.equipment_id,),
            )

        for end_time, expected_price in (("09:05", 5.0), ("10:01", 10.0)):
            response = self.client.post(
                f"/request-equipment/{self.equipment_id}",
                data={
                    "start_date": rental_date,
                    "end_date": rental_date,
                    "start_time": "09:00",
                    "end_time": end_time,
                },
            )
            self.assertEqual(response.status_code, 302)
            with self.database_connection() as connection:
                self.assertEqual(
                    connection.execute(
                        """
                        SELECT total_price, price_unit, unit_price
                        FROM borrowing_requests
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    ).fetchone(),
                    (expected_price, "hour", 5.0),
                )
                connection.execute("DELETE FROM borrowing_requests")

    def test_hourly_listing_accepts_multiple_non_overlapping_bookings(self):
        self.sign_in_as(self.borrower_id)
        rental_date = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE equipment
                SET price_unit = 'hour', price = 5,
                    pickup_time_start = '09:00', return_time_end = '18:00'
                WHERE id = ?
                """,
                (self.equipment_id,),
            )

        def request_time(start_time, end_time):
            return self.client.post(
                f"/request-equipment/{self.equipment_id}",
                data={
                    "start_date": rental_date,
                    "end_date": rental_date,
                    "start_time": start_time,
                    "end_time": end_time,
                },
            )

        first_response = request_time("09:00", "10:00")
        self.assertEqual(first_response.status_code, 302)

        overlap_response = request_time("09:30", "10:30")
        self.assertEqual(overlap_response.status_code, 409)

        adjacent_response = request_time("10:00", "11:00")
        self.assertEqual(adjacent_response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM borrowing_requests
                    WHERE equipment_id = ? AND status = 'Pending'
                    """,
                    (self.equipment_id,),
                ).fetchone()[0],
                2,
            )

    def test_daily_listing_accepts_multiple_non_overlapping_bookings(self):
        self.sign_in_as(self.borrower_id)
        first_date = date.today() + timedelta(days=2)
        second_date = first_date + timedelta(days=2)

        first_response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": first_date.isoformat(),
                "end_date": first_date.isoformat(),
            },
        )
        self.assertEqual(first_response.status_code, 302)

        overlapping_response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": first_date.isoformat(),
                "end_date": (first_date + timedelta(days=1)).isoformat(),
            },
        )
        self.assertEqual(overlapping_response.status_code, 409)

        non_overlapping_response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": second_date.isoformat(),
                "end_date": second_date.isoformat(),
            },
        )
        self.assertEqual(non_overlapping_response.status_code, 302)

    def test_borrower_can_request_non_overlapping_time_while_listing_is_booked(self):
        rental_date = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, ?, ?, 'Approved')
                """,
                (
                    self.equipment_id,
                    self.borrower_id,
                    rental_date,
                    rental_date,
                ),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get(f"/equipment/{self.equipment_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="rental-request-form"', response.data)
        self.assertIn(b"does not", response.data)

    def test_listing_pricing_unit_persists_and_displays(self):
        self.sign_in_as(self.owner_id)
        response = self.client.post(
            "/list-item",
            data=self.listing_form_data(price_unit="hour"),
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            listing_id, price_unit = connection.execute(
                """
                SELECT id, price_unit
                FROM equipment
                WHERE item_name = 'Tripod'
                """
            ).fetchone()
        self.assertEqual(price_unit, "hour")
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT pickup_time_start, return_time_end
                    FROM equipment WHERE id = ?
                    """,
                    (listing_id,),
                ).fetchone(),
                ("09:00", "18:00"),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get(f"/equipment/{listing_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"per hour", response.data)
        self.assertIn(b"09:00 to 18:00", response.data)
        self.assertIn(b'id="rental-cost-estimate"', response.data)
        self.assertIn(b'data-price-unit="hour"', response.data)
        self.assertIn(b'data-price="10.0"', response.data)
        self.assertIn(b"The security deposit is not included", response.data)
        self.assertIn(b"Listed by Owner", response.data)

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/edit-listing/{listing_id}",
            data=self.listing_form_data(price_unit="day"),
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT price_unit, pickup_time_start, return_time_end
                    FROM equipment WHERE id = ?
                    """,
                    (listing_id,),
                ).fetchone(),
                ("day", None, None),
            )

    def test_hourly_listing_rejects_invalid_or_reversed_time_window(self):
        self.sign_in_as(self.owner_id)
        for pickup_time, return_time in (("", "18:00"), ("18:00", "09:00")):
            with self.subTest(pickup_time=pickup_time, return_time=return_time):
                response = self.client.post(
                    "/list-item",
                    data=self.listing_form_data(
                        price_unit="hour",
                        pickup_time_start=pickup_time,
                        return_time_end=return_time,
                    ),
                )
                self.assertEqual(response.status_code, 400)
                with self.database_connection() as connection:
                    self.assertIsNone(
                        connection.execute(
                            "SELECT id FROM equipment WHERE item_name = 'Tripod'"
                        ).fetchone()
                    )

    def test_hourly_request_must_fit_owner_time_window(self):
        self.sign_in_as(self.borrower_id)
        rental_date = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE equipment
                SET price_unit = 'hour', pickup_time_start = '09:00',
                    return_time_end = '17:00'
                WHERE id = ?
                """,
                (self.equipment_id,),
            )

        for start_time, end_time in (("08:00", "10:00"), ("16:00", "17:05")):
            response = self.client.post(
                f"/request-equipment/{self.equipment_id}",
                data={
                    "start_date": rental_date,
                    "end_date": rental_date,
                    "start_time": start_time,
                    "end_time": end_time,
                },
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn(b"pickup and return window", response.data)

        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": rental_date,
                "end_date": rental_date,
                "start_time": "09:00",
                "end_time": "17:00",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_new_listing_rejects_past_availability_start_date(self):
        self.sign_in_as(self.owner_id)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        response = self.client.post(
            "/list-item",
            data=self.listing_form_data(
                available_from=yesterday,
                available_until=(date.today() + timedelta(days=30)).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Choose today or a future date.", response.data)
        self.assertIn(b'id="available_from_error"', response.data)
        self.assertIn(
            f'value="{yesterday}"'.encode(),
            response.data,
        )
        with self.database_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM equipment WHERE item_name = 'Tripod'"
                ).fetchone()
            )

    def test_edit_listing_rejects_past_availability_start_date(self):
        self.sign_in_as(self.owner_id)
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        response = self.client.post(
            f"/edit-listing/{self.equipment_id}",
            data=self.listing_form_data(
                item_name="Updated camera",
                available_from=yesterday,
                available_until=(date.today() + timedelta(days=30)).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Choose today or a future date.", response.data)
        self.assertIn(b'id="available_from_error"', response.data)
        self.assertIn(
            f'value="{yesterday}"'.encode(),
            response.data,
        )
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT item_name FROM equipment WHERE id = ?",
                    (self.equipment_id,),
                ).fetchone()[0],
                "Camera",
            )

    def test_date_pickers_block_past_dates_and_follow_availability(self):
        self.sign_in_as(self.owner_id)
        today = date.today().isoformat()
        available_until = (date.today() + timedelta(days=30)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE equipment
                SET available_from = ?, available_until = ?
                WHERE id = ?
                """,
                (
                    (date.today() - timedelta(days=1)).isoformat(),
                    available_until,
                    self.equipment_id,
                ),
            )

        listing_response = self.client.get("/list-item")
        edit_response = self.client.get(f"/edit-listing/{self.equipment_id}")
        self.sign_in_as(self.borrower_id)
        rental_response = self.client.get(f"/equipment/{self.equipment_id}")

        self.assertRegex(
            listing_response.get_data(as_text=True),
            rf'name="available_from"\s+value=""\s+min="{today}"',
        )
        self.assertRegex(
            edit_response.get_data(as_text=True),
            rf'name="available_from"\s+value="{(date.today() - timedelta(days=1)).isoformat()}"\s+min="{today}"',
        )
        self.assertRegex(
            edit_response.get_data(as_text=True),
            rf'name="available_until"\s+value="{available_until}"\s+min="{today}"',
        )
        self.assertIn(f'min="{today}"'.encode(), rental_response.data)
        self.assertIn(
            f'max="{available_until}"'.encode(),
            rental_response.data,
        )

    def test_hourly_request_rejects_cross_day_and_past_start(self):
        self.sign_in_as(self.borrower_id)
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE equipment SET price_unit = 'hour' WHERE id = ?",
                (self.equipment_id,),
            )

        tomorrow = date.today() + timedelta(days=1)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": tomorrow.isoformat(),
                "end_date": (tomorrow + timedelta(days=1)).isoformat(),
                "start_time": "23:00",
                "end_time": "01:00",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'id="end_date_error"', response.data)
        self.assertIn(
            b'aria-invalid="true"',
            response.data,
        )
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM borrowing_requests"
                ).fetchone()[0],
                0,
            )

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={
                "start_date": yesterday,
                "end_date": yesterday,
                "start_time": "09:00",
                "end_time": "10:00",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'id="start_time_error"', response.data)
        self.assertIn(b'value="09:00"', response.data)

    def test_daily_late_fee_is_daily_rate_times_days_overdue(self):
        overdue_days = 2
        return_date = date.today() - timedelta(days=overdue_days)
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, unit_price
                )
                VALUES (?, ?, ?, ?, 'Approved', 'Paid', 12)
                """,
                (
                    self.equipment_id,
                    self.borrower_id,
                    return_date.isoformat(),
                    return_date.isoformat(),
                ),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/requests/{request_id}/complete")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, late_fee FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone(),
                ("Completed", 12 * overdue_days),
            )

    def test_unreturned_rental_is_marked_overdue_and_can_be_completed(self):
        yesterday = date.today() - timedelta(days=1)
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, deposit_status, unit_price
                )
                VALUES (?, ?, ?, ?, 'Approved', 'Paid', 'Held', 12)
                """,
                (
                    self.equipment_id,
                    self.borrower_id,
                    yesterday.isoformat(),
                    yesterday.isoformat(),
                ),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.borrower_id)
        borrower_response = self.client.get("/my-rentals")
        self.assertEqual(borrower_response.status_code, 200)
        self.assertIn(b"Status:</strong>\n                Overdue", borrower_response.data)
        self.assertIn(b"Rental overdue. The agreed return date has passed.", borrower_response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                "Overdue",
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, message
                    FROM notifications
                    WHERE user_id = ?
                      AND message LIKE '%is overdue%'
                    """,
                    (self.borrower_id,),
                ).fetchone(),
                (
                    self.borrower_id,
                    "Your rental of Camera is overdue. Please return it "
                    "as soon as possible.",
                ),
            )

        self.sign_in_as(self.owner_id)
        owner_response = self.client.get("/requests")
        self.assertEqual(owner_response.status_code, 200)
        self.assertIn(b"This rental is overdue.", owner_response.data)
        self.assertIn(
            f"/requests/{request_id}/complete".encode(),
            owner_response.data,
        )

        response = self.client.post(f"/requests/{request_id}/complete")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, late_fee FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone(),
                ("Completed", 12.0),
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM notifications
                    WHERE user_id = ? AND message LIKE '%is overdue%'
                    """,
                    (self.borrower_id,),
                ).fetchone()[0],
                1,
            )

    def test_hourly_late_fee_uses_hourly_rate_once_per_late_day(self):
        yesterday = date.today() - timedelta(days=1)
        return_time = datetime.strptime("23:59", "%H:%M").time()
        expected_late_days = math.ceil(
            max(
                0,
                (
                    datetime.now()
                    - datetime.combine(yesterday, return_time)
                ).total_seconds(),
            )
            / (24 * 60 * 60)
        )
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE equipment SET price_unit = 'hour' WHERE id = ?",
                (self.equipment_id,),
            )
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date,
                    start_time, end_time, price_unit, unit_price, status,
                    payment_status, total_price
                )
                VALUES (?, ?, ?, ?, '09:00', '23:59', 'hour', 12,
                        'Approved', 'Paid', 12)
                """,
                (
                    self.equipment_id,
                    self.borrower_id,
                    yesterday.isoformat(),
                    yesterday.isoformat(),
                ),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/requests/{request_id}/complete")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, late_fee FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone(),
                ("Completed", 12.0 * expected_late_days),
            )

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
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT user_id, message FROM notifications ORDER BY id DESC"
                ).fetchone(),
                (self.borrower_id, "Your request for Camera was rejected."),
            )

    def test_rental_status_routes_reject_disallowed_transitions(self):
        self.sign_in_as(self.owner_id)
        invalid_transitions = {
            "Pending": ("complete",),
            "Approved": ("approve", "reject"),
            "Rejected": ("approve", "reject", "complete"),
            "Completed": ("approve", "reject", "complete"),
            "Overdue": ("approve", "reject"),
        }

        for initial_status, actions in invalid_transitions.items():
            with self.subTest(status=initial_status):
                with self.database_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO borrowing_requests (
                            equipment_id, borrower_id, start_date, end_date,
                            status, payment_status
                        )
                        VALUES (?, ?, '2026-11-01', '2026-11-02', ?, 'Paid')
                        """,
                        (self.equipment_id, self.borrower_id, initial_status),
                    )
                    request_id = connection.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]

                for action in actions:
                    with self.subTest(action=action):
                        response = self.client.post(
                            f"/requests/{request_id}/{action}"
                        )
                        self.assertEqual(response.status_code, 404)
                        self.assertEqual(self.get_request(request_id)[0], initial_status)

                with self.database_connection() as connection:
                    connection.execute(
                        "DELETE FROM borrowing_requests WHERE id = ?",
                        (request_id,),
                    )

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
        with patch("campusshare_routes.send_password_reset_email") as send_email:
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

    def test_registration_requires_verification_before_login(self):
        with patch("campusshare_routes.send_verification_email") as send_email:
            response = self.client.post(
                "/register",
                data={
                    "name": "New Student",
                    "email": "new@buaa.edu.cn",
                    "student_id": "new-student",
                    "password": "A-secure-password-123",
                    "confirm_password": "A-secure-password-123",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("verification=sent", response.headers["Location"])
        send_email.assert_called_once()
        recipient, verification_url = send_email.call_args.args
        self.assertEqual(recipient, "new@buaa.edu.cn")
        token = verification_url.rsplit("/", 1)[-1]

        with self.database_connection() as connection:
            user_id, is_verified = connection.execute(
                """
                SELECT id, email_verified
                FROM users WHERE email = 'new@buaa.edu.cn'
                """
            ).fetchone()
        self.assertEqual(is_verified, 0)

        response = self.client.post(
            "/login",
            data={
                "email": "new@buaa.edu.cn",
                "password": "A-secure-password-123",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn(b"Verify your email", response.data)

        response = self.client.get(f"/verify-email/{token}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Confirm email", response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT email_verified FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()[0],
                0,
            )

        csrf_token = re.search(
            rb'name="csrf_token" value="([^"]+)"',
            response.data,
        ).group(1).decode()
        campusshare.app.config["WTF_CSRF_ENABLED"] = True
        response = self.client.post(f"/verify-email/{token}")
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            f"/verify-email/{token}",
            data={"csrf_token": csrf_token},
        )
        campusshare.app.config["WTF_CSRF_ENABLED"] = False
        self.assertEqual(response.status_code, 302)
        self.assertIn("verified=1", response.headers["Location"])
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT email_verified FROM users WHERE id = ?",
                    (user_id,),
                ).fetchone()[0],
                1,
            )

        response = self.client.post(
            "/login",
            data={
                "email": "new@buaa.edu.cn",
                "password": "A-secure-password-123",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as user_session:
            self.assertEqual(user_session["user_id"], user_id)

    def test_registration_reports_when_verification_email_cannot_be_sent(self):
        verification_link = (
            "https://campusshare.test/verify-email/verification-secret-value"
        )
        with patch(
            "campusshare_routes.send_verification_email",
            side_effect=RuntimeError(
                f"Could not deliver {verification_link}: SMTP settings are missing"
            ),
        ), self.assertLogs("app", level="ERROR") as captured_logs:
            response = self.client.post(
                "/register",
                data={
                    "name": "New Student",
                    "email": "new@buaa.edu.cn",
                    "student_id": "new-student",
                    "password": "A-secure-password-123",
                    "confirm_password": "A-secure-password-123",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("verification=failed", response.headers["Location"])
        self.assertTrue(
            any(
                "Could not send an email verification message" in line
                for line in captured_logs.output
            )
        )
        joined_logs = "\n".join(captured_logs.output)
        self.assertNotIn("verification-secret-value", joined_logs)
        self.assertNotIn(verification_link, joined_logs)

    def test_listing_photo_upload_rejects_files_over_five_megabytes(self):
        self.sign_in_as(self.owner_id)
        response = self.client.post(
            "/list-item",
            data={
                "image": (
                    io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)),
                    "large.png",
                )
            },
            content_type="multipart/form-data",
            headers={"Referer": "https://example.invalid/"},
        )
        self.assertEqual(response.status_code, 413)
        self.assertIn(b"Request too large", response.data)
        self.assertIn(b"no larger than 5 MB", response.data)
        self.assertIn(b'href="/"', response.data)
        self.assertNotIn(b"example.invalid", response.data)

    def test_resend_verification_response_does_not_disclose_account_status(self):
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE users SET email_verified = 0 WHERE id = ?",
                (self.borrower_id,),
            )

        with patch("campusshare_routes.send_verification_email") as send_email:
            unverified_response = self.client.post(
                "/resend-verification",
                data={"email": "borrower@test.example"},
            )
            verified_response = self.client.post(
                "/resend-verification",
                data={"email": "owner@test.example"},
            )

        self.assertEqual(unverified_response.status_code, 200)
        self.assertEqual(unverified_response.data, verified_response.data)
        send_email.assert_called_once()

    def test_verification_resends_are_limited_per_email_and_ip(self):
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE users SET email_verified = 0 WHERE id = ?",
                (self.borrower_id,),
            )

        with patch("campusshare_routes.send_verification_email") as send_email:
            responses = [
                self.client.post(
                    "/resend-verification",
                    data={"email": "borrower@test.example"},
                )
                for _ in range(4)
            ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertTrue(
            all(response.data == responses[0].data for response in responses)
        )
        self.assertEqual(send_email.call_count, 3)

    def test_login_throttles_repeated_failures_and_clears_after_success(self):
        login_data = {
            "email": "borrower@test.example",
            "password": "incorrect-password",
        }
        for index in range(5):
            response = self.client.post(
                "/login",
                data=login_data,
                environ_overrides={"REMOTE_ADDR": f"192.0.2.{index + 1}"},
            )
            self.assertEqual(response.status_code, 401)

        response = self.client.post("/login", data=login_data)
        self.assertEqual(response.status_code, 429)

        with self.database_connection() as connection:
            connection.execute(
                "UPDATE login_attempts SET attempted_at = datetime('now', '-14 minutes')"
            )

        response = self.client.post("/login", data=login_data)
        self.assertEqual(response.status_code, 429)

        with self.database_connection() as connection:
            connection.execute(
                "UPDATE login_attempts SET attempted_at = datetime('now', '-16 minutes')"
            )

        response = self.client.post(
            "/login",
            data={
                "email": "borrower@test.example",
                "password": "test-password-long",
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM login_attempts"
                ).fetchone()[0],
                0,
            )

    def test_security_relevant_actions_are_logged_without_credentials(self):
        with self.assertLogs("app", level="INFO") as captured_logs:
            failed_login = self.client.post(
                "/login",
                data={
                    "email": "borrower@test.example",
                    "password": "wrong-secret-password",
                },
            )
            self.assertEqual(failed_login.status_code, 401)

            self.sign_in_as(self.borrower_id)
            request_response = self.client.post(
                f"/request-equipment/{self.equipment_id}",
                data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
            )
            self.assertEqual(request_response.status_code, 302)
            with self.database_connection() as connection:
                request_id = connection.execute(
                    "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]

            self.sign_in_as(self.owner_id)
            approval_response = self.client.post(
                f"/requests/{request_id}/approve"
            )
            self.assertEqual(approval_response.status_code, 302)
            with self.database_connection() as connection:
                connection.execute(
                    """
                    UPDATE borrowing_requests
                    SET payment_status = 'Paid'
                    WHERE id = ?
                    """,
                    (request_id,),
                )
            deposit_response = self.client.post(
                f"/confirm-deposit/{request_id}"
            )
            self.assertEqual(deposit_response.status_code, 302)
            with self.database_connection() as connection:
                connection.execute(
                    """
                    UPDATE borrowing_requests
                    SET status = 'Completed'
                    WHERE id = ?
                    """,
                    (request_id,),
                )

            damage_response = self.client.post(
                f"/damage-status/{request_id}/damaged",
                data={
                    "damage_report": "A cracked panel",
                    "damage_charge": "10",
                },
            )
            self.assertEqual(damage_response.status_code, 302)
            self.sign_in_as(self.borrower_id)
            dispute_response = self.client.post(
                f"/damage-dispute/{request_id}",
                data={"dispute_reason": "The damage was pre-existing."},
            )
            self.assertEqual(dispute_response.status_code, 302)
            self.sign_in_as(self.owner_id)
            resolution_response = self.client.post(
                f"/damage-dispute/{request_id}/resolve",
                data={"resolution": "cancel"},
            )
            self.assertEqual(resolution_response.status_code, 302)

            with self.database_connection() as connection:
                connection.execute(
                    """
                    UPDATE borrowing_requests
                    SET damage_reported_at = datetime('now', '-49 hours')
                    WHERE id = ?
                    """,
                    (request_id,),
                )
                connection.execute(
                    """
                    INSERT INTO borrowing_requests (
                        equipment_id, borrower_id, start_date, end_date, status,
                        payment_status, deposit_status, damage_status
                    )
                    VALUES (?, ?, '2026-11-03', '2026-11-04', 'Completed',
                            'Paid', 'Held', 'Good Condition')
                    """,
                    (self.equipment_id, self.borrower_id),
                )
                refund_request_id = connection.execute(
                    "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]

            settlement_response = self.client.post(
                f"/settle-damage/{request_id}"
            )
            self.assertEqual(settlement_response.status_code, 302)
            refund_response = self.client.post(
                f"/refund-deposit/{refund_request_id}"
            )
            self.assertEqual(refund_response.status_code, 302)

        logs = "\n".join(captured_logs.output)
        for event in (
            "login_failed",
            "rental_approved",
            "deposit_held",
            "damage_assessed",
            "damage_dispute_opened",
            "damage_dispute_resolved",
            "deposit_settled_for_damage",
            "deposit_refunded",
        ):
            self.assertIn(f"event={event}", logs)
        self.assertIn(f"actor_id={self.owner_id}", logs)
        self.assertIn(f"request_id={request_id}", logs)
        self.assertNotIn("borrower@test.example", logs)
        self.assertNotIn("wrong-secret-password", logs)
        self.assertNotIn("A cracked panel", logs)
        self.assertNotIn("The damage was pre-existing", logs)

    def test_login_session_timeout_and_cookie_security_attributes(self):
        response = self.client.post(
            "/login",
            data={
                "email": "borrower@test.example",
                "password": "test-password-long",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            campusshare.app.config["PERMANENT_SESSION_LIFETIME"],
            timedelta(minutes=30),
        )
        with self.client.session_transaction() as user_session:
            self.assertTrue(user_session.permanent)

        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertIn("Expires=", cookie)

        secure_client = campusshare.app.test_client()
        secure_response = secure_client.post(
            "/login",
            base_url="https://localhost",
            data={
                "email": "borrower@test.example",
                "password": "test-password-long",
            },
        )
        self.assertEqual(secure_response.status_code, 302)
        self.assertIn("Secure", secure_response.headers["Set-Cookie"])

    def test_production_redirects_http_to_https_and_adds_hsts(self):
        with patch.dict(
            campusshare.app.config,
            {"CAMPUSSHARE_ENV": "production"},
        ):
            response = self.client.get(
                "/",
                base_url="http://campusshare.example",
            )
            self.assertEqual(response.status_code, 308)
            self.assertEqual(
                response.headers["Location"],
                "https://campusshare.example/",
            )

            secure_response = self.client.get(
                "/",
                base_url="https://campusshare.example",
            )
            self.assertEqual(secure_response.status_code, 200)
            self.assertEqual(
                secure_response.headers["Strict-Transport-Security"],
                "max-age=31536000",
            )

    def test_logout_requires_csrf_and_does_not_mutate_on_get(self):
        campusshare.app.config["WTF_CSRF_ENABLED"] = True
        self.addCleanup(
            campusshare.app.config.__setitem__,
            "WTF_CSRF_ENABLED",
            False,
        )

        method_response = self.client.get("/logout")
        self.assertEqual(method_response.status_code, 405)
        self.assertIn(b"error-container", method_response.data)
        self.assertIn(b"Method not allowed", method_response.data)
        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"error-container", response.data)

    def test_passwords_reject_common_values_and_accept_eight_characters(self):
        registration = {
            "name": "New Student",
            "email": "new@buaa.edu.cn",
            "student_id": "new-student",
            "password": "Password123!",
            "confirm_password": "Password123!",
        }
        response = self.client.post("/register", data=registration)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"not commonly used", response.data)

        registration["password"] = "Campus8!"
        registration["confirm_password"] = "Campus8!"
        with patch("campusshare_routes.send_verification_email"):
            response = self.client.post("/register", data=registration)

        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            stored_password, stored_role = connection.execute(
                "SELECT password, role FROM users WHERE email = ?",
                ("new@buaa.edu.cn",),
            ).fetchone()
        self.assertNotEqual(stored_password, "Campus8!")
        self.assertTrue(check_password_hash(stored_password, "Campus8!"))
        self.assertEqual(stored_role, "user")

    def test_configured_admin_email_promotes_existing_account(self):
        with patch.dict(
            campusshare.app.config,
            {"ADMIN_EMAILS": frozenset({"owner@test.example"})},
        ):
            campusshare.init_db()
            with self.database_connection() as connection:
                role = connection.execute(
                    "SELECT role FROM users WHERE id = ?",
                    (self.owner_id,),
                ).fetchone()[0]
            self.assertEqual(role, "admin")

            self.sign_in_as(self.owner_id)
            response = self.client.get("/admin")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Administrator tools", response.data)

    def test_other_users_public_pages_show_names_not_emails_or_student_ids(self):
        owner_email = "private-owner-contact@buaa.edu.cn"
        owner_student_id = "private-owner-student-id"
        borrower_email = "private-borrower-contact@buaa.edu.cn"
        borrower_student_id = "private-borrower-student-id"
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE users SET email = ?, student_id = ?, role = 'admin'
                WHERE id = ?
                """,
                (owner_email, owner_student_id, self.owner_id),
            )
            connection.execute(
                "UPDATE users SET email = ?, student_id = ? WHERE id = ?",
                (borrower_email, borrower_student_id, self.borrower_id),
            )

        self.sign_in_as(self.owner_id)
        responses = [
            self.client.get("/students"),
            self.client.get("/browse"),
            self.client.get(f"/equipment/{self.equipment_id}"),
            self.client.get(f"/chat/{self.borrower_id}"),
            self.client.get("/admin"),
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        rendered_pages = b"\n".join(response.data for response in responses)
        self.assertIn(b"Owner", rendered_pages)
        self.assertIn(b"Borrower", rendered_pages)
        for private_value in (
            owner_email,
            owner_student_id,
            borrower_email,
            borrower_student_id,
        ):
            self.assertNotIn(private_value.encode(), rendered_pages)

    def test_allowlisted_registration_receives_admin_role(self):
        with patch.dict(
            campusshare.app.config,
            {"ADMIN_EMAILS": frozenset({"trusted@buaa.edu.cn"})},
        ), patch("campusshare_routes.send_verification_email"):
            response = self.client.post(
                "/register",
                data={
                    "name": "Trusted Administrator",
                    "email": "trusted@buaa.edu.cn",
                    "student_id": "trusted-admin",
                    "password": "TrustedCampus8!",
                    "confirm_password": "TrustedCampus8!",
                    "role": "user",
                },
            )

        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT role, email_verified FROM users WHERE email = ?",
                    ("trusted@buaa.edu.cn",),
                ).fetchone(),
                ("admin", 0),
            )

    def test_admin_can_remove_listing_without_deleting_rental_history(self):
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?",
                (self.owner_id,),
            )
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, total_price
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02',
                        'Completed', 'Paid', 24)
                """,
                (self.equipment_id, self.borrower_id),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/admin/listings/{self.equipment_id}/remove"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.get("/admin").status_code,
            403,
        )

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/admin/listings/{self.equipment_id}/remove"
        )
        self.assertEqual(response.status_code, 302)

        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT is_removed FROM equipment WHERE id = ?",
                    (self.equipment_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM borrowing_requests WHERE equipment_id = ?",
                    (self.equipment_id,),
                ).fetchone()[0],
                1,
            )

        self.assertEqual(
            self.client.get(f"/equipment/{self.equipment_id}").status_code,
            404,
        )
        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
        )
        self.assertEqual(response.status_code, 404)

    def test_admin_suspension_ends_existing_sessions_and_blocks_login(self):
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?",
                (self.owner_id,),
            )

        user_client = campusshare.app.test_client()
        with user_client.session_transaction() as user_session:
            user_session["user_id"] = self.borrower_id
            user_session["user_name"] = "Borrower"

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/admin/users/{self.borrower_id}/suspension",
            data={"action": "suspend"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(user_client.get("/dashboard").status_code, 302)
        response = user_client.post(
            "/login",
            data={
                "email": "borrower@test.example",
                "password": "test-password-long",
            },
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.post(
            f"/admin/users/{self.borrower_id}/suspension",
            data={"action": "restore"},
        )
        self.assertEqual(response.status_code, 302)
        response = user_client.post(
            "/login",
            data={
                "email": "borrower@test.example",
                "password": "test-password-long",
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/admin/users/{self.owner_id}/suspension",
            data={"action": "suspend"},
        )
        self.assertEqual(response.status_code, 403)

    def test_administrator_can_resolve_open_damage_dispute(self):
        with self.database_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'admin' WHERE id = ?",
                (self.owner_id,),
            )
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, deposit_status, damage_status,
                    damage_report, damage_charge, damage_reported_at,
                    damage_dispute_status, damage_dispute_reason
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02', 'Completed',
                        'Paid', 'Held', 'Damaged', 'Broken handle', 10,
                        datetime('now'), 'Pending', 'Damage was pre-existing')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Damage was pre-existing", response.data)
        response = self.client.post(
            f"/damage-dispute/{request_id}/resolve",
            data={"resolution": "revise", "damage_charge": "5"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin", response.headers["Location"])

        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_dispute_status, damage_charge
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Revised", 5.0),
            )

    def test_existing_accounts_are_verified_during_schema_migration(self):
        legacy_database = os.path.join(self.temp_dir.name, "legacy.db")
        connection = sqlite3.connect(legacy_database)
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                student_id TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE equipment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                condition TEXT NOT NULL,
                sharing_type TEXT NOT NULL,
                price REAL DEFAULT 0,
                deposit REAL DEFAULT 0,
                available_from TEXT NOT NULL,
                available_until TEXT NOT NULL,
                location TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO users (name, email, student_id, password)
            VALUES ('Existing Student', 'existing@test.example', 'old-1', 'hash')
            """
        )
        connection.commit()
        connection.close()

        current_database = campusshare.DATABASE
        campusshare.DATABASE = legacy_database
        try:
            campusshare.init_db()
            migrated = connect_database(legacy_database)
            try:
                self.assertEqual(
                    migrated.execute(
                        "SELECT email_verified FROM users WHERE id = 1"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    migrated.execute(
                        "SELECT notification_emails_enabled FROM users WHERE id = 1"
                    ).fetchone()[0],
                    0,
                )
                self.assertIn(
                    "image_path",
                    {
                        row[1]
                        for row in migrated.execute(
                            "PRAGMA table_info(equipment)"
                        ).fetchall()
                    },
                )
                migrated_equipment_columns = {
                    row[1]
                    for row in migrated.execute(
                        "PRAGMA table_info(equipment)"
                    ).fetchall()
                }
                self.assertIn("pickup_time_start", migrated_equipment_columns)
                self.assertIn("return_time_end", migrated_equipment_columns)
            finally:
                migrated.close()
        finally:
            campusshare.DATABASE = current_database

    def test_password_reset_email_failure_is_logged_without_account_disclosure(self):
        reset_link = "https://campusshare.test/reset-password/reset-secret-value"
        with patch(
            "campusshare_routes.send_password_reset_email",
            side_effect=RuntimeError(
                f"Could not deliver {reset_link}: SMTP settings are missing"
            ),
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
        self.assertEqual(unknown_response.status_code, 200)
        csrf_field = re.compile(
            rb'name="csrf_token" value="[^"]+"'
        )
        self.assertEqual(
            csrf_field.sub(b'name="csrf_token" value="<token>"',
                           registered_response.data),
            csrf_field.sub(b'name="csrf_token" value="<token>"',
                           unknown_response.data),
        )
        self.assertTrue(
            any("Could not send a password reset email" in line
                for line in captured_logs.output)
        )
        self.assertNotIn("reset-secret-value", "\n".join(captured_logs.output))
        self.assertNotIn(reset_link, "\n".join(captured_logs.output))

    def test_exception_and_access_logs_do_not_expose_tokens(self):
        sensitive_message = (
            "Sensitive token: reset-secret-value "
            "https://campusshare.test/reset-password/reset-secret-value"
        )
        with self.assertLogs("app", level="ERROR") as captured_logs:
            try:
                raise RuntimeError(sensitive_message)
            except RuntimeError:
                campusshare.app.log_exception(sys.exc_info())

        joined_logs = "\n".join(captured_logs.output)
        self.assertIn("Unhandled application exception (RuntimeError)", joined_logs)
        self.assertNotIn("reset-secret-value", joined_logs)
        self.assertNotIn(sensitive_message, joined_logs)

        record = logging.LogRecord(
            "werkzeug",
            logging.INFO,
            __file__,
            1,
            'GET %s HTTP/1.1',
            ("/verify-email/verification-secret-token",),
            None,
        )
        redaction_filter = campusshare.SensitiveUrlRedactionFilter()
        self.assertTrue(redaction_filter.filter(record))
        self.assertIn("/verify-email/[REDACTED]", record.getMessage())
        self.assertNotIn("verification-secret-token", record.getMessage())

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
        self.assertIn(b"8 characters", response.data)

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
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, message
                    FROM notifications
                    WHERE user_id = ?
                    ORDER BY id DESC
                    """,
                    (self.borrower_id,),
                ).fetchone(),
                (
                    self.borrower_id,
                    "The owner submitted a damage report. Review it and dispute "
                    "the proposed charge within 48 hours.",
                ),
            )

        self.sign_in_as(self.borrower_id)
        borrower_response = self.client.get("/my-rentals")
        self.assertEqual(borrower_response.status_code, 200)
        self.assertIn(b"Cracked lens cover", borrower_response.data)
        self.assertIn(b"Dispute Damage Report", borrower_response.data)
        self.assertIn(b"Proposed Damage Charge", borrower_response.data)
        response = self.client.post(
            f"/damage-dispute/{request_id}",
            data={"dispute_reason": "The lens cover was already cracked."},
        )
        self.assertEqual(response.status_code, 302)

        self.sign_in_as(self.owner_id)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_dispute_status, damage_dispute_reason
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Pending", "The lens cover was already cracked."),
            )

        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 409)
        response = self.client.post(
            f"/damage-dispute/{request_id}/resolve",
            data={"resolution": "revise", "damage_charge": "25"},
        )
        self.assertEqual(response.status_code, 400)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_dispute_status, damage_charge
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Pending", 15.0),
            )
        response = self.client.post(
            f"/damage-dispute/{request_id}/resolve",
            data={"resolution": "revise", "damage_charge": "12"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 409)

        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_dispute_status, damage_dispute_resolution,
                           damage_charge
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Revised", "Revised", 12.0),
            )
            connection.execute(
                """
                UPDATE borrowing_requests
                SET damage_reported_at = datetime('now', '-49 hours')
                WHERE id = ?
                """,
                (request_id,),
            )

        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT deposit_status, damage_charge, equipment.deposit
                    FROM borrowing_requests
                    JOIN equipment
                        ON borrowing_requests.equipment_id = equipment.id
                    WHERE borrowing_requests.id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                ("Settled", 12.0, 20.0),
            )
        owner_response = self.client.get("/requests")
        self.assertEqual(owner_response.status_code, 200)
        self.assertIn(b"Damage Charge:</strong>\n    \xc2\xa512.00", owner_response.data)
        self.assertIn(b"Refund to Borrower:</strong>\n    \xc2\xa58.00", owner_response.data)

        self.sign_in_as(self.borrower_id)
        borrower_response = self.client.get("/my-rentals")
        self.assertEqual(borrower_response.status_code, 200)
        self.assertIn(b"Final Damage Charge:</strong>\n                \xc2\xa512.00", borrower_response.data)
        self.assertIn(b"Refund Received:</strong>\n\n                \xc2\xa58.00", borrower_response.data)

    def test_damage_charge_cannot_be_settled_before_48_hour_review_window(self):
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
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/damage-status/{request_id}/damaged",
            data={"damage_report": "Broken handle", "damage_charge": "10"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"48-hour damage review window", response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deposit_status FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                "Held",
            )

        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE borrowing_requests
                SET damage_reported_at = datetime('now', '-49 hours')
                WHERE id = ?
                """,
                (request_id,),
            )
        response = self.client.post(f"/settle-damage/{request_id}")
        self.assertEqual(response.status_code, 302)

    def test_borrower_cannot_dispute_after_review_window_or_dispute_another_rental(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status,
                    payment_status, deposit_status, damage_status,
                    damage_report, damage_charge, damage_reported_at
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02',
                        'Completed', 'Paid', 'Held', 'Damaged',
                        'Broken handle', 10, datetime('now', '-49 hours'))
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/damage-dispute/{request_id}",
            data={"dispute_reason": "This damage predates the rental."},
        )
        self.assertEqual(response.status_code, 409)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT damage_dispute_status
                    FROM borrowing_requests WHERE id = ?
                    """,
                    (request_id,),
                ).fetchone()[0],
                "Not Disputed",
            )

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/damage-dispute/{request_id}",
            data={"dispute_reason": "Not the borrower."},
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_can_uphold_or_cancel_a_disputed_damage_charge(self):
        for resolution, expected_status, expected_charge in (
            ("uphold", "Upheld", 10.0),
            ("cancel", "Cancelled", 0.0),
        ):
            with self.subTest(resolution=resolution):
                with self.database_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO borrowing_requests (
                            equipment_id, borrower_id, start_date, end_date,
                            status, payment_status, deposit_status
                        )
                        VALUES (?, ?, '2026-11-01', '2026-11-02',
                                'Completed', 'Paid', 'Held')
                        """,
                        (self.equipment_id, self.borrower_id),
                    )
                    request_id = connection.execute(
                        "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]

                self.sign_in_as(self.owner_id)
                response = self.client.post(
                    f"/damage-status/{request_id}/damaged",
                    data={"damage_report": "Broken handle", "damage_charge": "10"},
                )
                self.assertEqual(response.status_code, 302)

                self.sign_in_as(self.borrower_id)
                response = self.client.post(
                    f"/damage-dispute/{request_id}",
                    data={"dispute_reason": "This was not damaged by me."},
                )
                self.assertEqual(response.status_code, 302)

                self.sign_in_as(self.owner_id)
                response = self.client.post(
                    f"/damage-dispute/{request_id}/resolve",
                    data={"resolution": resolution},
                )
                self.assertEqual(response.status_code, 302)
                with self.database_connection() as connection:
                    self.assertEqual(
                        connection.execute(
                            """
                            SELECT damage_dispute_status,
                                   damage_dispute_resolution,
                                   damage_charge
                            FROM borrowing_requests WHERE id = ?
                            """,
                            (request_id,),
                        ).fetchone(),
                        (expected_status, expected_status, expected_charge),
                    )

    def test_owner_can_record_zero_damage_charge(self):
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
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/damage-status/{request_id}/damaged",
            data={"damage_report": "Minor scratch, no charge", "damage_charge": "0"},
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
                ("Damaged", "Minor scratch, no charge", 0.0),
            )

    def test_owner_can_record_good_condition_on_return(self):
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
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(f"/damage-status/{request_id}/good")

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
                ("Good Condition", "", 0.0),
            )

    def test_deposit_refund_requires_completed_held_good_condition(self):
        self.sign_in_as(self.owner_id)
        invalid_states = (
            ("Approved", "Held", "Good Condition"),
            ("Completed", "Pending", "Good Condition"),
            ("Completed", "Refunded", "Good Condition"),
            ("Completed", "Held", "Damaged"),
            ("Completed", "Held", "Not Checked"),
        )

        for rental_status, deposit_status, damage_status in invalid_states:
            with self.subTest(
                rental_status=rental_status,
                deposit_status=deposit_status,
                damage_status=damage_status,
            ):
                with self.database_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO borrowing_requests (
                            equipment_id, borrower_id, start_date, end_date,
                            status, deposit_status, damage_status
                        )
                        VALUES (?, ?, '2026-11-01', '2026-11-02', ?, ?, ?)
                        """,
                        (
                            self.equipment_id,
                            self.borrower_id,
                            rental_status,
                            deposit_status,
                            damage_status,
                        ),
                    )
                    request_id = connection.execute(
                        "SELECT last_insert_rowid()"
                    ).fetchone()[0]

                response = self.client.post(
                    f"/refund-deposit/{request_id}"
                )
                self.assertEqual(response.status_code, 400)
                with self.database_connection() as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT deposit_status FROM borrowing_requests WHERE id = ?",
                            (request_id,),
                        ).fetchone()[0],
                        deposit_status,
                    )
                    connection.execute(
                        "DELETE FROM borrowing_requests WHERE id = ?",
                        (request_id,),
                    )

        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date,
                    status, deposit_status, damage_status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02',
                        'Completed', 'Held', 'Good Condition')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

        response = self.client.post(f"/refund-deposit/{request_id}")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT deposit_status FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                "Refunded",
            )

    def test_owner_cannot_record_negative_damage_charge(self):
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
                "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        self.sign_in_as(self.owner_id)
        response = self.client.post(
            f"/damage-status/{request_id}/damaged",
            data={"damage_report": "Broken part", "damage_charge": "-1"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Damage charge must be zero or greater", response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT damage_status FROM borrowing_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()[0],
                "Not Checked",
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

    def test_sqlite_foreign_keys_prevent_deleting_referenced_records(self):
        connection = connect_database(campusshare.DATABASE)
        try:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02')
                """,
                (self.equipment_id, self.borrower_id),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM equipment WHERE id = ?",
                    (self.equipment_id,),
                )
            connection.rollback()

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM users WHERE id = ?",
                    (self.owner_id,),
                )
            connection.rollback()
        finally:
            connection.close()

    def test_database_initialization_rejects_existing_orphan_records(self):
        connection = sqlite3.connect(campusshare.DATABASE)
        try:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date
                )
                VALUES (99999, 99999, '2026-11-01', '2026-11-02')
                """
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            RuntimeError,
            "foreign-key violation",
        ):
            campusshare.init_db()

    def test_past_rental_date_is_rejected_with_styled_error(self):
        self.sign_in_as(self.borrower_id)
        response = self.client.post(
            f"/request-equipment/{self.equipment_id}",
            data={"start_date": "2020-01-01", "end_date": "2020-01-02"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'id="start_date_error"', response.data)
        self.assertIn(b"Choose today or a future date.", response.data)
        self.assertIn(b'value="2020-01-01"', response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM borrowing_requests"
                ).fetchone()[0],
                0,
            )

    def test_non_owner_cannot_approve_and_pending_history_blocks_listing_delete(self):
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

    def test_listing_with_approved_or_completed_rental_cannot_be_deleted(self):
        self.sign_in_as(self.owner_id)
        for status in ("Approved", "Completed"):
            with self.subTest(status=status):
                with self.database_connection() as connection:
                    connection.execute(
                        """
                        INSERT INTO borrowing_requests (
                            equipment_id, borrower_id, start_date, end_date,
                            status
                        )
                        VALUES (?, ?, '2026-11-01', '2026-11-02', ?)
                        """,
                        (self.equipment_id, self.borrower_id, status),
                    )

                response = self.client.post(
                    f"/delete-listing/{self.equipment_id}"
                )

                self.assertEqual(response.status_code, 409)
                self.assertIn(b"rental history", response.data.lower())
                with self.database_connection() as connection:
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT id FROM equipment WHERE id = ?",
                            (self.equipment_id,),
                        ).fetchone()
                    )

                with self.database_connection() as connection:
                    connection.execute(
                        "DELETE FROM borrowing_requests WHERE equipment_id = ?",
                        (self.equipment_id,),
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

    def test_dashboard_does_not_count_pending_or_booked_items_as_available(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO equipment (
                    owner_id, item_name, category, description, condition,
                    sharing_type, price, deposit, available_from,
                    available_until, location
                )
                VALUES (?, 'Available book', 'Other', 'Test', 'Good',
                        'Free', 0, 0, '2026-01-01', '2027-12-31', 'Campus')
                """,
                (self.owner_id,),
            )
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02', 'Pending')
                """,
                (self.equipment_id, self.borrower_id),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.get_data(as_text=True),
            r"<h2>\s*1\s*</h2>",
        )

    def test_dashboard_shows_average_rating_received_by_user(self):
        with self.database_connection() as connection:
            for rating, student_id in (
                (5, "review-1"),
                (4, "review-2"),
            ):
                connection.execute(
                    """
                    INSERT INTO users (
                        name, email, student_id, password, email_verified
                    )
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (
                        "Reviewer",
                        f"{student_id}@test.example",
                        student_id,
                        generate_password_hash("test-password-long"),
                    ),
                )
                reviewer_id = connection.execute(
                    "SELECT id FROM users WHERE student_id = ?",
                    (student_id,),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO borrowing_requests (
                        equipment_id, borrower_id, start_date, end_date, status
                    )
                    VALUES (?, ?, '2026-11-01', '2026-11-02', 'Completed')
                    """,
                    (self.equipment_id, reviewer_id),
                )
                request_id = connection.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO ratings (
                        borrowing_request_id, rater_id, rated_user_id, rating
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (request_id, reviewer_id, self.borrower_id, rating),
                )

        self.sign_in_as(self.borrower_id)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Average Rating", response.data)
        self.assertRegex(
            response.get_data(as_text=True),
            r"<h2>\s*4\.5\s*</h2>\s*<p>Average Rating</p>",
        )

    def test_only_message_sender_can_delete_message(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO messages (sender_id, receiver_id, message_text)
                VALUES (?, ?, 'Sender-owned message')
                """,
                (self.owner_id, self.borrower_id),
            )
            sender_message_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO messages (sender_id, receiver_id, message_text)
                VALUES (?, ?, 'Receiver-owned message')
                """,
                (self.borrower_id, self.owner_id),
            )
            receiver_message_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

        self.sign_in_as(self.borrower_id)
        response = self.client.get(f"/chat/{self.owner_id}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            f"/delete-message/{sender_message_id}".encode(),
            response.data,
        )
        self.assertIn(
            f"/delete-message/{receiver_message_id}".encode(),
            response.data,
        )

        response = self.client.post(f"/delete-message/{sender_message_id}")
        self.assertEqual(response.status_code, 403)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM messages WHERE id = ?",
                    (sender_message_id,),
                ).fetchone()[0],
                1,
            )

        response = self.client.post(f"/delete-message/{receiver_message_id}")
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM messages WHERE id = ?",
                    (receiver_message_id,),
                ).fetchone()
            )

    def test_user_can_mark_only_their_own_notifications_as_read(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO notifications (user_id, message)
                VALUES (?, 'Borrower notification')
                """,
                (self.borrower_id,),
            )
            borrower_notification_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO notifications (user_id, message)
                VALUES (?, 'Owner notification')
                """,
                (self.owner_id,),
            )
            owner_notification_id = connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

        self.sign_in_as(self.borrower_id)
        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Mark as read", response.data)
        self.assertIn(b"Unread", response.data)

        response = self.client.post(
            f"/notifications/{owner_notification_id}/read"
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT is_read FROM notifications WHERE id = ?",
                    (owner_notification_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT is_read FROM notifications WHERE id = ?",
                    (borrower_notification_id,),
                ).fetchone()[0],
                0,
            )

        response = self.client.post(
            f"/notifications/{borrower_notification_id}/read"
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT is_read FROM notifications WHERE id = ?",
                    (borrower_notification_id,),
                ).fetchone()[0],
                1,
            )

        response = self.client.get("/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Read", response.data)
        self.assertNotIn(b"Mark as read", response.data)

    def test_notification_email_is_opt_in_and_sent_to_recipient(self):
        self.sign_in_as(self.borrower_id)
        with patch("notification_utils.send_notification_email") as send_email:
            response = self.client.post(
                f"/request-equipment/{self.equipment_id}",
                data={"start_date": "2026-11-01", "end_date": "2026-11-02"},
            )
            self.assertEqual(response.status_code, 302)
            send_email.assert_not_called()
            with self.database_connection() as connection:
                request_id = connection.execute(
                    "SELECT id FROM borrowing_requests ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]

            response = self.client.get("/notifications")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Email me when I receive CampusShare notifications", response.data)

            response = self.client.post(
                "/notifications/email-preference",
                data={"notification_emails_enabled": "1"},
            )
            self.assertEqual(response.status_code, 302)
            with self.database_connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT notification_emails_enabled FROM users WHERE id = ?",
                        (self.borrower_id,),
                    ).fetchone()[0],
                    1,
                )

            self.sign_in_as(self.owner_id)
            response = self.client.post(f"/requests/{request_id}/approve")
            self.assertEqual(response.status_code, 302)
            send_email.assert_called_once_with(
                "borrower@test.example",
                "Your request for Camera was approved.",
            )

    def test_notification_email_failure_is_logged_without_losing_in_app_notice(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                UPDATE users
                SET notification_emails_enabled = 1
                WHERE id = ?
                """,
                (self.owner_id,),
            )

        self.sign_in_as(self.borrower_id)
        with patch(
            "notification_utils.send_notification_email",
            side_effect=RuntimeError("SMTP settings are missing"),
        ):
            with self.assertLogs("notification_utils", level="ERROR") as logs:
                response = self.client.post(
                    f"/request-equipment/{self.equipment_id}",
                    data={
                        "start_date": "2026-11-01",
                        "end_date": "2026-11-02",
                    },
                )
        self.assertEqual(response.status_code, 302)
        self.assertIn("Could not send an opted-in notification email", logs.output[0])
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM notifications WHERE user_id = ?",
                    (self.owner_id,),
                ).fetchone()[0],
                1,
            )

    def test_borrower_and_owner_can_review_each_other_once_after_completion(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02', 'Completed')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.sign_in_as(self.borrower_id)
        response = self.client.get(f"/rate/{request_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Rate Owner", response.data)
        response = self.client.post(
            f"/rate/{request_id}",
            data={"rating": "5", "comment": "Great equipment"},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            f"/rate/{request_id}",
            data={"rating": "4", "comment": "Duplicate review"},
        )
        self.assertEqual(response.status_code, 409)

        self.sign_in_as(self.owner_id)
        response = self.client.get(f"/rate/{request_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Rate Borrower", response.data)
        response = self.client.post(
            f"/rate/{request_id}",
            data={"rating": "4", "comment": "Reliable borrower"},
        )
        self.assertEqual(response.status_code, 302)

        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT rater_id, rated_user_id, rating
                    FROM ratings
                    WHERE borrowing_request_id = ?
                    ORDER BY rater_id
                    """,
                    (request_id,),
                ).fetchall(),
                [
                    (self.owner_id, self.borrower_id, 4),
                    (self.borrower_id, self.owner_id, 5),
                ],
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get("/my-reviews")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Reliable borrower", response.data)
        self.assertIn(b"4.0/5", response.data)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(
            response.get_data(as_text=True),
            r"<h2>\s*4\.0\s*</h2>\s*<p>Average Rating</p>",
        )

        self.sign_in_as(self.owner_id)
        response = self.client.get("/requests")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"You have reviewed the borrower", response.data)

    def test_rating_schema_migration_preserves_existing_review(self):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, '2026-11-01', '2026-11-02', 'Completed')
                """,
                (self.equipment_id, self.borrower_id),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]
            connection.execute("DROP TABLE ratings")
            connection.execute(
                """
                CREATE TABLE ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    borrowing_request_id INTEGER NOT NULL UNIQUE,
                    rater_id INTEGER NOT NULL,
                    rated_user_id INTEGER NOT NULL,
                    rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (borrowing_request_id)
                        REFERENCES borrowing_requests(id),
                    FOREIGN KEY (rater_id) REFERENCES users(id),
                    FOREIGN KEY (rated_user_id) REFERENCES users(id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ratings (
                    borrowing_request_id, rater_id, rated_user_id,
                    rating, comment
                )
                VALUES (?, ?, ?, 5, 'Existing review')
                """,
                (request_id, self.borrower_id, self.owner_id),
            )

        campusshare.init_db()
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT comment FROM ratings"
                ).fetchone()[0],
                "Existing review",
            )
            connection.execute(
                """
                INSERT INTO ratings (
                    borrowing_request_id, rater_id, rated_user_id,
                    rating, comment
                )
                VALUES (?, ?, ?, 4, 'Reciprocal review')
                """,
                (request_id, self.owner_id, self.borrower_id),
            )

    def test_return_reminder_is_created_once_for_rental_due_tomorrow(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, ?, ?, 'Approved')
                """,
                (self.equipment_id, self.borrower_id, date.today().isoformat(),
                 tomorrow),
            )
            request_id = connection.execute(
                "SELECT id FROM borrowing_requests"
            ).fetchone()[0]

        self.assertEqual(
            create_due_return_reminders(campusshare.DATABASE, today=date.today()),
            1,
        )
        self.assertEqual(
            create_due_return_reminders(campusshare.DATABASE, today=date.today()),
            0,
        )
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute(
                    """
                    SELECT user_id, message, link
                    FROM notifications
                    WHERE message LIKE '%due for return tomorrow%'
                    """
                ).fetchall(),
                [
                    (
                        self.borrower_id,
                        f"Your rental of Camera is due for return tomorrow ({tomorrow}).",
                        "/my-rentals",
                    )
                ],
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT borrowing_request_id, reminder_type
                    FROM rental_reminders
                    WHERE borrowing_request_id = ?
                    """,
                    (request_id,),
                ).fetchone(),
                (request_id, "return_due_tomorrow"),
            )

    def test_active_rental_displays_due_date_countdown(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id, borrower_id, start_date, end_date, status
                )
                VALUES (?, ?, ?, ?, 'Approved')
                """,
                (self.equipment_id, self.borrower_id, date.today().isoformat(),
                 tomorrow),
            )

        self.sign_in_as(self.borrower_id)
        response = self.client.get("/my-rentals")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Return due tomorrow", response.data)

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

    def test_listing_photo_upload_displays_replaces_and_removes_safely(self):
        self.sign_in_as(self.owner_id)
        response = self.client.post(
            "/list-item",
            data=self.listing_form_data(
                image=(io.BytesIO(b"\x89PNG\r\n\x1a\nimage-bytes"), "tripod.png")
            ),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)

        with self.database_connection() as connection:
            equipment_id, image_path = connection.execute(
                "SELECT id, image_path FROM equipment ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertTrue(image_path.startswith("uploads/"))
        old_image = os.path.join(
            campusshare.app.config["UPLOAD_FOLDER"],
            os.path.basename(image_path),
        )
        self.assertTrue(os.path.isfile(old_image))

        browse_response = self.client.get("/browse")
        self.assertEqual(browse_response.status_code, 200)
        self.assertIn(image_path.encode(), browse_response.data)
        detail_response = self.client.get(f"/equipment/{equipment_id}")
        self.assertEqual(detail_response.status_code, 200)
        self.assertIn(image_path.encode(), detail_response.data)
        self.assertIn(b"Owner", detail_response.data)
        listings_response = self.client.get("/my-listings")
        self.assertEqual(listings_response.status_code, 200)
        self.assertIn(image_path.encode(), listings_response.data)

        response = self.client.post(
            f"/edit-listing/{equipment_id}",
            data=self.listing_form_data(
                item_name="Updated tripod",
                image=(io.BytesIO(b"\xff\xd8\xffjpeg-bytes"), "replacement.jpg"),
            ),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(os.path.exists(old_image))

        with self.database_connection() as connection:
            new_image_path = connection.execute(
                "SELECT image_path FROM equipment WHERE id = ?",
                (equipment_id,),
            ).fetchone()[0]
        self.assertTrue(new_image_path.endswith(".jpg"))

        response = self.client.post(
            f"/edit-listing/{equipment_id}",
            data=self.listing_form_data(
                item_name="Updated tripod",
                remove_image="1",
            ),
        )
        self.assertEqual(response.status_code, 302)
        with self.database_connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT image_path FROM equipment WHERE id = ?",
                    (equipment_id,),
                ).fetchone()[0]
            )

    def test_listing_photo_upload_rejects_fake_image_content(self):
        self.sign_in_as(self.owner_id)
        response = self.client.post(
            "/list-item",
            data=self.listing_form_data(
                image=(io.BytesIO(b"not a png"), "fake.png")
            ),
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"not a valid", response.data)
        with self.database_connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0],
                1,
            )

    def test_unknown_url_uses_styled_not_found_page(self):
        response = self.client.get("/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertIn(b"error-container", response.data)
        self.assertIn(b"Page not found", response.data)


if __name__ == "__main__":
    unittest.main()
