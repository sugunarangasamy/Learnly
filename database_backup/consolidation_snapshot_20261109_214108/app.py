
import os
import sqlite3
import json

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify,
    g
)
from werkzeug.security import generate_password_hash, check_password_hash

import knowledge_map_algorithm


app = Flask(__name__)

# Development fallback only. Set LEARNLY_SECRET_KEY before deployment.
app.secret_key = os.environ.get(
    "LEARNLY_SECRET_KEY",
    "change-this-development-key-before-deployment"
)

DATABASE = os.path.join(app.root_path, "learnly.db")


def get_db_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_db_connection()

    connection.execute("PRAGMA foreign_keys = ON;")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS courses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT
        )
    """)

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

    connection.execute("""
        CREATE TABLE IF NOT EXISTS topic_prerequisites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            prerequisite_topic_id INTEGER NOT NULL,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE,
            FOREIGN KEY (prerequisite_topic_id) REFERENCES topics (id) ON DELETE CASCADE,
            UNIQUE (topic_id, prerequisite_topic_id)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS learner_topic_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'Not Started',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (topic_id) REFERENCES topics (id) ON DELETE CASCADE,
            UNIQUE (user_id, topic_id)
        )
    """)

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

    connection.commit()

    # Ensure assessment questions are seeded if table is empty
    q_count = connection.execute("SELECT COUNT(*) FROM assessment_questions").fetchone()[0]
    if q_count == 0:
        connection.close()
        try:
            import seed_assessment_questions
            seed_assessment_questions.seed_assessment_questions()
        except Exception as e:
            print("Auto-seeding assessment questions note:", e)
        return

    connection.close()


initialize_database()


def render_home(error=None, auth_mode="login"):
    return render_template(
        "index.html",
        error=error,
        auth_mode=auth_mode
    )


@app.route("/")
def home():
    user_id = session.get("user_id")
    if not user_id:
        return render_home()

    user_progress = get_student_overall_progress(user_id=user_id)
    return render_template("index.html", user_progress=user_progress)


@app.route("/signup", methods=["POST"])
def signup():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not name or len(name) > 60:
        return render_home(
            "Enter a name containing 1 to 60 characters.",
            "signup"
        )

    if len(email) > 254 or "@" not in email:
        return render_home(
            "Enter a valid email address.",
            "signup"
        )

    if len(password) < 8:
        return render_home(
            "Your password must contain at least 8 characters.",
            "signup"
        )

    password_hash = generate_password_hash(password)

    connection = get_db_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO users (name, email, password_hash)
            VALUES (?, ?, ?)
            """,
            (name, email, password_hash)
        )
        connection.commit()
        user_id = cursor.lastrowid

    except sqlite3.IntegrityError:
        connection.close()
        return render_home(
            "An account with this email already exists. Please log in.",
            "login"
        )

    connection.close()

    session.clear()
    session["user_id"] = user_id
    session["user_name"] = name
    session["user_email"] = email

    return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    connection = get_db_connection()

    user = connection.execute(
        """
        SELECT id, name, email, password_hash
        FROM users
        WHERE email = ?
        """,
        (email,)
    ).fetchone()

    connection.close()

    if user is None or not check_password_hash(
        user["password_hash"], password
    ):
        return render_home(
            "Incorrect email or password.",
            "login"
        )

    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_email"] = user["email"]

    return redirect(url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


def find_course(connection, course_identifier):
    """
    Finds a course by ID, exact name, or flexible substring/alias match.
    Ensures seamless matching between 'Data Structures & Algorithms' and 'Data Structures',
    'AI/ML' and 'Artificial Intelligence and Machine Learning (AI/ML)', etc.
    Falls back gracefully to the first course if not found.
    """
    if not course_identifier:
        return connection.execute(
            "SELECT id, name, description FROM courses ORDER BY id ASC LIMIT 1"
        ).fetchone()

    # Match by numeric ID
    if isinstance(course_identifier, int) or (isinstance(course_identifier, str) and course_identifier.strip().isdigit()):
        course = connection.execute(
            "SELECT id, name, description FROM courses WHERE id = ?",
            (int(course_identifier),)
        ).fetchone()
        if course:
            return course

    raw_name = str(course_identifier).strip()

    # Exact match (case insensitive)
    course = connection.execute(
        "SELECT id, name, description FROM courses WHERE name = ? COLLATE NOCASE",
        (raw_name,)
    ).fetchone()
    if course:
        return course

    # Flexible normalized comparison
    all_courses = connection.execute(
        "SELECT id, name, description FROM courses ORDER BY id ASC"
    ).fetchall()

    clean_target = raw_name.lower().replace("&", "and")

    # Direct substring checks
    for c in all_courses:
        c_clean = c["name"].lower().replace("&", "and")
        if clean_target in c_clean or c_clean in clean_target:
            return c

    # Token overlap check
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
        return best_match

    return all_courses[0] if all_courses else None


def get_course_learning_path(course_name, user_id=None):
    """
    Fetches real topics, prerequisites, and learner progress for a given course.
    Computes dynamic status: Completed, In Progress, Available, or Locked based on prerequisite satisfaction.
    """
    connection = get_db_connection()

    # Find the course with flexible matching
    course = find_course(connection, course_name)

    if not course:
        connection.close()
        return None

    course_id = course["id"]

    # Fetch all topics for this course
    topics_rows = connection.execute(
        """
        SELECT id, name, description, display_order
        FROM topics
        WHERE course_id = ?
        ORDER BY display_order ASC, id ASC
        """,
        (course_id,)
    ).fetchall()

    # Fetch all prerequisites between topics in this course
    prereqs_rows = connection.execute(
        """
        SELECT tp.topic_id, tp.prerequisite_topic_id, t_req.name AS req_name
        FROM topic_prerequisites tp
        JOIN topics t_dep ON tp.topic_id = t_dep.id
        JOIN topics t_req ON tp.prerequisite_topic_id = t_req.id
        WHERE t_dep.course_id = ?
        """,
        (course_id,)
    ).fetchall()

    # Fetch learner's saved progress if logged in
    learner_status_map = {}
    if user_id:
        progress_rows = connection.execute(
            """
            SELECT topic_id, status
            FROM learner_topic_progress
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchall()
        for p in progress_rows:
            learner_status_map[p["topic_id"]] = p["status"]

    connection.close()

    # Build topic lookup & prerequisite graphs
    topics_dict = {}
    prereq_ids_map = {t["id"]: [] for t in topics_rows}
    prereq_names_map = {t["id"]: [] for t in topics_rows}

    for pr in prereqs_rows:
        tid = pr["topic_id"]
        if tid in prereq_ids_map:
            prereq_ids_map[tid].append(pr["prerequisite_topic_id"])
            prereq_names_map[tid].append(pr["req_name"])

    # Determine real-time status for each topic
    # Completed: recorded as 'Completed'
    # In Progress: recorded as 'In Progress'
    # Available: all prerequisites are Completed (or no prerequisites)
    # Locked: has at least one prerequisite not yet Completed
    completed_topic_ids = {
        tid for tid, stat in learner_status_map.items() if stat == "Completed"
    }

    topics_list = []
    completed_count = 0
    in_progress_count = 0
    available_count = 0
    locked_count = 0

    for idx, t in enumerate(topics_rows):
        tid = t["id"]
        raw_status = learner_status_map.get(tid, "Not Started")

        if raw_status == "Completed":
            computed_status = "Completed"
        elif raw_status == "In Progress":
            computed_status = "In Progress"
        else:
            # Check if all prerequisites are completed
            prereqs = prereq_ids_map.get(tid, [])
            if not prereqs or all(req_id in completed_topic_ids for req_id in prereqs):
                computed_status = "Available"
            else:
                computed_status = "Locked"

        if computed_status == "Completed":
            completed_count += 1
        elif computed_status == "In Progress":
            in_progress_count += 1
        elif computed_status == "Available":
            available_count += 1
        else:
            locked_count += 1

        topic_data = {
            "id": tid,
            "name": t["name"],
            "description": t["description"] or "",
            "display_order": t["display_order"],
            "status": computed_status,
            "prerequisites": prereq_names_map.get(tid, []),
            "prerequisite_ids": prereq_ids_map.get(tid, [])
        }
        topics_list.append(topic_data)

    total_topics = len(topics_list)
    progress_percentage = int((completed_count / total_topics * 100)) if total_topics > 0 else 0

    # Calculate dynamic graph layout using pure Python algorithm
    graph_layout = knowledge_map_algorithm.calculate_graph_layout(topics_list, prereqs_rows)

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "course_description": course["description"] or "",
        "topics": topics_list,
        "graph": graph_layout,
        "stats": {
            "total": total_topics,
            "completed": completed_count,
            "in_progress": in_progress_count,
            "available": available_count,
            "locked": locked_count,
            "percentage": progress_percentage
        }
    }


@app.route("/module3")
@app.route("/learning-path")
def module3():
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    connection = get_db_connection()
    all_courses = connection.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    connection.close()

    path_data = get_course_learning_path(course_name, user_id=user_id)
    return render_template(
        "module3.html",
        course=course_name,
        all_courses=all_courses,
        path_data=path_data,
        is_logged_in=bool(user_id)
    )


def get_course_skill_gaps(course_name, user_id=None):
    """
    Module 4: Fetches topic performance, detects skill gaps,
    and identifies missing prerequisite skills for a given course.
    """
    connection = get_db_connection()
    course = find_course(connection, course_name)
    if not course:
        connection.close()
        return None

    course_id = course["id"]

    topics_rows = connection.execute(
        """
        SELECT id, name, description, display_order
        FROM topics
        WHERE course_id = ?
        ORDER BY display_order ASC, id ASC
        """,
        (course_id,)
    ).fetchall()
    topics = [dict(r) for r in topics_rows]

    prereqs_rows = connection.execute(
        """
        SELECT tp.topic_id, tp.prerequisite_topic_id
        FROM topic_prerequisites tp
        JOIN topics t ON tp.topic_id = t.id
        WHERE t.course_id = ?
        """,
        (course_id,)
    ).fetchall()
    prereqs = [dict(r) for r in prereqs_rows]

    topic_scores = {}
    latest_assessment = None

    if user_id:
        latest_row = connection.execute(
            """
            SELECT total_score, total_questions, score_percentage, topic_scores_json, submitted_at
            FROM learner_assessment_results
            WHERE user_id = ? AND course_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, course_id)
        ).fetchone()

        if latest_row:
            latest_assessment = {
                "total_score": latest_row["total_score"],
                "total_questions": latest_row["total_questions"],
                "score_percentage": latest_row["score_percentage"],
                "submitted_at": latest_row["submitted_at"]
            }
            try:
                raw_json = latest_row["topic_scores_json"]
                topic_scores = json.loads(raw_json) if raw_json else {}
            except Exception:
                topic_scores = {}

    connection.close()

    # Get topic statuses (Completed, In Progress, Available, Locked)
    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    skill_gap_report = knowledge_map_algorithm.detect_skill_gaps(
        topics=topics,
        topic_prerequisites=prereqs,
        topic_scores=topic_scores,
        topic_statuses=topic_statuses
    )

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "course_description": course["description"] or "",
        "latest_assessment": latest_assessment,
        "skill_gaps": skill_gap_report,
        "stats": path_data.get("stats", {}) if path_data else {}
    }


@app.route("/module4")
@app.route("/skill-gap")
def module4():
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    connection = get_db_connection()
    all_courses = connection.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    connection.close()

    gap_data = get_course_skill_gaps(course_name, user_id=user_id)
    return render_template(
        "module4.html",
        course=course_name,
        all_courses=all_courses,
        gap_data=gap_data,
        is_logged_in=bool(user_id)
    )


@app.route("/api/skill-gap")
def api_skill_gap():
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    data = get_course_skill_gaps(course_name, user_id=user_id)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


def get_personalized_learning_path(course_name, user_id=None, goal="Full Curriculum"):
    """
    Module 5: Generates a personalized step-by-step learning sequence
    based on topological prerequisite ordering, skill gaps, and learner goals.
    """
    connection = get_db_connection()
    course = find_course(connection, course_name)
    if not course:
        connection.close()
        return None

    course_id = course["id"]

    topics_rows = connection.execute(
        """
        SELECT id, name, description, display_order
        FROM topics
        WHERE course_id = ?
        ORDER BY display_order ASC, id ASC
        """,
        (course_id,)
    ).fetchall()
    topics = [dict(r) for r in topics_rows]

    prereqs_rows = connection.execute(
        """
        SELECT tp.topic_id, tp.prerequisite_topic_id
        FROM topic_prerequisites tp
        JOIN topics t ON tp.topic_id = t.id
        WHERE t.course_id = ?
        """,
        (course_id,)
    ).fetchall()
    prereqs = [dict(r) for r in prereqs_rows]

    topic_scores = {}
    latest_assessment = None

    if user_id:
        latest_row = connection.execute(
            """
            SELECT total_score, total_questions, score_percentage, topic_scores_json, submitted_at
            FROM learner_assessment_results
            WHERE user_id = ? AND course_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, course_id)
        ).fetchone()

        if latest_row:
            latest_assessment = {
                "total_score": latest_row["total_score"],
                "total_questions": latest_row["total_questions"],
                "score_percentage": latest_row["score_percentage"],
                "submitted_at": latest_row["submitted_at"]
            }
            try:
                raw_json = latest_row["topic_scores_json"]
                topic_scores = json.loads(raw_json) if raw_json else {}
            except Exception:
                topic_scores = {}

    connection.close()

    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    personalized_data = knowledge_map_algorithm.generate_personalized_learning_path(
        topics=topics,
        topic_prerequisites=prereqs,
        topic_scores=topic_scores,
        topic_statuses=topic_statuses,
        goal=goal
    )

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "course_description": course["description"] or "",
        "latest_assessment": latest_assessment,
        "learning_path": personalized_data
    }


@app.route("/module5")
@app.route("/personalized-path")
def module5():
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal", "Full Curriculum")
    user_id = session.get("user_id")

    connection = get_db_connection()
    all_courses = connection.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    connection.close()

    path_result = get_personalized_learning_path(course_name, user_id=user_id, goal=goal)
    return render_template(
        "module5.html",
        course=course_name,
        goal=goal,
        all_courses=all_courses,
        path_result=path_result,
        is_logged_in=bool(user_id)
    )


@app.route("/api/personalized-path")
def api_personalized_path():
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal", "Full Curriculum")
    user_id = session.get("user_id")

    data = get_personalized_learning_path(course_name, user_id=user_id, goal=goal)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


@app.route("/api/learning-path")
def api_learning_path():
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    data = get_course_learning_path(course_name, user_id=user_id)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


def get_student_overall_progress(user_id=None):
    """
    Module 6: Computes overall progress, completed topics,
    course-by-course analytics, and adaptive next steps for a student.
    """
    connection = get_db_connection()
    courses = connection.execute("SELECT id, name, description FROM courses ORDER BY id ASC").fetchall()

    user_info = None
    if user_id:
        user_info = connection.execute(
            "SELECT id, name, email, created_at FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    total_topics_all = 0
    total_completed_all = 0
    total_in_progress_all = 0
    active_courses_count = 0

    courses_analytics = []

    for c in courses:
        cid = c["id"]
        cname = c["name"]

        topics = connection.execute(
            "SELECT id, name, display_order FROM topics WHERE course_id = ? ORDER BY display_order ASC",
            (cid,)
        ).fetchall()
        t_count = len(topics)
        total_topics_all += t_count

        completed_in_course = 0
        in_progress_in_course = 0
        learner_map = {}

        if user_id:
            progress_rows = connection.execute(
                """
                SELECT p.topic_id, p.status, t.name AS topic_name
                FROM learner_topic_progress p
                JOIN topics t ON p.topic_id = t.id
                WHERE p.user_id = ? AND t.course_id = ?
                """,
                (user_id, cid)
            ).fetchall()

            for pr in progress_rows:
                learner_map[pr["topic_id"]] = pr["status"]
                if pr["status"] == "Completed":
                    completed_in_course += 1
                elif pr["status"] == "In Progress":
                    in_progress_in_course += 1

        total_completed_all += completed_in_course
        total_in_progress_all += in_progress_in_course

        latest_assessment = None
        if user_id:
            latest_row = connection.execute(
                """
                SELECT total_score, total_questions, score_percentage, submitted_at
                FROM learner_assessment_results
                WHERE user_id = ? AND course_id = ?
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, cid)
            ).fetchone()
            if latest_row:
                latest_assessment = {
                    "total_score": latest_row["total_score"],
                    "total_questions": latest_row["total_questions"],
                    "score_percentage": latest_row["score_percentage"],
                    "submitted_at": latest_row["submitted_at"]
                }

        course_pct = int((completed_in_course / t_count * 100)) if t_count > 0 else 0
        is_active = (completed_in_course > 0 or in_progress_in_course > 0 or latest_assessment is not None)
        if is_active:
            active_courses_count += 1

        c_prereqs = connection.execute(
            """
            SELECT tp.topic_id, tp.prerequisite_topic_id
            FROM topic_prerequisites tp
            JOIN topics t ON tp.topic_id = t.id
            WHERE t.course_id = ?
            """,
            (cid,)
        ).fetchall()

        adaptation_eval = knowledge_map_algorithm.evaluate_adaptation(
            topics=[dict(t) for t in topics],
            topic_prerequisites=[dict(p) for p in c_prereqs],
            learner_status_map=learner_map
        )

        courses_analytics.append({
            "id": cid,
            "name": cname,
            "description": c["description"] or "",
            "total_topics": t_count,
            "completed_topics": completed_in_course,
            "in_progress_topics": in_progress_in_course,
            "percentage": course_pct,
            "is_active": is_active,
            "latest_assessment": latest_assessment,
            "next_focus": adaptation_eval.get("next_focus"),
            "available_topics": adaptation_eval.get("available_topics", []),
            "locked_topics": adaptation_eval.get("locked_topics", [])
        })

    connection.close()

    overall_pct = int((total_completed_all / total_topics_all * 100)) if total_topics_all > 0 else 0

    return {
        "user": dict(user_info) if user_info else None,
        "summary": {
            "total_topics_all": total_topics_all,
            "total_completed_all": total_completed_all,
            "total_in_progress_all": total_in_progress_all,
            "overall_percentage": overall_pct,
            "active_courses_count": active_courses_count,
            "total_courses_count": len(courses)
        },
        "courses": courses_analytics
    }


@app.route("/module6")
@app.route("/progress")
def module6():
    user_id = session.get("user_id")
    connection = get_db_connection()
    all_courses = connection.execute("SELECT id, name FROM courses ORDER BY id ASC").fetchall()
    connection.close()

    progress_data = get_student_overall_progress(user_id=user_id)
    return render_template(
        "module6.html",
        all_courses=all_courses,
        progress_data=progress_data,
        is_logged_in=bool(user_id)
    )


@app.route("/api/progress")
def api_progress():
    user_id = session.get("user_id")
    data = get_student_overall_progress(user_id=user_id)
    return jsonify(data)


@app.route("/api/topic-progress", methods=["POST"])
def api_topic_progress():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({
            "error": "Authentication required",
            "message": "Please log in to save and update your learning progress."
        }), 401

    payload = request.get_json(silent=True) or request.form
    topic_id = payload.get("topic_id")
    new_status = payload.get("status")

    if not topic_id or new_status not in ["Not Started", "In Progress", "Completed"]:
        return jsonify({"error": "Invalid topic_id or status"}), 400

    connection = get_db_connection()
    connection.execute("PRAGMA foreign_keys = ON;")

    # Verify topic exists
    topic = connection.execute(
        "SELECT id, course_id FROM topics WHERE id = ?", (topic_id,)
    ).fetchone()

    if not topic:
        connection.close()
        return jsonify({"error": "Topic not found"}), 404

    connection.execute(
        """
        INSERT INTO learner_topic_progress (user_id, topic_id, status, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, topic_id) DO UPDATE SET
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
        """,
        (user_id, topic_id, new_status)
    )
    connection.commit()

    # Dynamic adaptation: evaluate downstream unlocks
    adaptation = None
    if new_status == "Completed":
        prog_rows = connection.execute(
            "SELECT topic_id, status FROM learner_topic_progress WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        l_status_map = {r["topic_id"]: r["status"] for r in prog_rows}

        c_topics = connection.execute(
            "SELECT id, name FROM topics WHERE course_id = ?", (topic["course_id"],)
        ).fetchall()
        c_prereqs = connection.execute(
            """
            SELECT tp.topic_id, tp.prerequisite_topic_id
            FROM topic_prerequisites tp
            JOIN topics t ON tp.topic_id = t.id
            WHERE t.course_id = ?
            """,
            (topic["course_id"],)
        ).fetchall()

        adaptation = knowledge_map_algorithm.evaluate_adaptation(
            topics=[dict(t) for t in c_topics],
            topic_prerequisites=[dict(p) for p in c_prereqs],
            learner_status_map=l_status_map,
            updated_topic_id=int(topic_id)
        )

    connection.close()

    return jsonify({
        "success": True,
        "user_id": user_id,
        "topic_id": topic_id,
        "status": new_status,
        "adaptation": adaptation
    })


@app.route("/api/assessment-questions")
def api_assessment_questions():
    course_name = request.args.get("course", "Python Programming")

    connection = get_db_connection()
    course = find_course(connection, course_name)

    if not course:
        connection.close()
        return jsonify({"error": "Course not found", "questions": []}), 404

    course_id = course["id"]

    rows = connection.execute(
        """
        SELECT q.id, q.question_number, q.question_text,
               q.option_a, q.option_b, q.option_c, q.option_d,
               q.topic_id, t.name AS topic_name
        FROM assessment_questions q
        JOIN topics t ON q.topic_id = t.id
        WHERE q.course_id = ?
        ORDER BY q.question_number ASC
        """,
        (course_id,)
    ).fetchall()

    connection.close()

    questions = []
    for r in rows:
        questions.append({
            "id": r["id"],
            "number": r["question_number"],
            "text": r["question_text"],
            "options": {
                "A": r["option_a"],
                "B": r["option_b"],
                "C": r["option_c"],
                "D": r["option_d"]
            },
            "topic_id": r["topic_id"],
            "topic_name": r["topic_name"]
        })

    return jsonify({
        "course_id": course_id,
        "course_name": course["name"],
        "total_questions": len(questions),
        "questions": questions
    })


@app.route("/api/assessment-submit", methods=["POST"])
def api_assessment_submit():
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or request.form
    course_name = payload.get("course_name", "Python Programming")
    user_answers = payload.get("answers", {})

    connection = get_db_connection()
    connection.execute("PRAGMA foreign_keys = ON;")

    # Find course with flexible matching
    course = find_course(connection, course_name)

    if not course:
        connection.close()
        return jsonify({"error": "Course not found"}), 404

    course_id = course["id"]

    # Fetch real questions with correct answers
    q_rows = connection.execute(
        """
        SELECT q.id, q.question_number, q.question_text,
               q.correct_answer, q.topic_id, t.name AS topic_name
        FROM assessment_questions q
        JOIN topics t ON q.topic_id = t.id
        WHERE q.course_id = ?
        ORDER BY q.question_number ASC
        """,
        (course_id,)
    ).fetchall()

    questions = [dict(r) for r in q_rows]

    # Fetch topics and prerequisites for this course
    topics_rows = connection.execute(
        "SELECT id, name, description, display_order FROM topics WHERE course_id = ?",
        (course_id,)
    ).fetchall()
    topics = [dict(r) for r in topics_rows]

    prereq_rows = connection.execute(
        """
        SELECT tp.topic_id, tp.prerequisite_topic_id
        FROM topic_prerequisites tp
        JOIN topics t ON tp.topic_id = t.id
        WHERE t.course_id = ?
        """,
        (course_id,)
    ).fetchall()
    topic_prerequisites = [dict(r) for r in prereq_rows]

    # Execute the Knowledge Map Generation Algorithm
    result = knowledge_map_algorithm.generate_knowledge_map(
        course_id=course_id,
        questions=questions,
        user_answers=user_answers,
        topics=topics,
        topic_prerequisites=topic_prerequisites
    )

    # If learner is logged in, save assessment result and update learner_topic_progress
    if user_id:
        topic_scores_json = json.dumps(result["topic_scores"])
        connection.execute(
            """
            INSERT INTO learner_assessment_results (
                user_id, course_id, total_score, total_questions, score_percentage, topic_scores_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                course_id,
                result["total_correct"],
                result["total_questions"],
                result["overall_percentage"],
                topic_scores_json
            )
        )

        # Update topic progress for learner according to computed statuses
        for topic_id, computed_status in result["topic_statuses"].items():
            db_status = computed_status if computed_status in ["Completed", "In Progress"] else "Not Started"
            connection.execute(
                """
                INSERT INTO learner_topic_progress (user_id, topic_id, status, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, topic_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, topic_id, db_status)
            )

        connection.commit()

    connection.close()

    return jsonify({
        "success": True,
        "is_logged_in": bool(user_id),
        "course_id": course_id,
        "course_name": course["name"],
        "total_correct": result["total_correct"],
        "total_questions": result["total_questions"],
        "overall_percentage": result["overall_percentage"],
        "topic_scores": result["topic_scores"],
        "topic_statuses": result["topic_statuses"],
        "skill_gaps": result.get("skill_gaps"),
        "redirect_url": url_for("module4", course=course["name"])
    })


if __name__ == "__main__":
    import threading
    import webbrowser

    port = int(os.environ.get("PORT", 5000))
    print(f"\n==========================================")
    print(f" Learnly Web Server running at:")
    print(f" -> http://127.0.0.1:{port}/")
    print(f" -> http://localhost:{port}/")
    print(f"==========================================\n")

    # Automatically open browser once server starts
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        threading.Timer(1.2, lambda: webbrowser.open_new(f"http://127.0.0.1:{port}/")).start()

    app.run(host="0.0.0.0", port=port, debug=True)


