
import unittest
from datetime import date, datetime, timedelta

import test_app as original_tests


class HourlyOverdueTests(unittest.TestCase):
    # Reuse the existing CampusShare test setup
    setUp = original_tests.RentalWorkflowTests.setUp
    tearDown = original_tests.RentalWorkflowTests.tearDown
    database_connection = original_tests.RentalWorkflowTests.database_connection
    sign_in_as = original_tests.RentalWorkflowTests.sign_in_as

    def create_rental(
        self,
        end_date,
        end_time,
        price_unit="hour",
        awaiting_handover=0,
        handed_over=True,
    ):
        with self.database_connection() as connection:
            connection.execute(
                """
                INSERT INTO borrowing_requests (
                    equipment_id,
                    borrower_id,
                    start_date,
                    end_date,
                    start_time,
                    end_time,
                    price_unit,
                    unit_price,
                    status,
                    payment_status,
                    awaiting_handover,
                    handed_over_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 12,
                    'Approved', 'Paid', ?, ?
                )
                """,
                (
                    self.equipment_id,
                    self.borrower_id,
                    end_date,
                    end_date,
                    "00:00",
                    end_time,
                    price_unit,
                    awaiting_handover,
                    (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        if handed_over
                        else None
                    ),
                ),
            )

            return connection.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

    def check_rental_status(self, request_id):
        self.sign_in_as(self.borrower_id)

        response = self.client.get("/my-rentals")
        self.assertEqual(response.status_code, 200)

        with self.database_connection() as connection:
            return connection.execute(
                """
                SELECT status
                FROM borrowing_requests
                WHERE id = ?
                """,
                (request_id,),
            ).fetchone()[0]

    def test_hourly_rental_overdue_same_day(self):
        now = datetime.now()
        end_datetime = now - timedelta(minutes=5)

        if end_datetime.date() != now.date():
            self.skipTest("Too close to midnight")

        request_id = self.create_rental(
            end_datetime.date().isoformat(),
            end_datetime.strftime("%H:%M"),
        )

        self.assertEqual(
            self.check_rental_status(request_id),
            "Overdue",
        )

    def test_hourly_rental_not_overdue_before_end_time(self):
        now = datetime.now()
        end_datetime = now + timedelta(minutes=30)

        if end_datetime.date() != now.date():
            self.skipTest("Too close to midnight")

        request_id = self.create_rental(
            end_datetime.date().isoformat(),
            end_datetime.strftime("%H:%M"),
        )

        self.assertEqual(
            self.check_rental_status(request_id),
            "Approved",
        )

    def test_rental_awaiting_handover_not_overdue(self):
        yesterday = date.today() - timedelta(days=1)

        request_id = self.create_rental(
            yesterday.isoformat(),
            "12:00",
            awaiting_handover=1,
            handed_over=False,
        )

        self.assertEqual(
            self.check_rental_status(request_id),
            "Approved",
        )
