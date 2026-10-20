import sqlite3


def connect_database(database_path, timeout=5):
    connection = sqlite3.connect(database_path, timeout=timeout)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
