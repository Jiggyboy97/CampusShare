from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import os
from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.secret_key = "campusshare-secret-key"

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

        return redirect(url_for("login"))

    return render_template("register.html")


# =====================================================
# LOGIN
# =====================================================

@app.route("/login", methods=["GET", "POST"])
def login():

    if request.method == "POST":

        email = request.form["email"].strip()
        password = request.form["password"]

        connection = sqlite3.connect(DATABASE)

        user = connection.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
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

    # Create new request
    connection.execute(
        """
        INSERT INTO borrowing_requests (
            equipment_id,
            borrower_id,
            start_date,
            end_date,
            status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            equipment_id,
            session["user_id"],
            start_date,
            end_date,
            "Pending"
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
        SELECT borrowing_requests.id
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

    connection.execute(
        """
        UPDATE borrowing_requests
        SET status = 'Completed'
        WHERE id = ?
        """,
        (request_id,)
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
            borrowing_requests.status

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