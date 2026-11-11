"""
Learnly Database Diagnostic & Verification Script
==================================================
Run this script anytime to verify that learnly.db is valid and properly populated:
    python check_database.py
"""

import os
import sqlite3

DATABASE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "learnly.db"
)

print("=" * 60)
print("LEARNLY DATABASE HEALTH CHECK")
print("=" * 60)
print(f"Database Path : {DATABASE_PATH}")
print(f"File Exists   : {os.path.exists(DATABASE_PATH)}")

if not os.path.exists(DATABASE_PATH):
    print("\n[ERROR] Database file not found!")
    exit(1)

file_size = os.path.getsize(DATABASE_PATH)
print(f"File Size     : {file_size:,} bytes")

with open(DATABASE_PATH, "rb") as file:
    header = file.read(16)

if header == b"SQLite format 3\x00":
    print("SQLite Header : [OK] Valid SQLite 3 format header detected.")
else:
    print(f"SQLite Header : [FAIL] Unexpected header: {header!r}")
    exit(1)

print("\n" + "-" * 60)
print("DATABASE TABLES & RECORD COUNTS")
print("-" * 60)

conn = sqlite3.connect(DATABASE_PATH)
conn.row_factory = sqlite3.Row
c = conn.cursor()

tables = c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
for t in tables:
    tname = t["name"]
    count = c.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
    print(f"  * Table '{tname}': {count} rows")

print("\n" + "-" * 60)
print("COURSES & ASSESSMENT QUESTIONS")
print("-" * 60)

courses = c.execute("SELECT id, name FROM courses ORDER BY id").fetchall()
for course in courses:
    cid = course["id"]
    cname = course["name"]
    t_count = c.execute("SELECT COUNT(*) FROM topics WHERE course_id = ?", (cid,)).fetchone()[0]
    q_count = c.execute("SELECT COUNT(*) FROM assessment_questions WHERE course_id = ?", (cid,)).fetchone()[0]
    print(f"  * [{cid}] {cname}: {t_count} topics | {q_count} assessment questions")

print("\n" + "-" * 60)
print("REGISTERED USERS")
print("-" * 60)

users = c.execute("SELECT id, name, email, created_at FROM users").fetchall()
if users:
    for u in users:
        print(f"  * User #{u['id']}: {u['name']} <{u['email']}> (Registered: {u['created_at']})")
else:
    print("  * No users registered yet. You can sign up on the web home page.")

conn.close()

print("\n" + "=" * 60)
print("HOW TO RUN LEARNLY:")
print("=" * 60)
print("  1. In terminal, start the server:")
print("     python app.py")
print("  2. Open your browser to:")
print("     http://127.0.0.1:5000/")
print("  3. Log in or create an account, then start any course assessment!")
print("=" * 60)
