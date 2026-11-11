"""
learning_engine.py - Learnly Core Intelligence & Adaptive Learning Engine
=========================================================================
Responsibilities:
  - Cycle detection in prerequisite graphs using Depth-First Search (DFS)
  - Pure Python graph layout calculator (node coordinates & cubic bezier SVG paths)
  - 5-State Mastery & Prerequisite Satisfaction resolution:
      Unassessed | Needs Attention | Developing | Proficient | Verified Mastery
  - Prerequisite graph construction for Knowledge Graph (Module 3)
  - Multi-dimensional Skill-Gap detection & calibration diagnostics (Module 4)
  - Genuinely Adaptive Personalized Learning Path using Mastery-Weighted Kahn's
    Topological Sorting (skips verified mastery, targets weak bottlenecks, adapts
    per student profile & goal without hardcoded curriculum cuts) (Module 5)
  - Continuous adaptation & downstream unlocking upon practice/reassessment (Module 6)
"""

from assessment import calculate_topic_scores


PREREQ_SATISFYING_STATES = {"Verified Mastery", "Proficient"}

MASTERY_PRIORITY_RANK = {
    "Needs Attention": 0,
    "Developing": 1,
    "Unassessed": 2,
    "Proficient": 3,
    "Verified Mastery": 4,
}


def _get_field(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, "__getitem__"):
        try:
            return obj[key]
        except (KeyError, IndexError):
            return default
    return getattr(obj, key, default)


def _normalize_goal(goal):
    raw = str(goal or "Balanced Path").strip()
    lower = raw.lower()
    if lower in ("targeted gap fix", "weak areas first", "fix weak areas"):
        return "Targeted Gap Fix"
    if lower in ("adaptive fast-track", "fast-track", "skip mastered", "advanced"):
        return "Adaptive Fast-Track"
    if lower in ("foundations first", "build foundations", "beginner"):
        return "Foundations First"
    if lower in ("balanced path", "full curriculum", "intermediate", ""):
        return "Balanced Path"
    return raw


def is_prerequisite_met(mastery_state, status=None):
    """
    Prerequisites are satisfied ONLY when the prerequisite topic has reached
    Proficient (passed mastery assessment) or Verified Mastery.
    Opening a topic, studying subtopics, or raw manual 'Completed' statuses without
    Proficient mastery never satisfy prerequisites.
    """
    return mastery_state in PREREQ_SATISFYING_STATES


def compute_topic_status(
    mastery_state,
    prereqs_met,
    study_completed=False,
    studied_subtopics=None,
    legacy_raw_status=None,
):
    """
    Canonical 6-state topic operational status resolver:
      - Completed          : Passed assessment (Proficient / Verified Mastery)
      - Locked             : Prerequisites not yet Proficient
      - Needs Attention    : Failed assessment (< 4/5 or weak initial assessment)
      - Assessment Pending : Studied all subtopics; ready to take Mastery Assessment
      - In Progress        : Started studying subtopics or Developing mastery
      - Not Started        : Unlocked and untouched
    """
    if mastery_state in PREREQ_SATISFYING_STATES:
        return "Completed"
    if not prereqs_met:
        return "Locked"
    if mastery_state == "Needs Attention":
        return "Needs Attention"
    if study_completed:
        return "Assessment Pending"
    if (studied_subtopics and len(studied_subtopics) > 0) or mastery_state == "Developing" or legacy_raw_status == "In Progress":
        return "In Progress"
    return "Not Started"


def determine_topic_statuses(topics, topic_prerequisites, topic_scores, existing_progress=None):
    """
    Assigns canonical 6-state topic statuses ('Not Started', 'In Progress',
    'Assessment Pending', 'Needs Attention', 'Completed', 'Locked').
    """
    prereq_map = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        dep_id = _get_field(p, "topic_id")
        req_id = _get_field(p, "prerequisite_topic_id")
        if dep_id in prereq_map and req_id in prereq_map:
            prereq_map[dep_id].append(req_id)

    mastery_by_id = {}
    status_hint_by_id = {}
    studied_by_id = {}
    study_done_by_id = {}

    for t in topics:
        tid = t["id"]
        t_score = topic_scores.get(tid) or topic_scores.get(str(tid))
        existing = (existing_progress or {}).get(tid) or {}

        studied_by_id[tid] = existing.get("studied_subtopics") or []
        study_done_by_id[tid] = bool(existing.get("study_completed"))

        if t_score:
            m_state = t_score.get("mastery_state")
            if not m_state:
                kl = t_score.get("knowledge_level", "Unassessed")
                m_state = (
                    "Proficient" if kl == "Strong"
                    else ("Developing" if kl == "Moderate" else ("Needs Attention" if kl == "Weak" else "Unassessed"))
                )
            mastery_by_id[tid] = m_state
            status_hint_by_id[tid] = None
        else:
            mastery_by_id[tid] = existing.get("mastery_state", "Unassessed")
            status_hint_by_id[tid] = existing.get("status")

    met_ids = {
        tid for tid in mastery_by_id
        if is_prerequisite_met(mastery_by_id[tid], status_hint_by_id.get(tid))
    }

    final_statuses = {}
    for t in topics:
        tid = t["id"]
        reqs = prereq_map.get(tid, [])
        prereqs_met = (not reqs) or all(req_id in met_ids for req_id in reqs)
        final_statuses[tid] = compute_topic_status(
            mastery_state=mastery_by_id.get(tid, "Unassessed"),
            prereqs_met=prereqs_met,
            study_completed=study_done_by_id.get(tid, False),
            studied_subtopics=studied_by_id.get(tid),
            legacy_raw_status=status_hint_by_id.get(tid),
        )

    return final_statuses


def detect_prerequisite_cycles(topics, topic_prerequisites):
    """Checks for circular prerequisite dependencies using DFS."""
    adj = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")
        if req_id in adj and dep_id in adj:
            adj[req_id].append(dep_id)

    visited = set()
    rec_stack = set()

    def dfs(node):
        visited.add(node)
        rec_stack.add(node)
        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                return True
        rec_stack.remove(node)
        return False

    for t in topics:
        tid = t["id"]
        if tid not in visited:
            if dfs(tid):
                return True
    return False


def calculate_graph_layout(topics, topic_prerequisites):
    """
    Computes (x, y) coordinates and cubic bezier SVG paths in pure Python.
    Arranges nodes layer-by-layer based on prerequisite depth (topological level).
    """
    has_cycle = detect_prerequisite_cycles(topics, topic_prerequisites)
    if has_cycle:
        return {
            "nodes": [],
            "edges": [],
            "width": 700,
            "height": 400,
            "has_cycle": True,
        }

    prereq_map = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")
        if dep_id in prereq_map and req_id in prereq_map:
            prereq_map[dep_id].append(req_id)

    levels = {}

    def get_level(t_id, visited=None):
        if visited is None:
            visited = set()
        if t_id in levels:
            return levels[t_id]
        if t_id in visited:
            return 0
        visited.add(t_id)
        reqs = prereq_map.get(t_id, [])
        if not reqs:
            levels[t_id] = 0
        else:
            levels[t_id] = 1 + max(get_level(r, visited.copy()) for r in reqs)
        return levels[t_id]

    for t in topics:
        get_level(t["id"])

    level_groups = {}
    for t in topics:
        lvl = levels.get(t["id"], 0)
        level_groups.setdefault(lvl, []).append(t)

    sorted_levels = sorted(level_groups.keys())
    max_level = max(sorted_levels) if sorted_levels else 0
    max_nodes_in_row = max(len(group) for group in level_groups.values()) if level_groups else 1

    node_w = 186
    node_h = 76
    col_width = 230
    row_height = 130

    canvas_width = max(700, max_nodes_in_row * col_width + 60)
    canvas_height = max(480, (max_level + 1) * row_height + 50)

    node_positions = {}
    rendered_nodes = []

    for lvl in sorted_levels:
        row_nodes = level_groups[lvl]
        k = len(row_nodes)
        row_total_w = k * col_width
        start_x = (canvas_width - row_total_w) // 2 + (col_width - node_w) // 2
        y = 24 + lvl * row_height

        for idx, t in enumerate(row_nodes):
            x = start_x + idx * col_width
            tid = t["id"]
            node_positions[tid] = (x, y)

            status_str = t.get("status", "Available")
            status_class = status_str.lower().replace(" ", "-")
            mastery_state = t.get("mastery_state", "Unassessed")

            rendered_nodes.append({
                "id": tid,
                "name": t["name"],
                "description": t.get("description", ""),
                "status": status_str,
                "status_class": status_class,
                "mastery_state": mastery_state,
                "mastery_score": t.get("mastery_score", 0.0),
                "prerequisites": t.get("prerequisites", []),
                "prerequisite_ids": prereq_map.get(tid, []),
                "x": x,
                "y": y,
                "level": lvl,
            })

    rendered_edges = []
    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")

        if req_id in node_positions and dep_id in node_positions:
            src_x, src_y = node_positions[req_id]
            tgt_x, tgt_y = node_positions[dep_id]

            sx = src_x + node_w // 2
            sy = src_y + node_h
            tx = tgt_x + node_w // 2
            ty = tgt_y

            if ty > sy:
                mid_y = (sy + ty) // 2
                path_d = f"M {sx} {sy} C {sx} {mid_y}, {tx} {mid_y}, {tx} {ty}"
            else:
                path_d = f"M {sx} {sy} L {tx} {ty}"

            rendered_edges.append({
                "source_id": req_id,
                "target_id": dep_id,
                "path_d": path_d,
            })

    return {
        "nodes": rendered_nodes,
        "edges": rendered_edges,
        "width": canvas_width,
        "height": canvas_height,
        "has_cycle": False,
    }


def build_prerequisite_graph(
    course_name,
    topics_rows,
    prereqs_rows,
    learner_status_map,
    learner_mastery_map=None,
    course_id=None,
    course_description="",
):
    """
    Module 3: Builds the Knowledge Graph data structure for a course.
    Uses genuine mastery & prerequisite satisfaction so topics lock ONLY when
    essential prerequisites remain incomplete.
    """
    prereq_ids_map = {t["id"]: [] for t in topics_rows}
    prereq_names_map = {t["id"]: [] for t in topics_rows}

    for pr in prereqs_rows:
        tid = pr["topic_id"]
        req_id = pr["prerequisite_topic_id"]
        req_name = pr["req_name"] if "req_name" in pr.keys() else f"Topic {req_id}"
        if tid in prereq_ids_map:
            prereq_ids_map[tid].append(req_id)
            prereq_names_map[tid].append(req_name)

    learner_mastery_map = learner_mastery_map or {}
    met_topic_ids = set()
    for t in topics_rows:
        tid = t["id"]
        stat = learner_status_map.get(tid, "Not Started")
        m_info = learner_mastery_map.get(tid) or {}
        m_state = m_info.get("mastery_state", "Unassessed")
        if is_prerequisite_met(m_state, stat):
            met_topic_ids.add(tid)

    topics_list = []
    completed_count = 0
    in_progress_count = 0
    assessment_pending_count = 0
    needs_attention_count = 0
    not_started_count = 0
    locked_count = 0

    mastery_counts = {
        "Verified Mastery": 0,
        "Proficient": 0,
        "Developing": 0,
        "Needs Attention": 0,
        "Unassessed": 0,
    }

    for t in topics_rows:
        tid = t["id"]
        raw_status = learner_status_map.get(tid, "Not Started")
        m_info = learner_mastery_map.get(tid) or {}
        m_state = m_info.get("mastery_state", "Unassessed")
        m_score = float(m_info.get("mastery_score", 0.0) or 0.0)
        studied_subs = m_info.get("studied_subtopics") or []
        study_done = bool(m_info.get("study_completed"))

        mastery_counts[m_state] = mastery_counts.get(m_state, 0) + 1

        prereqs = prereq_ids_map.get(tid, [])
        missing_prereq_names = [
            prereq_names_map[tid][idx]
            for idx, req_id in enumerate(prereqs)
            if req_id not in met_topic_ids
        ]

        computed_status = compute_topic_status(
            mastery_state=m_state,
            prereqs_met=not bool(missing_prereq_names),
            study_completed=study_done,
            studied_subtopics=studied_subs,
            legacy_raw_status=raw_status,
        )

        if computed_status == "Completed":
            completed_count += 1
        elif computed_status == "In Progress":
            in_progress_count += 1
        elif computed_status == "Assessment Pending":
            assessment_pending_count += 1
        elif computed_status == "Needs Attention":
            needs_attention_count += 1
        elif computed_status == "Not Started":
            not_started_count += 1
        else:
            locked_count += 1

        topics_list.append({
            "id": tid,
            "name": t["name"],
            "description": t.get("description") or "",
            "display_order": t.get("display_order", 0),
            "status": computed_status,
            "status_class": computed_status.lower().replace(" ", "-"),
            "mastery_state": m_state,
            "mastery_score": m_score,
            "studied_subtopics": studied_subs,
            "study_completed": study_done,
            "assessment_attempt_count": int(m_info.get("assessment_attempt_count", 0) or 0),
            "evidence": m_info.get("evidence", {}),
            "prerequisites": prereq_names_map.get(tid, []),
            "prerequisite_ids": prereqs,
            "missing_prerequisites": missing_prereq_names,
        })

    graph_layout = calculate_graph_layout(topics_list, prereqs_rows)
    total_topics = len(topics_rows)
    progress_percentage = int((completed_count / total_topics * 100)) if total_topics > 0 else 0

    return {
        "course_id": course_id,
        "course_name": course_name,
        "course_description": course_description or "",
        "topics": topics_list,
        "graph": graph_layout,
        "mastery_counts": mastery_counts,
        "stats": {
            "total": total_topics,
            "completed": completed_count,
            "in_progress": in_progress_count,
            "assessment_pending": assessment_pending_count,
            "needs_attention": needs_attention_count,
            "not_started": not_started_count,
            "available": not_started_count + assessment_pending_count + needs_attention_count,
            "locked": locked_count,
            "percentage": progress_percentage,
            "verified_mastery": mastery_counts["Verified Mastery"] + mastery_counts["Proficient"],
        },
    }


def detect_skill_gaps(topics, topic_prerequisites, topic_scores, topic_statuses, topic_mastery_map=None):
    """
    Module 4: Multi-Dimensional Skill-Gap Detection Algorithm.
    Evaluates conceptual, code comprehension, practical application, confidence
    calibration, and missing prerequisite dependencies per topic.
    """
    topic_name_map = {t["id"]: t["name"] for t in topics}
    prereq_ids_map = {t["id"]: [] for t in topics}

    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")
        if dep_id in prereq_ids_map and req_id in topic_name_map:
            prereq_ids_map[dep_id].append(req_id)

    topic_mastery_map = topic_mastery_map or {}

    # Compute satisfied prerequisite ids across both assessment and latest progress
    met_prereqs = set()
    for t in topics:
        tid = t["id"]
        stat = topic_statuses.get(tid)
        s_info = topic_scores.get(tid) or topic_scores.get(str(tid)) or {}
        m_info = topic_mastery_map.get(tid) or {}
        m_state = m_info.get("mastery_state") or s_info.get("mastery_state", "Unassessed")
        if is_prerequisite_met(m_state, stat):
            met_prereqs.add(tid)

    analyzed_topics = []
    mastered_count = 0
    needs_practice_count = 0
    ready_count = 0
    locked_count = 0
    missing_skills_set = set()

    mastery_counts = {
        "Verified Mastery": 0,
        "Proficient": 0,
        "Developing": 0,
        "Needs Attention": 0,
        "Unassessed": 0,
    }

    for t in topics:
        t_id = t["id"]
        t_name = t["name"]

        score_info = topic_scores.get(t_id) or topic_scores.get(str(t_id)) or {}
        progress_info = topic_mastery_map.get(t_id) or {}
        evidence = progress_info.get("evidence") or score_info.get("evidence") or {}

        # Prefer latest progress mastery_state if updated via topic practice, else initial score_info
        m_state = progress_info.get("mastery_state")
        if not m_state or m_state == "Unassessed":
            m_state = score_info.get("mastery_state")
            if not m_state:
                kl = score_info.get("knowledge_level", "Unassessed")
                m_state = (
                    "Proficient" if kl == "Strong"
                    else ("Developing" if kl == "Moderate" else ("Needs Attention" if kl == "Weak" else "Unassessed"))
                )

        m_score = float(
            progress_info.get("mastery_score")
            if progress_info.get("mastery_state") and progress_info.get("mastery_state") != "Unassessed"
            else score_info.get("mastery_score", score_info.get("score_percentage", 0.0))
        )
        score_pct = score_info.get("score_percentage", m_score)
        correct_ans = score_info.get("correct_answers", 0)
        total_q = score_info.get("total_questions", 0)

        dimensions = evidence.get("dimensions") or score_info.get("dimensions") or {
            "conceptual": None,
            "comprehension": None,
            "practical": None,
        }
        inconsistency_flags = evidence.get("inconsistency_flags") or score_info.get("inconsistency_flags") or []
        guessed_count = evidence.get("guessed_correct_count", score_info.get("guessed_correct_count", 0))
        overconf_errors = evidence.get("overconfident_error_count", score_info.get("overconfident_error_count", 0))

        knowledge_level = score_info.get("knowledge_level") or (
            "Strong" if m_state in ("Verified Mastery", "Proficient")
            else ("Moderate" if m_state == "Developing" else ("Weak" if m_state == "Needs Attention" else "Unassessed"))
        )
        status = topic_statuses.get(t_id, "Not Started")
        mastery_counts[m_state] = mastery_counts.get(m_state, 0) + 1

        all_prereq_ids = prereq_ids_map.get(t_id, [])
        all_prereq_names = [topic_name_map[pid] for pid in all_prereq_ids if pid in topic_name_map]

        missing_prereq_names = []
        for pid in all_prereq_ids:
            if pid not in met_prereqs:
                p_name = topic_name_map.get(pid, f"Topic #{pid}")
                missing_prereq_names.append(p_name)
                missing_skills_set.add(p_name)

        if m_state in PREREQ_SATISFYING_STATES:
            mastered_count += 1
            gap_type = m_state
            is_gap = False
            recommendation = evidence.get("recommendation") or "Assessment passed. Topic is Completed."
        elif status == "Locked" or missing_prereq_names:
            locked_count += 1
            gap_type = "Missing Prerequisites"
            is_gap = True
            recommendation = (
                f"Pass foundational topic(s) {', '.join(missing_prereq_names)} first."
                if missing_prereq_names
                else "Complete earlier prerequisite topics first."
            )
        elif m_state == "Needs Attention" or status == "Needs Attention":
            needs_practice_count += 1
            gap_type = "Needs Attention"
            is_gap = True
            recommendation = evidence.get("recommendation") or score_info.get("recommendation") or "Revise weak concepts and score at least 4/5 on the Mastery Assessment."
        elif status == "Assessment Pending":
            ready_count += 1
            gap_type = "Assessment Pending"
            is_gap = False
            recommendation = "All subtopics studied. Take the 5-question Mastery Assessment to complete this topic."
        elif m_state == "Developing" or status == "In Progress":
            needs_practice_count += 1
            gap_type = "In Progress"
            is_gap = True
            recommendation = evidence.get("recommendation") or score_info.get("recommendation") or "Finish studying remaining subtopics and pass the Mastery Assessment."
        else:
            ready_count += 1
            gap_type = "Not Started"
            is_gap = False
            recommendation = "Prerequisites are met. Study subtopics and pass the Mastery Assessment (4/5)."

        analyzed_topics.append({
            "id": t_id,
            "name": t_name,
            "description": t.get("description", ""),
            "display_order": t.get("display_order", 0),
            "score_percentage": score_pct,
            "mastery_score": round(m_score, 1),
            "mastery_state": m_state,
            "dimensions": dimensions,
            "guessed_correct_count": guessed_count,
            "overconfident_error_count": overconf_errors,
            "inconsistency_flags": inconsistency_flags,
            "correct_answers": correct_ans,
            "total_questions": total_q,
            "knowledge_level": knowledge_level,
            "status": status,
            "prerequisites": all_prereq_names,
            "missing_prerequisites": missing_prereq_names,
            "is_gap": is_gap,
            "gap_type": gap_type,
            "recommendation": recommendation,
        })

    return {
        "topics": analyzed_topics,
        "summary": {
            "total_topics": len(topics),
            "mastered_count": mastered_count,
            "verified_mastery_count": mastery_counts["Verified Mastery"],
            "proficient_count": mastery_counts["Proficient"],
            "developing_count": mastery_counts["Developing"],
            "needs_attention_count": mastery_counts["Needs Attention"],
            "unassessed_count": mastery_counts["Unassessed"],
            "needs_practice_count": needs_practice_count,
            "ready_count": ready_count,
            "locked_count": locked_count,
            "total_gaps_count": needs_practice_count + locked_count,
            "missing_skills": sorted(list(missing_skills_set)),
        },
    }


def _collect_required_ancestors(target_ids, prereqs_of, met_ids):
    """Collects all unmastered prerequisite ancestors needed to legally reach target_ids."""
    needed = set(target_ids)
    stack = list(target_ids)
    while stack:
        curr = stack.pop()
        for req_id in prereqs_of.get(curr, []):
            if req_id not in met_ids and req_id not in needed:
                needed.add(req_id)
                stack.append(req_id)
    return needed


def _compute_downstream_gap_weight(tid, dependents_of, state_of):
    """Counts how many downstream weak/developing/unmastered topics depend on tid."""
    visited = set()
    stack = list(dependents_of.get(tid, []))
    weight = 0
    while stack:
        d = stack.pop()
        if d in visited:
            continue
        visited.add(d)
        st = state_of.get(d, "Unassessed")
        if st == "Needs Attention":
            weight += 3
        elif st == "Developing":
            weight += 2
        elif st == "Unassessed":
            weight += 1
        stack.extend(dependents_of.get(d, []))
    return weight


def _build_short_reason(t, m_state, m_score, missing_prereqs, dependent_names, is_locked, flags):
    """Generates a single crisp, clear sentence explaining why this topic is in its slot."""
    if is_locked:
        return f"Locked until you reach proficiency in {', '.join(missing_prereqs)}."
    if m_state == "Needs Attention":
        if dependent_names:
            return f"Needs attention ({int(round(m_score))}% mastery); building this foundation unlocks {dependent_names[0]}."
        return f"Scored {int(round(m_score))}% here; start with core examples and targeted practice."
    if m_state == "Developing":
        if "conceptual_without_practical" in flags:
            return "You understand the concepts, so focus on debugging and writing small code here."
        if "guessed_correct_answers" in flags:
            return "Earlier answers had low confidence; complete short practice to stabilize mastery."
        return f"Partially mastered ({int(round(m_score))}%); finish coding practice to lock in this topic."
    if m_state == "Proficient":
        return f"Almost verified ({int(round(m_score))}%); one quick check confirms Verified Mastery."
    if dependent_names:
        return f"Ready to explore; unlocks {dependent_names[0]} once completed."
    return "Prerequisites are met; ready to study and practice."


def generate_personalized_learning_path(
    topics,
    topic_prerequisites,
    topic_scores,
    topic_statuses,
    topic_mastery_map=None,
    goal="Balanced Path",
):
    """
    Module 5: Genuinely Adaptive Personalized Learning Path Algorithm.

    Uses Mastery-Weighted Priority Kahn's Topological Sort:
      - Preserves strict DAG prerequisite correctness (never recommends an advanced
        topic before its incomplete prerequisites).
      - Skips topics with Verified Mastery (listed separately as optional review).
      - Prioritizes weak topics & critical foundational bottlenecks over static order.
      - Adapts target subset & route cleanly by goal WITHOUT hardcoded percentage cuts.
    """
    norm_goal = _normalize_goal(goal)
    topic_map = {t["id"]: t for t in topics}
    topic_mastery_map = topic_mastery_map or {}

    prereqs_of = {t["id"]: [] for t in topics}
    dependents_of = {t["id"]: [] for t in topics}

    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")
        if dep_id in prereqs_of and req_id in prereqs_of:
            prereqs_of[dep_id].append(req_id)
            dependents_of[req_id].append(dep_id)

    state_of = {}
    score_of = {}
    flags_of = {}
    studied_of = {}
    study_done_of = {}
    raw_status_of = {}

    for t in topics:
        tid = t["id"]
        s_info = topic_scores.get(tid) or topic_scores.get(str(tid)) or {}
        p_info = topic_mastery_map.get(tid) or {}
        ev = p_info.get("evidence") or s_info.get("evidence") or {}
        studied_of[tid] = p_info.get("studied_subtopics") or []
        study_done_of[tid] = bool(p_info.get("study_completed"))
        raw_status_of[tid] = p_info.get("status", topic_statuses.get(tid, "Not Started"))

        m_state = p_info.get("mastery_state")
        if not m_state or m_state == "Unassessed":
            m_state = s_info.get("mastery_state")
        if not m_state or m_state == "Unassessed":
            raw_stat = topic_statuses.get(tid)
            m_state = "Developing" if raw_stat == "In Progress" else "Unassessed"

        m_score = float(
            p_info.get("mastery_score")
            if p_info.get("mastery_state") and p_info.get("mastery_state") != "Unassessed"
            else s_info.get("mastery_score", s_info.get("score_percentage", 0.0))
        )

        state_of[tid] = m_state
        score_of[tid] = m_score
        flags_of[tid] = ev.get("inconsistency_flags") or s_info.get("inconsistency_flags") or []

    # Prerequisites met ONLY when topic passed assessment (Proficient or Verified Mastery)
    met_ids = {
        tid for tid in topic_map
        if is_prerequisite_met(state_of[tid], topic_statuses.get(tid))
    }

    # Skip passed / Proficient topics from active study list
    skipped_mastered = []
    for t in topics:
        tid = t["id"]
        if state_of[tid] in PREREQ_SATISFYING_STATES:
            skipped_mastered.append({
                "topic_id": tid,
                "topic_name": t["name"],
                "mastery_state": state_of[tid],
                "mastery_score": round(score_of[tid], 1),
                "description": t.get("description", ""),
            })

    # Target remaining unmastered topics
    unmastered_ids = [t["id"] for t in topics if state_of[t["id"]] not in PREREQ_SATISFYING_STATES]

    if norm_goal == "Targeted Gap Fix":
        gap_roots = [tid for tid in unmastered_ids if state_of[tid] in ("Needs Attention", "Developing")]
        if not gap_roots:
            gap_roots = [tid for tid in unmastered_ids if state_of[tid] == "Unassessed"]
        selected_set = _collect_required_ancestors(gap_roots, prereqs_of, met_ids)
    elif norm_goal == "Adaptive Fast-Track":
        non_proficient = [tid for tid in unmastered_ids if state_of[tid] not in PREREQ_SATISFYING_STATES]
        selected_set = _collect_required_ancestors(non_proficient, prereqs_of, met_ids) if non_proficient else set()
    else:
        selected_set = set(unmastered_ids)

    # Calculate Kahn's in-degree ONLY from unmet prerequisites inside selected_set
    # Prerequisites already in met_ids (Verified Mastery or Proficient) or outside selected_set
    # are satisfied dependencies unless an incomplete prerequisite blocks downstream progress.
    active_prereqs = {
        tid: [req for req in prereqs_of[tid] if req in selected_set and req not in met_ids]
        for tid in selected_set
    }
    in_degree = {tid: len(active_prereqs[tid]) for tid in selected_set}

    gap_weight = {
        tid: _compute_downstream_gap_weight(tid, dependents_of, state_of)
        for tid in selected_set
    }

    def candidate_priority(tid):
        m_state = state_of[tid]
        m_rank = MASTERY_PRIORITY_RANK.get(m_state, 2)
        m_score = score_of[tid]
        g_w = gap_weight.get(tid, 0)
        disp = topic_map[tid].get("display_order", 0)

        if norm_goal == "Foundations First":
            # Prioritize foundational depth (fewer total prereqs), lowest score, needs attention first
            depth = len(prereqs_of.get(tid, []))
            return (depth, m_rank, m_score, -g_w, disp)
        if norm_goal == "Adaptive Fast-Track":
            # Prioritize unblocking power and lowest weak gaps first
            return (m_rank, -g_w, m_score, disp)
        # Balanced Path & Targeted Gap Fix: weakest mastery first, then bottlenecks that unlock weak downstream topics
        return (m_rank, m_score, -g_w, disp)

    available_queue = sorted([tid for tid in selected_set if in_degree[tid] == 0], key=candidate_priority)
    ordered_topic_ids = []
    temp_in_degree = dict(in_degree)

    while available_queue:
        curr_id = available_queue.pop(0)
        ordered_topic_ids.append(curr_id)

        for dep_id in dependents_of.get(curr_id, []):
            if dep_id in temp_in_degree:
                temp_in_degree[dep_id] -= 1
                if temp_in_degree[dep_id] == 0:
                    available_queue.append(dep_id)
                    available_queue.sort(key=candidate_priority)

    for tid in selected_set:
        if tid not in ordered_topic_ids:
            ordered_topic_ids.append(tid)

    # For Foundations First, place immediately actionable topics first and group deeply blocked future branches cleanly
    total_course_topics = len(topics)
    sequence = []
    current_focus = None

    for step_num, tid in enumerate(ordered_topic_ids, start=1):
        t = topic_map[tid]
        m_state = state_of[tid]
        m_score = score_of[tid]
        flags = flags_of[tid]

        prereq_names = [topic_map[pid]["name"] for pid in prereqs_of.get(tid, []) if pid in topic_map]
        missing_prereqs = [
            topic_map[pid]["name"]
            for pid in prereqs_of.get(tid, [])
            if pid in topic_map and pid not in met_ids
        ]
        dependent_names = [topic_map[did]["name"] for did in dependents_of.get(tid, []) if did in topic_map]

        is_locked = bool(missing_prereqs)
        eff_status = compute_topic_status(
            mastery_state=m_state,
            prereqs_met=not is_locked,
            study_completed=study_done_of.get(tid, False),
            studied_subtopics=studied_of.get(tid),
            legacy_raw_status=raw_status_of.get(tid),
        )

        is_current_focus = False
        if not is_locked and current_focus is None:
            is_current_focus = True

        if is_locked:
            step_action = f"Complete {', '.join(missing_prereqs)} first"
        elif eff_status == "Assessment Pending":
            step_action = "Start Mastery Assessment"
        elif eff_status == "Needs Attention":
            step_action = "Revise & Retake"
        elif eff_status == "In Progress":
            step_action = "Continue Studying"
        else:
            step_action = "Study"

        why_rec = _build_short_reason(t, m_state, m_score, missing_prereqs, dependent_names, is_locked, flags)

        step_data = {
            "step_number": step_num,
            "topic_id": tid,
            "topic_name": t["name"],
            "description": t.get("description", ""),
            "status": eff_status,
            "mastery_state": m_state,
            "mastery_score": round(m_score, 1),
            "knowledge_level": (
                "Strong" if m_state in ("Verified Mastery", "Proficient")
                else ("Moderate" if m_state == "Developing" else ("Weak" if m_state == "Needs Attention" else "Unassessed"))
            ),
            "score_percentage": round(m_score, 1),
            "prerequisites": prereq_names,
            "missing_prerequisites": missing_prereqs,
            "step_action": step_action,
            "why_recommended": why_rec,
            "is_current_focus": is_current_focus,
            "is_locked": is_locked,
            "estimated_time": "20-30 min practice" if m_state in ("Developing", "Proficient") else "45-60 min study",
        }

        if is_current_focus and current_focus is None:
            current_focus = step_data

        sequence.append(step_data)

    # Fallback current focus if all unmastered steps are somehow locked (only possible in broken cycle)
    if current_focus is None and sequence:
        sequence[0]["is_current_focus"] = True
        current_focus = sequence[0]

    verified_count = len(skipped_mastered)
    progress_pct = int((verified_count / total_course_topics * 100)) if total_course_topics > 0 else 0

    return {
        "goal": norm_goal,
        "total_course_topics": total_course_topics,
        "total_steps": len(sequence),
        "completed_steps": verified_count,
        "progress_percentage": progress_pct,
        "current_focus": current_focus,
        "sequence": sequence,
        "skipped_mastered": skipped_mastered,
    }


def evaluate_adaptation(topics, topic_prerequisites, learner_status_map, learner_mastery_map=None, updated_topic_id=None):
    """
    Module 6: Recalculates downstream prerequisite unlocks and recommends the
    next highest-priority unlocked topic whenever progress or reassessment completes.
    """
    topic_map = {t["id"]: t for t in topics}
    learner_mastery_map = learner_mastery_map or {}

    prereqs_of = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        req_id = _get_field(p, "prerequisite_topic_id")
        dep_id = _get_field(p, "topic_id")
        if dep_id in prereqs_of and req_id in prereqs_of:
            prereqs_of[dep_id].append(req_id)

    met_ids = set()
    state_of = {}
    score_of = {}
    studied_of = {}
    study_done_of = {}
    for t in topics:
        tid = t["id"]
        stat = learner_status_map.get(tid, "Not Started")
        m_info = learner_mastery_map.get(tid) or {}
        m_state = m_info.get("mastery_state", "Unassessed")
        studied_of[tid] = m_info.get("studied_subtopics") or []
        study_done_of[tid] = bool(m_info.get("study_completed"))
        state_of[tid] = m_state
        score_of[tid] = float(m_info.get("mastery_score", 0.0) or 0.0)
        if is_prerequisite_met(m_state, stat):
            met_ids.add(tid)

    newly_unlocked_topics = []
    available_topics = []
    completed_topics = []
    in_progress_topics = []
    assessment_pending_topics = []
    needs_attention_topics = []
    not_started_topics = []
    locked_topics = []
    unlocked_candidates = []

    for t in topics:
        tid = t["id"]
        raw_status = learner_status_map.get(tid, "Not Started")
        m_state = state_of[tid]
        reqs = prereqs_of.get(tid, [])
        prereqs_ok = (not reqs) or all(r in met_ids for r in reqs)

        eff_status = compute_topic_status(
            mastery_state=m_state,
            prereqs_met=prereqs_ok,
            study_completed=study_done_of.get(tid, False),
            studied_subtopics=studied_of.get(tid),
            legacy_raw_status=raw_status,
        )

        if eff_status == "Completed":
            completed_topics.append(t["name"])
        elif eff_status == "Locked":
            locked_topics.append(t["name"])
        else:
            unlocked_candidates.append(tid)
            available_topics.append(t["name"])
            if updated_topic_id and updated_topic_id in reqs and updated_topic_id in met_ids:
                newly_unlocked_topics.append(t["name"])

            if eff_status == "In Progress":
                in_progress_topics.append(t["name"])
            elif eff_status == "Assessment Pending":
                assessment_pending_topics.append(t["name"])
            elif eff_status == "Needs Attention":
                needs_attention_topics.append(t["name"])
            else:
                not_started_topics.append(t["name"])

    # Pick adaptive next focus: weakest unlocked topic priority first
    next_focus = None
    next_focus_id = None
    if unlocked_candidates:
        unlocked_candidates.sort(
            key=lambda tid: (MASTERY_PRIORITY_RANK.get(state_of[tid], 2), score_of[tid], topic_map[tid].get("display_order", 0))
        )
        next_focus_id = unlocked_candidates[0]
        next_focus = topic_map[next_focus_id]["name"]

    total_topics = len(topics)
    completed_count = len(completed_topics)
    progress_percentage = int((completed_count / total_topics * 100)) if total_topics > 0 else 0

    adaptation_notes = []
    if updated_topic_id and updated_topic_id in topic_map:
        updated_name = topic_map[updated_topic_id]["name"]
        updated_state = state_of.get(updated_topic_id, "Updated")
        if newly_unlocked_topics:
            adaptation_notes.append(
                f"{updated_name} ({updated_state}) unlocked: {', '.join(newly_unlocked_topics)}."
            )
        else:
            adaptation_notes.append(
                f"Updated {updated_name} mastery to {updated_state}. Path recalculated."
            )

    return {
        "total_topics": total_topics,
        "completed_count": completed_count,
        "in_progress_count": len(in_progress_topics),
        "available_count": len(available_topics),
        "locked_count": len(locked_topics),
        "progress_percentage": progress_percentage,
        "next_focus": next_focus,
        "next_focus_id": next_focus_id,
        "completed_topics": completed_topics,
        "in_progress_topics": in_progress_topics,
        "available_topics": available_topics,
        "locked_topics": locked_topics,
        "newly_unlocked_topics": newly_unlocked_topics,
        "adaptation_notes": adaptation_notes,
    }


def generate_knowledge_map(course_id, questions, user_answers, topics, topic_prerequisites, user_confidences=None):
    """
    Main assessment & adaptive path evaluation executor.
    Produces multi-dimensional topic scores, 5 mastery states, prerequisite-aware
    statuses, and diagnostic skill gaps.
    """
    eval_result = calculate_topic_scores(questions, user_answers, user_confidences=user_confidences)
    topic_scores = eval_result["topic_scores"]
    final_statuses = determine_topic_statuses(topics, topic_prerequisites, topic_scores)

    topic_mastery_map = {
        tid: {
            "mastery_state": s["mastery_state"],
            "mastery_score": s["mastery_score"],
            "evidence": s["evidence"],
        }
        for tid, s in topic_scores.items()
    }

    skill_gaps = detect_skill_gaps(
        topics, topic_prerequisites, topic_scores, final_statuses, topic_mastery_map=topic_mastery_map
    )

    return {
        "course_id": course_id,
        "total_correct": eval_result["total_correct"],
        "total_questions": eval_result["total_questions"],
        "overall_percentage": eval_result["overall_percentage"],
        "topic_scores": topic_scores,
        "topic_mastery": topic_mastery_map,
        "topic_statuses": final_statuses,
        "skill_gaps": skill_gaps,
        "item_results": eval_result.get("item_results", []),
    }
