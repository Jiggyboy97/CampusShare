import os
import statistics
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import app as application
from database import connect_database


LISTING_COUNT = 1000
CONCURRENT_USERS = 50
REQUESTS_PER_USER = 5
P95_TARGET_SECONDS = 2.0


def run_benchmark():
    original_database = application.DATABASE
    with tempfile.TemporaryDirectory() as directory:
        application.DATABASE = os.path.join(directory, "performance.db")
        try:
            application.init_db()
            connection = connect_database(application.DATABASE)
            connection.execute(
                """
                INSERT INTO users (name, email, student_id, password)
                VALUES ('Benchmark Owner', 'benchmark@example.test',
                        'benchmark-owner', 'not-used')
                """
            )
            owner_id = connection.execute(
                "SELECT id FROM users WHERE student_id = 'benchmark-owner'"
            ).fetchone()[0]
            connection.executemany(
                """
                INSERT INTO equipment (
                    owner_id, item_name, category, description, condition,
                    sharing_type, price, deposit, available_from,
                    available_until, location
                )
                VALUES (?, ?, ?, 'Performance test item', 'Good', 'Free',
                        0, 0, '2026-01-01', '2027-12-31', 'Campus')
                """,
                [
                    (owner_id, f"Benchmark item {index}", "Equipment")
                    for index in range(LISTING_COUNT)
                ],
            )
            connection.commit()
            connection.close()

            def timed_browse(request_index):
                client = application.app.test_client()
                started = time.perf_counter()
                query = (
                    "/browse?q=Benchmark"
                    if request_index % 2 == 0
                    else "/browse?category=Equipment"
                )
                response = client.get(query)
                elapsed = time.perf_counter() - started
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Browse returned HTTP {response.status_code}"
                    )
                return elapsed

            total_requests = CONCURRENT_USERS * REQUESTS_PER_USER
            with ThreadPoolExecutor(max_workers=CONCURRENT_USERS) as executor:
                samples = list(
                    executor.map(timed_browse, range(total_requests))
                )

            ordered = sorted(samples)
            p95 = ordered[round(0.95 * (len(ordered) - 1))]
            print(
                f"Environment: in-process Flask test client; "
                f"Python {os.sys.version.split()[0]}"
            )
            print(
                f"Listings: {LISTING_COUNT}; concurrent workers: "
                f"{CONCURRENT_USERS}; requests: {total_requests}"
            )
            print(
                f"Browse latency seconds: p50={statistics.median(samples):.3f}, "
                f"p95={p95:.3f}, max={max(samples):.3f}"
            )
            print(
                f"Proposed p95 target {P95_TARGET_SECONDS:.1f}s: "
                f"{'PASS' if p95 <= P95_TARGET_SECONDS else 'FAIL'}"
            )
            return p95
        finally:
            application.DATABASE = original_database


if __name__ == "__main__":
    run_benchmark()
