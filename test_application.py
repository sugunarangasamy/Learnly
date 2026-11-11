"""
test_application.py - Comprehensive Test Suite for Learnly
==========================================================
Runs in complete isolation against a temporary SQLite database copy
(`test_learnly_isolated.db`), preserving active `learnly.db` bit-for-bit.

Tests all requirements:
  1. Mixed-format Initial Assessments (MCQ, Predict Output, Debug Code, Write Code)
  2. Objective coding & output evaluators (equivalent valid solutions, spacing variants,
     rejection of unmodified buggy starter code, AST safety)
  3. Multi-dimensional 5 Mastery States (Unassessed, Needs Attention, Developing,
     Proficient, Verified Mastery) + Confidence & Consistency Calibration
  4. Guessed correct answers & MCQ-only cramming blocked from Verified Mastery
  5. Truly adaptive Learning Path (different student profiles get different routes,
     no fixed percentage curriculum cuts, prerequisite locking only when incomplete)
  6. Continuous Learning Cycle (Topic study -> practice -> reassessment -> mastery upgrade
     -> prerequisite unlocking -> path & progress recalculation without repeating initial exam)
  7. Preservation of existing auth, routes, dual routes, security (no answer leak), and UI hygiene
"""

import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys

os.environ["LEARNLY_SKIP_STARTUP_UPGRADE"] = "1"

from app import app
import assessment
import database
import learning_engine


ISOLATED_DB = "test_learnly_isolated.db"
ACTIVE_DB = "learnly.db"


def compute_table_hash(db_path, table_name):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table_name} ORDER BY id ASC").fetchall()
    conn.close()
    content = "".join(str(tuple(r)) for r in rows)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16], len(rows)


def extract_visible_html_text(html_content):
    no_scripts = re.sub(r"<script.*?</script>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
    no_styles = re.sub(r"<style.*?</style>", "", no_scripts, flags=re.DOTALL | re.IGNORECASE)
    no_comments = re.sub(r"<!--.*?-->", "", no_styles, flags=re.DOTALL)
    return re.sub(r"<[^>]+>", " ", no_comments)


def run_tests():
    print("=" * 70)
    print("LEARNLY V2 COMPREHENSIVE VERIFICATION SUITE (ISOLATED DB)")
    print("=" * 70)

    tables = [
        "users", "courses", "topics", "topic_prerequisites",
        "assessment_questions", "learner_assessment_results", "learner_topic_progress",
    ]
    pre_hashes = {t: compute_table_hash(ACTIVE_DB, t) for t in tables}

    if os.path.exists(ISOLATED_DB):
        os.remove(ISOLATED_DB)
    shutil.copyfile(ACTIVE_DB, ISOLATED_DB)

    app.config["DATABASE"] = ISOLATED_DB
    app.config["TESTING"] = True

    client = app.test_client()
    passed = 0
    failed = 0

    def check(condition, name, detail=""):
        nonlocal passed, failed
        if condition:
            print(f" [PASS] {name}")
            passed += 1
        else:
            print(f" [FAIL] {name}" + (f" -> {detail}" if detail else ""))
            failed += 1

    try:
        # -----------------------------------------------------------------
        # 1. QUESTION BANK & OBJECTIVE EVALUATION TESTS
        # -----------------------------------------------------------------
        print("\n--- 1. Multi-Type Questions & Objective Evaluation ---")
        res_py = client.get("/api/assessment-questions?course=Python+Programming")
        check(res_py.status_code == 200, "GET /api/assessment-questions returns 200")
        py_qs = res_py.get_json().get("questions", [])
        py_types = {q["question_type"] for q in py_qs}
        check(len(py_qs) == 24, f"Python initial assessment has 24 mixed questions (got {len(py_qs)})")
        check(
            py_types == {"mcq", "predict_output", "debug_code", "write_code"},
            f"All 4 question types present in ONE unified assessment: {sorted(py_types)}",
        )
        check(
            all("correct_answer" not in q and "evaluation_spec_json" not in q for q in py_qs),
            "Security: Zero correct_answer or evaluation spec leaked before submission",
        )

        # Verify questions are organized by topic
        seen_topics = []
        grouped_ok = True
        for q in py_qs:
            if not seen_topics or seen_topics[-1] != q["topic_id"]:
                if q["topic_id"] in seen_topics:
                    grouped_ok = False
                seen_topics.append(q["topic_id"])
        check(grouped_ok and len(seen_topics) == 6, "Assessment questions cleanly grouped by topic")

        # Test objective evaluator variants (alternate valid code + spacing variants + broken code)
        po_res = assessment.evaluate_predict_output("  [20,30] \n", {"expected_output": "[20, 30]", "normalize_whitespace": True}, "[20, 30]")
        check(po_res["is_correct"], "Predict Output tolerates minor spacing/newline differences")

        alt_code_q = {
            "id": 9991, "question_type": "write_code", "code_snippet": "def count_positives(nums):\n    pass",
            "eval_spec": {"language": "python", "test_cases": [
                {"call": "count_positives([-1, 2, 5])", "expected_return": 2},
                {"call": "count_positives([-3, 0])", "expected_return": 0},
            ]},
        }
        alt_code_solution = "def count_positives(nums):\n    total = 0\n    for item in nums:\n        if item > 0:\n            total = total + 1\n    return total"
        batch_out = assessment.evaluate_python_code_batch([{"id": "9991", "code": alt_code_solution, **alt_code_q}])
        check(batch_out["9991"]["is_correct"], "Objective runner accepts valid alternative variable names & loop implementation")

        # Reject unmodified buggy starter code
        buggy_starter = "def is_even(n):\n    if n % 2 = 0:\n        return True\n    return False"
        dbg_out = assessment.evaluate_python_code_batch([{
            "id": "9992", "question_type": "debug_code", "code_snippet": buggy_starter, "code": buggy_starter,
            "eval_spec": {"language": "python", "test_cases": [{"call": "is_even(4)", "expected_return": True}]},
        }])
        check(not dbg_out["9992"]["is_correct"], "Debug evaluator rejects unmodified buggy starter code")

        # Recursion AST gate rejects hardcoded iterative/constant cheat when recursion is required
        non_rec_cheat = "def factorial(n):\n    res = 1\n    for i in range(1, n+1): res *= i\n    return res"
        rec_out = assessment.evaluate_python_code_batch([{
            "id": "9993", "question_type": "write_code", "code_snippet": "", "code": non_rec_cheat,
            "eval_spec": {"language": "python", "require_recursion": True, "test_cases": [{"call": "factorial(4)", "expected_return": 24}]},
        }])
        check(not rec_out["9993"]["is_correct"], "Recursion check rejects non-recursive iterative solution")

        # Java alternative implementation passes real JDK / structural evaluator
        java_alt = "static int maxTwo(int a, int b) {\n    if (a > b) { return a; }\n    else { return b; }\n}"
        java_out = assessment.evaluate_java_code_batch([{
            "id": "9994", "question_type": "write_code", "code_snippet": "", "code": java_alt,
            "eval_spec": {"language": "java", "java_wrapper": "method", "java_test_cases": [
                {"expr": "maxTwo(4, 9)", "expected": "9"}, {"expr": "maxTwo(8, 2)", "expected": "8"}
            ], "required_pattern_groups": [[r"int\s+maxTwo"]]},
        }])
        check(java_out["9994"]["is_correct"], "Java evaluator accepts alternative valid if-else syntax")

        # -----------------------------------------------------------------
        # 2. CONFIDENCE, CONSISTENCY & 5 MASTERY STATES
        # -----------------------------------------------------------------
        print("\n--- 2. Confidence Calibration, Consistency & Mastery States ---")
        conn = database.get_db_connection(ISOLATED_DB)
        full_py_rows = [dict(r) for r in conn.execute(
            "SELECT q.*, t.name AS topic_name FROM assessment_questions q JOIN topics t ON q.topic_id=t.id WHERE q.course_id=1 AND q.question_pool='initial' ORDER BY t.display_order, q.question_number"
        ).fetchall()]
        conn.close()

        # Topic 1 (Variables) items
        t1_qs = [q for q in full_py_rows if q["topic_id"] == 1]

        # Profile Case A: Guessed ALL correct with low confidence -> Blocked from Verified Mastery!
        ans_all_ok = {str(q["id"]): q["correct_answer"] for q in t1_qs}
        conf_all_low = {str(q["id"]): "low" for q in t1_qs}
        res_guessed = assessment.calculate_topic_scores(t1_qs, ans_all_ok, user_confidences=conf_all_low)
        m_guessed = res_guessed["topic_scores"][1]["mastery_state"]
        check(m_guessed in ("Developing", "Needs Attention"), f"All low-confidence guessed answers blocked from Verified Mastery / Proficient (got '{m_guessed}')")

        # Profile Case B: Conceptual 100% MCQ + Predict, 0% Debug + Write Code -> Inconsistent / Developing (never Strong!)
        ans_mcq_only = {}
        conf_high = {str(q["id"]): "high" for q in t1_qs}
        for q in t1_qs:
            if q["question_type"] in ("mcq", "predict_output"):
                ans_mcq_only[str(q["id"])] = q["correct_answer"]
            else:
                ans_mcq_only[str(q["id"])] = ""
        res_no_code = assessment.calculate_topic_scores(t1_qs, ans_mcq_only, user_confidences=conf_high)
        m_no_code = res_no_code["topic_scores"][1]["mastery_state"]
        check(
            m_no_code in ("Developing", "Needs Attention") and "conceptual_without_practical" in res_no_code["topic_scores"][1]["inconsistency_flags"],
            f"MCQ/Trace without practical coding stays '{m_no_code}' and flags practical gap",
        )

        # Profile Case C: High confidence wrong answers -> Needs Attention with 0 credit & overconfidence alert
        ans_wrong = {str(q["id"]): "Z_WRONG" for q in t1_qs}
        res_overconf = assessment.calculate_topic_scores(t1_qs, ans_wrong, user_confidences=conf_high)
        m_overconf = res_overconf["topic_scores"][1]
        check(
            m_overconf["mastery_state"] == "Needs Attention" and m_overconf["mastery_score"] == 0.0 and m_overconf["overconfident_error_count"] == 4,
            "High confidence on wrong answers grants 0 score and flags misconceptions",
        )

        # Profile Case D: True Mastery across all 4 types with confident/medium calibration -> Verified Mastery
        res_mastered = assessment.calculate_topic_scores(t1_qs, ans_all_ok, user_confidences=conf_high)
        check(
            res_mastered["topic_scores"][1]["mastery_state"] == "Verified Mastery",
            f"Multi-type confident mastery earns 'Verified Mastery' (got {res_mastered['topic_scores'][1]['mastery_state']})",
        )

        # -----------------------------------------------------------------
        # 3. TWO STUDENTS RECEIVE DIFFERENT ADAPTIVE LEARNING ROUTES
        # -----------------------------------------------------------------
        print("\n--- 3. Adaptive Learning Paths & Multi-Student Divergence ---")
        ai_topics = database.get_course_topics(4, db_path=ISOLATED_DB)
        ai_prereqs = database.get_course_prerequisites(4, db_path=ISOLATED_DB)

        # Student Alpha: Verified Mastery in Python (20) & NumPy (22), Needs Attention in Math (21)
        alpha_scores = {
            20: {"mastery_state": "Verified Mastery", "mastery_score": 95.0, "score_percentage": 100.0},
            21: {"mastery_state": "Needs Attention", "mastery_score": 20.0, "score_percentage": 25.0},
            22: {"mastery_state": "Verified Mastery", "mastery_score": 92.0, "score_percentage": 100.0},
            23: {"mastery_state": "Developing", "mastery_score": 45.0, "score_percentage": 50.0},
            24: {"mastery_state": "Unassessed", "mastery_score": 0.0, "score_percentage": 0.0},
            25: {"mastery_state": "Unassessed", "mastery_score": 0.0, "score_percentage": 0.0},
        }
        alpha_statuses = learning_engine.determine_topic_statuses(ai_topics, ai_prereqs, alpha_scores)
        alpha_path = learning_engine.generate_personalized_learning_path(ai_topics, ai_prereqs, alpha_scores, alpha_statuses, goal="Balanced Path")

        # Student Beta: Needs Attention in Python (20), Verified Mastery in Math (21)
        beta_scores = {
            20: {"mastery_state": "Needs Attention", "mastery_score": 18.0, "score_percentage": 25.0},
            21: {"mastery_state": "Verified Mastery", "mastery_score": 94.0, "score_percentage": 100.0},
            22: {"mastery_state": "Developing", "mastery_score": 48.0, "score_percentage": 50.0},
            23: {"mastery_state": "Needs Attention", "mastery_score": 25.0, "score_percentage": 25.0},
            24: {"mastery_state": "Needs Attention", "mastery_score": 20.0, "score_percentage": 25.0},
            25: {"mastery_state": "Unassessed", "mastery_score": 0.0, "score_percentage": 0.0},
        }
        beta_statuses = learning_engine.determine_topic_statuses(ai_topics, ai_prereqs, beta_scores)
        beta_path = learning_engine.generate_personalized_learning_path(ai_topics, ai_prereqs, beta_scores, beta_statuses, goal="Balanced Path")

        alpha_next = alpha_path["current_focus"]["topic_name"]
        beta_next = beta_path["current_focus"]["topic_name"]
        check(
            alpha_next == "Mathematics and Statistics Basics" and beta_next == "Python Fundamentals" and alpha_next != beta_next,
            f"Two student profiles receive distinct next recommendations: Alpha='{alpha_next}', Beta='{beta_next}'",
        )
        check(
            len(alpha_path["skipped_mastered"]) == 2 and all(s["topic_name"] != "Python Fundamentals" for s in alpha_path["sequence"]),
            "Student Alpha skips Verified Mastery topics cleanly",
        )
        check(
            alpha_statuses[23] == "Locked",
            "Data Preprocessing (23) remains Locked while prerequisite Math (21) is Needs Attention",
        )

        # Verify Goal filter produces genuine targeted selection (NOT fixed 50%/75% length slicing)
        gap_fix_path = learning_engine.generate_personalized_learning_path(
            ai_topics, ai_prereqs, alpha_scores, alpha_statuses, goal="Targeted Gap Fix"
        )
        gap_fix_names = [s["topic_name"] for s in gap_fix_path["sequence"]]
        check(
            "Model Evaluation and AI Projects" not in gap_fix_names and "Mathematics and Statistics Basics" in gap_fix_names,
            f"Targeted Gap Fix isolates weak gaps & prerequisites dynamically (got {gap_fix_names})",
        )

        # -----------------------------------------------------------------
        # 4. END-TO-END WORKFLOWS: AUTH -> ASSESSMENT -> PRACTICE -> ADAPT
        # -----------------------------------------------------------------
        print("\n--- 4. Auth, Assessment, Continuous Practice & Adaptation Workflow ---")
        # Register new user cleanly in isolated DB
        res_signup = client.post("/signup", data={
            "name": "Adaptive Learner",
            "email": "adaptive@learnly.edu",
            "password": "Password123!",
        }, follow_redirects=True)
        check(res_signup.status_code == 200, "POST /signup creates learner session")

        # Submit mixed Initial Assessment for Python Programming:
        # Ace Topic 1 (Variables) + Topic 2 (Operators) + Topic 4 (Functions), FAIL Topic 3 (Loops) + Topic 5 + Topic 6
        c = database.get_db_connection(ISOLATED_DB)
        init_qs = [dict(r) for r in c.execute("SELECT * FROM assessment_questions WHERE course_id=1 AND question_pool='initial'").fetchall()]
        c.close()

        submitted_ans = {}
        submitted_conf = {}
        for q in init_qs:
            if q["topic_id"] in (1, 2, 4):
                submitted_ans[str(q["id"])] = q["correct_answer"]
                submitted_conf[str(q["id"])] = "high"
            else:
                submitted_ans[str(q["id"])] = ""
                submitted_conf[str(q["id"])] = "low"

        res_sub = client.post("/api/assessment-submit", json={
            "course_name": "Python Programming",
            "answers": submitted_ans,
            "confidences": submitted_conf,
            "learning_goal": "Balanced Path",
        })
        check(res_sub.status_code == 200 and res_sub.get_json()["success"], "POST /api/assessment-submit succeeds for mixed types")
        sub_data = res_sub.get_json()
        check(
            sub_data["topic_mastery"]["1"]["mastery_state"] == "Verified Mastery"
            and sub_data["topic_mastery"]["3"]["mastery_state"] == "Needs Attention",
            "Initial assessment assigns Verified Mastery to strong topics and Needs Attention to weak topics",
        )
        check(
            sub_data["topic_statuses"]["5"] == "Locked" and sub_data["topic_statuses"]["3"] == "Needs Attention",
            "Lists/Dicts (5) is Locked because prerequisite Loops (3) is Needs Attention, while Loops (3) is Needs Attention",
        )

        # Verify Personalized Path recommends Loops (3) next and skips 1, 2, 4
        res_m5_api = client.get("/api/personalized-path?course=Python+Programming")
        m5_json = res_m5_api.get_json()["learning_path"]
        check(
            m5_json["current_focus"]["topic_name"] == "Loops" and len(m5_json["skipped_mastered"]) == 3,
            f"Learning Path skips 3 mastered topics & puts Loops first (focus={m5_json['current_focus']['topic_name']})",
        )

        # Continuous Cycle Step 1: Open /practice for Loops (topic_id=3)
        res_prac_page = client.get("/practice?topic_id=3&course=Python+Programming")
        check(res_prac_page.status_code == 200, "GET /practice?topic_id=3 loads topic study & practice screen")

        res_prac_api = client.get("/api/topic-practice?topic_id=3")
        prac_api = res_prac_api.get_json()
        prac_types = {q["question_type"] for q in prac_api["questions"]}
        check(
            len(prac_api["questions"]) == 4 and prac_types == {"mcq", "predict_output", "debug_code", "write_code"},
            f"GET /api/topic-practice serves 4 mixed-type practice questions (types={sorted(prac_types)})",
        )
        check(
            all("correct_answer" not in q for q in prac_api["questions"]),
            "Security: Practice endpoint does NOT leak answers",
        )

        # Continuous Cycle Step 2: Solve all 4 Loops practice questions & submit reassessment
        conn = database.get_db_connection(ISOLATED_DB)
        loop_prac_rows = [dict(r) for r in conn.execute("SELECT * FROM assessment_questions WHERE topic_id=3 AND question_pool='practice'").fetchall()]
        conn.close()

        # Passing all 4 practice question types without confidence selector upgrades Loops -> Verified Mastery and unlocks Lists & Dictionaries!
        prac_ans = {str(q["id"]): q["correct_answer"] for q in loop_prac_rows}
        res_prac_sub = client.post("/api/topic-practice-submit", json={
            "topic_id": 3,
            "answers": prac_ans,
        })
        check(res_prac_sub.status_code == 200, "POST /api/topic-practice-submit returns 200 OK")
        ps_data = res_prac_sub.get_json()
        check(
            ps_data["previous_mastery"] == "Needs Attention"
            and ps_data["updated_mastery"] == "Verified Mastery",
            f"Mastery updated via practice reassessment: {ps_data['previous_mastery']} -> {ps_data['updated_mastery']}",
        )
        check(
            "Lists, Tuples, Sets, and Dictionaries" in (ps_data.get("adaptation") or {}).get("newly_unlocked_topics", []),
            f"Completing Loops practice automatically unlocked 'Lists, Tuples, Sets, and Dictionaries'! (unlocked={ps_data.get('adaptation', {}).get('newly_unlocked_topics')})",
        )

        # Verify Module 6 Progress Dashboard reflects reassessment without repeating initial exam
        res_prog = client.get("/api/progress")
        prog_json = res_prog.get_json()
        check(
            any(act["assessment_kind"] == "topic_practice" for act in prog_json.get("recent_activity", [])),
            "Progress dashboard logs topic practice reassessment entry cleanly",
        )

        # Multi-course HTTP API end-to-end checks: Data Structures (2), Java Programming (3), AI/ML (4)
        for cid, cname, pass_tids, fail_tid in [
            (2, "Data Structures", (7, 8), 9),
            (3, "Java Programming", (14, 15), 16),
            (4, "Artificial Intelligence and Machine Learning (AI/ML)", (20, 22), 21),
        ]:
            q_res = client.get(f"/api/assessment-questions?course={cname.replace('&', '%26').replace(' ', '+')}")
            q_json = q_res.get_json()
            qs_served = q_json.get("questions", [])
            types_served = {q["question_type"] for q in qs_served}
            check(
                q_res.status_code == 200
                and len(qs_served) >= 24
                and types_served == {"mcq", "predict_output", "debug_code", "write_code"}
                and all("correct_answer" not in q and "evaluation_spec_json" not in q for q in qs_served),
                f"{cname}: GET /api/assessment-questions serves {len(qs_served)} mixed-type questions safely",
            )

            conn = database.get_db_connection(ISOLATED_DB)
            course_init = [dict(r) for r in conn.execute(
                "SELECT * FROM assessment_questions WHERE course_id=? AND question_pool='initial'", (cid,)
            ).fetchall()]
            conn.close()
            c_ans = {}
            c_conf = {}
            for q in course_init:
                if q["topic_id"] in pass_tids:
                    c_ans[str(q["id"])] = q["correct_answer"]
                    c_conf[str(q["id"])] = "high"
                else:
                    c_ans[str(q["id"])] = ""
                    c_conf[str(q["id"])] = "low"
            sub_res = client.post("/api/assessment-submit", json={
                "course_name": cname,
                "answers": c_ans,
                "confidences": c_conf,
                "learning_goal": "Balanced Path",
            })
            sub_json = sub_res.get_json()
            check(
                sub_res.status_code == 200
                and sub_json.get("success")
                and all(sub_json["topic_mastery"][str(t)]["mastery_state"] == "Verified Mastery" for t in pass_tids)
                and sub_json["topic_mastery"][str(fail_tid)]["mastery_state"] == "Needs Attention",
                f"{cname}: POST /api/assessment-submit evaluates all 4 types into expected mastery states",
            )

        # -----------------------------------------------------------------
        # 5. TOPIC STUDY -> MASTERY ASSESSMENT & NO AUTO-COMPLETION FLOW
        # -----------------------------------------------------------------
        print("\n--- 5. Topic Completion Rules, 5-Type Mastery Assessment & Retake Flow ---")
        client.post("/logout")
        res_m_user = client.post("/signup", data={
            "name": "Mastery Flow Learner",
            "email": "masteryflow@learnly.edu",
            "password": "Password123!",
        }, follow_redirects=True)
        check(res_m_user.status_code == 200, "Fresh learner created for Mastery Assessment flow")

        # 5a. Reject manual Completed status write on /api/topic-progress
        res_manual = client.post("/api/topic-progress", json={"topic_id": 1, "status": "Completed"})
        check(res_manual.status_code == 400, "Manual Completed via POST /api/topic-progress rejected (HTTP 400)")

        # 5b. Open Topic 20 (AI/ML Python Fundamentals -> prerequisite for NumPy topic 22)
        res_study_open = client.get("/study?topic_id=20&course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)")
        check(res_study_open.status_code == 200, "Opening /study?topic_id=20 returns 200 OK")

        lp_before = client.get("/api/learning-path?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        t20_before = next(t for t in lp_before["topics"] if t["id"] == 20)
        t22_before = next(t for t in lp_before["topics"] if t["id"] == 22)
        check(
            t20_before["status"] == "Not Started" and t22_before["status"] == "Locked",
            "Opening study page leaves Topic 20 'Not Started' (never Completed) and dependent Topic 22 'Locked'",
        )

        # Study 1 subtopic -> In Progress, NOT Completed
        t20_subs = assessment.get_topic_study_notes("Python Fundamentals")["subtopics"]
        res_s1 = client.post("/api/study-subtopic", json={"topic_id": 20, "subtopic_id": t20_subs[0]["id"]})
        check(
            res_s1.status_code == 200 and res_s1.get_json()["status"] == "In Progress",
            "Studying partial subtopics sets status to 'In Progress' (not Completed)",
        )

        # Study ALL remaining subtopics -> Assessment Pending, NEVER Completed
        last_study = None
        for sub in t20_subs[1:]:
            last_study = client.post("/api/study-subtopic", json={"topic_id": 20, "subtopic_id": sub["id"]}).get_json()
        lp_studied = client.get("/api/learning-path?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        t20_studied = next(t for t in lp_studied["topics"] if t["id"] == 20)
        t22_studied = next(t for t in lp_studied["topics"] if t["id"] == 22)
        check(
            last_study and last_study["study_completed"] and t20_studied["status"] == "Assessment Pending" and t22_studied["status"] == "Locked",
            "Studying ALL subtopics moves status to 'Assessment Pending' (never Completed) and keeps dependents Locked",
        )

        # 5c. Verify all 25 topics have both mastery_1 and mastery_2 with exact 5 distinct types and zero leaked answers
        all_25_ok = True
        for tid_chk in range(1, 26):
            r1 = client.get(f"/api/mastery-assessment?topic_id={tid_chk}&retake=0").get_json()
            r2 = client.get(f"/api/mastery-assessment?topic_id={tid_chk}&retake=1&last_set={r1.get('question_set')}").get_json()
            types1 = {q["question_type"] for q in r1.get("questions", [])}
            types2 = {q["question_type"] for q in r2.get("questions", [])}
            ids1 = {q["id"] for q in r1.get("questions", [])}
            ids2 = {q["id"] for q in r2.get("questions", [])}
            leaked = any("correct_answer" in q or "evaluation_spec_json" in q for q in r1.get("questions", []) + r2.get("questions", []))
            if (
                len(r1.get("questions", [])) != 5 or len(r2.get("questions", [])) != 5
                or types1 != {"mcq", "predict_output", "debug_code", "fill_blank", "write_code"}
                or types2 != {"mcq", "predict_output", "debug_code", "fill_blank", "write_code"}
                or not ids1.isdisjoint(ids2) or leaked
            ):
                all_25_ok = False
                break
        check(all_25_ok, "All 25 topics serve 2 fresh 5-question Mastery Assessment sets (all 5 types, zero answer leak)")

        # 5d. Require all 5 questions attempted before submission
        m1_res = client.get("/api/mastery-assessment?topic_id=20&retake=0").get_json()
        m1_qs = m1_res["questions"]
        res_partial = client.post("/api/mastery-assessment-submit", json={
            "topic_id": 20, "question_set": m1_res["question_set"],
            "answers": {str(m1_qs[i]["id"]): "A" for i in range(4)},
        })
        check(
            res_partial.status_code == 400 and 5 in (res_partial.get_json() or {}).get("missing_questions", []),
            "Submitting only 4 of 5 questions rejected cleanly (HTTP 400)",
        )

        # 5e. Failing attempt (< 4/5 -> Needs Attention, stays incomplete, recommends subtopics, fresh retake works)
        conn = database.get_db_connection(ISOLATED_DB)
        m1_db_rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM assessment_questions WHERE topic_id=20 AND question_pool=?", (m1_res["question_set"],)).fetchall()
        }
        conn.close()
        fail_answers = {}
        for idx_q, q in enumerate(m1_qs):
            if idx_q < 3:
                fail_answers[str(q["id"])] = m1_db_rows[q["id"]]["correct_answer"]
            else:
                fail_answers[str(q["id"])] = "WRONG_ANSWER_XYZ"

        res_fail = client.post("/api/mastery-assessment-submit", json={
            "topic_id": 20, "question_set": m1_res["question_set"], "answers": fail_answers,
        })
        fail_json = res_fail.get_json()
        lp_after_fail = client.get("/api/learning-path?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        t20_after_fail = next(t for t in lp_after_fail["topics"] if t["id"] == 20)
        t22_after_fail = next(t for t in lp_after_fail["topics"] if t["id"] == 22)
        check(
            res_fail.status_code == 200
            and not fail_json["passed"]
            and fail_json["score"] == 3
            and fail_json["mastery_state"] == "Needs Attention"
            and t20_after_fail["status"] == "Needs Attention"
            and t22_after_fail["status"] == "Locked"
            and len(fail_json["incorrect_items"]) == 2
            and len(fail_json["weak_concepts"]) >= 1
            and len(fail_json["recommended_subtopics"]) >= 1,
            "Failing Mastery Assessment (3/5) keeps topic incomplete ('Needs Attention'), keeps dependents Locked, and returns weak concepts & subtopics",
        )

        # 5f. Retake fetches fresh set & Passing (>=4/5) with alternative Python code unlocks dependents and syncs ALL views
        m2_res = client.get(f"/api/mastery-assessment?topic_id=20&retake=1&last_set={m1_res['question_set']}").get_json()
        m2_qs = m2_res["questions"]
        conn = database.get_db_connection(ISOLATED_DB)
        m2_db_rows = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM assessment_questions WHERE topic_id=20 AND question_pool=?", (m2_res["question_set"],)).fetchall()
        }
        conn.close()
        pass_answers = {}
        for q in m2_qs:
            if q["question_type"] == "write_code":
                # Alternative valid implementation (explicit loop & different variable names, not exact string match)
                pass_answers[str(q["id"])] = (
                    "def positive_ratios(nums, total):\n"
                    "    out = []\n"
                    "    for val in nums:\n"
                    "        if val > 0:\n"
                    "            out.append(round(val / total, 2))\n"
                    "    return out"
                )
            else:
                pass_answers[str(q["id"])] = m2_db_rows[q["id"]]["correct_answer"]

        res_pass = client.post("/api/mastery-assessment-submit", json={
            "topic_id": 20, "question_set": m2_res["question_set"], "answers": pass_answers,
        })
        pass_json = res_pass.get_json()
        lp_after_pass = client.get("/api/learning-path?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        sg_after_pass = client.get("/api/skill-gap?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        pp_after_pass = client.get("/api/personalized-path?course=Artificial+Intelligence+and+Machine+Learning+(AI%2FML)").get_json()
        prog_after_pass = client.get("/api/progress").get_json()

        t20_pass = next(t for t in lp_after_pass["topics"] if t["id"] == 20)
        t22_pass = next(t for t in lp_after_pass["topics"] if t["id"] == 22)
        ai_course_prog = next(c for c in prog_after_pass["courses"] if c["id"] == 4)
        sg_t20 = next(t for t in sg_after_pass["skill_gaps"]["topics"] if t["id"] == 20)
        pp_done_ids = [s["topic_id"] for s in pp_after_pass["learning_path"]["skipped_mastered"]]

        check(
            pass_json["passed"]
            and pass_json["mastery_state"] == "Proficient"
            and pass_json["topic_status"] == "Completed"
            and t20_pass["status"] == "Completed"
            and t22_pass["status"] == "Not Started"
            and "NumPy and Pandas" in pass_json.get("newly_unlocked_topics", [])
            and sg_t20["status"] == "Completed"
            and 20 in pp_done_ids
            and ai_course_prog["completed_topics"] == 1
            and any(act["assessment_kind"] == "topic_mastery" for act in prog_after_pass.get("recent_activity", [])),
            "Passing Mastery Assessment marks Proficient + Completed, unlocks 'NumPy and Pandas', and synchronizes Learning Path, Knowledge Graph, Skill Gaps & Progress",
        )

        # -----------------------------------------------------------------
        # 6. EXISTING ROUTES, LEGACY USERS & CLEAN UI CHECKS
        # -----------------------------------------------------------------
        print("\n--- 6. Existing Routes, Legacy Users & Minimal UI Hygiene ---")
        client.post("/logout")
        res_login = client.post("/login", data={"email": "sugunar.25csd@kongu.edu", "password": "12345678"}, follow_redirects=True)
        check(res_login.status_code == 200 and "SUGUNA" in res_login.data.decode("utf-8"), "Existing account SUGUNA logs in cleanly")

        for route_url in [
            "/", "/module3?course=Python+Programming", "/learning-path?course=Java+Programming",
            "/module4?course=Data+Structures", "/skill-gap?course=Python+Programming",
            "/module5?course=Python+Programming", "/personalized-path?course=Java+Programming",
            "/module6", "/progress", "/practice?course=Python+Programming",
            "/study?topic_id=1&course=Python+Programming",
        ]:
            r = client.get(route_url)
            check(r.status_code == 200, f"Route GET {route_url} returns 200 OK")

        # Template cleanliness check (Zero 'Module X' labels)
        tpl_files = [
            "templates/index.html", "templates/module3.html", "templates/module4.html",
            "templates/module5.html", "templates/module6.html", "templates/practice.html",
        ]
        mod_re = re.compile(r"\bmodule\s*\d+\b", re.IGNORECASE)
        found_mod = []
        for tf in tpl_files:
            with open(tf, "r", encoding="utf-8") as f:
                vis = extract_visible_html_text(f.read())
                m = mod_re.findall(vis)
                if m:
                    found_mod.append((tf, m))
        check(not found_mod, f"Zero user-facing 'Module X' text across all 6 templates {found_mod}")

        with open("templates/index.html", "r", encoding="utf-8") as f:
            idx_html = f.read()
        with open("templates/practice.html", "r", encoding="utf-8") as f:
            prac_html = f.read()
        with open("templates/module3.html", "r", encoding="utf-8") as f:
            m3_html = f.read()
        with open("templates/module6.html", "r", encoding="utf-8") as f:
            m6_html = f.read()
        check(
            "question-sidebar" in idx_html and "question-number-grid" in idx_html,
            "Side question navigation panel present in Initial Assessment",
        )
        check(
            "Topics to Study" in prac_html and "References" in prac_html
            and "btn-study-topic" in prac_html and "btn-start-mastery-assessment" in prac_html
            and "btn-retake-mastery-assessment" in prac_html
            and "Mastery Assessment" in m6_html
            and not any(q_tok in (prac_html + m3_html) for q_tok in ["Start Practice", "Mark Topic Completed", "Mark Completed", "Mark In Progress", "p-question-sidebar", "p-area-mcq", "Submit Practice"]),
            "Study page & Knowledge Graph remove manual completion/practice buttons and include Mastery Assessment flow",
        )
        check(
            not any(phrase in (idx_html + prac_html) for phrase in ["How confident are you", "Just guessing", "Somewhat sure", "confidence-fieldset"]),
            "Confidence radio prompt removed cleanly from assessment and study templates",
        )

        # Verify all 25 topics have structured subtopics & references, including exact 7 subtopics for Variables and Data Types
        conn = database.get_db_connection(ISOLATED_DB)
        all_topic_names = [r["name"] for r in conn.execute("SELECT name FROM topics ORDER BY id").fetchall()]
        conn.close()
        subtopics_ok = all(
            len(assessment.get_topic_study_notes(name).get("subtopics", [])) >= 2
            and all(s.get("references") for s in assessment.get_topic_study_notes(name).get("subtopics", []))
            for name in all_topic_names
        )
        var_subs = [s["title"] for s in assessment.get_topic_study_notes("Variables and Data Types").get("subtopics", [])]
        expected_var_subs = [
            "Introduction to Variables",
            "Variable Declaration and Naming Rules",
            "Python Data Types",
            "Integers and Floating-Point Numbers",
            "Strings and Boolean Values",
            "Type Conversion",
            "Working with Variables and Data Types",
        ]
        check(
            subtopics_ok and var_subs == expected_var_subs,
            "All 25 topics provide structured subtopics & references (Variables and Data Types has all 7 required subtopics)",
        )
        with open("templates/module5.html", "r", encoding="utf-8") as f:
            m5_html = f.read()
        check(
            'id="course-select"' not in m5_html and 'id="goal-select"' not in m5_html,
            "Learning Path page does not show Course or Level switch dropdowns",
        )
        # Verify active course is removed from Explore Courses once started
        client.post("/logout")
        client.post("/login", data={"email": "masteryflow@learnly.edu", "password": "Password123!"})
        dash_html = client.get("/").data.decode("utf-8")
        explore_chunk = dash_html.split("<!-- EXPLORE COURSES -->", 1)[1].split("<!-- PAGE 2:", 1)[0]
        check(
            "startCourse('Artificial Intelligence and Machine Learning (AI/ML)')" not in explore_chunk
            and "startCourse('Python Programming')" in explore_chunk
            and "Study Next" in dash_html.split("<!-- EXPLORE COURSES -->", 1)[0],
            "Dashboard Explore Courses omits already-started course while My Courses shows Study Next",
        )
        vis_lvl_text = extract_visible_html_text(idx_html) + "\n" + extract_visible_html_text(m5_html)
        check(
            all(lbl in vis_lvl_text for lbl in ["Beginner", "Intermediate", "Advanced"])
            and not any(old in vis_lvl_text for old in ["Balanced Path", "Foundations First", "Adaptive Fast-Track"]),
            "Level selection displays simple Beginner / Intermediate / Advanced labels without verbose AI copy",
        )

    finally:
        app.config["DATABASE"] = ACTIVE_DB
        gc.collect()
        if os.path.exists(ISOLATED_DB):
            os.remove(ISOLATED_DB)

    # -----------------------------------------------------------------
    # 6. ACTIVE DATABASE INTEGRITY CHECK
    # -----------------------------------------------------------------
    print("\n--- 6. Active Database Integrity Audit (learnly.db) ---")
    any_diff = False
    for t in tables:
        h, cnt = compute_table_hash(ACTIVE_DB, t)
        ph, pcnt = pre_hashes[t]
        if h != ph or cnt != pcnt:
            any_diff = True
            print(f" [FAIL] Table {t} changed! Pre: {pcnt} ({ph}) -> Post: {cnt} ({h})")
        else:
            print(f" [OK] {t:<28}: {cnt} rows | hash {h}")
    check(not any_diff, "Active database learnly.db is 100% untouched during tests")

    print("\n" + "=" * 70)
    print(f"FINAL RESULTS: {passed} PASSED, {failed} FAILED.")
    print("=" * 70)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
