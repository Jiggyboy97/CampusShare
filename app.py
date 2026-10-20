
from importlib import import_module
import os

from flask import Flask, render_template, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from itsdangerous import URLSafeTimedSerializer
from database import connect_database

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY")
if not app.config["SECRET_KEY"]:
    raise RuntimeError(
        "FLASK_SECRET_KEY is required. Set it in the environment before starting CampusShare."
    )
app.config["WTF_CSRF_TIME_LIMIT"] = 3600
CSRFProtect(app)
serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"])
DATABASE = os.path.join(
    os.path.dirname(__file__),
    "campusshare.db"
)


# =====================================================
# DATABASE
# =====================================================

def init_db():

    connection = connect_database(DATABASE)

    # USERS TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            student_id TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        )
    """)

    # MESSAGES TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_read INTEGER DEFAULT 0,

            FOREIGN KEY (sender_id)
            REFERENCES users(id),

            FOREIGN KEY (receiver_id)
            REFERENCES users(id)
        )
    """)
    
    # EQUIPMENT TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS equipment (
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
    """)

    # BORROWING REQUESTS TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS borrowing_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            equipment_id INTEGER NOT NULL,
            borrower_id INTEGER NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
payment_status TEXT NOT NULL DEFAULT 'Unpaid',
deposit_status TEXT NOT NULL DEFAULT 'Pending',
total_price REAL DEFAULT 0,
late_fee REAL DEFAULT 0,
late_fee_status TEXT NOT NULL DEFAULT 'Unpaid',
damage_status TEXT NOT NULL DEFAULT 'Not Checked',
damage_report TEXT DEFAULT '',
damage_charge REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Pending',
            FOREIGN KEY (equipment_id) REFERENCES equipment(id),
            FOREIGN KEY (borrower_id) REFERENCES users(id)
        )
    """)
    columns = [
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(borrowing_requests)"
        ).fetchall()
    ]

    if "late_fee_status" not in columns:
        connection.execute(
            "ALTER TABLE borrowing_requests "
            "ADD COLUMN late_fee_status TEXT NOT NULL DEFAULT 'Unpaid'"
        )
    # RATINGS TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            borrowing_request_id INTEGER NOT NULL UNIQUE,
            rater_id INTEGER NOT NULL,
            rated_user_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating >= 1 AND rating <= 5),
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (borrowing_request_id) REFERENCES borrowing_requests(id),
            FOREIGN KEY (rater_id) REFERENCES users(id),
            FOREIGN KEY (rated_user_id) REFERENCES users(id)
        )
    """)
    # NOTIFICATIONS TABLE
    connection.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            link TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (user_id)
            REFERENCES users(id)
        )
    """)
    connection.commit()
    connection.close()

    









import_module("routes")


@app.errorhandler(404)
def page_not_found(_error):
    return render_template(
        "error.html",
        title="Page not found",
        message=_error.description,
        back_url=url_for("home"),
    ), 404


@app.errorhandler(500)
def internal_server_error(error):
    app.logger.error("Unhandled application error: %s", error)
    return render_template(
        "error.html",
        title="Something went wrong",
        message="The request could not be completed. Please try again.",
        back_url=url_for("home"),
    ), 500


@app.errorhandler(CSRFError)
def csrf_error(error):
    return render_template(
        "error.html",
        title="Form expired",
        message="This form is invalid or has expired. Please reload the page and try again.",
        back_url=url_for("home"),
    ), 400


# =====================================================
# START APPLICATION
# =====================================================

if __name__ == "__main__":

    init_db()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )