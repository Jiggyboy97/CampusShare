# CampusShare

CampusShare is a peer-to-peer equipment sharing and rental demo for university
students.

## Project progress

This checklist reflects the current demo implementation. Items are marked
complete only where the feature has been implemented and, where applicable,
covered by the project's automated tests. No overall percentage is shown
because the original slide percentages predate recent work and use estimates
that have not been recalculated against a verified scoring method.

### Completed for the demo

- [x] Register and log in with a Beihang University email; passwords are
      hashed and new/reset passwords must be 12–128 characters.
- [x] Create, edit, browse, search, and filter equipment listings.
- [x] Validate listing and rental dates, reject past rental starts, calculate
      rental prices, and prevent conflicting or duplicate active requests.
- [x] Restrict approval, rejection, return, listing deletion, deposit, and
      damage actions to the relevant owner or borrower and valid status.
- [x] Calculate late fees and support demo payment, deposit, damage, and return
      flows. Payments are simulated; no real money is processed.
- [x] Create in-app notifications for borrowing requests, approval, rejection,
      return, and new messages.
- [x] Protect POST forms with CSRF tokens; require a configured Flask secret
      key; bind the local demo server to `127.0.0.1`.
- [x] Enforce SQLite foreign keys, create database backups, show styled error
      pages, and run the automated tests.

### Still to do or verify

- [ ] Configure real email delivery for password resets and notifications;
      add email verification.
- [ ] Add account lockout or rate limiting for repeated failed logins.
- [ ] Decide whether the project needs a real payment provider before launch.
- [ ] Add image uploads and hourly rentals if they remain in the requirements.
- [ ] Complete remaining product features such as administration, account
      suspension, listing moderation, and dispute handling.
- [ ] Verify the app on Linux and at an actual 360px viewport; rerun checks on
      each platform claimed as supported.
- [ ] Before public deployment, configure HTTPS, production secrets, and a
      production-ready hosting setup. The current server is for local demo use.

### Turay Edward Joe
- Backend and database development
- Flask routes and business logic
- Rental workflow implementation
- Payment and deposit logic
- Late fee handling
- Damage handling
- Testing and debugging

### Amanda Nancy Skaikay
- Requirements and system analysis
- UI and usability review
- Borrowing and rental workflow testing
- Documentation and presentation support
- Functionality evaluation

### Sandra Massiwa
- System design
- Project documentation
- System architecture and workflow design
- Functional and security requirements documentation
- User roles and process-flow documentation
- Security and trust design considerations
- Presentation and project documentation support

## Installing dependencies

From the project directory, install the packages listed in `requirements.txt`:

```powershell
python -m pip install -r requirements.txt
```

Start the application with:

```powershell
# Generate this once, save it securely, and reuse the same value in later sessions.
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
$env:FLASK_SECRET_KEY = "<paste the generated value>"
python app.py
```

The secret is required and must remain private. Keep the generated value for
future runs so existing sessions and password-reset links remain valid. The
PowerShell environment variable lasts only for the current terminal session.
Do not commit the secret. The demo server listens on `127.0.0.1` and is not
exposed to other devices.

## Running the workflow tests

Run the rental and payment workflow tests from the project directory:

```powershell
python -m unittest discover -s tests -v
```

The tests use a temporary SQLite database and do not modify `campusshare.db`.
They use a test-only secret key and disable CSRF only for their simulated form
submissions; a dedicated test verifies that missing CSRF tokens are rejected.

New accounts and password resets require passwords of 12 to 128 characters.
All POST forms use CSRF protection. In-app notifications are created for new
borrowing requests and their approval, rejection, and return status changes.

## Database backups

Create a consistent timestamped SQLite backup with:

```powershell
python database_backup.py
```

Backups are written to `backups/`, which is excluded from Git. On Windows,
install a daily 2:00 AM Task Scheduler job from PowerShell:

```powershell
.\install_backup_task.ps1
```

On macOS or Linux, schedule `python /path/to/project/database_backup.py` once
per day with the operating system's scheduler. Periodically copy backups to a
separate device or secure cloud storage and test restoring one; a backup on
the same disk does not protect against disk failure.

## Performance benchmark

Run the proposed browse/search benchmark with:

```powershell
$env:FLASK_SECRET_KEY = "<your saved development secret>"
python performance_test.py
```

It seeds 1,000 listings, then sends requests from 50 concurrent workers and
reports p50/p95/max latency. Pagination limits each rendered results page to
24 listings. On the current Windows development environment, the in-process
test client measured p95 at about 0.88 seconds after pagination. The 2-second
p95 threshold is provisional and should be confirmed against the project
requirements. This is an application-level check, not a production-host or
real-network capacity certification.

## Platform and Python support

The project dependencies require Python 3.9 or newer. The current development
environment is Windows with Python 3.14.5. The project team has also reported
a macOS run; Linux has not yet been verified. Responsive styles target 360px,
and the integrated browser check found no overflow at its narrowest available
450px viewport. Verify on an actual 360px viewport before marking that
requirement fully tested. Re-run the test suite on each target operating
system before describing it as supported.

## Application structure

- `app.py` creates the Flask application and initializes the database schema.
- `routes.py` contains the HTTP route handlers and rendering logic.
- `database.py` creates SQLite connections with foreign-key enforcement enabled.
- `database_backup.py` creates consistent SQLite backups.
- `tests/` contains isolated workflow and database tests.