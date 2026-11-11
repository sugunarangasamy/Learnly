import sqlite3
import os

DATABASE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "learnly.db"
)

connection = sqlite3.connect(DATABASE_PATH)

users = connection.execute(
    "SELECT id, name, email, created_at FROM users"
).fetchall()

for user in users:
    print(user)

connection.close()