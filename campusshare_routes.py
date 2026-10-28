from datetime import date, datetime
import io
import math
import os
import sqlite3
import smtplib
import uuid

import qrcode
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import current_app, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import app as application
from app import app, serializer
from database import connect_database
from email_utils import send_password_reset_email, send_verification_email
from notification_utils import create_notification


LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_MINUTES = 15
VERIFICATION_EMAIL_LIMIT = 3
VERIFICATION_EMAIL_WINDOW_HOURS = 1
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
COMMON_PASSWORDS = {
    "1q2w3e4r",
    "1qaz2wsx",
    "12345678",
    "123456789",
    "1234567890",
    "abcdefgh",
    "abc12345",
    "adminadmin",
    "admin123",
    "baseball",
    "changeme",
    "charlie123",
    "dragon123",
    "football",
    "freedom123",
    "hello123",
    "iloveyou",
    "letmein",
    "letmein1",
    "letmein123",
    "master123",
    "michael1",
    "monkey123",
    "mustang",
    "password",
    "password1",
    "password123",
    "passw0rd",
    "qazwsx12",
    "qwerty123",
    "qwertyuiop",
    "shadow123",
    "starwars",
    "sunshine",
    "trustno1",
    "welcome1",
    "welcome123",
    "whatever",
    "zaq12wsx",
}


@app.before_request
def enforce_account_status():
    user_id = session.get("user_id")
    if user_id is None:
        return None

    connection = connect_database(application.DATABASE)
    account = connection.execute(
        "SELECT role, suspended FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    connection.close()

    if account is None or account[1]:
        session.clear()
        return redirect(url_for("login"))

    session["user_role"] = account[0]
    return None


@app.before_request
def mark_overdue_rentals():
    connection = connect_database(application.DATABASE, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("BEGIN IMMEDIATE")
    overdue_rentals = connection.execute(
        """
        SELECT borrowing_requests.id, borrowing_requests.borrower_id,
               equipment.item_name
        FROM borrowing_requests
        JOIN equipment ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.status IN ('Approved', 'Overdue')
          AND date(borrowing_requests.end_date) < date('now', 'localtime')
        """
    ).fetchall()

    for request_id, borrower_id, item_name in overdue_rentals:
        connection.execute(
            """
            UPDATE borrowing_requests
            SET status = 'Overdue'
            WHERE id = ?
              AND status = 'Approved'
              AND date(end_date) < date('now', 'localtime')
            """,
            (request_id,),
        )
        reminder = connection.execute(
            """
            INSERT OR IGNORE INTO rental_reminders (
                borrowing_request_id, reminder_type
            )
            VALUES (?, 'rental_overdue')
            """,
            (request_id,),
        )
        if reminder.rowcount:
            add_notification(
                connection,
                borrower_id,
                f"Your rental of {item_name} is overdue. Please return it "
                "as soon as possible.",
                url_for("my_rentals"),
            )

    connection.commit()
    connection.close()


def error_response(message, status_code=400, title="Please check your request"):
    return render_template(
        "error.html",
        title=title,
        message=message,
        back_url=application.safe_back_url(),
    ), status_code


def require_admin():
    if "user_id" not in session:
        return redirect(url_for("login"))
    if session.get("user_role") != "admin":
        return error_response(
            "Administrator access is required.",
            403,
            "Access denied",
        )
    return None


def parse_amount(value, field_name):
    try:
        amount = float(value or 0)
    except ValueError:
        return None, f"{field_name} must be a valid number."

    if not math.isfinite(amount) or amount < 0:
        return None, f"{field_name} must be zero or greater."

    return amount, None


def parse_listing_time_window(price_unit, pickup_time, return_time):
    if price_unit != "hour":
        return None, None, None

    try:
        pickup = datetime.strptime(pickup_time, "%H:%M").time()
        return_by = datetime.strptime(return_time, "%H:%M").time()
    except ValueError:
        return None, None, "Choose valid pickup and return times."

    if pickup >= return_by:
        return None, None, "Return time must be later than pickup time."

    return pickup.strftime("%H:%M"), return_by.strftime("%H:%M"), None


def booking_overlaps_existing(
    start_date,
    end_date,
    start_time,
    end_time,
    price_unit,
    existing_bookings,
):
    for booking in existing_bookings:
        existing_start = datetime.strptime(booking[0], "%Y-%m-%d").date()
        existing_end = datetime.strptime(booking[1], "%Y-%m-%d").date()
        if start_date > existing_end or end_date < existing_start:
            continue

        if (
            price_unit == "hour"
            and booking[4] == "hour"
            and start_date == end_date == existing_start == existing_end
            and start_time
            and end_time
            and booking[2]
            and booking[3]
        ):
            if end_time <= booking[2] or start_time >= booking[3]:
                continue

        return True

    return False


def password_error(password):
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 128:
        return "Password must be no longer than 128 characters."
    normalized_password = "".join(
        character
        for character in password.casefold()
        if character.isalnum()
    )
    if normalized_password in COMMON_PASSWORDS:
        return "Choose a password that is not commonly used."
    return None


def add_notification(connection, user_id, message, link):
    create_notification(connection, user_id, message, link)


def log_security_event(event, actor_id=None, request_id=None, outcome="success"):
    current_app.logger.info(
        "security_event event=%s actor_id=%s request_id=%s outcome=%s",
        event,
        actor_id if actor_id is not None else "-",
        request_id if request_id is not None else "-",
        outcome,
    )


def render_listing_date_error(template_name, field_errors, form_values, item=None):
    return render_template(
        template_name,
        today=date.today().isoformat(),
        field_errors=field_errors,
        form_values=form_values,
        item=item,
    ), 400


def store_equipment_image(upload):
    if upload is None or not upload.filename:
        return None, None

    extension = os.path.splitext(secure_filename(upload.filename))[1].lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("Use a JPG, PNG, or WebP image.")

    signature = upload.stream.read(12)
    upload.stream.seek(0)
    valid_signature = (
        extension in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff")
        or extension == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n")
        or extension == ".webp"
        and signature[:4] == b"RIFF"
        and signature[8:12] == b"WEBP"
    )
    if not valid_signature:
        raise ValueError("The selected file is not a valid JPG, PNG, or WebP image.")

    filename = f"{uuid.uuid4().hex}{extension}"
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    os.makedirs(upload_folder, exist_ok=True)
    absolute_path = os.path.join(upload_folder, filename)
    try:
        upload.save(absolute_path)
    except OSError:
        if os.path.isfile(absolute_path):
            os.remove(absolute_path)
        raise
    return f"uploads/{filename}", absolute_path


def delete_equipment_image(image_path):
    if not image_path or not image_path.startswith("uploads/"):
        return

    upload_folder = os.path.realpath(current_app.config["UPLOAD_FOLDER"])
    absolute_path = os.path.realpath(
        os.path.join(upload_folder, os.path.basename(image_path))
    )
    if os.path.commonpath((upload_folder, absolute_path)) != upload_folder:
        return
    if os.path.isfile(absolute_path):
        os.remove(absolute_path)


# =====================================================
# HOME
# =====================================================

@app.route("/")
def home():

    return render_template("index.html")


# =====================================================
# REGISTER
# =====================================================

@app.route("/register", methods=["GET", "POST"])
def register():

    if request.method == "POST":

        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        student_id = request.form["student_id"].strip()
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]
        if not email.lower().endswith("@buaa.edu.cn"):
            return error_response(
                "Registration is only available to Beihang University students."
            )
        if password != confirm_password:
            return error_response("Passwords do not match.")
        password_validation_error = password_error(password)
        if password_validation_error:
            return error_response(password_validation_error)

        hashed_password = generate_password_hash(password)

        connection = connect_database(application.DATABASE)

        try:

            existing_user = connection.execute(
                """
                SELECT 1 FROM users
                WHERE LOWER(email) = ? OR student_id = ?
                """,
                (email, student_id),
            ).fetchone()
            if existing_user:
                connection.close()
                return error_response("Email or Student ID already exists.", 409)

            inserted_user = connection.execute(
                """
                INSERT INTO users (
                    name,
                    email,
                    student_id,
                    password,
                    email_verified,
                    role
                )
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (
                    name,
                    email,
                    student_id,
                    hashed_password,
                    (
                        "admin"
                        if email in application.app.config["ADMIN_EMAILS"]
                        else "user"
                    ),
                )
            )
            user_id = inserted_user.lastrowid
            connection.commit()

        except sqlite3.IntegrityError:

            connection.close()

            return error_response("Email or Student ID already exists.", 409)

        connection.close()

        verification_token = serializer.dumps(
            {"user_id": user_id, "email": email},
            salt="email-verification-salt",
        )
        verification_url = url_for(
            "verify_email",
            token=verification_token,
            _external=True,
        )
        verification_sent = True
        try:
            send_verification_email(email, verification_url)
        except (OSError, RuntimeError, smtplib.SMTPException):
            current_app.logger.error(
                "Could not send an email verification message"
            )
            verification_sent = False

        return redirect(
            url_for(
                "login",
                verification="sent" if verification_sent else "failed",
            )
        )

    return render_template("register.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        ip_address = request.remote_addr or "unknown"

        connection = connect_database(application.DATABASE)
        connection.execute(
            """
            DELETE FROM login_attempts
            WHERE attempted_at <= datetime('now', ?)
            """,
            (f"-{LOGIN_ATTEMPT_WINDOW_MINUTES} minutes",),
        )
        recent_attempts = connection.execute(
            """
            SELECT COUNT(*)
            FROM login_attempts
            WHERE email = ?
                AND attempted_at > datetime('now', ?)
            """,
            (
                email,
                f"-{LOGIN_ATTEMPT_WINDOW_MINUTES} minutes",
            ),
        ).fetchone()[0]
        if recent_attempts >= LOGIN_ATTEMPT_LIMIT:
            connection.commit()
            connection.close()
            log_security_event("login_failed", outcome="rate_limited")
            return error_response(
                "Too many unsuccessful sign-in attempts. Try again in 15 minutes.",
                429,
                "Sign-in temporarily limited",
            )

        user = connection.execute(
            """
            SELECT id, name, password, email_verified, role, suspended
            FROM users
            WHERE LOWER(email) = ?
            """,
            (email,)
        ).fetchone()

        if user and check_password_hash(user[2], password):
            if not user[3]:
                connection.commit()
                connection.close()
                log_security_event(
                    "login_failed",
                    actor_id=user[0],
                    outcome="email_unverified",
                )
                return error_response(
                    "Verify your email before signing in. You can request a new "
                    "verification link from the login page.",
                    403,
                    "Email verification required",
                )
            if user[5]:
                connection.commit()
                connection.close()
                log_security_event(
                    "login_failed",
                    actor_id=user[0],
                    outcome="account_suspended",
                )
                return error_response(
                    "This account has been suspended. Contact CampusShare support.",
                    403,
                    "Account suspended",
                )

            connection.execute(
                "DELETE FROM login_attempts WHERE email = ?",
                (email,),
            )
            connection.commit()
            connection.close()
            session.permanent = True
            session["user_id"] = user[0]
            session["user_name"] = user[1]
            session["user_role"] = user[4]

            return redirect(
                url_for("dashboard")
            )

        connection.execute(
            """
            INSERT INTO login_attempts (email, ip_address)
            VALUES (?, ?)
            """,
            (email, ip_address),
        )
        connection.commit()
        connection.close()
        log_security_event("login_failed", outcome="invalid_credentials")
        return error_response(
            "Invalid email or password.", 401, "Sign-in failed"
        )

    return render_template("login.html")


@app.route("/resend-verification", methods=["GET", "POST"])
def resend_verification():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        ip_address = request.remote_addr or "unknown"
        connection = connect_database(application.DATABASE)
        connection.execute(
            """
            DELETE FROM verification_email_attempts
            WHERE attempted_at <= datetime('now', ?)
            """,
            (f"-{VERIFICATION_EMAIL_WINDOW_HOURS} hours",),
        )
        recent_attempts = connection.execute(
            """
            SELECT COUNT(*)
            FROM verification_email_attempts
            WHERE email = ? AND ip_address = ?
                AND attempted_at > datetime('now', ?)
            """,
            (
                email,
                ip_address,
                f"-{VERIFICATION_EMAIL_WINDOW_HOURS} hours",
            ),
        ).fetchone()[0]
        user = connection.execute(
            """
            SELECT id, email
            FROM users
            WHERE LOWER(email) = ? AND email_verified = 0
            """,
            (email,),
        ).fetchone()
        if recent_attempts < VERIFICATION_EMAIL_LIMIT:
            connection.execute(
                """
                INSERT INTO verification_email_attempts (email, ip_address)
                VALUES (?, ?)
                """,
                (email, ip_address),
            )
        connection.commit()
        connection.close()

        if user and recent_attempts < VERIFICATION_EMAIL_LIMIT:
            token = serializer.dumps(
                {"user_id": user[0], "email": user[1]},
                salt="email-verification-salt",
            )
            verification_url = url_for(
                "verify_email",
                token=token,
                _external=True,
            )
            try:
                send_verification_email(user[1], verification_url)
            except (OSError, RuntimeError, smtplib.SMTPException):
                current_app.logger.error(
                    "Could not resend an email verification message"
                )

        return render_template("verification_sent.html")

    return render_template("resend_verification.html")


@app.route("/verify-email/<token>", methods=["GET", "POST"])
def verify_email(token):
    try:
        verification_data = serializer.loads(
            token,
            salt="email-verification-salt",
            max_age=86400,
        )
    except (BadSignature, SignatureExpired):
        return error_response(
            "This verification link is invalid or has expired. Request a new one.",
            400,
            "Verification link unavailable",
        )

    if not isinstance(verification_data, dict):
        return error_response("Invalid verification link.", 400)
    user_id = verification_data.get("user_id")
    email = verification_data.get("email")
    if not isinstance(user_id, int) or not isinstance(email, str):
        return error_response("Invalid verification link.", 400)

    connection = connect_database(application.DATABASE)
    user = connection.execute(
        "SELECT email_verified FROM users WHERE id = ? AND LOWER(email) = ?",
        (user_id, email.lower()),
    ).fetchone()
    if user is None:
        connection.close()
        return error_response("This account could not be found.", 404)

    if request.method == "POST":
        connection.execute(
            "UPDATE users SET email_verified = 1 WHERE id = ?",
            (user_id,),
        )
        connection.commit()
    connection.close()

    if request.method == "POST":
        return redirect(url_for("login", verified="1"))

    return render_template("confirm_email.html", token=token)


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout", methods=["POST"])
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


@app.route("/admin")
def admin_dashboard():
    access_error = require_admin()
    if access_error is not None:
        return access_error

    connection = connect_database(application.DATABASE)
    users = connection.execute(
        """
        SELECT id, name, role, suspended
        FROM users
        ORDER BY name, id
        """
    ).fetchall()
    listings = connection.execute(
        """
        SELECT equipment.id, equipment.item_name, users.name
        FROM equipment
        JOIN users ON equipment.owner_id = users.id
        WHERE equipment.is_removed = 0
        ORDER BY equipment.id DESC
        """
    ).fetchall()
    disputes = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            equipment.item_name,
            borrower.name,
            owner.name,
            borrowing_requests.damage_report,
            borrowing_requests.damage_dispute_reason,
            borrowing_requests.damage_charge,
            equipment.deposit
        FROM borrowing_requests
        JOIN equipment ON borrowing_requests.equipment_id = equipment.id
        JOIN users AS borrower ON borrowing_requests.borrower_id = borrower.id
        JOIN users AS owner ON equipment.owner_id = owner.id
        WHERE borrowing_requests.damage_dispute_status = 'Pending'
        ORDER BY borrowing_requests.damage_reported_at
        """
    ).fetchall()
    connection.close()

    return render_template(
        "admin_dashboard.html",
        users=users,
        listings=listings,
        disputes=disputes,
    )


@app.route("/admin/listings/<int:equipment_id>/remove", methods=["POST"])
def admin_remove_listing(equipment_id):
    access_error = require_admin()
    if access_error is not None:
        return access_error

    connection = connect_database(application.DATABASE)
    cursor = connection.execute(
        """
        UPDATE equipment
        SET is_removed = 1
        WHERE id = ? AND is_removed = 0
        """,
        (equipment_id,),
    )
    connection.commit()
    connection.close()
    if cursor.rowcount != 1:
        return error_response("Listing not found or already removed.", 404)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/suspension", methods=["POST"])
def admin_update_suspension(user_id):
    access_error = require_admin()
    if access_error is not None:
        return access_error

    action = request.form.get("action", "")
    if action not in {"suspend", "restore"}:
        return error_response("Choose a valid account action.")

    connection = connect_database(application.DATABASE)
    account = connection.execute(
        "SELECT role FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if account is None:
        connection.close()
        return error_response("Account not found.", 404)
    if user_id == session["user_id"] or account[0] == "admin":
        connection.close()
        return error_response(
            "Administrators cannot suspend or restore administrator accounts.",
            403,
        )

    suspended = 1 if action == "suspend" else 0
    connection.execute(
        "UPDATE users SET suspended = ? WHERE id = ?",
        (suspended, user_id),
    )
    connection.commit()
    connection.close()
    return redirect(url_for("admin_dashboard"))


# =====================================================
# DASHBOARD
# =====================================================

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    total_equipment = connection.execute(
        """
        SELECT COUNT(*)
        FROM equipment
        WHERE equipment.is_removed = 0
        AND (
    equipment.available_until IS NULL
    OR date(equipment.available_until) >= date('now', 'localtime')
)
        AND NOT EXISTS (
            SELECT 1
            FROM borrowing_requests
            WHERE borrowing_requests.equipment_id = equipment.id
            AND borrowing_requests.status IN ('Pending', 'Approved', 'Overdue')
        )
        """
    ).fetchone()[0]

    my_listings = connection.execute(
        """
        SELECT COUNT(*)
        FROM equipment
        WHERE owner_id = ?
          AND is_removed = 0
        """,
        (session["user_id"],)
    ).fetchone()[0]

    my_borrowings = connection.execute(
        """
        SELECT COUNT(*)
        FROM borrowing_requests
        WHERE borrower_id = ?
        AND status IN ('Approved', 'Overdue')
        """,
        (session["user_id"],)
    ).fetchone()[0]

    my_rating = connection.execute(
        """
        SELECT ROUND(AVG(rating), 1)
        FROM ratings
        WHERE rated_user_id = ?
        """,
        (session["user_id"],)
    ).fetchone()[0]

    connection.close()

    return render_template(
        "dashboard.html",
        user_name=session["user_name"],
        total_equipment=total_equipment,
        my_listings=my_listings,
        my_borrowings=my_borrowings,
        my_rating=my_rating
    )

# =====================================================
# LIST EQUIPMENT
# =====================================================

@app.route("/list-item", methods=["GET", "POST"])
def list_item():

    if "user_id" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":

        item_name = request.form.get("item_name", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        condition = request.form.get("condition", "").strip()
        sharing_type = request.form.get("sharing_type", "").strip()
        price_unit = request.form.get("price_unit", "day")
        if price_unit not in {"day", "hour"}:
            return error_response("Choose a valid rental pricing unit.")
        pickup_time_start, return_time_end, time_error = parse_listing_time_window(
            price_unit,
            request.form.get("pickup_time_start", ""),
            request.form.get("return_time_end", ""),
        )
        if time_error:
            return error_response(time_error)

        price, price_error = parse_amount(
            request.form.get("price", ""), "Price"
        )
        deposit, deposit_error = parse_amount(
            request.form.get("deposit", ""), "Deposit"
        )
        if price_error or deposit_error:
            return error_response(price_error or deposit_error)
        if sharing_type == "Paid" and price <= 0:
            return error_response(
                "Paid rentals must have a price greater than zero."
            )

        available_from = request.form.get("available_from", "")
        available_until = request.form.get("available_until", "")
        location = request.form.get("location", "").strip()

        try:
            available_from_date = datetime.strptime(
                available_from, "%Y-%m-%d"
            ).date()
        except ValueError:
            return render_listing_date_error(
                "list_item.html",
                {"available_from": "Enter a valid start date."},
                request.form,
            )
        try:
            available_until_date = datetime.strptime(
                available_until, "%Y-%m-%d"
            ).date()
        except ValueError:
            return render_listing_date_error(
                "list_item.html",
                {"available_until": "Enter a valid end date."},
                request.form,
            )

        if not all((item_name.strip(), category.strip(), description.strip(),
                    condition.strip(), sharing_type.strip(), location.strip())):
            return error_response("Complete all required listing fields.")
        if available_from_date < date.today():
            return render_listing_date_error(
                "list_item.html",
                {"available_from": "Choose today or a future date."},
                request.form,
            )
        if available_until_date < available_from_date:
            return render_listing_date_error(
                "list_item.html",
                {
                    "available_until":
                        "Available Until must be on or after Available From."
                },
                request.form,
            )

        try:
            image_path, saved_image_path = store_equipment_image(
                request.files.get("image")
            )
        except ValueError as error:
            return error_response(str(error))

        connection = connect_database(application.DATABASE)

        try:
            connection.execute(
                """
                INSERT INTO equipment (
                    owner_id,
                    item_name,
                    category,
                    description,
                    condition,
                    sharing_type,
                    price,
                    deposit,
                    available_from,
                    available_until,
                    location,
                    image_path,
                    price_unit,
                    pickup_time_start,
                    return_time_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["user_id"],
                    item_name,
                    category,
                    description,
                    condition,
                    sharing_type,
                    price,
                    deposit,
                    available_from,
                    available_until,
                    location,
                    image_path,
                    price_unit,
                    pickup_time_start,
                    return_time_end,
                )
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            connection.close()
            if saved_image_path:
                delete_equipment_image(image_path)
            raise
        else:
            connection.close()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "list_item.html",
        today=date.today().isoformat(),
        field_errors={},
        form_values={},
    )


# =====================================================
# BROWSE EQUIPMENT
# =====================================================

@app.route("/browse")
def browse():

    search = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    page_size = 24

    connection = connect_database(application.DATABASE)

    categories = connection.execute(
        """
        SELECT DISTINCT category
        FROM equipment
        WHERE is_removed = 0
        AND category IS NOT NULL
        AND category != ''
        ORDER BY category
        """
    ).fetchall()

    search_pattern = f"%{search}%"

    query = """
        WITH equipment_with_status AS (
            SELECT
                equipment.id,
                equipment.item_name,
                equipment.category,
                equipment.description,
                equipment.condition,
                equipment.sharing_type,
                equipment.price,
                equipment.deposit,
                equipment.available_from,
                equipment.available_until,
                equipment.location,
                users.name,
             CASE
    WHEN equipment.available_until IS NOT NULL
         AND date(equipment.available_until) < date('now', 'localtime')
    THEN 'Expired'

    WHEN EXISTS (
        SELECT 1
        FROM borrowing_requests
        WHERE borrowing_requests.equipment_id = equipment.id
        AND borrowing_requests.status IN ('Approved', 'Overdue')
    )
    THEN 'Booked'
                    WHEN EXISTS (
                        SELECT 1
                        FROM borrowing_requests
                        WHERE borrowing_requests.equipment_id = equipment.id
                        AND borrowing_requests.status = 'Pending'
                    )
                    THEN 'Pending'
                    ELSE 'Available'
                END AS equipment_status,
                equipment.image_path,
                equipment.price_unit
            FROM equipment
            JOIN users ON equipment.owner_id = users.id
            WHERE equipment.is_removed = 0
        )
        SELECT *, COUNT(*) OVER() AS total_count
        FROM equipment_with_status
        WHERE
            (? = '' OR item_name LIKE ? OR category LIKE ? OR description LIKE ?)
            AND (? = '' OR category = ?)
            AND (? = '' OR equipment_status = ?)
        ORDER BY id DESC
        LIMIT ? OFFSET ?
    """
    query_parameters = (
        search,
        search_pattern,
        search_pattern,
        search_pattern,
        category,
        category,
        status,
        status,
    )
    equipment = connection.execute(
        query,
        query_parameters + (page_size, (page - 1) * page_size)
    ).fetchall()

    connection.close()
    total_count = equipment[0][15] if equipment else 0
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if page > total_pages:
        return redirect(
            url_for(
                "browse",
                q=search,
                category=category,
                status=status,
                page=total_pages,
            )
        )

    return render_template(
        "browse.html",
        equipment=equipment,
        search=search,
        category=category,
        categories=categories,
        status=status,
        page=page,
        total_pages=total_pages
    )
# EQUIPMENT DETAILS
def render_equipment_details(
    equipment_id,
    field_errors=None,
    form_values=None,
    status_code=400,
):
    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT
            equipment.id,
            equipment.owner_id,
            equipment.item_name,
            equipment.category,
            equipment.description,
            equipment.condition,
            equipment.sharing_type,
            equipment.price,
            equipment.deposit,
            equipment.available_from,
            equipment.available_until,
            equipment.location,
            equipment.image_path,
            equipment.price_unit,
            users.name,
            equipment.pickup_time_start,
            equipment.return_time_end
        FROM equipment
        JOIN users
        ON equipment.owner_id = users.id
        WHERE equipment.id = ?
          AND equipment.is_removed = 0
        """,
        (equipment_id,)
    ).fetchone()

    if item is None:
        connection.close()
        return error_response("Equipment not found.", 404, "Equipment unavailable")

    active_request = connection.execute(
        """
        SELECT status
        FROM borrowing_requests
        WHERE equipment_id = ?
        AND status IN ('Pending', 'Approved', 'Overdue')
        LIMIT 1
        """,
        (equipment_id,)
    ).fetchone()

    connection.close()

    if active_request:
        if active_request[0] in {"Approved", "Overdue"}:
            equipment_status = "Booked"
        else:
            equipment_status = "Pending"
    else:
        equipment_status = "Available"

    response = render_template(
        "item_details.html",
        item=item,
        equipment_status=equipment_status,
        today=date.today().isoformat(),
        field_errors=field_errors or {},
        form_values=form_values or {},
    )
    if field_errors:
        return response, status_code
    return response


@app.route("/equipment/<int:equipment_id>")
def equipment_details(equipment_id):
    return render_equipment_details(equipment_id)


# =====================================================
# REQUEST EQUIPMENT
# =====================================================
@app.route(
    "/request-equipment/<int:equipment_id>",
    methods=["POST"]
)
def request_equipment(equipment_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    start_date = request.form.get("start_date", "")
    end_date = request.form.get("end_date", "")
    start_time = None
    end_time = None

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        return render_equipment_details(
            equipment_id,
            {"start_date": "Choose a valid start date."},
            request.form,
        )
    try:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return render_equipment_details(
            equipment_id,
            {"end_date": "Choose a valid end date."},
            request.form,
        )

    if end < start:
        return render_equipment_details(
            equipment_id,
            {"end_date": "End date must be on or after the start date."},
            request.form,
        )

    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT *
        FROM equipment
        WHERE id = ?
          AND is_removed = 0
        """,
        (equipment_id,)
    ).fetchone()

    if item is None:
        connection.close()
        return error_response("Equipment not found.", 404, "Equipment unavailable")

    # Owner cannot request own equipment
    if item[1] == session["user_id"]:
        connection.close()
        return error_response("You cannot request your own equipment.", 403)

    price_unit = item[13]
    if price_unit == "hour":
        try:
            start_time = datetime.strptime(
                request.form.get("start_time", ""),
                "%H:%M",
            ).time()
        except ValueError:
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"start_time": "Choose a valid start time."},
                request.form,
            )
        try:
            end_time = datetime.strptime(
                request.form.get("end_time", ""),
                "%H:%M",
            ).time()
        except ValueError:
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"end_time": "Choose a valid end time."},
                request.form,
            )

        if start != end:
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"end_date": "Hourly rentals must start and end on the same day."},
                request.form,
            )
        start_datetime = datetime.combine(start, start_time)
        end_datetime = datetime.combine(end, end_time)
        if end_datetime <= start_datetime:
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"end_time": "End time must be after the start time."},
                request.form,
            )
        pickup_time_start = item[14]
        return_time_end = item[15]
        if (
            pickup_time_start
            and return_time_end
            and (
                start_time.strftime("%H:%M") < pickup_time_start
                or end_time.strftime("%H:%M") > return_time_end
            )
        ):
            connection.close()
            return render_equipment_details(
                equipment_id,
                {
                    "start_time":
                        "Choose times within the owner's pickup and return window.",
                    "end_time":
                        "Choose times within the owner's pickup and return window.",
                },
                request.form,
            )
        if start_datetime < datetime.now():
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"start_time": "Choose a future start time."},
                request.form,
            )
        billable_hours = max(
            1,
            math.ceil((end_datetime - start_datetime).total_seconds() / 3600),
        )
    else:
        if start < date.today():
            connection.close()
            return render_equipment_details(
                equipment_id,
                {"start_date": "Choose today or a future date."},
                request.form,
            )
        billable_hours = None

    # Dates must be inside equipment availability
    if start_date < item[9] or end_date > item[10]:
        connection.close()
        errors = {}
        if start_date < item[9]:
            errors["start_date"] = (
                "Choose a date on or after the equipment's available-from date."
            )
        if end_date > item[10]:
            errors["end_date"] = (
                "Choose a date on or before the equipment's available-until date."
            )
        return render_equipment_details(
            equipment_id,
            errors,
            request.form,
        )

    connection.execute("BEGIN IMMEDIATE")
    existing_bookings = connection.execute(
        """
        SELECT start_date, end_date, start_time, end_time, price_unit
        FROM borrowing_requests
        WHERE equipment_id = ?
        AND start_date <= ?
        AND end_date >= ?
        AND status IN ('Pending', 'Approved', 'Overdue')
        """,
        (equipment_id, end_date, start_date),
    ).fetchall()
    if booking_overlaps_existing(
        start,
        end,
        start_time.strftime("%H:%M") if start_time else None,
        end_time.strftime("%H:%M") if end_time else None,
        price_unit,
        existing_bookings,
    ):
        connection.close()
        overlap_message = (
            "These dates or times overlap another pending or approved booking. "
            "Choose a different time."
        )
        return render_equipment_details(
            equipment_id,
            {
                "start_date": overlap_message,
                "end_date": overlap_message,
            },
            request.form,
            status_code=409,
        )



    if item[6] == "Paid" and price_unit == "hour":
        total_price = billable_hours * float(item[7])
    elif item[6] == "Paid":
        rental_days = (end - start).days + 1
        total_price = rental_days * float(item[7])
    else:
        total_price = 0

    # Create new request
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
        total_price
)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

            """,
            (

        equipment_id,
        session["user_id"],
        start_date,
        end_date,
        start_time.strftime("%H:%M") if start_time else None,
        end_time.strftime("%H:%M") if end_time else None,
        price_unit,
        float(item[7] or 0),
        "Pending",
        total_price
        )
    )
    add_notification(
        connection,
        item[1],
        f"New borrowing request for {item[2]}.",
        url_for("owner_requests"),
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for("dashboard")
    )


# =====================================================
# APPROVE REQUEST
# =====================================================


# =====================================================
# CONFIRM EQUIPMENT HANDOVER
# =====================================================

@app.route(
    "/requests/<int:request_id>/approve",
    methods=["POST"]
)
def approve_request(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    request_data = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            equipment.item_name

        FROM borrowing_requests

        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
          AND equipment.owner_id = ?
          AND borrowing_requests.status = 'Pending'
          AND borrowing_requests.payment_status = 'Paid'
          AND borrowing_requests.meetup_status = 'Confirmed'
          AND borrowing_requests.meetup_owner_verified_at IS NOT NULL
          AND borrowing_requests.meetup_borrower_verified_at IS NOT NULL
          AND borrowing_requests.inspection_rejected_at IS NULL
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()

    if request_data is None:
        connection.close()

        return error_response(
            "Handover requires both inspection approvals "
            "and completed demo payment. Only the equipment "
            "owner can confirm handover.",
            409
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests

        SET status = 'Approved'

        WHERE id = ?
          AND status = 'Pending'
          AND payment_status = 'Paid'
          AND meetup_status = 'Confirmed'
          AND meetup_owner_verified_at IS NOT NULL
          AND meetup_borrower_verified_at IS NOT NULL
          AND inspection_rejected_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM equipment
              WHERE equipment.id = borrowing_requests.equipment_id
                AND equipment.owner_id = ?
          )
        """,
        (
            request_id,
            session["user_id"]
        )
    )

    if cursor.rowcount != 1:
        connection.close()

        return error_response(
            "This rental is not eligible for handover.",
            409
        )

    add_notification(
        connection,
        request_data[1],
        f"The owner confirmed the equipment handover "
        f"for {request_data[2]}.",
        url_for("my_rentals")
    )

    connection.commit()
    connection.close()

    log_security_event(
        "rental_handover_confirmed",
        actor_id=session["user_id"],
        request_id=request_id
    )

    return redirect(url_for("owner_requests"))


@app.route(
    "/requests/<int:request_id>/schedule-meetup",
    methods=["POST"]
)
def schedule_meetup(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    meetup_date = request.form.get("meetup_at", "").strip()
    meetup_location = request.form.get(
        "meetup_location", ""
    ).strip()

    # Check meetup date and time
    try:
        meetup_datetime = datetime.strptime(
            meetup_date, "%Y-%m-%dT%H:%M"
        )
    except ValueError:
        return error_response(
            "Please choose a valid meetup date and time."
        )

    if meetup_datetime <= datetime.now():
        return error_response(
            "The meetup must be scheduled for a future time."
        )

    # Check meetup location
    if not meetup_location or len(meetup_location) > 200:
        return error_response(
            "Enter a meetup location of 1 to 200 characters."
        )

    connection = connect_database(application.DATABASE)

    try:
        connection.execute("BEGIN IMMEDIATE")

        rental = connection.execute(
            """
            SELECT
                borrowing_requests.borrower_id,
                equipment.item_name
            FROM borrowing_requests
            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id
            WHERE borrowing_requests.id = ?
              AND equipment.owner_id = ?
              AND borrowing_requests.status = 'Pending'
              AND borrowing_requests.meetup_status
                  IN ('Not Scheduled', 'Proposed')
            """,
            (request_id, session["user_id"])
        ).fetchone()

        if rental is None:
            return error_response(
                "This request cannot be scheduled.",
                409
            )

        connection.execute(
            """
            UPDATE borrowing_requests
            SET meetup_at = ?,
                meetup_location = ?,
                meetup_status = 'Proposed',
                meetup_proposed_at = CURRENT_TIMESTAMP,
                meetup_confirmed_at = NULL,
                meetup_owner_verified_at = NULL,
                meetup_borrower_verified_at = NULL
            WHERE id = ?
              AND status = 'Pending'
            """,
            (
                meetup_datetime.isoformat(timespec="minutes"),
                meetup_location,
                request_id
            )
        )

        add_notification(
            connection,
            rental[0],
            f"The owner has proposed a meetup for "
            f"{rental[1]}. Please review the details.",
            url_for("my_rentals")
        )

        connection.commit()

    finally:
        connection.close()

    return redirect(url_for("owner_requests"))


@app.route(
    "/requests/<int:request_id>/confirm-meetup",
    methods=["POST"]
)
def confirm_meetup(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    try:
        connection.execute("BEGIN IMMEDIATE")

        rental = connection.execute(
            """
            SELECT
                borrowing_requests.borrower_id,
                equipment.owner_id,
                equipment.item_name,
                borrowing_requests.meetup_at
            FROM borrowing_requests
            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id
            WHERE borrowing_requests.id = ?
              AND borrowing_requests.borrower_id = ?
              AND borrowing_requests.status = 'Pending'
              AND borrowing_requests.meetup_status = 'Proposed'
            """,
            (request_id, session["user_id"])
        ).fetchone()

        if rental is None:
            return error_response(
                "No proposed meetup is available for confirmation.",
                409
            )

        if (
            not rental[3]
            or datetime.fromisoformat(rental[3]) <= datetime.now()
        ):
            return error_response(
                "This meetup time has passed. Ask the owner to reschedule.",
                409
            )

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET meetup_status = 'Confirmed',
                meetup_confirmed_at = CURRENT_TIMESTAMP
            WHERE id = ?
              AND borrower_id = ?
              AND status = 'Pending'
              AND meetup_status = 'Proposed'
            """,
            (request_id, session["user_id"])
        )

        if cursor.rowcount != 1:
            return error_response(
                "This meetup can no longer be confirmed.",
                409
            )

        add_notification(
            connection,
            rental[1],
            f"The borrower has confirmed the meetup "
            f"for {rental[2]}.",
            url_for("owner_requests")
        )

        connection.commit()

    finally:
        connection.close()

    return redirect(url_for("my_rentals"))


# =====================================================
# UPLOAD EQUIPMENT PICKUP INSPECTION PHOTO
# =====================================================

@app.route(
    "/rentals/<int:request_id>/inspection-photo",
    methods=["POST"]
)
def upload_pickup_inspection_photo(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    photo = request.files.get("inspection_photo")

    if photo is None or not photo.filename:
        return error_response("Please select a photo.", 400)

    # Limit uploads to 5 MB
    max_size = 5 * 1024 * 1024
    photo_bytes = photo.read(max_size + 1)

    if len(photo_bytes) > max_size:
        return error_response(
            "Photo must be 5 MB or smaller.", 400
        )

    # Check the image and remove original metadata
    try:
        with Image.open(io.BytesIO(photo_bytes)) as original:

            if original.format not in {"JPEG", "PNG", "WEBP"}:
                return error_response(
                    "Use a JPG, PNG, or WebP image.", 400
                )

            if original.width * original.height > 20000000:
                return error_response(
                    "Image dimensions are too large.", 400
                )

            cleaned = ImageOps.exif_transpose(original)
            cleaned = cleaned.convert("RGB")
            cleaned.thumbnail((1600, 1600))

            output = io.BytesIO()
            cleaned.save(
                output,
                format="JPEG",
                quality=85
            )

    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError
    ):
        return error_response(
            "The selected image is invalid.", 400
        )

    connection = connect_database(application.DATABASE)
    saved_path = None

    try:
        connection.execute("BEGIN IMMEDIATE")

        # Check that this borrower has a confirmed meetup
        rental = connection.execute(
            """
            SELECT id
            FROM borrowing_requests
            WHERE id = ?
              AND borrower_id = ?
              AND status = 'Pending'

              AND meetup_status = 'Confirmed'
              AND meetup_at IS NOT NULL
              AND meetup_at <= ?
            """,

(
    request_id,
    session["user_id"],
    datetime.now().strftime("%Y-%m-%dT%H:%M")
)

        ).fetchone()


        if rental is None:
            return error_response(
                "Only the borrower with a confirmed meetup "
                "can upload pickup inspection photos.",
                403
            )

        # Allow at most five pickup photos
        photo_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM rental_condition_photos
            WHERE borrowing_request_id = ?
              AND stage = 'pickup'
            """,
            (request_id,)
        ).fetchone()[0]

        if photo_count >= 5:
            return error_response(
                "Maximum of five pickup photos allowed.",
                400
            )

        # Save outside the public static folder
        folder = os.path.join(
            current_app.instance_path,
            "inspection_photos"
        )

        os.makedirs(folder, exist_ok=True)

        filename = uuid.uuid4().hex + ".jpg"
        saved_path = os.path.join(folder, filename)

        with open(saved_path, "xb") as image_file:
            image_file.write(output.getvalue())

        # Link photo to the borrowing request
        connection.execute(
            """
            INSERT INTO rental_condition_photos
                (
                    borrowing_request_id,
                    uploaded_by,
                    stage,
                    image_path
                )
            VALUES (?, ?, 'pickup', ?)
            """,
            (
                request_id,
                session["user_id"],
                filename
            )
        )

        connection.commit()

    except Exception:
        connection.rollback()

        if saved_path and os.path.isfile(saved_path):
            os.remove(saved_path)

        raise

    finally:
        connection.close()

    return redirect(url_for("my_rentals"))

# =====================================================
# VIEW EQUIPMENT INSPECTION PHOTO SECURELY
# =====================================================

@app.route("/inspection-photos/<int:photo_id>")
def view_inspection_photo(photo_id):

    # User must be logged in
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    try:
        # Only the borrower or equipment owner can view
        # photographs belonging to their rental.
        photo = connection.execute(
            """
            SELECT rental_condition_photos.image_path
            FROM rental_condition_photos

            JOIN borrowing_requests
                ON rental_condition_photos.borrowing_request_id =
                   borrowing_requests.id

            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id

            WHERE rental_condition_photos.id = ?
              AND (
                  borrowing_requests.borrower_id = ?
                  OR equipment.owner_id = ?
              )
            """,
            (
                photo_id,
                session["user_id"],
                session["user_id"]
            )
        ).fetchone()

    finally:
        connection.close()

    if photo is None:
        return error_response(
            "Photo not found or access denied.",
            404
        )

    filename = photo[0]

    # Prevent unsafe file paths
    if (
        not filename
        or os.path.basename(filename) != filename
        or not filename.lower().endswith(".jpg")
    ):
        return error_response(
            "Invalid photo path.",
            400
        )

    folder = os.path.realpath(
        os.path.join(
            current_app.instance_path,
            "inspection_photos"
        )
    )

    photo_path = os.path.realpath(
        os.path.join(folder, filename)
    )

    if (
        os.path.commonpath((folder, photo_path)) != folder
        or not os.path.isfile(photo_path)
    ):
        return error_response(
            "Photo file not found.",
            404
        )

    response = send_file(
        photo_path,
        mimetype="image/jpeg"
    )

    response.headers["Cache-Control"] = "private, no-store"

    return response

# =====================================================
# VERIFY EQUIPMENT INSPECTION
# =====================================================

@app.route(
    "/rentals/<int:request_id>/verify-inspection",
    methods=["POST"]
)
def verify_equipment_inspection(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    connection = connect_database(application.DATABASE)

    try:
        connection.execute("BEGIN IMMEDIATE")

        rental = connection.execute(
            """
            SELECT
                br.borrower_id,
                e.owner_id
            FROM borrowing_requests AS br
            JOIN equipment AS e
                ON br.equipment_id = e.id
            WHERE br.id = ?
              AND br.status = 'Pending'
              AND br.meetup_status = 'Confirmed'
              AND br.meetup_at IS NOT NULL
              AND datetime('now', '+8 hours')
                  >= datetime(br.meetup_at)
              AND EXISTS (
                  SELECT 1
                  FROM rental_condition_photos AS p
                  WHERE p.borrowing_request_id = br.id
                    AND p.stage = 'pickup'
                    AND datetime(p.created_at, '+8 hours')
                        >= datetime(br.meetup_at)
              )
            """,
            (request_id,)
        ).fetchone()

        if rental is None:
            return error_response(
                "Inspection verification requires a confirmed "
                "meetup that has started and a new pickup photo "
                "taken and uploaded after the meetup.",
                403
            )

        borrower_id, owner_id = rental

        if user_id == borrower_id:

            connection.execute(
                """
                UPDATE borrowing_requests
                SET meetup_borrower_verified_at =
                    COALESCE(
                        meetup_borrower_verified_at,
                        CURRENT_TIMESTAMP
                    )
                WHERE id = ?
                """,
                (request_id,)
            )

            destination = "my_rentals"

        elif user_id == owner_id:

            connection.execute(
                """
                UPDATE borrowing_requests
                SET meetup_owner_verified_at =
                    COALESCE(
                        meetup_owner_verified_at,
                        CURRENT_TIMESTAMP
                    )
                WHERE id = ?
                """,
                (request_id,)
            )

            destination = "owner_requests"

        else:
            return error_response(
                "You cannot verify this inspection.",
                403
            )

        connection.commit()

    finally:
        connection.close()

    return redirect(url_for(destination))
# =====================================================
# REJECT REQUEST
# =====================================================

@app.route(
    "/requests/<int:request_id>/reject",
    methods=["POST"]
)
def reject_request(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    request_data = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            equipment.item_name

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
        AND equipment.owner_id = ?
        AND borrowing_requests.status = 'Pending'
        AND borrowing_requests.payment_status = 'Unpaid'
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()

    if request_data is None:

        connection.close()

        return error_response(
            "Pending request not found or you are not the owner.",
            404,
            "Request unavailable",
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Rejected'
        WHERE id = ?
          AND status = 'Pending'
            AND payment_status = 'Unpaid'
        """,
        (request_id,)
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("This request is no longer pending.", 409)
    add_notification(
        connection,
        request_data[1],
        f"Your request for {request_data[2]} was rejected.",
        url_for("my_rentals"),
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for("owner_requests")
    )


# =====================================================
# MY RENTALS
# =====================================================

@app.route("/my-rentals")
def my_rentals():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)
    connection.row_factory = sqlite3.Row
    rentals = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            equipment.item_name,
            owner.name,
            borrowing_requests.start_date,
            borrowing_requests.end_date,
            borrowing_requests.status,

            borrowing_requests.payment_status,
borrowing_requests.deposit_status,
borrowing_requests.total_price,
equipment.location,

CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM ratings
                    WHERE ratings.borrowing_request_id = borrowing_requests.id
                    AND ratings.rater_id = ?
                )
                THEN 1
                ELSE 0
            END AS has_rated,
equipment.deposit,
borrowing_requests.damage_status,
borrowing_requests.damage_report,
borrowing_requests.damage_charge,
borrowing_requests.late_fee,
borrowing_requests.late_fee_status,
CASE
    WHEN borrowing_requests.status IN ('Approved', 'Overdue')
    THEN CAST(
        julianday(borrowing_requests.end_date)
        - julianday(date('now', 'localtime'))
        AS INTEGER
    )
    ELSE NULL
END AS days_until_return,
            borrowing_requests.start_time,
borrowing_requests.end_time,
borrowing_requests.damage_dispute_status,
borrowing_requests.damage_dispute_reason,
borrowing_requests.damage_dispute_resolution,
borrowing_requests.damage_reported_at,
datetime(borrowing_requests.damage_reported_at, '+48 hours')
    AS damage_dispute_deadline,
CASE
    WHEN datetime('now') < datetime(
        borrowing_requests.damage_reported_at, '+48 hours'
    )
    THEN 1
    ELSE 0
END AS damage_dispute_window_open,
borrowing_requests.meetup_status,
borrowing_requests.meetup_at,
borrowing_requests.meetup_location,

equipment.image_path,
borrowing_requests.meetup_owner_verified_at,
borrowing_requests.meetup_borrower_verified_at,
borrowing_requests.inspection_rejected_at

FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users AS owner
        ON equipment.owner_id = owner.id

        WHERE borrowing_requests.borrower_id = ?

        ORDER BY borrowing_requests.id DESC
        """,
        (session["user_id"], session["user_id"])
    ).fetchall()


    # =============================================
    # GET INSPECTION PHOTOS FOR MY RENTALS
    # =============================================

    photos = connection.execute(
        """
        SELECT
            p.id,
            p.borrowing_request_id,
            p.stage,
            p.created_at
        FROM rental_condition_photos AS p

        JOIN borrowing_requests AS br
            ON p.borrowing_request_id = br.id

        WHERE br.borrower_id = ?

        ORDER BY p.created_at ASC, p.id ASC
        """,
        (session["user_id"],)
    ).fetchall()

    # Group photos by rental request
    photos_by_rental = {}

    for photo in photos:
        request_id = photo[1]

        if request_id not in photos_by_rental:
            photos_by_rental[request_id] = []

        photos_by_rental[request_id].append({
            "id": photo[0],
            "stage": photo[2],
            "created_at": photo[3]
        })

    connection.close()

    return render_template(
        "my_rentals.html",
        rentals=rentals,
        photos_by_rental=photos_by_rental
    )

# =====================================================
# MY LISTINGS
# =====================================================

@app.route("/my-listings")
def my_listings():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    listings = connection.execute(
        """
        SELECT
            equipment.id,
            equipment.item_name,
            equipment.category,
            equipment.description,
            equipment.condition,
            equipment.sharing_type,
            equipment.price,
            equipment.deposit,
            equipment.available_from,
            equipment.available_until,
            equipment.location,
            equipment.image_path,

            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM borrowing_requests
                    WHERE borrowing_requests.equipment_id = equipment.id
                    AND borrowing_requests.status IN ('Approved', 'Overdue')
                )
                THEN 'Booked'

                WHEN EXISTS (
                    SELECT 1
                    FROM borrowing_requests
                    WHERE borrowing_requests.equipment_id = equipment.id
                    AND borrowing_requests.status = 'Pending'
                )
                THEN 'Pending'

                ELSE 'Available'
            END AS listing_status,
            equipment.price_unit

        FROM equipment

        WHERE equipment.owner_id = ?
          AND equipment.is_removed = 0

        ORDER BY equipment.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    connection.close()

    return render_template(
        "my_listings.html",
        listings=listings
    )


# =====================================================
# EDIT LISTING
# =====================================================

@app.route(
    "/edit-listing/<int:equipment_id>",
    methods=["GET", "POST"]
)
def edit_listing(equipment_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT *
        FROM equipment
        WHERE id = ?
        AND owner_id = ?
        AND is_removed = 0
        """,
        (
            equipment_id,
            session["user_id"]
        )
    ).fetchone()

    if item is None:

        connection.close()

        return error_response(
            "Listing not found or you are not the owner.",
            404,
            "Listing unavailable",
        )

    if request.method == "POST":

        item_name = request.form.get("item_name", "").strip()
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        condition = request.form.get("condition", "").strip()
        sharing_type = request.form.get("sharing_type", "").strip()
        price_unit = request.form.get("price_unit", "day")
        if price_unit not in {"day", "hour"}:
            connection.close()
            return error_response("Choose a valid rental pricing unit.")
        pickup_time_start, return_time_end, time_error = parse_listing_time_window(
            price_unit,
            request.form.get("pickup_time_start", ""),
            request.form.get("return_time_end", ""),
        )
        if time_error:
            connection.close()
            return error_response(time_error)

        price, price_error = parse_amount(
            request.form.get("price", ""), "Price"
        )
        deposit, deposit_error = parse_amount(
            request.form.get("deposit", ""), "Deposit"
        )
        if price_error or deposit_error:
            connection.close()
            return error_response(price_error or deposit_error)
        if sharing_type == "Paid" and price <= 0:
            connection.close()
            return error_response("Paid rentals must have a price greater than zero.")

        available_from = request.form.get("available_from", "")
        available_until = request.form.get("available_until", "")
        location = request.form.get("location", "").strip()

        try:
            available_from_date = datetime.strptime(
                available_from, "%Y-%m-%d"
            ).date()
        except ValueError:
            connection.close()
            return render_listing_date_error(
                "edit_listing.html",
                {"available_from": "Enter a valid start date."},
                request.form,
                item,
            )
        try:
            available_until_date = datetime.strptime(
                available_until, "%Y-%m-%d"
            ).date()
        except ValueError:
            connection.close()
            return render_listing_date_error(
                "edit_listing.html",
                {"available_until": "Enter a valid end date."},
                request.form,
                item,
            )

        if available_from_date < date.today():
            connection.close()
            return render_listing_date_error(
                "edit_listing.html",
                {"available_from": "Choose today or a future date."},
                request.form,
                item,
            )
        if available_until_date < available_from_date:
            connection.close()
            return render_listing_date_error(
                "edit_listing.html",
                {
                    "available_until":
                        "Available Until must be on or after Available From."
                },
                request.form,
                item,
            )
        if not all((item_name, category, description, condition,
                    sharing_type, location.strip())):
            connection.close()
            return error_response("Complete all required listing fields.")

        try:
            uploaded_image_path, saved_image_path = store_equipment_image(
                request.files.get("image")
            )
        except ValueError as error:
            connection.close()
            return error_response(str(error))
        except OSError:
            connection.close()
            raise
        image_path = uploaded_image_path or item[12]
        if request.form.get("remove_image") == "1" and not uploaded_image_path:
            image_path = None

        try:
            connection.execute(
                """
                UPDATE equipment

                SET
                    item_name = ?,
                    category = ?,
                    description = ?,
                    condition = ?,
                    sharing_type = ?,
                    price = ?,
                    price_unit = ?,
                    deposit = ?,
                    available_from = ?,
                    available_until = ?,
                    location = ?,
                    image_path = ?,
                    pickup_time_start = ?,
                    return_time_end = ?

                WHERE id = ?
                AND owner_id = ?
                """,
                (
                    item_name,
                    category,
                    description,
                    condition,
                    sharing_type,
                    price,
                    price_unit,
                    deposit,
                    available_from,
                    available_until,
                    location,
                    image_path,
                    pickup_time_start,
                    return_time_end,
                    equipment_id,
                    session["user_id"]
                )
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            connection.close()
            if saved_image_path:
                delete_equipment_image(uploaded_image_path)
            raise
        else:
            connection.close()

        if image_path != item[12]:
            delete_equipment_image(item[12])

        return redirect(
            url_for("my_listings")
        )

    connection.close()

    return render_template(
        "edit_listing.html",
        item=item,
        today=date.today().isoformat(),
        field_errors={},
        form_values={},
    )


# =====================================================
# DELETE LISTING
# =====================================================

@app.route(
    "/delete-listing/<int:equipment_id>",
    methods=["POST"]
)
def delete_listing(equipment_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT id, image_path
        FROM equipment
        WHERE id = ?
        AND owner_id = ?
        AND is_removed = 0
        """,
        (
            equipment_id,
            session["user_id"]
        )
    ).fetchone()

    if item is None:

        connection.close()

        return error_response(
            "Listing not found or you are not the owner.",
            404,
            "Listing unavailable",
        )

    linked_request = connection.execute(
        "SELECT 1 FROM borrowing_requests WHERE equipment_id = ? LIMIT 1",
        (equipment_id,),
    ).fetchone()
    if linked_request:
        connection.close()
        return error_response(
            "This listing has rental history and cannot be deleted.",
            409,
            "Listing has rental history",
        )

    # Delete equipment
    connection.execute(
        """
        DELETE FROM equipment
        WHERE id = ?
        AND owner_id = ?
        """,
        (
            equipment_id,
            session["user_id"]
        )
    )

    connection.commit()
    connection.close()
    delete_equipment_image(item[1])

    return redirect(
        url_for("my_listings")
    )



@app.route(
    "/requests/<int:request_id>/complete",
    methods=["POST"]
)
def complete_request(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    request_data = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            borrowing_requests.end_date,
            borrowing_requests.payment_status,
            equipment.item_name,
            COALESCE(borrowing_requests.unit_price, equipment.price),
            borrowing_requests.price_unit,
            borrowing_requests.end_time
        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
        AND equipment.owner_id = ?
        AND borrowing_requests.status IN ('Approved', 'Overdue')
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()

    if request_data is None:
        connection.close()
        return error_response(
            "Active rental not found or you are not the owner.",
            404,
            "Rental unavailable",
        )
    if float(request_data[5] or 0) > 0 and request_data[3] != "Paid":
        connection.close()
        return error_response(
            "The rental payment must be marked paid before the item can be returned.",
            400,
        )
    agreed_return_date = datetime.strptime(
        request_data[2],
        "%Y-%m-%d"
    ).date()

    if request_data[6] == "hour":
        if not request_data[7]:
            connection.close()
            return error_response(
                "This hourly rental is missing its scheduled return time.",
                409,
                "Rental time unavailable",
            )
        agreed_return_datetime = datetime.combine(
            agreed_return_date,
            datetime.strptime(request_data[7], "%H:%M").time(),
        )
        late_seconds = (
            datetime.now() - agreed_return_datetime
        ).total_seconds()
        late_days = max(0, math.ceil(late_seconds / (24 * 60 * 60)))
    else:
        actual_return_date = datetime.now().date()
        late_days = max(0, (actual_return_date - agreed_return_date).days)

    daily_price = float(request_data[5] or 0)

    late_fee = late_days * daily_price
    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Completed',
            late_fee = ?
        WHERE id = ?
          AND status IN ('Approved', 'Overdue')
        """,
        (
            late_fee,
            request_id
        )
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("This rental is no longer active.", 409)
    add_notification(
        connection,
        request_data[1],
        f"Your rental of {request_data[4]} has been marked returned.",
        url_for("my_rentals"),
    )

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
@app.route("/rate/<int:request_id>", methods=["GET", "POST"])
def rate_owner(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            borrowing_requests.status,
            equipment.owner_id,
            owner.name,
            borrower.name

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users AS owner
        ON equipment.owner_id = owner.id

        JOIN users AS borrower
        ON borrowing_requests.borrower_id = borrower.id

        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response("Rental not found.", 404, "Rental unavailable")

    if session["user_id"] == rental[1]:
        rated_user_id = rental[3]
        rated_user_name = rental[4]
        rated_role = "Owner"
    elif session["user_id"] == rental[3]:
        rated_user_id = rental[1]
        rated_user_name = rental[5]
        rated_role = "Borrower"
    else:
        connection.close()
        return error_response("You cannot rate this rental.", 403)

    if rental[2] != "Completed":
        connection.close()
        return error_response("You can only rate a completed rental.")

    existing_rating = connection.execute(
        """
        SELECT id
        FROM ratings
        WHERE borrowing_request_id = ? AND rater_id = ?
        """,
        (request_id, session["user_id"])
    ).fetchone()

    if request.method == "POST":

        if existing_rating:
            connection.close()
            return error_response("You have already rated this rental.", 409)

        try:
            rating = int(request.form.get("rating", ""))
        except ValueError:
            connection.close()
            return error_response("Select a rating from 1 to 5.")
        comment = request.form.get("comment", "").strip()
        if rating < 1 or rating > 5:
            connection.close()
            return error_response("Select a rating from 1 to 5.")
        if len(comment) > 1000:
            connection.close()
            return error_response("Review comments must be 1,000 characters or fewer.")

        connection.execute(
            """
            INSERT INTO ratings (
                borrowing_request_id,
                rater_id,
                rated_user_id,
                rating,
                comment
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                request_id,
                session["user_id"],
                rated_user_id,
                rating,
                comment
            )
        )

        add_notification(
            connection,
            rated_user_id,
            "You received a new review.",
            url_for("my_reviews"),
        )
        connection.commit()
        connection.close()

        return redirect(url_for("my_rentals" if rated_role == "Owner" else "owner_requests"))

    connection.close()

    return render_template(
        "rate.html",
        rated_user_name=rated_user_name,
        rated_role=rated_role,
    )
@app.route("/my-reviews")
def my_reviews():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    reviews = connection.execute(
        """
        SELECT
            ratings.rating,
            ratings.comment,
            users.name,
            equipment.item_name

        FROM ratings

        JOIN users
        ON ratings.rater_id = users.id

        JOIN borrowing_requests
        ON ratings.borrowing_request_id = borrowing_requests.id

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE ratings.rated_user_id = ?

        ORDER BY ratings.id DESC
        """,
        (session["user_id"],)
    ).fetchall()
    average_rating = connection.execute(
        """
        SELECT AVG(rating), COUNT(*)
        FROM ratings
        WHERE rated_user_id = ?
        """,
        (session["user_id"],)
    ).fetchone()

    connection.close()

    return render_template(
        "my_reviews.html",
        reviews=reviews,
        average_rating=average_rating[0],
        review_count=average_rating[1],
    )
# OWNER REQUESTS
@app.route("/requests")
def owner_requests():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)


    requests_data = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            equipment.item_name,
            users.name,
            borrowing_requests.start_date,
            borrowing_requests.end_date,
            borrowing_requests.status,
            borrowing_requests.payment_status,
            borrowing_requests.deposit_status,
            equipment.deposit,
            borrowing_requests.late_fee,
            borrowing_requests.damage_status,
            borrowing_requests.damage_report,
            borrowing_requests.damage_charge,
            EXISTS (
                SELECT 1 FROM ratings
                WHERE ratings.borrowing_request_id = borrowing_requests.id
                AND ratings.rater_id = borrowing_requests.borrower_id
            ),
            EXISTS (
                SELECT 1 FROM ratings
                WHERE ratings.borrowing_request_id = borrowing_requests.id
                AND ratings.rater_id = equipment.owner_id
            ),
            borrowing_requests.start_time,
            borrowing_requests.end_time,
            borrowing_requests.damage_dispute_status,
            borrowing_requests.damage_dispute_reason,
            borrowing_requests.damage_dispute_resolution,
            borrowing_requests.damage_reported_at,
            datetime(borrowing_requests.damage_reported_at, '+48 hours')
                AS damage_dispute_deadline,
            CASE
                WHEN datetime('now') >= datetime(
                    borrowing_requests.damage_reported_at, '+48 hours'
                )
                 AND borrowing_requests.damage_dispute_status != 'Pending'
                THEN 1
                ELSE 0

END AS damage_settlement_allowed,
borrowing_requests.meetup_status,
borrowing_requests.meetup_at,

borrowing_requests.meetup_location,
borrowing_requests.meetup_owner_verified_at,
borrowing_requests.meetup_borrower_verified_at

FROM borrowing_requests

        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        JOIN users
            ON borrowing_requests.borrower_id = users.id
        WHERE equipment.owner_id = ?
        ORDER BY borrowing_requests.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    # =============================================
    # GET INSPECTION PHOTOS FOR EQUIPMENT OWNERS
    # =============================================

    photos = connection.execute(
        """
        SELECT
            p.id,
            p.borrowing_request_id,
            p.stage,
            p.created_at
        FROM rental_condition_photos AS p

        JOIN borrowing_requests AS br
            ON p.borrowing_request_id = br.id

        JOIN equipment AS e
            ON br.equipment_id = e.id

        WHERE e.owner_id = ?

        ORDER BY p.created_at ASC, p.id ASC
        """,
        (session["user_id"],)
    ).fetchall()

    photos_by_request = {}

    for photo in photos:
        request_id = photo[1]

        if request_id not in photos_by_request:
            photos_by_request[request_id] = []

        photos_by_request[request_id].append({
            "id": photo[0],
            "stage": photo[2],
            "created_at": photo[3]
        })

    connection.close()

    return render_template(
        "requests.html",
        requests_list=requests_data,
        photos_by_request=photos_by_request
    )

@app.route("/students")
def students():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    students_list = connection.execute(
        """
        SELECT
            id,
            name
        FROM users
        WHERE id != ?
        ORDER BY name
        """,
        (session["user_id"],)
    ).fetchall()

    connection.close()

    return render_template(
        "students.html",
        students=students_list
    )
# MESSAGE BORROWER
@app.route("/requests/<int:request_id>/message-borrower")
def message_request_borrower(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    owner_id = session["user_id"]

    connection = connect_database(application.DATABASE)

    try:
        borrower = connection.execute(
            """
            SELECT borrowing_requests.borrower_id
            FROM borrowing_requests
            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id
            WHERE borrowing_requests.id = ?
              AND equipment.owner_id = ?
            """,
            (request_id, owner_id)
        ).fetchone()

    finally:
        connection.close()

    if borrower is None:
        return "Borrowing request not found.", 404

    return redirect(
        url_for("chat", student_id=borrower[0])
    )
@app.route("/chat/<int:student_id>", methods=["GET", "POST"])
def chat(student_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    current_user_id = session["user_id"]

    if student_id == current_user_id:
        return redirect(url_for("students"))

    connection = connect_database(application.DATABASE, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    student = connection.execute(
        """
        SELECT id, name
        FROM users
        WHERE id = ?
        """,
        (student_id,)
    ).fetchone()

    if student is None:
        connection.close()
        return error_response("Student not found.", 404, "Student unavailable")
    connection.execute(
    """
    UPDATE messages
    SET is_read = 1
    WHERE sender_id = ?
    AND receiver_id = ?
    AND is_read = 0
    """,
    (
        student_id,
        current_user_id
    )
)

    connection.commit()

    if request.method == "POST":

        message_text = request.form["message"].strip()

        if message_text and len(message_text) <= 1000:

            connection.execute(
                """
                INSERT INTO messages (
                    sender_id,
                    receiver_id,
                    message_text
                )
                VALUES (?, ?, ?)
                """,
                (
                    current_user_id,
                    student_id,
                    message_text
                )
            )
            sender = connection.execute(
                """
                SELECT name
                FROM users
                WHERE id = ?
                """,
                (current_user_id,)
            ).fetchone()

            sender_name = sender[0] if sender else "A student"

            add_notification(
                connection,
                student_id,
                f"New message from {sender_name}",
                f"/chat/{current_user_id}",
            )
            connection.commit()

        connection.close()

        return redirect(
            url_for(
                "chat",
                student_id=student_id
            )
        )

    messages = connection.execute(
        """
        SELECT
            messages.id,
            messages.sender_id,
            messages.receiver_id,
            messages.message_text,
            messages.created_at,
            users.name

        FROM messages

        JOIN users
        ON messages.sender_id = users.id

        WHERE
            (
                messages.sender_id = ?
                AND messages.receiver_id = ?
            )
            OR
            (
                messages.sender_id = ?
                AND messages.receiver_id = ?
            )

        ORDER BY messages.id ASC
        """,
        (
            current_user_id,
            student_id,
            student_id,
            current_user_id
        )
    ).fetchall()

    connection.close()

    return render_template(
        "chat.html",
        student=student,
        messages=messages,
        current_user_id=current_user_id
    )

@app.route("/notifications")
def notifications():

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE, timeout=30)

    email_preference = connection.execute(
        """
        SELECT notification_emails_enabled
        FROM users
        WHERE id = ?
        """,
        (session["user_id"],),
    ).fetchone()
    notifications_list = connection.execute(
        """
        SELECT
            id,
            message,
            link,
            is_read,
            created_at
        FROM notifications
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    connection.close()

    return render_template(
        "notifications.html",
        notifications=notifications_list,
        notification_emails_enabled=bool(email_preference[0]),
    )


@app.route("/notifications/email-preference", methods=["POST"])
def update_notification_email_preference():
    if "user_id" not in session:
        return redirect(url_for("login"))

    enabled = 1 if request.form.get("notification_emails_enabled") == "1" else 0
    connection = connect_database(application.DATABASE, timeout=30)
    cursor = connection.execute(
        """
        UPDATE users
        SET notification_emails_enabled = ?
        WHERE id = ?
        """,
        (enabled, session["user_id"]),
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("Account not found.", 404)

    connection.commit()
    connection.close()
    return redirect(url_for("notifications"))


@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE, timeout=30)
    connection.execute(
        """
        UPDATE notifications
        SET is_read = 1
        WHERE id = ?
          AND user_id = ?
        """,
        (notification_id, session["user_id"]),
    )
    connection.commit()
    connection.close()
    return redirect(url_for("notifications"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    message = None
    if request.method == "POST":

        email = request.form["email"].strip().lower()

        connection = connect_database(application.DATABASE, timeout=30)

        user = connection.execute(
            """
            SELECT id, email
            FROM users
            WHERE LOWER(email) = ?
            """,
            (email,)
        ).fetchone()

        connection.close()

        if user:
            token = serializer.dumps(
                user[1],
                salt="password-reset-salt"
            )
            reset_url = url_for(
                "reset_password",
                token=token,
                _external=True,
            )
            try:
                send_password_reset_email(user[1], reset_url)
            except (OSError, RuntimeError, smtplib.SMTPException):
                current_app.logger.error(
                    "Could not send a password reset email."
                )

        message = (
            "For privacy, we cannot confirm whether that address has an "
            "account. If it does, check your inbox for reset instructions. "
            "If no email arrives, try again later or contact the CampusShare "
            "administrator."
        )

    return render_template(
        "forgot_password.html",
        message=message
    )
@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):

    try:
        email = serializer.loads(
            token,
            salt="password-reset-salt",
            max_age=1800
        )

    except SignatureExpired:
        return error_response(
            "This password reset link has expired.", 400, "Reset link expired"
        )

    except BadSignature:
        return error_response(
            "Invalid password reset link.", 400, "Invalid reset link"
        )

    if request.method == "POST":

        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            return error_response("Passwords do not match.")
        password_validation_error = password_error(password)
        if password_validation_error:
            return error_response(password_validation_error)

        hashed_password = generate_password_hash(password)

        connection = connect_database(application.DATABASE, timeout=30)

        connection.execute(
            """
            UPDATE users
            SET password = ?
            WHERE LOWER(email) = ?
            """,
            (
                hashed_password,
                email.lower()
            )
        )

        connection.commit()
        connection.close()

        return redirect(
            url_for("login", reset="1")
        )

    return render_template(
        "reset_password.html",
        token=token
    )
@app.route("/messages")
def messages():

    if "user_id" not in session:
        return redirect(url_for("login"))

    current_user_id = session["user_id"]

    connection = connect_database(application.DATABASE, timeout=30)

    conversations = connection.execute(
        """
        SELECT
            users.id,
            users.name,
            messages.message_text,
            messages.created_at,
            messages.sender_id,
            (
    SELECT COUNT(*)
    FROM messages AS unread
    WHERE unread.sender_id = users.id
    AND unread.receiver_id = ?
    AND unread.is_read = 0
) AS unread_count

        FROM users

        JOIN messages
        ON (
            messages.sender_id = users.id
            AND messages.receiver_id = ?
        )
        OR (
            messages.receiver_id = users.id
            AND messages.sender_id = ?
        )

        WHERE messages.id = (
            SELECT MAX(m2.id)
            FROM messages AS m2

            WHERE (
                m2.sender_id = users.id
                AND m2.receiver_id = ?
            )
            OR (
                m2.receiver_id = users.id
                AND m2.sender_id = ?
            )
        )

        ORDER BY messages.id DESC
        """,
        (
    current_user_id,
    current_user_id,
    current_user_id,
    current_user_id,
    current_user_id
)
    ).fetchall()

    connection.close()

    return render_template(
        "messages.html",
        conversations=conversations,
        current_user_id=current_user_id
    )
@app.route("/delete-message/<int:message_id>", methods=["POST"])
def delete_message(message_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    current_user_id = session["user_id"]

    connection = connect_database(application.DATABASE, timeout=30)

    message = connection.execute(
        """
        SELECT sender_id, receiver_id
        FROM messages
        WHERE id = ?
        """,
        (message_id,)
    ).fetchone()

    if message is None:
        connection.close()
        return redirect(url_for("messages"))

    # Only the sender can delete their own message
    if message[0] != current_user_id:
        connection.close()
        return error_response("You cannot delete this message.", 403)

    other_student_id = message[1]

    connection.execute(
        """
        DELETE FROM messages
        WHERE id = ?
        AND sender_id = ?
        """,
        (
            message_id,
            current_user_id
        )
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for(
            "chat",
            student_id=other_student_id
        )

    )

# =====================================================
# QR RENTAL PAYMENT
# =====================================================

@app.route("/qr-payment/<int:request_id>")
def qr_payment(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            borrowing_requests.status,
            borrowing_requests.payment_status,
            borrowing_requests.total_price,
            equipment.item_name,

users.name,
borrowing_requests.meetup_owner_verified_at,
borrowing_requests.meetup_borrower_verified_at,

borrowing_requests.inspection_rejected_at,
equipment.deposit



        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users
        ON equipment.owner_id = users.id

        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    connection.close()

    if rental is None:
        return redirect(url_for("my_rentals"))

    if rental[1] != session["user_id"]:
        return error_response("You cannot pay for this rental.", 403)


    inspection_approved = (
        rental[2] == "Pending"
        and rental[7] is not None
        and rental[8] is not None
        and rental[9] is None
    )

    existing_approved = (
        rental[2] in {"Approved", "Overdue"}
        and rental[9] is None
    )

    if not (inspection_approved or existing_approved):
        return error_response(
            "Both students must accept the equipment "
            "inspection before proceeding to payment.",
            400
        )

    if rental[3] == "Paid":
        return redirect(url_for("my_rentals"))

    token = serializer.dumps(
        {
            "request_id": rental[0],
            "borrower_id": rental[1]
        },
        salt="qr-payment-salt"
    )

    payment_url = url_for(
        "qr_payment_confirm",
        token=token,
        _external=True
    )

    return render_template(
        "qr_payment.html",
        rental=rental,
        token=token,
        payment_url=payment_url
    )


# =====================================================
# QR IMAGE
# =====================================================

@app.route("/qr-payment/image/<token>")
def qr_payment_image(token):

    try:
        serializer.loads(
            token,
            salt="qr-payment-salt",
            max_age=600
        )

    except SignatureExpired:
        return error_response(
            "QR payment code has expired.", 400, "QR code expired"
        )

    except BadSignature:
        return error_response("Invalid QR payment code.", 400)

    payment_url = url_for(
        "qr_payment_confirm",
        token=token,
        _external=True
    )

    qr = qrcode.QRCode(
        version=1,
        box_size=10,
        border=4
    )

    qr.add_data(payment_url)
    qr.make(fit=True)

    qr_image = qr.make_image()

    image_buffer = io.BytesIO()

    qr_image.save(
        image_buffer,
        format="PNG"
    )

    image_buffer.seek(0)

    return send_file(
        image_buffer,
        mimetype="image/png"
    )


# =====================================================
# READ-ONLY QR PAYMENT PREVIEW
# =====================================================

@app.route("/qr-payment/demo-preview/<int:request_id>")
def qr_demo_preview(request_id):
    import base64
    from flask import render_template_string

    borrower_id = session.get("user_id")

    if borrower_id is None:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    try:
        rental = connection.execute(
            """
            SELECT
                equipment.item_name,
                borrowing_requests.total_price,
                equipment.deposit
            FROM borrowing_requests
            JOIN equipment
                ON borrowing_requests.equipment_id = equipment.id
            WHERE borrowing_requests.id = ?
              AND borrowing_requests.borrower_id = ?
            """,
            (request_id, borrower_id)
        ).fetchone()
    finally:
        connection.close()

    if rental is None:
        return error_response("Rental not found.", 404)

    preview_url = url_for(
        "qr_demo_preview",
        request_id=request_id,
        _external=True
    )

    image_buffer = io.BytesIO()

    qr = qrcode.make(preview_url)
    qr.save(image_buffer, format="PNG")

    qr_image = base64.b64encode(
        image_buffer.getvalue()
    ).decode("ascii")

    return render_template_string(
        """
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport"
                  content="width=device-width, initial-scale=1">
            <title>CampusShare QR Payment Preview</title>
        </head>

        <body style="
            font-family: Arial;
            background: #f5f7fa;
            padding: 25px;
            text-align: center;
        ">

        <div style="
            background: white;
            max-width: 450px;
            margin: auto;
            padding: 25px;
            border-radius: 12px;
        ">

            <h2>CampusShare QR Payment Preview</h2>

            <h3>{{ item_name }}</h3>

            <p>
                <strong>Rental Fee:</strong>
                &yen;{{ "%.2f"|format(fee or 0) }}
            </p>

            <p>
                <strong>Refundable Security Deposit:</strong>
                &yen;{{ "%.2f"|format(deposit or 0) }}
            </p>

            <hr>

            <h3>Scan QR Code</h3>

            <img
                src="data:image/png;base64,{{ qr_image }}"
                alt="Demo QR Code"
                style="width: 260px; max-width: 100%;"
            >

            <p>
                Scan this QR code with your phone
                to open the preview page.
            </p>

            <p style="color: #b45309;">
                DEMO PREVIEW ONLY
            </p>

            <p style="font-size: 13px; color: #666;">
                No real payment will be made.
                This preview does not change
                the rental payment status.
            </p>

            <a href="{{ url_for('my_rentals') }}">
                Back to My Rentals
            </a>

        </div>
        </body>
        </html>
        """,
        item_name=rental[0],
        fee=rental[1],
        deposit=rental[2],
        qr_image=qr_image
    )


# =====================================================
# QR PAYMENT CONFIRMATION
# =====================================================

"""
            UPDATE borrowing_requests
            SET payment_status = 'Paid'
            WHERE id = ?
            AND borrower_id = ?
            AND payment_status = 'Unpaid'

inspection_approved = (
    rental[2] == "Pending"
    and rental[9] is not None
    and rental[10] is not None
    and rental[11] is None
)

existing_approved = (
    rental[2] in {"Approved", "Overdue"}
    and rental[11] is None
)

if not (inspection_approved or existing_approved):
    connection.close()
    return error_response(
        "Both students must accept the equipment inspection before payment.",
        400
    )


        cursor = connection.execute(
            """

@app.route("/qr-payment/confirm/<token>", methods=["GET", "POST"])
def qr_payment_confirm(token):
    try:
        payment_data = serializer.loads(
            token,
            salt="qr-payment-salt",
            max_age=600
        )
    except SignatureExpired:
        return error_response(
            "This QR payment code has expired.",
            400,
            "QR code expired"
        )
    except BadSignature:
        return error_response("Invalid QR payment code.", 400)

    request_id = payment_data.get("request_id")
    borrower_id = payment_data.get("borrower_id")

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            borrowing_requests.id,
            borrowing_requests.borrower_id,
            borrowing_requests.status,
            borrowing_requests.payment_status,
            borrowing_requests.total_price,
            equipment.item_name,
            owner.name,
            equipment.owner_id,
            borrower.name,
            borrowing_requests.meetup_owner_verified_at,
            borrowing_requests.meetup_borrower_verified_at,
            borrowing_requests.inspection_rejected_at
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        JOIN users AS owner
            ON equipment.owner_id = owner.id
        JOIN users AS borrower
            ON borrowing_requests.borrower_id = borrower.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response(
            "Rental not found.", 404, "Rental unavailable"
        )

    if rental[1] != borrower_id:
        connection.close()
        return error_response("Invalid payment request.", 403)

    if rental[3] == "Paid":
        connection.close()
        return render_template(
            "qr_payment_confirm.html",
            rental=rental,
            payment_success=True
        )

    if request.method == "POST":
        inspection_approved = (
            rental[2] == "Pending"
            and rental[9] is not None
            and rental[10] is not None
            and rental[11] is None
        )

        existing_approved = (
            rental[2] in {"Approved", "Overdue"}
            and rental[11] is None
        )

        if not (inspection_approved or existing_approved):
            connection.close()
            return error_response(
                "Both students must accept the equipment "
                "inspection before payment.",
                400
            )

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET payment_status = 'Paid'
            WHERE id = ?
              AND borrower_id = ?
              AND payment_status = 'Unpaid'
              AND inspection_rejected_at IS NULL
              AND (
                  status IN ('Approved', 'Overdue')
                  OR (
                      status = 'Pending'
                      AND meetup_status = 'Confirmed'
                      AND meetup_owner_verified_at IS NOT NULL
                      AND meetup_borrower_verified_at IS NOT NULL
                  )
              )
            """,
            (request_id, borrower_id)
        )

        if cursor.rowcount != 1:
            connection.close()
            return error_response(
                "This rental payment is no longer pending.",
                409
            )

        add_notification(
            connection,
            rental[7],
            f"{rental[8]} paid for the rental of {rental[5]}.",
            url_for("owner_requests")
        )

        connection.commit()
        connection.close()

        return render_template(
            "qr_payment_confirm.html",
            rental=rental,
            payment_success=True
        )

    connection.close()

    return render_template(
        "qr_payment_confirm.html",
        rental=rental,
        payment_success=False
    )


# =====================================================
# PAY LATE FEE
# =====================================================

@app.route("/pay-late-fee/<int:request_id>", methods=["POST"])
def pay_late_fee(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            borrower_id,
            status,
            late_fee,
            late_fee_status
        FROM borrowing_requests
        WHERE id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return redirect(url_for("my_rentals"))

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot pay this late fee.", 403
        )

    if rental[1] != "Completed":
        connection.close()
        return error_response(
            "The rental must be completed first.", 400
        )

    if float(rental[2] or 0) <= 0:
        connection.close()
        return error_response(
            "There is no late fee to pay.", 400
        )

    if rental[3] == "Paid":
        connection.close()
        return redirect(url_for("my_rentals"))

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET late_fee_status = 'Paid'
        WHERE id = ?
          AND borrower_id = ?
          AND status = 'Completed'
          AND late_fee_status = 'Unpaid'
        """,
        (request_id, session["user_id"])
    )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This late fee is no longer pending.", 409
        )

    connection.commit()
    connection.close()

    return redirect(url_for("my_rentals"))


# =====================================================
# CONFIRM DEPOSIT
# =====================================================

@app.route("/confirm-deposit/<int:request_id>", methods=["POST"])
def confirm_deposit(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            borrowing_requests.status,
            borrowing_requests.payment_status,
            borrowing_requests.deposit_status
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response(
            "Rental not found.", 404, "Rental unavailable"
        )

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot confirm this deposit.", 403
        )

    if rental[1] not in {"Approved", "Overdue"} or rental[2] != "Paid":
        connection.close()
        return error_response(
            "The rental must be approved and paid "
            "before confirming its deposit.",
            400
        )

    if rental[3] != "Pending":
        connection.close()
        return error_response(
            "This deposit is no longer pending.", 409
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Held'
        WHERE id = ?
          AND status IN ('Approved', 'Overdue')
          AND payment_status = 'Paid'
          AND deposit_status = 'Pending'
        """,
        (request_id,)
    )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This deposit is no longer pending.", 409
        )

    connection.commit()
    connection.close()

    log_security_event(
        "deposit_held",
        actor_id=session["user_id"],
        request_id=request_id
    )

    return redirect(url_for("owner_requests"))


# =====================================================
# DAMAGE STATUS
# =====================================================

@app.route(
    "/damage-status/<int:request_id>/<condition>",
    methods=["POST"]
)
def set_damage_status(request_id, condition):
    if "user_id" not in session:
        return redirect(url_for("login"))

    if condition not in {"good", "damaged"}:
        return error_response(
            "Choose a valid equipment condition.", 400
        )

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            equipment.deposit,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status,
            borrowing_requests.borrower_id
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response(
            "Rental not found.", 404, "Rental unavailable"
        )

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot update this damage report.", 403
        )

    if rental[2] != "Completed" or rental[3] != "Held":
        connection.close()
        return error_response(
            "The completed rental must have a held deposit "
            "before inspection.",
            400
        )

    if rental[4] != "Not Checked":
        connection.close()
        return error_response(
            "This rental has already been inspected.", 409
        )

    if condition == "good":
        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET damage_status = 'Good Condition',
                damage_report = '',
                damage_charge = 0
            WHERE id = ?
              AND status = 'Completed'
              AND deposit_status = 'Held'
              AND damage_status = 'Not Checked'
            """,
            (request_id,)
        )

    else:
        damage_report = request.form.get(
            "damage_report", ""
        ).strip()

        if not damage_report or len(damage_report) > 1000:
            connection.close()
            return error_response(
                "Enter a damage description of "
                "1 to 1000 characters.",
                400
            )

        damage_charge, amount_error = parse_amount(
            request.form.get("damage_charge", ""),
            "Damage charge"
        )

        if amount_error:
            connection.close()
            return error_response(amount_error)

        if damage_charge < 0:
            connection.close()
            return error_response(
                "Damage charge cannot be negative.", 400
            )

        if damage_charge > float(rental[1] or 0):
            connection.close()
            return error_response(
                "Damage charge cannot exceed the "
                "held security deposit.",
                400
            )

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET damage_status = 'Damaged',
                damage_report = ?,
                damage_charge = ?,
                damage_reported_at = CURRENT_TIMESTAMP,
                damage_dispute_status = 'Not Disputed',
                damage_dispute_reason = '',
                damage_dispute_resolution = ''
            WHERE id = ?
              AND status = 'Completed'
              AND deposit_status = 'Held'
              AND damage_status = 'Not Checked'
            """,
            (damage_report, damage_charge, request_id)
        )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This rental has already been inspected.", 409
        )

    if condition == "damaged":
        add_notification(
            connection,
            rental[5],
            "The owner submitted a damage report. "
            "Review it and dispute the proposed "
            "charge within 48 hours.",
            url_for("my_rentals")
        )

    connection.commit()
    connection.close()

    log_security_event(
        "damage_assessed",
        actor_id=session["user_id"],
        request_id=request_id,
        outcome=condition
    )

    return redirect(url_for("owner_requests"))


# =====================================================
# DISPUTE DAMAGE REPORT
# =====================================================

@app.route("/damage-dispute/<int:request_id>", methods=["POST"])
def dispute_damage_report(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    dispute_reason = request.form.get(
        "dispute_reason", ""
    ).strip()

    if not dispute_reason or len(dispute_reason) > 1000:
        return error_response(
            "Enter a dispute explanation of "
            "1 to 1000 characters.",
            400
        )

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            borrowing_requests.borrower_id,
            equipment.owner_id,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status,
            borrowing_requests.damage_dispute_status,
            borrowing_requests.damage_reported_at,
            equipment.item_name
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response(
            "Rental not found.", 404, "Rental unavailable"
        )

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot dispute this damage report.", 403
        )

    if (
        rental[2] != "Completed"
        or rental[3] != "Held"
        or rental[4] != "Damaged"
    ):
        connection.close()
        return error_response(
            "This damage report is no longer "
            "open for dispute.",
            409
        )

    if rental[5] != "Not Disputed":
        connection.close()
        return error_response(
            "This damage report has already been disputed.",
            409
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET damage_dispute_status = 'Pending',
            damage_dispute_reason = ?
        WHERE id = ?
          AND borrower_id = ?
          AND status = 'Completed'
          AND deposit_status = 'Held'
          AND damage_status = 'Damaged'
          AND damage_dispute_status = 'Not Disputed'
          AND damage_reported_at IS NOT NULL
          AND datetime('now') < datetime(
              damage_reported_at, '+48 hours'
          )
        """,
        (
            dispute_reason,
            request_id,
            session["user_id"]
        )
    )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "The 48-hour dispute window has closed.", 409
        )

    add_notification(
        connection,
        rental[1],
        f"The borrower disputed the damage report "
        f"for {rental[7]}. Review the dispute before "
        "settling the deposit.",
        url_for("owner_requests")
    )

    connection.commit()
    connection.close()

    log_security_event(
        "damage_dispute_opened",
        actor_id=session["user_id"],
        request_id=request_id
    )

    return redirect(url_for("my_rentals"))


# =====================================================
# RESOLVE DAMAGE DISPUTE
# =====================================================

@app.route(
    "/damage-dispute/<int:request_id>/resolve",
    methods=["POST"]
)
def resolve_damage_dispute(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    resolution = request.form.get("resolution", "")

    if resolution not in {"uphold", "revise", "cancel"}:
        return error_response(
            "Choose a valid dispute resolution.", 400
        )

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status,
            borrowing_requests.damage_dispute_status,
            equipment.deposit,
            borrowing_requests.borrower_id,
            equipment.item_name
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response(
            "Rental not found.", 404, "Rental unavailable"
        )

    is_administrator = (
        session.get("user_role") == "admin"
    )

    if (
        rental[0] != session["user_id"]
        and not is_administrator
    ):
        connection.close()
        return error_response(
            "You cannot resolve this damage dispute.", 403
        )

    if (
        rental[1] != "Completed"
        or rental[2] != "Held"
        or rental[3] != "Damaged"
        or rental[4] != "Pending"
    ):
        connection.close()
        return error_response(
            "There is no open damage dispute to resolve.",
            409
        )

    if resolution == "revise":
        damage_charge, amount_error = parse_amount(
            request.form.get("damage_charge", ""),
            "Damage charge"
        )

        if amount_error:
            connection.close()
            return error_response(amount_error)

        if damage_charge < 0:
            connection.close()
            return error_response(
                "Damage charge cannot be negative.", 400
            )

        if damage_charge > float(rental[5] or 0):
            connection.close()
            return error_response(
                "Damage charge cannot exceed the "
                "held security deposit.",
                400
            )

        resolution_status = "Revised"

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET damage_charge = ?,
                damage_dispute_status = ?,
                damage_dispute_resolution = ?
            WHERE id = ?
              AND status = 'Completed'
              AND deposit_status = 'Held'
              AND damage_status = 'Damaged'
              AND damage_dispute_status = 'Pending'
            """,
            (
                damage_charge,
                resolution_status,
                resolution_status,
                request_id
            )
        )

    else:
        resolution_status = (
            "Upheld"
            if resolution == "uphold"
            else "Cancelled"
        )

        if resolution == "cancel":
            cursor = connection.execute(
                """
                UPDATE borrowing_requests
                SET damage_charge = 0,
                    damage_dispute_status = ?,
                    damage_dispute_resolution = ?
                WHERE id = ?
                  AND status = 'Completed'
                  AND deposit_status = 'Held'
                  AND damage_status = 'Damaged'
                  AND damage_dispute_status = 'Pending'
                """,
                (
                    resolution_status,
                    resolution_status,
                    request_id
                )
            )

        else:
            cursor = connection.execute(
                """
                UPDATE borrowing_requests
                SET damage_dispute_status = ?,
                    damage_dispute_resolution = ?
                WHERE id = ?
                  AND status = 'Completed'
                  AND deposit_status = 'Held'
                  AND damage_status = 'Damaged'
                  AND damage_dispute_status = 'Pending'
                """,
                (
                    resolution_status,
                    resolution_status,
                    request_id
                )
            )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This damage dispute has already been resolved.",
            409
        )

    add_notification(
        connection,
        rental[6],
        f"The {'administrator' if is_administrator else 'owner'} "
        f"{resolution_status.lower()} the damage dispute for "
        f"{rental[7]}. Check My Rentals for the updated charge.",
        url_for("my_rentals")
    )

    connection.commit()
    connection.close()

    log_security_event(
        "damage_dispute_resolved",
        actor_id=session["user_id"],
        request_id=request_id,
        outcome=resolution_status.lower()
    )

    return redirect(
        url_for(
            "admin_dashboard"
            if is_administrator
            else "owner_requests"
        )
    )


# =====================================================
# REFUND DEPOSIT
# =====================================================

@app.route("/refund-deposit/<int:request_id>", methods=["POST"])
def refund_deposit(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return redirect(url_for("owner_requests"))

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot refund this deposit.", 403
        )

    if rental[1] != "Completed":
        connection.close()
        return error_response(
            "The equipment must be returned first.", 400
        )

    if rental[2] != "Held":
        connection.close()
        return error_response(
            "There is no held deposit to refund.", 400
        )

    if rental[3] != "Good Condition":
        connection.close()
        return error_response(
            "Mark the item as Good Condition "
            "before refunding the deposit.",
            400
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Refunded'
        WHERE id = ?
          AND status = 'Completed'
          AND deposit_status = 'Held'
          AND damage_status = 'Good Condition'
        """,
        (request_id,)
    )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This deposit is no longer eligible for refund.",
            409
        )

    connection.commit()
    connection.close()

    log_security_event(
        "deposit_refunded",
        actor_id=session["user_id"],
        request_id=request_id
    )

    return redirect(url_for("owner_requests"))


# =====================================================
# SETTLE DAMAGE
# =====================================================

@app.route("/settle-damage/<int:request_id>", methods=["POST"])
def settle_damage(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status,
            borrowing_requests.damage_dispute_status,
            borrowing_requests.damage_reported_at
        FROM borrowing_requests
        JOIN equipment
            ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return redirect(url_for("owner_requests"))

    if rental[0] != session["user_id"]:
        connection.close()
        return error_response(
            "You cannot settle this damage.", 403
        )

    if rental[1] != "Completed":
        connection.close()
        return error_response(
            "The equipment must be returned first.", 400
        )

    if rental[2] != "Held":
        connection.close()
        return error_response(
            "The deposit is not currently held.", 400
        )

    if rental[3] != "Damaged":
        connection.close()
        return error_response(
            "This item is not marked as damaged.", 400
        )

    if rental[4] == "Pending":
        connection.close()
        return error_response(
            "Resolve the borrower's damage dispute "
            "before settling the deposit.",
            409
        )

    dispute_window_open = (
        rental[5] is None
        or connection.execute(
            """
            SELECT datetime('now') <
                   datetime(?, '+48 hours')
            """,
            (rental[5],)
        ).fetchone()[0]
    )

    if dispute_window_open:
        connection.close()
        return error_response(
            "Wait until the borrower's 48-hour "
            "damage review window has closed.",
            409
        )

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Settled'
        WHERE id = ?
          AND status = 'Completed'
          AND deposit_status = 'Held'
          AND damage_status = 'Damaged'
          AND damage_dispute_status != 'Pending'
          AND damage_reported_at IS NOT NULL
          AND datetime('now') >=
              datetime(damage_reported_at, '+48 hours')
        """,
        (request_id,)
    )

    if cursor.rowcount != 1:
        connection.close()
        return error_response(
            "This damage settlement has already "
            "been completed.",
            409
        )

    connection.commit()
    connection.close()

    log_security_event(
        "deposit_settled_for_damage",
        actor_id=session["user_id"],
        request_id=request_id,
        outcome="damage_charge_applied"
    )

    return redirect(url_for("owner_requests"))


# =====================================================
# REJECT EQUIPMENT INSPECTION
# =====================================================

@app.route(
    "/rentals/<int:request_id>/reject-inspection",
    methods=["POST"]
)
def reject_equipment_inspection(request_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    reason = (
        request.form.get("inspection_rejection_reason") or ""
    ).strip()

    if not 10 <= len(reason) <= 500:
        return error_response(
            "Please provide a reason between "
            "10 and 500 characters.",
            400
        )

    connection = connect_database(application.DATABASE)

    try:
        connection.execute("BEGIN IMMEDIATE")

        rental = connection.execute(
            """
            SELECT
                br.borrower_id,
                equipment.owner_id,
                br.inspection_rejected_at
            FROM borrowing_requests AS br
            JOIN equipment
                ON equipment.id = br.equipment_id
            WHERE br.id = ?
              AND br.status = 'Pending'
              AND br.payment_status = 'Unpaid'
              AND br.meetup_status = 'Confirmed'
              AND br.meetup_at IS NOT NULL
              AND br.meetup_at <= ?
            """,
            (
                request_id,
                datetime.now().strftime("%Y-%m-%dT%H:%M")
            )
        ).fetchone()

        if rental is None:
            return error_response(
                "This inspection cannot be rejected "
                "at this stage.",
                409
            )

        borrower_id, owner_id, rejected_at = rental

        if user_id not in (borrower_id, owner_id):
            return error_response("Access denied.", 403)

        if rejected_at is not None:
            return error_response(
                "This inspection has already been rejected.",
                409
            )

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET inspection_rejected_by = ?,
                inspection_rejection_reason = ?,
                inspection_rejected_at = ?,
                meetup_owner_verified_at = NULL,
                meetup_borrower_verified_at = NULL,
                status = 'Rejected'
            WHERE id = ?
              AND status = 'Pending'
              AND payment_status = 'Unpaid'
              AND inspection_rejected_at IS NULL
            """,
            (
                user_id,
                reason,
                datetime.now().isoformat(timespec="seconds"),
                request_id
            )
        )

        if cursor.rowcount != 1:
            return error_response(
                "This inspection is no longer available "
                "for rejection.",
                409
            )

        connection.commit()

    finally:
        connection.close()

    if user_id == borrower_id:
        return redirect(url_for("my_rentals"))

    return redirect(url_for("owner_requests"))
