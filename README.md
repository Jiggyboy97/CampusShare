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

- [x] Require email-link confirmation before account activation; store passwords
      as salted hashes and reject common passwords (8–128 characters).
- [x] Keep the Flask signing key outside source code, protect state-changing
      requests with CSRF, expire sessions after 30 minutes of inactivity, and
      use HttpOnly/SameSite cookies with Secure enabled over HTTPS.
- [x] Avoid logging or printing secrets, access tokens, or password-reset
      links; redact token-bearing URLs from development access logs.
- [x] Log failed sign-ins, rental approvals, deposit holds/refunds/settlements,
      damage assessments, and damage-dispute decisions without recording
      credentials, contact details, or report/dispute text.
- [x] Bind the development server to loopback only and redirect production
      HTTP requests to HTTPS when deployed behind a configured trusted proxy.
- [x] Temporarily block login after five failed attempts for the same account
      within 15 minutes, regardless of source IP.
- [x] Provide administrator tools to remove listings, suspend accounts, and
      review and resolve damage disputes without deleting rental history.
- [x] Show other users only account names; keep email addresses and student IDs
      out of public profiles, listings, chats, and administration pages.
- [x] Create, edit, browse, search, and filter equipment listings.
- [x] Reject listings whose availability starts in the past, validate listing
      and rental dates, calculate rental prices, and allow multiple bookings
      when their dates and (for hourly rentals) times do not overlap.
- [x] Show date validation errors beside their fields, preserve submitted dates
      after validation failures, and prevent past-date selection in listing and
      rental date pickers.
- [x] Allow Pending → Approved, Pending → Rejected, and Approved → Completed
      rental status changes; automatically mark an unreturned rental Overdue
      after its end date and allow its owner to mark it Completed on return;
      restrict approval, rejection, return, deposit, and damage actions to the
      relevant owner or borrower, and prevent listing deletion while rental
      request history exists.
- [x] Calculate late fees as the saved rental rate multiplied by whole late
      days; let owners record Good Condition or Damaged with a description and
      a non-negative charge within the held deposit; apply damage charges
      against the deposit; give borrowers 48 hours to review and dispute a
      damage report before settlement, require owners to resolve open disputes
      by upholding, revising, or cancelling the charge, and show the borrower
      the final charge and remaining refund; allow deposit refunds only for
      completed rentals with a held deposit and Good Condition; support demo
      payment, deposit, and return flows. Payments are simulated; no real money
      is processed.
- [x] Create in-app notifications for borrowing requests, approval, rejection,
      return, rental payment, damage assessment, overdue rentals, and new
      messages; offer opt-in email delivery for verified users; let message
      recipients mark their notifications as read; let message senders delete
      only their own messages; create duplicate-safe return reminders 24 hours
      before the due date and show active-rental countdowns.
- [x] Let both borrowers and owners leave one review for each other after a
      rental is completed; show each user's average received rating on their
      dashboard and preserve existing reviews during the schema upgrade.
- [x] Require new accounts to verify their university email before login and
      limit verification resends to three per email/IP pair per hour.
- [x] Upload and display equipment photos with JPG, PNG, or WebP signature
      checks, a 5 MB limit, owner-managed replacement/removal, and local-only
      storage ignored by Git.
- [x] Offer hourly or daily listing prices; hourly bookings stay on one day,
      owners set a daily pickup/return window, borrowers choose times within
      that window, partial hours round up with a one-hour minimum, and the
      live borrower cost estimate follows the same hourly/daily rounding rules;
      the hourly rate is used for each late day on hourly bookings.
- [x] Protect POST forms with CSRF tokens; require a configured Flask secret
      key; bind the local demo server to `127.0.0.1`.
- [x] Enforce SQLite foreign keys on application connections, prevent deletion
      of referenced records, and reject database startup when existing orphan
      records are detected; create database backups and run automated tests.
- [x] Render application and HTTP errors using the shared, styled error page
      while preserving the appropriate HTTP status code.

### Still to do or verify

- [ ] Configure SMTP credentials and verify password-reset and account
      verification delivery in a real inbox.
- [ ] Decide whether the project needs a real payment provider before launch.
- [ ] Complete remaining product features such as administration, account
      suspension, listing moderation, and dispute handling.
- [ ] Verify the app on Linux and at an actual 360px viewport; rerun checks on
      each platform claimed as supported.
- [ ] Before public deployment, configure HTTPS, production secrets, and a
      production-ready hosting setup. The current server is for local demo use.

### Rental return reminders

The app shows each active rental's days remaining on the My Rentals page and
notifies the borrower when the rental is overdue. To create an in-app
notification for a rental due the following day, run:

```powershell
python rental_reminders.py
```

On Windows, install a daily 8:00 AM Task Scheduler job after initializing the
database by running:

```powershell
.\install_reminder_task.ps1
```

The scheduled task runs as the current Windows user. Borrowers see the reminder
in their CampusShare notifications; repeated runs do not create duplicates.

Equipment photos are saved locally under `static/uploads/` and excluded from
Git. The SQLite backup job does not back up these files. Before deployment,
move uploads to durable object storage and include them in the backup plan.

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
$env:CAMPUSSHARE_ADMIN_EMAILS = "administrator@buaa.edu.cn"
$env:CAMPUSSHARE_ENV = "development"
$env:CAMPUSSHARE_TRUSTED_PROXY_HOPS = "0"
python app.py
```

The secret is required and must remain private. Keep the generated value for
future runs so existing sessions and password-reset links remain valid. The
PowerShell environment variable lasts only for the current terminal session.
Set `CAMPUSSHARE_ADMIN_EMAILS` to a comma-separated allowlist of trusted
university addresses before startup. Those addresses receive the Administrator
role after registration and email verification; existing accounts on the
allowlist are promoted during database initialization. Administrator privileges
cannot be self-selected during public registration.
Do not commit the secret. The demo server listens on `127.0.0.1` and is not
exposed to other devices.

### HTTPS deployment

The built-in server started by `python app.py` is for local development only
and binds to `127.0.0.1`. For deployment, run the Flask app with a production
WSGI server behind a TLS-terminating HTTPS reverse proxy; do not expose Flask's
built-in server. Configure the proxy to overwrite `X-Forwarded-Proto` and
`X-Forwarded-Host` with values it received from the HTTPS request, then set
`CAMPUSSHARE_ENV=production` and
`CAMPUSSHARE_TRUSTED_PROXY_HOPS` to the exact number of trusted proxies that
set those headers. The app refuses production startup if that count is missing
or zero, redirects HTTP requests to HTTPS, sets HSTS, and marks session cookies
Secure. Never trust forwarded headers from untrusted clients or proxies.

### Email delivery (SMTP)

CampusShare sends account-verification and password-reset links using SMTP.
Optional notification emails also require SMTP settings; without them, email
delivery will fail. Users can opt into email copies of notifications from the
Notifications page. Email delivery is off by default, and is sent only to
verified account addresses. SMTP failures are logged while the in-app
notification is still saved.
Before starting the app, set the SMTP connection details in the same
PowerShell session. For Gmail, use an App Password (with 2-Step Verification
enabled), not your regular account password:

```powershell
$env:CAMPUSSHARE_SMTP_HOST = "smtp.gmail.com"
$env:CAMPUSSHARE_SMTP_PORT = "587"
$env:CAMPUSSHARE_SMTP_SECURITY = "starttls"
$env:CAMPUSSHARE_SMTP_FROM = "your-account@gmail.com"
$env:CAMPUSSHARE_SMTP_USERNAME = "your-account@gmail.com"
$env:CAMPUSSHARE_SMTP_PASSWORD = "<your Gmail App Password>"
```

For another provider, use its SMTP host, port, security mode (`starttls` or
`ssl`), sender address, and credentials. Keep passwords out of source control.
SMTP delivery errors are recorded in the application log. Registration reports
if the verification email could not be sent; the user can try again using
"Resend verification email" on the login page. Password-reset requests always
show the same response whether or not the address belongs to an account.
Verification links expire after 24 hours, and password-reset links expire
after 30 minutes. The links use the address of the website serving the request;
for a deployed site, make sure its public web address is reachable by email
recipients.

## Running the workflow tests

Run the rental and payment workflow tests from the project directory:

```powershell
python -m unittest discover -s tests -v
```

The tests use a temporary SQLite database and do not modify `campusshare.db`.
They use a test-only secret key and disable CSRF only for their simulated form
submissions; a dedicated test verifies that missing CSRF tokens are rejected.

New accounts and password resets require passwords of 8 to 128 characters and
reject a curated set of common passwords; Werkzeug stores passwords as salted
hashes. Account activation requires a signed email-verification link. The
Flask secret key must be provided through `FLASK_SECRET_KEY`; it is not stored
in source code. Flask-WTF CSRF protection covers state-changing form requests,
including the final email-confirmation action and logout. The signed email link
opens a confirmation form and does not activate the account by itself.
Authenticated sessions are permanent with a 30-minute
inactivity lifetime; session cookies are HttpOnly and SameSite=Lax and receive
the Secure flag on HTTPS requests. Five failed login attempts for one email
address trigger a 15-minute lockout across IP addresses. Administrator access
is provisioned only from `CAMPUSSHARE_ADMIN_EMAILS`; suspended users are denied
login and their existing sessions are ended on their next request. Administrator
listing removal hides the listing from new borrowers while retaining its
associated rental records, and administrators can suspend or restore non-admin
accounts and resolve open damage disputes.
Security-event logs record failed sign-ins, approvals, deposit state changes,
damage assessments, and dispute decisions using event names and internal
account/rental IDs only; login identifiers, passwords, and user-submitted
damage/dispute descriptions are excluded.
Application error logs omit exception messages and tracebacks, email delivery
failures log only generic events, and development-server access logs redact
verification, password-reset, and payment-image tokens. Production reverse
proxies and hosting platforms must also be configured not to log sensitive
token-bearing URLs.

In-app notifications are created for new
borrowing requests and their approval, rejection, payment, return, and damage
assessment events.

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
- `campusshare_routes.py` contains the HTTP route handlers and rendering logic.
- `database.py` creates SQLite connections with foreign-key enforcement enabled.
- `database_backup.py` creates consistent SQLite backups.
- `tests/` contains isolated workflow and database tests.