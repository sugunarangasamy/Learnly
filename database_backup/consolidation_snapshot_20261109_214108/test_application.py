"""
Comprehensive test script for Learnly Application (Modules 1 - 6)
Verifies:
  1. Route status codes (200 OK)
  2. Complete absence of 'Module X' user-facing text
  3. Knowledge Graph generation and rendering
  4. Skill-gap detection and analysis
  5. Learning path generation and goal filtering
  6. Progress tracking and adaptation
  7. Assessment question retrieval and evaluation
"""

import re
import sys
from app import app, get_db_connection

def extract_visible_html_text(html_content):
    # Remove script tags
    no_scripts = re.sub(r'<script.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    # Remove style tags
    no_styles = re.sub(r'<style.*?</style>', '', no_scripts, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    no_comments = re.sub(r'<!--.*?-->', '', no_styles, flags=re.DOTALL)
    # Remove HTML tags themselves
    text_only = re.sub(r'<[^>]+>', ' ', no_comments)
    return text_only

def run_tests():
    print("=" * 60)
    print("RUNNING LEARNLY APPLICATION TESTS")
    print("=" * 60)

    client = app.test_client()

    # Simulate a logged-in session with our existing user SUGUNA
    with client.session_transaction() as sess:
        sess["user_id"] = 1
        sess["user_name"] = "SUGUNA"
        sess["user_email"] = "sugunar.25csd@kongu.edu"

    tests_passed = 0
    tests_failed = 0

    def assert_test(condition, test_name, extra_info=""):
        nonlocal tests_passed, tests_failed
        if condition:
            print(f" [PASS] {test_name}")
            tests_passed += 1
        else:
            print(f" [FAIL] {test_name}: {extra_info}")
            tests_failed += 1

    # 1. Test Home / Dashboard (Module 1)
    res_home = client.get("/")
    assert_test(res_home.status_code == 200, "Module 1: Dashboard route (/) loads with 200 OK")
    home_html = res_home.data.decode("utf-8")
    assert_test("My Progress" in home_html, "Module 1: Dashboard navbar shows 'My Progress'")
    assert_test("My Progress (Module 6)" not in home_html, "Module 1: Dashboard navbar does NOT have 'Module 6'")

    # 2. Test Assessment Questions API (Module 2)
    res_q = client.get("/api/assessment-questions?course=Data+Structures")
    assert_test(res_q.status_code == 200, "Module 2: Assessment questions API returns 200 OK")
    q_data = res_q.get_json()
    assert_test(len(q_data.get("questions", [])) == 20, "Module 2: 20 assessment questions returned for Data Structures")

    # 3. Test Knowledge Graph (Module 3)
    res_m3 = client.get("/module3?course=Python+Programming")
    assert_test(res_m3.status_code == 200, "Module 3: Knowledge Graph route (/module3) returns 200 OK")
    m3_html = res_m3.data.decode("utf-8")
    assert_test("Course Knowledge Graph" in m3_html, "Module 3: Title rendered as 'Course Knowledge Graph'")
    assert_test("Module 3" not in m3_html, "Module 3: No 'Module 3' text appears in the rendered page")
    assert_test("Numbered List" in m3_html and "Flow Diagram" in m3_html, "Module 3: List & Diagram switcher present")

    # 4. Test Skill Gaps (Module 4)
    res_m4 = client.get("/module4?course=Data+Structures")
    assert_test(res_m4.status_code == 200, "Module 4: Skill Gaps route (/module4) returns 200 OK")
    m4_html = res_m4.data.decode("utf-8")
    assert_test("Skill Gaps &amp; Analysis" in m4_html or "Skill Gaps & Analysis" in m4_html, "Module 4: Title rendered as 'Skill Gaps & Analysis'")
    assert_test("Module 4" not in m4_html, "Module 4: No 'Module 4' text appears in the rendered page")
    assert_test("Mastered Topics" in m4_html, "Module 4: Clean summary cards present")

    # 5. Test Learning Path (Module 5)
    res_m5 = client.get("/module5?course=Python+Programming&goal=Full+Curriculum")
    assert_test(res_m5.status_code == 200, "Module 5: Learning Path route (/module5) returns 200 OK")
    m5_html = res_m5.data.decode("utf-8")
    assert_test("Personalized Learning Path" in m5_html, "Module 5: Title rendered as 'Personalized Learning Path'")
    assert_test("Module 5" not in m5_html, "Module 5: No 'Module 5' text appears in the rendered page")
    assert_test("Sequential Curriculum Steps" in m5_html, "Module 5: Sequence roadmap rendered")

    # 6. Test My Progress (Module 6)
    res_m6 = client.get("/module6")
    assert_test(res_m6.status_code == 200, "Module 6: Progress & Adaptation route (/module6) returns 200 OK")
    m6_html = res_m6.data.decode("utf-8")
    assert_test("Progress Tracking &amp; Learning Adaptation" in m6_html or "Progress Tracking & Learning Adaptation" in m6_html, "Module 6: Title rendered as 'Progress Tracking & Learning Adaptation'")
    assert_test("Module 6" not in m6_html, "Module 6: No 'Module 6' text appears in the rendered page")
    assert_test("Automated Prerequisite Adaptation" in m6_html, "Module 6: Adaptation explanation rendered")

    # 7. Test Topic Progress Save API
    res_prog = client.post("/api/topic-progress", json={"topic_id": 1, "status": "In Progress"})
    assert_test(res_prog.status_code == 200, "Progress API: Updating topic progress returns 200 OK")
    prog_json = res_prog.get_json()
    assert_test(prog_json.get("success") is True, "Progress API: Success confirms update")

    # 8. Check all HTML templates for any remaining user-facing 'Module X' occurrences
    template_files = ["templates/index.html", "templates/module3.html", "templates/module4.html", "templates/module5.html", "templates/module6.html"]
    pattern = re.compile(r'\bmodule\s*\d+\b', re.IGNORECASE)
    any_found = False
    for tf in template_files:
        with open(tf, "r", encoding="utf-8") as f:
            content = f.read()
            visible_text = extract_visible_html_text(content)
            matches = pattern.findall(visible_text)
            if matches:
                any_found = True
                print(f" [WARNING] Found user-facing 'Module X' in {tf}: {matches}")

    assert_test(not any_found, "UI Refinement: Zero user-facing 'Module X' text in all HTML templates")

    print("-" * 60)
    print(f"RESULTS: {tests_passed} passed, {tests_failed} failed.")
    print("=" * 60)

    if tests_failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    run_tests()
