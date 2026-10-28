
import logging
import os
import sys
from urllib.parse import urlsplit, urlunsplit
from datetime import timedelta
from flask import Flask, render_template, request, redirect, session
from flask_wtf.csrf import CSRFError, CSRFProtect
from itsdangerous import URLSafeTimedSerializer

from database import connect_database


# =====================================================
# CAMPUSSHARE APPLICATION CONFIGURATION
# =====================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE = os.path.join(BASE_DIR, "campusshare.db")

app = Flask(__name__)

app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
def safe_log_exception(exc_info):
    exception_type = exc_info[0]
    error_name = (
        exception_type.__name__
        if exception_type is not None
        else "UnknownError"
    )

    app.logger.error(
        "Unhandled application exception (%s)",
        error_name
    )


app.log_exception = safe_log_exception


class SensitiveUrlRedactionFilter(logging.Filter):
    def filter(self, record):
        import re

        message = record.getMessage()

        message = re.sub(
            r"/(verify-email|reset-password|qr-payment/confirm)/[^/?\s]+",
            r"/\1/[REDACTED]",
            message
        )

        record.msg = message
        record.args = ()

        return True

app.config["UPLOAD_FOLDER"] = os.path.join(
    BASE_DIR,
    "static",
    "uploads"
)

app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY")

if not app.config["SECRET_KEY"]:
    raise RuntimeError(
        "FLASK_SECRET_KEY is required. "
        "Set it before starting CampusShare."
    )

app.config["WTF_CSRF_TIME_LIMIT"] = 3600
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
class SecureSessionInterface(app.session_interface.__class__):
    def get_cookie_secure(self, app):
        return request.is_secure


app.session_interface = SecureSessionInterface()
app.config["ADMIN_EMAILS"] = {
    email.strip().lower()
    for email in os.environ.get(
        "CAMPUSSHARE_ADMIN_EMAILS", ""
    ).split(",")
    if email.strip()
}
@app.context_processor
def inject_unread_notification_count():
    user_id = session.get("user_id")

    if not user_id:
        return {"unread_notification_count": 0}

    connection = connect_database(DATABASE)

    try:
        result = connection.execute(
            """
            SELECT COUNT(*)
            FROM notifications
            WHERE user_id = ?
            AND COALESCE(is_read, 0) = 0
            """,
            (user_id,)
        ).fetchone()

        unread_count = result[0]

    finally:
        connection.close()

    return {
        "unread_notification_count": unread_count
    }
# Count unread private messages
@app.context_processor
def inject_unread_message_count():

    user_id = session.get("user_id")

    if not user_id:
        return {"unread_message_count": 0}

    connection = connect_database(DATABASE)

    try:
        result = connection.execute(
            """
            SELECT COUNT(*)
            FROM messages
            WHERE receiver_id = ?
              AND is_read = 0
            """,
            (user_id,)
        ).fetchone()

        return {
            "unread_message_count": result[0] if result else 0
        }

    finally:
        connection.close()
CSRFProtect(app)
@app.before_request
def enforce_https():
    if (
        app.config.get("CAMPUSSHARE_ENV") == "production"
        and not request.is_secure
    ):
        return redirect(
            request.url.replace("http://", "https://", 1),
            code=308
        )


@app.after_request
def add_hsts_header(response):
    if (
        app.config.get("CAMPUSSHARE_ENV") == "production"
        and request.is_secure
    ):
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000"
        )

    return response

serializer = URLSafeTimedSerializer(
    app.config["SECRET_KEY"]
)


# =====================================================
# SAFE BACK URL
# =====================================================

def safe_back_url():
    referrer = request.referrer

    if not referrer:
        return "/"

    try:
        parsed = urlsplit(referrer)
        current = urlsplit(request.host_url)
    except ValueError:
        return "/"

    if parsed.scheme not in ("http", "https"):
        return "/"

    if parsed.netloc != current.netloc:
        return "/"

    if not parsed.path.startswith("/"):
        return "/"

    if parsed.path.startswith("//"):
        return "/"

    return urlunsplit((
        "",
        "",
        parsed.path,
        parsed.query,
        parsed.fragment
    ))


# =====================================================
# DATABASE HELPER FUNCTIONS
# =====================================================

def get_table_columns(connection, table_name):
    allowed_tables = {
        "users",
        "equipment",
        "borrowing_requests"
    }

    if table_name not in allowed_tables:
        raise ValueError("Invalid database table")

    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def add_missing_columns(connection, table_name, definitions):
    existing_columns = get_table_columns(
        connection,
        table_name
    )

    for column_name, column_type in definitions.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE {table_name} "
                f"ADD COLUMN {column_name} {column_type}"
            )


# =====================================================
# DATABASE INITIALIZATION
# =====================================================

def init_db():
    connection = connect_database(DATABASE)

    try:

        # =============================================
        # USERS TABLE
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                student_id TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL,
                email_verified INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'user',
                suspended INTEGER NOT NULL DEFAULT 0,
                notification_emails_enabled
                    INTEGER NOT NULL DEFAULT 0
            )
        """)

        add_missing_columns(connection, "users", {
            "email_verified": "INTEGER NOT NULL DEFAULT 1",
            "role": "TEXT NOT NULL DEFAULT 'user'",
            "suspended": "INTEGER NOT NULL DEFAULT 0",
            "notification_emails_enabled":
                "INTEGER NOT NULL DEFAULT 0"
        })
        # Promote approved administrator accounts
        admin_emails = app.config.get("ADMIN_EMAILS", set())

        for admin_email in admin_emails:
            connection.execute(
                """
                UPDATE users
                SET role = 'admin'
                WHERE LOWER(email) = ?
                """,
                (admin_email.strip().lower(),)
            )

        # =============================================
        # LOGIN ATTEMPTS
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                attempted_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_login_attempts_email_ip_time
            ON login_attempts (
                email,
                ip_address,
                attempted_at
            )
        """)

        # =============================================
        # EMAIL VERIFICATION ATTEMPTS
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS
                verification_email_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    attempted_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
        """)

        connection.execute("""
            CREATE INDEX IF NOT EXISTS
                idx_verification_attempts_email_ip_time
            ON verification_email_attempts (
                email,
                ip_address,
                attempted_at
            )
        """)

        # =============================================
        # MESSAGES TABLE
        # =============================================

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

        # =============================================
        # EQUIPMENT TABLE
        # =============================================

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
                image_path TEXT,
                price_unit TEXT NOT NULL DEFAULT 'day',
                pickup_time_start TEXT,
                return_time_end TEXT,
                is_removed INTEGER NOT NULL DEFAULT 0,

                FOREIGN KEY (owner_id)
                    REFERENCES users(id)
            )
        """)

        add_missing_columns(connection, "equipment", {
            "image_path": "TEXT",
            "price_unit": "TEXT NOT NULL DEFAULT 'day'",
            "pickup_time_start": "TEXT",
            "return_time_end": "TEXT",
            "is_removed": "INTEGER NOT NULL DEFAULT 0"
        })

        # =============================================
        # BORROWING REQUESTS TABLE
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS borrowing_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment_id INTEGER NOT NULL,
                borrower_id INTEGER NOT NULL,

                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,

                payment_status TEXT NOT NULL
                    DEFAULT 'Unpaid',

                deposit_status TEXT NOT NULL
                    DEFAULT 'Pending',

                total_price REAL DEFAULT 0,

                start_time TEXT,
                end_time TEXT,

                price_unit TEXT NOT NULL DEFAULT 'day',
                unit_price REAL,

                late_fee REAL DEFAULT 0,

                late_fee_status TEXT NOT NULL
                    DEFAULT 'Unpaid',

                damage_status TEXT NOT NULL
                    DEFAULT 'Not Checked',

                damage_report TEXT DEFAULT '',
                damage_charge REAL DEFAULT 0,

                damage_reported_at TEXT,

                damage_dispute_status TEXT NOT NULL
                    DEFAULT 'Not Disputed',

                damage_dispute_reason TEXT NOT NULL
                    DEFAULT '',

                damage_dispute_resolution TEXT NOT NULL
                    DEFAULT '',

                status TEXT NOT NULL DEFAULT 'Pending',

                FOREIGN KEY (equipment_id)
                    REFERENCES equipment(id),

                FOREIGN KEY (borrower_id)
                    REFERENCES users(id)
            )
        """)

        add_missing_columns(
            connection,
            "borrowing_requests",
            {
                "payment_status":
                    "TEXT NOT NULL DEFAULT 'Unpaid'",

                "deposit_status":
                    "TEXT NOT NULL DEFAULT 'Pending'",

                "total_price": "REAL DEFAULT 0",

                "start_time": "TEXT",

                "end_time": "TEXT",

                "price_unit":
                    "TEXT NOT NULL DEFAULT 'day'",

                "unit_price": "REAL",

                "late_fee": "REAL DEFAULT 0",

                "late_fee_status":
                    "TEXT NOT NULL DEFAULT 'Unpaid'",

                "damage_status":
                    "TEXT NOT NULL DEFAULT 'Not Checked'",

                "damage_report":
                    "TEXT DEFAULT ''",

                "damage_charge":
                    "REAL DEFAULT 0",

                "damage_reported_at":
                    "TEXT",

                "damage_dispute_status":
                    "TEXT NOT NULL DEFAULT 'Not Disputed'",

                "damage_dispute_reason":
                    "TEXT NOT NULL DEFAULT ''",

               "damage_dispute_resolution":
    "TEXT NOT NULL DEFAULT ''",

"meetup_at": "TEXT",
"meetup_location": "TEXT",
"meetup_status": "TEXT NOT NULL DEFAULT 'Not Scheduled'",
"meetup_proposed_at": "TEXT",
"meetup_confirmed_at": "TEXT",
"meetup_owner_verified_at": "TEXT",

"meetup_borrower_verified_at": "TEXT",
"inspection_rejected_by": "INTEGER",
"inspection_rejection_reason": "TEXT NOT NULL DEFAULT ''",
"inspection_rejected_at": "TEXT"

            }
        )
        # =============================================
        # EQUIPMENT INSPECTION PHOTOS
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS rental_condition_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                borrowing_request_id INTEGER NOT NULL,

                uploaded_by INTEGER NOT NULL,

                stage TEXT NOT NULL
                    CHECK (stage IN ('pickup', 'return')),

                image_path TEXT NOT NULL,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (borrowing_request_id)
                    REFERENCES borrowing_requests(id),

                FOREIGN KEY (uploaded_by)
                    REFERENCES users(id)
            )
        """)

        # =============================================
        # RATINGS AND REVIEWS
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                borrowing_request_id INTEGER NOT NULL,
                rater_id INTEGER NOT NULL,
                rated_user_id INTEGER NOT NULL,

                rating INTEGER NOT NULL
                    CHECK (rating >= 1 AND rating <= 5),

                comment TEXT,

                created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                UNIQUE (
                    borrowing_request_id,
                    rater_id
                ),

                FOREIGN KEY (borrowing_request_id)
                    REFERENCES borrowing_requests(id),

                FOREIGN KEY (rater_id)
                    REFERENCES users(id),

                FOREIGN KEY (rated_user_id)
                    REFERENCES users(id)
            )
        """)

        rating_indexes = connection.execute(
            "PRAGMA index_list(ratings)"
        ).fetchall()

        has_single_review_constraint = any(
            index[2]
            and [
                column[2]
                for column in connection.execute(
                    f'PRAGMA index_info("{index[1]}")'
                ).fetchall()
            ] == ["borrowing_request_id"]
            for index in rating_indexes
        )

        if has_single_review_constraint:

            connection.execute("""
                CREATE TABLE ratings_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    borrowing_request_id INTEGER NOT NULL,
                    rater_id INTEGER NOT NULL,
                    rated_user_id INTEGER NOT NULL,

                    rating INTEGER NOT NULL
                        CHECK (rating >= 1 AND rating <= 5),

                    comment TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                    UNIQUE (
                        borrowing_request_id,
                        rater_id
                    ),

                    FOREIGN KEY (borrowing_request_id)
                        REFERENCES borrowing_requests(id),

                    FOREIGN KEY (rater_id)
                        REFERENCES users(id),

                    FOREIGN KEY (rated_user_id)
                        REFERENCES users(id)
                )
            """)

            connection.execute("""
                INSERT INTO ratings_new (
                    id,
                    borrowing_request_id,
                    rater_id,
                    rated_user_id,
                    rating,
                    comment,
                    created_at
                )

                SELECT
                    id,
                    borrowing_request_id,
                    rater_id,
                    rated_user_id,
                    rating,
                    comment,
                    created_at

                FROM ratings
            """)

            connection.execute("DROP TABLE ratings")

            connection.execute(
                "ALTER TABLE ratings_new RENAME TO ratings"
            )

        # =============================================
        # NOTIFICATIONS
        # =============================================

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

        # =============================================
        # RENTAL REMINDERS
        # =============================================

        connection.execute("""
            CREATE TABLE IF NOT EXISTS rental_reminders (
                borrowing_request_id INTEGER NOT NULL,
                reminder_type TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,

                PRIMARY KEY (
                    borrowing_request_id,
                    reminder_type
                ),

                FOREIGN KEY (borrowing_request_id)
                    REFERENCES borrowing_requests(id)
            )
        """)

                # Check for broken database relationships
        violations = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()

        if violations:
            raise RuntimeError(
                "Database foreign-key violation detected. "
                "Check the database before continuing."
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# =====================================================
# LOAD CAMPUSSHARE ROUTES
# =====================================================

if __name__ == "__main__":
    sys.modules.setdefault(
        "app",
        sys.modules[__name__]
    )

import campusshare_routes  # noqa: E402


# =====================================================
# ERROR HANDLING
# =====================================================

def show_error(title, message, status_code):
    return render_template(
        "error.html",
        title=title,
        message=message,
        back_url=safe_back_url()
    ), status_code


@app.errorhandler(404)
def page_not_found(error):
    return show_error(
        "Page not found",
        error.description,
        404
    )


@app.errorhandler(405)
def method_not_allowed(_error):
    return show_error(
        "Method not allowed",
        "This action cannot be performed using this request method.",
        405
    )


@app.errorhandler(413)
def request_entity_too_large(_error):
    return show_error(
        "Request too large",
        "Uploads must be no larger than 5 MB.",
        413
    )


@app.errorhandler(CSRFError)
def csrf_error(_error):
    return show_error(
        "Form expired",
        "This form is invalid or has expired. "
        "Please reload the page and try again.",
        400
    )


@app.errorhandler(500)
def internal_server_error(error):
    app.logger.error(
        "Unhandled application error: %s",
        error
    )

    return show_error(
        "Something went wrong",
        "The request could not be completed. Please try again.",
        500
    )


# =====================================================
# START CAMPUSSHARE
# =====================================================

if __name__ == "__main__":
    init_db()

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
