"""
Knowledge Map Generation Algorithm
===================================
1. Collect Answers: Retrieve learner's responses to assessment questions.
2. Calculate Topic Scores: Compare each response with the correct answer and calculate scores per topic.
3. Classify Knowledge Level:
   - Strong: 80% – 100%
   - Moderate: 50% – 79%
   - Weak: 0% – 49%
4. Map Topics: Represent topics as nodes and prerequisite relationships as directed edges.
5. Determine Topic Status:
   - Strong topic (>= 80%): Classified as Completed (mastered).
   - Moderate topic (50% - 79%): Classified as In Progress (needs review).
   - Weak topic (< 50%):
       - If all prerequisites are Completed -> Available (ready to learn).
       - If prerequisites are not yet completed -> Locked.
6. Display Knowledge Map: Show topic-wise strengths, gaps, and prerequisite connections.
"""

def classify_knowledge_level(score_percentage):

    if score_percentage >= 80.0:
        return "Strong"
    elif score_percentage >= 50.0:
        return "Moderate"
    else:
        return "Weak"


def calculate_topic_scores(questions, user_answers):
    """
    Compares learner's responses with correct answers and groups them by topic.
    Returns:
        topic_scores: dict mapping topic_id -> {
            "topic_id": int,
            "topic_name": str,
            "correct_answers": int,
            "total_questions": int,
            "score_percentage": float,
            "knowledge_level": str,
            "questions": list
        }
    """
    # Group questions by topic
    topics_grouped = {}
    for q in questions:
        t_id = q["topic_id"]
        if t_id not in topics_grouped:
            topics_grouped[t_id] = {
                "topic_id": t_id,
                "topic_name": q["topic_name"],
                "questions": []
            }
        topics_grouped[t_id]["questions"].append(q)

    topic_scores = {}
    total_correct = 0
    total_q = len(questions)

    for t_id, t_data in topics_grouped.items():
        correct_count = 0
        total_topic_q = len(t_data["questions"])

        question_breakdown = []
        for q in t_data["questions"]:
            q_num = q["question_number"]
            # user_answers can be keyed by question_number (1-indexed) or list
            if isinstance(user_answers, dict):
                given_ans = user_answers.get(str(q_num)) or user_answers.get(q_num, "")
            elif isinstance(user_answers, list) and q_num <= len(user_answers):
                given_ans = user_answers[q_num - 1]
            else:
                given_ans = ""

            given_ans = str(given_ans).strip().upper()
            is_correct = (given_ans == q["correct_answer"].strip().upper())
            if is_correct:
                correct_count += 1
                total_correct += 1

            question_breakdown.append({
                "question_number": q_num,
                "question_text": q["question_text"],
                "given_answer": given_ans,
                "correct_answer": q["correct_answer"],
                "is_correct": is_correct
            })

        score_pct = round((correct_count / total_topic_q) * 100, 1) if total_topic_q > 0 else 0.0
        knowledge_level = classify_knowledge_level(score_pct)

        topic_scores[t_id] = {
            "topic_id": t_id,
            "topic_name": t_data["topic_name"],
            "correct_answers": correct_count,
            "total_questions": total_topic_q,
            "score_percentage": score_pct,
            "knowledge_level": knowledge_level,
            "questions": question_breakdown
        }

    overall_pct = round((total_correct / total_q) * 100, 1) if total_q > 0 else 0.0

    return {
        "topic_scores": topic_scores,
        "total_correct": total_correct,
        "total_questions": total_q,
        "overall_percentage": overall_pct
    }


def determine_topic_statuses(topics, topic_prerequisites, topic_scores):
    """
    Assigns real learning path status (Completed, In Progress, Available, Locked)
    based on knowledge classification and prerequisite satisfaction.

    Rules:
    - Strong (>= 80%): Completed
    - Moderate (50-79%): In Progress
    - Weak (< 50%):
        - If all prerequisites are Completed -> Available (needs to learn)
        - Otherwise -> Locked
    """
    # Build prerequisite map
    prereq_map = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        dep_id = p["topic_id"]
        req_id = p["prerequisite_topic_id"]
        if dep_id in prereq_map:
            prereq_map[dep_id].append(req_id)

    # Initial pass: mark Completed topics
    completed_ids = set()
    initial_statuses = {}

    for t in topics:
        t_id = t["id"]
        t_score = topic_scores.get(t_id) or topic_scores.get(str(t_id))
        if t_score and t_score["knowledge_level"] == "Strong":
            initial_statuses[t_id] = "Completed"
            completed_ids.add(t_id)
        elif t_score and t_score["knowledge_level"] == "Moderate":
            initial_statuses[t_id] = "In Progress"
        else:
            initial_statuses[t_id] = None  # To be determined by prereqs

    # Second pass: determine Available vs Locked for weak / unassessed topics
    final_statuses = {}
    for t in topics:
        t_id = t["id"]
        current_status = initial_statuses.get(t_id)

        if current_status:
            final_statuses[t_id] = current_status
        else:
            prereqs = prereq_map.get(t_id, [])
            if not prereqs or all(req_id in completed_ids for req_id in prereqs):
                final_statuses[t_id] = "Available"
            else:
                final_statuses[t_id] = "Locked"

    return final_statuses


def detect_prerequisite_cycles(topics, topic_prerequisites):
    """
    Checks if there are any circular prerequisite dependencies using Depth-First Search (DFS).
    Returns True if a cycle is detected, False otherwise.
    """
    adj = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        # p can be a sqlite Row or a dict
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)
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
    Computes (x, y) coordinates and SVG connection paths in pure Python.
    Arranges nodes layer-by-layer based on prerequisite depth (topological level).
    """
    has_cycle = detect_prerequisite_cycles(topics, topic_prerequisites)
    if has_cycle:
        return {
            "nodes": [],
            "edges": [],
            "width": 700,
            "height": 400,
            "has_cycle": True
        }

    # Map each topic to its prerequisite IDs
    prereq_map = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)
        if dep_id in prereq_map and req_id in prereq_map:
            prereq_map[dep_id].append(req_id)

    # Calculate level (depth) for each topic
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

    # Group topics by their calculated level
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

    # Position each node
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

            rendered_nodes.append({
                "id": tid,
                "name": t["name"],
                "description": t.get("description", ""),
                "status": status_str,
                "status_class": status_class,
                "prerequisites": t.get("prerequisites", []),
                "prerequisite_ids": prereq_map.get(tid, []),
                "x": x,
                "y": y,
                "level": lvl
            })

    # Generate SVG curved connection paths between prerequisites and dependents
    rendered_edges = []
    for p in topic_prerequisites:
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)

        if req_id in node_positions and dep_id in node_positions:
            src_x, src_y = node_positions[req_id]
            tgt_x, tgt_y = node_positions[dep_id]

            # Bottom center of source node
            sx = src_x + node_w // 2
            sy = src_y + node_h

            # Top center of target node
            tx = tgt_x + node_w // 2
            ty = tgt_y

            # Cubic bezier curve for smooth dependency flow
            if ty > sy:
                mid_y = (sy + ty) // 2
                path_d = f"M {sx} {sy} C {sx} {mid_y}, {tx} {mid_y}, {tx} {ty}"
            else:
                path_d = f"M {sx} {sy} L {tx} {ty}"

            rendered_edges.append({
                "source_id": req_id,
                "target_id": dep_id,
                "path_d": path_d
            })

    return {
        "nodes": rendered_nodes,
        "edges": rendered_edges,
        "width": canvas_width,
        "height": canvas_height,
        "has_cycle": False
    }


def detect_skill_gaps(topics, topic_prerequisites, topic_scores, topic_statuses):
    """
    Module 4: Skill-Gap Detection Algorithm.
    Identifies weak topics, missing prerequisite knowledge, and learning gaps.

    Returns:
        dict: Detailed skill gap analysis and summary metrics.
    """
    # Build lookup of topic names by ID
    topic_name_map = {t["id"]: t["name"] for t in topics}

    # Build prerequisite map (topic_id -> list of prerequisite topic_ids)
    prereq_ids_map = {t["id"]: [] for t in topics}
    for p in topic_prerequisites:
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)
        if dep_id in prereq_ids_map and req_id in topic_name_map:
            prereq_ids_map[dep_id].append(req_id)

    analyzed_topics = []
    mastered_count = 0
    needs_practice_count = 0
    ready_count = 0
    locked_count = 0
    missing_skills_set = set()

    for t in topics:
        t_id = t["id"]
        t_name = t["name"]

        # 1. Fetch score and knowledge classification
        score_info = topic_scores.get(t_id) or topic_scores.get(str(t_id)) or {}
        score_pct = score_info.get("score_percentage", 0.0)
        correct_ans = score_info.get("correct_answers", 0)
        total_q = score_info.get("total_questions", 0)

        if score_info and "knowledge_level" in score_info:
            knowledge_level = score_info["knowledge_level"]
        else:
            knowledge_level = "Unassessed"

        # 2. Topic status (Completed, In Progress, Available, Locked)
        status = topic_statuses.get(t_id, "Available")

        # 3. Identify missing prerequisites
        all_prereq_ids = prereq_ids_map.get(t_id, [])
        all_prereq_names = [topic_name_map[pid] for pid in all_prereq_ids if pid in topic_name_map]

        missing_prereq_names = []
        for pid in all_prereq_ids:
            # A prerequisite is satisfied only if its status is 'Completed'
            p_status = topic_statuses.get(pid)
            if p_status != "Completed":
                p_name = topic_name_map.get(pid, f"Topic #{pid}")
                missing_prereq_names.append(p_name)
                missing_skills_set.add(p_name)

        # 4. Determine Gap Classification and Actionable Guidance
        if status == "Completed":
            mastered_count += 1
            gap_type = "Mastered"
            is_gap = False
            recommendation = "Strong grasp demonstrated (80%+). Ready to apply in advanced modules."
        elif status == "In Progress":
            needs_practice_count += 1
            gap_type = "Needs Practice"
            is_gap = True
            recommendation = "Moderate understanding (50-79%). Review key concepts and solve additional practice problems."
        elif status == "Available":
            ready_count += 1
            gap_type = "Ready to Learn"
            is_gap = (knowledge_level == "Weak")
            if knowledge_level == "Weak":
                recommendation = "Foundational prerequisites met, but topic score is below 50%. Begin with fundamentals."
            else:
                recommendation = "Prerequisites satisfied! You can begin studying this topic now."
        else: # Locked
            locked_count += 1
            gap_type = "Missing Prerequisites"
            is_gap = True
            if missing_prereq_names:
                recommendation = f"Missing foundational knowledge in: {', '.join(missing_prereq_names)}. Complete those first to unlock."
            else:
                recommendation = "Prerequisite courses or topics must be completed before starting."

        analyzed_topics.append({
            "id": t_id,
            "name": t_name,
            "description": t.get("description", ""),
            "display_order": t.get("display_order", 0),
            "score_percentage": score_pct,
            "correct_answers": correct_ans,
            "total_questions": total_q,
            "knowledge_level": knowledge_level,
            "status": status,
            "prerequisites": all_prereq_names,
            "missing_prerequisites": missing_prereq_names,
            "is_gap": is_gap,
            "gap_type": gap_type,
            "recommendation": recommendation
        })

    total_topics = len(topics)

    return {
        "topics": analyzed_topics,
        "summary": {
            "total_topics": total_topics,
            "mastered_count": mastered_count,
            "needs_practice_count": needs_practice_count,
            "ready_count": ready_count,
            "locked_count": locked_count,
            "total_gaps_count": needs_practice_count + locked_count + (1 if any(t["knowledge_level"] == "Weak" and t["status"] == "Available" for t in analyzed_topics) else 0),
            "missing_skills": sorted(list(missing_skills_set))
        }
    }


def generate_personalized_learning_path(topics, topic_prerequisites, topic_scores, topic_statuses, goal="Full Curriculum"):
    """
    Module 5: Personalized Learning Path Generation Algorithm.
    Generates an ordered, step-by-step curriculum sequence based on:
      1. Topological sorting (valid prerequisite order).
      2. Skill-gap remediation priority (weak prerequisites first).
      3. Learner's goal (Beginner, Intermediate, Advanced, Full Curriculum).

    Returns:
        dict: Ordered sequence of learning steps with current focus and milestones.
    """
    topic_map = {t["id"]: t for t in topics}

    # Build prerequisite map (topic_id -> list of prerequisite IDs)
    # and dependent map (topic_id -> list of topics that depend on it)
    prereqs_of = {t["id"]: [] for t in topics}
    dependents_of = {t["id"]: [] for t in topics}

    for p in topic_prerequisites:
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)
        if dep_id in prereqs_of and req_id in prereqs_of:
            prereqs_of[dep_id].append(req_id)
            dependents_of[req_id].append(dep_id)

    # Topological Sort using Kahn's algorithm (calculating in-degrees)
    in_degree = {t["id"]: len(prereqs_of[t["id"]]) for t in topics}

    # Priority Queue / Candidates: topics with 0 unmet prerequisites
    # Sort candidates by:
    #   1. Status ('In Progress' / 'Available' before 'Locked')
    #   2. Number of dependents (topics that unlock more downstream topics come first)
    #   3. display_order
    def candidate_priority(tid):
        stat = topic_statuses.get(tid, "Available")
        # Lower number = higher priority
        stat_rank = 0 if stat == "In Progress" else (1 if stat == "Available" else (2 if stat == "Completed" else 3))
        unlock_power = len(dependents_of.get(tid, []))
        disp_order = topic_map[tid].get("display_order", 0)
        return (stat_rank, -unlock_power, disp_order)

    available_queue = [t["id"] for t in topics if in_degree[t["id"]] == 0]
    available_queue.sort(key=candidate_priority)

    ordered_topic_ids = []
    temp_in_degree = in_degree.copy()

    while available_queue:
        current_id = available_queue.pop(0)
        ordered_topic_ids.append(current_id)

        for dep_id in dependents_of.get(current_id, []):
            temp_in_degree[dep_id] -= 1
            if temp_in_degree[dep_id] == 0:
                available_queue.append(dep_id)
                available_queue.sort(key=candidate_priority)

    # In case of remaining nodes (e.g. if graph had partial cycles or disconnected nodes)
    for t in topics:
        if t["id"] not in ordered_topic_ids:
            ordered_topic_ids.append(t["id"])

    # Goal-based filtering:
    # Beginner: First ~50% of foundation topics
    # Intermediate: Up to ~75% of topics
    # Advanced / Full: All topics
    total_count = len(ordered_topic_ids)
    if goal == "Beginner" and total_count > 3:
        cutoff = max(3, int(total_count * 0.5))
        target_ids = ordered_topic_ids[:cutoff]
    elif goal == "Intermediate" and total_count > 4:
        cutoff = max(4, int(total_count * 0.75))
        target_ids = ordered_topic_ids[:cutoff]
    else:
        target_ids = ordered_topic_ids

    # Assemble sequence steps
    sequence = []
    current_focus = None
    completed_steps = 0

    for step_num, tid in enumerate(target_ids, start=1):
        t = topic_map[tid]
        status = topic_statuses.get(tid, "Available")
        score_info = topic_scores.get(tid) or topic_scores.get(str(tid)) or {}
        knowledge_level = score_info.get("knowledge_level", "Unassessed")
        score_pct = score_info.get("score_percentage", 0.0)

        prereq_names = [topic_map[pid]["name"] for pid in prereqs_of.get(tid, []) if pid in topic_map]
        missing_prereqs = [topic_map[pid]["name"] for pid in prereqs_of.get(tid, []) if pid in topic_map and topic_statuses.get(pid) != "Completed"]

        # Milestone categorization based on step position
        if step_num <= max(1, total_count // 3):
            milestone = "Milestone 1: Foundations & Core Syntax"
        elif step_num <= max(2, (2 * total_count) // 3):
            milestone = "Milestone 2: Logic, Modularity & Data Handling"
        else:
            milestone = "Milestone 3: Advanced Architectures & Algorithms"

        # Determine step role and highlight the current focus
        is_current_focus = False
        if status == "Completed":
            completed_steps += 1
            step_action = "Completed (Mastered)"
        elif status == "In Progress":
            if current_focus is None:
                is_current_focus = True
                step_action = "In Progress (Current Focus)"
            else:
                step_action = "In Progress (Needs Review)"
        elif status == "Available":
            if current_focus is None:
                is_current_focus = True
                step_action = "Ready to Study (Current Focus)"
            else:
                step_action = "Ready to Study (Up Next)"
        else: # Locked
            step_action = "Locked (Complete Prerequisites First)"

        step_data = {
            "step_number": step_num,
            "topic_id": tid,
            "topic_name": t["name"],
            "description": t.get("description", ""),
            "status": status,
            "knowledge_level": knowledge_level,
            "score_percentage": score_pct,
            "milestone": milestone,
            "prerequisites": prereq_names,
            "missing_prerequisites": missing_prereqs,
            "step_action": step_action,
            "is_current_focus": is_current_focus,
            "estimated_time": "2-3 hours"
        }

        if is_current_focus and current_focus is None:
            current_focus = step_data

        sequence.append(step_data)

    # Fallback for current focus if all were completed or none selected
    if current_focus is None:
        for step in sequence:
            if step["status"] != "Completed":
                step["is_current_focus"] = True
                current_focus = step
                break

    progress_pct = int((completed_steps / len(sequence) * 100)) if sequence else 0

    return {
        "goal": goal,
        "total_steps": len(sequence),
        "completed_steps": completed_steps,
        "progress_percentage": progress_pct,
        "current_focus": current_focus,
        "sequence": sequence
    }


def evaluate_adaptation(topics, topic_prerequisites, learner_status_map, updated_topic_id=None):
    """
    Module 6: Progress Tracking and Dynamic Adaptation Algorithm.
    Recalculates topic unlocks, adaptation shifts, and course completion
    whenever a student finishes a topic or updates their progress.

    Returns:
        dict: Adaptation analytics, newly unlocked topics, and updated progression state.
    """
    topic_map = {t["id"]: t for t in topics}

    # Prerequisite map (topic_id -> list of prerequisite IDs)
    prereqs_of = {t["id"]: [] for t in topics}
    dependents_of = {t["id"]: [] for t in topics}

    for p in topic_prerequisites:
        req_id = p["prerequisite_topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'prerequisite_topic_id', None)
        dep_id = p["topic_id"] if isinstance(p, dict) or hasattr(p, '__getitem__') else getattr(p, 'topic_id', None)
        if dep_id in prereqs_of and req_id in prereqs_of:
            prereqs_of[dep_id].append(req_id)
            dependents_of[req_id].append(dep_id)

    completed_ids = {tid for tid, stat in learner_status_map.items() if stat == "Completed"}

    newly_unlocked_topics = []
    available_topics = []
    completed_topics = []
    in_progress_topics = []
    locked_topics = []

    for t in topics:
        tid = t["id"]
        raw_status = learner_status_map.get(tid, "Not Started")

        if raw_status == "Completed":
            completed_topics.append(t["name"])
        elif raw_status == "In Progress":
            in_progress_topics.append(t["name"])
        else:
            reqs = prereqs_of.get(tid, [])
            if not reqs or all(r in completed_ids for r in reqs):
                available_topics.append(t["name"])
                # If this topic was unlocked because updated_topic_id was just completed:
                if updated_topic_id and updated_topic_id in reqs:
                    newly_unlocked_topics.append(t["name"])
            else:
                locked_topics.append(t["name"])

    total_topics = len(topics)
    completed_count = len(completed_topics)
    progress_percentage = int((completed_count / total_topics * 100)) if total_topics > 0 else 0

    # Determine adaptive next focus
    next_focus = None
    if in_progress_topics:
        next_focus = in_progress_topics[0]
    elif available_topics:
        next_focus = available_topics[0]

    adaptation_notes = []
    if updated_topic_id and updated_topic_id in topic_map:
        completed_name = topic_map[updated_topic_id]["name"]
        if newly_unlocked_topics:
            adaptation_notes.append(
                f"Mastered '{completed_name}'! This successfully unlocked downstream topic(s): {', '.join(newly_unlocked_topics)}."
            )
        else:
            adaptation_notes.append(
                f"Progress recorded for '{completed_name}'. Learning path adapted."
            )

    return {
        "total_topics": total_topics,
        "completed_count": completed_count,
        "in_progress_count": len(in_progress_topics),
        "available_count": len(available_topics),
        "locked_count": len(locked_topics),
        "progress_percentage": progress_percentage,
        "next_focus": next_focus,
        "completed_topics": completed_topics,
        "in_progress_topics": in_progress_topics,
        "available_topics": available_topics,
        "locked_topics": locked_topics,
        "newly_unlocked_topics": newly_unlocked_topics,
        "adaptation_notes": adaptation_notes
    }


def generate_knowledge_map(course_id, questions, user_answers, topics, topic_prerequisites):
    """
    Main algorithm executor. Produces the complete knowledge assessment summary,
    topic statuses, and skill-gap detection results to be saved in the database.
    """
    eval_result = calculate_topic_scores(questions, user_answers)
    topic_scores = eval_result["topic_scores"]
    final_statuses = determine_topic_statuses(topics, topic_prerequisites, topic_scores)

    # Run Module 4 Skill-Gap Detection
    skill_gaps = detect_skill_gaps(topics, topic_prerequisites, topic_scores, final_statuses)

    return {
        "course_id": course_id,
        "total_correct": eval_result["total_correct"],
        "total_questions": eval_result["total_questions"],
        "overall_percentage": eval_result["overall_percentage"],
        "topic_scores": topic_scores,
        "topic_statuses": final_statuses,
        "skill_gaps": skill_gaps
    }
