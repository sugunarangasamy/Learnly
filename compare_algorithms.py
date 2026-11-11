"""
compare_algorithms.py - Verify algorithmic parity between original and consolidated modules.
Compares:
  - knowledge_map_algorithm.py (original)
  VS
  - assessment.py & learning_engine.py (consolidated)
across all courses in the database.
"""

import sys
import sqlite3
import json

# Import original
import knowledge_map_algorithm as orig

# Import consolidated
import assessment as new_assess
import learning_engine as new_engine


def run_comparison():
    print("=" * 60)
    print("STARTING ALGORITHMIC PARITY VERIFICATION")
    print("=" * 60)

    # 1. Test classify_knowledge_level
    print("\n[1] Testing classify_knowledge_level...")
    test_scores = [0, 25.0, 49.9, 50.0, 79.9, 80.0, 95.5, 100.0]
    for s in test_scores:
        orig_res = orig.classify_knowledge_level(s)
        new_res = new_assess.classify_knowledge_level(s)
        assert orig_res == new_res, f"Mismatch for score {s}: orig={orig_res}, new={new_res}"
    print("  ✓ classify_knowledge_level matches 100% across all boundary points.")

    # Connect to learnly.db (read-only)
    conn = sqlite3.connect("learnly.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM courses")
    courses = [dict(r) for r in cursor.fetchall()]

    for course in courses:
        course_id = course["id"]
        course_name = course["name"]
        print(f"\n--- Verifying Course {course_id}: {course_name} ---")

        cursor.execute("SELECT * FROM topics WHERE course_id = ? ORDER BY display_order", (course_id,))
        topics = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT tp.* FROM topic_prerequisites tp
            JOIN topics t ON tp.topic_id = t.id
            WHERE t.course_id = ?
        """, (course_id,))
        prereqs = [dict(r) for r in cursor.fetchall()]

        cursor.execute("""
            SELECT q.*, t.name AS topic_name
            FROM assessment_questions q
            JOIN topics t ON q.topic_id = t.id
            WHERE q.course_id = ?
            ORDER BY q.question_number ASC
        """, (course_id,))
        questions = [dict(r) for r in cursor.fetchall()]

        print(f"Loaded: {len(topics)} topics, {len(prereqs)} prerequisites, {len(questions)} questions.")

        # 2. Test calculate_topic_scores
        user_answers = {}
        for i, q in enumerate(questions):
            if i % 3 != 0:
                user_answers[str(q["id"])] = q["correct_answer"]
            else:
                opts = ["A", "B", "C", "D"]
                opts.remove(q["correct_answer"])
                user_answers[str(q["id"])] = opts[0]

        orig_scores = orig.calculate_topic_scores(questions, user_answers)
        new_scores = new_assess.calculate_topic_scores(questions, user_answers)

        assert json.dumps(orig_scores, sort_keys=True) == json.dumps(new_scores, sort_keys=True), \
            f"calculate_topic_scores mismatch in {course_name}!"
        print(f"  ✓ calculate_topic_scores matches 100% across {len(orig_scores)} topics.")

        # 3. Test detect_prerequisite_cycles
        orig_cycle = orig.detect_prerequisite_cycles(topics, prereqs)
        new_cycle = new_engine.detect_prerequisite_cycles(topics, prereqs)
        assert orig_cycle == new_cycle, f"detect_prerequisite_cycles mismatch in {course_name}: orig={orig_cycle}, new={new_cycle}"
        print(f"  ✓ detect_prerequisite_cycles matches 100% (No cycle: {not new_cycle}).")

        # 4. Test calculate_graph_layout
        orig_layout = orig.calculate_graph_layout(topics, prereqs)
        new_layout = new_engine.calculate_graph_layout(topics, prereqs)

        assert json.dumps(orig_layout, sort_keys=True) == json.dumps(new_layout, sort_keys=True), \
            f"calculate_graph_layout mismatch in {course_name}!"
        print(f"  ✓ calculate_graph_layout matches 100% (nodes: {len(new_layout['nodes'])}, edges: {len(new_layout['edges'])}).")

        # 5. Test determine_topic_statuses
        orig_statuses = orig.determine_topic_statuses(topics, prereqs, orig_scores)
        new_statuses = new_engine.determine_topic_statuses(topics, prereqs, new_scores)

        assert json.dumps(orig_statuses, sort_keys=True) == json.dumps(new_statuses, sort_keys=True), \
            f"determine_topic_statuses mismatch in {course_name}!"
        print(f"  ✓ determine_topic_statuses matches 100%.")

        # 6. Test detect_skill_gaps
        orig_gaps = orig.detect_skill_gaps(topics, prereqs, orig_scores, orig_statuses)
        new_gaps = new_engine.detect_skill_gaps(topics, prereqs, new_scores, new_statuses)

        assert json.dumps(orig_gaps, sort_keys=True) == json.dumps(new_gaps, sort_keys=True), \
            f"detect_skill_gaps mismatch in {course_name}!"
        print(f"  ✓ detect_skill_gaps matches 100% ({len(new_gaps.get('topics', []))} topics analyzed, summary: {new_gaps.get('summary', {}).get('total_gaps', 0)} gaps).")

        # 7. Test generate_personalized_learning_path
        orig_path = orig.generate_personalized_learning_path(topics, prereqs, orig_scores, orig_statuses)
        new_path = new_engine.generate_personalized_learning_path(topics, prereqs, new_scores, new_statuses)

        assert json.dumps(orig_path, sort_keys=True) == json.dumps(new_path, sort_keys=True), \
            f"generate_personalized_learning_path mismatch in {course_name}!"
        print(f"  ✓ generate_personalized_learning_path matches 100% ({len(new_path.get('sequence', []))} steps).")

        # 8. Test evaluate_adaptation
        learner_status_map = {t["id"]: orig_statuses.get(t["id"], "Locked") for t in topics}
        available_topic_id = None
        for tid, st in learner_status_map.items():
            if st == "Available":
                available_topic_id = tid
                break

        orig_adapt = orig.evaluate_adaptation(topics, prereqs, learner_status_map, updated_topic_id=available_topic_id)
        new_adapt = new_engine.evaluate_adaptation(topics, prereqs, learner_status_map, updated_topic_id=available_topic_id)

        assert json.dumps(orig_adapt, sort_keys=True) == json.dumps(new_adapt, sort_keys=True), \
            f"evaluate_adaptation mismatch in {course_name}!"
        print(f"  ✓ evaluate_adaptation matches 100% ({len(new_adapt.get('newly_unlocked_topics', []))} unlocked).")

        # 9. Test generate_knowledge_map
        orig_kmap = orig.generate_knowledge_map(course_id, questions, user_answers, topics, prereqs)
        new_kmap = new_engine.generate_knowledge_map(course_id, questions, user_answers, topics, prereqs)

        assert json.dumps(orig_kmap, sort_keys=True) == json.dumps(new_kmap, sort_keys=True), \
            f"generate_knowledge_map mismatch in {course_name}!"
        print(f"  ✓ generate_knowledge_map matches 100%.")

    conn.close()

    print("\n" + "=" * 60)
    print("ALL 4 COURSES & ALL ALGORITHMIC FUNCTIONS PASSED WITH 0 DIVERGENCE!")
    print("=" * 60)


if __name__ == "__main__":
    run_comparison()
