"""
database.py - Learnly Database Layer
====================================
Responsibilities:
  - SQLite database connection management (Row factory & Foreign Key PRAGMA)
  - Schema initialization for all 7 application tables
  - CRUD operations for users, courses, topics, prerequisites, progress, and assessment results
  - Database health and integrity verification (absorbed from check_database.py)
"""

import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATABASE_PATH = os.path.join(BASE_DIR, "learnly.db")


def get_db_connection(db_path=None):
    """
    Creates and returns an active SQLite connection with Row factory and PRAGMA foreign_keys = ON.
    Accepts an optional db_path for isolated test databases.
    """
    target_path = db_path or DEFAULT_DATABASE_PATH
    connection = sqlite3.connect(target_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")
    return connection


def initialize_database(db_path=None):
    """
    Initializes all 7 application tables in the database if they do not exist.
    """
    connection = get_db_connection(db_path)

    # 1. Users table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 2. Courses table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        )
    """)

    # 3. Topics table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            display_order INTEGER,
            FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
        )
    """)

    # 4. Topic prerequisites table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS topic_prerequisites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            prerequisite_topic_id INTEGER NOT NULL,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE,
            FOREIGN KEY (prerequisite_topic_id) REFERENCES topics (id) ON DELETE CASCADE,
            UNIQUE(topic_id, prerequisite_topic_id)
        )
    """)

    # 5. Assessment questions table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS assessment_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            question_number INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            option_a TEXT NOT NULL,
            option_b TEXT NOT NULL,
            option_c TEXT NOT NULL,
            option_d TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE
        )
    """)

    # 6. Learner assessment results table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS learner_assessment_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            total_score INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            score_percentage REAL NOT NULL,
            topic_scores_json TEXT NOT NULL,
            submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (course_id) REFERENCES courses (id) ON DELETE CASCADE
        )
    """)

    # 7. Learner topic progress table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS learner_topic_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, topic_id),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE
        )
    """)

    connection.commit()
    connection.close()


# -------------------------------------------------------------
# User Account Queries
# -------------------------------------------------------------

def get_user_by_email(email, db_path=None):
    """Fetches a user row by email (case-insensitive)."""
    conn = get_db_connection(db_path)
    user = conn.execute(
        "SELECT id, name, email, password_hash FROM users WHERE email = ? COLLATE NOCASE",
        (email.strip().lower(),)
    ).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id, db_path=None):
    """Fetches a user row by integer user_id."""
    conn = get_db_connection(db_path)
    user = conn.execute(
        "SELECT id, name, email, password_hash FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return user


def create_user(name, email, password_hash, db_path=None):
    """
    Inserts a new user record.
    Returns user_id on success, raises sqlite3.IntegrityError if email exists.
    """
    conn = get_db_connection(db_path)
    cursor = conn.execute(
        "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
        (name.strip(), email.strip().lower(), password_hash)
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


# -------------------------------------------------------------
# Course & Topic Queries
# -------------------------------------------------------------

def get_all_courses(db_path=None):
    """Returns a list of all course rows ordered by ID."""
    conn = get_db_connection(db_path)
    rows = conn.execute("SELECT id, name, description FROM courses ORDER BY id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_course(course_identifier, db_path=None):
    """
    Looks up a course by exact name, ID, or substring.
    Falls back to the first course if not found.
    """
    conn = get_db_connection(db_path)

    # 1. Try by integer ID
    if isinstance(course_identifier, int) or (isinstance(course_identifier, str) and course_identifier.isdigit()):
        course = conn.execute(
            "SELECT id, name, description FROM courses WHERE id = ?",
            (int(course_identifier),)
        ).fetchone()
        if course:
            conn.close()
            return dict(course)

    # 2. Try exact name match (case-insensitive)
    course = conn.execute(
        "SELECT id, name, description FROM courses WHERE name = ? COLLATE NOCASE",
        (str(course_identifier),)
    ).fetchone()

    # 3. Try fuzzy/like match
    if not course and course_identifier:
        raw_name = str(course_identifier).strip()
        course = conn.execute(
            "SELECT id, name, description FROM courses WHERE name LIKE ? COLLATE NOCASE",
            (f"%{raw_name}%",)
        ).fetchone()

        # 3b. Token / alias matching (e.g. 'Data Structures & Algorithms' -> 'Data Structures')
        if not course:
            all_courses = conn.execute("SELECT id, name, description FROM courses ORDER BY id ASC").fetchall()
            clean_target = raw_name.lower().replace("&", "and")
            for c in all_courses:
                c_clean = c["name"].lower().replace("&", "and")
                if clean_target in c_clean or c_clean in clean_target:
                    course = c
                    break

            if not course:
                def tokenize(text):
                    return [tok for tok in text.lower().replace("&", " ").replace("(", " ").replace(")", " ").replace("/", " ").split() if len(tok) > 2]
                target_tokens = set(tokenize(raw_name))
                best_match = None
                max_overlap = 0
                for c in all_courses:
                    c_tokens = set(tokenize(c["name"]))
                    overlap = len(target_tokens & c_tokens)
                    if overlap > max_overlap:
                        max_overlap = overlap
                        best_match = c
                if best_match and max_overlap > 0:
                    course = best_match

    # 4. Fallback to first course in database
    if not course:
        course = conn.execute(
            "SELECT id, name, description FROM courses ORDER BY id ASC LIMIT 1"
        ).fetchone()

    conn.close()
    return dict(course) if course else None


def get_course_topics(course_id, db_path=None):
    """Returns all topics for a given course_id ordered by display_order."""
    conn = get_db_connection(db_path)
    rows = conn.execute(
        """
        SELECT id, course_id, name, description, display_order
        FROM topics
        WHERE course_id = ?
        ORDER BY display_order ASC, id ASC
        """,
        (course_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_course_prerequisites(course_id, db_path=None):
    """Returns all prerequisite relationships between topics within a course."""
    conn = get_db_connection(db_path)
    rows = conn.execute(
        """
        SELECT tp.topic_id, tp.prerequisite_topic_id, t_req.name AS req_name
        FROM topic_prerequisites tp
        JOIN topics t_dep ON tp.topic_id = t_dep.id
        JOIN topics t_req ON tp.prerequisite_topic_id = t_req.id
        WHERE t_dep.course_id = ?
        """,
        (course_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------
# Assessment & Progress Queries
# -------------------------------------------------------------

def get_assessment_questions(course_id, db_path=None):
    """Fetches all 20 assessment questions for a given course."""
    conn = get_db_connection(db_path)
    rows = conn.execute(
        """
        SELECT q.id, q.question_number, q.question_text,
               q.option_a, q.option_b, q.option_c, q.option_d,
               q.correct_answer, q.topic_id, t.name AS topic_name
        FROM assessment_questions q
        JOIN topics t ON q.topic_id = t.id
        WHERE q.course_id = ?
        ORDER BY q.question_number ASC
        """,
        (course_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_assessment_result(user_id, course_id, db_path=None):
    """Fetches the most recent assessment result for a student in a course."""
    if not user_id:
        return None
    conn = get_db_connection(db_path)
    row = conn.execute(
        """
        SELECT total_score, total_questions, score_percentage, topic_scores_json, submitted_at
        FROM learner_assessment_results
        WHERE user_id = ? AND course_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id, course_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def save_assessment_result(user_id, course_id, total_score, total_questions, score_percentage, topic_scores_json, db_path=None):
    """Inserts a new assessment submission result."""
    conn = get_db_connection(db_path)
    cursor = conn.execute(
        """
        INSERT INTO learner_assessment_results (
            user_id, course_id, total_score, total_questions, score_percentage, topic_scores_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, course_id, total_score, total_questions, score_percentage, topic_scores_json)
    )
    conn.commit()
    result_id = cursor.lastrowid
    conn.close()
    return result_id


def get_learner_topic_progress(user_id, course_id=None, db_path=None):
    """
    Returns a dict mapping topic_id -> status ('Completed', 'In Progress', 'Not Started')
    for a learner. If course_id is specified, filters by topics in that course.
    """
    if not user_id:
        return {}

    conn = get_db_connection(db_path)
    if course_id:
        rows = conn.execute(
            """
            SELECT p.topic_id, p.status
            FROM learner_topic_progress p
            JOIN topics t ON p.topic_id = t.id
            WHERE p.user_id = ? AND t.course_id = ?
            """,
            (user_id, course_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT topic_id, status FROM learner_topic_progress WHERE user_id = ?",
            (user_id,)
        ).fetchall()

    conn.close()
    return {r["topic_id"]: r["status"] for r in rows}


def save_topic_progress(user_id, topic_id, status, db_path=None):
    """
    Inserts or updates a topic progress entry for a student.
    Validates that the topic exists before committing.
    """
    conn = get_db_connection(db_path)
    topic = conn.execute("SELECT id FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        return False

    conn.execute(
        """
        INSERT INTO learner_topic_progress (user_id, topic_id, status, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, topic_id) DO UPDATE SET
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, topic_id, status)
    )
    conn.commit()
    conn.close()
    return True


# -------------------------------------------------------------
# Database Health Diagnostic (Absorbed from check_database.py)
# -------------------------------------------------------------

def verify_database_health(db_path=None):
    """
    Runs a full database integrity, table count, and user verification check.
    """
    target = db_path or DEFAULT_DATABASE_PATH
    exists = os.path.exists(target)
    print("=" * 60)
    print("LEARNLY DATABASE HEALTH CHECK")
    print("=" * 60)
    print("Database Path :", target)
    print("File Exists   :", exists)

    if not exists:
        print("[FAIL] Database file not found!")
        return False

    size = os.path.getsize(target)
    print(f"File Size     : {size:,} bytes")

    with open(target, "rb") as f:
        header = f.read(16)

    if header != b"SQLite format 3\x00":
        print("[FAIL] Invalid SQLite format header!")
        return False

    print("SQLite Header : [OK] Valid SQLite 3 format header detected.")

    conn = get_db_connection(target)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    table_names = [r["name"] for r in tables]

    print("\n" + "-" * 60)
    print(f"DATABASE TABLES ({len(table_names)} Total: 7 Application + 1 SQLite System)")
    print("-" * 60)

    for t in table_names:
        cnt = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        tag = "[SYSTEM]" if t == "sqlite_sequence" else "[APP]"
        print(f"  * {tag} {t:<28}: {cnt} rows")

    print("\n" + "-" * 60)
    print("COURSES & TOPICS")
    print("-" * 60)
    courses = conn.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    for c in courses:
        t_cnt = conn.execute("SELECT COUNT(*) FROM topics WHERE course_id = ?", (c["id"],)).fetchone()[0]
        q_cnt = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE course_id = ?", (c["id"],)).fetchone()[0]
        print(f"  * [{c['id']}] {c['name']}: {t_cnt} topics | {q_cnt} assessment questions")

    print("\n" + "-" * 60)
    print("REGISTERED USERS")
    print("-" * 60)
    users = conn.execute("SELECT id, name, email, created_at FROM users ORDER BY id ASC").fetchall()
    for u in users:
        print(f"  * User #{u['id']}: {u['name']} <{u['email']}> (Registered: {u['created_at']})")

    print("=" * 60)
    conn.close()
    return True


if __name__ == "__main__":
    verify_database_health()
