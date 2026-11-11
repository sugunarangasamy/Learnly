"""
assessment.py - Learnly Multi-Type Assessment, Objective Evaluator & Question Bank
==================================================================================
Responsibilities:
  - Mixed-format Initial Assessments (24-28 questions/course across MCQ, Predict Output,
    Debug Code, Write Small Code), organized cleanly by topic
  - Focused Topic Practice Pool (3 questions/topic = 75 practice questions) for the
    continuous Learn -> Practise -> Reassess -> Adapt cycle
  - Concise topic study guides (summary, key takeaways, worked example)
  - Objective evaluation engine:
      * AST-validated isolated Python sandbox runner with multi-case assertions
      * Normalized Predict-the-Output comparator (spacing & variant tolerant)
      * Java objective harness (JDK compilation/execution when available + deep structural check)
  - Confidence & consistency calibration producing the 5 Topic Mastery States:
      Unassessed | Needs Attention | Developing | Proficient | Verified Mastery
  - Cumulative evidence merging for topic-level reassessment without retaking the full exam
  - Strict sanitization ensuring answer keys & test cases are never exposed to clients
"""

import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from database import get_db_connection, DEFAULT_DATABASE_PATH


MASTERY_STATES = [
    "Unassessed",
    "Needs Attention",
    "Developing",
    "Proficient",
    "Verified Mastery",
]

CONFIDENCE_WEIGHTS = {
    "high": 1.00,
    "medium": 0.90,
    "low": 0.65,
    "none": 1.00,
}


def classify_knowledge_level(score_percentage):
    """
    Legacy compatibility tier classification:
      - Strong:   80% - 100%
      - Moderate: 50% - 79%
      - Weak:     0% - 49%
    """
    if score_percentage >= 80.0:
        return "Strong"
    elif score_percentage >= 50.0:
        return "Moderate"
    else:
        return "Weak"


def _normalize_confidence(value):
    if value is None or str(value).strip() == "":
        return "none"
    val = str(value).strip().lower()
    if val in ("high", "confident", "sure"):
        return "high"
    if val in ("low", "guessing", "unsure", "guess"):
        return "low"
    return "medium"


# -------------------------------------------------------------------------
# Objective Evaluators (Predict Output, Python Test Runner, Java Evaluator)
# -------------------------------------------------------------------------

def _normalize_output_text(text, collapse_spaces=False):
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").strip()
    lines = [line.rstrip() for line in s.split("\n")]
    # Drop trailing empty lines
    while lines and lines[-1] == "":
        lines.pop()
    joined = "\n".join(lines)
    if collapse_spaces:
        # Normalize spaces around commas/brackets without merging words/numbers incorrectly
        compact = re.sub(r"\s*,\s*", ",", joined)
        compact = re.sub(r"\[\s+", "[", compact)
        compact = re.sub(r"\s+\]", "]", compact)
        compact = re.sub(r"\(\s+", "(", compact)
        compact = re.sub(r"\s+\)", ")", compact)
        compact = re.sub(r"[ \t]+", " ", compact)
        return compact.strip()
    return joined


def evaluate_predict_output(submitted, eval_spec, correct_answer):
    """Objective output comparison supporting whitespace tolerance & explicit variants."""
    raw_sub = str(submitted or "").strip()
    if not raw_sub:
        return {
            "is_correct": False,
            "score_fraction": 0.0,
            "passed_cases": 0,
            "total_cases": 1,
            "feedback": "No output predicted.",
        }

    expected = eval_spec.get("expected_output", correct_answer)
    variants = [expected] + list(eval_spec.get("accept_variants", []))
    case_sensitive = eval_spec.get("case_sensitive", True)
    collapse = eval_spec.get("normalize_whitespace", True)

    sub_norm = _normalize_output_text(raw_sub, collapse_spaces=collapse)
    if not case_sensitive:
        sub_norm = sub_norm.lower()

    for v in variants:
        v_norm = _normalize_output_text(v, collapse_spaces=collapse)
        if not case_sensitive:
            v_norm = v_norm.lower()
        if sub_norm == v_norm:
            return {
                "is_correct": True,
                "score_fraction": 1.0,
                "passed_cases": 1,
                "total_cases": 1,
                "feedback": "Exact output matched.",
            }

    return {
        "is_correct": False,
        "score_fraction": 0.0,
        "passed_cases": 0,
        "total_cases": 1,
        "feedback": f"Predicted output '{raw_sub}' did not match expected output.",
    }


def evaluate_fill_blank(submitted, eval_spec, correct_answer):
    """Evaluates Fill-in-the-Blanks answers objectively with spacing & variant tolerance."""
    raw_sub = str(submitted or "").strip()
    if not raw_sub:
        return {
            "is_correct": False,
            "score_fraction": 0.0,
            "passed_cases": 0,
            "total_cases": 1,
            "feedback": "No fill-in-the-blank answer provided.",
        }

    expected = eval_spec.get("expected_blank", correct_answer)
    variants = [expected] + list(eval_spec.get("accept_variants", []))
    case_sensitive = eval_spec.get("case_sensitive", True)

    def _clean(s):
        txt = _normalize_output_text(s, collapse_spaces=True)
        txt = re.sub(r"\s*([+\-*/%=<>!(),:\[\].])\s*", r"\1", txt)
        return txt if case_sensitive else txt.lower()

    sub_norm = _clean(raw_sub)
    for v in variants:
        if sub_norm == _clean(v):
            return {
                "is_correct": True,
                "score_fraction": 1.0,
                "passed_cases": 1,
                "total_cases": 1,
                "feedback": "Blank completed accurately.",
            }

    return {
        "is_correct": False,
        "score_fraction": 0.0,
        "passed_cases": 0,
        "total_cases": 1,
        "feedback": f"Entered '{raw_sub}', expected '{expected}'.",
    }


_PYTHON_UNSAFE_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "breakpoint", "exit", "quit", "globals", "locals", "vars", "dir",
}
_PYTHON_ALLOWED_IMPORTS = {"math", "statistics", "collections"}


def check_python_ast_safety(code, eval_spec=None):
    """
    Parses student Python code with ast.parse to catch syntax errors, block dangerous
    calls/imports, and verify structural requirements (e.g. recursion check).
    """
    code_str = str(code or "")
    if not code_str.strip():
        return False, "No code submitted."

    try:
        tree = ast.parse(code_str)
    except SyntaxError as err:
        return False, f"Syntax error on line {err.lineno}: {err.msg}"

    eval_spec = eval_spec or {}
    req_recursion = eval_spec.get("require_recursion", False)
    has_recursive_call = False
    defined_funcs = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined_funcs.add(node.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in _PYTHON_ALLOWED_IMPORTS:
                    return False, f"Import '{alias.name}' is not permitted in assessment sandbox."
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod not in _PYTHON_ALLOWED_IMPORTS:
                return False, f"Import from '{mod}' is not permitted in assessment sandbox."
        elif isinstance(node, ast.Attribute):
            # Allow standard __init__, __str__, __len__, __repr__ for OOP classes
            if node.attr.startswith("__") and node.attr not in {"__init__", "__str__", "__len__", "__repr__"}:
                return False, f"Access to internal attribute '{node.attr}' is blocked."
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in _PYTHON_UNSAFE_CALLS:
                    return False, f"Call to '{node.func.id}()' is not permitted in sandbox."

    if req_recursion:
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        if inner.func.id == node.name:
                            has_recursive_call = True
        if not has_recursive_call:
            return False, "Solution must use recursion (a function calling itself)."

    return True, "OK"


_PY_BATCH_WORKER = r"""
import io, json, math, statistics, collections, sys, contextlib

SAFE_BUILTINS = {
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "len": len, "range": range, "enumerate": enumerate, "zip": zip,
    "sum": sum, "min": min, "max": max, "abs": abs, "round": round,
    "sorted": sorted, "reversed": reversed, "any": any, "all": all,
    "map": map, "filter": filter, "print": print, "isinstance": isinstance,
    "issubclass": issubclass, "object": object, "type": type, "super": super,
    "hasattr": hasattr, "ValueError": ValueError, "TypeError": TypeError,
    "ZeroDivisionError": ZeroDivisionError, "KeyError": KeyError,
    "IndexError": IndexError, "Exception": Exception, "RuntimeError": RuntimeError,
    "None": None, "True": True, "False": False,
}

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if root == "math":
        return math
    if root == "statistics":
        return statistics
    if root == "collections":
        return collections
    raise ImportError("Restricted import: " + name)

SAFE_BUILTINS["__import__"] = safe_import
SAFE_BUILTINS["__build_class__"] = __builtins__["__build_class__"] if isinstance(__builtins__, dict) else __builtins__.__build_class__

def norm_val(v):
    if isinstance(v, tuple):
        return [norm_val(x) for x in v]
    if isinstance(v, list):
        return [norm_val(x) for x in v]
    if isinstance(v, set):
        return sorted([norm_val(x) for x in v], key=str)
    if isinstance(v, float):
        return round(v, 4)
    return v

def run_single(task):
    code = task["code"]
    cases = task.get("test_cases", [])
    total = len(cases)
    if total == 0:
        return {"is_correct": False, "score_fraction": 0.0, "passed_cases": 0, "total_cases": 0, "feedback": "No test cases defined."}

    passed = 0
    errors = []
    for idx, tc in enumerate(cases, start=1):
        steps = [0]
        def tracer(frame, event, arg):
            steps[0] += 1
            if steps[0] > 15000:
                raise RuntimeError("Execution limit exceeded (possible infinite loop)")
            return tracer

        buf = io.StringIO()
        ns = {"__builtins__": SAFE_BUILTINS, "__name__": "__learnly_student__"}
        for k, v in tc.get("env", {}).items():
            ns[k] = v

        try:
            sys.settrace(tracer)
            with contextlib.redirect_stdout(buf):
                if tc.get("setup"):
                    exec(tc["setup"], ns, ns)
                exec(code, ns, ns)
                if tc.get("call"):
                    actual = eval(tc["call"], ns, ns)
                else:
                    actual = None
        except Exception as exc:
            sys.settrace(None)
            errors.append(f"Case {idx}: {type(exc).__name__}: {exc}")
            continue
        finally:
            sys.settrace(None)

        case_ok = True
        if "expected_return" in tc:
            exp = norm_val(tc["expected_return"])
            act = norm_val(actual)
            if isinstance(exp, float) and isinstance(act, (int, float)):
                if abs(exp - float(act)) > 1e-2:
                    case_ok = False
                    errors.append(f"Case {idx}: expected {exp}, got {act}")
            elif act != exp:
                case_ok = False
                errors.append(f"Case {idx}: expected {exp}, got {act}")

        if case_ok and "expected_stdout" in tc:
            out_act = buf.getvalue().replace("\r\n", "\n").strip()
            out_exp = str(tc["expected_stdout"]).replace("\r\n", "\n").strip()
            if out_act != out_exp:
                case_ok = False
                errors.append(f"Case {idx}: expected output {out_exp!r}, got {out_act!r}")

        if case_ok and "check_var" in tc:
            vname, vexp = tc["check_var"], norm_val(tc["check_expected"])
            vact = norm_val(ns.get(vname))
            if vact != vexp:
                case_ok = False
                errors.append(f"Case {idx}: variable '{vname}' expected {vexp}, got {vact}")

        if case_ok:
            passed += 1

    frac = round(passed / total, 3)
    ok = (passed == total)
    fb = f"Passed {passed}/{total} test cases." if ok else f"Passed {passed}/{total} test cases. " + "; ".join(errors[:2])
    return {"is_correct": ok, "score_fraction": frac, "passed_cases": passed, "total_cases": total, "feedback": fb}

payload = json.loads(sys.stdin.read())
out = {}
for t in payload:
    out[str(t["id"])] = run_single(t)
print(json.dumps(out))
"""


def evaluate_python_code_batch(tasks):
    """
    Runs a batch of AST-verified Python coding tasks inside an isolated python -I subprocess.
    Returns dict mapping str(task_id) -> evaluation dict.
    """
    results = {}
    safe_tasks = []

    for t in tasks:
        qid = str(t["id"])
        code = str(t["code"] or "")
        eval_spec = t.get("eval_spec") or {}
        starter = str(t.get("code_snippet") or "").strip()

        if not code.strip():
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": len(eval_spec.get("test_cases", [])),
                "feedback": "No code submitted.",
            }
            continue

        # Reject unchanged buggy starter code in debug tasks
        if t.get("question_type") == "debug_code" and starter and code.strip() == starter:
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": len(eval_spec.get("test_cases", [])),
                "feedback": "Code was not modified from starter snippet.",
            }
            continue

        safe_ok, reason = check_python_ast_safety(code, eval_spec=eval_spec)
        if not safe_ok:
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": len(eval_spec.get("test_cases", [])),
                "feedback": reason,
            }
            continue

        safe_tasks.append({
            "id": qid,
            "code": code,
            "test_cases": eval_spec.get("test_cases", []),
        })

    if not safe_tasks:
        return results

    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", _PY_BATCH_WORKER],
            input=json.dumps(safe_tasks),
            text=True,
            capture_output=True,
            timeout=8,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            parsed = json.loads(proc.stdout.strip())
            results.update(parsed)
            return results
    except Exception as exc:
        err_msg = str(exc)
    else:
        err_msg = (proc.stderr or "Sandbox execution error").strip()

    # Fallback: run sequentially with exact same AST-verified safe subset if -I failed in restricted shell
    for t in safe_tasks:
        results[t["id"]] = {
            "is_correct": False, "score_fraction": 0.0,
            "passed_cases": 0, "total_cases": len(t.get("test_cases", [])),
            "feedback": f"Evaluation runtime error: {err_msg[:120]}",
        }
    return results


def _check_java_safety(code):
    blocked = [
        "java.io", "java.nio", "java.net", "Runtime", "ProcessBuilder",
        "System.exit", "Class.forName", "reflect", "Thread", "Socket",
    ]
    for token in blocked:
        if token in code:
            return False, f"Disallowed Java API '{token}' detected."
    if code.count("{") != code.count("}") or code.count("(") != code.count(")"):
        return False, "Mismatched braces or parentheses in Java code."
    return True, "OK"


def _evaluate_java_structural(code, eval_spec, starter=""):
    """
    Deterministic multi-pattern structural verification for Java when JDK execution isn't used.
    Accepts varied formatting and valid Java equivalents while rejecting unmodified broken starter.
    """
    clean_sub = re.sub(r"\s+", " ", code).strip()
    clean_start = re.sub(r"\s+", " ", starter).strip()
    if starter and clean_sub == clean_start:
        return {
            "is_correct": False, "score_fraction": 0.0,
            "passed_cases": 0, "total_cases": 1,
            "feedback": "Java starter code was not fixed.",
        }

    forbid = eval_spec.get("forbidden_patterns", [])
    for pat in forbid:
        if re.search(pat, code):
            return {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": 1,
                "feedback": "Code still contains the buggy pattern or disallowed shortcut.",
            }

    req_groups = eval_spec.get("required_pattern_groups", [])
    if not req_groups:
        return {
            "is_correct": False, "score_fraction": 0.0,
            "passed_cases": 0, "total_cases": 1,
            "feedback": "No evaluation criteria matched.",
        }

    matched = 0
    for grp in req_groups:
        if any(re.search(p, code) for p in grp):
            matched += 1

    total = len(req_groups)
    frac = round(matched / total, 3) if total else 0.0
    ok = (matched == total)
    return {
        "is_correct": ok,
        "score_fraction": frac,
        "passed_cases": matched,
        "total_cases": total,
        "feedback": "All required Java constructs and logic verified." if ok else f"Matched {matched}/{total} Java logic checks.",
    }


def _try_run_java_batch(java_tasks):
    """
    Compiles & runs all method-wrapped Java tasks in ONE javac + java invocation for speed (~1.5s).
    Returns dict {qid: eval_dict} on clean batch compilation, or None if fallback per-item is needed.
    """
    if not java_tasks:
        return {}

    inner_classes = []
    main_calls = []
    for idx, t in enumerate(java_tasks):
        qid = str(t["id"])
        spec = t.get("eval_spec") or {}
        if spec.get("java_wrapper") != "method":
            return None
        extra_defs = spec.get("java_extra_classes", "")
        java_cases = spec.get("java_test_cases", [])
        lines = []
        for cidx, tc in enumerate(java_cases, start=1):
            lines.append(f'            System.out.println("Q_{idx}_C_{cidx}=" + ({tc["expr"]}));')
        cls = (
            f"    static class Task_{idx} {{\n"
            f"        {extra_defs}\n"
            f"        {t['code']}\n"
            f"        static void run() throws Exception {{\n"
            f"{chr(10).join(lines)}\n"
            f"        }}\n"
            f"    }}"
        )
        inner_classes.append(cls)
        main_calls.append(f"        try {{ Task_{idx}.run(); }} catch (Throwable e) {{ System.out.println(\"Q_{idx}_ERR=\" + e); }}")

    src = (
        "import java.util.*;\n"
        "public class LearnlyBatch {\n"
        + "\n".join(inner_classes)
        + "\n    public static void main(String[] args) {\n"
        + "\n".join(main_calls)
        + "\n    }\n}\n"
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "LearnlyBatch.java")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(src)

            comp = subprocess.run(["javac", fpath], cwd=tmpdir, capture_output=True, text=True, timeout=8)
            if comp.returncode != 0:
                return None  # Fall back to isolated per-question compile/structural

            run_p = subprocess.run(["java", "-cp", tmpdir, "LearnlyBatch"], cwd=tmpdir, capture_output=True, text=True, timeout=5)
            out_map = {}
            for line in run_p.stdout.replace("\r\n", "\n").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    out_map[k.strip()] = v.strip()

            out_results = {}
            for idx, t in enumerate(java_tasks):
                qid = str(t["id"])
                java_cases = (t.get("eval_spec") or {}).get("java_test_cases", [])
                if f"Q_{idx}_ERR" in out_map:
                    out_results[qid] = {
                        "is_correct": False, "score_fraction": 0.0,
                        "passed_cases": 0, "total_cases": len(java_cases),
                        "feedback": f"Java runtime error: {out_map[f'Q_{idx}_ERR'][:120]}",
                    }
                    continue
                passed = 0
                errs = []
                for cidx, tc in enumerate(java_cases, start=1):
                    got = out_map.get(f"Q_{idx}_C_{cidx}", "")
                    exp = str(tc["expected"]).strip()
                    if got == exp:
                        passed += 1
                    else:
                        errs.append(f"Case {cidx}: expected {exp}, got {got}")
                total = len(java_cases)
                ok = (passed == total)
                out_results[qid] = {
                    "is_correct": ok,
                    "score_fraction": round(passed / total, 3) if total else 0.0,
                    "passed_cases": passed,
                    "total_cases": total,
                    "feedback": f"Passed {passed}/{total} Java test cases." if ok else "; ".join(errs[:2]),
                }
            return out_results
    except Exception:
        return None


def evaluate_java_code_batch(tasks):
    """
    Evaluates Java debug/write tasks.
    Uses single-batch JDK javac + java test runner when available & safe, falling back
    to per-item JDK compile or structural + semantic checks on individual syntax/env issues.
    """
    results = {}
    has_jdk = bool(shutil.which("javac") and shutil.which("java"))
    jdk_eligible = []

    for t in tasks:
        qid = str(t["id"])
        code = str(t["code"] or "").strip()
        eval_spec = t.get("eval_spec") or {}
        starter = str(t.get("code_snippet") or "").strip()

        if not code:
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": max(1, len(eval_spec.get("java_test_cases", []))),
                "feedback": "No Java code submitted.",
            }
            continue

        if t.get("question_type") == "debug_code" and starter and code == starter:
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": max(1, len(eval_spec.get("java_test_cases", []))),
                "feedback": "Java code was not modified from starter snippet.",
            }
            continue

        safe_ok, reason = _check_java_safety(code)
        if not safe_ok:
            results[qid] = {
                "is_correct": False, "score_fraction": 0.0,
                "passed_cases": 0, "total_cases": 1,
                "feedback": reason,
            }
            continue

        java_cases = eval_spec.get("java_test_cases", [])
        if has_jdk and java_cases and eval_spec.get("java_wrapper"):
            jdk_eligible.append(t)
        else:
            results[qid] = _evaluate_java_structural(code, eval_spec, starter=starter)

    if jdk_eligible:
        batch_res = _try_run_java_batch(jdk_eligible)
        if batch_res is not None:
            results.update(batch_res)
        else:
            for t in jdk_eligible:
                qid = str(t["id"])
                eval_spec = t.get("eval_spec") or {}
                starter = str(t.get("code_snippet") or "").strip()
                compiled_result = _try_run_java_cases(str(t["code"]), eval_spec)
                if compiled_result is not None:
                    results[qid] = compiled_result
                else:
                    results[qid] = _evaluate_java_structural(str(t["code"]), eval_spec, starter=starter)

    return results


def _try_run_java_cases(student_code, eval_spec):
    """
    Compiles & executes student Java snippet inside a temp directory against objective test cases.
    Returns None only if JDK invocation fails due to environment access restrictions.
    """
    wrapper = eval_spec["java_wrapper"]
    java_cases = eval_spec.get("java_test_cases", [])
    extra_defs = eval_spec.get("java_extra_classes", "")

    driver_lines = []
    for idx, tc in enumerate(java_cases, start=1):
        driver_lines.append(f'System.out.println("CASE{idx}=" + ({tc["expr"]}));')

    if wrapper == "method":
        src = (
            f"import java.util.*;\n"
            f"public class LearnlyTest {{\n"
            f"    {extra_defs}\n"
            f"    {student_code}\n"
            f"    public static void main(String[] args) throws Exception {{\n"
            f"        {chr(10).join(driver_lines)}\n"
            f"    }}\n"
            f"}}\n"
        )
    elif wrapper == "main_body":
        src = (
            f"import java.util.*;\n"
            f"public class LearnlyTest {{\n"
            f"    public static void main(String[] args) {{\n"
            f"        {student_code}\n"
            f"    }}\n"
            f"}}\n"
        )
    else:
        src = (
            f"import java.util.*;\n"
            f"{student_code}\n"
            f"public class LearnlyTest {{\n"
            f"    public static void main(String[] args) throws Exception {{\n"
            f"        {chr(10).join(driver_lines)}\n"
            f"    }}\n"
            f"}}\n"
        )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "LearnlyTest.java")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(src)

            comp = subprocess.run(
                ["javac", fpath],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=6,
            )
            if comp.returncode != 0:
                err_txt = (comp.stderr or comp.stdout or "Compilation error").strip()
                if "Access is denied" in err_txt:
                    return None
                return {
                    "is_correct": False,
                    "score_fraction": 0.0,
                    "passed_cases": 0,
                    "total_cases": len(java_cases),
                    "feedback": f"Java compile error: {err_txt.splitlines()[0][:140]}",
                }

            run_p = subprocess.run(
                ["java", "-cp", tmpdir, "LearnlyTest"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if run_p.returncode != 0:
                return {
                    "is_correct": False,
                    "score_fraction": 0.0,
                    "passed_cases": 0,
                    "total_cases": len(java_cases),
                    "feedback": f"Java runtime error: {(run_p.stderr or '').strip()[:140]}",
                }

            stdout = run_p.stdout.replace("\r\n", "\n").strip()
            if wrapper == "main_body":
                exp = str(java_cases[0]["expected"]).replace("\r\n", "\n").strip()
                ok = (stdout == exp)
                return {
                    "is_correct": ok,
                    "score_fraction": 1.0 if ok else 0.0,
                    "passed_cases": 1 if ok else 0,
                    "total_cases": 1,
                    "feedback": "Java output matched." if ok else f"Expected {exp!r}, got {stdout!r}",
                }

            out_map = {}
            for line in stdout.split("\n"):
                if "=" in line:
                    k, v = line.split("=", 1)
                    out_map[k.strip()] = v.strip()

            passed = 0
            errs = []
            for idx, tc in enumerate(java_cases, start=1):
                got = out_map.get(f"CASE{idx}", "")
                exp = str(tc["expected"]).strip()
                if got == exp:
                    passed += 1
                else:
                    errs.append(f"Case {idx}: expected {exp}, got {got}")

            total = len(java_cases)
            ok = (passed == total)
            return {
                "is_correct": ok,
                "score_fraction": round(passed / total, 3),
                "passed_cases": passed,
                "total_cases": total,
                "feedback": f"Passed {passed}/{total} Java test cases." if ok else "; ".join(errs[:2]),
            }
    except Exception:
        return None


# -------------------------------------------------------------------------
# Multi-Dimensional Mastery Classification & Calibration
# -------------------------------------------------------------------------

def compute_topic_mastery_summary(item_results):
    """
    Computes holistic mastery score and 5-tier mastery state for a topic from evaluated items.

    Dimensions:
      - conceptual: mcq
      - comprehension: predict_output
      - practical: debug_code + write_code

    States:
      - Unassessed
      - Needs Attention
      - Developing
      - Proficient
      - Verified Mastery
    """
    if not item_results:
        return {
            "mastery_state": "Unassessed",
            "mastery_score": 0.0,
            "raw_accuracy": 0.0,
            "knowledge_level": "Unassessed",
            "dimensions": {
                "conceptual": None,
                "comprehension": None,
                "practical": None,
            },
            "types_attempted": [],
            "types_passed": [],
            "guessed_correct_count": 0,
            "overconfident_error_count": 0,
            "inconsistency_flags": [],
            "recommendation": "Complete questions for this topic to measure mastery.",
        }

    dim_totals = {"conceptual": [], "comprehension": [], "practical": []}
    types_attempted = []
    types_passed = []
    raw_fractions = []
    calibrated_fractions = []

    guessed_correct = 0
    overconfident_errors = 0
    correct_items = 0

    for item in item_results:
        qtype = item.get("question_type", "mcq")
        frac = float(item.get("score_fraction", 1.0 if item.get("is_correct") else 0.0))
        conf = _normalize_confidence(item.get("confidence"))
        is_ok = bool(item.get("is_correct") or frac >= 0.85)

        if qtype not in types_attempted:
            types_attempted.append(qtype)
        if is_ok and qtype not in types_passed:
            types_passed.append(qtype)

        if qtype == "mcq":
            dim_totals["conceptual"].append(frac)
        elif qtype == "predict_output":
            dim_totals["comprehension"].append(frac)
        else:
            dim_totals["practical"].append(frac)

        raw_fractions.append(frac)
        if is_ok:
            correct_items += 1
            if conf == "low":
                guessed_correct += 1
        elif frac < 0.5 and conf == "high":
            overconfident_errors += 1

        # High confidence on wrong answer yields zero credit.
        # Guessed low-confidence correct answer yields reduced certainty signal.
        conf_w = CONFIDENCE_WEIGHTS.get(conf, 0.90)
        calibrated_fractions.append(frac * conf_w)

    def _avg_pct(vals):
        return round((sum(vals) / len(vals)) * 100.0, 1) if vals else None

    conceptual_pct = _avg_pct(dim_totals["conceptual"])
    comprehension_pct = _avg_pct(dim_totals["comprehension"])
    practical_pct = _avg_pct(dim_totals["practical"])

    raw_accuracy = round((sum(raw_fractions) / len(raw_fractions)) * 100.0, 1)

    # Compute weighted holistic mastery score
    dim_weights = {"conceptual": 0.20, "comprehension": 0.30, "practical": 0.50}
    dim_scores = {
        "conceptual": conceptual_pct,
        "comprehension": comprehension_pct,
        "practical": practical_pct,
    }
    active_weight_sum = sum(w for d, w in dim_weights.items() if dim_scores[d] is not None)
    weighted_raw = (
        sum(dim_scores[d] * w for d, w in dim_weights.items() if dim_scores[d] is not None) / active_weight_sum
        if active_weight_sum > 0 else raw_accuracy
    )

    calibration_ratio = (
        (sum(calibrated_fractions) / sum(raw_fractions))
        if sum(raw_fractions) > 0 else 1.0
    )
    overconf_penalty = min(15.0, overconfident_errors * 6.0)
    mastery_score = round(max(0.0, min(100.0, weighted_raw * calibration_ratio - overconf_penalty)), 1)

    # Cap mastery score if only conceptual MCQs exist (cannot prove operational mastery from MCQs alone)
    if comprehension_pct is None and practical_pct is None:
        mastery_score = min(mastery_score, 60.0)

    # Diagnostic inconsistency flags
    flags = []
    tested_dim_values = [v for v in dim_scores.values() if v is not None]
    if len(tested_dim_values) >= 2 and (max(tested_dim_values) - min(tested_dim_values)) >= 45.0:
        flags.append("inconsistent_dimensions")

    if conceptual_pct is not None and conceptual_pct >= 75.0 and practical_pct is not None and practical_pct < 50.0:
        flags.append("conceptual_without_practical")

    if practical_pct is None:
        flags.append("missing_practical_evidence")

    guessed_heavy = (
        guessed_correct >= 2
        or (correct_items > 0 and (guessed_correct / correct_items) >= 0.5)
    )
    if guessed_heavy:
        flags.append("guessed_correct_answers")

    if overconfident_errors > 0:
        flags.append("overconfident_errors")

    # Classify into the 5 Topic Mastery States
    has_conceptual = conceptual_pct is not None and conceptual_pct >= 75.0
    has_comprehension = comprehension_pct is not None and comprehension_pct >= 75.0
    has_practical = practical_pct is not None and practical_pct >= 75.0

    if (
        len(item_results) >= 3
        and len(types_attempted) >= 3
        and has_conceptual
        and has_comprehension
        and has_practical
        and mastery_score >= 82.0
        and raw_accuracy >= 85.0
        and overconfident_errors == 0
        and not guessed_heavy
    ):
        mastery_state = "Verified Mastery"
    elif (
        mastery_score >= 70.0
        and raw_accuracy >= 70.0
        and practical_pct is not None
        and practical_pct >= 50.0
        and not guessed_heavy
        and overconfident_errors <= 1
    ):
        mastery_state = "Proficient"
    elif mastery_score >= 45.0 or raw_accuracy >= 50.0:
        mastery_state = "Developing"
    else:
        mastery_state = "Needs Attention"

    # Build targeted pedagogical feedback message
    if mastery_state == "Verified Mastery":
        rec = "Verified across concept, output tracing, and coding tasks."
    elif "conceptual_without_practical" in flags:
        rec = "Strong theory, but code writing/debugging needs practice before mastery."
    elif "guessed_correct_answers" in flags:
        rec = "Answers were marked with low confidence; solve practice problems to confirm stability."
    elif "overconfident_errors" in flags:
        rec = "High-confidence errors detected; review core concepts carefully."
    elif mastery_state == "Proficient":
        rec = "Solid grasp across tasks; complete one short verification problem to confirm Verified Mastery."
    elif mastery_state == "Developing":
        rec = "Partial understanding; strengthen debugging and practical coding through topic practice."
    else:
        rec = "Needs focused study and guided practice before advancing."

    k_level = (
        "Strong" if mastery_state in ("Verified Mastery", "Proficient")
        else ("Moderate" if mastery_state == "Developing" else "Weak")
    )

    return {
        "mastery_state": mastery_state,
        "mastery_score": mastery_score,
        "raw_accuracy": raw_accuracy,
        "knowledge_level": k_level,
        "dimensions": {
            "conceptual": conceptual_pct,
            "comprehension": comprehension_pct,
            "practical": practical_pct,
        },
        "types_attempted": types_attempted,
        "types_passed": types_passed,
        "guessed_correct_count": guessed_correct,
        "overconfident_error_count": overconfident_errors,
        "inconsistency_flags": flags,
        "recommendation": rec,
    }


def merge_topic_evidence(existing_evidence, new_item_results):
    """
    Updates cumulative evidence for a topic when the student completes targeted
    topic practice / reassessment (Continuous Learning Cycle).
    Replaces old per-type evidence with newer practice outcomes so students can
    advance from Needs Attention / Developing -> Proficient -> Verified Mastery
    without retaking the entire course initial assessment.
    """
    existing = existing_evidence if isinstance(existing_evidence, dict) else {}
    by_type = dict(existing.get("by_type") or {})

    # Update per-question-type evidence from the new practice attempt
    grouped_new = {}
    for item in new_item_results:
        qt = item.get("question_type", "mcq")
        grouped_new.setdefault(qt, []).append(item)

    for qt, items in grouped_new.items():
        # Store the evaluated items for this question type (latest reassessment supersedes prior weakness)
        by_type[qt] = items

    # Flatten all cumulative evidence items by question type
    merged_items = []
    for qt, items in by_type.items():
        if isinstance(items, list):
            merged_items.extend(items)

    if not merged_items:
        merged_items = list(new_item_results)

    summary = compute_topic_mastery_summary(merged_items)
    summary["by_type"] = by_type
    summary["practice_attempts"] = int(existing.get("practice_attempts", 0)) + 1
    return summary


# -------------------------------------------------------------------------
# Assessment Submission Evaluation
# -------------------------------------------------------------------------

def _extract_answer_and_confidence(q, user_answers, user_confidences=None):
    """Extracts student's answer & confidence cleanly across ID-keyed, num-keyed, or list payloads."""
    q_id = q.get("id")
    q_num = q.get("question_number")

    ans = ""
    conf = None

    if isinstance(user_answers, dict):
        # Support keyed by q.id (str or int) OR question_number (str or int)
        candidates = []
        if q_id is not None:
            candidates.extend([str(q_id), q_id])
        if q_num is not None:
            candidates.extend([str(q_num), q_num])

        # Prefer q_id key if distinct keys exist, else question_number
        found_val = None
        for key in candidates:
            if key in user_answers:
                found_val = user_answers[key]
                break

        if isinstance(found_val, dict):
            ans = found_val.get("answer", "")
            conf = found_val.get("confidence")
        elif found_val is not None:
            ans = found_val
    elif isinstance(user_answers, list) and q_num and q_num <= len(user_answers):
        entry = user_answers[q_num - 1]
        if isinstance(entry, dict):
            ans = entry.get("answer", "")
            conf = entry.get("confidence")
        else:
            ans = entry

    if conf is None and isinstance(user_confidences, dict):
        for key in ([str(q_id), q_id] if q_id is not None else []) + ([str(q_num), q_num] if q_num is not None else []):
            if key in user_confidences:
                conf = user_confidences[key]
                break

    return str(ans or ""), _normalize_confidence(conf)


def _evaluate_all_questions(questions, user_answers, user_confidences=None):
    """
    Runs objective evaluation across all question types (MCQ, Predict Output,
    Debug Code, Write Code), batching Python and Java test execution efficiently.
    """
    extracted = []
    py_tasks = []
    java_tasks = []

    for q in questions:
        ans, conf = _extract_answer_and_confidence(q, user_answers, user_confidences)
        qtype = q.get("question_type", "mcq")
        raw_spec = q.get("evaluation_spec_json") or "{}"
        try:
            eval_spec = json.loads(raw_spec) if isinstance(raw_spec, str) else dict(raw_spec)
        except Exception:
            eval_spec = {}

        qid_key = str(q.get("id") or q.get("question_number"))
        extracted.append((q, qid_key, ans, conf, qtype, eval_spec))

        if qtype in ("debug_code", "write_code"):
            lang = eval_spec.get("language", "python").lower()
            task = {
                "id": qid_key,
                "code": ans,
                "question_type": qtype,
                "code_snippet": q.get("code_snippet", ""),
                "eval_spec": eval_spec,
            }
            if lang == "java":
                java_tasks.append(task)
            else:
                py_tasks.append(task)

    py_evals = evaluate_python_code_batch(py_tasks) if py_tasks else {}
    java_evals = evaluate_java_code_batch(java_tasks) if java_tasks else {}

    evaluated_by_topic = {}
    all_item_evals = []

    for q, qid_key, ans, conf, qtype, eval_spec in extracted:
        if qtype == "mcq":
            given = ans.strip().upper()
            expected = str(q.get("correct_answer", "")).strip().upper()
            is_ok = (given == expected and given != "")
            eval_res = {
                "is_correct": is_ok,
                "score_fraction": 1.0 if is_ok else 0.0,
                "passed_cases": 1 if is_ok else 0,
                "total_cases": 1,
                "feedback": "Correct conceptual choice." if is_ok else f"Selected '{given or 'None'}', expected '{expected}'.",
            }
        elif qtype == "predict_output":
            eval_res = evaluate_predict_output(ans, eval_spec, q.get("correct_answer", ""))
        elif qtype == "fill_blank":
            eval_res = evaluate_fill_blank(ans, eval_spec, q.get("correct_answer", ""))
        elif qtype in ("debug_code", "write_code"):
            lang = eval_spec.get("language", "python").lower()
            if lang == "java":
                eval_res = java_evals.get(qid_key, {"is_correct": False, "score_fraction": 0.0, "passed_cases": 0, "total_cases": 1, "feedback": "Not evaluated."})
            else:
                eval_res = py_evals.get(qid_key, {"is_correct": False, "score_fraction": 0.0, "passed_cases": 0, "total_cases": 1, "feedback": "Not evaluated."})
        else:
            eval_res = {"is_correct": False, "score_fraction": 0.0, "passed_cases": 0, "total_cases": 1, "feedback": "Unsupported question type."}

        # Format expected display safely for result feedback screen
        expected_display = str(q.get("correct_answer", "") or "")
        if qtype == "mcq":
            opt_letter = expected_display.strip().upper()
            opt_val = q.get(f"option_{opt_letter.lower()}", "") if opt_letter in ("A", "B", "C", "D") else ""
            if opt_val:
                expected_display = f"{opt_letter}. {opt_val}"

        item_record = {
            "question_id": q.get("id"),
            "question_number": q.get("question_number"),
            "topic_id": q["topic_id"],
            "topic_name": q.get("topic_name", f"Topic {q['topic_id']}"),
            "question_type": qtype,
            "question_text": q["question_text"],
            "code_snippet": q.get("code_snippet", "") or "",
            "given_answer": ans,
            "expected_answer": expected_display,
            "confidence": conf,
            "is_correct": bool(eval_res["is_correct"]),
            "score_fraction": float(eval_res["score_fraction"]),
            "passed_cases": eval_res.get("passed_cases", 0),
            "total_cases": eval_res.get("total_cases", 1),
            "feedback": eval_res.get("feedback", ""),
            "explanation": eval_spec.get("explanation", ""),
            "weak_concept": eval_spec.get("weak_concept", ""),
            "recommended_subtopic": eval_spec.get("recommended_subtopic", ""),
        }
        all_item_evals.append(item_record)
        evaluated_by_topic.setdefault(q["topic_id"], []).append(item_record)

    return all_item_evals, evaluated_by_topic


def calculate_topic_scores(questions, user_answers, user_confidences=None):
    """
    Evaluates all question types objectively, computes multi-dimensional evidence &
    confidence calibration per topic, and returns full breakdown.
    """
    all_items, by_topic = _evaluate_all_questions(questions, user_answers, user_confidences)

    topic_scores = {}
    total_correct = 0
    total_q = len(questions)

    # Preserve topic display order
    topic_order = []
    for q in questions:
        tid = q["topic_id"]
        if tid not in topic_order:
            topic_order.append(tid)

    for tid in topic_order:
        items = by_topic.get(tid, [])
        t_name = items[0]["topic_name"] if items else f"Topic {tid}"
        correct_count = sum(1 for it in items if it["is_correct"])
        total_correct += correct_count
        total_topic_q = len(items)
        score_pct = round((correct_count / total_topic_q) * 100.0, 1) if total_topic_q > 0 else 0.0

        mastery_summary = compute_topic_mastery_summary(items)
        by_type = {}
        for it in items:
            by_type.setdefault(it["question_type"], []).append(it)
        mastery_summary["by_type"] = by_type

        topic_scores[tid] = {
            "topic_id": tid,
            "topic_name": t_name,
            "correct_answers": correct_count,
            "total_questions": total_topic_q,
            "score_percentage": score_pct,
            "knowledge_level": mastery_summary["knowledge_level"],
            "mastery_state": mastery_summary["mastery_state"],
            "mastery_score": mastery_summary["mastery_score"],
            "dimensions": mastery_summary["dimensions"],
            "guessed_correct_count": mastery_summary["guessed_correct_count"],
            "overconfident_error_count": mastery_summary["overconfident_error_count"],
            "inconsistency_flags": mastery_summary["inconsistency_flags"],
            "recommendation": mastery_summary["recommendation"],
            "evidence": mastery_summary,
            "questions": items,
        }

    overall_pct = round((total_correct / total_q) * 100.0, 1) if total_q > 0 else 0.0

    return {
        "topic_scores": topic_scores,
        "total_correct": total_correct,
        "total_questions": total_q,
        "overall_percentage": overall_pct,
        "item_results": all_items,
    }


def evaluate_assessment(course_id, questions, user_answers, user_confidences=None):
    """Executes multi-type scoring and returns complete summary."""
    result = calculate_topic_scores(questions, user_answers, user_confidences=user_confidences)
    result["course_id"] = course_id
    return result


def evaluate_topic_mastery_assessment(topic_name, questions, user_answers):
    """
    Evaluates the 5-question Topic Mastery Assessment (1 mark per question, pass >= 4/5 = 80%).
    Returns detailed mastery outcome, weak concepts, and recommended subtopics for revision.
    """
    all_items, _ = _evaluate_all_questions(questions, user_answers, user_confidences=None)
    total_q = len(all_items)
    score = sum(1 for it in all_items if it["is_correct"])
    pct = round((score / total_q) * 100.0, 1) if total_q > 0 else 0.0
    pass_score = 4 if total_q == 5 else max(1, int(round(total_q * 0.8)))
    passed = score >= pass_score

    study_info = get_topic_study_notes(topic_name)
    subtopics = study_info.get("subtopics") or []
    subtopic_idx_map = {s["title"]: i for i, s in enumerate(subtopics)}

    incorrect_items = []
    weak_concepts = []
    recommended_subtopics = []
    seen_sub_titles = set()

    for idx, it in enumerate(all_items, start=1):
        it["display_number"] = idx
        if not it["is_correct"]:
            wc = it.get("weak_concept") or it["question_type"].replace("_", " ").title()
            if wc not in weak_concepts:
                weak_concepts.append(wc)

            rec_sub = it.get("recommended_subtopic") or ""
            if not rec_sub and subtopics:
                rec_sub = subtopics[(idx - 1) % len(subtopics)]["title"]
            if rec_sub and rec_sub not in seen_sub_titles:
                seen_sub_titles.add(rec_sub)
                recommended_subtopics.append({
                    "title": rec_sub,
                    "index": subtopic_idx_map.get(rec_sub, 0),
                })

            incorrect_items.append({
                "display_number": idx,
                "question_type": it["question_type"],
                "question_text": it["question_text"],
                "code_snippet": it.get("code_snippet", ""),
                "student_answer": it["given_answer"] or "(No answer)",
                "correct_answer": it["expected_answer"],
                "feedback": it["feedback"],
                "explanation": it["explanation"],
                "weak_concept": wc,
                "recommended_subtopic": rec_sub,
                "recommended_subtopic_index": subtopic_idx_map.get(rec_sub, 0),
            })

    mastery_state = "Proficient" if passed else "Needs Attention"
    topic_status = "Completed" if passed else "Needs Attention"

    summary = compute_topic_mastery_summary(all_items)
    summary["mastery_state"] = mastery_state
    summary["mastery_score"] = pct
    summary["raw_accuracy"] = pct
    summary["weak_concepts"] = weak_concepts
    summary["recommended_subtopics"] = recommended_subtopics
    if passed:
        summary["recommendation"] = f"Proficient ({score}/{total_q}, {pct}%). Topic marked Completed."
    else:
        summary["recommendation"] = (
            f"Scored {score}/{total_q} ({pct}%). Revise {', '.join(weak_concepts) or topic_name} and score at least {pass_score}/{total_q} to pass."
        )

    return {
        "passed": passed,
        "score": score,
        "total_questions": total_q,
        "pass_score": pass_score,
        "score_percentage": pct,
        "mastery_state": mastery_state,
        "topic_status": topic_status,
        "incorrect_items": incorrect_items,
        "weak_concepts": weak_concepts,
        "recommended_subtopics": recommended_subtopics,
        "item_results": all_items,
        "summary": summary,
    }


def sanitize_questions_for_client(questions):
    """
    Security safeguard: Removes 'correct_answer' and hidden test cases ('evaluation_spec_json')
    before sending JSON to the client browser.
    """
    sanitized = []
    for q in questions:
        qtype = q.get("question_type", "mcq")
        raw_spec = q.get("evaluation_spec_json") or "{}"
        try:
            spec = json.loads(raw_spec) if isinstance(raw_spec, str) else dict(raw_spec)
        except Exception:
            spec = {}

        item = {
            "id": q["id"],
            "number": q["question_number"],
            "text": q["question_text"],
            "question_type": qtype,
            "code_snippet": q.get("code_snippet", "") or "",
            "language": spec.get("language", "python"),
            "topic_id": q["topic_id"],
            "topic_name": q.get("topic_name", ""),
        }
        if qtype == "mcq":
            item["options"] = {
                "A": q["option_a"],
                "B": q["option_b"],
                "C": q["option_c"],
                "D": q["option_d"],
            }
        else:
            item["options"] = None
        sanitized.append(item)
    return sanitized


# -------------------------------------------------------------------------
# Structured Topic Study Content (Subtopics + Learning Material + References)
# -------------------------------------------------------------------------

def _ref(title, url, source, note=""):
    return {"title": title, "url": url, "source": source, "note": note}


def _subtopic(slug, title, content, key_points=None, example="", references=None):
    return {
        "id": slug,
        "title": title,
        "content": content if isinstance(content, list) else [content],
        "key_points": key_points or [],
        "example": example or "",
        "references": references or [],
    }


TOPIC_STUDY_NOTES = {
    "Variables and Data Types": {
        "summary": "Variables bind names to objects in memory. Core primitive types are int, float, str, and bool.",
        "key_points": [
            "Variable names use letters, digits, and underscores, never starting with a digit.",
            "Strings are quoted text; integers have no decimal point; floats have decimals.",
            "Convert explicitly with int(), float(), and str() before mixing text and numbers.",
        ],
        "example": "count = 5\nprice = 2.5\ntotal = count * price\nprint(f'Total: {total}')",
        "references": [
            _ref("Python Official Tutorial: Numbers and Strings", "https://docs.python.org/3/tutorial/introduction.html", "Python Docs", "Official beginner introduction to variables and basic values."),
            _ref("Python Built-in Types Documentation", "https://docs.python.org/3/library/stdtypes.html", "Python Docs", "Complete reference for int, float, bool, and str types."),
            _ref("W3Schools Python Variables & Data Types", "https://www.w3schools.com/python/python_variables.asp", "W3Schools", "Quick examples and interactive overview."),
        ],
        "subtopics": [
            _subtopic(
                "intro-variables",
                "Introduction to Variables",
                [
                    "A variable is a named label that stores a value in your program so you can reuse or update it later. Instead of repeating raw values, assigning a name makes code easier to read and maintain.",
                    "In Python, you assign a value using the equals operator (=). Python automatically determines the type of value stored without needing a separate type declaration."
                ],
                ["Assignment syntax is name = value.", "Reassigning a variable updates what value the name points to."],
                "name = 'Ada'\nage = 20\nage = age + 1\nprint(name, age)  # Ada 21",
                [
                    _ref("An Informal Introduction to Python", "https://docs.python.org/3/tutorial/introduction.html", "Python Docs"),
                    _ref("Variables in Python", "https://realpython.com/python-variables/", "Real Python"),
                ],
            ),
            _subtopic(
                "naming-rules",
                "Variable Declaration and Naming Rules",
                [
                    "Valid Python variable names may contain letters (a-z, A-Z), digits (0-9), and underscores (_). A name can never begin with a digit, and spaces or hyphens are not allowed.",
                    "Variable names are case-sensitive (score and Score are different variables) and cannot match reserved Python keywords like for, if, class, or return."
                ],
                ["Start with a letter or underscore.", "Use lowercase snake_case names like student_score for readability.", "Avoid built-in names like list, str, or sum."],
                "student_name = 'Ravi'\n_temp_count = 3\n# invalid: 2nd_score = 10, user-age = 18",
                [
                    _ref("PEP 8 Naming Conventions", "https://peps.python.org/pep-0008/#naming-conventions", "Python PEP 8"),
                    _ref("Python Keywords List", "https://docs.python.org/3/reference/lexical_analysis.html#keywords", "Python Docs"),
                ],
            ),
            _subtopic(
                "python-data-types",
                "Python Data Types",
                [
                    "Every value in Python belongs to a data type, which defines what operations work on it. Core built-in types include numbers (int, float), text (str), truth values (bool), and None.",
                    "You can inspect the data type of any value or variable at runtime using the built-in type() function."
                ],
                ["int: whole numbers; float: decimals; str: text; bool: True or False.", "Use type(x) to verify what kind of value a variable holds."],
                "print(type(42))      # <class 'int'>\nprint(type(3.14))    # <class 'float'>\nprint(type('hi'))    # <class 'str'>\nprint(type(True))    # <class 'bool'>",
                [
                    _ref("Built-in Types", "https://docs.python.org/3/library/stdtypes.html", "Python Docs"),
                    _ref("Basic Data Types in Python", "https://realpython.com/python-data-types/", "Real Python"),
                ],
            ),
            _subtopic(
                "int-and-float",
                "Integers and Floating-Point Numbers",
                [
                    "Integers (int) store exact whole numbers, positive or negative, of any size (such as -5, 0, 1024). Floating-point numbers (float) store decimal or fractional numbers (such as 2.5, -0.75, 1.0).",
                    "Standard division (/) always produces a float in Python (for example 8 / 2 gives 4.0), whereas floor division (//) drops the fractional remainder."
                ],
                ["No decimal point creates an int; adding .0 creates a float.", "Watch out for tiny decimal rounding artifacts in floating-point arithmetic; use round(val, 2) for currency."],
                "items = 4\nprice = 12.50\ntotal = items * price\nprint(round(total, 2))  # 50.0",
                [
                    _ref("Using Python as a Calculator (Numbers)", "https://docs.python.org/3/tutorial/introduction.html#numbers", "Python Docs"),
                    _ref("Numeric Types — int, float, complex", "https://docs.python.org/3/library/stdtypes.html#numeric-types-int-float-complex", "Python Docs"),
                ],
            ),
            _subtopic(
                "strings-and-booleans",
                "Strings and Boolean Values",
                [
                    "Strings (str) hold text wrapped inside matching single ('...') or double (\"...\") quotes. You can join strings with +, repeat them with *, and format values cleanly using f-strings.",
                    "Booleans (bool) have exactly two values: True and False (capitalized). They result from comparisons and control program decisions."
                ],
                ["Use f\"Hello {name}\" for readable string formatting.", "Boolean literals True and False always start with capital letters."],
                "course = 'Python'\nis_active = True\nprint(f'{course} active: {is_active}')",
                [
                    _ref("Python Text Sequence Type — str", "https://docs.python.org/3/library/stdtypes.html#text-sequence-type-str", "Python Docs"),
                    _ref("Boolean Values in Python", "https://docs.python.org/3/library/stdtypes.html#truth-value-testing", "Python Docs"),
                ],
            ),
            _subtopic(
                "type-conversion",
                "Type Conversion",
                [
                    "When data comes from user input or files, numbers often arrive as strings (like '45'). Adding a string and an integer directly raises a TypeError.",
                    "Explicit type conversion functions such as int(), float(), str(), and bool() safely convert values into the required type before calculations or display."
                ],
                ["int('25') -> 25; float('3.5') -> 3.5; str(100) -> '100'.", "int('abc') raises ValueError because letters cannot convert to a base-10 integer."],
                "marks_input = '85'\nbonus = 5\nfinal_score = int(marks_input) + bonus\nprint('Final: ' + str(final_score))",
                [
                    _ref("Built-in Functions: int(), float(), str()", "https://docs.python.org/3/library/functions.html", "Python Docs"),
                    _ref("W3Schools Python Casting", "https://www.w3schools.com/python/python_casting.asp", "W3Schools"),
                ],
            ),
            _subtopic(
                "working-variables-types",
                "Working with Variables and Data Types",
                [
                    "Real programs combine multiple types together: reading string inputs, converting numeric fields to integers or floats, tracking flags with booleans, and formatting summary output.",
                    "Checking types early and naming variables clearly prevents subtle type bugs such as string concatenation ('5' + '2' -> '52') when numeric addition (5 + 2 -> 7) was intended."
                ],
                ["Always convert string numbers before arithmetic.", "Keep one consistent meaning per variable name."],
                "unit_price = float('19.99')\nqty = int('3')\nhas_discount = False\nsubtotal = round(unit_price * qty, 2)\nprint(f'Items: {qty}, Subtotal: {subtotal}')",
                [
                    _ref("Python Tutorial Chapter 3", "https://docs.python.org/3/tutorial/introduction.html", "Python Docs"),
                    _ref("GeeksforGeeks Python Variables", "https://www.geeksforgeeks.org/python-variables/", "GeeksforGeeks"),
                ],
            ),
        ],
    },

    "Operators and Conditions": {
        "summary": "Use comparison (==, !=, <, >) and logical operators (and, or, not) to branch program flow.",
        "key_points": [
            "= assigns a value; == compares two values for equality.",
            "// performs floor integer division; % returns the remainder.",
            "if / elif / else executes only the first matching branch.",
        ],
        "example": "score = 72\nif score >= 80:\n    print('High')\nelif score >= 50:\n    print('Pass')",
        "references": [
            _ref("Python Control Flow Tools (if Statements)", "https://docs.python.org/3/tutorial/controlflow.html#if-statements", "Python Docs"),
            _ref("Python Operators Guide", "https://www.w3schools.com/python/python_operators.asp", "W3Schools"),
        ],
        "subtopics": [
            _subtopic(
                "arithmetic-assignment",
                "Arithmetic and Assignment Operators",
                ["Python supports +, -, *, / (true division), // (floor division), % (modulo remainder), and ** (exponentiation), along with compound assignment (+=, -=, *=)."],
                ["// rounds down toward negative infinity.", "% returns the remainder after division (useful for even/odd checks: n % 2 == 0)."],
                "a, b = 14, 4\nprint(a / b, a // b, a % b)  # 3.5 3 2",
                [_ref("Python Operators Reference", "https://www.w3schools.com/python/python_operators.asp", "W3Schools")],
            ),
            _subtopic(
                "comparison-logical",
                "Comparison and Logical Operators",
                ["Comparison operators (==, !=, <, <=, >, >=) evaluate relationships and return True or False. Logical operators (and, or, not) combine conditions."],
                ["Never confuse assignment (=) with equality comparison (==).", "Python lets you chain comparisons cleanly, e.g., 10 <= x <= 20."],
                "age = 20\nhas_id = True\nprint(age >= 18 and has_id)  # True",
                [_ref("Comparisons & Boolean Operations", "https://docs.python.org/3/library/stdtypes.html#boolean-operations-and-or-not", "Python Docs")],
            ),
            _subtopic(
                "if-elif-else",
                "Conditional Branching with if, elif, and else",
                ["Conditional statements choose which code block runs depending on boolean expressions. Indentation defines which lines belong inside each branch."],
                ["Only the first True branch runs in an if / elif / else chain.", "Always end each header line with a colon (:)."],
                "marks = 82\nif marks >= 90:\n    grade = 'A'\nelif marks >= 75:\n    grade = 'B'\nelse:\n    grade = 'C'",
                [_ref("More Control Flow Tools: if Statements", "https://docs.python.org/3/tutorial/controlflow.html#if-statements", "Python Docs")],
            ),
            _subtopic(
                "nested-and-ternary",
                "Nested Conditions and Conditional Expressions",
                ["You can place an if inside another if for multi-step checks, or write concise inline expressions like value_if_true if cond else value_if_false."],
                ["Keep nesting shallow when possible by combining with 'and'."],
                "label = 'Even' if num % 2 == 0 else 'Odd'",
                [_ref("Conditional Statements in Python", "https://realpython.com/python-conditional-statements/", "Real Python")],
            ),
        ],
    },

    "Loops": {
        "summary": "Loops repeat actions cleanly over ranges or sequences using for and while.",
        "key_points": [
            "range(start, stop) stops right before 'stop'.",
            "break exits the nearest loop immediately; continue skips to the next iteration.",
            "Initialize accumulator variables (e.g., total = 0) before the loop starts.",
        ],
        "example": "total = 0\nfor n in range(1, 5):\n    total += n\nprint(total)  # 10",
        "references": [
            _ref("Python for Statements", "https://docs.python.org/3/tutorial/controlflow.html#for-statements", "Python Docs"),
            _ref("Python Loops Guide", "https://www.w3schools.com/python/python_for_loops.asp", "W3Schools"),
        ],
        "subtopics": [
            _subtopic(
                "for-and-range",
                "The for Loop and range()",
                ["A for loop iterates sequentially over elements of any sequence such as a list, string, or range(). range(a, b) generates integers starting at a and stopping before b."],
                ["range(5) produces 0, 1, 2, 3, 4.", "range(start, stop, step) supports custom increments and counting down."],
                "for i in range(1, 4):\n    print(i)",
                [_ref("The range() Function", "https://docs.python.org/3/tutorial/controlflow.html#the-range-function", "Python Docs")],
            ),
            _subtopic(
                "while-loops",
                "Condition-Controlled while Loops",
                ["A while loop keeps repeating as long as its condition evaluates to True. Make sure variables used in the condition are updated inside the loop so it terminates."],
                ["Use while when the number of iterations isn't known upfront.", "Check loop bounds carefully to prevent infinite loops."],
                "n = 3\nwhile n > 0:\n    print(n)\n    n -= 1",
                [_ref("Python while Loops", "https://realpython.com/python-while-loop/", "Real Python")],
            ),
            _subtopic(
                "break-continue",
                "Controlling Flow with break and continue",
                ["break exits the enclosing loop immediately, while continue jumps directly to the next iteration without running the remaining statements in the current cycle."],
                ["break stops early once a target item is found.", "continue filters out unwanted iterations cleanly."],
                "for x in [1, -2, 3, 0, 5]:\n    if x < 0: continue\n    if x == 0: break\n    print(x)",
                [_ref("break and continue Statements", "https://docs.python.org/3/tutorial/controlflow.html#break-and-continue-statements", "Python Docs")],
            ),
            _subtopic(
                "accumulators-nested",
                "Accumulators and Nested Loops",
                ["Accumulators initialize a total or result variable outside the loop and update it on each step. Nested loops place one loop inside another to walk pairs, grids, or tables."],
                ["Always reset inner totals inside the outer loop when aggregating per row."],
                "total = 0\nfor val in [4, 6, 10]:\n    total += val\nprint(total)  # 20",
                [_ref("Python for Loops Tutorial", "https://realpython.com/python-for-loop/", "Real Python")],
            ),
        ],
    },

    "Functions": {
        "summary": "Functions package reusable logic with parameters and explicit return values.",
        "key_points": [
            "Define functions with def name(params): and send values back using return.",
            "Printing inside a function does not return a value (functions without return yield None).",
            "Keep functions pure and predictable when transforming input arguments.",
        ],
        "example": "def square(x):\n    return x * x\nprint(square(4))  # 16",
        "references": [
            _ref("Defining Functions in Python", "https://docs.python.org/3/tutorial/controlflow.html#defining-functions", "Python Docs"),
            _ref("Defining Your Own Python Function", "https://realpython.com/defining-your-own-python-function/", "Real Python"),
        ],
        "subtopics": [
            _subtopic(
                "defining-calling",
                "Defining and Calling Functions",
                ["Functions group statements under a reusable name using the def keyword. You invoke a function by writing its name followed by parentheses containing any required arguments."],
                ["Use descriptive lowercase names like compute_total.", "Call a function only after it has been defined."],
                "def greet():\n    print('Welcome!')\ngreet()",
                [_ref("Defining Functions", "https://docs.python.org/3/tutorial/controlflow.html#defining-functions", "Python Docs")],
            ),
            _subtopic(
                "parameters-return",
                "Parameters vs Arguments and Return Values",
                ["Parameters act as input placeholders inside the function header, while arguments are concrete values passed during a call. The return statement ends the function and hands back a result."],
                ["print() displays output on screen; return sends data back to the caller.", "Functions reaching the end without return automatically return None."],
                "def add(a, b):\n    return a + b\nresult = add(3, 5)",
                [_ref("Python Functions Tutorial", "https://www.w3schools.com/python/python_functions.asp", "W3Schools")],
            ),
            _subtopic(
                "default-keyword-args",
                "Default and Keyword Arguments",
                ["Parameters can specify default values (def power(x, exp=2):). Callers can pass arguments by position or by keyword name for extra clarity."],
                ["Place parameters with default values after non-default parameters."],
                "def greet(name, msg='Hello'):\n    return f'{msg}, {name}'",
                [_ref("More on Defining Functions", "https://docs.python.org/3/tutorial/controlflow.html#more-on-defining-functions", "Python Docs")],
            ),
            _subtopic(
                "variable-scope",
                "Local vs Global Variable Scope",
                ["Variables created inside a function exist only within that function (local scope) and vanish once the function returns. Prefer passing parameters and returning results over mutating globals."],
                ["Local variables cannot be accessed directly from outside the function."],
                "def calc(x):\n    doubled = x * 2\n    return doubled",
                [_ref("Python Scope (LEGB Rule)", "https://realpython.com/python-scope-legb-rule/", "Real Python")],
            ),
        ],
    },

    "Lists, Tuples, Sets, and Dictionaries": {
        "summary": "Choose Python collections based on mutability, ordering, uniqueness, and key lookup.",
        "key_points": [
            "Lists [] are mutable and ordered; tuples () are immutable.",
            "Sets {} store unique unordered items efficiently.",
            "Dictionaries {key: value} map unique keys to values with fast O(1) lookup.",
        ],
        "example": "scores = {'alice': 90, 'bob': 85}\nscores['alice'] += 5",
        "references": [
            _ref("Python Data Structures Tutorial", "https://docs.python.org/3/tutorial/datastructures.html", "Python Docs"),
            _ref("W3Schools Python Collections", "https://www.w3schools.com/python/python_lists.asp", "W3Schools"),
        ],
        "subtopics": [
            _subtopic(
                "lists-and-methods",
                "Lists and Common List Operations",
                ["A list ([1, 2, 3]) stores ordered elements that can grow, shrink, or change in place. Useful methods include append(), extend(), pop(), remove(), and sort()."],
                ["Lists preserve insertion order and allow duplicate elements.", "Indexing starts at 0; -1 refers to the last element."],
                "nums = [10, 20]\nnums.append(30)\nprint(nums[-1])  # 30",
                [_ref("More on Lists", "https://docs.python.org/3/tutorial/datastructures.html#more-on-lists", "Python Docs")],
            ),
            _subtopic(
                "slicing-and-tuples",
                "Slicing Sequences and Immutable Tuples",
                ["Slicing seq[start:stop:step] extracts sub-sequences without altering the original sequence. Tuples ((10, 20)) behave like lists except they cannot be modified after creation."],
                ["Use tuples for fixed records such as (x, y) coordinates.", "Slices create shallow copies cleanly."],
                "coords = (3, 7)\nitems = [1, 2, 3, 4]\nprint(items[1:3])  # [2, 3]",
                [_ref("Tuples and Sequences", "https://docs.python.org/3/tutorial/datastructures.html#tuples-and-sequences", "Python Docs")],
            ),
            _subtopic(
                "sets-uniqueness",
                "Sets and Uniqueness",
                ["A set ({'a', 'b'}) holds distinct, unordered elements. Sets make deduplication and membership checks (x in my_set) very fast."],
                ["Create an empty set with set(), NOT {} (which makes an empty dictionary).", "Sets support union (|), intersection (&), and difference (-)."],
                "unique = set([1, 2, 2, 3])\nprint(2 in unique)  # True",
                [_ref("Python Sets Documentation", "https://docs.python.org/3/tutorial/datastructures.html#sets", "Python Docs")],
            ),
            _subtopic(
                "dictionaries",
                "Dictionaries and Key-Value Mapping",
                ["Dictionaries store pairs of unique keys and associated values ({'name': 'Ada', 'score': 95}). Looking up or updating a key takes O(1) average time."],
                ["Use d.get(key, default) to avoid KeyError when a key might be missing.", "Loop over pairs with for k, v in d.items():."],
                "counts = {}\nfor c in 'aba':\n    counts[c] = counts.get(c, 0) + 1",
                [_ref("Dictionaries in Python", "https://docs.python.org/3/tutorial/datastructures.html#dictionaries", "Python Docs")],
            ),
        ],
    },

    "Object-Oriented Programming": {
        "summary": "Classes bundle state (attributes) and behavior (methods) into reusable blueprints.",
        "key_points": [
            "__init__(self, ...) initializes attributes when an instance is created.",
            "Instance methods always accept self as their first parameter.",
            "Subclasses inherit attributes and methods from a base class via class Child(Parent):",
        ],
        "example": "class Counter:\n    def __init__(self, start=0):\n        self.value = start\n    def inc(self):\n        self.value += 1",
        "references": [
            _ref("Python Classes Tutorial", "https://docs.python.org/3/tutorial/classes.html", "Python Docs"),
            _ref("OOP in Python", "https://realpython.com/python3-object-oriented-programming/", "Real Python"),
        ],
        "subtopics": [
            _subtopic(
                "classes-objects",
                "Classes, Objects, and __init__",
                ["A class defines a custom blueprint; calling the class creates an object (instance). The __init__(self, ...) method runs automatically on creation to set initial attributes."],
                ["self refers to the current object instance.", "Assign instance fields with self.attr = value."],
                "class Student:\n    def __init__(self, name):\n        self.name = name\ns = Student('Maya')",
                [_ref("Class Objects and Instance Objects", "https://docs.python.org/3/tutorial/classes.html#class-objects", "Python Docs")],
            ),
            _subtopic(
                "methods-encapsulation",
                "Instance Methods and Object State",
                ["Instance methods are functions defined inside a class that read or modify the object's own self attributes."],
                ["Always list self as the first parameter of an instance method."],
                "class Wallet:\n    def __init__(self, bal=0): self.bal = bal\n    def add(self, amt): self.bal += amt",
                [_ref("Python Classes & Objects Guide", "https://www.w3schools.com/python/python_classes.asp", "W3Schools")],
            ),
            _subtopic(
                "inheritance-overriding",
                "Inheritance, super(), and Method Overriding",
                ["A child class inherits attributes and methods from a parent class via class Child(Parent):. Child classes can customize behavior by redefining methods or calling super()."],
                ["super().__init__(...) reuses parent initialization cleanly."],
                "class Animal:\n    def speak(self): return '...'\nclass Dog(Animal):\n    def speak(self): return 'Woof'",
                [_ref("Python Inheritance", "https://docs.python.org/3/tutorial/classes.html#inheritance", "Python Docs")],
            ),
        ],
    },

    "Programming Basics": {
        "summary": "Core algorithmic thinking relies on time complexity analysis and foundational storage models.",
        "key_points": [
            "Direct array indexing is O(1); single-pass traversal is O(n).",
            "Stacks use LIFO (Last In First Out); Queues use FIFO (First In First Out).",
            "Swap two variables safely or track min/max across an iterable in one pass.",
        ],
        "example": "def find_max(nums):\n    best = nums[0]\n    for x in nums:\n        if x > best: best = x\n    return best",
        "references": [
            _ref("Python TimeComplexity Wiki", "https://wiki.python.org/moin/TimeComplexity", "Python Wiki"),
            _ref("Big-O Notation Explained", "https://www.geeksforgeeks.org/analysis-algorithms-big-o-analysis/", "GeeksforGeeks"),
        ],
        "subtopics": [
            _subtopic(
                "algorithm-complexity",
                "Algorithmic Thinking and Big-O Notation",
                ["Big-O describes how runtime or memory grows as input size n increases. O(1) runs in constant time, O(log n) halves the search space each step, O(n) scans once, and O(n^2) compares all pairs."],
                ["Drop constant factors (3n + 5 is O(n)).", "Array access arr[i] is O(1)."],
                "def contains(arr, target):\n    return any(x == target for x in arr)  # O(n)",
                [_ref("Asymptotic Analysis & Big-O", "https://www.geeksforgeeks.org/analysis-algorithms-big-o-analysis/", "GeeksforGeeks")],
            ),
            _subtopic(
                "single-pass-patterns",
                "Swapping and Single-Pass Linear Scans",
                ["Many introductory DS problems track running minimum, maximum, or complement matches in a single loop. Python lets you swap variables concisely with a, b = b, a."],
                ["Initialize min/max trackers to arr[0] for non-empty arrays."],
                "m = arr[0]\nfor x in arr:\n    if x < m: m = x",
                [_ref("Data Structures Intro", "https://www.w3schools.com/dsa/", "W3Schools DSA")],
            ),
            _subtopic(
                "choosing-structures",
                "Matching Problems to Data Structures",
                ["Choosing the right structure turns O(n^2) brute force into O(n) solutions — for instance using a set or dictionary to check pair sums in one pass."],
                ["Sets and dicts provide fast O(1) lookup.", "Stacks track last-seen states; queues preserve arrival order."],
                "seen = set()\nfor x in nums:\n    if target - x in seen: return True\n    seen.add(x)",
                [_ref("VisuAlgo Data Structure Visualizations", "https://visualgo.net/en", "VisuAlgo")],
            ),
        ],
    },

    "Arrays and Strings": {
        "summary": "Arrays store indexed sequences; strings are immutable character sequences supporting slicing.",
        "key_points": [
            "Zero-based indexing starts at 0; last element is at index -1 or len(arr) - 1.",
            "In Python strings are immutable, so transformations return new strings.",
            "Two-pointer scans reverse or compare symmetric elements in O(n) time.",
        ],
        "example": "text = 'learn'\nprint(text[::-1])  # 'nrael'",
        "references": [
            _ref("Array Data Structure", "https://www.geeksforgeeks.org/array-data-structure/", "GeeksforGeeks"),
            _ref("Two Pointers Technique", "https://www.geeksforgeeks.org/two-pointers-technique/", "GeeksforGeeks"),
        ],
        "subtopics": [
            _subtopic(
                "array-indexing",
                "Array Memory Model and Indexing",
                ["Arrays place items contiguously so computing any position arr[i] takes constant O(1) time. Inserting or removing at the front requires shifting later items in O(n) time."],
                ["Append at end is O(1) amortized; insert at index 0 is O(n)."],
                "arr = [4, 8, 15, 16]\narr[1] = 10",
                [_ref("Introduction to Arrays", "https://www.geeksforgeeks.org/array-data-structure/", "GeeksforGeeks")],
            ),
            _subtopic(
                "string-immutability",
                "String Immutability and Slicing",
                ["Unlike lists, strings cannot be edited in place (s[0] = 'x' raises TypeError). Instead, string methods and slices return new string values."],
                ["s[::-1] produces a reversed string cleanly."],
                "s = 'hello'\nrev = s[::-1]",
                [_ref("Python Text Sequence Operations", "https://docs.python.org/3/library/stdtypes.html#string-methods", "Python Docs")],
            ),
            _subtopic(
                "two-pointers",
                "Two-Pointer Scans on Sorted Arrays and Strings",
                ["By advancing left and right pointers from opposite ends (or slow and fast pointers in one direction), you can check palindromes, remove duplicates, or find sorted pair sums in O(n) time and O(1) extra space."],
                ["Start left = 0, right = len(arr) - 1 while left < right."],
                "def is_palindrome(s):\n    l, r = 0, len(s) - 1\n    while l < r:\n        if s[l] != s[r]: return False\n        l += 1; r -= 1\n    return True",
                [_ref("Two Pointers Technique", "https://www.geeksforgeeks.org/two-pointers-technique/", "GeeksforGeeks")],
            ),
        ],
    },

    "Linked Lists": {
        "summary": "Linked lists chain nodes dynamically where each node stores data and a next reference.",
        "key_points": [
            "Inserting at the head takes O(1) time by updating head references.",
            "The tail node points to None / NULL.",
            "Always advance current = current.next inside traversal loops to avoid infinite loops.",
        ],
        "example": "def list_sum(nodes):\n    return sum(node['val'] for node in nodes)",
        "references": [
            _ref("Linked List Data Structure", "https://www.geeksforgeeks.org/data-structures/linked-list/", "GeeksforGeeks"),
            _ref("VisuAlgo Linked List Visualizer", "https://visualgo.net/en/list", "VisuAlgo"),
        ],
        "subtopics": [
            _subtopic(
                "nodes-pointers",
                "Nodes, Data, and Next References",
                ["A linked list consists of independent node objects linked by references instead of contiguous array indices. Each node stores a value and a pointer/reference to the next node (or None at the end)."],
                ["Access by position k takes O(k) traversal starting from head."],
                "class Node:\n    def __init__(self, val, nxt=None):\n        self.val = val\n        self.next = nxt",
                [_ref("What is a Linked List?", "https://www.geeksforgeeks.org/data-structures/linked-list/", "GeeksforGeeks")],
            ),
            _subtopic(
                "list-traversal",
                "Traversing, Inserting, and Deleting Nodes",
                ["Traversing walks cur = cur.next until cur is None. Head insertion updates new_node.next = head in O(1) time. Deleting a node updates prev.next = target.next."],
                ["Handle empty lists (head is None) before accessing head.next."],
                "cur = head\nwhile cur is not None:\n    print(cur.val)\n    cur = cur.next",
                [_ref("Linked Lists Interactive", "https://visualgo.net/en/list", "VisuAlgo")],
            ),
        ],
    },

    "Stacks and Queues": {
        "summary": "Stacks enforce LIFO order (push/pop at top); queues enforce FIFO order (enqueue back, dequeue front).",
        "key_points": [
            "Balanced parentheses checking is the classic Stack use case.",
            "Breadth-First traversal relies on Queue ordering.",
            "Always check emptiness before calling pop/dequeue.",
        ],
        "example": "stack = []\nstack.append('a')\nstack.append('b')\nprint(stack.pop())  # 'b'",
        "references": [
            _ref("Python collections.deque", "https://docs.python.org/3/library/collections.html#collections.deque", "Python Docs"),
            _ref("Stack and Queue Tutorials", "https://www.geeksforgeeks.org/stack-data-structure/", "GeeksforGeeks"),
        ],
        "subtopics": [
            _subtopic(
                "stack-lifo",
                "Stacks (Last-In, First-Out) and Applications",
                ["A stack adds (push) and removes (pop) items only from one end called the top. Function call stacks, undo history, and balanced bracket validation rely on LIFO stacks."],
                ["Check len(stack) > 0 before popping.", "Python lists work well as O(1) stacks via append() and pop()."],
                "st = []\nst.append(10)\nst.append(20)\ntop = st.pop()  # 20",
                [_ref("Stack Data Structure", "https://www.geeksforgeeks.org/stack-data-structure/", "GeeksforGeeks")],
            ),
            _subtopic(
                "queue-fifo",
                "Queues (First-In, First-Out) and deque",
                ["A queue adds elements to the rear (enqueue) and removes from the front (dequeue) in arrival order. Use collections.deque in Python so popleft() runs in O(1) time."],
                ["Avoid list.pop(0) in tight loops since shifting takes O(n) time."],
                "from collections import deque\nq = deque(['first', 'second'])\nprint(q.popleft())  # 'first'",
                [_ref("Queue Data Structure & deque", "https://docs.python.org/3/library/collections.html#collections.deque", "Python Docs")],
            ),
        ],
    },

    "Recursion Basics": {
        "summary": "A recursive function solves a problem by reducing it toward one or more base cases.",
        "key_points": [
            "Every recursion needs a stopping base case to prevent stack overflow.",
            "Each recursive call must progress closer to the base case.",
            "Example: factorial(n) = n * factorial(n - 1) with base case n <= 1 -> 1.",
        ],
        "example": "def fact(n):\n    if n <= 1:\n        return 1\n    return n * fact(n - 1)",
        "references": [
            _ref("Thinking Recursively in Python", "https://realpython.com/python-thinking-recursively/", "Real Python"),
            _ref("Introduction to Recursion", "https://www.geeksforgeeks.org/introduction-to-recursion-2/", "GeeksforGeeks"),
        ],
        "subtopics": [
            _subtopic(
                "call-stack-base-case",
                "Call Stack and Base Cases",
                ["Recursion solves a problem by invoking the function on a smaller subproblem. Each active call is pushed onto the program's call stack until a base case returns directly without recursing further."],
                ["Missing or unreachable base cases trigger RecursionError / stack overflow."],
                "def countdown(n):\n    if n <= 0: return\n    print(n)\n    countdown(n - 1)",
                [_ref("Thinking Recursively in Python", "https://realpython.com/python-thinking-recursively/", "Real Python")],
            ),
            _subtopic(
                "recursive-patterns",
                "Writing Recursive Solutions (Factorial, Sum, Fibonacci)",
                ["Define two clear parts: (1) Base case for minimal input, and (2) Recursive step combining the current value with the smaller subproblem result."],
                ["Always return the recursive call's combined expression."],
                "def sum_n(n):\n    if n <= 0: return 0\n    return n + sum_n(n - 1)",
                [_ref("Introduction to Recursion", "https://www.geeksforgeeks.org/introduction-to-recursion-2/", "GeeksforGeeks")],
            ),
        ],
    },

    "Trees and Binary Search Trees": {
        "summary": "Trees model hierarchy from a root; BSTs keep smaller keys left and larger keys right.",
        "key_points": [
            "Inorder traversal (Left, Root, Right) of a BST yields sorted values.",
            "Leaf nodes have no children.",
            "Balanced BST search runs in O(log n) average time.",
        ],
        "example": "# BST invariant: left < node < right",
        "references": [
            _ref("Binary Search Tree Tutorial", "https://www.geeksforgeeks.org/binary-search-tree-data-structure/", "GeeksforGeeks"),
            _ref("VisuAlgo BST Visualizer", "https://visualgo.net/en/bst", "VisuAlgo"),
        ],
        "subtopics": [
            _subtopic(
                "tree-terminology",
                "Tree Terminology and Traversals",
                ["A tree starts at a root node branching into child nodes down to leaf nodes. Standard depth-first traversals visit nodes in Preorder (Root-Left-Right), Inorder (Left-Root-Right), or Postorder (Left-Right-Root)."],
                ["Leaf nodes have left == None and right == None."],
                "def inorder(node):\n    if not node: return []\n    return inorder(node.left) + [node.val] + inorder(node.right)",
                [_ref("Tree Traversals Explained", "https://www.geeksforgeeks.org/tree-traversals-inorder-preorder-and-postorder/", "GeeksforGeeks")],
            ),
            _subtopic(
                "bst-properties",
                "Binary Search Tree (BST) Search & Invariant",
                ["In a BST, every node's left subtree contains strictly smaller keys and its right subtree contains larger keys. Because half the remaining tree is discarded at each step on balanced trees, search runs in O(log n) average time."],
                ["Inorder traversal of any valid BST outputs values in sorted ascending order."],
                "# Check left subtree if target < node.val, else right subtree",
                [_ref("VisuAlgo BST Visualizer", "https://visualgo.net/en/bst", "VisuAlgo")],
            ),
        ],
    },

    "Graphs and Hashing": {
        "summary": "Graphs model vertices connected by edges; hash tables map keys to buckets in O(1) average time.",
        "key_points": [
            "Use a visited set during BFS/DFS to avoid infinite cycles.",
            "Adjacency lists store neighbors per vertex.",
            "Dictionaries/sets use hashing to count frequencies or detect duplicates in O(n).",
        ],
        "example": "counts = {}\nfor x in ['a', 'b', 'a']:\n    counts[x] = counts.get(x, 0) + 1",
        "references": [
            _ref("Graph Data Structure", "https://www.geeksforgeeks.org/graph-data-structure-and-algorithms/", "GeeksforGeeks"),
            _ref("VisuAlgo Graph Traversal", "https://visualgo.net/en/dfsbfs", "VisuAlgo"),
        ],
        "subtopics": [
            _subtopic(
                "graph-representation-bfs-dfs",
                "Adjacency Lists, BFS, and DFS",
                ["Graphs connect vertices with directed or undirected edges and are commonly stored as adjacency lists ({'A': ['B', 'C']}). BFS uses a Queue for level-order exploration; DFS uses recursion or a Stack."],
                ["Track visited vertices in a set so cycles don't cause infinite loops."],
                "from collections import deque\n# BFS visits neighbors level by level",
                [_ref("BFS and DFS Visualizer", "https://visualgo.net/en/dfsbfs", "VisuAlgo")],
            ),
            _subtopic(
                "hashing-maps",
                "Hash Tables and Frequency Dictionaries",
                ["Hash tables compute an array index from each key using a hash function, enabling O(1) average insert and lookup. Frequency maps and two-sum lookups rely directly on hashing."],
                ["Dictionaries and sets implement hash tables in Python."],
                "freq = {}\nfor word in ['go', 'py', 'go']:\n    freq[word] = freq.get(word, 0) + 1",
                [_ref("Hashing Data Structure", "https://www.geeksforgeeks.org/hashing-data-structure/", "GeeksforGeeks")],
            ),
        ],
    },

    "Java Syntax and Data Types": {
        "summary": "Java is statically typed; execution begins at public static void main(String[] args).",
        "key_points": [
            "Use final to declare constants; use int, double, boolean, char for primitives.",
            "Strings use double quotes \"...\" and are objects of class String.",
            "Every statement ends with a semicolon (;).",
        ],
        "example": "final int MAX = 100;\nString lang = \"Java\";",
        "references": [
            _ref("Oracle Java Language Basics", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/index.html", "Oracle Java Docs"),
            _ref("W3Schools Java Intro", "https://www.w3schools.com/java/", "W3Schools"),
        ],
        "subtopics": [
            _subtopic(
                "java-program-structure",
                "Java Program Structure and Variables",
                ["Every Java file wraps code inside a class and starts running from public static void main(String[] args). Variables must declare their type before use (for example int count = 5;)."],
                ["End every Java statement with a semicolon (;).", "Use final to declare unchangeable constants."],
                "public class Hello {\n    public static void main(String[] args) {\n        System.out.println(\"Hello, Java\");\n    }\n}",
                [_ref("Getting Started with Java", "https://dev.java/learn/getting-started/", "dev.java")],
            ),
            _subtopic(
                "java-primitives-strings",
                "Primitive Types, Type Division, and Strings",
                ["Java primitives include int, double, char (single quotes), and boolean. Dividing two integers like 7 / 2 truncates the decimal to 3 unless at least one operand is double (7.0 / 2 -> 3.5)."],
                ["String uses double quotes and is a reference class, not a primitive."],
                "int a = 7;\ndouble exact = a / 2.0;  // 3.5\nchar grade = 'A';",
                [_ref("Java Primitive Data Types", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/datatypes.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Operators and Control Statements": {
        "summary": "Control execution using ==, &&, ||, if-else, switch, and ternary (cond ? a : b).",
        "key_points": [
            "Use && for logical AND and || for logical OR.",
            "Compare String content with .equals(), not ==.",
            "Use break inside switch cases to prevent unwanted fall-through.",
        ],
        "example": "int res = (a > b) ? a : b;",
        "references": [
            _ref("Java Operators & Control Flow", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/flow.html", "Oracle Java Docs"),
            _ref("dev.java Control Flow", "https://dev.java/learn/language-basics/controlling-flow/", "dev.java"),
        ],
        "subtopics": [
            _subtopic(
                "java-operators-strings",
                "Logical Operators and String Comparison",
                ["Combine conditions using && (AND), || (OR), and ! (NOT). Compare primitive numbers with ==, but compare Java String contents using str1.equals(str2) so value comparison works reliably."],
                ["Avoid == on Strings because it checks object reference rather than text."],
                "if (role.equals(\"admin\") && active) {\n    System.out.println(\"Access\");\n}",
                [_ref("Java Equality and Relational Operators", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/op2.html", "Oracle Java Docs")],
            ),
            _subtopic(
                "if-switch-ternary",
                "if-else, switch Statements, and Ternary",
                ["Java supports if / else if / else blocks, switch statements (remember break after each case to avoid fall-through), and concise ternary expressions (cond ? a : b)."],
                ["Include a default branch in switch statements for unmatched values."],
                "int max = (a > b) ? a : b;",
                [_ref("The switch Statement", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/switch.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Loops and Arrays": {
        "summary": "Iterate fixed arrays with length-guarded for loops (i < arr.length) or enhanced for-each loops.",
        "key_points": [
            "Array indices go from 0 to arr.length - 1; <= causes ArrayIndexOutOfBoundsException.",
            "Use arr.length (property, not method) on Java arrays.",
            "do-while guarantees the body runs at least once.",
        ],
        "example": "int sum = 0;\nfor (int x : arr) { sum += x; }",
        "references": [
            _ref("Java Arrays Documentation", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/arrays.html", "Oracle Java Docs"),
            _ref("W3Schools Java Arrays", "https://www.w3schools.com/java/java_arrays.asp", "W3Schools"),
        ],
        "subtopics": [
            _subtopic(
                "java-loops",
                "for, while, and do-while Loops",
                ["for loops combine initialization, condition, and update on one line. while tests before each run, whereas do-while runs the body once before checking the loop condition."],
                ["Standard loop pattern: for (int i = 0; i < arr.length; i++)"],
                "for (int i = 0; i < 5; i++) {\n    System.out.println(i);\n}",
                [_ref("Java for and while Statements", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/for.html", "Oracle Java Docs")],
            ),
            _subtopic(
                "java-arrays",
                "Fixed Arrays and Enhanced for-each",
                ["Arrays store fixed-size typed elements: int[] nums = {10, 20, 30}. Use nums.length (no parentheses) for size, and avoid i <= nums.length which throws ArrayIndexOutOfBoundsException."],
                ["Use for (int x : nums) when index position isn't needed."],
                "int sum = 0;\nfor (int x : nums) { sum += x; }",
                [_ref("Java Arrays Tutorial", "https://docs.oracle.com/javase/tutorial/java/nutsandbolts/arrays.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Methods and Classes": {
        "summary": "Java classes encapsulate fields and methods; use new to create instances.",
        "key_points": [
            "Specify explicit return type on methods (or void if no value returns).",
            "private limits field access strictly to the declaring class.",
            "Method overloading reuses a method name with different parameter types/counts.",
        ],
        "example": "public static int add(int a, int b) { return a + b; }",
        "references": [
            _ref("Oracle Java Classes and Objects", "https://docs.oracle.com/javase/tutorial/java/javaOO/index.html", "Oracle Java Docs"),
            _ref("dev.java Classes & Objects", "https://dev.java/learn/classes-objects/", "dev.java"),
        ],
        "subtopics": [
            _subtopic(
                "java-methods",
                "Java Methods and Method Overloading",
                ["Methods declare an explicit return type (such as int, String, or void if nothing is returned). Method overloading lets multiple methods share one name as long as parameter lists differ."],
                ["static methods belong to the class and can be called without creating an instance."],
                "static int area(int w, int h) {\n    return w * h;\n}",
                [_ref("Defining Methods in Java", "https://docs.oracle.com/javase/tutorial/java/javaOO/methods.html", "Oracle Java Docs")],
            ),
            _subtopic(
                "java-classes-constructors",
                "Classes, Constructors, and Encapsulation",
                ["Classes hold instance fields and constructors matching the class name. Mark fields private and expose controlled getter/setter methods to keep internal state valid."],
                ["Allocate objects with new ClassName(...)."],
                "class Box {\n    private int size;\n    Box(int s) { this.size = s; }\n    int getSize() { return size; }\n}",
                [_ref("Providing Constructors for Your Classes", "https://docs.oracle.com/javase/tutorial/java/javaOO/constructors.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Inheritance and Polymorphism": {
        "summary": "Subclasses extend a parent class (extends), override behavior (@Override), and reuse parent constructors via super().",
        "key_points": [
            "Java supports single class inheritance (extends one superclass).",
            "Polymorphism dispatches overridden methods based on runtime object type.",
            "Use @Override when redefining a superclass method.",
        ],
        "example": "class Dog extends Animal {\n    @Override String speak() { return \"Woof\"; }\n}",
        "references": [
            _ref("Oracle Java Inheritance", "https://docs.oracle.com/javase/tutorial/java/IandI/subclasses.html", "Oracle Java Docs"),
            _ref("dev.java Inheritance", "https://dev.java/learn/inheritance/", "dev.java"),
        ],
        "subtopics": [
            _subtopic(
                "extends-super",
                "Single Inheritance with extends and super()",
                ["Subclasses inherit fields and methods from a superclass using extends. Java classes inherit from one parent class at a time and invoke the parent constructor with super(...)."],
                ["Call super(...) as the first statement inside a subclass constructor."],
                "class Student extends Person {\n    Student(String name) { super(name); }\n}",
                [_ref("Using the Keyword super", "https://docs.oracle.com/javase/tutorial/java/IandI/super.html", "Oracle Java Docs")],
            ),
            _subtopic(
                "overriding-polymorphism",
                "Method Overriding and Polymorphism",
                ["A subclass can replace a parent method implementation using the @Override annotation. At runtime, Java invokes the method matching the actual object instance even when referenced via a superclass variable."],
                ["@Override catches typos in method signatures at compile time."],
                "Animal pet = new Dog();\nSystem.out.println(pet.speak()); // Woof",
                [_ref("Polymorphism in Java", "https://docs.oracle.com/javase/tutorial/java/IandI/polymorphism.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Collections and Exception Handling": {
        "summary": "Manage dynamic collections (ArrayList, HashMap) and handle runtime failures with try-catch-finally.",
        "key_points": [
            "ArrayList preserves insertion order; HashMap maps unique keys to values.",
            "finally always runs after try/catch blocks.",
            "Use throw to trigger an exception explicitly.",
        ],
        "example": "try {\n    int x = 10 / n;\n} catch (ArithmeticException e) {\n    System.out.println(0);\n}",
        "references": [
            _ref("Oracle Java Collections", "https://docs.oracle.com/javase/tutorial/collections/index.html", "Oracle Java Docs"),
            _ref("Java Exceptions Lesson", "https://docs.oracle.com/javase/tutorial/essential/exceptions/", "Oracle Java Docs"),
        ],
        "subtopics": [
            _subtopic(
                "arraylist-hashmap",
                "ArrayList and HashMap Basics",
                ["Unlike fixed arrays, ArrayList grows dynamically using .add(), .get(i), and .size(). HashMap<K, V> maps unique keys to values using .put(), .get(), and .getOrDefault()."],
                ["Check map.getOrDefault(key, 0) to safely handle missing keys."],
                "HashMap<String, Integer> scores = new HashMap<>();\nscores.put(\"ada\", 95);",
                [_ref("The Collections Framework", "https://docs.oracle.com/javase/tutorial/collections/intro/index.html", "Oracle Java Docs")],
            ),
            _subtopic(
                "try-catch-finally",
                "Handling Errors with try, catch, and finally",
                ["Wrap risky operations (like parsing strings or dividing) inside a try block, catch specific exceptions cleanly in catch blocks, and put cleanup logic inside finally (which always executes)."],
                ["Catch specific exceptions such as NumberFormatException cleanly."],
                "try { return Integer.parseInt(s); }\ncatch (NumberFormatException e) { return -1; }",
                [_ref("Catching and Handling Exceptions", "https://docs.oracle.com/javase/tutorial/essential/exceptions/handling.html", "Oracle Java Docs")],
            ),
        ],
    },

    "Python Fundamentals": {
        "summary": "Data science Python leans on clean list comprehensions, lambdas, and dictionary aggregations.",
        "key_points": [
            "List comprehensions transform and filter sequences concisely.",
            "lambda defines quick inline single-expression functions.",
            "Zip pairs corresponding feature lists cleanly.",
        ],
        "example": "squares = [x * x for x in [1, 2, 3]]",
        "references": [
            _ref("List Comprehensions in Python", "https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions", "Python Docs"),
            _ref("Built-in Functions (zip, filter, map)", "https://docs.python.org/3/library/functions.html", "Python Docs"),
        ],
        "subtopics": [
            _subtopic(
                "list-comprehensions",
                "List Comprehensions and Inline Filtering",
                ["List comprehensions build transformed or filtered lists in one clear expression: [expr for x in items if condition]. They replace verbose loop-and-append boilerplate in data pipelines."],
                ["Filter conditions appear after the for clause."],
                "positives = [x * 2 for x in [-1, 3, 5] if x > 0]  # [6, 10]",
                [_ref("List Comprehensions", "https://docs.python.org/3/tutorial/datastructures.html#list-comprehensions", "Python Docs")],
            ),
            _subtopic(
                "lambda-zip-dicts",
                "Lambda Functions and Pairing with zip()",
                ["lambda creates small anonymous functions (e.g. for sort keys). zip(a, b) pairs elements position-by-position across two lists, ideal for combining feature columns or predictions."],
                ["zip() stops at the shortest iterable cleanly."],
                "totals = [a + b for a, b in zip([1, 2], [3, 4])]  # [4, 6]",
                [_ref("Python zip() Function", "https://docs.python.org/3/library/functions.html#zip", "Python Docs")],
            ),
        ],
    },

    "Mathematics and Statistics Basics": {
        "summary": "Mean, variance, dot products, and gradient steps form the math backbone of ML models.",
        "key_points": [
            "Mean is sum / N; Median is robust to outliers.",
            "Standard deviation measures dispersion around the mean.",
            "Gradient Descent updates weights in the opposite direction of loss gradient.",
        ],
        "example": "mean = sum(vals) / len(vals)",
        "references": [
            _ref("Python statistics Module", "https://docs.python.org/3/library/statistics.html", "Python Docs"),
            _ref("Khan Academy Statistics & Probability", "https://www.khanacademy.org/math/statistics-probability", "Khan Academy"),
        ],
        "subtopics": [
            _subtopic(
                "central-tendency-spread",
                "Mean, Median, Variance, and Standard Deviation",
                ["Mean averages all values (sum / n), whereas median takes the middle sorted value and resists extreme outliers. Variance averages squared deviations from the mean, and standard deviation is its square root."],
                ["Use median when extreme values could distort the average."],
                "vals = [10, 20, 30]\nmean = sum(vals) / len(vals)\nvar = sum((x - mean) ** 2 for x in vals) / len(vals)",
                [_ref("Descriptive Statistics Guide", "https://www.khanacademy.org/math/statistics-probability/summarizing-quantitative-data", "Khan Academy")],
            ),
            _subtopic(
                "vectors-gradients",
                "Dot Products and Gradient Descent Intuition",
                ["The dot product multiplies aligned weights and features and sums them (sum(w_i * x_i)). Training algorithms iteratively adjust weights downward along the loss gradient: w_new = w - lr * grad."],
                ["Learning rate (lr) controls how large each weight update step is."],
                "dot = sum(w * x for w, x in zip(weights, features))\nw_new = w - lr * grad",
                [_ref("Gradient Descent Overview", "https://developers.google.com/machine-learning/crash-course/linear-regression/gradient-descent", "Google ML Crash Course")],
            ),
        ],
    },

    "NumPy and Pandas": {
        "summary": "Vectorized numerical operations (ndarray) and tabular manipulation (DataFrame) power ML pipelines.",
        "key_points": [
            "Broadcasting applies element-wise math across compatible shapes without explicit loops.",
            "Use dropna() to drop missing rows and fillna() to impute them.",
            "Slicing & boolean masking extract clean subsets quickly.",
        ],
        "example": "# Element-wise vector scaling: [x * 2 for x in vec]",
        "references": [
            _ref("NumPy Quickstart Tutorial", "https://numpy.org/doc/stable/user/quickstart.html", "NumPy Docs"),
            _ref("10 Minutes to pandas", "https://pandas.pydata.org/docs/user_guide/10min.html", "pandas Docs"),
        ],
        "subtopics": [
            _subtopic(
                "numpy-arrays-broadcasting",
                "NumPy Arrays, Shapes, and Broadcasting",
                ["NumPy's ndarray stores typed numeric grids in contiguous memory for fast vectorized math. Operations like arr * 2 + 1 broadcast cleanly across every element without slow Python for loops."],
                ["Check shape with arr.shape before matrix multiplication."],
                "# arr = np.array([1, 2, 3]); arr * 10 -> [10, 20, 30]",
                [_ref("NumPy Quickstart", "https://numpy.org/doc/stable/user/quickstart.html", "NumPy Docs")],
            ),
            _subtopic(
                "pandas-dataframes",
                "Pandas Series, DataFrames, and Missing Values",
                ["A Pandas DataFrame organizes CSV/SQL datasets into labeled columns and rows. Inspect missing values with isna(), remove gaps with dropna(), or impute missing values using fillna()."],
                ["Select columns by df['col'] and filter rows via boolean masks df[df['age'] > 18]."],
                "# df['score'] = df['score'].fillna(df['score'].mean())",
                [_ref("10 Minutes to pandas", "https://pandas.pydata.org/docs/user_guide/10min.html", "pandas Docs")],
            ),
        ],
    },

    "Data Preprocessing and Visualization": {
        "summary": "Clean, scale, and encode raw features before feeding them into distance or gradient-based models.",
        "key_points": [
            "Min-Max scaling rescales values to [0, 1] via (x - min) / (max - min).",
            "One-Hot Encoding prevents models from assuming fake ordinal ranking on categories.",
            "Handle missing values (None/NaN) consistently across train and test splits.",
        ],
        "example": "scaled = (x - mn) / (mx - mn)",
        "references": [
            _ref("Scikit-Learn Preprocessing Guide", "https://scikit-learn.org/stable/modules/preprocessing.html", "scikit-learn Docs"),
            _ref("Matplotlib Tutorials", "https://matplotlib.org/stable/tutorials/index.html", "Matplotlib Docs"),
        ],
        "subtopics": [
            _subtopic(
                "scaling-encoding",
                "Feature Scaling and One-Hot Encoding",
                ["Min-Max scaling compresses values into [0, 1], while standardization (z-score) centers data at mean 0 with unit variance. Categorical text fields are converted to binary indicator columns using One-Hot Encoding."],
                ["Fit scalers ONLY on training data, then transform validation/test splits to prevent data leakage."],
                "mn, mx = min(vals), max(vals)\nscaled = [(x - mn) / (mx - mn) for x in vals]",
                [_ref("Preprocessing Data (scikit-learn)", "https://scikit-learn.org/stable/modules/preprocessing.html", "scikit-learn Docs")],
            ),
            _subtopic(
                "splitting-visualization",
                "Train-Test Splitting and Data Visualization",
                ["Hold out an unseen test split before fitting models or scalers so accuracy reflects real-world generalization. Histograms, scatter plots, and box plots reveal skew, outliers, and class balance."],
                ["Keep test split completely separate during model tuning."],
                "# Scatter plots check relationships; histograms check distribution",
                [_ref("Cross-validation & Data Splitting", "https://scikit-learn.org/stable/modules/cross_validation.html", "scikit-learn Docs")],
            ),
        ],
    },

    "Machine Learning Algorithms": {
        "summary": "Understand how supervised learners (regression, trees, classifiers) and unsupervised learners (K-Means) operate.",
        "key_points": [
            "Supervised learning maps features X to labeled targets y.",
            "Decision trees split data recursively; Random Forest averages many trees.",
            "K-Means clusters unlabeled points by distance to nearest centroid.",
        ],
        "example": "preds = [1 if p >= 0.5 else 0 for p in probs]",
        "references": [
            _ref("Scikit-Learn User Guide", "https://scikit-learn.org/stable/user_guide.html", "scikit-learn Docs"),
            _ref("Google Machine Learning Crash Course", "https://developers.google.com/machine-learning/crash-course", "Google ML"),
        ],
        "subtopics": [
            _subtopic(
                "supervised-regression-trees",
                "Supervised Learning: Linear Models & Decision Trees",
                ["Supervised algorithms learn from input features X paired with known labels y. Linear/Logistic models learn weighted sums; Decision Trees split features hierarchically, and Random Forests combine many trees to reduce variance."],
                ["Regression predicts continuous quantities; classification predicts discrete categories."],
                "preds = [1 if prob >= 0.5 else 0 for prob in probabilities]",
                [_ref("Supervised Learning in scikit-learn", "https://scikit-learn.org/stable/supervised_learning.html", "scikit-learn Docs")],
            ),
            _subtopic(
                "unsupervised-clustering",
                "Unsupervised Learning: K-Means Clustering",
                ["Unsupervised models find structure in unlabeled data. K-Means assigns each point to its nearest centroid, then updates each centroid to the mean of its assigned points until stable."],
                ["Feature scaling matters before distance-based clustering or KNN."],
                "# Nearest 1D centroid check: 'C1' if abs(x - c1) < abs(x - c2) else 'C2'",
                [_ref("Clustering in scikit-learn", "https://scikit-learn.org/stable/modules/clustering.html", "scikit-learn Docs")],
            ),
        ],
    },

    "Model Evaluation and AI Projects": {
        "summary": "Measure generalization fairly using precision, recall, F1, confusion counts, and validation splits.",
        "key_points": [
            "Overfitting occurs when training score is high but unseen test score drops.",
            "Precision = TP / (TP + FP); Recall = TP / (TP + FN).",
            "Always keep test data unseen during fitting and scaling.",
        ],
        "example": "precision = tp / (tp + fp)",
        "references": [
            _ref("Classification Metrics Guide", "https://scikit-learn.org/stable/modules/model_evaluation.html", "scikit-learn Docs"),
            _ref("Classification & Confusion Matrix", "https://developers.google.com/machine-learning/crash-course/classification", "Google ML"),
        ],
        "subtopics": [
            _subtopic(
                "confusion-precision-recall",
                "Confusion Matrix, Precision, Recall, and F1",
                ["A confusion matrix counts True Positives (TP), False Positives (FP), False Negatives (FN), and True Negatives (TN). Precision measures how accurate positive predictions are; Recall measures how many true positives were caught."],
                ["Use Precision/Recall/F1 alongside Accuracy whenever classes are imbalanced."],
                "precision = tp / (tp + fp) if (tp + fp) else 0.0\nrecall = tp / (tp + fn) if (tp + fn) else 0.0",
                [_ref("Metrics and Scoring (scikit-learn)", "https://scikit-learn.org/stable/modules/model_evaluation.html", "scikit-learn Docs")],
            ),
            _subtopic(
                "overfitting-workflow",
                "Overfitting vs Underfitting and Project Workflow",
                ["Overfitting happens when a complex model memorizes training noise and fails on unseen test inputs. Mitigate overfitting with simpler models, cross-validation, more data, or regularization."],
                ["Verify train vs test gap rather than looking only at training accuracy."],
                "# Always evaluate final model once on held-out test data",
                [_ref("Generalization & Overfitting", "https://developers.google.com/machine-learning/crash-course/generalization", "Google ML")],
            ),
        ],
    },
}


def get_topic_study_notes(topic_name):
    if topic_name in TOPIC_STUDY_NOTES:
        return TOPIC_STUDY_NOTES[topic_name]
    return {
        "summary": f"Core principles and study guide for {topic_name}.",
        "key_points": [
            "Review core definitions and key syntax.",
            "Study worked code examples carefully.",
        ],
        "example": "",
        "references": [
            _ref("Python Official Documentation", "https://docs.python.org/3/", "Python Docs", "Official language reference."),
        ],
        "subtopics": [
            _subtopic(
                "overview",
                f"Introduction to {topic_name}",
                [f"Core concepts, rules, and worked examples for {topic_name}."],
                ["Understand the fundamental syntax and rules first.", "Work through examples line by line."],
                "",
                [_ref("Python Official Documentation", "https://docs.python.org/3/", "Python Docs")],
            ),
        ],
    }


# -------------------------------------------------------------------------
# Question Constructors (Keeps bank crisp, uniform & inspectable)
# -------------------------------------------------------------------------

def _mcq(course, topic, num, text, a, b, c, d, ans, pool="initial", explanation="", weak_concept="", recommended_subtopic=""):
    spec = {"explanation": explanation}
    if weak_concept:
        spec["weak_concept"] = weak_concept
    if recommended_subtopic:
        spec["recommended_subtopic"] = recommended_subtopic
    return (
        course, topic, num, text, a, b, c, d, ans,
        "mcq", "", json.dumps(spec), pool
    )


def _pred(course, topic, num, text, snippet, expected, variants=None, pool="initial", explanation="", case_sensitive=True, weak_concept="", recommended_subtopic=""):
    spec = {
        "expected_output": expected,
        "accept_variants": variants or [],
        "normalize_whitespace": True,
        "case_sensitive": case_sensitive,
        "explanation": explanation,
    }
    if weak_concept:
        spec["weak_concept"] = weak_concept
    if recommended_subtopic:
        spec["recommended_subtopic"] = recommended_subtopic
    return (
        course, topic, num, text, "", "", "", "", expected,
        "predict_output", snippet, json.dumps(spec), pool
    )


def _fill(course, topic, num, text, snippet, expected, variants=None, pool="mastery_1", explanation="", case_sensitive=True, weak_concept="", recommended_subtopic=""):
    spec = {
        "expected_blank": expected,
        "accept_variants": variants or [],
        "case_sensitive": case_sensitive,
        "explanation": explanation,
    }
    if weak_concept:
        spec["weak_concept"] = weak_concept
    if recommended_subtopic:
        spec["recommended_subtopic"] = recommended_subtopic
    return (
        course, topic, num, text, "", "", "", "", expected,
        "fill_blank", snippet, json.dumps(spec), pool
    )


def _py_code(course, topic, num, qtype, text, starter, solution, test_cases, pool="initial", explanation="", require_recursion=False, weak_concept="", recommended_subtopic=""):
    spec = {
        "language": "python",
        "test_cases": test_cases,
        "require_recursion": require_recursion,
        "explanation": explanation,
    }
    if weak_concept:
        spec["weak_concept"] = weak_concept
    if recommended_subtopic:
        spec["recommended_subtopic"] = recommended_subtopic
    return (
        course, topic, num, text, "", "", "", "", solution,
        qtype, starter, json.dumps(spec), pool
    )


def _java_code(course, topic, num, qtype, text, starter, solution, spec_extra, pool="initial", explanation="", weak_concept="", recommended_subtopic=""):
    spec = {"language": "java", "explanation": explanation}
    spec.update(spec_extra)
    if weak_concept:
        spec["weak_concept"] = weak_concept
    if recommended_subtopic:
        spec["recommended_subtopic"] = recommended_subtopic
    return (
        course, topic, num, text, "", "", "", "", solution,
        qtype, starter, json.dumps(spec), pool
    )


def build_curriculum_question_bank(courses_map, topic_map):
    """
    Constructs all 100 initial questions (4 mixed types per topic across all 4 courses)
    plus 75 topic-level practice questions (3 per topic).
    """
    rows = []

    def add_course(course_name, specs_fn):
        cid = courses_map.get(course_name)
        if not cid:
            return
        def tid(tname):
            return topic_map.get((cid, tname))
        rows.extend(specs_fn(cid, tid))

    # =====================================================================
    # 1. PYTHON PROGRAMMING (6 Topics x 4 Initial = 24, + 18 Practice = 42)
    # =====================================================================
    def python_bank(cid, tid):
        t1 = tid("Variables and Data Types")
        t2 = tid("Operators and Conditions")
        t3 = tid("Loops")
        t4 = tid("Functions")
        t5 = tid("Lists, Tuples, Sets, and Dictionaries")
        t6 = tid("Object-Oriented Programming")
        qs = [
            # Topic 1: Variables and Data Types (Q1..Q4)
            _mcq(cid, t1, 1, "Which Python data type stores whole integers without decimals?", "float", "str", "int", "bool", "C"),
            _pred(cid, t1, 2, "What will this Python code output?", "x = '4'\ny = 2\nprint(x * y)", "44", explanation="String repetition `'4' * 2` gives `'44'`."),
            _py_code(cid, t1, 3, "debug_code", "Fix the type error so `add_bonus(score_str, bonus)` returns the numeric sum.",
                     "def add_bonus(score_str, bonus):\n    return score_str + bonus",
                     "def add_bonus(score_str, bonus):\n    return int(score_str) + bonus",
                     [{"call": "add_bonus('40', 5)", "expected_return": 45},
                      {"call": "add_bonus('10', 0)", "expected_return": 10},
                      {"call": "add_bonus('75', 25)", "expected_return": 100}]),
            _py_code(cid, t1, 4, "write_code", "Write `format_price(amount, tax)` returning float `amount + tax` rounded to 2 decimal places.",
                     "def format_price(amount, tax):\n    pass",
                     "def format_price(amount, tax):\n    return round(float(amount) + float(tax), 2)",
                     [{"call": "format_price(10.5, 1.25)", "expected_return": 11.75},
                      {"call": "format_price(5, 0.3333)", "expected_return": 5.33},
                      {"call": "format_price(0, 2.5)", "expected_return": 2.5}]),

            # Topic 2: Operators and Conditions (Q5..Q8)
            _mcq(cid, t2, 5, "What is the result of `14 // 4` in Python?", "3.5", "3", "2", "4", "B"),
            _pred(cid, t2, 6, "What will this conditional snippet output?", "a = 7\nb = 3\nif a % b == 1 and a > 5:\n    print('Valid')\nelse:\n    print('Invalid')", "Valid"),
            _py_code(cid, t2, 7, "debug_code", "Fix the condition bug in `is_even(n)` so it returns `True` for even numbers and `False` otherwise.",
                     "def is_even(n):\n    if n % 2 = 0:\n        return True\n    return False",
                     "def is_even(n):\n    if n % 2 == 0:\n        return True\n    return False",
                     [{"call": "is_even(4)", "expected_return": True},
                      {"call": "is_even(7)", "expected_return": False},
                      {"call": "is_even(0)", "expected_return": True}]),
            _py_code(cid, t2, 8, "write_code", "Write `grade_label(score)` returning `'Pass'` if `score >= 50` else `'Retry'`.",
                     "def grade_label(score):\n    pass",
                     "def grade_label(score):\n    return 'Pass' if score >= 50 else 'Retry'",
                     [{"call": "grade_label(85)", "expected_return": "Pass"},
                      {"call": "grade_label(50)", "expected_return": "Pass"},
                      {"call": "grade_label(49)", "expected_return": "Retry"}]),

            # Topic 3: Loops (Q9..Q12)
            _mcq(cid, t3, 9, "Which statement skips the rest of the current loop iteration and jumps to the next?", "break", "pass", "continue", "stop", "C"),
            _pred(cid, t3, 10, "What will this loop print?", "total = 0\nfor i in range(1, 5):\n    if i % 2 == 0:\n        total += i\nprint(total)", "6"),
            _py_code(cid, t3, 11, "debug_code", "Fix the off-by-one bug in `sum_to_n(n)` so it returns the sum `1 + 2 + ... + n`.",
                     "def sum_to_n(n):\n    total = 0\n    for i in range(1, n):\n        total += i\n    return total",
                     "def sum_to_n(n):\n    total = 0\n    for i in range(1, n + 1):\n        total += i\n    return total",
                     [{"call": "sum_to_n(4)", "expected_return": 10},
                      {"call": "sum_to_n(1)", "expected_return": 1},
                      {"call": "sum_to_n(5)", "expected_return": 15}]),
            _py_code(cid, t3, 12, "write_code", "Write `count_positives(nums)` returning the count of integers greater than 0 in `nums`.",
                     "def count_positives(nums):\n    pass",
                     "def count_positives(nums):\n    return sum(1 for x in nums if x > 0)",
                     [{"call": "count_positives([-2, 0, 3, 5])", "expected_return": 2},
                      {"call": "count_positives([-1, -5])", "expected_return": 0},
                      {"call": "count_positives([10, 20, 30])", "expected_return": 3}]),

            # Topic 4: Functions (Q13..Q16)
            _mcq(cid, t4, 13, "What does a Python function return if execution reaches the end with no `return` statement?", "0", "False", "None", "Empty string", "C"),
            _pred(cid, t4, 14, "What will this code print?", "def step(x, inc=2):\n    return x + inc\nprint(step(5) + step(3, 1))", "11"),
            _py_code(cid, t4, 15, "debug_code", "Fix `multiply(a, b)` so it returns the product instead of returning `None`.",
                     "def multiply(a, b):\n    res = a * b\n    print(res)",
                     "def multiply(a, b):\n    return a * b",
                     [{"call": "multiply(3, 4)", "expected_return": 12},
                      {"call": "multiply(5, 0)", "expected_return": 0},
                      {"call": "multiply(-2, 6)", "expected_return": -12}]),
            _py_code(cid, t4, 16, "write_code", "Write `clamp(val, low, high)` returning `low` if `val < low`, `high` if `val > high`, else `val`.",
                     "def clamp(val, low, high):\n    pass",
                     "def clamp(val, low, high):\n    return max(low, min(high, val))",
                     [{"call": "clamp(5, 1, 10)", "expected_return": 5},
                      {"call": "clamp(-3, 0, 10)", "expected_return": 0},
                      {"call": "clamp(15, 0, 10)", "expected_return": 10}]),

            # Topic 5: Lists, Tuples, Sets, and Dictionaries (Q17..Q20)
            _mcq(cid, t5, 17, "Which collection type guarantees unique elements and supports set intersection?", "list", "tuple", "set", "dict keys list", "C"),
            _pred(cid, t5, 18, "What will this code output?", "data = [10, 20, 30, 40]\nprint(data[1:3])", "[20, 30]", ["[20,30]"]),
            _py_code(cid, t5, 19, "debug_code", "Fix `get_score(scores, name)` so it safely returns `0` when `name` is missing instead of raising KeyError.",
                     "def get_score(scores, name):\n    return scores[name]",
                     "def get_score(scores, name):\n    return scores.get(name, 0)",
                     [{"call": "get_score({'amy': 90}, 'amy')", "expected_return": 90},
                      {"call": "get_score({'amy': 90}, 'bob')", "expected_return": 0},
                      {"call": "get_score({}, 'x')", "expected_return": 0}]),
            _py_code(cid, t5, 20, "write_code", "Write `unique_sorted(nums)` returning a sorted list of unique numbers from `nums`.",
                     "def unique_sorted(nums):\n    pass",
                     "def unique_sorted(nums):\n    return sorted(set(nums))",
                     [{"call": "unique_sorted([3, 1, 2, 3, 1])", "expected_return": [1, 2, 3]},
                      {"call": "unique_sorted([])", "expected_return": []},
                      {"call": "unique_sorted([5, 5, 2])", "expected_return": [2, 5]}]),

            # Topic 6: Object-Oriented Programming (Q21..Q24)
            _mcq(cid, t6, 21, "What is the role of `self` in an instance method inside a Python class?", "Refers to the module", "Refers to the current object instance", "Creates a static field", "Deletes the instance", "B"),
            _pred(cid, t6, 22, "What will this class code print?", "class Box:\n    def __init__(self, v):\n        self.v = v * 2\nb = Box(6)\nprint(b.v)", "12"),
            _py_code(cid, t6, 23, "debug_code", "Fix the missing `self` assignment in `Student.__init__` so `s.name` stores the name.",
                     "class Student:\n    def __init__(self, name):\n        name = name\n    def greet(self):\n        return 'Hi ' + self.name",
                     "class Student:\n    def __init__(self, name):\n        self.name = name\n    def greet(self):\n        return 'Hi ' + self.name",
                     [{"call": "Student('Maya').greet()", "expected_return": "Hi Maya"},
                      {"call": "Student('Leo').name", "expected_return": "Leo"}]),
            _py_code(cid, t6, 24, "write_code", "Write class `Rectangle` with `__init__(self, w, h)` and method `area(self)` returning `w * h`.",
                     "class Rectangle:\n    def __init__(self, w, h):\n        pass\n    def area(self):\n        pass",
                     "class Rectangle:\n    def __init__(self, w, h):\n        self.w = w\n        self.h = h\n    def area(self):\n        return self.w * self.h",
                     [{"call": "Rectangle(4, 5).area()", "expected_return": 20},
                      {"call": "Rectangle(3, 7).area()", "expected_return": 21},
                      {"call": "Rectangle(0, 9).area()", "expected_return": 0}]),
        ]

        # Practice Pool for Python Programming (3 per topic = 18)
        qs.extend([
            # T1 Practice
            _pred(cid, t1, 101, "What output does this produce?", "a = 5\nb = float(a)\nprint(type(b).__name__)", "float", pool="practice"),
            _py_code(cid, t1, 102, "debug_code", "Fix `to_percent(val)` so it converts string `'0.45'` cleanly to float `45.0`.",
                     "def to_percent(val):\n    return val * 100",
                     "def to_percent(val):\n    return float(val) * 100",
                     [{"call": "to_percent('0.45')", "expected_return": 45.0}, {"call": "to_percent('0.5')", "expected_return": 50.0}], pool="practice"),
            _py_code(cid, t1, 103, "write_code", "Write `full_name(first, last)` joining them with one space and capitalized title case.",
                     "def full_name(first, last):\n    pass",
                     "def full_name(first, last):\n    return f'{first} {last}'.title()",
                     [{"call": "full_name('ada', 'lovelace')", "expected_return": "Ada Lovelace"}, {"call": "full_name('alan', 'turing')", "expected_return": "Alan Turing"}], pool="practice"),

            # T2 Practice
            _pred(cid, t2, 104, "What will this print?", "x = 12\nprint(x > 10 and x % 3 == 0)", "True", pool="practice"),
            _py_code(cid, t2, 105, "debug_code", "Fix `abs_diff(a, b)` so it always returns a non-negative difference.",
                     "def abs_diff(a, b):\n    if a < b:\n        return a - b\n    return a - b",
                     "def abs_diff(a, b):\n    if a < b:\n        return b - a\n    return a - b",
                     [{"call": "abs_diff(3, 10)", "expected_return": 7}, {"call": "abs_diff(10, 3)", "expected_return": 7}], pool="practice"),
            _py_code(cid, t2, 106, "write_code", "Write `sign_tag(n)` returning `'positive'`, `'negative'`, or `'zero'`.",
                     "def sign_tag(n):\n    pass",
                     "def sign_tag(n):\n    if n > 0: return 'positive'\n    if n < 0: return 'negative'\n    return 'zero'",
                     [{"call": "sign_tag(4)", "expected_return": "positive"}, {"call": "sign_tag(-2)", "expected_return": "negative"}, {"call": "sign_tag(0)", "expected_return": "zero"}], pool="practice"),

            # T3 Practice
            _pred(cid, t3, 107, "What does this loop print?", "res = []\nfor x in [2, 4, 6]:\n    res.append(x // 2)\nprint(res)", "[1, 2, 3]", ["[1,2,3]"], pool="practice"),
            _py_code(cid, t3, 108, "debug_code", "Fix the loop bug so `sum_evens(nums)` returns the sum of only even numbers.",
                     "def sum_evens(nums):\n    s = 0\n    for x in nums:\n        if x % 2 == 1:\n            s += x\n    return s",
                     "def sum_evens(nums):\n    s = 0\n    for x in nums:\n        if x % 2 == 0:\n            s += x\n    return s",
                     [{"call": "sum_evens([1, 2, 3, 4])", "expected_return": 6}, {"call": "sum_evens([1, 3, 5])", "expected_return": 0}], pool="practice"),
            _py_code(cid, t3, 109, "write_code", "Write `countdown_list(n)` returning `[n, n-1, ..., 1]` for positive `n`.",
                     "def countdown_list(n):\n    pass",
                     "def countdown_list(n):\n    return list(range(n, 0, -1))",
                     [{"call": "countdown_list(4)", "expected_return": [4, 3, 2, 1]}, {"call": "countdown_list(1)", "expected_return": [1]}], pool="practice"),

            # T4 Practice
            _pred(cid, t4, 110, "What will this print?", "def f(a):\n    return a + 1\nprint(f(f(3)))", "5", pool="practice"),
            _py_code(cid, t4, 111, "debug_code", "Fix `is_long(word, limit=5)` so it returns True when `len(word) >= limit`.",
                     "def is_long(word, limit=5):\n    return len(word) > limit",
                     "def is_long(word, limit=5):\n    return len(word) >= limit",
                     [{"call": "is_long('hello')", "expected_return": True}, {"call": "is_long('hi')", "expected_return": False}], pool="practice"),
            _py_code(cid, t4, 112, "write_code", "Write `max_of_three(a, b, c)` returning the largest of three numbers.",
                     "def max_of_three(a, b, c):\n    pass",
                     "def max_of_three(a, b, c):\n    return max(a, b, c)",
                     [{"call": "max_of_three(3, 9, 4)", "expected_return": 9}, {"call": "max_of_three(-1, -5, -2)", "expected_return": -1}], pool="practice"),

            # T5 Practice
            _pred(cid, t5, 113, "What does this output?", "d = {'a': 1, 'b': 2}\nd['b'] = 9\nprint(len(d), d['b'])", "2 9", pool="practice"),
            _py_code(cid, t5, 114, "debug_code", "Fix `count_freq(items)` so it accurately counts occurrences in a dict.",
                     "def count_freq(items):\n    counts = {}\n    for x in items:\n        counts[x] = 1\n    return counts",
                     "def count_freq(items):\n    counts = {}\n    for x in items:\n        counts[x] = counts.get(x, 0) + 1\n    return counts",
                     [{"call": "count_freq(['a', 'b', 'a'])", "expected_return": {"a": 2, "b": 1}}, {"call": "count_freq([])", "expected_return": {}}], pool="practice"),
            _py_code(cid, t5, 115, "write_code", "Write `invert_dict(d)` swapping string keys and values `{v: k for k, v in d.items()}`.",
                     "def invert_dict(d):\n    pass",
                     "def invert_dict(d):\n    return {v: k for k, v in d.items()}",
                     [{"call": "invert_dict({'x': 'a', 'y': 'b'})", "expected_return": {"a": "x", "b": "y"}}, {"call": "invert_dict({})", "expected_return": {}}], pool="practice"),

            # T6 Practice
            _pred(cid, t6, 116, "What does this OOP code output?", "class A:\n    def val(self): return 1\nclass B(A):\n    def val(self): return 2\nprint(B().val())", "2", pool="practice"),
            _py_code(cid, t6, 117, "debug_code", "Fix `BankAccount.deposit` so `self.balance` increases by `amt` and returns new balance.",
                     "class BankAccount:\n    def __init__(self, bal=0):\n        self.balance = bal\n    def deposit(self, amt):\n        balance = self.balance + amt\n        return self.balance",
                     "class BankAccount:\n    def __init__(self, bal=0):\n        self.balance = bal\n    def deposit(self, amt):\n        self.balance += amt\n        return self.balance",
                     [{"call": "BankAccount(10).deposit(15)", "expected_return": 25}, {"call": "BankAccount().deposit(5)", "expected_return": 5}], pool="practice"),
            _py_code(cid, t6, 118, "write_code", "Write class `Circle` with `__init__(self, r)` and `circumference(self)` returning `round(2 * 3.14 * self.r, 2)`.",
                     "class Circle:\n    def __init__(self, r):\n        pass\n    def circumference(self):\n        pass",
                     "class Circle:\n    def __init__(self, r):\n        self.r = r\n    def circumference(self):\n        return round(2 * 3.14 * self.r, 2)",
                     [{"call": "Circle(5).circumference()", "expected_return": 31.4}, {"call": "Circle(2).circumference()", "expected_return": 12.56}], pool="practice"),
            _mcq(cid, t1, 119, "Which Python function converts the string `'25'` into an integer?", "str()", "int()", "float()", "bool()", "B", pool="practice", explanation="int('25') returns integer 25."),
            _mcq(cid, t2, 120, "Which operator checks whether two values are unequal in Python?", "==", "=", "!=", "<>", "C", pool="practice", explanation="`!=` tests inequality in Python."),
            _mcq(cid, t3, 121, "Which statement immediately exits the innermost loop in Python?", "continue", "pass", "return", "break", "D", pool="practice", explanation="`break` exits the enclosing loop immediately."),
            _mcq(cid, t4, 122, "What does a Python function return by default if it executes without a `return` statement?", "0", "False", "None", "Empty string", "C", pool="practice", explanation="Functions without an explicit return value return `None`."),
            _mcq(cid, t5, 123, "Which Python collection is unordered and automatically removes duplicate elements?", "list", "tuple", "set", "dict", "C", pool="practice", explanation="A `set` stores unique elements with no duplicates."),
            _mcq(cid, t6, 124, "What does the `self` parameter represent inside a Python instance method?", "The parent module", "The current object instance", "A static class constant", "The global scope", "B", pool="practice", explanation="`self` refers to the specific object instance calling the method."),
        ])
        return qs

    # =====================================================================
    # 2. DATA STRUCTURES (7 Topics x 4 Initial = 28, + 21 Practice = 49)
    # =====================================================================
    def ds_bank(cid, tid):
        t7 = tid("Programming Basics")
        t8 = tid("Arrays and Strings")
        t9 = tid("Linked Lists")
        t10 = tid("Stacks and Queues")
        t11 = tid("Recursion Basics")
        t12 = tid("Trees and Binary Search Trees")
        t13 = tid("Graphs and Hashing")
        qs = [
            # T7 Programming Basics
            _mcq(cid, t7, 1, "What is the time complexity of accessing an element in a Python list/array by index?", "O(n)", "O(log n)", "O(1)", "O(n^2)", "C"),
            _pred(cid, t7, 2, "What will this swap code output?", "a, b = 3, 8\na, b = b, a\nprint(a, b)", "8 3"),
            _py_code(cid, t7, 3, "debug_code", "Fix `find_min(arr)` so it finds the actual minimum element instead of returning the first element.",
                     "def find_min(arr):\n    m = arr[0]\n    for x in arr:\n        if x > m:\n            m = x\n    return m",
                     "def find_min(arr):\n    m = arr[0]\n    for x in arr:\n        if x < m:\n            m = x\n    return m",
                     [{"call": "find_min([7, 2, 9, 4])", "expected_return": 2},
                      {"call": "find_min([-3, 0, 5])", "expected_return": -3}]),
            _py_code(cid, t7, 4, "write_code", "Write `two_sum_exists(nums, target)` returning True if any two distinct indices sum to `target`.",
                     "def two_sum_exists(nums, target):\n    pass",
                     "def two_sum_exists(nums, target):\n    seen = set()\n    for x in nums:\n        if (target - x) in seen:\n            return True\n        seen.add(x)\n    return False",
                     [{"call": "two_sum_exists([2, 7, 11], 9)", "expected_return": True},
                      {"call": "two_sum_exists([1, 2, 3], 10)", "expected_return": False},
                      {"call": "two_sum_exists([3, 3], 6)", "expected_return": True}]),

            # T8 Arrays and Strings
            _mcq(cid, t8, 5, "Why can't you assign `s[0] = 'X'` on a Python `str`?", "Strings only hold digits", "Strings are immutable", "Strings are zero-length", "Strings have no indices", "B"),
            _pred(cid, t8, 6, "What does this slicing output?", "word = 'STRUCT'\nprint(word[1:4][::-1])", "URT"),
            _py_code(cid, t8, 7, "debug_code", "Fix `is_palindrome(s)` so it ignores letter case (e.g., `'Racecar'` -> `True`).",
                     "def is_palindrome(s):\n    return s == s[::-1]",
                     "def is_palindrome(s):\n    clean = s.lower()\n    return clean == clean[::-1]",
                     [{"call": "is_palindrome('Racecar')", "expected_return": True},
                      {"call": "is_palindrome('hello')", "expected_return": False},
                      {"call": "is_palindrome('Madam')", "expected_return": True}]),
            _py_code(cid, t8, 8, "write_code", "Write `reverse_words(sentence)` reversing word order (e.g. `'hello world'` -> `'world hello'`).",
                     "def reverse_words(sentence):\n    pass",
                     "def reverse_words(sentence):\n    return ' '.join(sentence.split()[::-1])",
                     [{"call": "reverse_words('hello world')", "expected_return": "world hello"},
                      {"call": "reverse_words('one two three')", "expected_return": "three two one"},
                      {"call": "reverse_words('code')", "expected_return": "code"}]),

            # T9 Linked Lists
            _mcq(cid, t9, 9, "Which operation is O(1) when you hold a pointer to `head` in a singly linked list?", "Finding the middle node", "Inserting a new node before `head`", "Searching by value", "Deleting the last node", "B"),
            _pred(cid, t9, 10, "What does this linked-node traversal output?", "class N:\n    def __init__(self, v, nxt=None): self.v, self.nxt = v, nxt\nh = N(1, N(2, N(3)))\nc = 0\nwhile h:\n    c += h.v\n    h = h.nxt\nprint(c)", "6"),
            _py_code(cid, t9, 11, "debug_code", "Fix the pointer update bug in `linked_length(head)` so it terminates and counts all nodes.",
                     "def linked_length(head):\n    count = 0\n    curr = head\n    while curr is not None:\n        count += 1\n    return count",
                     "def linked_length(head):\n    count = 0\n    curr = head\n    while curr is not None:\n        count += 1\n        curr = curr.get('next')\n    return count",
                     [{"call": "linked_length({'val': 1, 'next': {'val': 2, 'next': None}})", "expected_return": 2},
                      {"call": "linked_length(None)", "expected_return": 0}]),
            _py_code(cid, t9, 12, "write_code", "Nodes are dicts `{'val': x, 'next': node_or_None}`. Write `linked_to_list(head)` returning a list of node values.",
                     "def linked_to_list(head):\n    pass",
                     "def linked_to_list(head):\n    out = []\n    curr = head\n    while curr is not None:\n        out.append(curr['val'])\n        curr = curr['next']\n    return out",
                     [{"call": "linked_to_list({'val': 5, 'next': {'val': 9, 'next': None}})", "expected_return": [5, 9]},
                      {"call": "linked_to_list(None)", "expected_return": []}]),

            # T10 Stacks and Queues
            _mcq(cid, t10, 13, "Which data structure handles print jobs in exact arrival order (FIFO)?", "Stack", "Queue", "Binary Heap", "Tree", "B"),
            _pred(cid, t10, 14, "What will this stack trace print?", "st = []\nst.append(10)\nst.append(20)\nst.pop()\nst.append(30)\nprint(st[-1])", "30"),
            _py_code(cid, t10, 15, "debug_code", "Fix `is_balanced(s)` checking `'('` and `')'` using a stack so extra closing parens return `False`.",
                     "def is_balanced(s):\n    st = []\n    for ch in s:\n        if ch == '(':\n            st.append(ch)\n        elif ch == ')':\n            st.pop()\n    return len(st) == 0",
                     "def is_balanced(s):\n    st = []\n    for ch in s:\n        if ch == '(':\n            st.append(ch)\n        elif ch == ')':\n            if not st:\n                return False\n            st.pop()\n    return len(st) == 0",
                     [{"call": "is_balanced('(())')", "expected_return": True},
                      {"call": "is_balanced('())')", "expected_return": False},
                      {"call": "is_balanced('(()')", "expected_return": False}]),
            _py_code(cid, t10, 16, "write_code", "Write `simulate_queue(ops)` handling `('enqueue', x)` and `'dequeue'` on a list queue and returning dequeued items.",
                     "def simulate_queue(ops):\n    pass",
                     "def simulate_queue(ops):\n    q, out = [], []\n    for op in ops:\n        if isinstance(op, tuple) and op[0] == 'enqueue':\n            q.append(op[1])\n        elif op == 'dequeue' and q:\n            out.append(q.pop(0))\n    return out",
                     [{"call": "simulate_queue([('enqueue', 1), ('enqueue', 2), 'dequeue'])", "expected_return": [1]},
                      {"call": "simulate_queue(['dequeue', ('enqueue', 5), 'dequeue'])", "expected_return": [5]}]),

            # T11 Recursion Basics
            _mcq(cid, t11, 17, "What happens if a recursive function has no base case?", "It runs faster", "It repeats forever until RecursionError / stack overflow", "It returns None immediately", "It switches to a loop", "B"),
            _pred(cid, t11, 18, "What will this recursive call print?", "def f(n):\n    if n <= 0: return 0\n    return n + f(n - 2)\nprint(f(5))", "9"),
            _py_code(cid, t11, 19, "debug_code", "Fix the missing base case in `factorial(n)` so it returns `n!` cleanly using recursion.",
                     "def factorial(n):\n    return n * factorial(n - 1)",
                     "def factorial(n):\n    if n <= 1:\n        return 1\n    return n * factorial(n - 1)",
                     [{"call": "factorial(4)", "expected_return": 24},
                      {"call": "factorial(0)", "expected_return": 1},
                      {"call": "factorial(5)", "expected_return": 120}], require_recursion=True),
            _py_code(cid, t11, 20, "write_code", "Write recursive `rec_sum(nums)` returning the sum of a list of integers using recursion.",
                     "def rec_sum(nums):\n    pass",
                     "def rec_sum(nums):\n    if not nums:\n        return 0\n    return nums[0] + rec_sum(nums[1:])",
                     [{"call": "rec_sum([1, 2, 3, 4])", "expected_return": 10},
                      {"call": "rec_sum([])", "expected_return": 0},
                      {"call": "rec_sum([7])", "expected_return": 7}], require_recursion=True),

            # T12 Trees and Binary Search Trees
            _mcq(cid, t12, 21, "Which traversal of a Binary Search Tree visits nodes in ascending sorted order?", "Preorder", "Inorder", "Postorder", "Level-order", "B"),
            _pred(cid, t12, 22, "What does this BST inorder function output?", "t = {'v': 5, 'l': {'v': 2, 'l': None, 'r': None}, 'r': {'v': 8, 'l': None, 'r': None}}\ndef inord(n):\n    return inord(n['l']) + [n['v']] + inord(n['r']) if n else []\nprint(inord(t))", "[2, 5, 8]", ["[2,5,8]"]),
            _py_code(cid, t12, 23, "debug_code", "Fix `bst_contains(node, target)` so it searches the right subtree when `target > node['v']`.",
                     "def bst_contains(node, target):\n    if not node:\n        return False\n    if node['v'] == target:\n        return True\n    return bst_contains(node['l'], target)",
                     "def bst_contains(node, target):\n    if not node:\n        return False\n    if node['v'] == target:\n        return True\n    if target < node['v']:\n        return bst_contains(node['l'], target)\n    return bst_contains(node['r'], target)",
                     [{"call": "bst_contains({'v': 5, 'l': {'v': 2, 'l': None, 'r': None}, 'r': {'v': 8, 'l': None, 'r': None}}, 8)", "expected_return": True},
                      {"call": "bst_contains({'v': 5, 'l': None, 'r': None}, 9)", "expected_return": False}]),
            _py_code(cid, t12, 24, "write_code", "Nodes have `{'v': x, 'l': left, 'r': right}`. Write `tree_count(node)` counting all nodes.",
                     "def tree_count(node):\n    pass",
                     "def tree_count(node):\n    if not node:\n        return 0\n    return 1 + tree_count(node['l']) + tree_count(node['r'])",
                     [{"call": "tree_count({'v': 1, 'l': {'v': 2, 'l': None, 'r': None}, 'r': None})", "expected_return": 2},
                      {"call": "tree_count(None)", "expected_return": 0}]),

            # T13 Graphs and Hashing
            _mcq(cid, t13, 25, "Which structure detects already-visited nodes during Breadth-First Search in O(1) lookup?", "Linked List", "Hash Set", "Stack", "Array scan", "B"),
            _pred(cid, t13, 26, "What does this hashing lookup code print?", "seen = set()\ndups = []\nfor x in [1, 2, 1, 3, 2]:\n    if x in seen: dups.append(x)\n    seen.add(x)\nprint(dups)", "[1, 2]", ["[1,2]"]),
            _py_code(cid, t13, 27, "debug_code", "Fix `bfs_reachable(adj, start)` so it marks nodes visited and doesn't loop infinitely on cycles.",
                     "def bfs_reachable(adj, start):\n    q = [start]\n    order = []\n    while q:\n        curr = q.pop(0)\n        order.append(curr)\n        for nb in adj.get(curr, []):\n            q.append(nb)\n    return sorted(order)",
                     "def bfs_reachable(adj, start):\n    q = [start]\n    visited = {start}\n    while q:\n        curr = q.pop(0)\n        for nb in adj.get(curr, []):\n            if nb not in visited:\n                visited.add(nb)\n                q.append(nb)\n    return sorted(visited)",
                     [{"call": "bfs_reachable({1: [2], 2: [1, 3], 3: []}, 1)", "expected_return": [1, 2, 3]},
                      {"call": "bfs_reachable({1: [], 2: [3]}, 1)", "expected_return": [1]}]),
            _py_code(cid, t13, 28, "write_code", "Write `has_duplicate(nums)` using hashing/sets in O(n) time returning True if any value repeats.",
                     "def has_duplicate(nums):\n    pass",
                     "def has_duplicate(nums):\n    return len(set(nums)) < len(nums)",
                     [{"call": "has_duplicate([1, 2, 3, 1])", "expected_return": True},
                      {"call": "has_duplicate([4, 5, 6])", "expected_return": False},
                      {"call": "has_duplicate([])", "expected_return": False}]),
        ]

        # Practice Pool for Data Structures (3 per topic = 21)
        qs.extend([
            _pred(cid, t7, 101, "What output does this produce?", "x = [1, 2, 3]\ny = x\ny.append(4)\nprint(len(x))", "4", pool="practice"),
            _py_code(cid, t7, 102, "debug_code", "Fix `running_total(nums)` so it builds cumulative sums without mutating the input list.",
                     "def running_total(nums):\n    for i in range(1, len(nums)):\n        nums[i] += nums[i-1]\n    return nums",
                     "def running_total(nums):\n    res = []\n    s = 0\n    for x in nums:\n        s += x\n        res.append(s)\n    return res",
                     [{"call": "running_total([1, 2, 3])", "expected_return": [1, 3, 6]}, {"call": "running_total([])", "expected_return": []}], pool="practice"),
            _py_code(cid, t7, 103, "write_code", "Write `binary_search(arr, target)` returning index of `target` in sorted `arr`, or `-1`.",
                     "def binary_search(arr, target):\n    pass",
                     "def binary_search(arr, target):\n    lo, hi = 0, len(arr) - 1\n    while lo <= hi:\n        mid = (lo + hi) // 2\n        if arr[mid] == target: return mid\n        if arr[mid] < target: lo = mid + 1\n        else: hi = mid - 1\n    return -1",
                     [{"call": "binary_search([1, 3, 5, 7], 5)", "expected_return": 2}, {"call": "binary_search([1, 3, 5], 4)", "expected_return": -1}], pool="practice"),

            _pred(cid, t8, 104, "What will this print?", "s = 'abac'\nprint(s.count('a'), s[-1])", "2 c", pool="practice"),
            _py_code(cid, t8, 105, "debug_code", "Fix `rotate_left(arr)` so `[1, 2, 3]` becomes `[2, 3, 1]` and `[]` stays `[]`.",
                     "def rotate_left(arr):\n    return arr[1:] + arr[0]",
                     "def rotate_left(arr):\n    return arr[1:] + [arr[0]] if arr else []",
                     [{"call": "rotate_left([1, 2, 3])", "expected_return": [2, 3, 1]}, {"call": "rotate_left([])", "expected_return": []}], pool="practice"),
            _py_code(cid, t8, 106, "write_code", "Write `count_vowels(s)` counting `a,e,i,o,u` case-insensitively.",
                     "def count_vowels(s):\n    pass",
                     "def count_vowels(s):\n    return sum(1 for ch in s.lower() if ch in 'aeiou')",
                     [{"call": "count_vowels('Hello')", "expected_return": 2}, {"call": "count_vowels('SKY')", "expected_return": 0}], pool="practice"),

            _pred(cid, t9, 107, "What does this print?", "a = {'v': 10, 'next': {'v': 20, 'next': None}}\nprint(a['next']['v'])", "20", pool="practice"),
            _py_code(cid, t9, 108, "debug_code", "Fix `prepend_node(head, val)` so it creates a new head pointing to the old `head`.",
                     "def prepend_node(head, val):\n    return {'val': val, 'next': None}",
                     "def prepend_node(head, val):\n    return {'val': val, 'next': head}",
                     [{"call": "prepend_node({'val': 2, 'next': None}, 1)['next']['val']", "expected_return": 2},
                      {"call": "prepend_node(None, 5)", "expected_return": {"val": 5, "next": None}}], pool="practice"),
            _py_code(cid, t9, 109, "write_code", "Write `linked_contains(head, target)` returning True if any node has `val == target`.",
                     "def linked_contains(head, target):\n    pass",
                     "def linked_contains(head, target):\n    curr = head\n    while curr:\n        if curr['val'] == target: return True\n        curr = curr['next']\n    return False",
                     [{"call": "linked_contains({'val': 1, 'next': {'val': 4, 'next': None}}, 4)", "expected_return": True},
                      {"call": "linked_contains(None, 4)", "expected_return": False}], pool="practice"),

            _pred(cid, t10, 110, "What does this print?", "s = [1, 2]\ns.append(3)\nprint(s.pop() + s.pop())", "5", pool="practice"),
            _py_code(cid, t10, 111, "debug_code", "Fix `reverse_with_stack(items)` so popping builds the reversed list.",
                     "def reverse_with_stack(items):\n    st = list(items)\n    out = []\n    while st:\n        out.append(st.pop(0))\n    return out",
                     "def reverse_with_stack(items):\n    st = list(items)\n    out = []\n    while st:\n        out.append(st.pop())\n    return out",
                     [{"call": "reverse_with_stack([1, 2, 3])", "expected_return": [3, 2, 1]}, {"call": "reverse_with_stack([])", "expected_return": []}], pool="practice"),
            _py_code(cid, t10, 112, "write_code", "Write `eval_rpn_add_sub(tokens)` for space-separated `'3 4 +'` or `'10 3 -'` RPN expressions.",
                     "def eval_rpn_add_sub(expr):\n    pass",
                     "def eval_rpn_add_sub(expr):\n    st = []\n    for tok in expr.split():\n        if tok in ('+', '-'):\n            b, a = st.pop(), st.pop()\n            st.append(a + b if tok == '+' else a - b)\n        else:\n            st.append(int(tok))\n    return st[0]",
                     [{"call": "eval_rpn_add_sub('3 4 +')", "expected_return": 7}, {"call": "eval_rpn_add_sub('10 3 - 2 +')", "expected_return": 9}], pool="practice"),

            _pred(cid, t11, 113, "What does this recursion print?", "def fib(n):\n    return n if n <= 1 else fib(n-1)+fib(n-2)\nprint(fib(5))", "5", pool="practice"),
            _py_code(cid, t11, 114, "debug_code", "Fix `power(base, exp)` using recursion with base case `exp == 0` returning `1`.",
                     "def power(base, exp):\n    return base * power(base, exp - 1)",
                     "def power(base, exp):\n    if exp == 0: return 1\n    return base * power(base, exp - 1)",
                     [{"call": "power(2, 4)", "expected_return": 16}, {"call": "power(5, 0)", "expected_return": 1}], require_recursion=True, pool="practice"),
            _py_code(cid, t11, 115, "write_code", "Write recursive `count_digits(n)` counting decimal digits of positive int `n` (base case `n < 10 -> 1`).",
                     "def count_digits(n):\n    pass",
                     "def count_digits(n):\n    if n < 10: return 1\n    return 1 + count_digits(n // 10)",
                     [{"call": "count_digits(1234)", "expected_return": 4}, {"call": "count_digits(7)", "expected_return": 1}], require_recursion=True, pool="practice"),

            _pred(cid, t12, 116, "What does this tree sum return?", "t = {'v': 10, 'l': {'v': 3, 'l': None, 'r': None}, 'r': None}\ndef s(n): return n['v'] + s(n['l']) + s(n['r']) if n else 0\nprint(s(t))", "13", pool="practice"),
            _py_code(cid, t12, 117, "debug_code", "Fix `tree_max(node)` so empty children don't crash and it finds the max node value.",
                     "def tree_max(node):\n    return max(node['v'], tree_max(node['l']), tree_max(node['r']))",
                     "def tree_max(node):\n    if not node: return float('-inf')\n    return max(node['v'], tree_max(node['l']), tree_max(node['r']))",
                     [{"call": "tree_max({'v': 4, 'l': {'v': 1, 'l': None, 'r': None}, 'r': {'v': 9, 'l': None, 'r': None}})", "expected_return": 9}], pool="practice"),
            _py_code(cid, t12, 118, "write_code", "Write `leaf_count(node)` returning the count of leaf nodes (where both `'l'` and `'r'` are None).",
                     "def leaf_count(node):\n    pass",
                     "def leaf_count(node):\n    if not node: return 0\n    if not node['l'] and not node['r']: return 1\n    return leaf_count(node['l']) + leaf_count(node['r'])",
                     [{"call": "leaf_count({'v': 1, 'l': {'v': 2, 'l': None, 'r': None}, 'r': {'v': 3, 'l': None, 'r': None}})", "expected_return": 2},
                      {"call": "leaf_count(None)", "expected_return": 0}], pool="practice"),

            _pred(cid, t13, 119, "What does this graph check print?", "g = {1: [2, 3], 2: [], 3: []}\nprint(len(g[1]))", "2", pool="practice"),
            _py_code(cid, t13, 120, "debug_code", "Fix `char_counts(s)` so repeated letters increment instead of overwriting with `1`.",
                     "def char_counts(s):\n    m = {}\n    for c in s:\n        m[c] = 1\n    return m",
                     "def char_counts(s):\n    m = {}\n    for c in s:\n        m[c] = m.get(c, 0) + 1\n    return m",
                     [{"call": "char_counts('aab')", "expected_return": {"a": 2, "b": 1}}], pool="practice"),
            _py_code(cid, t13, 121, "write_code", "Write `out_degree(adj, node)` returning how many outgoing neighbors `node` has in `adj`.",
                     "def out_degree(adj, node):\n    pass",
                     "def out_degree(adj, node):\n    return len(adj.get(node, []))",
                     [{"call": "out_degree({'A': ['B', 'C'], 'B': []}, 'A')", "expected_return": 2},
                      {"call": "out_degree({'A': []}, 'Z')", "expected_return": 0}], pool="practice"),
            _mcq(cid, t7, 122, "Which time complexity grows slowest as input size `n` increases?", "O(n^2)", "O(n log n)", "O(n)", "O(log n)", "D", pool="practice", explanation="Logarithmic growth O(log n) scales much slower than linear or quadratic growth."),
            _mcq(cid, t8, 123, "Why is checking whether a sorted array contains a value faster than an unsorted array?", "Binary search runs in O(log n)", "Arrays shrink automatically", "Index 0 stores the length", "Duplicates are removed", "A", pool="practice", explanation="Binary search halves the search range each step on sorted arrays in O(log n)."),
            _mcq(cid, t9, 124, "In a singly linked list, what does the `next` field of the last node hold?", "The head node", "Its own value", "None (or null)", "Index -1", "C", pool="practice", explanation="The tail node points to None/null to mark the end of the list."),
            _mcq(cid, t10, 125, "Which data structure follows First-In, First-Out (FIFO) ordering?", "Stack", "Queue", "Binary Heap", "Hash Table", "B", pool="practice", explanation="A Queue serves elements in the order they arrived (FIFO)."),
            _mcq(cid, t11, 126, "What is essential in every recursive function to prevent infinite calls?", "A global counter", "A base case that stops recursion", "Two return loops", "A class constructor", "B", pool="practice", explanation="Every recursive function needs a reachable base case to terminate."),
            _mcq(cid, t12, 127, "In a Binary Search Tree (BST), which traversal visits keys in sorted ascending order?", "Preorder", "Postorder", "Inorder", "Level-order", "C", pool="practice", explanation="Inorder traversal (left, node, right) yields BST keys in sorted order."),
            _mcq(cid, t13, 128, "Which traversal algorithm uses a Queue to explore a graph level by level?", "Depth-First Search (DFS)", "Breadth-First Search (BFS)", "Binary Search", "Insertion Sort", "B", pool="practice", explanation="BFS uses a Queue to visit vertices in order of distance from the start."),
        ])
        return qs

    # =====================================================================
    # 3. JAVA PROGRAMMING (6 Topics x 4 Initial = 24, + 18 Practice = 42)
    # =====================================================================
    def java_bank(cid, tid):
        t14 = tid("Java Syntax and Data Types")
        t15 = tid("Operators and Control Statements")
        t16 = tid("Loops and Arrays")
        t17 = tid("Methods and Classes")
        t18 = tid("Inheritance and Polymorphism")
        t19 = tid("Collections and Exception Handling")
        qs = [
            # T14 Java Syntax and Data Types
            _mcq(cid, t14, 1, "Which keyword makes a variable constant after assignment in Java?", "const", "final", "static", "fixed", "B"),
            _pred(cid, t14, 2, "What will this Java snippet output?", "int a = 7;\nint b = 2;\nSystem.out.println(a / b);", "3", explanation="Integer division 7 / 2 truncates to 3 in Java."),
            _java_code(cid, t14, 3, "debug_code", "Fix the type mismatch in `doubleHalf(int n)` so it returns `n / 2.0` as a `double`.",
                       "static int doubleHalf(int n) {\n    return n / 2.0;\n}",
                       "static double doubleHalf(int n) {\n    return n / 2.0;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "doubleHalf(5)", "expected": "2.5"}, {"expr": "doubleHalf(4)", "expected": "2.0"}],
                        "required_pattern_groups": [[r"\bstatic\s+double\s+doubleHalf\s*\(\s*int\s+\w+\s*\)"], [r"return\s+"]] }),
            _java_code(cid, t14, 4, "write_code", "Write Java method `static String greetUser(String name)` returning `\"Hello, \" + name`.",
                       "static String greetUser(String name) {\n    // return greeting\n}",
                       "static String greetUser(String name) {\n    return \"Hello, \" + name;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "greetUser(\"Ada\")", "expected": "Hello, Ada"}, {"expr": "greetUser(\"Sam\")", "expected": "Hello, Sam"}],
                        "required_pattern_groups": [[r"String\s+greetUser\s*\(\s*String\s+\w+\s*\)"], [r"return\s+.*Hello"]] }),

            # T15 Operators and Control Statements
            _mcq(cid, t15, 5, "How should you compare the character contents of two Java `String` objects `a` and `b`?", "a == b", "a === b", "a.equals(b)", "a := b", "C"),
            _pred(cid, t15, 6, "What does this Java ternary expression print?", "int x = 10;\nString r = (x >= 10 && x < 20) ? \"Mid\" : \"Out\";\nSystem.out.println(r);", "Mid"),
            _java_code(cid, t15, 7, "debug_code", "Fix the logical operator in `inRange(int x)` so it checks `x >= 1 AND x <= 10`.",
                       "static boolean inRange(int x) {\n    return x >= 1 || x <= 10;\n}",
                       "static boolean inRange(int x) {\n    return x >= 1 && x <= 10;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "inRange(5)", "expected": "true"}, {"expr": "inRange(15)", "expected": "false"}, {"expr": "inRange(0)", "expected": "false"}],
                        "forbidden_patterns": [r"\|\|"],
                        "required_pattern_groups": [[r"&&"], [r"return\s+"]] }),
            _java_code(cid, t15, 8, "write_code", "Write `static int maxTwo(int a, int b)` returning the larger integer.",
                       "static int maxTwo(int a, int b) {\n    \n}",
                       "static int maxTwo(int a, int b) {\n    return (a > b) ? a : b;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "maxTwo(4, 9)", "expected": "9"}, {"expr": "maxTwo(8, 2)", "expected": "8"}],
                        "required_pattern_groups": [[r"int\s+maxTwo\s*\(\s*int\s+\w+\s*,\s*int\s+\w+\s*\)"], [r"return\s+"]] }),

            # T16 Loops and Arrays
            _mcq(cid, t16, 9, "Which expression returns the length of a Java array `nums`?", "nums.length()", "nums.size()", "nums.length", "len(nums)", "C"),
            _pred(cid, t16, 10, "What will this Java loop output?", "int[] a = {2, 4, 6};\nint s = 0;\nfor (int i = 0; i < a.length; i++) s += a[i];\nSystem.out.println(s);", "12"),
            _java_code(cid, t16, 11, "debug_code", "Fix the array bounds bug in `sumArray(int[] arr)` so it does not throw ArrayIndexOutOfBoundsException.",
                       "static int sumArray(int[] arr) {\n    int sum = 0;\n    for (int i = 0; i <= arr.length; i++) {\n        sum += arr[i];\n    }\n    return sum;\n}",
                       "static int sumArray(int[] arr) {\n    int sum = 0;\n    for (int i = 0; i < arr.length; i++) {\n        sum += arr[i];\n    }\n    return sum;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "sumArray(new int[]{1, 2, 3})", "expected": "6"}, {"expr": "sumArray(new int[]{})", "expected": "0"}],
                        "forbidden_patterns": [r"<=\s*arr\.length"],
                        "required_pattern_groups": [[r"<\s*arr\.length|:\s*arr"]] }),
            _java_code(cid, t16, 12, "write_code", "Write `static int countPositives(int[] arr)` counting values `> 0`.",
                       "static int countPositives(int[] arr) {\n    \n}",
                       "static int countPositives(int[] arr) {\n    int c = 0;\n    for (int x : arr) { if (x > 0) c++; }\n    return c;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "countPositives(new int[]{-1, 2, 5, 0})", "expected": "2"}, {"expr": "countPositives(new int[]{-3})", "expected": "0"}],
                        "required_pattern_groups": [[r"for\s*\("], [r">\s*0"]] }),

            # T17 Methods and Classes
            _mcq(cid, t17, 13, "Which access modifier hides a class field so only code inside the same class can access it?", "public", "protected", "private", "package", "C"),
            _pred(cid, t17, 14, "What will this overloaded Java method print?", "static int f(int x) { return x + 1; }\nstatic int f(int x, int y) { return x + y; }\nSystem.out.println(f(3) + f(2, 4));", "10"),
            _java_code(cid, t17, 15, "debug_code", "Fix `Counter` constructor so field `this.val` is set properly.",
                       "static class Counter {\n    int val;\n    Counter(int val) { val = val; }\n    int getVal() { return val; }\n}",
                       "static class Counter {\n    int val;\n    Counter(int val) { this.val = val; }\n    int getVal() { return val; }\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "new Counter(7).getVal()", "expected": "7"}],
                        "required_pattern_groups": [[r"this\.val\s*="]] }),
            _java_code(cid, t17, 16, "write_code", "Write `static int square(int n)` returning `n * n`.",
                       "static int square(int n) {\n    \n}",
                       "static int square(int n) {\n    return n * n;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "square(6)", "expected": "36"}, {"expr": "square(-3)", "expected": "9"}],
                        "required_pattern_groups": [[r"int\s+square\s*\(\s*int\s+\w+\s*\)"], [r"return\s+"]] }),

            # T18 Inheritance and Polymorphism
            _mcq(cid, t18, 17, "Which keyword is used by a Java subclass to inherit from a superclass?", "implements", "inherits", "extends", "super", "C"),
            _pred(cid, t18, 18, "What will this polymorphic Java snippet print?", "class Animal { String say() { return \"...\"; } }\nclass Cat extends Animal { String say() { return \"Meow\"; } }\nAnimal a = new Cat();\nSystem.out.println(a.say());", "Meow"),
            _java_code(cid, t18, 19, "debug_code", "Fix `Dog` so it properly inherits from `Animal` and overrides `sound()` returning `\"Bark\"`.",
                       "static class Animal { String sound() { return \"?\"; } }\nstatic class Dog {\n    @Override String sound() { return \"Bark\"; }\n}",
                       "static class Animal { String sound() { return \"?\"; } }\nstatic class Dog extends Animal {\n    @Override String sound() { return \"Bark\"; }\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "((Animal) new Dog()).sound()", "expected": "Bark"}],
                        "required_pattern_groups": [[r"class\s+Dog\s+extends\s+Animal"], [r"Bark"]] }),
            _java_code(cid, t18, 20, "write_code", "Given `static class Base { int id() { return 1; } }`, write `static class Sub extends Base` overriding `id()` to return `2`.",
                       "static class Sub extends Base {\n    \n}",
                       "static class Sub extends Base {\n    @Override int id() { return 2; }\n}",
                       {"java_wrapper": "method",
                        "java_extra_classes": "static class Base { int id() { return 1; } }",
                        "java_test_cases": [{"expr": "new Sub().id()", "expected": "2"}],
                        "required_pattern_groups": [[r"extends\s+Base"], [r"return\s+2\s*;"]] }),

            # T19 Collections and Exception Handling
            _mcq(cid, t19, 21, "Which block in a `try-catch-finally` statement executes even if no exception is thrown?", "catch only", "finally", "throw", "throws", "B"),
            _pred(cid, t19, 22, "What will this Java try-catch block output?", "try {\n    int x = 10 / 0;\n    System.out.print(\"A\");\n} catch (Exception e) {\n    System.out.print(\"B\");\n} finally {\n    System.out.print(\"C\");\n}", "BC"),
            _java_code(cid, t19, 23, "debug_code", "Fix `safeDiv(int a, int b)` using try-catch so dividing by 0 returns `0` cleanly.",
                       "static int safeDiv(int a, int b) {\n    return a / b;\n}",
                       "static int safeDiv(int a, int b) {\n    try { return a / b; }\n    catch (ArithmeticException e) { return 0; }\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "safeDiv(10, 2)", "expected": "5"}, {"expr": "safeDiv(10, 0)", "expected": "0"}],
                        "required_pattern_groups": [[r"try|==\s*0|!=\s*0"], [r"return\s+"]] }),
            _java_code(cid, t19, 24, "write_code", "Write `static int listSum(ArrayList<Integer> list)` summing all integers in the `ArrayList`.",
                       "static int listSum(ArrayList<Integer> list) {\n    \n}",
                       "static int listSum(ArrayList<Integer> list) {\n    int s = 0;\n    for (int x : list) s += x;\n    return s;\n}",
                       {"java_wrapper": "method",
                        "java_test_cases": [{"expr": "listSum(new ArrayList<>(Arrays.asList(3, 5, 2)))", "expected": "10"}],
                        "required_pattern_groups": [[r"ArrayList<Integer>"], [r"for\s*\("]] }),
        ]

        # Practice Pool for Java (18 questions)
        qs.extend([
            _pred(cid, t14, 101, "What will this print?", "double d = 5.0 / 2;\nSystem.out.println(d);", "2.5", pool="practice"),
            _java_code(cid, t14, 102, "debug_code", "Fix the constant bug so `PI` cannot be reassigned or compiles properly and returns `3.14`.",
                       "static double getPi() {\n    int pi = 3.14;\n    return pi;\n}",
                       "static double getPi() {\n    final double pi = 3.14;\n    return pi;\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "getPi()", "expected": "3.14"}], "required_pattern_groups": [[r"double\s+pi\s*=\s*3\.14"]]}, pool="practice"),
            _java_code(cid, t14, 103, "write_code", "Write `static int toCents(double dollars)` returning `(int) Math.round(dollars * 100)`.",
                       "static int toCents(double dollars) {\n    \n}",
                       "static int toCents(double dollars) {\n    return (int) Math.round(dollars * 100);\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "toCents(2.5)", "expected": "250"}], "required_pattern_groups": [[r"\*\s*100"]]}, pool="practice"),

            _pred(cid, t15, 104, "What will this switch print?", "int n = 2;\nswitch(n) { case 1: System.out.print(\"A\"); break; case 2: System.out.print(\"B\"); break; default: System.out.print(\"C\"); }", "B", pool="practice"),
            _java_code(cid, t15, 105, "debug_code", "Fix string comparison in `isAdmin(String role)` so it compares values properly with `.equals`.",
                       "static boolean isAdmin(String role) {\n    return role == \"admin\";\n}",
                       "static boolean isAdmin(String role) {\n    return \"admin\".equals(role);\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "isAdmin(new String(\"admin\"))", "expected": "true"}, {"expr": "isAdmin(\"user\")", "expected": "false"}],
                        "forbidden_patterns": [r"role\s*=="], "required_pattern_groups": [[r"\.equals\("]]}, pool="practice"),
            _java_code(cid, t15, 106, "write_code", "Write `static String passOrFail(int marks)` returning `\"Pass\"` if `marks >= 40` else `\"Fail\"`.",
                       "static String passOrFail(int marks) {\n    \n}",
                       "static String passOrFail(int marks) {\n    return marks >= 40 ? \"Pass\" : \"Fail\";\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "passOrFail(40)", "expected": "Pass"}, {"expr": "passOrFail(35)", "expected": "Fail"}],
                        "required_pattern_groups": [[r">=\s*40"], [r"Pass"]]}, pool="practice"),

            _pred(cid, t16, 107, "What does this do-while print?", "int i = 5;\ndo { System.out.print(i); i++; } while (i < 3);", "5", pool="practice"),
            _java_code(cid, t16, 108, "debug_code", "Fix `findMax(int[] a)` so it tracks the largest value instead of overwriting on index `0`.",
                       "static int findMax(int[] a) {\n    int m = a[0];\n    for (int x : a) {\n        if (x < m) m = x;\n    }\n    return m;\n}",
                       "static int findMax(int[] a) {\n    int m = a[0];\n    for (int x : a) {\n        if (x > m) m = x;\n    }\n    return m;\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "findMax(new int[]{3, 9, 2})", "expected": "9"}], "required_pattern_groups": [[r">\s*m|m\s*<"]]}, pool="practice"),
            _java_code(cid, t16, 109, "write_code", "Write `static int sumRange(int n)` returning `1 + 2 + ... + n`.",
                       "static int sumRange(int n) {\n    \n}",
                       "static int sumRange(int n) {\n    int s = 0;\n    for (int i = 1; i <= n; i++) s += i;\n    return s;\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "sumRange(4)", "expected": "10"}], "required_pattern_groups": [[r"for\s*\("], [r"return\s+"]]}, pool="practice"),

            _pred(cid, t17, 110, "What does this print?", "class P { int x = 4; }\nP p1 = new P();\nP p2 = p1;\np2.x = 9;\nSystem.out.println(p1.x);", "9", pool="practice"),
            _java_code(cid, t17, 111, "debug_code", "Fix the missing return type on `doubleVal(int n)`.",
                       "static doubleVal(int n) {\n    return n * 2;\n}",
                       "static int doubleVal(int n) {\n    return n * 2;\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "doubleVal(5)", "expected": "10"}], "required_pattern_groups": [[r"int\s+doubleVal"]]}, pool="practice"),
            _java_code(cid, t17, 112, "write_code", "Write `static class Box` with `int size;` constructor `Box(int size)` and `int area() { return size * size; }`.",
                       "static class Box {\n    \n}",
                       "static class Box {\n    int size;\n    Box(int size) { this.size = size; }\n    int area() { return size * size; }\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "new Box(5).area()", "expected": "25"}], "required_pattern_groups": [[r"class\s+Box"], [r"area\s*\("]]}, pool="practice"),

            _pred(cid, t18, 113, "What does `super.x` print here?", "class A { int x = 10; }\nclass B extends A { int x = 20; void show() { System.out.println(super.x); } }\nnew B().show();", "10", pool="practice"),
            _java_code(cid, t18, 114, "debug_code", "Fix constructor chaining in `Child` to call `super(n)` instead of reassigning directly.",
                       "static class Parent { int n; Parent(int n) { this.n = n; } }\nstatic class Child extends Parent {\n    Child(int n) { }\n}",
                       "static class Parent { int n; Parent(int n) { this.n = n; } }\nstatic class Child extends Parent {\n    Child(int n) { super(n); }\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "new Child(8).n", "expected": "8"}], "required_pattern_groups": [[r"super\s*\("]]}, pool="practice"),
            _java_code(cid, t18, 115, "write_code", "Given `static class Shape { int sides() { return 0; } }`, write `static class Triangle extends Shape` returning `3` in `sides()`.",
                       "static class Triangle extends Shape {\n    \n}",
                       "static class Triangle extends Shape {\n    @Override int sides() { return 3; }\n}",
                       {"java_wrapper": "method", "java_extra_classes": "static class Shape { int sides() { return 0; } }",
                        "java_test_cases": [{"expr": "new Triangle().sides()", "expected": "3"}], "required_pattern_groups": [[r"extends\s+Shape"], [r"3"]]}, pool="practice"),

            _pred(cid, t19, 116, "What does this HashMap snippet print?", "HashMap<String, Integer> m = new HashMap<>();\nm.put(\"a\", 1);\nm.put(\"a\", 5);\nSystem.out.println(m.size() + \":\" + m.get(\"a\"));", "1:5", pool="practice"),
            _java_code(cid, t19, 117, "debug_code", "Fix `getOrZero(HashMap<String, Integer> map, String k)` so missing keys return `0` instead of `null`.",
                       "static int getOrZero(HashMap<String, Integer> map, String k) {\n    return map.get(k);\n}",
                       "static int getOrZero(HashMap<String, Integer> map, String k) {\n    return map.getOrDefault(k, 0);\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "getOrZero(new HashMap<>(), \"missing\")", "expected": "0"}],
                        "required_pattern_groups": [[r"getOrDefault|containsKey|!=\s*null|==\s*null"]]}, pool="practice"),
            _java_code(cid, t19, 118, "write_code", "Write `static int parseSafe(String s)` using try-catch around `Integer.parseInt(s)`, returning `-1` on NumberFormatException.",
                       "static int parseSafe(String s) {\n    \n}",
                       "static int parseSafe(String s) {\n    try { return Integer.parseInt(s); }\n    catch (NumberFormatException e) { return -1; }\n}",
                       {"java_wrapper": "method", "java_test_cases": [{"expr": "parseSafe(\"42\")", "expected": "42"}, {"expr": "parseSafe(\"abc\")", "expected": "-1"}],
                        "required_pattern_groups": [[r"Integer\.parseInt"], [r"catch"]]}, pool="practice"),
            _mcq(cid, t14, 119, "Which Java primitive type stores a single 16-bit Unicode character?", "String", "byte", "char", "boolean", "C", pool="practice", explanation="`char` stores a single 16-bit Unicode character in single quotes."),
            _mcq(cid, t15, 120, "Which statement prevents fall-through to the next `case` inside a Java `switch` block?", "skip", "stop", "break", "continue", "C", pool="practice", explanation="`break` exits the `switch` block after matching a case."),
            _mcq(cid, t16, 121, "In Java, how do you get the number of elements in an array `int[] nums`?", "nums.size()", "nums.length", "nums.length()", "len(nums)", "B", pool="practice", explanation="Java arrays expose a public `.length` field (not a method)."),
            _mcq(cid, t17, 122, "Which return type must a Java method declare if it does not return any value?", "null", "empty", "void", "None", "C", pool="practice", explanation="`void` indicates a method returns no value."),
            _mcq(cid, t18, 123, "Which annotation tells the Java compiler that a subclass method overrides a superclass method?", "@Overload", "@Override", "@Extends", "@Super", "B", pool="practice", explanation="`@Override` asks the compiler to verify a valid method override."),
            _mcq(cid, t19, 124, "Which block in Java always executes whether an exception is thrown or not?", "catch", "throw", "finally", "retry", "C", pool="practice", explanation="`finally` runs regardless of whether the `try` block throws an exception."),
        ])
        return qs

    # =====================================================================
    # 4. AI/ML (6 Topics x 4 Initial = 24, + 18 Practice = 42)
    # =====================================================================
    def aiml_bank(cid, tid):
        t20 = tid("Python Fundamentals")
        t21 = tid("Mathematics and Statistics Basics")
        t22 = tid("NumPy and Pandas")
        t23 = tid("Data Preprocessing and Visualization")
        t24 = tid("Machine Learning Algorithms")
        t25 = tid("Model Evaluation and AI Projects")
        qs = [
            # T20 Python Fundamentals
            _mcq(cid, t20, 1, "What does `[x * 2 for x in [1, 2, 3]]` demonstrate in Python?", "Recursion", "List comprehension", "Lambda binding", "Generator closing", "B"),
            _pred(cid, t20, 2, "What will this Python code print?", "nums = [1, 2, 3, 4]\nres = list(filter(lambda x: x % 2 == 0, nums))\nprint(res)", "[2, 4]", ["[2,4]"]),
            _py_code(cid, t20, 3, "debug_code", "Fix `scale_positive(vals, k)` so it multiplies only values `> 0` by `k`.",
                     "def scale_positive(vals, k):\n    return [x * k for x in vals if x < 0]",
                     "def scale_positive(vals, k):\n    return [x * k for x in vals if x > 0]",
                     [{"call": "scale_positive([-1, 2, 3], 10)", "expected_return": [20, 30]},
                      {"call": "scale_positive([-5, 0], 2)", "expected_return": []}]),
            _py_code(cid, t20, 4, "write_code", "Write `pair_sum(a, b)` returning element-wise sums `[x + y for x, y in zip(a, b)]`.",
                     "def pair_sum(a, b):\n    pass",
                     "def pair_sum(a, b):\n    return [x + y for x, y in zip(a, b)]",
                     [{"call": "pair_sum([1, 2, 3], [4, 5, 6])", "expected_return": [5, 7, 9]},
                      {"call": "pair_sum([], [])", "expected_return": []}]),

            # T21 Mathematics and Statistics Basics
            _mcq(cid, t21, 5, "Which measure of center is LEAST affected by an extreme outlier in a dataset?", "Mean", "Median", "Sum", "Range", "B"),
            _pred(cid, t21, 6, "What value does this dot-product loop print?", "a = [2, 3]\nb = [4, 1]\ndot = sum(x * y for x, y in zip(a, b))\nprint(dot)", "11"),
            _py_code(cid, t21, 7, "debug_code", "Fix `calc_mean(vals)` so it divides by `len(vals)` rather than `len(vals) - 1`.",
                     "def calc_mean(vals):\n    return sum(vals) / (len(vals) - 1)",
                     "def calc_mean(vals):\n    return sum(vals) / len(vals)",
                     [{"call": "calc_mean([2, 4, 6])", "expected_return": 4.0},
                      {"call": "calc_mean([10, 20])", "expected_return": 15.0}]),
            _py_code(cid, t21, 8, "write_code", "Write `variance(vals)` computing population variance: average of `(x - mean)^2`.",
                     "def variance(vals):\n    pass",
                     "def variance(vals):\n    mu = sum(vals) / len(vals)\n    return sum((x - mu) ** 2 for x in vals) / len(vals)",
                     [{"call": "variance([2, 4, 4, 4, 5, 5, 7, 9])", "expected_return": 4.0},
                      {"call": "variance([5, 5, 5])", "expected_return": 0.0}]),

            # T22 NumPy and Pandas
            _mcq(cid, t22, 9, "In NumPy, what lets an operation like `arr + 5` apply to every element regardless of dimensions?", "Inheritance", "Broadcasting", "Serialization", "Hashing", "B"),
            _pred(cid, t22, 10, "What does this tabular filter expression print?", "rows = [{'age': 15}, {'age': 22}, {'age': 19}]\nkept = [r['age'] for r in rows if r['age'] >= 18]\nprint(len(kept))", "2"),
            _py_code(cid, t22, 11, "debug_code", "Fix `dropna_rows(rows, key)` so it keeps rows where `row.get(key) is not None`.",
                     "def dropna_rows(rows, key):\n    return [r for r in rows if r.get(key) is None]",
                     "def dropna_rows(rows, key):\n    return [r for r in rows if r.get(key) is not None]",
                     [{"call": "dropna_rows([{'a': 1}, {'a': None}, {'a': 3}], 'a')", "expected_return": [{"a": 1}, {"a": 3}]}]),
            _py_code(cid, t22, 12, "write_code", "Write `fillna_col(vals, fill)` replacing every `None` in list `vals` with `fill`.",
                     "def fillna_col(vals, fill):\n    pass",
                     "def fillna_col(vals, fill):\n    return [fill if v is None else v for v in vals]",
                     [{"call": "fillna_col([1, None, 3], 0)", "expected_return": [1, 0, 3]},
                      {"call": "fillna_col([None], 5)", "expected_return": [5]}]),

            # T23 Data Preprocessing and Visualization
            _mcq(cid, t23, 13, "Why do we apply One-Hot Encoding to nominal categorical text features?", "To remove outliers", "To avoid imposing a false numeric ordering", "To double the rows", "To compute mean", "B"),
            _pred(cid, t23, 14, "What will this Min-Max scaling step output?", "x, mn, mx = 15, 10, 20\nprint((x - mn) / (mx - mn))", "0.5"),
            _py_code(cid, t23, 15, "debug_code", "Fix `min_max_scale(vals)` so division uses `(mx - mn)` instead of `(mx + mn)`.",
                     "def min_max_scale(vals):\n    mn, mx = min(vals), max(vals)\n    return [(x - mn) / (mx + mn) for x in vals]",
                     "def min_max_scale(vals):\n    mn, mx = min(vals), max(vals)\n    if mx == mn: return [0.0 for _ in vals]\n    return [round((x - mn) / (mx - mn), 4) for x in vals]",
                     [{"call": "min_max_scale([10, 15, 20])", "expected_return": [0.0, 0.5, 1.0]},
                      {"call": "min_max_scale([0, 5, 10])", "expected_return": [0.0, 0.5, 1.0]}]),
            _py_code(cid, t23, 16, "write_code", "Write `one_hot(label, classes)` returning binary list with `1` at `classes.index(label)` and `0` elsewhere.",
                     "def one_hot(label, classes):\n    pass",
                     "def one_hot(label, classes):\n    return [1 if c == label else 0 for c in classes]",
                     [{"call": "one_hot('cat', ['dog', 'cat', 'bird'])", "expected_return": [0, 1, 0]},
                      {"call": "one_hot('red', ['red', 'blue'])", "expected_return": [1, 0]}]),

            # T24 Machine Learning Algorithms
            _mcq(cid, t24, 17, "Which algorithm groups unlabeled data into `K` clusters by nearest centroid distance?", "Linear Regression", "Logistic Regression", "K-Means Clustering", "Decision Tree Classifier", "C"),
            _pred(cid, t24, 18, "What will this threshold classifier output?", "probs = [0.2, 0.7, 0.5]\npreds = [1 if p >= 0.5 else 0 for p in probs]\nprint(preds)", "[0, 1, 1]", ["[0,1,1]"]),
            _py_code(cid, t24, 19, "debug_code", "Fix `mse_loss(y_true, y_pred)` so errors are squared rather than added linearly.",
                     "def mse_loss(y_true, y_pred):\n    return sum(t - p for t, p in zip(y_true, y_pred)) / len(y_true)",
                     "def mse_loss(y_true, y_pred):\n    return sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true)",
                     [{"call": "mse_loss([3, 5], [1, 5])", "expected_return": 2.0},
                      {"call": "mse_loss([2, 4], [2, 4])", "expected_return": 0.0}]),
            _py_code(cid, t24, 20, "write_code", "Write `linear_predict(weights, x, bias)` returning `sum(w * xi for w, xi in zip(weights, x)) + bias`.",
                     "def linear_predict(weights, x, bias):\n    pass",
                     "def linear_predict(weights, x, bias):\n    return sum(w * xi for w, xi in zip(weights, x)) + bias",
                     [{"call": "linear_predict([2, -1], [3, 4], 1)", "expected_return": 3},
                      {"call": "linear_predict([0.5, 0.5], [10, 20], 0)", "expected_return": 15.0}]),

            # T25 Model Evaluation and AI Projects
            _mcq(cid, t25, 21, "If a model gets 99% accuracy on training data but 60% on unseen test data, what happened?", "Underfitting", "Overfitting", "Convergence", "Regularization", "B"),
            _pred(cid, t25, 22, "What accuracy does this snippet output?", "y_true = [1, 0, 1, 1]\ny_pred = [1, 1, 1, 0]\nacc = sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)\nprint(acc)", "0.5"),
            _py_code(cid, t25, 23, "debug_code", "Fix `precision(tp, fp)` so denominator uses `(tp + fp)` instead of `(tp + fn)`-style wrong divisor `tp`.",
                     "def precision(tp, fp):\n    return tp / fp",
                     "def precision(tp, fp):\n    if (tp + fp) == 0: return 0.0\n    return tp / (tp + fp)",
                     [{"call": "precision(8, 2)", "expected_return": 0.8},
                      {"call": "precision(0, 5)", "expected_return": 0.0}]),
            _py_code(cid, t25, 24, "write_code", "Write `f1_score(prec, rec)` returning `2 * prec * rec / (prec + rec)` (or `0.0` if sum is 0).",
                     "def f1_score(prec, rec):\n    pass",
                     "def f1_score(prec, rec):\n    if (prec + rec) == 0: return 0.0\n    return 2 * prec * rec / (prec + rec)",
                     [{"call": "f1_score(0.8, 0.8)", "expected_return": 0.8},
                      {"call": "f1_score(0.0, 0.0)", "expected_return": 0.0},
                      {"call": "f1_score(1.0, 0.5)", "expected_return": 0.6667}]),
        ]

        # Practice Pool for AI/ML (18 questions)
        qs.extend([
            _pred(cid, t20, 101, "What will this list comprehension print?", "print([x for x in range(5) if x % 2 == 1])", "[1, 3]", ["[1,3]"], pool="practice"),
            _py_code(cid, t20, 102, "debug_code", "Fix `apply_fn(vals, fn)` so it returns transformed values in order.",
                     "def apply_fn(vals, fn):\n    return [fn for x in vals]",
                     "def apply_fn(vals, fn):\n    return [fn(x) for x in vals]",
                     [{"call": "apply_fn([1, 2, 3], lambda x: x + 1)", "expected_return": [2, 3, 4]}], pool="practice"),
            _py_code(cid, t20, 103, "write_code", "Write `normalize_labels(labels)` stripping whitespace and lowercasing each label.",
                     "def normalize_labels(labels):\n    pass",
                     "def normalize_labels(labels):\n    return [s.strip().lower() for s in labels]",
                     [{"call": "normalize_labels([' Cat ', 'DOG'])", "expected_return": ["cat", "dog"]}], pool="practice"),

            _pred(cid, t21, 104, "What gradient step output does this print?", "w, lr, grad = 5.0, 0.1, 10.0\nw = w - lr * grad\nprint(w)", "4.0", pool="practice"),
            _py_code(cid, t21, 105, "debug_code", "Fix `median(vals)` so it sorts `vals` before selecting the middle element.",
                     "def median(vals):\n    s = vals\n    return s[len(s) // 2]",
                     "def median(vals):\n    s = sorted(vals)\n    m = len(s) // 2\n    return s[m] if len(s) % 2 == 1 else (s[m-1] + s[m]) / 2",
                     [{"call": "median([9, 1, 5])", "expected_return": 5}, {"call": "median([10, 2, 4, 8])", "expected_return": 6.0}], pool="practice"),
            _py_code(cid, t21, 106, "write_code", "Write `l1_distance(a, b)` returning `sum(abs(x - y) for x, y in zip(a, b))`.",
                     "def l1_distance(a, b):\n    pass",
                     "def l1_distance(a, b):\n    return sum(abs(x - y) for x, y in zip(a, b))",
                     [{"call": "l1_distance([1, 5], [4, 1])", "expected_return": 7}], pool="practice"),

            _pred(cid, t22, 107, "What does this vector broadcast check print?", "vec = [1, 2, 3]\nb = [v + 10 for v in vec]\nprint(sum(b))", "36", pool="practice"),
            _py_code(cid, t22, 108, "debug_code", "Fix `mean_impute(vals)` so missing `None` values are replaced by the mean of non-None values.",
                     "def mean_impute(vals):\n    valid = [v for v in vals if v is not None]\n    return [0 if v is None else v for v in vals]",
                     "def mean_impute(vals):\n    valid = [v for v in vals if v is not None]\n    mu = sum(valid) / len(valid)\n    return [mu if v is None else v for v in vals]",
                     [{"call": "mean_impute([2, None, 6])", "expected_return": [2, 4.0, 6]}], pool="practice"),
            _py_code(cid, t22, 109, "write_code", "Write `select_columns(rows, cols)` returning list of dicts keeping only keys in `cols`.",
                     "def select_columns(rows, cols):\n    pass",
                     "def select_columns(rows, cols):\n    return [{c: r[c] for c in cols if c in r} for r in rows]",
                     [{"call": "select_columns([{'a': 1, 'b': 2}], ['a'])", "expected_return": [{"a": 1}]}], pool="practice"),

            _pred(cid, t23, 110, "What does this standardization check print?", "x, mean, std = 15, 10, 5\nprint((x - mean) / std)", "1.0", pool="practice"),
            _py_code(cid, t23, 111, "debug_code", "Fix `clip_outliers(vals, hi)` so values above `hi` are capped at `hi`.",
                     "def clip_outliers(vals, hi):\n    return [x for x in vals if x <= hi]",
                     "def clip_outliers(vals, hi):\n    return [min(x, hi) for x in vals]",
                     [{"call": "clip_outliers([1, 5, 99], 10)", "expected_return": [1, 5, 10]}], pool="practice"),
            _py_code(cid, t23, 112, "write_code", "Write `z_score(vals)` computing `(x - mean) / std` with population std.",
                     "def z_score(vals):\n    pass",
                     "def z_score(vals):\n    mu = sum(vals) / len(vals)\n    std = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5\n    return [round((x - mu) / std, 3) for x in vals]",
                     [{"call": "z_score([10, 20])", "expected_return": [-1.0, 1.0]}], pool="practice"),

            _pred(cid, t24, 113, "What does this nearest-1D-centroid check print?", "c1, c2, x = 2, 10, 8\nprint('C1' if abs(x-c1) < abs(x-c2) else 'C2')", "C2", pool="practice"),
            _py_code(cid, t24, 114, "debug_code", "Fix `majority_vote(labels)` so it picks the most frequent label.",
                     "def majority_vote(labels):\n    return min(set(labels), key=labels.count)",
                     "def majority_vote(labels):\n    return max(set(labels), key=labels.count)",
                     [{"call": "majority_vote(['A', 'B', 'A'])", "expected_return": "A"}], pool="practice"),
            _py_code(cid, t24, 115, "write_code", "Write `mae_loss(y_true, y_pred)` computing Mean Absolute Error.",
                     "def mae_loss(y_true, y_pred):\n    pass",
                     "def mae_loss(y_true, y_pred):\n    return sum(abs(t - p) for t, p in zip(y_true, y_pred)) / len(y_true)",
                     [{"call": "mae_loss([10, 20], [12, 16])", "expected_return": 3.0}], pool="practice"),

            _pred(cid, t25, 116, "What confusion-matrix metric prints here?", "tp, fn = 9, 1\nprint(tp / (tp + fn))", "0.9", pool="practice"),
            _py_code(cid, t25, 117, "debug_code", "Fix `recall(tp, fn)` so denominator is `(tp + fn)`.",
                     "def recall(tp, fn):\n    return tp / (tp + 0)",
                     "def recall(tp, fn):\n    return tp / (tp + fn) if (tp + fn) else 0.0",
                     [{"call": "recall(6, 4)", "expected_return": 0.6}], pool="practice"),
            _py_code(cid, t25, 118, "write_code", "Write `confusion_counts(y_true, y_pred)` returning `{'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn}` for binary 0/1 lists.",
                     "def confusion_counts(y_true, y_pred):\n    pass",
                     "def confusion_counts(y_true, y_pred):\n    c = {'tp': 0, 'fp': 0, 'fn': 0, 'tn': 0}\n    for t, p in zip(y_true, y_pred):\n        if t == 1 and p == 1: c['tp'] += 1\n        elif t == 0 and p == 1: c['fp'] += 1\n        elif t == 1 and p == 0: c['fn'] += 1\n        else: c['tn'] += 1\n    return c",
                     [{"call": "confusion_counts([1, 0, 1, 0], [1, 1, 0, 0])", "expected_return": {"tp": 1, "fp": 1, "fn": 1, "tn": 1}}], pool="practice"),
            _mcq(cid, t20, 119, "Which built-in Python function pairs elements from two lists position by position?", "map()", "zip()", "slice()", "split()", "B", pool="practice", explanation="`zip(a, b)` yields `(a[i], b[i])` pairs up to the shorter iterable."),
            _mcq(cid, t21, 120, "Which statistic represents the middle value of a sorted numeric dataset?", "Mean", "Variance", "Median", "Mode", "C", pool="practice", explanation="The median is the middle value (or average of two middle values) when sorted."),
            _mcq(cid, t22, 121, "In Pandas, what data structure represents a 2D labeled table with rows and columns?", "Series", "DataFrame", "ndarray", "Dictionary", "B", pool="practice", explanation="A Pandas `DataFrame` stores 2D tabular data with labeled columns and rows."),
            _mcq(cid, t23, 122, "Why should a feature scaler be fit only on training data before transforming test data?", "To increase overfitting", "To prevent data leakage", "To delete null labels", "To sort categories alphabetically", "B", pool="practice", explanation="Fitting on test data leaks test distribution statistics into training."),
            _mcq(cid, t24, 123, "What problem occurs when a model memorizes training noise and performs poorly on unseen test data?", "Underfitting", "Overfitting", "Convergence", "Stratification", "B", pool="practice", explanation="Overfitting means fitting training noise rather than generalizable patterns."),
            _mcq(cid, t25, 124, "Which metric measures the proportion of predicted positives that are truly positive?", "Recall", "Precision", "Specificity", "MAE", "B", pool="practice", explanation="Precision = TP / (TP + FP), the accuracy of positive predictions."),
        ])
        return qs

    add_course("Python Programming", python_bank)
    add_course("Data Structures", ds_bank)
    add_course("Java Programming", java_bank)
    add_course("Artificial Intelligence and Machine Learning (AI/ML)", aiml_bank)
    rows.extend(build_mastery_question_sets(courses_map, topic_map))
    return rows


def build_mastery_question_sets(courses_map, topic_map):
    """
    Constructs TWO fresh 5-question Mastery Assessment sets ('mastery_1' and 'mastery_2')
    for all 25 topics across all 4 courses (25 x 5 x 2 = 250 questions).
    Each set contains 1 MCQ, 1 Predict Output, 1 Find & Correct Error, 1 Fill in the Blanks,
    and 1 Write a Small Code Snippet, with weak concept & subtopic tags for revision.
    """
    rows = []

    def add_pair(cname, tname, q1, q2):
        cid = courses_map.get(cname)
        if not cid:
            return
        tid = topic_map.get((cid, tname))
        if not tid:
            return
        for idx, item in enumerate(q1, start=1):
            rows.append(item(cid, tid, 200 + idx, "mastery_1"))
        for idx, item in enumerate(q2, start=1):
            rows.append(item(cid, tid, 300 + idx, "mastery_2"))

    # Helper closures so each question builds cleanly per pool
    def mcq(txt, a, b, c, d, ans, exp, wc, sub):
        return lambda cid, tid, n, p: _mcq(cid, tid, n, txt, a, b, c, d, ans, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    def pred(txt, snip, exp_out, exp, wc, sub, var=None):
        return lambda cid, tid, n, p: _pred(cid, tid, n, txt, snip, exp_out, variants=var, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    def dbg(txt, start, sol, cases, exp, wc, sub):
        return lambda cid, tid, n, p: _py_code(cid, tid, n, "debug_code", txt, start, sol, cases, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    def fill(txt, snip, exp_b, exp, wc, sub, var=None):
        return lambda cid, tid, n, p: _fill(cid, tid, n, txt, snip, exp_b, variants=var, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    def wrt(txt, start, sol, cases, exp, wc, sub, rec=False):
        return lambda cid, tid, n, p: _py_code(cid, tid, n, "write_code", txt, start, sol, cases, pool=p, explanation=exp, require_recursion=rec, weak_concept=wc, recommended_subtopic=sub)

    def jdbg(txt, start, sol, spec_extra, exp, wc, sub):
        return lambda cid, tid, n, p: _java_code(cid, tid, n, "debug_code", txt, start, sol, spec_extra, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    def jwrt(txt, start, sol, spec_extra, exp, wc, sub):
        return lambda cid, tid, n, p: _java_code(cid, tid, n, "write_code", txt, start, sol, spec_extra, pool=p, explanation=exp, weak_concept=wc, recommended_subtopic=sub)

    # =====================================================================
    # COURSE 1: PYTHON PROGRAMMING (6 Topics)
    # =====================================================================
    py = "Python Programming"

    add_pair(py, "Variables and Data Types",
        [
            mcq("Which variable name is invalid in Python?", "user_1", "_total", "3rd_rank", "score2", "C",
                "Variable names cannot begin with a numeric digit.", "Identifier Naming Rules", "Variable Declaration and Naming Rules"),
            pred("What output does this Python code display?", "val = 9 / 3\nflag = bool(0)\nprint(type(val).__name__, flag)", "float False",
                 "True division (/) always yields a float (3.0), and bool(0) evaluates to False.", "Numeric Division & Booleans", "Integers and Floating-Point Numbers"),
            dbg("Find and fix the error so `bill_total(qty, price_str)` returns the numeric total as a float.",
                "def bill_total(qty, price_str):\n    return qty * price_str",
                "def bill_total(qty, price_str):\n    return qty * float(price_str)",
                [{"call": "bill_total(3, '4.5')", "expected_return": 13.5}, {"call": "bill_total(2, '10')", "expected_return": 20.0}],
                "Convert price_str with float(price_str) before multiplying, otherwise qty * str repeats text.", "Type Conversion Error", "Type Conversion"),
            fill("Fill in the blank `___` to convert `raw_age` into an integer before adding 5.",
                 "raw_age = '19'\nnext_age = ___(raw_age) + 5", "int",
                 "Use int(raw_age) to convert string digits to an integer.", "Explicit Casting", "Type Conversion"),
            wrt("Write `format_tag(item, count)` returning f-string `\"<item>: <count>\"` using explicit types safely.",
                "def format_tag(item, count):\n    pass",
                "def format_tag(item, count):\n    return f\"{str(item)}: {int(count)}\"",
                [{"call": "format_tag('Book', '4')", "expected_return": "Book: 4"}, {"call": "format_tag('Pen', 2)", "expected_return": "Pen: 2"}],
                "Convert inputs cleanly and format with an f-string.", "String & Number Formatting", "Working with Variables and Data Types"),
        ],
        [
            mcq("What happens when Python runs `x = 12; x = 'done'`?", "TypeError is raised", "x now holds a str", "x stays an int", "Syntax error", "B",
                "Python variables rebind dynamically to any object type.", "Dynamic Rebinding", "Introduction to Variables"),
            pred("Predict the exact printed output.", "p = '7'\nq = 3\nprint(int(p) + q, p * 2)", "10 77",
                 "int('7') + 3 = 10, while '7' * 2 repeats the string as '77'.", "Strings vs Numbers", "Strings and Boolean Values"),
            dbg("Fix the bug in `is_positive_whole(s)` so it returns True when integer `int(s) > 0`.",
                "def is_positive_whole(s):\n    return s > 0",
                "def is_positive_whole(s):\n    return int(s) > 0",
                [{"call": "is_positive_whole('8')", "expected_return": True}, {"call": "is_positive_whole('-2')", "expected_return": False}],
                "String cannot be compared directly to 0 without int(s).", "Type Comparison Bug", "Working with Variables and Data Types"),
            fill("Fill in the blank `___` so `ratio` stores a float value explicitly.",
                 "ratio = ___(\"0.75\")", "float",
                 "float('0.75') parses decimal text into a float.", "Float Conversion", "Integers and Floating-Point Numbers"),
            wrt("Write `calc_remainder_text(a, b)` returning string `\"rem=X\"` where X is `int(a) % int(b)`.",
                "def calc_remainder_text(a, b):\n    pass",
                "def calc_remainder_text(a, b):\n    return f\"rem={int(a) % int(b)}\"",
                [{"call": "calc_remainder_text('11', '4')", "expected_return": "rem=3"}, {"call": "calc_remainder_text(10, 5)", "expected_return": "rem=0"}],
                "Cast operands with int() and embed the remainder inside an f-string.", "Type Casting & Arithmetic", "Working with Variables and Data Types"),
        ]
    )

    add_pair(py, "Operators and Conditions",
        [
            mcq("What is the value of `19 // 5` in Python?", "3.8", "3", "4", "1", "B",
                "// returns the integer floor quotient of division.", "Floor Division", "Arithmetic and Assignment Operators"),
            pred("Predict the printed output.", "a, b = 4, 9\nif a * 2 > b or b % 3 == 0:\n    print('Accept')\nelse:\n    print('Reject')", "Accept",
                 "4 * 2 > 9 is False, but 9 % 3 == 0 is True, so 'or' evaluates True.", "Logical OR Evaluation", "Comparison and Logical Operators"),
            dbg("Fix the assignment/equality bug in `check_pin(entered, actual)`.",
                "def check_pin(entered, actual):\n    if entered = actual:\n        return 'Unlocked'\n    return 'Denied'",
                "def check_pin(entered, actual):\n    if entered == actual:\n        return 'Unlocked'\n    return 'Denied'",
                [{"call": "check_pin(1234, 1234)", "expected_return": "Unlocked"}, {"call": "check_pin(1111, 1234)", "expected_return": "Denied"}],
                "Equality comparison uses ==, not single =.", "Equality vs Assignment", "Comparison and Logical Operators"),
            fill("Fill in the blank `___` to check if `x` is strictly between 10 and 20 using logical AND.",
                 "valid = x > 10 ___ x < 20", "and",
                 "Python combines boolean conditions with the lowercase keyword 'and'.", "Logical Conjunction", "Comparison and Logical Operators"),
            wrt("Write `classify_temp(c)` returning `'Freezing'` if `c <= 0`, `'Warm'` if `c < 30`, else `'Hot'`.",
                "def classify_temp(c):\n    pass",
                "def classify_temp(c):\n    if c <= 0:\n        return 'Freezing'\n    elif c < 30:\n        return 'Warm'\n    return 'Hot'",
                [{"call": "classify_temp(-2)", "expected_return": "Freezing"}, {"call": "classify_temp(22)", "expected_return": "Warm"}, {"call": "classify_temp(35)", "expected_return": "Hot"}],
                "Check <= 0 first, then elif < 30, else 'Hot'.", "Multi-Branch Conditionals", "Conditional Branching with if, elif, and else"),
        ],
        [
            mcq("Which expression checks if `n` is divisible by both 2 and 3?", "n % 2 == 0 or n % 3 == 0", "n % 6 == 0", "n // 6 == 0", "n != 6", "B",
                "A number divisible by both 2 and 3 has remainder 0 when divided by 6.", "Modulo Conditions", "Arithmetic and Assignment Operators"),
            pred("What will this snippet print?", "score = 65\nlabel = 'High' if score >= 80 else ('Pass' if score >= 50 else 'Fail')\nprint(label)", "Pass",
                 "65 >= 80 is False, and 65 >= 50 is True, giving 'Pass'.", "Conditional Expressions", "Nested Conditions and Conditional Expressions"),
            dbg("Fix `ticket_price(age, is_student)` so anyone under 12 OR a student pays 50, else 100.",
                "def ticket_price(age, is_student):\n    if age < 12 and is_student:\n        return 50\n    return 100",
                "def ticket_price(age, is_student):\n    if age < 12 or is_student:\n        return 50\n    return 100",
                [{"call": "ticket_price(10, False)", "expected_return": 50}, {"call": "ticket_price(22, True)", "expected_return": 50}, {"call": "ticket_price(30, False)", "expected_return": 100}],
                "Use 'or' instead of 'and' when either condition qualifies.", "Logical AND vs OR", "Comparison and Logical Operators"),
            fill("Fill in the blank `___` for the fallback branch when previous conditions fail.",
                 "if n > 0:\n    sign = 'pos'\n___:\n    sign = 'non-pos'", "else",
                 "'else' captures remaining cases when if/elif conditions are False.", "Branch Fallback", "Conditional Branching with if, elif, and else"),
            wrt("Write `leap_div4(year)` returning True if `year % 4 == 0` and `(year % 100 != 0 or year % 400 == 0)`.",
                "def leap_div4(year):\n    pass",
                "def leap_div4(year):\n    return (year % 4 == 0) and (year % 100 != 0 or year % 400 == 0)",
                [{"call": "leap_div4(2024)", "expected_return": True}, {"call": "leap_div4(1900)", "expected_return": False}, {"call": "leap_div4(2000)", "expected_return": True}],
                "Combine divisibility rules with 'and' / 'or' parentheses.", "Compound Boolean Logic", "Comparison and Logical Operators"),
        ]
    )

    add_pair(py, "Loops",
        [
            mcq("Which sequence does `list(range(2, 9, 3))` produce?", "[2, 5, 8]", "[2, 5, 8, 9]", "[3, 6, 9]", "[2, 3, 9]", "A",
                "range(2, 9, 3) starts at 2, steps by 3, and stops before 9.", "Range Stepped Indexing", "The for Loop and range()"),
            pred("What will this loop print?", "res = 0\nfor k in [2, 4, -1, 5]:\n    if k < 0:\n        break\n    res += k\nprint(res)", "6",
                 "Adds 2 and 4 (= 6), then exits immediately upon hitting -1.", "Loop Break Behavior", "Controlling Flow with break and continue"),
            dbg("Fix the accumulator bug in `product_list(nums)` so it multiplies all items properly.",
                "def product_list(nums):\n    prod = 0\n    for x in nums:\n        prod *= x\n    return prod",
                "def product_list(nums):\n    prod = 1\n    for x in nums:\n        prod *= x\n    return prod",
                [{"call": "product_list([2, 3, 4])", "expected_return": 24}, {"call": "product_list([5])", "expected_return": 5}],
                "Multiplicative accumulators must start at 1, not 0.", "Accumulator Initialization", "Accumulators and Nested Loops"),
            fill("Fill in the blank `___` to skip odd numbers without exiting the loop.",
                 "for n in range(1, 6):\n    if n % 2 == 1:\n        ___\n    print(n)", "continue",
                 "'continue' moves immediately to the next iteration.", "Loop Iteration Skipping", "Controlling Flow with break and continue"),
            wrt("Write `count_above(nums, limit)` returning how many values in `nums` are strictly greater than `limit`.",
                "def count_above(nums, limit):\n    pass",
                "def count_above(nums, limit):\n    c = 0\n    for x in nums:\n        if x > limit:\n            c += 1\n    return c",
                [{"call": "count_above([3, 8, 1, 9], 5)", "expected_return": 2}, {"call": "count_above([1, 2], 10)", "expected_return": 0}],
                "Loop over elements and increment a counter whenever x > limit.", "Counting Loop Pattern", "Accumulators and Nested Loops"),
        ],
        [
            mcq("What risk arises in a `while` loop if the loop variable never updates?", "SyntaxError", "Infinite loop", "TypeError", "Auto break after 10 steps", "B",
                "If the condition remains True forever, the loop runs infinitely.", "While Termination", "Condition-Controlled while Loops"),
            pred("Predict the printed output.", "s = 0\nfor x in range(1, 6):\n    if x == 3:\n        continue\n    s += x\nprint(s)", "12",
                 "1 + 2 + 4 + 5 = 12 (3 is skipped by continue).", "Continue Filtering", "Controlling Flow with break and continue"),
            dbg("Fix `while_sum_to(n)` so it sums integers `1` through `n` inclusive without infinite looping.",
                "def while_sum_to(n):\n    total, i = 0, 1\n    while i < n:\n        total += i\n    return total",
                "def while_sum_to(n):\n    total, i = 0, 1\n    while i <= n:\n        total += i\n        i += 1\n    return total",
                [{"call": "while_sum_to(4)", "expected_return": 10}, {"call": "while_sum_to(1)", "expected_return": 1}],
                "Use i <= n for inclusive upper bound and increment i += 1 each step.", "While Loop Bounds & Step", "Condition-Controlled while Loops"),
            fill("Fill in the blank `___` to loop backwards from 5 down to 1.",
                 "for i in range(5, 0, ___):\n    print(i)", "-1",
                 "Step -1 counts downward from 5 stopping before 0.", "Reverse Range Step", "The for Loop and range()"),
            wrt("Write `sum_multiples_3(n)` returning the sum of integers from 1 to `n` inclusive that are divisible by 3.",
                "def sum_multiples_3(n):\n    pass",
                "def sum_multiples_3(n):\n    total = 0\n    for i in range(1, n + 1):\n        if i % 3 == 0:\n            total += i\n    return total",
                [{"call": "sum_multiples_3(9)", "expected_return": 18}, {"call": "sum_multiples_3(2)", "expected_return": 0}],
                "Use range(1, n + 1) and add i when i % 3 == 0.", "Filtered Accumulation", "Accumulators and Nested Loops"),
        ]
    )

    add_pair(py, "Functions",
        [
            mcq("What happens when a function reaches its end without executing a `return` statement?", "Raises ValueError", "Returns 0", "Returns None", "Returns empty string", "C",
                "Every Python function implicitly returns None if no return runs.", "Default Return Behavior", "Parameters vs Arguments and Return Values"),
            pred("What output does this function snippet print?", "def bump(x, step=3):\n    return x + step\nprint(bump(7), bump(7, 5))", "10 12",
                 "First call uses default step=3 (10); second passes step=5 (12).", "Default Parameters", "Default and Keyword Arguments"),
            dbg("Fix `double_sum(a, b)` so calling code receives the integer result instead of `None`.",
                "def double_sum(a, b):\n    ans = (a + b) * 2\n    print(ans)",
                "def double_sum(a, b):\n    return (a + b) * 2",
                [{"call": "double_sum(3, 4)", "expected_return": 14}, {"call": "double_sum(0, 5)", "expected_return": 10}],
                "Use 'return' instead of 'print' so callers receive the computed value.", "Print vs Return", "Parameters vs Arguments and Return Values"),
            fill("Fill in the blank `___` to give parameter `rate` a default value of 0.1.",
                 "def calc_tax(amount, rate___0.1):\n    return amount * rate", "=",
                 "Default parameter syntax uses parameter=default_value.", "Default Argument Syntax", "Default and Keyword Arguments"),
            wrt("Write `clamp_num(x, lo=0, hi=100)` returning `lo` if `x < lo`, `hi` if `x > hi`, else `x`.",
                "def clamp_num(x, lo=0, hi=100):\n    pass",
                "def clamp_num(x, lo=0, hi=100):\n    if x < lo: return lo\n    if x > hi: return hi\n    return x",
                [{"call": "clamp_num(120)", "expected_return": 100}, {"call": "clamp_num(-5)", "expected_return": 0}, {"call": "clamp_num(42, 10, 50)", "expected_return": 42}],
                "Use default bounds lo=0, hi=100 and return bounded value.", "Bounded Function & Defaults", "Default and Keyword Arguments"),
        ],
        [
            mcq("Inside `def f(a): b = a + 1`, where can local variable `b` be read?", "Anywhere in the file", "Only inside function f", "In all imported modules", "In global scope only", "B",
                "Variables assigned inside a function belong to its local scope.", "Local Scope", "Local vs Global Variable Scope"),
            pred("What will this code print?", "def make_tag(code, prefix='ID'):\n    return f'{prefix}-{code}'\nprint(make_tag(42, prefix='USR'))", "USR-42",
                 "Keyword argument prefix='USR' overrides default 'ID'.", "Keyword Arguments", "Default and Keyword Arguments"),
            dbg("Fix `safe_ratio(a, b)` so dividing by zero returns `0.0` cleanly.",
                "def safe_ratio(a, b):\n    return a / b",
                "def safe_ratio(a, b):\n    if b == 0:\n        return 0.0\n    return a / b",
                [{"call": "safe_ratio(9, 3)", "expected_return": 3.0}, {"call": "safe_ratio(5, 0)", "expected_return": 0.0}],
                "Guard b == 0 with an early return before division.", "Early Return Guard", "Parameters vs Arguments and Return Values"),
            fill("Fill in the blank `___` keyword to define a Python function.",
                 "___ cube(n):\n    return n ** 3", "def",
                 "Python functions are declared using the 'def' keyword.", "Function Declaration", "Defining and Calling Functions"),
            wrt("Write `min_max_diff(nums)` returning `max(nums) - min(nums)` if `nums` is non-empty, else `0`.",
                "def min_max_diff(nums):\n    pass",
                "def min_max_diff(nums):\n    if not nums:\n        return 0\n    return max(nums) - min(nums)",
                [{"call": "min_max_diff([3, 10, 5])", "expected_return": 7}, {"call": "min_max_diff([])", "expected_return": 0}],
                "Return 0 early when nums is empty; otherwise max - min.", "Function Edge Case & Return", "Parameters vs Arguments and Return Values"),
        ]
    )

    add_pair(py, "Lists, Tuples, Sets, and Dictionaries",
        [
            mcq("Which expression creates an empty `set` in Python?", "{}", "set()", "[]", "()", "B",
                "{} creates an empty dict; set() creates an empty set.", "Set Instantiation", "Sets and Uniqueness"),
            pred("What output does this collection snippet print?", "nums = [10, 20, 30, 40]\npart = nums[1:3]\nprint(part, len(set([5, 5, 8])))", "[20, 30] 2",
                 "Slice [1:3] takes indices 1 and 2; set deduplicates [5, 5, 8] to 2 items.", "Slicing & Set Uniqueness", "Slicing Sequences and Immutable Tuples", ["[20,30] 2"]),
            dbg("Fix `add_points(board, player, pts)` so missing players don't raise `KeyError`.",
                "def add_points(board, player, pts):\n    board[player] = board[player] + pts\n    return board",
                "def add_points(board, player, pts):\n    board[player] = board.get(player, 0) + pts\n    return board",
                [{"call": "add_points({'amy': 10}, 'amy', 5)", "expected_return": {"amy": 15}}, {"call": "add_points({}, 'sam', 7)", "expected_return": {"sam": 7}}],
                "Use board.get(player, 0) to safely default missing keys.", "Safe Dictionary Lookup", "Dictionaries and Key-Value Mapping"),
            fill("Fill in the blank `___` method to safely read key `'role'` with fallback `'guest'`.",
                 "role = user.___('role', 'guest')", "get",
                 "dict.get(key, default) prevents KeyError when key is absent.", "Dict get Method", "Dictionaries and Key-Value Mapping"),
            wrt("Write `unique_sorted(nums)` returning a sorted list of distinct elements in `nums`.",
                "def unique_sorted(nums):\n    pass",
                "def unique_sorted(nums):\n    return sorted(set(nums))",
                [{"call": "unique_sorted([4, 2, 4, 1, 2])", "expected_return": [1, 2, 4]}, {"call": "unique_sorted([])", "expected_return": []}],
                "Use set() to deduplicate and sorted() to return a sorted list.", "Deduplicating with Sets", "Sets and Uniqueness"),
        ],
        [
            mcq("Which Python collection is immutable once created?", "list", "dict", "set", "tuple", "D",
                "Tuples cannot have items added, removed, or reassigned.", "Collection Mutability", "Slicing Sequences and Immutable Tuples"),
            pred("What does this code output?", "data = {'x': 2, 'y': 9}\nprint(data.get('z', 5), data['x'])", "5 2",
                 "data.get('z', 5) returns fallback 5; data['x'] returns 2.", "Dictionary Lookup", "Dictionaries and Key-Value Mapping"),
            dbg("Fix `last_two(items)` so it returns a slice of the final 2 elements without altering `items`.",
                "def last_two(items):\n    return items[:-2]",
                "def last_two(items):\n    return items[-2:]",
                [{"call": "last_two([1, 2, 3, 4])", "expected_return": [3, 4]}, {"call": "last_two([7])", "expected_return": [7]}],
                "items[-2:] slices the last two items; items[:-2] drops them.", "Negative Slicing", "Slicing Sequences and Immutable Tuples"),
            fill("Fill in the blank `___` to append value `99` to list `arr` in place.",
                 "arr.___(99)", "append",
                 "list.append(item) adds an item to the end of a list.", "List Append", "Lists and Common List Operations"),
            wrt("Write `invert_lookup(d)` taking a dict `{k: v}` (unique values) and returning `{v: k}`.",
                "def invert_lookup(d):\n    pass",
                "def invert_lookup(d):\n    return {v: k for k, v in d.items()}",
                [{"call": "invert_lookup({'a': 'one', 'b': 'two'})", "expected_return": {"one": "a", "two": "b"}}, {"call": "invert_lookup({})", "expected_return": {}}],
                "Iterate over d.items() and build reversed pairs.", "Dictionary Comprehension & Iteration", "Dictionaries and Key-Value Mapping"),
        ]
    )

    add_pair(py, "Object-Oriented Programming",
        [
            mcq("Which special method initializes attributes when a Python class instance is created?", "__str__", "__init__", "__new__", "__self__", "B",
                "Python invokes __init__(self, ...) immediately after object allocation.", "Constructor Method", "Classes, Objects, and __init__"),
            pred("Predict the printed output.", "class Meter:\n    def __init__(self, v=1):\n        self.v = v\n    def step(self):\n        self.v *= 2\n        return self.v\nm = Meter(3)\nprint(m.step())", "6",
                 "Initial self.v = 3; m.step() doubles it to 6 and returns 6.", "Instance State Mutation", "Instance Methods and Object State"),
            dbg("Fix `ScoreBox.add_points` so points actually save onto the instance.",
                "class ScoreBox:\n    def __init__(self):\n        self.total = 0\n    def add_points(self, pts):\n        total = self.total + pts\n        return self.total",
                "class ScoreBox:\n    def __init__(self):\n        self.total = 0\n    def add_points(self, pts):\n        self.total += pts\n        return self.total",
                [{"call": "ScoreBox().add_points(20)", "expected_return": 20}],
                "Assign to self.total, not a local variable 'total'.", "Missing self Attribute Reference", "Classes, Objects, and __init__"),
            fill("Fill in the blank `___` to invoke the parent class method inside a subclass.",
                 "class Child(Base):\n    def label(self):\n        return ___().label() + '!'", "super",
                 "super() accesses methods defined on the superclass.", "Using super()", "Inheritance, super(), and Method Overriding"),
            wrt("Write class `Rectangle` with `__init__(self, w, h)` and `area(self)` returning `w * h`.",
                "class Rectangle:\n    def __init__(self, w, h):\n        pass\n    def area(self):\n        pass",
                "class Rectangle:\n    def __init__(self, w, h):\n        self.w = w\n        self.h = h\n    def area(self):\n        return self.w * self.h",
                [{"call": "Rectangle(4, 5).area()", "expected_return": 20}, {"call": "Rectangle(3, 7).area()", "expected_return": 21}],
                "Store self.w and self.h in __init__, return self.w * self.h in area().", "Class Design & Instance Methods", "Classes, Objects, and __init__"),
        ],
        [
            mcq("What must be the first parameter of any regular instance method in Python?", "cls", "this", "self", "obj", "C",
                "By convention and Python dispatch, self receives the object instance.", "Instance Method Signature", "Instance Methods and Object State"),
            pred("What will this inheritance code print?", "class Base:\n    def tag(self): return 'Base'\nclass Sub(Base):\n    def tag(self): return 'Sub'\nprint(Sub().tag())", "Sub",
                 "Sub overrides tag(), so Sub().tag() executes Sub's implementation.", "Method Overriding", "Inheritance, super(), and Method Overriding"),
            dbg("Fix `Book.__init__` so both `self.title` and `self.pages` are saved.",
                "class Book:\n    def __init__(self, title, pages):\n        title = title\n        self.pages = pages\n    def info(self):\n        return f'{self.title}:{self.pages}'",
                "class Book:\n    def __init__(self, title, pages):\n        self.title = title\n        self.pages = pages\n    def info(self):\n        return f'{self.title}:{self.pages}'",
                [{"call": "Book('Py', 120).info()", "expected_return": "Py:120"}],
                "Prefix attributes with self. inside __init__.", "Constructor Attribute Binding", "Classes, Objects, and __init__"),
            fill("Fill in the blank `___` to make `Truck` inherit from class `Vehicle`.",
                 "class Truck(___):\n    pass", "Vehicle",
                 "Place parent class inside parentheses after subclass name.", "Subclass Declaration", "Inheritance, super(), and Method Overriding"),
            wrt("Write class `StepCounter` with `__init__(self, start=0)` and `advance(self, n=1)` adding `n` and returning `self.count`.",
                "class StepCounter:\n    def __init__(self, start=0):\n        pass\n    def advance(self, n=1):\n        pass",
                "class StepCounter:\n    def __init__(self, start=0):\n        self.count = start\n    def advance(self, n=1):\n        self.count += n\n        return self.count",
                [{"call": "StepCounter().advance(3)", "expected_return": 3}, {"call": "StepCounter(10).advance()", "expected_return": 11}],
                "Initialize self.count = start and increment by n.", "Stateful Object Methods", "Instance Methods and Object State"),
        ]
    )

    # =====================================================================
    # COURSE 2: DATA STRUCTURES (7 Topics)
    # =====================================================================
    ds = "Data Structures"

    add_pair(ds, "Programming Basics",
        [
            mcq("What is the time complexity of looking up `arr[k]` by integer index in an array?", "O(n)", "O(log n)", "O(1)", "O(n^2)", "C",
                "Contiguous indexing computes memory offset directly in O(1) time.", "Array Access Complexity", "Algorithmic Thinking and Big-O Notation"),
            pred("What will this loop complexity trace output?", "c = 0\nfor i in range(4):\n    for j in range(i):\n        c += 1\nprint(c)", "6",
                 "Inner loop executes 0 + 1 + 2 + 3 = 6 times.", "Nested Loop Counting", "Algorithmic Thinking and Big-O Notation"),
            dbg("Fix `has_duplicate(nums)` so it runs in O(n) using a set correctly.",
                "def has_duplicate(nums):\n    seen = set()\n    for x in nums:\n        seen.add(x)\n        if x in seen:\n            return True\n    return False",
                "def has_duplicate(nums):\n    seen = set()\n    for x in nums:\n        if x in seen:\n            return True\n        seen.add(x)\n    return False",
                [{"call": "has_duplicate([1, 2, 3])", "expected_return": False}, {"call": "has_duplicate([4, 1, 4])", "expected_return": True}],
                "Check 'if x in seen' BEFORE calling seen.add(x).", "Set Membership Order", "Matching Problems to Data Structures"),
            fill("Fill in the blank `___` to return the time complexity of binary search on a sorted array.",
                 "# Binary search discards half each step: O(___)", "log n",
                 "Halving input size each step gives logarithmic O(log n) time.", "Logarithmic Complexity", "Algorithmic Thinking and Big-O Notation", ["logn", "log(n)"]),
            wrt("Write `second_largest(nums)` returning the second largest distinct integer in `nums` (assume >= 2 distinct).",
                "def second_largest(nums):\n    pass",
                "def second_largest(nums):\n    vals = sorted(set(nums))\n    return vals[-2]",
                [{"call": "second_largest([10, 5, 20, 20])", "expected_return": 10}, {"call": "second_largest([1, 2])", "expected_return": 1}],
                "Deduplicate distinct values and return the second largest element.", "Single-Pass Selection", "Swapping and Single-Pass Linear Scans"),
        ],
        [
            mcq("Which expression simplifies to O(n) in Big-O analysis?", "5*n + 100", "n*n + n", "2^n", "n * log(n)", "A",
                "Drop constant multipliers and lower-order terms: 5n + 100 simplifies to O(n).", "Big-O Simplification", "Algorithmic Thinking and Big-O Notation"),
            pred("Predict the output after tuple swap.", "x, y = 12, 5\nif x > y:\n    x, y = y, x\nprint(x, y)", "5 12",
                 "Since 12 > 5, swap makes x = 5 and y = 12.", "Tuple Swapping", "Swapping and Single-Pass Linear Scans"),
            dbg("Fix `range_sum(nums)` so it returns `max - min` in one pass instead of zero.",
                "def range_sum(nums):\n    lo = hi = nums[0]\n    for x in nums:\n        if x > lo: lo = x\n        if x < hi: hi = x\n    return hi - lo",
                "def range_sum(nums):\n    lo = hi = nums[0]\n    for x in nums:\n        if x < lo: lo = x\n        if x > hi: hi = x\n    return hi - lo",
                [{"call": "range_sum([4, 9, 2, 7])", "expected_return": 7}, {"call": "range_sum([5, 5])", "expected_return": 0}],
                "Update lo when x < lo and hi when x > hi.", "Min/Max Tracker Bug", "Swapping and Single-Pass Linear Scans"),
            fill("Fill in the blank `___` to swap `arr[i]` and `arr[j]` in one statement.",
                 "arr[i], arr[j] = ___", "arr[j], arr[i]",
                 "Python tuple unpacking swaps two positions without a temp variable.", "In-Place Array Swap", "Swapping and Single-Pass Linear Scans"),
            wrt("Write `pair_diff_exists(nums, diff)` returning True if any two elements have `abs(a - b) == diff` and distinct indices.",
                "def pair_diff_exists(nums, diff):\n    pass",
                "def pair_diff_exists(nums, diff):\n    for i in range(len(nums)):\n        for j in range(i + 1, len(nums)):\n            if abs(nums[i] - nums[j]) == diff:\n                return True\n    return False",
                [{"call": "pair_diff_exists([1, 5, 9], 4)", "expected_return": True}, {"call": "pair_diff_exists([3, 3], 0)", "expected_return": True}, {"call": "pair_diff_exists([1, 2], 5)", "expected_return": False}],
                "Compare distinct index pairs or hash complements carefully.", "Hash / Pair Search", "Matching Problems to Data Structures"),
        ]
    )

    add_pair(ds, "Arrays and Strings",
        [
            mcq("What is the time complexity of inserting a new item at index 0 of an array of size `n`?", "O(1)", "O(log n)", "O(n)", "O(n^2)", "C",
                "All existing elements must shift right by one slot in O(n) time.", "Array Shifting Cost", "Array Memory Model and Indexing"),
            pred("What will this string slice output?", "word = 'structure'\nprint(word[1:6:2])", "tuc",
                 "Indices 1 ('t'), 3 ('u'), 5 ('c') form 'tuc'.", "Strided Slicing", "String Immutability and Slicing"),
            dbg("Fix `is_pal(s)` so it properly moves both left and right pointers inward.",
                "def is_pal(s):\n    l, r = 0, len(s) - 1\n    while l < r:\n        if s[l] != s[r]:\n            return False\n        l += 1\n    return True",
                "def is_pal(s):\n    l, r = 0, len(s) - 1\n    while l < r:\n        if s[l] != s[r]:\n            return False\n        l += 1\n        r -= 1\n    return True",
                [{"call": "is_pal('level')", "expected_return": True}, {"call": "is_pal('code')", "expected_return": False}],
                "Both l += 1 and r -= 1 must advance each step.", "Two-Pointer Advance Bug", "Two-Pointer Scans on Sorted Arrays and Strings"),
            fill("Fill in the blank `___` to move the right pointer one step leftward.",
                 "while l < r:\n    l += 1\n    ___", "r -= 1",
                 "Decrement right pointer with r -= 1 toward the middle.", "Two-Pointer Convergence", "Two-Pointer Scans on Sorted Arrays and Strings"),
            wrt("Write `move_zeros_end(nums)` returning a new list with all non-zeros in order followed by zeros.",
                "def move_zeros_end(nums):\n    pass",
                "def move_zeros_end(nums):\n    non_z = [x for x in nums if x != 0]\n    return non_z + [0] * (len(nums) - len(non_z))",
                [{"call": "move_zeros_end([0, 3, 0, 5, 1])", "expected_return": [3, 5, 1, 0, 0]}, {"call": "move_zeros_end([1, 2])", "expected_return": [1, 2]}],
                "Collect non-zero elements in original order and append trailing zeros.", "Array Partitioning", "Array Memory Model and Indexing"),
        ],
        [
            mcq("Why does `s = 'abc'; s[0] = 'z'` fail in Python?", "Index out of bounds", "Strings are immutable", "Wrong quote style", "Strings need integer keys", "B",
                "Python strings cannot be modified in place; create a new string instead.", "String Immutability", "String Immutability and Slicing"),
            pred("What output does this array step print?", "arr = [2, 4, 6, 8]\nprint(arr[-1] - arr[0])", "6",
                 "Last item 8 minus first item 2 equals 6.", "Zero & Negative Indexing", "Array Memory Model and Indexing"),
            dbg("Fix `count_vowels(text)` so uppercase vowels are counted too.",
                "def count_vowels(text):\n    return sum(1 for ch in text if ch in 'aeiou')",
                "def count_vowels(text):\n    return sum(1 for ch in text.lower() if ch in 'aeiou')",
                [{"call": "count_vowels('Apple')", "expected_return": 2}, {"call": "count_vowels('SKY')", "expected_return": 0}],
                "Normalize case with text.lower() before checking vowels.", "Case-Normalized Scan", "String Immutability and Slicing"),
            fill("Fill in the blank `___` to build a reversed copy of string `s` using slicing.",
                 "rev = s[___]", "::-1",
                 "Step -1 over the full slice reverses any string.", "Reverse String Slicing", "String Immutability and Slicing"),
            wrt("Write `sorted_pair_target(arr, target)` using two pointers on sorted `arr`, returning True if two distinct elements sum to `target`.",
                "def sorted_pair_target(arr, target):\n    pass",
                "def sorted_pair_target(arr, target):\n    l, r = 0, len(arr) - 1\n    while l < r:\n        s = arr[l] + arr[r]\n        if s == target: return True\n        elif s < target: l += 1\n        else: r -= 1\n    return False",
                [{"call": "sorted_pair_target([1, 3, 6, 10], 9)", "expected_return": True}, {"call": "sorted_pair_target([2, 4, 7], 20)", "expected_return": False}],
                "Start l=0, r=len-1 and shift l or r depending on arr[l] + arr[r].", "Sorted Two-Pointer Search", "Two-Pointer Scans on Sorted Arrays and Strings"),
        ]
    )

    add_pair(ds, "Linked Lists",
        [
            mcq("What is the time complexity of finding the 50th element in a singly linked list?", "O(1)", "O(n)", "O(log n)", "O(1) with tail pointer", "B",
                "Linked lists lack random indexing and must traverse node by node in O(n).", "Sequential Access Cost", "Nodes, Data, and Next References"),
            pred("What output does this linked-node dictionary traversal produce?", "n3 = {'v': 9, 'next': None}\nn2 = {'v': 4, 'next': n3}\nn1 = {'v': 1, 'next': n2}\ncur = n1\nprint(cur['next']['v'])", "4",
                 "n1['next'] is n2 whose value is 4.", "Pointer Chaining", "Nodes, Data, and Next References"),
            dbg("Fix `count_linked_nodes(head)` so it traverses every node instead of looping infinitely.",
                "def count_linked_nodes(head):\n    c = 0\n    cur = head\n    while cur is not None:\n        c += 1\n    return c",
                "def count_linked_nodes(head):\n    c = 0\n    cur = head\n    while cur is not None:\n        c += 1\n        cur = cur.get('next')\n    return c",
                [{"call": "count_linked_nodes({'v': 1, 'next': {'v': 2, 'next': None}})", "expected_return": 2}, {"call": "count_linked_nodes(None)", "expected_return": 0}],
                "Advance cur = cur.get('next') inside the while loop.", "Missing Pointer Advance", "Traversing, Inserting, and Deleting Nodes"),
            fill("Fill in the blank `___` to point `prev`'s next reference past deleted node `cur`.",
                 "prev['next'] = cur[___]", "'next'",
                 "Set prev['next'] = cur['next'] to unlink cur from the chain.", "Node Deletion Link", "Traversing, Inserting, and Deleting Nodes", ['"next"', "next"]),
            wrt("Write `sum_linked(head)` summing all `'val'` fields through dict nodes `{'val': x, 'next': ...}` until `None`.",
                "def sum_linked(head):\n    pass",
                "def sum_linked(head):\n    total = 0\n    cur = head\n    while cur is not None:\n        total += cur['val']\n        cur = cur.get('next')\n    return total",
                [{"call": "sum_linked({'val': 5, 'next': {'val': 15, 'next': None}})", "expected_return": 20}, {"call": "sum_linked(None)", "expected_return": 0}],
                "Iterate while cur is not None adding cur['val'] and advancing cur.", "Linked List Summation", "Traversing, Inserting, and Deleting Nodes"),
        ],
        [
            mcq("When inserting a new node at the head of a singly linked list, how fast is the operation?", "O(1)", "O(n)", "O(log n)", "O(n^2)", "A",
                "You only set new_node.next = head and update head = new_node in O(1).", "Head Insertion Complexity", "Traversing, Inserting, and Deleting Nodes"),
            pred("Predict what this prints.", "tail = {'v': 'Z', 'next': None}\nhead = {'v': 'A', 'next': tail}\nprint(head['next']['next'])", "None",
                 "tail['next'] holds None to terminate the linked list.", "Tail Null Terminator", "Nodes, Data, and Next References"),
            dbg("Fix `prepend_value(head, val)` so the new node links to the existing `head`.",
                "def prepend_value(head, val):\n    return {'v': val, 'next': None}",
                "def prepend_value(head, val):\n    return {'v': val, 'next': head}",
                [{"call": "prepend_value({'v': 2, 'next': None}, 1)['next']['v']", "expected_return": 2}],
                "Point the new head's 'next' field to the previous head.", "Head Link Reset Bug", "Traversing, Inserting, and Deleting Nodes"),
            fill("Fill in the blank `___` to check safely if the linked list is empty before traversing.",
                 "if head ___ None:\n    return 0", "is",
                 "Use 'if head is None:' to guard empty lists.", "Empty Head Guard", "Traversing, Inserting, and Deleting Nodes", ["=="]),
            wrt("Write `linked_to_list(head)` returning a Python list of `'val'` values from `{'val': v, 'next': ...}`.",
                "def linked_to_list(head):\n    pass",
                "def linked_to_list(head):\n    out = []\n    cur = head\n    while cur is not None:\n        out.append(cur['val'])\n        cur = cur.get('next')\n    return out",
                [{"call": "linked_to_list({'val': 3, 'next': {'val': 6, 'next': None}})", "expected_return": [3, 6]}, {"call": "linked_to_list(None)", "expected_return": []}],
                "Walk the chain appending cur['val'] until cur is None.", "Linked Traversal Extraction", "Traversing, Inserting, and Deleting Nodes"),
        ]
    )

    add_pair(ds, "Stacks and Queues",
        [
            mcq("Which operation removes the oldest element from a Queue?", "push", "peek", "dequeue", "pop_top", "C",
                "A queue dequeues from the front in FIFO order.", "Queue Operations", "Queues (First-In, First-Out) and deque"),
            pred("Predict the printed output.", "st = [10]\nst.append(20)\nst.append(30)\na = st.pop()\nprint(a, st[-1])", "30 20",
                 "pop() removes 30 (LIFO), leaving 20 at top.", "Stack LIFO Execution", "Stacks (Last-In, First-Out) and Applications"),
            dbg("Fix `fifo_serve(queue)` so it removes and returns the FRONT item instead of the back.",
                "def fifo_serve(queue):\n    if not queue: return None\n    return queue.pop()",
                "def fifo_serve(queue):\n    if not queue: return None\n    return queue.pop(0)",
                [{"call": "fifo_serve(['first', 'second'])", "expected_return": "first"}, {"call": "fifo_serve([])", "expected_return": None}],
                "queue.pop(0) removes the front item in FIFO order; pop() removed the back.", "FIFO vs LIFO Endpoint", "Queues (First-In, First-Out) and deque"),
            fill("Fill in the blank `___` from Python's `collections` module to implement an O(1) Queue.",
                 "from collections import ___", "deque",
                 "collections.deque supports O(1) append and popleft.", "Deque Import", "Queues (First-In, First-Out) and deque"),
            wrt("Write `reverse_using_stack(items)` returning a new reversed list by pushing then popping items on a stack list.",
                "def reverse_using_stack(items):\n    pass",
                "def reverse_using_stack(items):\n    st = list(items)\n    out = []\n    while st:\n        out.append(st.pop())\n    return out",
                [{"call": "reverse_using_stack([1, 2, 3])", "expected_return": [3, 2, 1]}, {"call": "reverse_using_stack([])", "expected_return": []}],
                "Popping elements sequentially reverses LIFO order.", "Stack Reversal Pattern", "Stacks (Last-In, First-Out) and Applications"),
        ],
        [
            mcq("Which data structure keeps track of active function calls inside a runtime?", "Queue", "Stack", "Hash Set", "Graph", "B",
                "Program execution maintains an active LIFO call stack.", "Call Stack Structure", "Stacks (Last-In, First-Out) and Applications"),
            pred("What output does this queue trace print?", "from collections import deque\nq = deque([5, 6])\nq.append(7)\nprint(q.popleft(), len(q))", "5 2",
                 "popleft() removes 5, leaving [6, 7] of length 2.", "Deque popleft Behavior", "Queues (First-In, First-Out) and deque"),
            dbg("Fix `brackets_balanced(s)` so it checks opening `'['` / `']'` pairs safely without crashing on empty stack.",
                "def brackets_balanced(s):\n    st = []\n    for ch in s:\n        if ch == '[': st.append(ch)\n        elif ch == ']': st.pop()\n    return len(st) == 0",
                "def brackets_balanced(s):\n    st = []\n    for ch in s:\n        if ch == '[': st.append(ch)\n        elif ch == ']':\n            if not st: return False\n            st.pop()\n    return len(st) == 0",
                [{"call": "brackets_balanced('[[]]')", "expected_return": True}, {"call": "brackets_balanced('][')", "expected_return": False}],
                "Return False if a closing ']' appears when the stack is empty.", "Empty Stack Guard", "Stacks (Last-In, First-Out) and Applications"),
            fill("Fill in the blank `___` to inspect the top element of non-empty stack `st` without removing it.",
                 "top_item = st[___]", "-1",
                 "Negative index -1 peeks at the top element without modifying the stack.", "Stack Peek Operation", "Stacks (Last-In, First-Out) and Applications"),
            wrt("Write `next_in_line(waitlist, arrive)` appending `arrive` to `waitlist` and popping/returning the first waiting person.",
                "def next_in_line(waitlist, arrive):\n    pass",
                "def next_in_line(waitlist, arrive):\n    waitlist.append(arrive)\n    return waitlist.pop(0)",
                [{"call": "next_in_line(['Ada', 'Bob'], 'Cid')", "expected_return": "Ada"}, {"call": "next_in_line([], 'Zoe')", "expected_return": "Zoe"}],
                "Enqueue arrive at the back and dequeue index 0 from front.", "Queue Simulation", "Queues (First-In, First-Out) and deque"),
        ]
    )

    add_pair(ds, "Recursion Basics",
        [
            mcq("What terminates a chain of recursive function calls?", "A for loop", "A global variable", "The base case", "An exception handler", "C",
                "Recursion stops when input satisfies the non-recursive base case.", "Base Case Purpose", "Call Stack and Base Cases"),
            pred("Predict what this recursive function prints.", "def f(n):\n    if n <= 1: return n\n    return n + f(n - 2)\nprint(f(5))", "9",
                 "5 + f(3) -> 5 + (3 + f(1)) -> 5 + 3 + 1 = 9.", "Recursive Trace", "Writing Recursive Solutions (Factorial, Sum, Fibonacci)"),
            dbg("Fix `power_rec(base, exp)` so `exp == 0` returns `1` instead of looping infinitely.",
                "def power_rec(base, exp):\n    return base * power_rec(base, exp - 1)",
                "def power_rec(base, exp):\n    if exp == 0:\n        return 1\n    return base * power_rec(base, exp - 1)",
                [{"call": "power_rec(2, 4)", "expected_return": 16}, {"call": "power_rec(5, 0)", "expected_return": 1}],
                "Add base case 'if exp == 0: return 1'.", "Missing Base Case", "Call Stack and Base Cases"),
            fill("Fill in the blank `___` to step `n` closer to base case `n == 0`.",
                 "def sum_down(n):\n    if n <= 0: return 0\n    return n + sum_down(___)", "n - 1",
                 "Each call must reduce the problem toward the base case (n - 1).", "Recursive Progress Step", "Call Stack and Base Cases", ["n-1"]),
            wrt("Write recursive `rec_product(n)` returning `1 * 2 * ... * n` for positive `n` (base case `n <= 1 -> 1`).",
                "def rec_product(n):\n    pass",
                "def rec_product(n):\n    if n <= 1:\n        return 1\n    return n * rec_product(n - 1)",
                [{"call": "rec_product(4)", "expected_return": 24}, {"call": "rec_product(1)", "expected_return": 1}],
                "Return 1 when n <= 1; otherwise return n * rec_product(n - 1).", "Recursive Factorial Pattern", "Writing Recursive Solutions (Factorial, Sum, Fibonacci)", rec=True),
        ],
        [
            mcq("Which error occurs in Python if recursion runs past maximum call depth without hitting a base case?", "MemoryLeak", "RecursionError", "OverflowException", "StopIteration", "B",
                "Python raises RecursionError when the call stack limit is exceeded.", "Stack Overflow Guard", "Call Stack and Base Cases"),
            pred("What output does this recursive trace print?", "def count_str(s):\n    if not s: return 0\n    return 1 + count_str(s[1:])\nprint(count_str('code'))", "4",
                 "Strips 1 character per recursive step across 'code' -> 4.", "String Slice Recursion", "Writing Recursive Solutions (Factorial, Sum, Fibonacci)"),
            dbg("Fix recursive `rec_list_sum(arr)` so it returns the sum instead of `None` on empty slice.",
                "def rec_list_sum(arr):\n    if not arr: return\n    return arr[0] + rec_list_sum(arr[1:])",
                "def rec_list_sum(arr):\n    if not arr: return 0\n    return arr[0] + rec_list_sum(arr[1:])",
                [{"call": "rec_list_sum([4, 6, 5])", "expected_return": 15}, {"call": "rec_list_sum([])", "expected_return": 0}],
                "Return 0 (not None) in the empty-list base case.", "Base Case Return Value", "Call Stack and Base Cases"),
            fill("Fill in the blank `___` for the Fibonacci base case when `n <= 1`.",
                 "def fib(n):\n    if n <= 1: return ___\n    return fib(n - 1) + fib(n - 2)", "n",
                 "fib(0) = 0 and fib(1) = 1, so return n.", "Fibonacci Base Case", "Writing Recursive Solutions (Factorial, Sum, Fibonacci)"),
            wrt("Write recursive `rec_count_target(arr, x)` counting occurrences of `x` in `arr` recursively.",
                "def rec_count_target(arr, x):\n    pass",
                "def rec_count_target(arr, x):\n    if not arr:\n        return 0\n    return (1 if arr[0] == x else 0) + rec_count_target(arr[1:], x)",
                [{"call": "rec_count_target([2, 5, 2, 2], 2)", "expected_return": 3}, {"call": "rec_count_target([], 2)", "expected_return": 0}],
                "Count 1 if arr[0] == x plus rec_count_target(arr[1:], x).", "Recursive List Accumulation", "Writing Recursive Solutions (Factorial, Sum, Fibonacci)", rec=True),
        ]
    )

    add_pair(ds, "Trees and Binary Search Trees",
        [
            mcq("In a Binary Search Tree, where are keys strictly smaller than a node stored?", "Left subtree", "Right subtree", "Only at root", "Only in leaf right child", "A",
                "BST invariant keeps smaller keys in the left subtree and larger keys in the right.", "BST Invariant", "Binary Search Tree (BST) Search & Invariant"),
            pred("What order does Preorder traversal visit nodes in a tree?", "def visit(): print('Root -> Left -> Right')\nvisit()", "Root -> Left -> Right",
                 "Preorder processes the current node first, then left, then right.", "Tree Traversal Order", "Tree Terminology and Traversals"),
            dbg("Fix `bst_contains(node, target)` so it branches left when `target < node['val']`.",
                "def bst_contains(node, target):\n    if node is None: return False\n    if node['val'] == target: return True\n    if target < node['val']:\n        return bst_contains(node.get('right'), target)\n    return bst_contains(node.get('left'), target)",
                "def bst_contains(node, target):\n    if node is None: return False\n    if node['val'] == target: return True\n    if target < node['val']:\n        return bst_contains(node.get('left'), target)\n    return bst_contains(node.get('right'), target)",
                [{"call": "bst_contains({'val': 10, 'left': {'val': 4}, 'right': {'val': 15}}, 4)", "expected_return": True},
                 {"call": "bst_contains({'val': 10, 'left': {'val': 4}, 'right': {'val': 15}}, 99)", "expected_return": False}],
                "Go left when target < node['val'] and right when target > node['val'].", "BST Branch Direction", "Binary Search Tree (BST) Search & Invariant"),
            fill("Fill in the blank `___` to name the BST traversal that outputs keys in sorted ascending order.",
                 "# Left -> Root -> Right traversal is called ___", "Inorder",
                 "Inorder traversal visits left subtree, node, then right subtree.", "Inorder BST Traversal", "Tree Terminology and Traversals", ["inorder", "in-order"]),
            wrt("Write `count_nodes(root)` counting nodes recursively in a dict tree `{'val': x, 'left': ..., 'right': ...}`.",
                "def count_nodes(root):\n    pass",
                "def count_nodes(root):\n    if root is None:\n        return 0\n    return 1 + count_nodes(root.get('left')) + count_nodes(root.get('right'))",
                [{"call": "count_nodes({'val': 1, 'left': {'val': 2}, 'right': None})", "expected_return": 2}, {"call": "count_nodes(None)", "expected_return": 0}],
                "Base case root is None -> 0; else 1 + count(left) + count(right).", "Recursive Tree Size", "Tree Terminology and Traversals"),
        ],
        [
            mcq("Which node in a tree has zero child nodes?", "Root node", "Leaf node", "Internal node", "Subtree node", "B",
                "A leaf node is a terminal node with no left or right children.", "Leaf Definition", "Tree Terminology and Traversals"),
            pred("If a BST has root `20`, left child `10`, right child `30`, what does Inorder traversal produce?", "print('10 20 30')", "10 20 30",
                 "Inorder (left, root, right) prints sorted values 10, 20, 30.", "Inorder Output", "Binary Search Tree (BST) Search & Invariant"),
            dbg("Fix `bst_min(node)` so it returns the smallest value in a non-empty BST.",
                "def bst_min(node):\n    while node.get('right') is not None:\n        node = node['right']\n    return node['val']",
                "def bst_min(node):\n    while node.get('left') is not None:\n        node = node['left']\n    return node['val']",
                [{"call": "bst_min({'val': 8, 'left': {'val': 3, 'left': {'val': 1}}})", "expected_return": 1}],
                "The minimum key in a BST lives down the leftmost path.", "BST Minimum Path", "Binary Search Tree (BST) Search & Invariant"),
            fill("Fill in the blank `___` for average search complexity of a balanced BST.",
                 "# Balanced BST search discards half the tree each step: O(___)", "log n",
                 "Balanced BST height is O(log n).", "Balanced Search Complexity", "Binary Search Tree (BST) Search & Invariant", ["logn", "log(n)"]),
            wrt("Write `tree_height(root)` returning height (number of nodes along longest root-to-leaf path, 0 for `None`).",
                "def tree_height(root):\n    pass",
                "def tree_height(root):\n    if root is None:\n        return 0\n    return 1 + max(tree_height(root.get('left')), tree_height(root.get('right')))",
                [{"call": "tree_height({'val': 1, 'left': {'val': 2, 'left': {'val': 3}}, 'right': None})", "expected_return": 3}, {"call": "tree_height(None)", "expected_return": 0}],
                "Return 1 + max(left height, right height).", "Recursive Tree Height", "Tree Terminology and Traversals"),
        ]
    )

    add_pair(ds, "Graphs and Hashing",
        [
            mcq("Why do BFS and DFS keep a `visited` set when traversing a graph?", "To sort edges", "To avoid revisiting vertices in cycles", "To increase speed to O(1)", "To delete leaf nodes", "B",
                "Graphs may contain cycles; tracking visited prevents infinite loops.", "Cycle Prevention", "Adjacency Lists, BFS, and DFS"),
            pred("Predict what this hash frequency check prints.", "m = {}\nfor c in 'tree':\n    m[c] = m.get(c, 0) + 1\nprint(m['e'])", "2",
                 "'e' appears twice in 'tree'.", "Hash Counting", "Hash Tables and Frequency Dictionaries"),
            dbg("Fix `has_edge(adj, u, v)` so it handles missing vertex `u` cleanly without raising `KeyError`.",
                "def has_edge(adj, u, v):\n    return v in adj[u]",
                "def has_edge(adj, u, v):\n    return v in adj.get(u, [])",
                [{"call": "has_edge({'A': ['B']}, 'A', 'B')", "expected_return": True}, {"call": "has_edge({'A': ['B']}, 'Z', 'B')", "expected_return": False}],
                "Use adj.get(u, []) before membership lookup.", "Missing Vertex Guard", "Adjacency Lists, BFS, and DFS"),
            fill("Fill in the blank `___` to name the graph search that explores level by level using a Queue.",
                 "# Level-order graph search using a queue: ___", "BFS",
                 "Breadth-First Search (BFS) explores neighbors level by level using a queue.", "Graph Traversal Selection", "Adjacency Lists, BFS, and DFS", ["Breadth-First Search"]),
            wrt("Write `word_freq(words)` returning a dict mapping each word to how many times it appears.",
                "def word_freq(words):\n    pass",
                "def word_freq(words):\n    freq = {}\n    for w in words:\n        freq[w] = freq.get(w, 0) + 1\n    return freq",
                [{"call": "word_freq(['py', 'ml', 'py'])", "expected_return": {"py": 2, "ml": 1}}, {"call": "word_freq([])", "expected_return": {}}],
                "Accumulate counts using a dictionary frequency map.", "Hash Frequency Map", "Hash Tables and Frequency Dictionaries"),
        ],
        [
            mcq("What average lookup complexity does a well-sized hash table provide?", "O(1)", "O(log n)", "O(n)", "O(n log n)", "A",
                "Direct bucket hashing enables constant O(1) average lookup time.", "Hash Table Lookup", "Hash Tables and Frequency Dictionaries"),
            pred("Predict the printed output.", "g = {'X': ['Y', 'Z'], 'Y': ['Z']}\nprint(len(g.get('X', [])))", "2",
                 "Node 'X' has 2 outgoing neighbors: 'Y' and 'Z'.", "Adjacency Degree", "Adjacency Lists, BFS, and DFS"),
            dbg("Fix `count_reachable(adj, start)` BFS so it tracks visited nodes and terminates on cycles.",
                "def count_reachable(adj, start):\n    q = [start]\n    cnt = 0\n    while q:\n        cur = q.pop(0)\n        cnt += 1\n        for nb in adj.get(cur, []):\n            q.append(nb)\n    return cnt",
                "def count_reachable(adj, start):\n    seen = {start}\n    q = [start]\n    while q:\n        cur = q.pop(0)\n        for nb in adj.get(cur, []):\n            if nb not in seen:\n                seen.add(nb)\n                q.append(nb)\n    return len(seen)",
                [{"call": "count_reachable({'A': ['B'], 'B': ['A']}, 'A')", "expected_return": 2}, {"call": "count_reachable({'A': ['B', 'C']}, 'A')", "expected_return": 3}],
                "Add nodes to a visited/seen set before queueing so cycles don't repeat.", "BFS Visited Tracking", "Adjacency Lists, BFS, and DFS"),
            fill("Fill in the blank `___` to mark `node` as visited in set `seen`.",
                 "if node not in seen:\n    seen.___(node)", "add",
                 "Python sets insert new elements with .add().", "Set Visited Update", "Adjacency Lists, BFS, and DFS"),
            wrt("Write `common_neighbors(adj, u, v)` returning a sorted list of neighbors shared by both `u` and `v` in `adj`.",
                "def common_neighbors(adj, u, v):\n    pass",
                "def common_neighbors(adj, u, v):\n    return sorted(set(adj.get(u, [])) & set(adj.get(v, [])))",
                [{"call": "common_neighbors({'A': ['C', 'B'], 'D': ['B', 'E']}, 'A', 'D')", "expected_return": ["B"]}, {"call": "common_neighbors({'A': ['B']}, 'A', 'Z')", "expected_return": []}],
                "Intersect neighbor sets of u and v and sort the result.", "Adjacency Set Intersection", "Adjacency Lists, BFS, and DFS"),
        ]
    )

    # =====================================================================
    # COURSE 3: JAVA PROGRAMMING (6 Topics)
    # =====================================================================
    jv = "Java Programming"

    add_pair(jv, "Java Syntax and Data Types",
        [
            mcq("Which Java keyword creates an unchangeable constant variable?", "const", "final", "static", "fixed", "B",
                "Java uses 'final' to lock a variable after first assignment.", "Java Constants", "Java Program Structure and Variables"),
            pred("What will this Java snippet print?", "int x = 9;\nint y = 4;\nSystem.out.println(x / y);", "2",
                 "Integer division 9 / 4 truncates decimal remainder to 2.", "Java Integer Division", "Primitive Types, Type Division, and Strings"),
            jdbg("Fix the compile error in `toFahrenheit(int c)` so decimal math returns a `double` accurately.",
                 "static int toFahrenheit(int c) {\n    return c * 1.8 + 32;\n}",
                 "static double toFahrenheit(int c) {\n    return c * 1.8 + 32;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "toFahrenheit(0)", "expected": "32.0"}, {"expr": "toFahrenheit(10)", "expected": "50.0"}],
                  "required_pattern_groups": [[r"static\s+double\s+toFahrenheit"], [r"1\.8"]]},
                 "Returning c * 1.8 + 32 produces a double, so the return type must be double.", "Return Type Mismatch", "Primitive Types, Type Division, and Strings"),
            fill("Fill in the blank `___` to declare the entry-point method in a Java class.",
                 "public static ___ main(String[] args) { }", "void",
                 "main does not return a value, so its return type is void.", "Main Entry Signature", "Java Program Structure and Variables"),
            jwrt("Write `static String fullLabel(String code, int num)` returning `code + \"-\" + num`.",
                 "static String fullLabel(String code, int num) {\n    \n}",
                 "static String fullLabel(String code, int num) {\n    return code + \"-\" + num;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "fullLabel(\"CS\", 101)", "expected": "CS-101"}, {"expr": "fullLabel(\"PY\", 3)", "expected": "PY-3"}],
                  "required_pattern_groups": [[r"String\s+fullLabel"], [r"return"]]},
                 "Concatenate String code, '-', and int num cleanly.", "Java String Concatenation", "Primitive Types, Type Division, and Strings"),
        ],
        [
            mcq("Which quotes surround a `char` literal in Java?", "Double quotes \"a\"", "Single quotes 'a'", "Backticks `a`", "No quotes", "B",
                "char literals require single quotes ('a'), while Strings use double quotes.", "Java char vs String", "Primitive Types, Type Division, and Strings"),
            pred("Predict the Java console output.", "double val = 5 / 2.0;\nSystem.out.println(val);", "2.5",
                 "Dividing by double 2.0 promotes integer 5 to double (2.5).", "Mixed Numeric Promotion", "Primitive Types, Type Division, and Strings"),
            jdbg("Fix the missing semicolon and type mismatch in `halfValue(int n)`.",
                 "static double halfValue(int n) {\n    return n / 2\n}",
                 "static double halfValue(int n) {\n    return n / 2.0;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "halfValue(5)", "expected": "2.5"}, {"expr": "halfValue(6)", "expected": "3.0"}],
                  "required_pattern_groups": [[r"2\.0"], [r";"]]},
                 "End statement with ';' and divide by 2.0 to avoid integer truncation.", "Statement Syntax & Division", "Java Program Structure and Variables"),
            fill("Fill in the blank `___` to declare a constant integer limit in Java.",
                 "___ int MAX_LIMIT = 50;", "final",
                 "'final' makes a variable constant after assignment.", "Final Constant Keyword", "Java Program Structure and Variables"),
            jwrt("Write `static double avgTwo(int a, int b)` returning the exact decimal average `(a + b) / 2.0`.",
                 "static double avgTwo(int a, int b) {\n    \n}",
                 "static double avgTwo(int a, int b) {\n    return (a + b) / 2.0;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "avgTwo(3, 4)", "expected": "3.5"}, {"expr": "avgTwo(10, 20)", "expected": "15.0"}],
                  "required_pattern_groups": [[r"double\s+avgTwo"], [r"2\.0"]]},
                 "Use 2.0 so Java evaluates floating-point division.", "Floating-Point Average", "Primitive Types, Type Division, and Strings"),
        ]
    )

    add_pair(jv, "Operators and Control Statements",
        [
            mcq("How should you compare two `String` objects for equal text content in Java?", "a == b", "a.equals(b)", "a === b", "a.same(b)", "B",
                "Use .equals() to compare characters inside String objects.", "String Value Equality", "Logical Operators and String Comparison"),
            pred("What does this Java switch snippet print?", "int d = 2;\nswitch (d) {\n    case 1: System.out.print(\"One\"); break;\n    case 2: System.out.print(\"Two\");\n    default: System.out.print(\"End\");\n}", "TwoEnd",
                 "Missing break after case 2 falls through into default and prints TwoEnd.", "Switch Fall-Through", "if-else, switch Statements, and Ternary"),
            jdbg("Fix `isMatch(String a, String b)` so it checks text equality using `.equals()`, not `==`.",
                 "static boolean isMatch(String a, String b) {\n    return a == b;\n}",
                 "static boolean isMatch(String a, String b) {\n    return a != null && a.equals(b);\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "isMatch(new String(\"ok\"), new String(\"ok\"))", "expected": "true"}, {"expr": "isMatch(\"hi\", \"no\")", "expected": "false"}],
                  "required_pattern_groups": [[r"\.equals\s*\("]]},
                 "new String(\"ok\") == new String(\"ok\") is false; .equals() returns true.", "String Reference vs Value", "Logical Operators and String Comparison"),
            fill("Fill in the blank `___` for logical OR in Java.",
                 "if (x < 0 ___ x > 100) { return false; }", "||",
                 "Java uses || for logical OR and && for logical AND.", "Logical OR Operator", "Logical Operators and String Comparison"),
            jwrt("Write `static String signLabel(int n)` returning `\"Positive\"` if `n > 0`, `\"Negative\"` if `n < 0`, else `\"Zero\"`.",
                 "static String signLabel(int n) {\n    \n}",
                 "static String signLabel(int n) {\n    if (n > 0) return \"Positive\";\n    if (n < 0) return \"Negative\";\n    return \"Zero\";\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "signLabel(5)", "expected": "Positive"}, {"expr": "signLabel(-3)", "expected": "Negative"}, {"expr": "signLabel(0)", "expected": "Zero"}],
                  "required_pattern_groups": [[r"if\s*\("], [r"Positive"]]},
                 "Branch cleanly across > 0, < 0, and else.", "Multi-Branch Conditionals", "if-else, switch Statements, and Ternary"),
        ],
        [
            mcq("Which operator represents logical AND in Java?", "and", "&", "&&", "||", "C",
                "&& performs short-circuit logical AND in Java.", "Java Logical AND", "Logical Operators and String Comparison"),
            pred("What output does this ternary expression produce?", "int x = 11;\nSystem.out.println((x % 2 == 0) ? \"E\" : \"O\");", "O",
                 "11 % 2 != 0, so the false branch 'O' prints.", "Ternary Evaluation", "if-else, switch Statements, and Ternary"),
            jdbg("Fix `dayType(int d)` so case 1 doesn't fall through into case 2.",
                 "static String dayType(int d) {\n    switch (d) {\n        case 1: return \"Start\";\n        case 6:\n        case 7: return \"Weekend\";\n        default: return \"Weekday\";\n    }\n}",
                 "static String dayType(int d) {\n    String out;\n    switch (d) {\n        case 1: out = \"Start\"; break;\n        case 6:\n        case 7: out = \"Weekend\"; break;\n        default: out = \"Weekday\";\n    }\n    return out;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "dayType(1)", "expected": "Start"}, {"expr": "dayType(7)", "expected": "Weekend"}, {"expr": "dayType(3)", "expected": "Weekday"}],
                  "required_pattern_groups": [[r"switch\s*\("]]},
                 "Return or break each switch branch cleanly.", "Switch Branching", "if-else, switch Statements, and Ternary"),
            fill("Fill in the blank `___` keyword to stop switch fall-through.",
                 "case 1:\n    msg = \"One\";\n    ___;", "break",
                 "break exits the switch block once the matching case finishes.", "Switch Break Control", "if-else, switch Statements, and Ternary"),
            jwrt("Write `static int maxOfThree(int a, int b, int c)` returning the largest of three integers.",
                 "static int maxOfThree(int a, int b, int c) {\n    \n}",
                 "static int maxOfThree(int a, int b, int c) {\n    int m = a;\n    if (b > m) m = b;\n    if (c > m) m = c;\n    return m;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "maxOfThree(3, 9, 4)", "expected": "9"}, {"expr": "maxOfThree(-5, -1, -8)", "expected": "-1"}],
                  "required_pattern_groups": [[r"if\s*\("], [r"return"]]},
                 "Compare candidates using conditional branches.", "Relational Comparison Logic", "Logical Operators and String Comparison"),
        ]
    )

    add_pair(jv, "Loops and Arrays",
        [
            mcq("How do you read the number of items in a Java array `int[] scores`?", "scores.length()", "scores.size()", "scores.length", "len(scores)", "C",
                "Java arrays provide a public .length field without parentheses.", "Array Length Property", "Fixed Arrays and Enhanced for-each"),
            pred("Predict the printed Java output.", "int[] a = {2, 4, 6};\nint s = 0;\nfor (int x : a) { s += x; }\nSystem.out.println(s);", "12",
                 "Enhanced for-each sums 2 + 4 + 6 = 12.", "Enhanced For-Each", "Fixed Arrays and Enhanced for-each"),
            jdbg("Fix the `ArrayIndexOutOfBoundsException` off-by-one loop boundary in `sumArray`.",
                 "static int sumArray(int[] arr) {\n    int s = 0;\n    for (int i = 0; i <= arr.length; i++) { s += arr[i]; }\n    return s;\n}",
                 "static int sumArray(int[] arr) {\n    int s = 0;\n    for (int i = 0; i < arr.length; i++) { s += arr[i]; }\n    return s;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "sumArray(new int[]{3, 5, 2})", "expected": "10"}, {"expr": "sumArray(new int[]{})", "expected": "0"}],
                  "required_pattern_groups": [[r"i\s*<\s*arr\.length"]]},
                 "Valid indices are 0 .. arr.length - 1, so condition must be i < arr.length.", "Array Bounds Condition", "Fixed Arrays and Enhanced for-each"),
            fill("Fill in the blank `___` for a loop construct guaranteed to run at least once.",
                 "___ {\n    count++;\n} while (count < 5);", "do",
                 "do { ... } while (...); evaluates condition after running body once.", "Do-While Syntax", "for, while, and do-while Loops"),
            jwrt("Write `static int countNegatives(int[] nums)` returning how many elements in `nums` are `< 0`.",
                 "static int countNegatives(int[] nums) {\n    \n}",
                 "static int countNegatives(int[] nums) {\n    int c = 0;\n    for (int x : nums) { if (x < 0) c++; }\n    return c;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "countNegatives(new int[]{-1, 4, -7, 0})", "expected": "2"}, {"expr": "countNegatives(new int[]{1, 2})", "expected": "0"}],
                  "required_pattern_groups": [[r"for\s*\("], [r"x\s*<\s*0|nums\[.*\]\s*<\s*0"]]},
                 "Iterate array items and increment counter whenever element < 0.", "Array Filtering Loop", "Fixed Arrays and Enhanced for-each"),
        ],
        [
            mcq("What happens when indexing `arr[arr.length]` on a Java array?", "Returns 0", "Returns last item", "ArrayIndexOutOfBoundsException", "Wraps to index 0", "C",
                "Last valid index is arr.length - 1; arr.length throws ArrayIndexOutOfBoundsException.", "Valid Index Bounds", "Fixed Arrays and Enhanced for-each"),
            pred("What does this `do-while` loop print?", "int k = 10;\ndo {\n    System.out.print(k);\n    k++;\n} while (k < 5);", "10",
                 "do-while always executes the body once even though 10 < 5 is false.", "Do-While Post-Test", "for, while, and do-while Loops"),
            jdbg("Fix `firstIndexOf(int[] arr, int target)` so it returns index of first match, or `-1` if absent.",
                 "static int firstIndexOf(int[] arr, int target) {\n    for (int i = 0; i < arr.length; i++) {\n        if (arr[i] == target) return arr[i];\n    }\n    return -1;\n}",
                 "static int firstIndexOf(int[] arr, int target) {\n    for (int i = 0; i < arr.length; i++) {\n        if (arr[i] == target) return i;\n    }\n    return -1;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "firstIndexOf(new int[]{5, 8, 2}, 8)", "expected": "1"}, {"expr": "firstIndexOf(new int[]{5, 8}, 9)", "expected": "-1"}],
                  "required_pattern_groups": [[r"return\s+i\s*;"]]},
                 "Return the index i, not element arr[i].", "Index vs Element Return", "Fixed Arrays and Enhanced for-each"),
            fill("Fill in the blank `___` to declare an enhanced for-each loop over `int[] vals`.",
                 "for (int v ___ vals) { total += v; }", ":",
                 "Java for-each loop syntax separates item and array with a colon (:).", "Enhanced For Syntax", "Fixed Arrays and Enhanced for-each"),
            jwrt("Write `static int findMax(int[] nums)` returning the largest element (assume `nums.length >= 1`).",
                 "static int findMax(int[] nums) {\n    \n}",
                 "static int findMax(int[] nums) {\n    int m = nums[0];\n    for (int x : nums) { if (x > m) m = x; }\n    return m;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "findMax(new int[]{3, 19, 7})", "expected": "19"}, {"expr": "findMax(new int[]{-8, -2, -5})", "expected": "-2"}],
                  "required_pattern_groups": [[r"for\s*\("], [r">"]]},
                 "Initialize tracker to nums[0] and update when x > m.", "Array Traversal Maximum", "Fixed Arrays and Enhanced for-each"),
        ]
    )

    add_pair(jv, "Methods and Classes",
        [
            mcq("What defines valid method overloading in Java?", "Same name and parameters, different return type", "Same name with different parameter types or count", "Calling static from instance", "Putting private on return", "B",
                "Overloaded methods share a name while differing in parameter signature.", "Method Overloading", "Java Methods and Method Overloading"),
            pred("What does this class constructor test output?", "System.out.println(new String(\"Hi\").length());", "2",
                 "'new' allocates a String object whose length() method returns 2.", "Object Instantiation", "Classes, Constructors, and Encapsulation"),
            jdbg("Fix `squareDiff(int a, int b)` so it returns `(a * a) - (b * b)` as an integer instead of `void`.",
                 "static void squareDiff(int a, int b) {\n    int d = (a * a) - (b * b);\n}",
                 "static int squareDiff(int a, int b) {\n    return (a * a) - (b * b);\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "squareDiff(5, 3)", "expected": "16"}, {"expr": "squareDiff(4, 4)", "expected": "0"}],
                  "required_pattern_groups": [[r"static\s+int\s+squareDiff"], [r"return\s+"]]},
                 "Change return type from void to int and include return.", "Void vs Return Type", "Java Methods and Method Overloading"),
            fill("Fill in the blank `___` access modifier to restrict a field strictly inside its declaring class.",
                 "___ int balance = 0;", "private",
                 "private hides fields from direct external access (encapsulation).", "Access Encapsulation", "Classes, Constructors, and Encapsulation"),
            jwrt("Write `static int clampZero(int n)` returning `n` if `n >= 0`, else `0`.",
                 "static int clampZero(int n) {\n    \n}",
                 "static int clampZero(int n) {\n    return n >= 0 ? n : 0;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "clampZero(8)", "expected": "8"}, {"expr": "clampZero(-4)", "expected": "0"}],
                  "required_pattern_groups": [[r"int\s+clampZero\s*\(\s*int\s+\w+\s*\)"], [r"return"]]},
                 "Check n >= 0 and return n or 0.", "Method Return Semantics", "Java Methods and Method Overloading"),
        ],
        [
            mcq("Which keyword allocates a new instance of a class in Java?", "create", "make", "new", "alloc", "C",
                "Objects are instantiated with new ClassName(...).", "Object Creation", "Classes, Constructors, and Encapsulation"),
            pred("What does this overloaded call output?", "System.out.println(Math.max(4, 9));", "9",
                 "Math.max resolves the int overload and returns 9.", "Method Dispatch", "Java Methods and Method Overloading"),
            jdbg("Fix `cube(int n)` so it returns `n * n * n` cleanly without truncation.",
                 "static int cube(int n) {\n    int res = n * n * n;\n}",
                 "static int cube(int n) {\n    return n * n * n;\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "cube(3)", "expected": "27"}, {"expr": "cube(-2)", "expected": "-8"}],
                  "required_pattern_groups": [[r"return\s+"]]},
                 "Missing return statement inside non-void method.", "Missing Return Statement", "Java Methods and Method Overloading"),
            fill("Fill in the blank `___` keyword to refer to the current object instance inside a constructor.",
                 "Box(int size) { ___.size = size; }", "this",
                 "'this' distinguishes instance fields from constructor parameters.", "Using this Reference", "Classes, Constructors, and Encapsulation"),
            jwrt("Write `static int rectPerimeter(int w, int h)` returning `2 * (w + h)`.",
                 "static int rectPerimeter(int w, int h) {\n    \n}",
                 "static int rectPerimeter(int w, int h) {\n    return 2 * (w + h);\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "rectPerimeter(4, 5)", "expected": "18"}, {"expr": "rectPerimeter(2, 1)", "expected": "6"}],
                  "required_pattern_groups": [[r"int\s+rectPerimeter"], [r"return"]]},
                 "Declare int method accepting w and h and returning 2 * (w + h).", "Java Method Definition", "Java Methods and Method Overloading"),
        ]
    )

    add_pair(jv, "Inheritance and Polymorphism",
        [
            mcq("How many direct superclasses can a Java class extend using `extends`?", "Up to two", "Only one", "Unlimited", "None", "B",
                "Java enforces single class inheritance (one parent class).", "Single Inheritance", "Single Inheritance with extends and super()"),
            pred("What will polymorphic dispatch print?", "class P { String f() { return \"P\"; } }\nclass C extends P { @Override String f() { return \"C\"; } }\nP obj = new C();\nSystem.out.println(obj.f());", "C",
                 "At runtime Java calls the overridden method of the actual instance C.", "Runtime Polymorphism", "Method Overriding and Polymorphism"),
            jdbg("Fix the `@Override` method signature bug inside `SubItem` (`tag` returns String).",
                 "class Item { String tag() { return \"Base\"; } }\nclass SubItem extends Item {\n    @Override int tag() { return 1; }\n}",
                 "class Item { String tag() { return \"Base\"; } }\nclass SubItem extends Item {\n    @Override String tag() { return \"Sub\"; }\n}",
                 {"java_wrapper": "class", "java_test_cases": [{"expr": "new SubItem().tag()", "expected": "Sub"}],
                  "required_pattern_groups": [[r"extends\s+Item"], [r"String\s+tag\s*\(\s*\)"]]},
                 "Overridden method must match the parent signature's compatible return type (String).", "Override Signature Match", "Method Overriding and Polymorphism"),
            fill("Fill in the blank `___` to call the parent class constructor from a subclass constructor.",
                 "Student(String name) {\n    ___(name);\n}", "super",
                 "super(...) calls the immediate parent class constructor.", "Super Constructor Call", "Single Inheritance with extends and super()"),
            jwrt("Create `class Device { String kind() { return \"Generic\"; } }` and `class Phone extends Device` overriding `kind()` to return `\"Phone\"`.",
                 "class Device {\n    String kind() { return \"Generic\"; }\n}\nclass Phone extends Device {\n    \n}",
                 "class Device {\n    String kind() { return \"Generic\"; }\n}\nclass Phone extends Device {\n    @Override String kind() { return \"Phone\"; }\n}",
                 {"java_wrapper": "class", "java_test_cases": [{"expr": "((Device) new Phone()).kind()", "expected": "Phone"}],
                  "required_pattern_groups": [[r"class\s+Phone\s+extends\s+Device"], [r"String\s+kind\s*\(\s*\)"]]},
                 "Extend Device and override kind() returning 'Phone'.", "Polymorphic Override Implementation", "Method Overriding and Polymorphism"),
        ],
        [
            mcq("Why should `@Override` be placed above an overridden subclass method?", "Speeds execution 10x", "Makes compiler verify you matched parent signature", "Allows private overrides", "Replaces extends keyword", "B",
                "@Override tells javac to catch typos in overridden method signatures.", "Override Annotation Safety", "Method Overriding and Polymorphism"),
            pred("Predict the output.", "class A { int val() { return 10; } }\nclass B extends A { @Override int val() { return super.val() + 5; } }\nSystem.out.println(new B().val());", "15",
                 "super.val() gives 10 plus 5 = 15.", "Calling Super Method", "Single Inheritance with extends and super()"),
            jdbg("Fix `Bike` so it inherits `wheels()` from `Cycle` using `extends`.",
                 "class Cycle { int wheels() { return 2; } }\nclass Bike implements Cycle {}",
                 "class Cycle { int wheels() { return 2; } }\nclass Bike extends Cycle {}",
                 {"java_wrapper": "class", "java_test_cases": [{"expr": "new Bike().wheels()", "expected": "2"}],
                  "required_pattern_groups": [[r"class\s+Bike\s+extends\s+Cycle"]]},
                 "Class-to-class inheritance uses 'extends', not 'implements'.", "Extends vs Implements", "Single Inheritance with extends and super()"),
            fill("Fill in the blank `___` keyword to derive subclass `Circle` from `Shape`.",
                 "class Circle ___ Shape { }", "extends",
                 "Java uses 'extends' for class inheritance.", "Inheritance Keyword", "Single Inheritance with extends and super()"),
            jwrt("Create `class Alert { String level() { return \"Info\"; } }` and `class ErrorAlert extends Alert` overriding `level()` to return `\"High\"`.",
                 "class Alert {\n    String level() { return \"Info\"; }\n}\nclass ErrorAlert extends Alert {\n    \n}",
                 "class Alert {\n    String level() { return \"Info\"; }\n}\nclass ErrorAlert extends Alert {\n    @Override String level() { return \"High\"; }\n}",
                 {"java_wrapper": "class", "java_test_cases": [{"expr": "((Alert) new ErrorAlert()).level()", "expected": "High"}],
                  "required_pattern_groups": [[r"extends\s+Alert"], [r"level\s*\("]]},
                 "Override level() inside ErrorAlert extending Alert.", "Inheritance & Overriding", "Method Overriding and Polymorphism"),
        ]
    )

    add_pair(jv, "Collections and Exception Handling",
        [
            mcq("Which block runs whether or not an exception occurs inside `try`?", "catch", "finally", "throw", "default", "B",
                "finally always executes after try and any matching catch.", "Try-Catch-Finally Flow", "Handling Errors with try, catch, and finally"),
            pred("What does this Java try-catch snippet print?", "try {\n    int x = 10 / 0;\n    System.out.print(\"OK\");\n} catch (ArithmeticException e) {\n    System.out.print(\"Err\");\n}", "Err",
                 "10 / 0 throws ArithmeticException before print('OK'), triggering catch.", "Exception Catching Flow", "Handling Errors with try, catch, and finally"),
            jdbg("Fix `safeGet(ArrayList<String> list, int idx)` so out-of-bounds indices return `\"NA\"` safely.",
                 "static String safeGet(ArrayList<String> list, int idx) {\n    return list.get(idx);\n}",
                 "static String safeGet(ArrayList<String> list, int idx) {\n    if (idx < 0 || idx >= list.size()) return \"NA\";\n    return list.get(idx);\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "safeGet(new ArrayList<>(Arrays.asList(\"X\")), 5)", "expected": "NA"}, {"expr": "safeGet(new ArrayList<>(Arrays.asList(\"X\")), 0)", "expected": "X"}],
                  "required_pattern_groups": [[r"list\.size\s*\(\)|catch"]]},
                 "Check idx >= 0 && idx < list.size() (or catch IndexOutOfBoundsException).", "List Bounds Guard", "ArrayList and HashMap Basics"),
            fill("Fill in the blank `___` to safely retrieve key `k` from `HashMap<String, Integer> map` with default `0`.",
                 "int val = map.___(k, 0);", "getOrDefault",
                 "HashMap.getOrDefault(k, defaultVal) avoids null pointer dereferences.", "HashMap Safe Access", "ArrayList and HashMap Basics"),
            jwrt("Write `static int safeDiv(int a, int b)` using `try { return a / b; } catch (ArithmeticException e) { return 0; }`.",
                 "static int safeDiv(int a, int b) {\n    \n}",
                 "static int safeDiv(int a, int b) {\n    try { return a / b; }\n    catch (ArithmeticException e) { return 0; }\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "safeDiv(12, 3)", "expected": "4"}, {"expr": "safeDiv(10, 0)", "expected": "0"}],
                  "required_pattern_groups": [[r"try\s*\{"], [r"catch\s*\("]]},
                 "Catch ArithmeticException on division by zero and return 0.", "Exception Handling Pattern", "Handling Errors with try, catch, and finally"),
        ],
        [
            mcq("Which `ArrayList` method returns how many items are currently stored?", ".length", ".count()", ".size()", ".len()", "C",
                "Java collections such as ArrayList and HashMap expose .size().", "Collection Size Method", "ArrayList and HashMap Basics"),
            pred("What output does this HashMap snippet print?", "HashMap<String, Integer> m = new HashMap<>();\nm.put(\"a\", 5);\nm.put(\"a\", 9);\nSystem.out.println(m.get(\"a\"));", "9",
                 "Putting an existing key updates its mapped value cleanly.", "HashMap Key Overwrite", "ArrayList and HashMap Basics"),
            jdbg("Fix `safeParseZero(String s)` so non-numeric strings return `0` instead of crashing.",
                 "static int safeParseZero(String s) {\n    return Integer.parseInt(s);\n}",
                 "static int safeParseZero(String s) {\n    try { return Integer.parseInt(s); }\n    catch (NumberFormatException e) { return 0; }\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "safeParseZero(\"17\")", "expected": "17"}, {"expr": "safeParseZero(\"bad\")", "expected": "0"}],
                  "required_pattern_groups": [[r"Integer\.parseInt"], [r"catch"]]},
                 "Wrap Integer.parseInt(s) in try-catch catching NumberFormatException.", "Try-Catch Parse Protection", "Handling Errors with try, catch, and finally"),
            fill("Fill in the blank `___` keyword to manually raise an exception object.",
                 "if (age < 0) { ___ new IllegalArgumentException(); }", "throw",
                 "The 'throw' keyword raises an exception instance explicitly.", "Throwing Exceptions", "Handling Errors with try, catch, and finally"),
            jwrt("Write `static int mapCountOrDefault(HashMap<String, Integer> map, String key)` returning `map.getOrDefault(key, -1)`.",
                 "static int mapCountOrDefault(HashMap<String, Integer> map, String key) {\n    \n}",
                 "static int mapCountOrDefault(HashMap<String, Integer> map, String key) {\n    return map.getOrDefault(key, -1);\n}",
                 {"java_wrapper": "method", "java_test_cases": [{"expr": "mapCountOrDefault(new HashMap<>(), \"x\")", "expected": "-1"}],
                  "required_pattern_groups": [[r"getOrDefault|containsKey|null"]]},
                 "Use getOrDefault(key, -1) or check containsKey.", "Safe HashMap Lookup", "ArrayList and HashMap Basics"),
        ]
    )

    # =====================================================================
    # COURSE 4: AI/ML (6 Topics)
    # =====================================================================
    ai = "Artificial Intelligence and Machine Learning (AI/ML)"

    add_pair(ai, "Python Fundamentals",
        [
            mcq("What does `[x for x in [3, 8, 2, 9] if x > 4]` return?", "[3, 2]", "[8, 9]", "[True, True]", "[4, 5]", "B",
                "List comprehension filters values satisfying x > 4.", "Comprehension Filtering", "List Comprehensions and Inline Filtering"),
            pred("Predict the printed output.", "a = [1, 2, 3]\nb = [10, 20]\nprint(list(zip(a, b)))", "[(1, 10), (2, 20)]",
                 "zip() stops when the shorter sequence finishes.", "Zip Length Truncation", "Lambda Functions and Pairing with zip()"),
            dbg("Fix `double_evens(nums)` so it keeps ONLY even values and doubles them.",
                "def double_evens(nums):\n    return [x * 2 for x in nums if x % 2 == 1]",
                "def double_evens(nums):\n    return [x * 2 for x in nums if x % 2 == 0]",
                [{"call": "double_evens([1, 2, 3, 4])", "expected_return": [4, 8]}, {"call": "double_evens([1, 5])", "expected_return": []}],
                "Even numbers satisfy x % 2 == 0.", "Modulo Filter Bug", "List Comprehensions and Inline Filtering"),
            fill("Fill in the blank `___` to sort `pairs` by each tuple's second element.",
                 "sorted(pairs, key=___ p: p[1])", "lambda",
                 "Inline key extractors are created concisely with 'lambda'.", "Lambda Key Expression", "Lambda Functions and Pairing with zip()"),
            wrt("Write `dot_zip(a, b)` computing the sum of `x * y` across corresponding pairs using `zip(a, b)`.",
                "def dot_zip(a, b):\n    pass",
                "def dot_zip(a, b):\n    return sum(x * y for x, y in zip(a, b))",
                [{"call": "dot_zip([2, 3], [4, 5])", "expected_return": 23}, {"call": "dot_zip([], [])", "expected_return": 0}],
                "Use sum(x * y for x, y in zip(a, b)).", "Zip Feature Aggregation", "Lambda Functions and Pairing with zip()"),
        ],
        [
            mcq("Which expression builds a list of word lengths from `words`?", "[len(w) for w in words]", "len(for w in words)", "[w.count for w in words]", "zip(words, len)", "A",
                "[len(w) for w in words] maps len() over every string.", "Comprehension Transformation", "List Comprehensions and Inline Filtering"),
            pred("What will this lambda sorted step print?", "items = [('b', 2), ('a', 10)]\nprint(min(items, key=lambda t: t[1])[0])", "b",
                 "Comparing second item 2 < 10 selects tuple ('b', 2), whose [0] is 'b'.", "Lambda Comparator", "Lambda Functions and Pairing with zip()"),
            dbg("Fix `filter_valid(rows)` so empty/whitespace-only strings are excluded.",
                "def filter_valid(rows):\n    return [r.strip() for r in rows if r]",
                "def filter_valid(rows):\n    return [r.strip() for r in rows if r.strip()]",
                [{"call": "filter_valid(['  hi ', '   ', 'ok'])", "expected_return": ["hi", "ok"]}],
                "'   ' is truthy until stripped; filter using if r.strip().", "Whitespace Filter Guard", "List Comprehensions and Inline Filtering"),
            fill("Fill in the blank `___` to combine keys `cols` and values `vals` into a dictionary.",
                 "row = dict(___(cols, vals))", "zip",
                 "dict(zip(keys, vals)) constructs a dictionary from parallel lists.", "Pairing Keys and Values", "Lambda Functions and Pairing with zip()"),
            wrt("Write `positive_ratios(nums, total)` returning `[round(x / total, 2) for x in nums if x > 0]`.",
                "def positive_ratios(nums, total):\n    pass",
                "def positive_ratios(nums, total):\n    return [round(x / total, 2) for x in nums if x > 0]",
                [{"call": "positive_ratios([-2, 5, 10], 20)", "expected_return": [0.25, 0.5]}, {"call": "positive_ratios([0, -1], 5)", "expected_return": []}],
                "Filter x > 0 and round ratio to 2 decimal places.", "Filtered Comprehension", "List Comprehensions and Inline Filtering"),
        ]
    )

    add_pair(ai, "Mathematics and Statistics Basics",
        [
            mcq("Which statistic is LEAST sensitive to a single huge outlier value?", "Mean", "Range", "Median", "Variance", "C",
                "Median picks the center sorted value and ignores extreme tail magnitude.", "Outlier Robustness", "Mean, Median, Variance, and Standard Deviation"),
            pred("What output does this dot product print?", "w = [2, -1, 3]\nx = [4, 5, 1]\nprint(sum(a * b for a, b in zip(w, x)))", "6",
                 "2*4 + (-1)*5 + 3*1 = 8 - 5 + 3 = 6.", "Vector Dot Product", "Dot Products and Gradient Descent Intuition"),
            dbg("Fix `grad_step(weight, grad, lr)` so gradient descent moves OPPOSITE to the gradient.",
                "def grad_step(weight, grad, lr):\n    return weight + lr * grad",
                "def grad_step(weight, grad, lr):\n    return weight - lr * grad",
                [{"call": "grad_step(5.0, 2.0, 0.5)", "expected_return": 4.0}, {"call": "grad_step(2.0, -4.0, 0.25)", "expected_return": 3.0}],
                "Gradient descent subtracts lr * grad: weight - lr * grad.", "Gradient Descent Sign Error", "Dot Products and Gradient Descent Intuition"),
            fill("Fill in the blank `___` to compute standard deviation from precomputed variance `var`.",
                 "std = var ** ___", "0.5",
                 "Standard deviation is the square root of variance (var ** 0.5).", "Standard Deviation Formula", "Mean, Median, Variance, and Standard Deviation"),
            wrt("Write `variance_pop(vals)` returning population variance `sum((x - mean) ** 2 for x in vals) / len(vals)`.",
                "def variance_pop(vals):\n    pass",
                "def variance_pop(vals):\n    mu = sum(vals) / len(vals)\n    return sum((x - mu) ** 2 for x in vals) / len(vals)",
                [{"call": "variance_pop([2, 4, 4, 4, 5, 5, 7, 9])", "expected_return": 4.0}, {"call": "variance_pop([10, 10])", "expected_return": 0.0}],
                "Compute mean first, then average squared deviations from mean.", "Population Variance Calculation", "Mean, Median, Variance, and Standard Deviation"),
        ],
        [
            mcq("What happens when gradient descent uses an excessively large learning rate?", "Faster exact convergence", "Updates can overshoot the minimum and diverge", "Zero gradients", "Reduces training data", "B",
                "Too large a step size jumps past the valley and can diverge.", "Learning Rate Tuning", "Dot Products and Gradient Descent Intuition"),
            pred("What will this statistics calculation print?", "v = [12, 4, 8]\ns = sorted(v)\nprint(s[len(s)//2])", "8",
                 "Sorted list [4, 8, 12] has middle value 8.", "Median Lookup", "Mean, Median, Variance, and Standard Deviation"),
            dbg("Fix `weighted_sum(xs, ws)` so each feature `x` multiplies its matching weight `w`.",
                "def weighted_sum(xs, ws):\n    return sum(xs) * sum(ws)",
                "def weighted_sum(xs, ws):\n    return sum(x * w for x, w in zip(xs, ws))",
                [{"call": "weighted_sum([2, 3], [5, 1])", "expected_return": 13}, {"call": "weighted_sum([1, 0], [4, 9])", "expected_return": 4}],
                "Dot product pairs elements first instead of multiplying sums.", "Dot Product Pairing", "Dot Products and Gradient Descent Intuition"),
            fill("Fill in the blank `___` in the gradient descent update equation.",
                 "w_new = w - lr * ___", "grad",
                 "Each step subtracts learning rate times gradient.", "Weight Update Rule", "Dot Products and Gradient Descent Intuition", ["gradient"]),
            wrt("Write `cosine_dot_norm(a, b)` returning `dot(a, b) / ((sum(x*x for x in a)**0.5) * (sum(y*y for y in b)**0.5))` rounded to 2 decimals.",
                "def cosine_dot_norm(a, b):\n    pass",
                "def cosine_dot_norm(a, b):\n    dot = sum(x * y for x, y in zip(a, b))\n    na = sum(x * x for x in a) ** 0.5\n    nb = sum(y * y for y in b) ** 0.5\n    return round(dot / (na * nb), 2) if (na and nb) else 0.0",
                [{"call": "cosine_dot_norm([1, 0], [1, 0])", "expected_return": 1.0}, {"call": "cosine_dot_norm([1, 0], [0, 5])", "expected_return": 0.0}],
                "Divide dot product by product of vector L2 norms.", "Vector Similarity & Norms", "Dot Products and Gradient Descent Intuition"),
        ]
    )

    add_pair(ai, "NumPy and Pandas",
        [
            mcq("What Pandas method replaces `NaN` missing cells with a chosen value?", "dropna()", "fillna()", "isna()", "drop_duplicates()", "B",
                "fillna(value) imputes missing cells in place of NaN.", "Missing Value Imputation", "Pandas Series, DataFrames, and Missing Values"),
            pred("What does this element-wise array simulation print?", "grid = [[1, 2], [3, 4]]\nflat_scaled = [x + 1 for row in grid for x in row]\nprint(sum(flat_scaled))", "14",
                 "(1+1)+(2+1)+(3+1)+(4+1) = 2+3+4+5 = 14.", "Broadcasting Sum", "NumPy Arrays, Shapes, and Broadcasting"),
            dbg("Fix `filter_rows(rows, min_score)` so dictionaries missing `'score'` don't crash with `KeyError`.",
                "def filter_rows(rows, min_score):\n    return [r for r in rows if r['score'] >= min_score]",
                "def filter_rows(rows, min_score):\n    return [r for r in rows if r.get('score') is not None and r['score'] >= min_score]",
                [{"call": "filter_rows([{'score': 80}, {'id': 2}, {'score': None}], 70)", "expected_return": [{"score": 80}]}],
                "Verify key exists and is not None before comparing >= min_score.", "Null Row Handling", "Pandas Series, DataFrames, and Missing Values"),
            fill("Fill in the blank `___` DataFrame method to drop any rows containing missing values.",
                 "clean_df = df.___()", "dropna",
                 "df.dropna() removes rows containing null/NaN cells.", "Dropping Missing Rows", "Pandas Series, DataFrames, and Missing Values"),
            wrt("Write `fill_missing_median(vals)` replacing `None` entries with the integer/float median of non-None values.",
                "def fill_missing_median(vals):\n    pass",
                "def fill_missing_median(vals):\n    clean = sorted(v for v in vals if v is not None)\n    m = len(clean) // 2\n    med = clean[m] if len(clean) % 2 == 1 else (clean[m - 1] + clean[m]) / 2\n    return [med if v is None else v for v in vals]",
                [{"call": "fill_missing_median([10, None, 30, 20])", "expected_return": [10, 20, 30, 20]}],
                "Compute median of valid items and substitute wherever None appears.", "Tabular Median Imputation", "Pandas Series, DataFrames, and Missing Values"),
        ],
        [
            mcq("Which NumPy attribute inspects array dimensions like `(100, 4)`?", ".size", ".shape", ".dtype", ".len", "B",
                "ndarray.shape returns a tuple describing the array's row/column dimensions.", "Array Shape Inspection", "NumPy Arrays, Shapes, and Broadcasting"),
            pred("What output does this 2D row count print?", "table = [{'a': 1}, {'a': 2}, {'a': 3}]\nprint(len([r for r in table if r['a'] > 1]))", "2",
                 "Boolean filtering retains 2 rows where a > 1.", "Boolean Mask Filtering", "Pandas Series, DataFrames, and Missing Values"),
            dbg("Fix `scale_vector(vec, factor)` so each entry multiplies by `factor` instead of duplicating the list.",
                "def scale_vector(vec, factor):\n    return vec * factor",
                "def scale_vector(vec, factor):\n    return [x * factor for x in vec]",
                [{"call": "scale_vector([2, 5], 3)", "expected_return": [6, 15]}],
                "Python list * int repeats the list; scale element-wise like NumPy broadcasting.", "List Repeat vs Broadcast Math", "NumPy Arrays, Shapes, and Broadcasting"),
            fill("Fill in the blank `___` to check for null entries in a Pandas DataFrame.",
                 "missing_mask = df.___()", "isna",
                 "df.isna() (or df.isnull()) marks null cells as True.", "Null Detection Method", "Pandas Series, DataFrames, and Missing Values", ["isnull"]),
            wrt("Write `col_means(rows, keys)` returning a dict `{k: round(avg, 2)}` ignoring `None` values per key.",
                "def col_means(rows, keys):\n    pass",
                "def col_means(rows, keys):\n    out = {}\n    for k in keys:\n        vals = [r[k] for r in rows if r.get(k) is not None]\n        out[k] = round(sum(vals) / len(vals), 2) if vals else 0.0\n    return out",
                [{"call": "col_means([{'x': 2, 'y': 10}, {'x': 4, 'y': None}], ['x', 'y'])", "expected_return": {"x": 3.0, "y": 10.0}}],
                "Aggregate each column skipping None entries.", "Column Aggregation", "Pandas Series, DataFrames, and Missing Values"),
        ]
    )

    add_pair(ai, "Data Preprocessing and Visualization",
        [
            mcq("What bug occurs if you compute `mean` and `std` across the combined train+test set before splitting?", "Underfitting", "Data leakage", "Zero variance", "Class imbalance", "B",
                "Using test statistics during scaling leaks test distribution info into training.", "Data Leakage Prevention", "Train-Test Splitting and Data Visualization"),
            pred("What does Min-Max scaling produce here?", "x, mn, mx = 25, 10, 40\nprint((x - mn) / (mx - mn))", "0.5",
                 "(25 - 10) / (40 - 10) = 15 / 30 = 0.5.", "Min-Max Scaling Math", "Feature Scaling and One-Hot Encoding"),
            dbg("Fix `minmax_scale(vals)` so zero range (`mx == mn`) returns zeros cleanly without `ZeroDivisionError`.",
                "def minmax_scale(vals):\n    mn, mx = min(vals), max(vals)\n    return [(x - mn) / (mx - mn) for x in vals]",
                "def minmax_scale(vals):\n    if not vals: return []\n    mn, mx = min(vals), max(vals)\n    if mx == mn: return [0.0 for _ in vals]\n    return [round((x - mn) / (mx - mn), 4) for x in vals]",
                [{"call": "minmax_scale([5, 5, 5])", "expected_return": [0.0, 0.0, 0.0]}, {"call": "minmax_scale([10, 20, 30])", "expected_return": [0.0, 0.5, 1.0]}],
                "Guard constant features where max == min before dividing.", "Zero Range Scaling Guard", "Feature Scaling and One-Hot Encoding"),
            fill("Fill in the blank `___` encoding technique converts unordered categories like `['red', 'blue']` into binary indicator columns.",
                 "# Categorical conversion without fake numeric ordering: ___ Encoding", "One-Hot",
                 "One-Hot Encoding gives each category an independent 0/1 feature column.", "Categorical Encoding", "Feature Scaling and One-Hot Encoding", ["One Hot", "one-hot"]),
            wrt("Write `one_hot_encode(val, categories)` returning a binary list `[1 if c == val else 0 for c in categories]`.",
                "def one_hot_encode(val, categories):\n    pass",
                "def one_hot_encode(val, categories):\n    return [1 if c == val else 0 for c in categories]",
                [{"call": "one_hot_encode('B', ['A', 'B', 'C'])", "expected_return": [0, 1, 0]}, {"call": "one_hot_encode('Z', ['A', 'B'])", "expected_return": [0, 0]}],
                "Place 1 at the matching category index and 0 elsewhere.", "One-Hot Vector Construction", "Feature Scaling and One-Hot Encoding"),
        ],
        [
            mcq("Which chart best checks the frequency distribution of a single continuous variable?", "Pie chart", "Histogram", "Line plot over IDs", "Confusion matrix", "B",
                "Histograms bin numeric ranges to reveal distribution shape and skew.", "Distribution Visualization", "Train-Test Splitting and Data Visualization"),
            pred("What does this z-score standardization snippet print?", "x, mu, sd = 6, 10, 2\nprint((x - mu) / sd)", "-2.0",
                 "(6 - 10) / 2 = -2.0.", "Standardization Calculation", "Feature Scaling and One-Hot Encoding"),
            dbg("Fix `split_train_test(items, train_ratio)` so slice splits train and test without dropping the boundary item.",
                "def split_train_test(items, train_ratio):\n    cut = int(len(items) * train_ratio)\n    return {'train': items[:cut], 'test': items[cut+1:]}",
                "def split_train_test(items, train_ratio):\n    cut = int(len(items) * train_ratio)\n    return {'train': items[:cut], 'test': items[cut:]}",
                [{"call": "split_train_test([1, 2, 3, 4, 5], 0.6)", "expected_return": {"train": [1, 2, 3], "test": [4, 5]}}],
                "Test split starts right at index cut (items[cut:]), not cut + 1.", "Train-Test Slice Boundary", "Train-Test Splitting and Data Visualization"),
            fill("Fill in the blank `___` to normalize `x` using precomputed training `mn` and `mx`.",
                 "scaled_x = (x - mn) / (___ - mn)", "mx",
                 "Min-Max scaling divides by full range (mx - mn).", "Scaler Range Formula", "Feature Scaling and One-Hot Encoding"),
            wrt("Write `apply_train_zscore(train, test)` fitting mean and std ONLY on `train`, and returning standardized `test` rounded to 2 decimals.",
                "def apply_train_zscore(train, test):\n    pass",
                "def apply_train_zscore(train, test):\n    mu = sum(train) / len(train)\n    sd = (sum((x - mu) ** 2 for x in train) / len(train)) ** 0.5\n    return [round((x - mu) / sd, 2) for x in test]",
                [{"call": "apply_train_zscore([10, 20], [15, 25])", "expected_return": [0.0, 2.0]}],
                "Compute mu and sd from train only, then transform test.", "Leakage-Free Standardization", "Train-Test Splitting and Data Visualization"),
        ]
    )

    add_pair(ai, "Machine Learning Algorithms",
        [
            mcq("Which task predicts a continuous numeric price rather than a discrete category?", "Clustering", "Regression", "Classification", "Dimensionality reduction", "B",
                "Regression predicts continuous values; classification predicts discrete classes.", "Regression vs Classification", "Supervised Learning: Linear Models & Decision Trees"),
            pred("What class does this thresholded probability produce?", "prob = 0.38\nprint('Pos' if prob >= 0.5 else 'Neg')", "Neg",
                 "0.38 < 0.5 predicts negative class.", "Decision Threshold", "Supervised Learning: Linear Models & Decision Trees"),
            dbg("Fix `linear_predict(weights, features, bias)` so it sums weighted products plus `bias`.",
                "def linear_predict(weights, features, bias):\n    return sum(w + x for w, x in zip(weights, features)) + bias",
                "def linear_predict(weights, features, bias):\n    return sum(w * x for w, x in zip(weights, features)) + bias",
                [{"call": "linear_predict([2, -1], [3, 4], 1)", "expected_return": 3}, {"call": "linear_predict([0, 5], [10, 2], 0)", "expected_return": 10}],
                "Multiply weights by features (w * x), don't add them.", "Linear Model Formula Bug", "Supervised Learning: Linear Models & Decision Trees"),
            fill("Fill in the blank `___` to name the unsupervised algorithm that groups unlabeled points around K centroids.",
                 "# Unsupervised centroid clustering algorithm: ___", "K-Means",
                 "K-Means assigns each point to its nearest cluster centroid.", "Clustering Algorithm", "Unsupervised Learning: K-Means Clustering", ["kmeans", "KMeans", "k-means"]),
            wrt("Write `classify_threshold(probs, cutoff=0.5)` returning list of `1` where `p >= cutoff` else `0`.",
                "def classify_threshold(probs, cutoff=0.5):\n    pass",
                "def classify_threshold(probs, cutoff=0.5):\n    return [1 if p >= cutoff else 0 for p in probs]",
                [{"call": "classify_threshold([0.2, 0.5, 0.85])", "expected_return": [0, 1, 1]}, {"call": "classify_threshold([0.6], 0.7)", "expected_return": [0]}],
                "Map probabilities to 1/0 using >= cutoff.", "Binary Classification Threshold", "Supervised Learning: Linear Models & Decision Trees"),
        ],
        [
            mcq("How does a Random Forest reduce variance compared to a single Decision Tree?", "Uses no splits", "Averages predictions across multiple diverse trees", "Deletes training rows", "Assumes linear boundaries", "B",
                "Ensemble averaging across trees stabilizes predictions and limits overfitting.", "Ensemble Variance Reduction", "Supervised Learning: Linear Models & Decision Trees"),
            pred("Predict which centroid index wins in 1D distance.", "pt = 4\ncenters = [1, 10]\nbest = min(range(len(centers)), key=lambda i: abs(pt - centers[i]))\nprint(best)", "0",
                 "|4 - 1| = 3 < |4 - 10| = 6, so centroid index 0 wins.", "Nearest Centroid Selection", "Unsupervised Learning: K-Means Clustering"),
            dbg("Fix `mse_loss(y_true, y_pred)` so errors are squared rather than left signed.",
                "def mse_loss(y_true, y_pred):\n    return sum((t - p) for t, p in zip(y_true, y_pred)) / len(y_true)",
                "def mse_loss(y_true, y_pred):\n    return sum((t - p) ** 2 for t, p in zip(y_true, y_pred)) / len(y_true)",
                [{"call": "mse_loss([3, 5], [1, 7])", "expected_return": 4.0}, {"call": "mse_loss([2, 2], [2, 2])", "expected_return": 0.0}],
                "Mean Squared Error squares each residual: (t - p) ** 2.", "MSE Squaring Error", "Supervised Learning: Linear Models & Decision Trees"),
            fill("Fill in the blank `___` learning type trains models using unlabeled inputs without target labels.",
                 "# Learning without target labels y: ___ learning", "Unsupervised",
                 "Unsupervised learning discovers hidden clusters or structure without y labels.", "Learning Paradigms", "Unsupervised Learning: K-Means Clustering", ["unsupervised"]),
            wrt("Write `assign_cluster_1d(vals, c0, c1)` returning list of `0` if `abs(v - c0) <= abs(v - c1)` else `1`.",
                "def assign_cluster_1d(vals, c0, c1):\n    pass",
                "def assign_cluster_1d(vals, c0, c1):\n    return [0 if abs(v - c0) <= abs(v - c1) else 1 for v in vals]",
                [{"call": "assign_cluster_1d([2, 9, 5], 1, 10)", "expected_return": [0, 1, 0]}],
                "Assign each point to closer centroid c0 or c1.", "K-Means Assignment Step", "Unsupervised Learning: K-Means Clustering"),
        ]
    )

    add_pair(ai, "Model Evaluation and AI Projects",
        [
            mcq("When a model scores 99% on training data and 52% on unseen test data, what is occurring?", "Underfitting", "Overfitting", "Zero variance", "Regularization", "B",
                "High training accuracy paired with poor test generalization indicates overfitting.", "Overfitting Diagnosis", "Overfitting vs Underfitting and Project Workflow"),
            pred("What Precision prints from these confusion values?", "tp, fp = 12, 4\nprint(tp / (tp + fp))", "0.75",
                 "Precision = 12 / (12 + 4) = 0.75.", "Precision Calculation", "Confusion Matrix, Precision, Recall, and F1"),
            dbg("Fix `f1_score(prec, rec)` so both denominator check and `2 * p * r / (p + r)` work properly.",
                "def f1_score(prec, rec):\n    return (prec + rec) / 2",
                "def f1_score(prec, rec):\n    return (2 * prec * rec) / (prec + rec) if (prec + rec) else 0.0",
                [{"call": "f1_score(0.8, 0.5)", "expected_return": 0.6153846153846154}, {"call": "f1_score(0.0, 0.0)", "expected_return": 0.0}],
                "F1 is harmonic mean 2 * P * R / (P + R), not arithmetic mean.", "Harmonic F1 Mean Bug", "Confusion Matrix, Precision, Recall, and F1"),
            fill("Fill in the blank `___` to complete the Recall denominator: `tp / (tp + ___)`.",
                 "recall = tp / (tp + ___)", "fn",
                 "Recall measures caught positives: TP / (TP + FN).", "Recall Formula", "Confusion Matrix, Precision, Recall, and F1", ["FN"]),
            wrt("Write `accuracy_score(y_true, y_pred)` computing fraction of matching pairs `round(matches / len(y_true), 2)`.",
                "def accuracy_score(y_true, y_pred):\n    pass",
                "def accuracy_score(y_true, y_pred):\n    if not y_true: return 0.0\n    ok = sum(1 for t, p in zip(y_true, y_pred) if t == p)\n    return round(ok / len(y_true), 2)",
                [{"call": "accuracy_score([1, 0, 1, 1], [1, 1, 1, 1])", "expected_return": 0.75}, {"call": "accuracy_score([0, 0], [0, 0])", "expected_return": 1.0}],
                "Count matches t == p and divide by total samples.", "Classification Accuracy", "Confusion Matrix, Precision, Recall, and F1"),
        ],
        [
            mcq("Why can raw Accuracy mislead on an imbalanced dataset where 98% of cases are negative?", "Model ignores positives yet still gets 98% accuracy", "Accuracy fails on binary labels", "Precision is always 100%", "Recall becomes negative", "A",
                "Predicting the majority class all the time yields 98% accuracy despite missing every positive.", "Imbalanced Metric Pitfall", "Confusion Matrix, Precision, Recall, and F1"),
            pred("What generalization gap prints here?", "train_acc, test_acc = 0.96, 0.71\nprint(round(train_acc - test_acc, 2))", "0.25",
                 "0.96 - 0.71 = 0.25 accuracy drop on unseen data.", "Train-Test Gap Check", "Overfitting vs Underfitting and Project Workflow"),
            dbg("Fix `precision(tp, fp)` so zero positive predictions returns `0.0` instead of crashing.",
                "def precision(tp, fp):\n    return tp / (tp + fp)",
                "def precision(tp, fp):\n    return tp / (tp + fp) if (tp + fp) > 0 else 0.0",
                [{"call": "precision(0, 0)", "expected_return": 0.0}, {"call": "precision(9, 1)", "expected_return": 0.9}],
                "Guard tp + fp == 0 before dividing.", "Zero Division Metric Guard", "Confusion Matrix, Precision, Recall, and F1"),
            fill("Fill in the blank `___` term describes a model too simple to capture patterns in either training or test data.",
                 "# Low accuracy on both train and test sets indicates ___", "Underfitting",
                 "Underfitting fails to learn underlying trends even on training data.", "Underfitting Diagnosis", "Overfitting vs Underfitting and Project Workflow", ["underfitting"]),
            wrt("Write `specificity(tn, fp)` computing True Negative Rate `tn / (tn + fp)` cleanly (0.0 if denominator 0).",
                "def specificity(tn, fp):\n    pass",
                "def specificity(tn, fp):\n    return tn / (tn + fp) if (tn + fp) > 0 else 0.0",
                [{"call": "specificity(8, 2)", "expected_return": 0.8}, {"call": "specificity(0, 0)", "expected_return": 0.0}],
                "Return tn / (tn + fp) guarded against zero denominator.", "Confusion Rate Computation", "Confusion Matrix, Precision, Recall, and F1"),
        ]
    )

    return rows


def seed_default_questions(db_path=None):
    """Populates all initial multi-type & practice questions if assessment_questions is empty."""
    conn = get_db_connection(db_path)
    c = conn.cursor()

    courses_map = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM courses").fetchall()}
    topic_map = {(r["course_id"], r["name"]): r["id"] for r in c.execute("SELECT id, course_id, name FROM topics").fetchall()}

    all_questions = build_curriculum_question_bank(courses_map, topic_map)
    c.execute("DELETE FROM assessment_questions")
    c.executemany("""
        INSERT INTO assessment_questions (
            course_id, topic_id, question_number, question_text,
            option_a, option_b, option_c, option_d, correct_answer,
            question_type, code_snippet, evaluation_spec_json, question_pool
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, all_questions)

    conn.commit()
    conn.close()
    return len(all_questions)


def upgrade_question_bank_if_needed(db_path=None):
    """
    Safely upgrades the question bank if missing multi-type questions, practice pool,
    or mastery_1 / mastery_2 sets. Never touches user accounts, results, or progress.
    """
    conn = get_db_connection(db_path)
    total = conn.execute("SELECT COUNT(*) FROM assessment_questions").fetchone()[0]
    distinct_types = conn.execute("SELECT COUNT(DISTINCT question_type) FROM assessment_questions").fetchone()[0]
    practice_cnt = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE question_pool = 'practice'").fetchone()[0]
    m1_cnt = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE question_pool = 'mastery_1'").fetchone()[0]
    m2_cnt = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE question_pool = 'mastery_2'").fetchone()[0]
    fill_cnt = conn.execute("SELECT COUNT(*) FROM assessment_questions WHERE question_type = 'fill_blank'").fetchone()[0]
    conn.close()

    if total == 0 or distinct_types < 4 or practice_cnt == 0 or m1_cnt < 125 or m2_cnt < 125 or fill_cnt == 0:
        return seed_default_questions(db_path)
    return 0


def verify_question_bank_reference_solutions():
    """
    Self-verification audit: Evaluates every question's canonical correct_answer across
    all 4 courses and both pools to guarantee 100% pass rate with zero broken specs.
    """
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM assessment_questions ORDER BY course_id, question_pool, question_number").fetchall()
    conn.close()
    questions = [dict(r) for r in rows]
    answers = {str(q["id"]): q["correct_answer"] for q in questions}
    confs = {str(q["id"]): "high" for q in questions}
    res = calculate_topic_scores(questions, answers, user_confidences=confs)
    failures = [it for it in res["item_results"] if not it["is_correct"]]
    return len(questions), len(failures), failures


if __name__ == "__main__":
    seed_default_questions()
    total, fail_cnt, failures = verify_question_bank_reference_solutions()
    print(f"Verified {total} questions: {total - fail_cnt} passed, {fail_cnt} failed.")
    for f in failures:
        print("  FAIL:", f["question_id"], f["question_type"], f["question_text"][:50], "->", f["feedback"])
