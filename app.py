"""
app.py - Learnly Main Application & Route Controller
====================================================
Responsibilities:
  - Flask application configuration and session management
  - Routing for all 6 modules + Continuous Topic Practice & Reassessment
  - Safe JSON API endpoints (no answer leakage prior to submission)
  - Orchestration across database, assessment, and learning_engine layers
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
    jsonify,
)
from werkzeug.security import generate_password_hash, check_password_hash

import database
import learning_engine
import assessment


app = Flask(__name__)

# Secret key configuration (environment variable or development fallback)
app.secret_key = os.environ.get(
    "LEARNLY_SECRET_KEY",
    "change-this-development-key-before-deployment",
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


def _safe_startup_init():
    """Initializes schema & upgrades legacy MCQ-only DB once if not running under test isolation."""
    if os.environ.get("LEARNLY_SKIP_STARTUP_UPGRADE") == "1":
        return
    database.initialize_database(DATABASE)
    assessment.upgrade_question_bank_if_needed(DATABASE)


_safe_startup_init()


# -------------------------------------------------------------
# Core Helper / Orchestration Functions
# -------------------------------------------------------------

def render_home(error=None, auth_mode="login"):
    """Renders the landing page with login/signup tabs."""
    return render_template(
        "index.html",
        error=error,
        auth_mode=auth_mode,
    )


def _load_topic_scores_and_progress(user_id, course_id, db_path):
    """
    Loads latest initial assessment topic breakdown along with live per-topic
    progress & updated mastery evidence from topic practice/reassessment.
    """
    topic_scores = {}
    latest_assessment = None

    if user_id:
        latest_row = database.get_latest_assessment_result(
            user_id, course_id, assessment_kind=None, db_path=db_path
        )
        # Prefer latest 'initial' assessment for baseline topic scores if available
        initial_row = database.get_latest_assessment_result(
            user_id, course_id, assessment_kind="initial", db_path=db_path
        )
        base_row = initial_row or latest_row
        if base_row:
            latest_assessment = {
                "total_score": base_row["total_score"],
                "total_questions": base_row["total_questions"],
                "score_percentage": base_row["score_percentage"],
                "submitted_at": base_row["submitted_at"],
                "experience_level": base_row.get("experience_level", ""),
            }
            try:
                raw_json = base_row["topic_scores_json"]
                topic_scores = json.loads(raw_json) if raw_json else {}
            except Exception:
                topic_scores = {}

    learner_progress = (
        database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        if user_id else {}
    )
    return topic_scores, latest_assessment, learner_progress


def get_course_learning_path(course_name, user_id=None):
    """
    Module 3: Knowledge Graph Orchestration.
    Fetches topics, prerequisites, and mastery/progress; builds graph layout.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    learner_progress = (
        database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        if user_id else {}
    )
    learner_status_map = {tid: info["status"] for tid, info in learner_progress.items()}

    return learning_engine.build_prerequisite_graph(
        course_name=course["name"],
        topics_rows=topics,
        prereqs_rows=prereqs,
        learner_status_map=learner_status_map,
        learner_mastery_map=learner_progress,
        course_id=course_id,
        course_description=course["description"] or "",
    )


def get_course_skill_gaps(course_name, user_id=None):
    """
    Module 4: Multi-Dimensional Skill-Gap Analysis Orchestration.
    Evaluates conceptual, code comprehension, coding practice, confidence
    calibration, and missing prerequisites.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    topic_scores, latest_assessment, learner_progress = _load_topic_scores_and_progress(
        user_id, course_id, db_path
    )

    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    skill_gap_report = learning_engine.detect_skill_gaps(
        topics=topics,
        topic_prerequisites=prereqs,
        topic_scores=topic_scores,
        topic_statuses=topic_statuses,
        topic_mastery_map=learner_progress,
    )

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "course_description": course["description"] or "",
        "latest_assessment": latest_assessment,
        "skill_gaps": skill_gap_report,
        "stats": path_data.get("stats", {}) if path_data else {},
    }


def get_personalized_learning_path(course_name, user_id=None, goal=None):
    """
    Module 5: Adaptive Personalized Learning Path Orchestration.
    Combines prerequisite graph, mastery states, topic practice updates,
    and student goals without fixed percentage cuts.
    """
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)
    if not course:
        return None

    course_id = course["id"]
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    topic_scores, latest_assessment, learner_progress = _load_topic_scores_and_progress(
        user_id, course_id, db_path
    )

    resolved_goal = goal
    if not resolved_goal:
        # Check saved goal in progress/assessment if available
        for info in learner_progress.values():
            if info.get("learning_goal"):
                resolved_goal = info["learning_goal"]
                break
        if not resolved_goal and latest_assessment and latest_assessment.get("experience_level"):
            resolved_goal = latest_assessment["experience_level"]
        if not resolved_goal:
            resolved_goal = "Balanced Path"

    path_data = get_course_learning_path(course["name"], user_id=user_id)
    topic_statuses = {t["id"]: t["status"] for t in path_data["topics"]} if path_data else {}

    personalized_data = learning_engine.generate_personalized_learning_path(
        topics=topics,
        topic_prerequisites=prereqs,
        topic_scores=topic_scores,
        topic_statuses=topic_statuses,
        topic_mastery_map=learner_progress,
        goal=resolved_goal,
    )

    return {
        "course_id": course_id,
        "course_name": course["name"],
        "course_description": course["description"] or "",
        "latest_assessment": latest_assessment,
        "learning_path": personalized_data,
    }


def get_student_overall_progress(user_id=None):
    """
    Module 6: Progress Tracking and Learning Adaptation Orchestration.
    Computes multi-state mastery counts, completed topics, course analytics,
    and next recommended focus for a learner.
    """
    db_path = get_db_path()
    courses = database.get_all_courses(db_path=db_path)
    user_info = database.get_user_by_id(user_id, db_path=db_path) if user_id else None

    total_topics_all = 0
    total_completed_all = 0
    total_in_progress_all = 0
    verified_mastery_all = 0
    proficient_all = 0
    developing_all = 0
    needs_attention_all = 0
    active_courses_count = 0

    courses_analytics = []

    for c in courses:
        cid = c["id"]
        cname = c["name"]
        topics = database.get_course_topics(cid, db_path=db_path)
        t_count = len(topics)
        total_topics_all += t_count

        learner_progress = (
            database.get_learner_topic_progress_details(user_id, course_id=cid, db_path=db_path)
            if user_id else {}
        )
        learner_map = {tid: info["status"] for tid, info in learner_progress.items()}

        completed_in_course = 0
        in_progress_in_course = 0
        verified_in_course = 0

        for t in topics:
            tid = t["id"]
            info = learner_progress.get(tid) or {}
            status = info.get("status", "Not Started")
            m_state = info.get("mastery_state", "Unassessed")
            studied_subs = info.get("studied_subtopics") or []
            study_done = bool(info.get("study_completed"))

            if m_state in learning_engine.PREREQ_SATISFYING_STATES:
                completed_in_course += 1
            elif study_done or studied_subs or status == "In Progress" or m_state == "Developing":
                in_progress_in_course += 1

            if m_state == "Verified Mastery":
                verified_mastery_all += 1
                verified_in_course += 1
            elif m_state == "Proficient":
                proficient_all += 1
            elif m_state == "Developing":
                developing_all += 1
            elif m_state == "Needs Attention":
                needs_attention_all += 1

        total_completed_all += completed_in_course
        total_in_progress_all += in_progress_in_course

        latest_assessment = None
        if user_id:
            latest_row = database.get_latest_assessment_result(
                user_id, cid, assessment_kind="initial", db_path=db_path
            ) or database.get_latest_assessment_result(user_id, cid, assessment_kind=None, db_path=db_path)
            if latest_row:
                latest_assessment = {
                    "total_score": latest_row["total_score"],
                    "total_questions": latest_row["total_questions"],
                    "score_percentage": latest_row["score_percentage"],
                    "submitted_at": latest_row["submitted_at"],
                }

        course_pct = int((completed_in_course / t_count * 100)) if t_count > 0 else 0
        is_active = (
            completed_in_course > 0
            or in_progress_in_course > 0
            or any(info.get("mastery_state") != "Unassessed" for info in learner_progress.values())
            or latest_assessment is not None
        )
        if is_active:
            active_courses_count += 1

        c_prereqs = database.get_course_prerequisites(cid, db_path=db_path)
        adaptation_eval = learning_engine.evaluate_adaptation(
            topics=topics,
            topic_prerequisites=c_prereqs,
            learner_status_map=learner_map,
            learner_mastery_map=learner_progress,
        )

        courses_analytics.append({
            "id": cid,
            "name": cname,
            "description": c["description"] or "",
            "total_topics": t_count,
            "completed_topics": completed_in_course,
            "verified_topics": verified_in_course,
            "in_progress_topics": in_progress_in_course,
            "percentage": course_pct,
            "is_active": is_active,
            "latest_assessment": latest_assessment,
            "next_focus": adaptation_eval.get("next_focus"),
            "next_focus_id": adaptation_eval.get("next_focus_id"),
            "available_topics": adaptation_eval.get("available_topics", []),
            "locked_topics": adaptation_eval.get("locked_topics", []),
        })

    overall_pct = int((total_completed_all / total_topics_all * 100)) if total_topics_all > 0 else 0
    recent_activity = database.get_recent_assessments(user_id, limit=5, db_path=db_path) if user_id else []

    return {
        "user": dict(user_info) if user_info else None,
        "summary": {
            "total_topics_all": total_topics_all,
            "total_completed_all": total_completed_all,
            "total_in_progress_all": total_in_progress_all,
            "verified_mastery_all": verified_mastery_all,
            "proficient_all": proficient_all,
            "developing_all": developing_all,
            "needs_attention_all": needs_attention_all,
            "overall_percentage": overall_pct,
            "active_courses_count": active_courses_count,
            "total_courses_count": len(courses),
        },
        "courses": courses_analytics,
        "recent_activity": recent_activity,
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
        is_logged_in=bool(user_id),
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
        is_logged_in=bool(user_id),
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
    """Module 5: Adaptive personalized learning sequence."""
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal")
    user_id = session.get("user_id")

    all_courses = database.get_all_courses(db_path=get_db_path())
    path_result = get_personalized_learning_path(course_name, user_id=user_id, goal=goal)
    effective_goal = (
        path_result["learning_path"]["goal"]
        if path_result and path_result.get("learning_path")
        else (goal or "Balanced Path")
    )

    return render_template(
        "module5.html",
        course=course_name,
        goal=effective_goal,
        all_courses=all_courses,
        path_result=path_result,
        is_logged_in=bool(user_id),
    )


@app.route("/api/personalized-path")
def api_personalized_path():
    """Returns personalized topological learning path as JSON."""
    course_name = request.args.get("course", "Python Programming")
    goal = request.args.get("goal")
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
        is_logged_in=bool(user_id),
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
            "message": "Please log in to save and update your learning progress.",
        }), 401

    payload = request.get_json(silent=True) or request.form
    topic_id = payload.get("topic_id")
    new_status = payload.get("status")

    if new_status == "Completed":
        return jsonify({
            "error": "Topic completion requires passing the 5-question Mastery Assessment (at least 4/5).",
        }), 400

    if not topic_id or new_status not in ["Not Started", "In Progress", "Assessment Pending"]:
        return jsonify({"error": "Invalid topic_id or status"}), 400

    db_path = get_db_path()
    conn = database.get_db_connection(db_path)
    topic = conn.execute("SELECT id, course_id FROM topics WHERE id = ?", (topic_id,)).fetchone()
    conn.close()

    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    database.save_topic_progress(user_id, int(topic_id), new_status, db_path=db_path)

    # Evaluate dynamic unlocks and updated next focus
    adaptation = None
    cid = topic["course_id"]
    l_progress = database.get_learner_topic_progress_details(user_id, course_id=cid, db_path=db_path)
    l_status_map = {tid: info["status"] for tid, info in l_progress.items()}
    c_topics = database.get_course_topics(cid, db_path=db_path)
    c_prereqs = database.get_course_prerequisites(cid, db_path=db_path)
    adaptation = learning_engine.evaluate_adaptation(
        topics=c_topics,
        topic_prerequisites=c_prereqs,
        learner_status_map=l_status_map,
        learner_mastery_map=l_progress,
        updated_topic_id=int(topic_id),
    )

    return jsonify({
        "success": True,
        "user_id": user_id,
        "topic_id": topic_id,
        "status": new_status,
        "adaptation": adaptation,
    })


# -------------------------------------------------------------
# Module 2: Initial Assessment Delivery & Evaluation APIs
# -------------------------------------------------------------

@app.route("/api/assessment-questions")
def api_assessment_questions():
    """
    Fetches the mixed-type comprehensive initial assessment questions for a course,
    organized cleanly by topic. Strips answers & test cases before sending to client.
    """
    course_name = request.args.get("course", "Python Programming")
    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)

    if not course:
        return jsonify({"error": "Course not found", "questions": []}), 404

    course_id = course["id"]
    questions = database.get_assessment_questions(course_id, pool="initial", db_path=db_path)
    client_questions = assessment.sanitize_questions_for_client(questions)

    return jsonify({
        "course_id": course_id,
        "course_name": course["name"],
        "total_questions": len(client_questions),
        "questions": client_questions,
    })


@app.route("/api/assessment-submit", methods=["POST"])
def api_assessment_submit():
    """
    Evaluates all question types objectively (MCQ, Predict Output, Debug Code, Write Code)
    along with confidence & consistency calibration, persists multi-state mastery evidence,
    and prepares adaptive skill gaps & learning path.
    """
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or request.form
    course_name = payload.get("course_name", "Python Programming")
    user_answers = payload.get("answers", {})
    user_confidences = payload.get("confidences", {})
    experience_level = payload.get("experience_level", "")
    learning_goal = payload.get("learning_goal") or experience_level or ""

    db_path = get_db_path()
    course = database.find_course(course_name, db_path=db_path)

    if not course:
        return jsonify({"error": "Course not found"}), 404

    course_id = course["id"]
    questions = database.get_assessment_questions(course_id, pool="initial", db_path=db_path)
    topics = database.get_course_topics(course_id, db_path=db_path)
    prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

    result = learning_engine.generate_knowledge_map(
        course_id=course_id,
        questions=questions,
        user_answers=user_answers,
        topics=topics,
        topic_prerequisites=prereqs,
        user_confidences=user_confidences,
    )

    if user_id:
        topic_scores_json = json.dumps(result["topic_scores"])
        eval_details_json = json.dumps({
            "topic_mastery": result["topic_mastery"],
            "learning_goal": learning_goal,
        })
        database.save_assessment_result(
            user_id=user_id,
            course_id=course_id,
            total_score=result["total_correct"],
            total_questions=result["total_questions"],
            score_percentage=result["overall_percentage"],
            topic_scores_json=topic_scores_json,
            assessment_kind="initial",
            experience_level=learning_goal,
            evaluation_details_json=eval_details_json,
            db_path=db_path,
        )

        for t_id, computed_status in result["topic_statuses"].items():
            db_status = computed_status if computed_status in ("Completed", "In Progress", "Needs Attention", "Assessment Pending") else "Not Started"
            t_score_info = result["topic_scores"].get(t_id) or result["topic_scores"].get(str(t_id)) or {}
            m_state = t_score_info.get("mastery_state", "Unassessed")
            m_score = float(t_score_info.get("mastery_score", 0.0))
            ev_json = json.dumps(t_score_info.get("evidence", {}))

            database.save_topic_progress(
                user_id=user_id,
                topic_id=int(t_id),
                status=db_status,
                mastery_state=m_state,
                mastery_score=m_score,
                evidence_json=ev_json,
                learning_goal=learning_goal,
                db_path=db_path,
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
        "topic_mastery": result["topic_mastery"],
        "topic_statuses": result["topic_statuses"],
        "skill_gaps": result.get("skill_gaps"),
        "learning_goal": learning_goal,
        "redirect_url": url_for("module5", course=course["name"]),
    })


# -------------------------------------------------------------
# Continuous Learning Cycle: Topic Study, Practice & Reassessment
# -------------------------------------------------------------

def _build_topic_practice_context(topic_id, user_id):
    db_path = get_db_path()
    topic = database.get_topic_by_id(topic_id, db_path=db_path)
    if not topic:
        return None

    course_id = topic["course_id"]
    course_name = topic["course_name"]
    practice_rows = database.get_assessment_questions(
        course_id, pool="practice", topic_id=topic_id, db_path=db_path
    )
    if not practice_rows:
        practice_rows = database.get_assessment_questions(
            course_id, pool="initial", topic_id=topic_id, db_path=db_path
        )

    client_questions = assessment.sanitize_questions_for_client(practice_rows)
    study_notes = assessment.get_topic_study_notes(topic["name"])

    path_data = get_course_learning_path(course_name, user_id=user_id)
    topic_entry = next((t for t in path_data["topics"] if t["id"] == topic_id), None) if path_data else None

    return {
        "topic": topic,
        "course_name": course_name,
        "study_notes": study_notes,
        "topic_state": topic_entry or {
            "id": topic_id,
            "name": topic["name"],
            "mastery_state": "Unassessed",
            "mastery_score": 0.0,
            "status": "Not Started",
            "studied_subtopics": [],
            "study_completed": False,
            "assessment_attempt_count": 0,
            "missing_prerequisites": [],
            "prerequisites": [],
        },
        "questions": client_questions,
        "total_questions": len(client_questions),
    }


@app.route("/study")
@app.route("/practice")
@app.route("/topic-practice")
def topic_practice():
    """Topic Study view: displays topic overview, subtopics, learning content, and references."""
    user_id = session.get("user_id")
    db_path = get_db_path()
    raw_topic_id = request.args.get("topic_id")
    course_name = request.args.get("course", "Python Programming")
    initial_subtopic = request.args.get("subtopic", "")

    topic_id = None
    if raw_topic_id and str(raw_topic_id).isdigit():
        topic_id = int(raw_topic_id)
    else:
        # Resolve first recommended focus topic for this course
        lp = get_personalized_learning_path(course_name, user_id=user_id)
        if lp and lp.get("learning_path"):
            cf = lp["learning_path"].get("current_focus")
            if cf:
                topic_id = cf["topic_id"]
            elif lp["learning_path"].get("sequence"):
                topic_id = lp["learning_path"]["sequence"][0]["topic_id"]
        if not topic_id:
            course = database.find_course(course_name, db_path=db_path)
            if course:
                topics = database.get_course_topics(course["id"], db_path=db_path)
                if topics:
                    topic_id = topics[0]["id"]

    ctx = _build_topic_practice_context(topic_id, user_id) if topic_id else None
    all_courses = database.get_all_courses(db_path=db_path)

    return render_template(
        "practice.html",
        ctx=ctx,
        course=ctx["course_name"] if ctx else course_name,
        all_courses=all_courses,
        is_logged_in=bool(user_id),
        initial_subtopic=initial_subtopic,
    )


@app.route("/api/topic-practice")
def api_topic_practice():
    """Returns concise study guide + sanitized practice questions for a topic (no answer leak)."""
    raw_topic_id = request.args.get("topic_id")
    if not raw_topic_id or not str(raw_topic_id).isdigit():
        return jsonify({"error": "Valid topic_id required"}), 400

    ctx = _build_topic_practice_context(int(raw_topic_id), session.get("user_id"))
    if not ctx:
        return jsonify({"error": "Topic not found"}), 404

    return jsonify(ctx)


@app.route("/api/topic-practice-submit", methods=["POST"])
def api_topic_practice_submit():
    """
    Evaluates topic practice answers objectively, merges evidence into existing topic
    mastery record, updates progress, unlocks downstream topics, and recalculates
    the next recommended topic — without requiring a full course reassessment.
    """
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or request.form
    raw_topic_id = payload.get("topic_id")
    user_answers = payload.get("answers", {})
    user_confidences = payload.get("confidences", {})

    if not raw_topic_id or not str(raw_topic_id).isdigit():
        return jsonify({"error": "Valid topic_id required"}), 400

    topic_id = int(raw_topic_id)
    db_path = get_db_path()
    topic = database.get_topic_by_id(topic_id, db_path=db_path)
    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    course_id = topic["course_id"]
    course_name = topic["course_name"]

    practice_rows = database.get_assessment_questions(
        course_id, pool="practice", topic_id=topic_id, db_path=db_path
    )
    if not practice_rows:
        practice_rows = database.get_assessment_questions(
            course_id, pool="initial", topic_id=topic_id, db_path=db_path
        )

    eval_result = assessment.calculate_topic_scores(
        practice_rows, user_answers, user_confidences=user_confidences
    )
    topic_eval = eval_result["topic_scores"].get(topic_id) or {}
    new_items = eval_result.get("item_results", [])

    # Merge with prior topic evidence if logged in
    previous_mastery = "Unassessed"
    updated_summary = topic_eval.get("evidence") or assessment.compute_topic_mastery_summary(new_items)

    if user_id:
        existing_map = database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        prev_info = existing_map.get(topic_id) or {}
        previous_mastery = prev_info.get("mastery_state", "Unassessed")
        existing_ev = prev_info.get("evidence") or {}

        updated_summary = assessment.merge_topic_evidence(existing_ev, new_items)
        new_mastery_state = updated_summary["mastery_state"]
        new_mastery_score = updated_summary["mastery_score"]
        new_status = "Completed" if new_mastery_state == "Verified Mastery" else "In Progress"

        database.save_topic_progress(
            user_id=user_id,
            topic_id=topic_id,
            status=new_status,
            mastery_state=new_mastery_state,
            mastery_score=new_mastery_score,
            evidence_json=json.dumps(updated_summary),
            db_path=db_path,
        )

        database.save_assessment_result(
            user_id=user_id,
            course_id=course_id,
            total_score=eval_result["total_correct"],
            total_questions=eval_result["total_questions"],
            score_percentage=eval_result["overall_percentage"],
            topic_scores_json=json.dumps(eval_result["topic_scores"]),
            assessment_kind="topic_practice",
            evaluation_details_json=json.dumps(updated_summary),
            topic_id=topic_id,
            db_path=db_path,
        )

        l_progress = database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        l_status_map = {tid: info["status"] for tid, info in l_progress.items()}
        c_topics = database.get_course_topics(course_id, db_path=db_path)
        c_prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

        adaptation = learning_engine.evaluate_adaptation(
            topics=c_topics,
            topic_prerequisites=c_prereqs,
            learner_status_map=l_status_map,
            learner_mastery_map=l_progress,
            updated_topic_id=topic_id,
        )
        recalculated_path = get_personalized_learning_path(course_name, user_id=user_id)
        next_focus = (
            recalculated_path["learning_path"].get("current_focus")
            if recalculated_path and recalculated_path.get("learning_path")
            else None
        )
    else:
        adaptation = None
        next_focus = None

    return jsonify({
        "success": True,
        "is_logged_in": bool(user_id),
        "topic_id": topic_id,
        "topic_name": topic["name"],
        "course_name": course_name,
        "practice_correct": eval_result["total_correct"],
        "practice_total": eval_result["total_questions"],
        "practice_percentage": eval_result["overall_percentage"],
        "previous_mastery": previous_mastery,
        "updated_mastery": updated_summary["mastery_state"],
        "mastery_score": updated_summary["mastery_score"],
        "dimensions": updated_summary["dimensions"],
        "recommendation": updated_summary["recommendation"],
        "item_results": new_items,
        "adaptation": adaptation,
        "next_focus": next_focus,
        "learning_path_url": url_for("module5", course=course_name),
        "progress_url": url_for("module6"),
    })


# -------------------------------------------------------------
# Topic Study Tracking & 5-Question Mastery Assessment APIs
# -------------------------------------------------------------

@app.route("/api/study-subtopic", methods=["POST"])
def api_study_subtopic():
    """
    Persists that the logged-in learner opened/studied a subtopic.
    Never marks a topic Completed; advances status to 'In Progress' or 'Assessment Pending'.
    """
    payload = request.get_json(silent=True) or request.form
    raw_topic_id = payload.get("topic_id")
    subtopic_id = payload.get("subtopic_id", "")

    if not raw_topic_id or not str(raw_topic_id).isdigit():
        return jsonify({"error": "Valid topic_id required"}), 400

    topic_id = int(raw_topic_id)
    db_path = get_db_path()
    topic = database.get_topic_by_id(topic_id, db_path=db_path)
    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    notes = assessment.get_topic_study_notes(topic["name"])
    total_subtopics = len(notes.get("subtopics") or [])
    user_id = session.get("user_id")

    if not user_id:
        return jsonify({
            "success": True,
            "is_logged_in": False,
            "topic_id": topic_id,
            "total_subtopics": total_subtopics,
        })

    prog = database.save_subtopic_study_progress(
        user_id=user_id,
        topic_id=topic_id,
        subtopic_id=subtopic_id,
        total_subtopics=total_subtopics,
        db_path=db_path,
    )
    return jsonify({
        "success": True,
        "is_logged_in": True,
        "topic_id": topic_id,
        **(prog or {}),
    })


def _select_mastery_pool(user_id, topic_id, retake=False, last_set="", db_path=None):
    """Selects 'mastery_1' or 'mastery_2' ensuring retakes receive a fresh set of questions."""
    if retake and last_set in ("mastery_1", "mastery_2"):
        return "mastery_2" if last_set == "mastery_1" else "mastery_1"

    attempts = 0
    if user_id:
        progress_map = database.get_learner_topic_progress_details(user_id, db_path=db_path)
        info = progress_map.get(topic_id) or {}
        attempts = int(info.get("assessment_attempt_count", 0) or 0)
        if attempts == 0:
            attempts = len(database.get_topic_mastery_attempts(user_id, topic_id, db_path=db_path))

    if retake:
        attempts += 1
    return "mastery_2" if (attempts % 2 == 1) else "mastery_1"


@app.route("/api/mastery-assessment")
def api_mastery_assessment():
    """
    Delivers the 5-question Topic Mastery Assessment (1 MCQ, 1 Predict Output,
    1 Find & Correct Error, 1 Fill in the Blanks, 1 Write Small Code).
    Supports ?retake=1&last_set=mastery_1|mastery_2 to serve a fresh question set.
    """
    raw_topic_id = request.args.get("topic_id")
    if not raw_topic_id or not str(raw_topic_id).isdigit():
        return jsonify({"error": "Valid topic_id required"}), 400

    topic_id = int(raw_topic_id)
    db_path = get_db_path()
    topic = database.get_topic_by_id(topic_id, db_path=db_path)
    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    user_id = session.get("user_id")
    retake = str(request.args.get("retake", "0")).strip().lower() in ("1", "true", "yes")
    last_set = str(request.args.get("last_set", "")).strip()

    chosen_pool = _select_mastery_pool(user_id, topic_id, retake=retake, last_set=last_set, db_path=db_path)
    rows = database.get_assessment_questions(topic["course_id"], pool=chosen_pool, topic_id=topic_id, db_path=db_path)
    if not rows:
        chosen_pool = "mastery_1"
        rows = database.get_assessment_questions(topic["course_id"], pool=chosen_pool, topic_id=topic_id, db_path=db_path)

    client_qs = assessment.sanitize_questions_for_client(rows)
    return jsonify({
        "topic_id": topic_id,
        "topic_name": topic["name"],
        "course_name": topic["course_name"],
        "question_set": chosen_pool,
        "total_questions": len(client_qs),
        "pass_score": 4,
        "questions": client_qs,
    })


@app.route("/api/mastery-assessment-submit", methods=["POST"])
def api_mastery_assessment_submit():
    """
    Evaluates the 5-question Topic Mastery Assessment (1 mark each, passing >= 4/5 = 80%).
    Marks topic Completed (Proficient) only when score >= 4/5; unlocks prerequisites;
    on fail (< 4/5) identifies weak concepts, subtopics to revise, and allows fresh retake.
    """
    user_id = session.get("user_id")
    payload = request.get_json(silent=True) or request.form
    raw_topic_id = payload.get("topic_id")
    q_set = str(payload.get("question_set") or "mastery_1").strip()
    user_answers = payload.get("answers", {})

    if not raw_topic_id or not str(raw_topic_id).isdigit():
        return jsonify({"error": "Valid topic_id required"}), 400

    topic_id = int(raw_topic_id)
    db_path = get_db_path()
    topic = database.get_topic_by_id(topic_id, db_path=db_path)
    if not topic:
        return jsonify({"error": "Topic not found"}), 404

    if q_set not in ("mastery_1", "mastery_2"):
        q_set = "mastery_1"

    rows = database.get_assessment_questions(topic["course_id"], pool=q_set, topic_id=topic_id, db_path=db_path)
    if not rows:
        return jsonify({"error": "Mastery assessment questions unavailable"}), 404

    # Enforce Rule 3: All 5 questions must be attempted before submission
    missing = []
    for idx, q in enumerate(rows, start=1):
        qid_str = str(q["id"])
        qnum_str = str(q["question_number"])
        val = ""
        if isinstance(user_answers, dict):
            if qid_str in user_answers:
                val = user_answers[qid_str]
            elif q["id"] in user_answers:
                val = user_answers[q["id"]]
            elif qnum_str in user_answers:
                val = user_answers[qnum_str]
        if not str(val or "").strip():
            missing.append(idx)

    if missing:
        return jsonify({
            "error": f"Please attempt all {len(rows)} questions before submitting (missing question {', '.join(map(str, missing))}).",
            "missing_questions": missing,
        }), 400

    eval_out = assessment.evaluate_topic_mastery_assessment(topic["name"], rows, user_answers)
    course_id = topic["course_id"]
    course_name = topic["course_name"]

    adaptation = None
    next_focus = None
    course_progress_pct = None

    if user_id:
        prev_map = database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        prev_info = prev_map.get(topic_id) or {}
        prev_attempts = int(prev_info.get("assessment_attempt_count", 0) or 0)
        new_attempts = prev_attempts + 1

        notes = assessment.get_topic_study_notes(topic["name"])
        all_subs = [s["id"] for s in (notes.get("subtopics") or [])]
        studied_json = json.dumps(prev_info.get("studied_subtopics") or all_subs)

        # Keep Proficient/Completed if already previously passed? Or strictly reflect mastery result:
        # Passing (>=4/5) earns Proficient + Completed; failing sets Needs Attention + incomplete unless already proficient
        database.save_topic_progress(
            user_id=user_id,
            topic_id=topic_id,
            status=eval_out["topic_status"],
            mastery_state=eval_out["mastery_state"],
            mastery_score=eval_out["score_percentage"],
            evidence_json=json.dumps(eval_out["summary"]),
            studied_subtopics_json=studied_json,
            study_completed=1,
            assessment_attempt_count=new_attempts,
            db_path=db_path,
        )

        database.save_assessment_result(
            user_id=user_id,
            course_id=course_id,
            total_score=eval_out["score"],
            total_questions=eval_out["total_questions"],
            score_percentage=eval_out["score_percentage"],
            topic_scores_json=json.dumps({
                str(topic_id): {
                    "topic_id": topic_id,
                    "topic_name": topic["name"],
                    "score": eval_out["score"],
                    "total_questions": eval_out["total_questions"],
                    "score_percentage": eval_out["score_percentage"],
                    "mastery_state": eval_out["mastery_state"],
                }
            }),
            assessment_kind="topic_mastery",
            evaluation_details_json=json.dumps({
                "question_set": q_set,
                "passed": eval_out["passed"],
                "weak_concepts": eval_out["weak_concepts"],
                "recommended_subtopics": eval_out["recommended_subtopics"],
                "attempt_number": new_attempts,
            }),
            topic_id=topic_id,
            db_path=db_path,
        )

        l_progress = database.get_learner_topic_progress_details(user_id, course_id=course_id, db_path=db_path)
        l_status_map = {tid: info["status"] for tid, info in l_progress.items()}
        c_topics = database.get_course_topics(course_id, db_path=db_path)
        c_prereqs = database.get_course_prerequisites(course_id, db_path=db_path)

        adaptation = learning_engine.evaluate_adaptation(
            topics=c_topics,
            topic_prerequisites=c_prereqs,
            learner_status_map=l_status_map,
            learner_mastery_map=l_progress,
            updated_topic_id=topic_id,
        )
        course_progress_pct = adaptation.get("progress_percentage")
        recalculated_path = get_personalized_learning_path(course_name, user_id=user_id)
        if recalculated_path and recalculated_path.get("learning_path"):
            next_focus = recalculated_path["learning_path"].get("current_focus")

    return jsonify({
        "success": True,
        "is_logged_in": bool(user_id),
        "topic_id": topic_id,
        "topic_name": topic["name"],
        "course_name": course_name,
        "question_set": q_set,
        "passed": eval_out["passed"],
        "score": eval_out["score"],
        "total_questions": eval_out["total_questions"],
        "pass_score": eval_out["pass_score"],
        "score_percentage": eval_out["score_percentage"],
        "mastery_state": eval_out["mastery_state"],
        "topic_status": eval_out["topic_status"],
        "incorrect_items": eval_out["incorrect_items"],
        "weak_concepts": eval_out["weak_concepts"],
        "recommended_subtopics": eval_out["recommended_subtopics"],
        "item_results": eval_out["item_results"],
        "adaptation": adaptation,
        "newly_unlocked_topics": (adaptation or {}).get("newly_unlocked_topics", []),
        "course_progress_percentage": course_progress_pct,
        "next_focus": next_focus,
        "learning_path_url": url_for("module5", course=course_name),
        "progress_url": url_for("module6"),
    })


# -------------------------------------------------------------
# Development Server Startup
# -------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n==========================================")
    print(" Learnly Web Server running at:")
    print(f" -> http://127.0.0.1:{port}/")
    print(f" -> http://localhost:{port}/")
    print("==========================================\n")

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        threading.Timer(1.2, lambda: webbrowser.open_new(f"http://127.0.0.1:{port}/")).start()

    app.run(host="0.0.0.0", port=port, debug=True)
