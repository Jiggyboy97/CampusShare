import os
import sqlite3
import tempfile
import unittest

from database_backup import create_backup


class DatabaseBackupTests(unittest.TestCase):
    def test_backup_is_a_valid_copy_of_the_source_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "source.db")
            backup_dir = os.path.join(temp_dir, "backups")
            connection = sqlite3.connect(source_path)
            try:
                connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample VALUES ('preserved')")
                connection.commit()
            finally:
                connection.close()

            backup_path = create_backup(source_path, backup_dir)

            connection = sqlite3.connect(backup_path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "preserved",
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
