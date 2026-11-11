"""
app.py - Learnly Main Application & Route Controller
====================================================
Responsibilities:
  - Flask application configuration and session management
  - Route routing for all 6 modules (Dashboard, Assessment, Knowledge Graph,
    Skill Gaps, Personalized Path, Progress Tracking)
  - Public JSON API endpoints for AJAX interactions
  - Request orchestration across database, assessment, and learning_engine layers
"""

import os
import json
import sqlite3
import threading
import webbrowser

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

import database
import learning_engine
import assessment


app = Flask(__name__)

# Secret key configuration (environment variable or development fallback)
app.secret_key = os.environ.get(
    "LEARNLY_SECRET_KEY",
    "change-this-development-key-before-deployment"
)

DATABASE = os.environ.get("LEARNLY_DATABASE") or os.path.join(app.root_path, "learnly.db")


def get_db_path():
    """Returns the active database path (allows isolated test databases via app.config)."""
    return app.config.get("DATABASE") or DATABASE


def get_db_connection():
    """Creates an active SQLite connection to the active database (with Row factory & FK enabled)."""
    return database.get_db_connection(get_db_path())


def find_course(connection, course_identifier):
    """Compatibility bridge: delegates to database.find_course."""
    return database.find_course(course_identifier, db_path=get_db_path())


# Initialize database schema and verify seed data on startup
database.initialize_database(DATABASE)


# -------------------------------------------------------------
# Core Helper / Orchestration Functions
# -------------------------------------------------------------

def render_home(error=None, auth_mode="login"):
    """Renders the landing page with login/signup tabs."""
    return render_template(
        "index.html",
        error=error,
        auth_mode=auth_mode
    )


def get_course_learning_path(course_name, user_id=None):
    """
    Module 3: Knowledge Graph Orchestration.
    Fetches topics, prerequisites, and learner progress; builds graph layout.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    learner_status_map = {}
    if user_id:
        learner_status_map = database.get_learner_topic_progress(user_id, course_id=course_id, db_path=db_path)

    return learning_engine.build_prerequisite_graph(
        course_name=course["name"],
        topics_rows=topics,
        prereqs_rows=prereqs,
        learner_status_map=learner_status_map,
        course_id=course_id,
        course_description=course["description"] or ""
    )


def get_course_skill_gaps(course_name, user_id=None):
    """
    Module 4: Skill-Gap Analysis Orchestration.
    Analyzes assessment scores and prerequisite chains to detect unmastered competencies.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    topic_scores = {}
    latest_assessment = None

    if user_id:
        latest_row = database.get_latest_assessment_result(user_id, course_id, db_path=db_path)
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

    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    skill_gap_report = learning_engine.detect_skill_gaps(
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


def get_personalized_learning_path(course_name, user_id=None, goal="Full Curriculum"):
    """
    Module 5: Personalized Learning Path Orchestration.
    Applies Kahn's topological sort and goal filtering to produce ordered curriculum steps.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    topic_scores = {}
    latest_assessment = None

    if user_id:
        latest_row = database.get_latest_assessment_result(user_id, course_id, db_path=db_path)
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

    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    personalized_data = learning_engine.generate_personalized_learning_path(
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


def get_student_overall_progress(user_id=None):
    """
    Module 6: Progress Tracking and Learning Adaptation Orchestration.
    Computes overall progress, completed topics, course analytics, and adaptive next steps.
    """
    db_path = get_db_path()
    courses = database.get_all_courses(db_path=db_path)
    user_info = database.get_user_by_id(user_id, db_path=db_path) if user_id else None

    total_topics_all = 0
    total_completed_all = 0
    total_in_progress_all = 0
    active_courses_count = 0

    courses_analytics = []

    for c in courses:
        cid = c["id"]
        cname = c["name"]
        topics = database.get_course_topics(cid, db_path=db_path)
        t_count = len(topics)
        total_topics_all += t_count

        learner_map = {}
        completed_in_course = 0
        in_progress_in_course = 0

        if user_id:
            learner_map = database.get_learner_topic_progress(user_id, course_id=cid, db_path=db_path)
            for status in learner_map.values():
                if status == "Completed":
                    completed_in_course += 1
                elif status == "In Progress":
                    in_progress_in_course += 1

        total_completed_all += completed_in_course
        total_in_progress_all += in_progress_in_course

        latest_assessment = None
        if user_id:
            latest_row = database.get_latest_assessment_result(user_id, cid, db_path=db_path)
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

        c_prereqs = database.get_course_prerequisites(cid, db_path=db_path)
        adaptation_eval = learning_engine.evaluate_adaptation(
            topics=topics,
            topic_prerequisites=c_prereqs,
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


# -------------------------------------------------------------
# Authentication & Navigation Routes (Module 1)
# -------------------------------------------------------------

@app.route("/")
def home():
    """Module 1: Dashboard for authenticated learners, or Login/Signup form."""
    user_id = session.get("user_id")
    if not user_id:
        return render_home()

    user_progress = get_student_overall_progress(user_id=user_id)
    return render_template("index.html", user_progress=user_progress)


@app.route("/signup", methods=["POST"])
def signup():
    """Registers a new learner account with hashed password."""
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not name or len(name) > 60:
        return render_home("Enter a name containing 1 to 60 characters.", "signup")

    if len(email) > 254 or "@" not in email:
        return render_home("Enter a valid email address.", "signup")

    if len(password) < 8:
        return render_home("Your password must contain at least 8 characters.", "signup")

    password_hash = generate_password_hash(password)

    try:
        user_id = database.create_user(name, email, password_hash, db_path=get_db_path())
    except sqlite3.IntegrityError:
        return render_home("An account with this email already exists. Please log in.", "login")

    session.clear()
    session["user_id"] = user_id
    session["user_name"] = name
    session["user_email"] = email

    return redirect(url_for("home"))


@app.route("/login", methods=["POST"])
def login():
    """Authenticates learner credentials and initializes session."""
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    user = database.get_user_by_email(email, db_path=get_db_path())

    if user is None or not check_password_hash(user["password_hash"], password):
        return render_home("Incorrect email or password.", "login")

    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_email"] = user["email"]

    return redirect(url_for("home"))


@app.route("/logout", methods=["POST"])
def logout():
    """Clears learner session and returns to login view."""
    session.clear()
    return redirect(url_for("home"))


# -------------------------------------------------------------
# Module 3: Knowledge Graph Views & APIs
# -------------------------------------------------------------

@app.route("/module3")
@app.route("/learning-path")
def module3():
    """Module 3: Visual Knowledge Graph and prerequisite flow map."""
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    all_courses = database.get_all_courses(db_path=get_db_path())
    path_data = get_course_learning_path(course_name, user_id=user_id)

    return render_template(
        "module3.html",
        course=course_name,
        all_courses=all_courses,
        path_data=path_data,
        is_logged_in=bool(user_id)
    )


@app.route("/api/learning-path")
def api_learning_path():
    """Returns knowledge graph data as JSON."""
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    data = get_course_learning_path(course_name, user_id=user_id)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


# -------------------------------------------------------------
# Module 4: Skill-Gap Analysis Views & APIs
# -------------------------------------------------------------

@app.route("/module4")
@app.route("/skill-gap")
def module4():
    """Module 4: Diagnostic Skill Gaps and remedial action plan."""
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    all_courses = database.get_all_courses(db_path=get_db_path())
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
    """Returns skill gap diagnostics as JSON."""
    course_name = request.args.get("course", "Python Programming")
    user_id = session.get("user_id")

    data = get_course_skill_gaps(course_name, user_id=user_id)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


# -------------------------------------------------------------
# Module 5: Personalized Learning Path Views & APIs
# -------------------------------------------------------------

@app.route("/module5")
@app.route("/personalized-path")
def module5():
    """Module 5: Adaptive sequential curriculum path."""
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal", "Full Curriculum")
    user_id = session.get("user_id")

    all_courses = database.get_all_courses(db_path=get_db_path())
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
    """Returns personalized topological learning path as JSON."""
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal", "Full Curriculum")
    user_id = session.get("user_id")

    data = get_personalized_learning_path(course_name, user_id=user_id, goal=goal)
    if not data:
        return jsonify({"error": "Course not found"}), 404

    return jsonify(data)


# -------------------------------------------------------------
# Module 6: Progress Tracking & Adaptation Views & APIs
# -------------------------------------------------------------

@app.route("/module6")
@app.route("/progress")
def module6():
    """Module 6: Progress Dashboard and learning adaptation."""
    user_id = session.get("user_id")
    all_courses = database.get_all_courses(db_path=get_db_path())
    progress_data = get_student_overall_progress(user_id=user_id)

    return render_template(
        "module6.html",
        all_courses=all_courses,
        progress_data=progress_data,
        is_logged_in=bool(user_id)
    )


@app.route("/api/progress")
def api_progress():
    """Returns overall student progress metrics as JSON."""
    user_id = session.get("user_id")
    data = get_student_overall_progress(user_id=user_id)
    return jsonify(data)


@app.route("/api/topic-progress", methods=["POST"])
def api_topic_progress():
    """
    Updates status for a topic ('Completed', 'In Progress', 'Not Started').
    Triggers automated downstream prerequisite unlocking evaluation.
    """
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

    db_path = get_db_path()
    conn = database.get_db_connection(db_path)
    topic = conn.execute("SELECT id, course_id FROM topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()

    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    database.save_topic_progress(user_id, int(topic_id), new_status, db_path=db_path)

    # Evaluate dynamic unlocks if marked Completed
    adaptation = None
    if new_status == "Completed":
        cid = topic["course_id"]
        l_status_map = database.get_learner_topic_progress(user_id, course_id=cid, db_path=db_path)
        c_topics = database.get_course_topics(cid, db_path=db_path)
        c_prereqs = database.get_course_prerequisites(cid, db_path=db_path)
        adaptation = learning_engine.evaluate_adaptation(
            topics=c_topics,
            topic_prerequisites=c_prereqs,
            learner_status_map=l_status_map,
            updated_topic_id=int(topic_id)
        )

    return jsonify({
        "success": True,
        "user_id": user_id,
        "topic_id": topic_id,
        "status": new_status,
        "adaptation": adaptation
    })


# -------------------------------------------------------------
# Module 2: Assessment Delivery & Scoring APIs
# -------------------------------------------------------------

@app.route("/api/assessment-questions")
def api_assessment_questions():
    """
    Fetches the 20 diagnostic questions for a course.
    Safeguard: Stretches and strips 'correct_answer' before returning JSON.
    """
    course_name = request.args.get("course", "Python Programming")
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)

    if not course:
        return jsonify({"error": "Course not found", "questions": []}), 404

    course_id = course["id"]
    questions = database.get_assessment_questions(course_id, db_path=db_path)
    client_questions = assessment.sanitize_questions_for_client(questions)

    return jsonify({
        "course_id": course_id,
        "course_name": course["name"],
        "total_questions": len(client_questions),
        "questions": client_questions
    })


@app.route("/api/assessment-submit", methods=["POST"])
def api_assessment_submit():
    """
    Evaluates assessment responses, computes topic breakdown, saves result,
    updates learner progress, and redirects to Module 4 (Skill Gaps).
    """
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or request.form
    course_name = payload.get("course_name", "Python Programming")
    user_answers = payload.get("answers", {})

    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)

    if not course:
        return jsonify({"error": "Course not found"}), 404

    course_id = course["id"]
    questions = database.get_assessment_questions(course_id, db_path=db_path)
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    # Execute Knowledge Map & Scoring algorithm
    result = learning_engine.generate_knowledge_map(
        course_id=course_id,
        questions=questions,
        user_answers=user_answers,
        topics=topics,
        topic_prerequisites=prereqs
    )

    # Persist results if learner is logged in
    if user_id:
        topic_scores_json = json.dumps(result["topic_scores"])
        database.save_assessment_result(
            user_id=user_id,
            course_id=course_id,
            total_score=result["total_correct"],
            total_questions=result["total_questions"],
            score_percentage=result["overall_percentage"],
            topic_scores_json=topic_scores_json,
            db_path=db_path
        )

        for topic_id, computed_status in result["topic_statuses"].items():
            db_status = computed_status if computed_status in ["Completed", "In Progress"] else "Not Started"
            database.save_topic_progress(
                user_id=user_id,
                topic_id=int(topic_id),
                status=db_status,
                db_path=db_path
            )

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


# -------------------------------------------------------------
# Development Server Startup
# -------------------------------------------------------------

if __name__ == "__main__":
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
