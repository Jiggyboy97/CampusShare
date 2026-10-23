from datetime import date, timedelta
import os

from database import connect_database
from notification_utils import create_notification


DATABASE = os.path.join(os.path.dirname(__file__), "campusshare.db")


def create_due_return_reminders(database_path, today=None):
    today = today or date.today()
    return_date = (today + timedelta(days=1)).isoformat()
    connection = connect_database(database_path)
    reminders_created = 0

    try:
        rentals = connection.execute(
            """
            SELECT
                borrowing_requests.id,
                borrowing_requests.borrower_id,
                borrowing_requests.end_date,
                equipment.item_name
            FROM borrowing_requests
            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id
            WHERE borrowing_requests.status = 'Approved'
                AND date(borrowing_requests.end_date) = ?
            """,
            (return_date,),
        ).fetchall()

        for request_id, borrower_id, end_date, item_name in rentals:
            reminder = connection.execute(
                """
                INSERT OR IGNORE INTO rental_reminders (
                    borrowing_request_id, reminder_type
                )
                VALUES (?, 'return_due_tomorrow')
                """,
                (request_id,),
            )
            if reminder.rowcount:
                create_notification(
                    connection,
                    borrower_id,
                    f"Your rental of {item_name} is due for return tomorrow "
                    f"({end_date}).",
                    "/my-rentals",
                )
                reminders_created += 1

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return reminders_created


if __name__ == "__main__":
    count = create_due_return_reminders(DATABASE)
    print(f"Created {count} return reminder(s).")
