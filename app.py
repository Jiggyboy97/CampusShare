from multiprocessing import connection

from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
from datetime import datetime
import os
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature

app = Flask(__name__)
app.secret_key = "campusshare-secret-key"
serializer = URLSafeTimedSerializer(app.secret_key)
DATABASE = os.path.join(
    os.path.dirname(__file__),
    "campusshare.db"
)


# =====================================================
# DATABASE
# =====================================================

def init_db():

    connection = sqlite3.connect(DATABASE)

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
damage_status TEXT NOT NULL DEFAULT 'Not Checked',
damage_report TEXT DEFAULT '',
damage_charge REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'Pending',
            FOREIGN KEY (equipment_id) REFERENCES equipment(id),
            FOREIGN KEY (borrower_id) REFERENCES users(id)
        )
    """)

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
            return "Registration is only available to Beihang University students."
        if password != confirm_password:
            return "Passwords do not match."

        hashed_password = generate_password_hash(password)

        connection = sqlite3.connect(DATABASE)

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

            return "Email or Student ID already exists."

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

        connection = sqlite3.connect(DATABASE)

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

        return "Invalid email or password."

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

    connection = sqlite3.connect(DATABASE)

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

        item_name = request.form["item_name"]
        category = request.form["category"]
        description = request.form["description"]
        condition = request.form["condition"]
        sharing_type = request.form["sharing_type"]

        price = request.form["price"] or 0
        deposit = request.form["deposit"] or 0

        available_from = request.form["available_from"]
        available_until = request.form["available_until"]
        location = request.form["location"]

        if available_until < available_from:

            return (
                "Available Until date must "
                "be after Available From date."
            )

        connection = sqlite3.connect(DATABASE)

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
        "list_item.html"
    )


# =====================================================
# BROWSE EQUIPMENT
# =====================================================

@app.route("/browse")
def browse():

    search = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()

    connection = sqlite3.connect(DATABASE)

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

    equipment = connection.execute(
        """
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

            JOIN users
            ON equipment.owner_id = users.id
        )

        SELECT *
        FROM equipment_with_status

        WHERE
            (
                ? = ''
                OR item_name LIKE ?
                OR category LIKE ?
                OR description LIKE ?
            )

        AND
            (
                ? = ''
                OR category = ?
            )

        AND
            (
                ? = ''
                OR equipment_status = ?
            )

        ORDER BY id DESC
        """,
        (
            search,
            search_pattern,
            search_pattern,
            search_pattern,
            category,
            category,
            status,
            status
        )
    ).fetchall()

    connection.close()

    return render_template(
        "browse.html",
        equipment=equipment,
        search=search,
        category=category,
        categories=categories,
        status=status
    )
# EQUIPMENT DETAILS
@app.route("/equipment/<int:equipment_id>")
def equipment_details(equipment_id):

    connection = sqlite3.connect(DATABASE)

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
        return "Equipment not found."

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
        equipment_status=equipment_status
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

    start_date = request.form["start_date"]
    end_date = request.form["end_date"]

    connection = sqlite3.connect(DATABASE)

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
        return "Equipment not found."

    # Owner cannot request own equipment
    if item[1] == session["user_id"]:
        connection.close()
        return "You cannot request your own equipment."

    # End date cannot be before start date
    if end_date < start_date:
        connection.close()
        return "End date must be after the start date."

    # Dates must be inside equipment availability
    if start_date < item[9] or end_date > item[10]:
        connection.close()
        return (
            "Please choose dates within "
            "the equipment availability period."
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
        return "You already have an active request for this equipment."

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
            return "This equipment is already booked."

        return "This equipment already has a pending request."

     

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")

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

    connection = sqlite3.connect(DATABASE)

    request_data = connection.execute(
        """
        SELECT
            borrowing_requests.id

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
        AND equipment.owner_id = ?
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()

    if request_data is None:

        connection.close()

        return (
            "Request not found or "
            "you are not the owner."
        )

    connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Approved'
        WHERE id = ?
        """,
        (request_id,)
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

    connection = sqlite3.connect(DATABASE)

    request_data = connection.execute(
        """
        SELECT
            borrowing_requests.id

        FROM borrowing_requests

        JOIN equipment
        ON borrowing_requests.equipment_id = equipment.id

        WHERE borrowing_requests.id = ?
        AND equipment.owner_id = ?
        """,
        (
            request_id,
            session["user_id"]
        )
    ).fetchone()

    if request_data is None:

        connection.close()

        return (
            "Request not found or "
            "you are not the owner."
        )

    connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Rejected'
        WHERE id = ?
        """,
        (request_id,)
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

    connection = sqlite3.connect(DATABASE)

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
            END AS has_rated

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

    connection = sqlite3.connect(DATABASE)

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

    connection = sqlite3.connect(DATABASE)

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

        return (
            "Listing not found or "
            "you are not the owner."
        )

    if request.method == "POST":

        item_name = request.form["item_name"]
        category = request.form["category"]
        description = request.form["description"]
        condition = request.form["condition"]
        sharing_type = request.form["sharing_type"]

        price = request.form["price"] or 0
        deposit = request.form["deposit"] or 0

        available_from = request.form["available_from"]
        available_until = request.form["available_until"]
        location = request.form["location"]

        if available_until < available_from:

            connection.close()

            return (
                "Available Until must be "
                "after Available From."
            )

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
        item=item
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

    connection = sqlite3.connect(DATABASE)

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

        return (
            "Listing not found or "
            "you are not the owner."
        )

    # Delete requests linked to this equipment
    connection.execute(
        """
        DELETE FROM borrowing_requests
        WHERE equipment_id = ?
        """,
        (equipment_id,)
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

    connection = sqlite3.connect(DATABASE)

    request_data = connection.execute(
        """
        SELECT
    borrowing_requests.id,
    borrowing_requests.end_date,
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
        return "Active rental not found or you are not the owner."
    agreed_return_date = datetime.strptime(
        request_data[1],
        "%Y-%m-%d"
    ).date()

    actual_return_date = datetime.now().date()

    late_days = max(
        0,
        (actual_return_date - agreed_return_date).days
    )

    daily_price = float(request_data[2] or 0)

    late_fee = late_days * daily_price
    connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Completed',
            late_fee = ?
        WHERE id = ?
        """,
        (
            late_fee,
            request_id
        )
    )

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
@app.route("/rate/<int:request_id>", methods=["GET", "POST"])
def rate_owner(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = sqlite3.connect(DATABASE)

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
        return "Rental not found."

    if rental[1] != session["user_id"]:
        connection.close()
        return "You cannot rate this rental."

    if rental[2] != "Completed":
        connection.close()
        return "You can only rate a completed rental."

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
            return "You have already rated this rental."

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

    connection = sqlite3.connect(DATABASE)

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

    connection = sqlite3.connect(DATABASE)


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

    connection = sqlite3.connect(DATABASE)

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

    connection = sqlite3.connect(DATABASE, timeout=30)
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
        return "Student not found."
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

    connection = sqlite3.connect(DATABASE, timeout=30)

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
    reset_link = None
    if request.method == "POST":

        email = request.form["email"].strip().lower()

        connection = sqlite3.connect(DATABASE, timeout=30)

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
            
        print("RESET LINK: http://127.0.0.1:5000/reset-password/" + token)
        message = (
            "If that email is registered, "
            "password reset instructions will be provided."
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
        return "This password reset link has expired."

    except BadSignature:
        return "Invalid password reset link."

    if request.method == "POST":

        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            return "Passwords do not match."

        hashed_password = generate_password_hash(password)

        connection = sqlite3.connect(DATABASE, timeout=30)

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

    connection = sqlite3.connect(DATABASE, timeout=30)

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

    connection = sqlite3.connect(DATABASE, timeout=30)

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
        return "You cannot delete this message."

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
@app.route("/pay-rental/<int:request_id>", methods=["POST"])
def pay_rental(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = sqlite3.connect(DATABASE)

    rental = connection.execute(
        """
        SELECT borrower_id, status, payment_status
        FROM borrowing_requests
        WHERE id = ?
        """,
        (request_id,)
    ).fetchone()

    if rental is None:
        connection.close()
        return redirect(url_for("my_rentals"))

    # Only the borrower can pay
    if rental[0] != session["user_id"]:
        connection.close()
        return "You cannot pay for this rental."

    # Do not allow rejected rentals to be paid
    if rental[1] == "Rejected":
        connection.close()
        return "This rental was rejected."

    connection.execute(
        """
        UPDATE borrowing_requests
        SET payment_status = 'Paid'
        WHERE id = ?
        """,
        (request_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("my_rentals"))



@app.route("/confirm-deposit/<int:request_id>", methods=["POST"])
def confirm_deposit(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = sqlite3.connect(DATABASE)

    rental = connection.execute(
        """
        SELECT equipment.owner_id,
               borrowing_requests.payment_status,
               borrowing_requests.status
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
        return "You cannot confirm this deposit."

    if rental[1] != "Paid":
        connection.close()
        return "Payment must be completed first."

    connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Held'
        WHERE id = ?
        """,
        (request_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
@app.route("/damage-status/<int:request_id>/<condition>", methods=["POST"])
def update_damage_status(request_id, condition):

    if "user_id" not in session:
        return redirect(url_for("login"))

    allowed_conditions = {
        "good": "Good Condition",
        "damaged": "Damaged"
    }

    if condition not in allowed_conditions:
        return redirect(url_for("owner_requests"))

    connection = sqlite3.connect(DATABASE)

    rental = connection.execute(
        """
        SELECT
            equipment.owner_id,
            borrowing_requests.status
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
        return "You cannot update this rental."

    if rental[1] != "Completed":
        connection.close()
        return "The equipment must be returned first."

    damage_report = request.form.get("damage_report", "").strip()

    if condition == "damaged" and not damage_report:
        connection.close()
        return "Please describe the damage before submitting."

    if condition == "good":
        damage_report = ""
    damage_charge_text = request.form.get("damage_charge", "").strip()

    if condition == "damaged":
        try:
            damage_charge = float(damage_charge_text)
        except ValueError:
            connection.close()
            return "Please enter a valid damage charge."

        if damage_charge < 0:
            connection.close()
            return "Damage charge cannot be negative."
    else:
        damage_charge = 0





    connection.execute(
        """
        UPDATE borrowing_requests
       SET damage_status = ?,
    damage_report = ?,
    damage_charge = ?
WHERE id = ?
        """,
    allowed_conditions[condition],
damage_report,
damage_charge,
request_id  
        
        
    )

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
@app.route("/refund-deposit/<int:request_id>", methods=["POST"])
def refund_deposit(request_id):

    if "user_id" not in session:
        return redirect(url_for("login"))

    connection = sqlite3.connect(DATABASE)

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
        return "You cannot refund this deposit."

    if rental[1] != "Completed":
        connection.close()
        return "The equipment must be returned first."

    if rental[2] != "Held":
        connection.close()
        return "There is no held deposit to refund."
    if rental[3] != "Good Condition":
        connection.close()
        return "Mark the item as Good Condition before refunding the deposit."
    connection.execute(
        """
        UPDATE borrowing_requests
        SET deposit_status = 'Refunded'
        WHERE id = ?
        """,
        (request_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("owner_requests"))
# =====================================================
# START APPLICATION
# =====================================================

if __name__ == "__main__":

    init_db()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )