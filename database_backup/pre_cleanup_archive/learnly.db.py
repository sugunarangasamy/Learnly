"""
Learnly Database Helper Module
==============================
Provides connection utilities for SQLite database.
If executed directly in terminal:
    python learnly.db.py
it prints database summary and verification.
"""

import sqlite3
import os

DATABASE_NAME = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "learnly.db"
)


def get_connection():
    connection = sqlite3.connect(DATABASE_NAME)
    connection.row_factory = sqlite3.Row
    return connection


if __name__ == "__main__":
    print(f"Connecting to Learnly SQLite database: {DATABASE_NAME}")
    if os.path.exists(DATABASE_NAME):
        print(f"Status: Database exists ({os.path.getsize(DATABASE_NAME):,} bytes)")
        conn = get_connection()
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"Tables ({len(tables)}):", [t["name"] for t in tables])
        courses = conn.execute("SELECT name FROM courses").fetchall()
        print(f"Courses ({len(courses)}):", [c["name"] for c in courses])
        conn.close()
        print("\nDatabase is healthy and ready to use.")
        print("To launch Learnly, run: python app.py")
    else:
        print("Status: Database file does not exist yet. Run `python app.py` to initialize.")
