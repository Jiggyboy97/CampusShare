from datetime import date, datetime
import io
import math
import sqlite3
import smtplib

import qrcode
from flask import current_app, redirect, render_template, request, send_file, session, url_for
from itsdangerous import BadSignature, SignatureExpired
from werkzeug.security import check_password_hash, generate_password_hash

import app as application
from app import app, serializer
from database import connect_database
from email_utils import send_password_reset_email


def error_response(message, status_code=400, title="Please check your request"):
    return render_template(
        "error.html",
        title=title,
        message=message,
        back_url=request.referrer or url_for("home"),
    ), status_code


def parse_amount(value, field_name):
    try:
        amount = float(value or 0)
    except ValueError:
        return None, f"{field_name} must be a valid number."

    if not math.isfinite(amount) or amount < 0:
        return None, f"{field_name} must be zero or greater."

    return amount, None


def password_error(password):
    if len(password) < 12:
        return "Password must be at least 12 characters long."
    if len(password) > 128:
        return "Password must be no longer than 128 characters."
    return None


def add_notification(connection, user_id, message, link):
    connection.execute(
        """
        INSERT INTO notifications (user_id, message, link)
        VALUES (?, ?, ?)
        """,
        (user_id, message, link),
    )


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
        email = request.form["email"].strip()
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

            connection.execute(
                """
                INSERT INTO users (
                    name,
                    email,
                    student_id,
                    password
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    name,
                    email,
                    student_id,
                    hashed_password
                )
            )

            connection.commit()

        except sqlite3.IntegrityError:

            connection.close()

            return error_response("Email or Student ID already exists.", 409)

        connection.close()

        return redirect(url_for("login", registered="1"))

    return render_template("register.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip().lower()
        password = request.form["password"]

        connection = connect_database(application.DATABASE)

        user = connection.execute(
            """
            SELECT *
            FROM users
            WHERE LOWER(email) = ?
            """,
            (email,)
        ).fetchone()

        connection.close()

        if user and check_password_hash(
            user[4],
            password
        ):

            session["user_id"] = user[0]
            session["user_name"] = user[1]

            return redirect(
                url_for("dashboard")
            )

        return error_response(
            "Invalid email or password.", 401, "Sign-in failed"
        )

    return render_template("login.html")


# =====================================================
# LOGOUT
# =====================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


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
        WHERE NOT EXISTS (
            SELECT 1
            FROM borrowing_requests
            WHERE borrowing_requests.equipment_id = equipment.id
            AND borrowing_requests.status = 'Approved'
        )
        """
    ).fetchone()[0]

    my_listings = connection.execute(
        """
        SELECT COUNT(*)
        FROM equipment
        WHERE owner_id = ?
        """,
        (session["user_id"],)
    ).fetchone()[0]

    my_borrowings = connection.execute(
        """
        SELECT COUNT(*)
        FROM borrowing_requests
        WHERE borrower_id = ?
        AND status = 'Approved'
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
            available_until_date = datetime.strptime(
                available_until, "%Y-%m-%d"
            ).date()
        except ValueError:
            return error_response("Enter valid availability dates.")

        if not all((item_name.strip(), category.strip(), description.strip(),
                    condition.strip(), sharing_type.strip(), location.strip())):
            return error_response("Complete all required listing fields.")
        if available_from_date < date.today():
            return error_response("Available From cannot be in the past.")
        if available_until_date < available_from_date:
            return error_response(
                "Available Until must be on or after Available From."
            )

        connection = connect_database(application.DATABASE)

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
                location
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                location
            )
        )

        connection.commit()
        connection.close()

        return redirect(
            url_for("dashboard")
        )

    return render_template(
        "list_item.html",
        today=date.today().isoformat()
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
        WHERE category IS NOT NULL
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
                    WHEN EXISTS (
                        SELECT 1
                        FROM borrowing_requests
                        WHERE borrowing_requests.equipment_id = equipment.id
                        AND borrowing_requests.status = 'Approved'
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
                END AS equipment_status
            FROM equipment
            JOIN users ON equipment.owner_id = users.id
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
    total_count = equipment[0][13] if equipment else 0
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
@app.route("/equipment/<int:equipment_id>")
def equipment_details(equipment_id):

    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT
            equipment.*,
            users.name
        FROM equipment
        JOIN users
        ON equipment.owner_id = users.id
        WHERE equipment.id = ?
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
        AND status IN ('Pending', 'Approved')
        LIMIT 1
        """,
        (equipment_id,)
    ).fetchone()

    connection.close()

    if active_request:
        if active_request[0] == "Approved":
            equipment_status = "Booked"
        else:
            equipment_status = "Pending"
    else:
        equipment_status = "Available"

    return render_template(
        "item_details.html",
        item=item,
        equipment_status=equipment_status,
        today=date.today().isoformat()
    )
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

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return error_response("Choose valid rental dates.")

    if start < date.today():
        return error_response("Rental start date cannot be in the past.")
    if end < start:
        return error_response("End date must be on or after the start date.")

    connection = connect_database(application.DATABASE)

    item = connection.execute(
        """
        SELECT *
        FROM equipment
        WHERE id = ?
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

    # Dates must be inside equipment availability
    if start_date < item[9] or end_date > item[10]:
        connection.close()
        return error_response(
            "Choose dates within the equipment availability period."
        )

    # Stop the same student from requesting twice
    existing_request = connection.execute(
        """
        SELECT id
        FROM borrowing_requests
        WHERE equipment_id = ?
        AND borrower_id = ?
        AND status IN ('Pending', 'Approved')
        """,
        (
            equipment_id,
            session["user_id"]
        )
    ).fetchone()

    if existing_request:
        connection.close()
        return error_response(
            "You already have an active request for this equipment.",
            409,
            "Request already exists",
        )

    # Stop requests when equipment is already pending or booked
    unavailable_request = connection.execute(
        """
        SELECT status
        FROM borrowing_requests
        WHERE equipment_id = ?
        AND status IN ('Pending', 'Approved')
        LIMIT 1
        """,
        (equipment_id,)
    ).fetchone()

    if unavailable_request:

        connection.close()

        if unavailable_request[0] == "Approved":
            return error_response("This equipment is already booked.", 409)

        return error_response(
            "This equipment already has a pending request.", 409
        )



    rental_days = (end - start).days + 1

    if item[6] == "Paid":
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
    status,
    total_price
)
VALUES (?, ?, ?, ?, ?, ?)

        """,
        (

    equipment_id,
    session["user_id"],
    start_date,
    end_date,
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
        SET status = 'Approved'
        WHERE id = ?
          AND status = 'Pending'
        """,
        (request_id,)
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("This request is no longer pending.", 409)
    add_notification(
        connection,
        request_data[1],
        f"Your request for {request_data[2]} was approved.",
        url_for("my_rentals"),
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for("owner_requests")
    )


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
                )
                THEN 1
                ELSE 0
            END AS has_rated,
equipment.deposit,
borrowing_requests.damage_status,
borrowing_requests.damage_report,
borrowing_requests.damage_charge,
borrowing_requests.late_fee,
borrowing_requests.late_fee_status
        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users AS owner
        ON equipment.owner_id = owner.id

        WHERE borrowing_requests.borrower_id = ?

        ORDER BY borrowing_requests.id DESC
        """,
        (session["user_id"],)
    ).fetchall()

    connection.close()

    return render_template(
        "my_rentals.html",
        rentals=rentals
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

            CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM borrowing_requests
                    WHERE borrowing_requests.equipment_id = equipment.id
                    AND borrowing_requests.status = 'Approved'
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
            END AS listing_status

        FROM equipment

        WHERE equipment.owner_id = ?

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
            available_until_date = datetime.strptime(
                available_until, "%Y-%m-%d"
            ).date()
        except ValueError:
            connection.close()
            return error_response("Enter valid availability dates.")

        if available_from_date < date.today():
            connection.close()
            return error_response("Available From cannot be in the past.")
        if available_until_date < available_from_date:
            connection.close()
            return error_response(
                "Available Until must be on or after Available From."
            )
        if not all((item_name, category, description, condition,
                    sharing_type, location.strip())):
            connection.close()
            return error_response("Complete all required listing fields.")

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
                deposit = ?,
                available_from = ?,
                available_until = ?,
                location = ?

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
                deposit,
                available_from,
                available_until,
                location,
                equipment_id,
                session["user_id"]
            )
        )

        connection.commit()
        connection.close()

        return redirect(
            url_for("my_listings")
        )

    connection.close()

    return render_template(
        "edit_listing.html",
        item=item,
        today=date.today().isoformat()
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
        SELECT id
        FROM equipment
        WHERE id = ?
        AND owner_id = ?
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
            equipment.price
        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
        AND equipment.owner_id = ?
        AND borrowing_requests.status = 'Approved'
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

    actual_return_date = datetime.now().date()

    late_days = max(
        0,
        (actual_return_date - agreed_return_date).days
    )

    daily_price = float(request_data[5] or 0)

    late_fee = late_days * daily_price
    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Completed',
            late_fee = ?
        WHERE id = ?
          AND status = 'Approved'
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
            users.name

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users
        ON equipment.owner_id = users.id

        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response("Rental not found.", 404, "Rental unavailable")

    if rental[1] != session["user_id"]:
        connection.close()
        return error_response("You cannot rate this rental.", 403)

    if rental[2] != "Completed":
        connection.close()
        return error_response("You can only rate a completed rental.")

    existing_rating = connection.execute(
        """
        SELECT id
        FROM ratings
        WHERE borrowing_request_id = ?
        """,
        (request_id,)
    ).fetchone()

    if request.method == "POST":

        if existing_rating:
            connection.close()
            return error_response("You have already rated this rental.", 409)

        rating = request.form["rating"]
        comment = request.form["comment"]

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
                rental[3],
                rating,
                comment
            )
        )

        connection.commit()
        connection.close()

        return redirect(url_for("my_rentals"))

    owner_name = rental[4]

    connection.close()

    return render_template(
        "rate.html",
        owner_name=owner_name
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

    connection.close()

    return render_template(
        "my_reviews.html",
        reviews=reviews
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
            borrowing_requests.damage_charge
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
    connection.close()

    return render_template(
        "requests.html",
        requests_list=requests_data
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
            name,
            email
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

            connection.execute(
                """
                INSERT INTO notifications (
                    user_id,
                    message,
                    link
                )
                VALUES (?, ?, ?)
                """,
                (
                    student_id,
                    f"New message from {sender_name}",
                    f"/chat/{current_user_id}"
                )
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
        notifications=notifications_list
    )
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
                current_app.logger.exception(
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
            users.name

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

    if rental[2] != "Approved":
        return error_response(
            "This rental must be approved before payment.", 400
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
# QR PAYMENT CONFIRMATION
# =====================================================

@app.route(
    "/qr-payment/confirm/<token>",
    methods=["GET", "POST"]
)
def qr_payment_confirm(token):

    try:
        payment_data = serializer.loads(
            token,
            salt="qr-payment-salt",
            max_age=600
        )

    except SignatureExpired:
        return error_response(
            "This QR payment code has expired.", 400, "QR code expired"
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
            users.name

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        JOIN users
        ON equipment.owner_id = users.id

        WHERE borrowing_requests.id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response("Rental not found.", 404, "Rental unavailable")

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

        if rental[2] != "Approved":
            connection.close()
            return error_response(
                "The rental is not approved for payment.", 400
            )

        connection.execute(
            """
            UPDATE borrowing_requests
            SET payment_status = 'Paid'
            WHERE id = ?
            AND borrower_id = ?
            """,
            (
                request_id,
                borrower_id
            )
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


@app.route("/pay-late-fee/<int:request_id>", methods=["POST"])
def pay_late_fee(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = connect_database(application.DATABASE)

    rental = connection.execute(
        """
        SELECT borrower_id, status, late_fee, late_fee_status
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
        return error_response("You cannot pay this late fee.", 403)

    if rental[1] != "Completed":
        connection.close()
        return error_response("The rental must be completed first.", 400)

    if float(rental[2] or 0) <= 0:
        connection.close()
        return error_response("There is no late fee to pay.", 400)

    if rental[3] == "Paid":
        connection.close()
        return redirect(url_for("my_rentals"))

    connection.execute(
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

    connection.commit()
    connection.close()

    return redirect(url_for("my_rentals"))


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
        JOIN equipment ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,),
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response("Rental not found.", 404, "Rental unavailable")
    if rental[0] != session["user_id"]:
        connection.close()
        return error_response("You cannot confirm this deposit.", 403)
    if rental[1] != "Approved" or rental[2] != "Paid":
        connection.close()
        return error_response(
            "The rental must be approved and paid before confirming its deposit.",
            400,
        )
    if rental[3] != "Pending":
        connection.close()
        return error_response("This deposit is no longer pending.", 409)

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Held'
        WHERE id = ?
          AND status = 'Approved'
          AND payment_status = 'Paid'
          AND deposit_status = 'Pending'
        """,
        (request_id,),
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("This deposit is no longer pending.", 409)

    connection.commit()
    connection.close()
    return redirect(url_for("owner_requests"))


@app.route("/damage-status/<int:request_id>/<condition>", methods=["POST"])
def set_damage_status(request_id, condition):

    if "user_id" not in session:
        return redirect(url_for("login"))
    if condition not in {"good", "damaged"}:
        return error_response("Choose a valid equipment condition.", 400)

    connection = connect_database(application.DATABASE)
    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            equipment.deposit,
            borrowing_requests.status,
            borrowing_requests.deposit_status,
            borrowing_requests.damage_status
        FROM borrowing_requests
        JOIN equipment ON borrowing_requests.equipment_id = equipment.id
        WHERE borrowing_requests.id = ?
        """,
        (request_id,),
    ).fetchone()

    if rental is None:
        connection.close()
        return error_response("Rental not found.", 404, "Rental unavailable")
    if rental[0] != session["user_id"]:
        connection.close()
        return error_response("You cannot update this damage report.", 403)
    if rental[2] != "Completed" or rental[3] != "Held":
        connection.close()
        return error_response(
            "The completed rental must have a held deposit before inspection.",
            400,
        )
    if rental[4] != "Not Checked":
        connection.close()
        return error_response("This rental has already been inspected.", 409)

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
            (request_id,),
        )
    else:
        damage_report = request.form.get("damage_report", "").strip()
        if not damage_report or len(damage_report) > 1000:
            connection.close()
            return error_response(
                "Enter a damage description of 1 to 1000 characters."
            )

        damage_charge, amount_error = parse_amount(
            request.form.get("damage_charge", ""),
            "Damage charge",
        )
        if amount_error:
            connection.close()
            return error_response(amount_error)
        if damage_charge <= 0:
            connection.close()
            return error_response("Damage charge must be greater than zero.")
        if damage_charge > float(rental[1] or 0):
            connection.close()
            return error_response(
                "Damage charge cannot exceed the held security deposit."
            )

        cursor = connection.execute(
            """
            UPDATE borrowing_requests
            SET damage_status = 'Damaged',
                damage_report = ?,
                damage_charge = ?
            WHERE id = ?
              AND status = 'Completed'
              AND deposit_status = 'Held'
              AND damage_status = 'Not Checked'
            """,
            (damage_report, damage_charge, request_id),
        )

    if cursor.rowcount != 1:
        connection.close()
        return error_response("This rental has already been inspected.", 409)

    connection.commit()
    connection.close()
    return redirect(url_for("owner_requests"))


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
        return error_response("You cannot refund this deposit.", 403)

    if rental[1] != "Completed":
        connection.close()
        return error_response("The equipment must be returned first.", 400)

    if rental[2] != "Held":
        connection.close()
        return error_response("There is no held deposit to refund.", 400)
    if rental[3] != "Good Condition":
        connection.close()
        return error_response(
            "Mark the item as Good Condition before refunding the deposit.",
            400,
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
        return error_response("This deposit is no longer eligible for refund.", 409)

    connection.commit()
    connection.close()
    return redirect(url_for("owner_requests"))
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
        return error_response("You cannot settle this damage.", 403)

    if rental[1] != "Completed":
        connection.close()
        return error_response("The equipment must be returned first.", 400)

    if rental[2] != "Held":
        connection.close()
        return error_response("The deposit is not currently held.", 400)

    if rental[3] != "Damaged":
        connection.close()
        return error_response("This item is not marked as damaged.", 400)

    cursor = connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Settled'
        WHERE id = ?
          AND status = 'Completed'
          AND deposit_status = 'Held'
          AND damage_status = 'Damaged'
        """,
        (request_id,)
    )
    if cursor.rowcount != 1:
        connection.close()
        return error_response("This damage settlement has already been completed.", 409)

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
