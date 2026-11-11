"""
database.py - Learnly Database Layer
====================================
Responsibilities:
  - SQLite database connection management (Row factory & Foreign Key PRAGMA)
  - Schema initialization & safe idempotent column migrations for all 7 tables
  - CRUD operations for users, courses, topics, prerequisites, progress,
    multi-type assessments, and topic mastery evidence
  - Database health and integrity verification
"""

import json
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


def _get_existing_columns(connection, table_name):
    rows = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    return {row["name"] for row in rows}


def _apply_migrations(connection):
    """
    Safely adds missing columns to pre-existing tables without dropping or altering data.
    """
    migrations = {
        "assessment_questions": [
            ("question_type", "TEXT NOT NULL DEFAULT 'mcq'"),
            ("code_snippet", "TEXT DEFAULT ''"),
            ("evaluation_spec_json", "TEXT DEFAULT '{}'"),
            ("question_pool", "TEXT NOT NULL DEFAULT 'initial'"),
        ],
        "learner_assessment_results": [
            ("assessment_kind", "TEXT NOT NULL DEFAULT 'initial'"),
            ("experience_level", "TEXT DEFAULT ''"),
            ("evaluation_details_json", "TEXT DEFAULT '{}'"),
            ("topic_id", "INTEGER DEFAULT NULL"),
        ],
        "learner_topic_progress": [
            ("mastery_state", "TEXT NOT NULL DEFAULT 'Unassessed'"),
            ("mastery_score", "REAL NOT NULL DEFAULT 0.0"),
            ("evidence_json", "TEXT DEFAULT '{}'"),
            ("learning_goal", "TEXT DEFAULT ''"),
            ("studied_subtopics_json", "TEXT DEFAULT '[]'"),
            ("study_completed", "INTEGER NOT NULL DEFAULT 0"),
            ("assessment_attempt_count", "INTEGER NOT NULL DEFAULT 0"),
        ],
    }

    for table_name, columns in migrations.items():
        existing = _get_existing_columns(connection, table_name)
        for col_name, col_def in columns:
            if col_name not in existing:
                connection.execute(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {col_def}'
                )


def initialize_database(db_path=None):
    """
    Initializes all 7 application tables and applies idempotent migrations if needed.
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
            option_a TEXT NOT NULL DEFAULT '',
            option_b TEXT NOT NULL DEFAULT '',
            option_c TEXT NOT NULL DEFAULT '',
            option_d TEXT NOT NULL DEFAULT '',
            correct_answer TEXT NOT NULL DEFAULT '',
            question_type TEXT NOT NULL DEFAULT 'mcq',
            code_snippet TEXT DEFAULT '',
            evaluation_spec_json TEXT DEFAULT '{}',
            question_pool TEXT NOT NULL DEFAULT 'initial',
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
            assessment_kind TEXT NOT NULL DEFAULT 'initial',
            experience_level TEXT DEFAULT '',
            evaluation_details_json TEXT DEFAULT '{}',
            topic_id INTEGER DEFAULT NULL,
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
            status TEXT NOT NULL DEFAULT 'Not Started',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            mastery_state TEXT NOT NULL DEFAULT 'Unassessed',
            mastery_score REAL NOT NULL DEFAULT 0.0,
            evidence_json TEXT DEFAULT '{}',
            learning_goal TEXT DEFAULT '',
            studied_subtopics_json TEXT DEFAULT '[]',
            study_completed INTEGER NOT NULL DEFAULT 0,
            assessment_attempt_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, topic_id),
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE
        )
    """)

    _apply_migrations(connection)
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
    Looks up a course by exact name, ID, or substring/alias.
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


def get_topic_by_id(topic_id, db_path=None):
    """Fetches a single topic row along with its parent course details."""
    conn = get_db_connection(db_path)
    row = conn.execute(
        """
        SELECT t.id, t.course_id, t.name, t.description, t.display_order,
               c.name AS course_name, c.description AS course_description
        FROM topics t
        JOIN courses c ON t.course_id = c.id
        WHERE t.id = ?
        """,
        (topic_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


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

def get_assessment_questions(course_id, pool="initial", topic_id=None, db_path=None):
    """
    Fetches assessment or topic-practice questions for a given course.
    Organizes questions by topic display order and question_number.
    """
    conn = get_db_connection(db_path)
    clauses = ["q.course_id = ?"]
    params = [course_id]

    if pool is not None:
        clauses.append("q.question_pool = ?")
        params.append(pool)

    if topic_id is not None:
        clauses.append("q.topic_id = ?")
        params.append(int(topic_id))

    where_sql = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT q.id, q.course_id, q.question_number, q.question_text,
               q.option_a, q.option_b, q.option_c, q.option_d,
               q.correct_answer, q.topic_id, t.name AS topic_name,
               t.display_order AS topic_display_order,
               q.question_type, q.code_snippet, q.evaluation_spec_json, q.question_pool
        FROM assessment_questions q
        JOIN topics t ON q.topic_id = t.id
        WHERE {where_sql}
        ORDER BY t.display_order ASC, q.question_number ASC, q.id ASC
        """,
        tuple(params)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_assessment_result(user_id, course_id, assessment_kind="initial", db_path=None):
    """
    Fetches the most recent assessment result for a student in a course.
    If assessment_kind is provided ('initial' or 'topic_practice'), filters by that kind.
    """
    if not user_id:
        return None
    conn = get_db_connection(db_path)
    if assessment_kind:
        row = conn.execute(
            """
            SELECT id, total_score, total_questions, score_percentage, topic_scores_json,
                   submitted_at, assessment_kind, experience_level, evaluation_details_json, topic_id
            FROM learner_assessment_results
            WHERE user_id = ? AND course_id = ? AND assessment_kind = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, course_id, assessment_kind)
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, total_score, total_questions, score_percentage, topic_scores_json,
                   submitted_at, assessment_kind, experience_level, evaluation_details_json, topic_id
            FROM learner_assessment_results
            WHERE user_id = ? AND course_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, course_id)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recent_assessments(user_id, course_id=None, limit=5, db_path=None):
    """Returns recent initial assessments and topic reassessments for a learner."""
    if not user_id:
        return []
    conn = get_db_connection(db_path)
    if course_id:
        rows = conn.execute(
            """
            SELECT r.id, r.course_id, c.name AS course_name, r.total_score, r.total_questions,
                   r.score_percentage, r.submitted_at, r.assessment_kind, r.experience_level,
                   r.topic_id, t.name AS topic_name
            FROM learner_assessment_results r
            JOIN courses c ON r.course_id = c.id
            LEFT JOIN topics t ON r.topic_id = t.id
            WHERE r.user_id = ? AND r.course_id = ?
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (user_id, course_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT r.id, r.course_id, c.name AS course_name, r.total_score, r.total_questions,
                   r.score_percentage, r.submitted_at, r.assessment_kind, r.experience_level,
                   r.topic_id, t.name AS topic_name
            FROM learner_assessment_results r
            JOIN courses c ON r.course_id = c.id
            LEFT JOIN topics t ON r.topic_id = t.id
            WHERE r.user_id = ?
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (user_id, limit)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_assessment_result(
    user_id,
    course_id,
    total_score,
    total_questions,
    score_percentage,
    topic_scores_json,
    assessment_kind="initial",
    experience_level="",
    evaluation_details_json="{}",
    topic_id=None,
    db_path=None,
):
    """Inserts a new initial assessment or topic practice submission result."""
    conn = get_db_connection(db_path)
    cursor = conn.execute(
        """
        INSERT INTO learner_assessment_results (
            user_id, course_id, total_score, total_questions, score_percentage,
            topic_scores_json, assessment_kind, experience_level, evaluation_details_json, topic_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            course_id,
            total_score,
            total_questions,
            score_percentage,
            topic_scores_json,
            assessment_kind,
            experience_level or "",
            evaluation_details_json or "{}",
            topic_id,
        )
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
    details = get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
    return {tid: info["status"] for tid, info in details.items()}


def get_learner_topic_progress_details(user_id, course_id=None, db_path=None):
    """
    Returns full progress & mastery metadata keyed by topic_id:
      {status, mastery_state, mastery_score, evidence, evidence_json, learning_goal, updated_at}
    """
    if not user_id:
        return {}

    conn = get_db_connection(db_path)
    if course_id:
        rows = conn.execute(
            """
            SELECT p.topic_id, p.status, p.mastery_state, p.mastery_score,
                   p.evidence_json, p.learning_goal, p.updated_at,
                   p.studied_subtopics_json, p.study_completed, p.assessment_attempt_count
            FROM learner_topic_progress p
            JOIN topics t ON p.topic_id = t.id
            WHERE p.user_id = ? AND t.course_id = ?
            """,
            (user_id, course_id)
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT topic_id, status, mastery_state, mastery_score,
                   evidence_json, learning_goal, updated_at,
                   studied_subtopics_json, study_completed, assessment_attempt_count
            FROM learner_topic_progress
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()

    conn.close()

    result = {}
    for r in rows:
        raw_ev = r["evidence_json"] or "{}"
        try:
            parsed_ev = json.loads(raw_ev)
        except Exception:
            parsed_ev = {}
        raw_subs = r["studied_subtopics_json"] or "[]"
        try:
            parsed_subs = json.loads(raw_subs)
            if not isinstance(parsed_subs, list):
                parsed_subs = []
        except Exception:
            parsed_subs = []
        result[r["topic_id"]] = {
            "status": r["status"],
            "mastery_state": r["mastery_state"] or "Unassessed",
            "mastery_score": float(r["mastery_score"] or 0.0),
            "evidence_json": raw_ev,
            "evidence": parsed_ev,
            "learning_goal": r["learning_goal"] or "",
            "studied_subtopics": parsed_subs,
            "study_completed": bool(r["study_completed"]),
            "assessment_attempt_count": int(r["assessment_attempt_count"] or 0),
            "updated_at": r["updated_at"],
        }
    return result


def save_topic_progress(
    user_id,
    topic_id,
    status,
    mastery_state=None,
    mastery_score=None,
    evidence_json=None,
    learning_goal=None,
    studied_subtopics_json=None,
    study_completed=None,
    assessment_attempt_count=None,
    db_path=None,
):
    """
    Inserts or updates a topic progress entry for a student.
    Preserves existing mastery fields if None is passed for optional columns.
    Never upgrades mastery_state to Proficient or status to Completed without an assessment.
    """
    conn = get_db_connection(db_path)
    topic = conn.execute("SELECT id FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic:
        conn.close()
        return False

    existing = conn.execute(
        """
        SELECT status, mastery_state, mastery_score, evidence_json, learning_goal,
               studied_subtopics_json, study_completed, assessment_attempt_count
        FROM learner_topic_progress
        WHERE user_id = ? AND topic_id = ?
        """,
        (user_id, topic_id)
    ).fetchone()

    if existing:
        final_mastery_state = mastery_state if mastery_state is not None else existing["mastery_state"]
        final_mastery_score = float(mastery_score) if mastery_score is not None else float(existing["mastery_score"] or 0.0)
        final_evidence_json = evidence_json if evidence_json is not None else (existing["evidence_json"] or "{}")
        final_goal = learning_goal if learning_goal is not None else (existing["learning_goal"] or "")
        final_subs_json = studied_subtopics_json if studied_subtopics_json is not None else (existing["studied_subtopics_json"] or "[]")
        final_study_done = int(bool(study_completed)) if study_completed is not None else int(existing["study_completed"] or 0)
        final_attempts = int(assessment_attempt_count) if assessment_attempt_count is not None else int(existing["assessment_attempt_count"] or 0)

        # Enforce completion rule: Completed requires Proficient / Verified Mastery mastery state
        safe_status = status
        if safe_status == "Completed" and final_mastery_state not in ("Proficient", "Verified Mastery"):
            safe_status = "Needs Attention" if final_mastery_state == "Needs Attention" else (
                "Assessment Pending" if final_study_done else "In Progress"
            )

        conn.execute(
            """
            UPDATE learner_topic_progress
            SET status = ?,
                mastery_state = ?,
                mastery_score = ?,
                evidence_json = ?,
                learning_goal = ?,
                studied_subtopics_json = ?,
                study_completed = ?,
                assessment_attempt_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND topic_id = ?
            """,
            (
                safe_status,
                final_mastery_state,
                final_mastery_score,
                final_evidence_json,
                final_goal,
                final_subs_json,
                final_study_done,
                final_attempts,
                user_id,
                topic_id,
            )
        )
    else:
        final_mastery_state = mastery_state if mastery_state is not None else (
            "Developing" if status == "In Progress" else ("Needs Attention" if status == "Needs Attention" else "Unassessed")
        )
        final_mastery_score = float(mastery_score) if mastery_score is not None else 0.0
        final_evidence_json = evidence_json or "{}"
        final_goal = learning_goal or ""
        final_subs_json = studied_subtopics_json or "[]"
        final_study_done = int(bool(study_completed)) if study_completed is not None else 0
        final_attempts = int(assessment_attempt_count) if assessment_attempt_count is not None else 0

        safe_status = status
        if safe_status == "Completed" and final_mastery_state not in ("Proficient", "Verified Mastery"):
            safe_status = "Assessment Pending" if final_study_done else "In Progress"

        conn.execute(
            """
            INSERT INTO learner_topic_progress (
                user_id, topic_id, status, mastery_state, mastery_score,
                evidence_json, learning_goal, studied_subtopics_json,
                study_completed, assessment_attempt_count, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                user_id,
                topic_id,
                safe_status,
                final_mastery_state,
                final_mastery_score,
                final_evidence_json,
                final_goal,
                final_subs_json,
                final_study_done,
                final_attempts,
            )
        )

    conn.commit()
    conn.close()
    return True


def save_subtopic_study_progress(user_id, topic_id, subtopic_id, total_subtopics, db_path=None):
    """
    Records that the learner has opened/studied a specific subtopic.
    Never sets topic status to Completed; marks 'In Progress' or 'Assessment Pending'.
    """
    if not user_id:
        return None

    conn = get_db_connection(db_path)
    existing = conn.execute(
        """
        SELECT status, mastery_state, studied_subtopics_json, study_completed
        FROM learner_topic_progress
        WHERE user_id = ? AND topic_id = ?
        """,
        (user_id, topic_id),
    ).fetchone()

    studied = []
    m_state = "Unassessed"
    if existing:
        m_state = existing["mastery_state"] or "Unassessed"
        try:
            parsed = json.loads(existing["studied_subtopics_json"] or "[]")
            if isinstance(parsed, list):
                studied = parsed
        except Exception:
            studied = []

    sub_str = str(subtopic_id or "").strip()
    if sub_str and sub_str not in studied:
        studied.append(sub_str)

    all_done = bool(total_subtopics > 0 and len(studied) >= total_subtopics) or bool(existing and existing["study_completed"])

    if m_state in ("Proficient", "Verified Mastery"):
        new_status = "Completed"
    elif m_state == "Needs Attention":
        new_status = "Needs Attention"
    elif all_done:
        new_status = "Assessment Pending"
    else:
        new_status = "In Progress"

    conn.close()

    save_topic_progress(
        user_id=user_id,
        topic_id=topic_id,
        status=new_status,
        studied_subtopics_json=json.dumps(studied),
        study_completed=1 if all_done else 0,
        db_path=db_path,
    )

    return {
        "studied_subtopics": studied,
        "studied_count": len(studied),
        "total_subtopics": total_subtopics,
        "study_completed": all_done,
        "status": new_status,
    }


def get_topic_mastery_attempts(user_id, topic_id, db_path=None):
    """Returns prior topic_mastery assessment results for a learner and topic."""
    if not user_id:
        return []
    conn = get_db_connection(db_path)
    rows = conn.execute(
        """
        SELECT id, total_score, total_questions, score_percentage,
               evaluation_details_json, submitted_at
        FROM learner_assessment_results
        WHERE user_id = ? AND topic_id = ? AND assessment_kind = 'topic_mastery'
        ORDER BY submitted_at DESC, id DESC
        """,
        (user_id, topic_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# -------------------------------------------------------------
# Database Health Diagnostic
# -------------------------------------------------------------

def verify_database_health(db_path=None):
    """
    Runs a full database integrity, table count, question pool, and user verification check.
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
    print("COURSES & QUESTION POOLS")
    print("-" * 60)
    courses = conn.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    for c in courses:
        t_cnt = conn.execute("SELECT COUNT(*) FROM topics WHERE course_id = ?", (c["id"],)).fetchone()[0]
        q_init = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE course_id = ? AND question_pool = 'initial'", (c["id"],)).fetchone()[0]
        q_prac = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE course_id = ? AND question_pool = 'practice'", (c["id"],)).fetchone()[0]
        print(f"  * [{c['id']}] {c['name']}: {t_cnt} topics | initial: {q_init} Qs | practice: {q_prac} Qs")

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
