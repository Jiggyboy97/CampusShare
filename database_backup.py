import argparse
from contextlib import closing
from datetime import datetime
import os
import sqlite3


PROJECT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(PROJECT_DIRECTORY, "campusshare.db")
BACKUP_DIRECTORY = os.path.join(PROJECT_DIRECTORY, "backups")


def create_backup(database_path=DATABASE_PATH, backup_directory=BACKUP_DIRECTORY):
    if not os.path.isfile(database_path):
        raise FileNotFoundError(f"Database not found: {database_path}")

    os.makedirs(backup_directory, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = os.path.join(backup_directory, f"campusshare-{timestamp}.db")

    with closing(sqlite3.connect(database_path)) as source:
        with closing(sqlite3.connect(backup_path)) as destination:
            source.backup(destination)
            destination.commit()

    return backup_path


def main():
    parser = argparse.ArgumentParser(description="Create a consistent SQLite backup.")
    parser.add_argument(
        "--database",
        default=DATABASE_PATH,
        help="Source database path (defaults to campusshare.db).",
    )
    parser.add_argument(
        "--destination",
        default=BACKUP_DIRECTORY,
        help="Directory to store the timestamped backup.",
    )
    arguments = parser.parse_args()
    backup_path = create_backup(arguments.database, arguments.destination)
    print(f"Database backup created: {backup_path}")


if __name__ == "__main__":
    main()
