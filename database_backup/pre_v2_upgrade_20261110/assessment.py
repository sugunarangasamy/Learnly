"""
assessment.py - Learnly Assessment & Question Bank Layer
========================================================
Responsibilities:
  - 80 Multiple-Choice Questions definition (20 for each of the 4 courses)
  - Question seeding utility (seed_default_questions)
  - Knowledge classification logic (Strong >= 80%, Moderate >= 50%, Weak < 50%)
  - Objective scoring per topic and overall percentage calculation
  - Question sanitization (ensuring correct answers are never sent to the client browser)
"""

import os
import sqlite3
from database import get_db_connection, DEFAULT_DATABASE_PATH


def classify_knowledge_level(score_percentage):
    """
    Standard educational mastery threshold rule:
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
                "topic_name": q.get("topic_name", f"Topic {t_id}"),
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
            # user_answers can be keyed by question_number (1-indexed), str or int, or list
            if isinstance(user_answers, dict):
                given_ans = user_answers.get(str(q_num)) if str(q_num) in user_answers else user_answers.get(q_num, "")
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


def evaluate_assessment(course_id, questions, user_answers):
    """
    Executes scoring and returns complete summary for saving and UI rendering.
    """
    result = calculate_topic_scores(questions, user_answers)
    result["course_id"] = course_id
    return result


def sanitize_questions_for_client(questions):
    """
    Security safeguard: Removes 'correct_answer' from questions before
    sending JSON to the client browser.
    """
    sanitized = []
    for q in questions:
        sanitized.append({
            "id": q["id"],
            "number": q["question_number"],
            "text": q["question_text"],
            "options": {
                "A": q["option_a"],
                "B": q["option_b"],
                "C": q["option_c"],
                "D": q["option_d"]
            },
            "topic_id": q["topic_id"],
            "topic_name": q.get("topic_name", "")
        })
    return sanitized


# =========================================================================
# 80-Question Curriculum Bank Seeder
# =========================================================================

def seed_default_questions(db_path=None):
    """
    Populates all 80 MCQs across the 4 courses if not already present.
    """
    conn = get_db_connection(db_path)
    c = conn.cursor()

    courses_rows = c.execute("SELECT id, name FROM courses").fetchall()
    courses_map = {row["name"]: row["id"] for row in courses_rows}

    topics_rows = c.execute("SELECT id, course_id, name FROM topics").fetchall()
    topic_map = {(row["course_id"], row["name"]): row["id"] for row in topics_rows}

    all_questions = []

    # 1. PYTHON PROGRAMMING
    python_id = courses_map.get("Python Programming")
    if python_id:
        def py_tid(name):
            return topic_map.get((python_id, name))

        all_questions.extend([
            (python_id, py_tid("Variables and Data Types"), 1, "Which data type is used to store textual data in Python?", "int", "float", "str", "bool", "C"),
            (python_id, py_tid("Variables and Data Types"), 2, "What is the output of type(3.14) in Python?", "<class 'int'>", "<class 'float'>", "<class 'double'>", "<class 'number'>", "B"),
            (python_id, py_tid("Variables and Data Types"), 3, "Which of the following is a valid Python variable name?", "2nd_number", "user_score", "user-score", "class", "B"),
            (python_id, py_tid("Operators and Conditions"), 4, "What is the result of 10 // 3 in Python?", "3.33", "3", "1", "3.0", "B"),
            (python_id, py_tid("Operators and Conditions"), 5, "Which operator checks equality between two operands in Python?", "=", "==", "===", "!=", "B"),
            (python_id, py_tid("Operators and Conditions"), 6, "Which keyword is used for 'else if' conditional branching in Python?", "else if", "elseif", "elif", "case", "C"),
            (python_id, py_tid("Loops"), 7, "What sequence of numbers does range(1, 5) produce in Python?", "1, 2, 3, 4, 5", "1, 2, 3, 4", "0, 1, 2, 3, 4", "0, 1, 2, 3, 4, 5", "B"),
            (python_id, py_tid("Loops"), 8, "Which statement immediately terminates the loop execution?", "continue", "pass", "exit", "break", "D"),
            (python_id, py_tid("Loops"), 9, "Which loop structure is guaranteed to iterate over iterable sequences such as lists?", "while loop", "for loop", "do-while loop", "repeat loop", "B"),
            (python_id, py_tid("Functions"), 10, "Which keyword is used to declare a function in Python?", "function", "func", "def", "lambda", "C"),
            (python_id, py_tid("Functions"), 11, "What keyword is used to return a result from a Python function?", "yield", "send", "return", "output", "C"),
            (python_id, py_tid("Functions"), 12, "What is the default return value of a function that does not include a return statement?", "0", "False", "None", "undefined", "C"),
            (python_id, py_tid("Lists, Tuples, Sets, and Dictionaries"), 13, "Which of the following collection types is immutable in Python?", "list", "dict", "set", "tuple", "D"),
            (python_id, py_tid("Lists, Tuples, Sets, and Dictionaries"), 14, "How do you retrieve the value for key 'age' from a dictionary called student?", "student('age')", "student.age", "student['age']", "student->age", "C"),
            (python_id, py_tid("Lists, Tuples, Sets, and Dictionaries"), 15, "Which method removes and returns the last element from a list?", "pop()", "remove()", "discard()", "delete()", "A"),
            (python_id, py_tid("Lists, Tuples, Sets, and Dictionaries"), 16, "Which collection type automatically removes duplicate entries?", "list", "set", "tuple", "dict", "B"),
            (python_id, py_tid("Object-Oriented Programming"), 17, "Which keyword is used to create a class in Python?", "class", "object", "type", "structure", "A"),
            (python_id, py_tid("Object-Oriented Programming"), 18, "What is the name of the constructor method in a Python class?", "__construct__()", "__init__()", "__new__()", "__create__()", "B"),
            (python_id, py_tid("Object-Oriented Programming"), 19, "What does the first parameter 'self' refer to in an instance method?", "The class blueprint", "The current instance of the object", "The global module", "The parent class", "B"),
            (python_id, py_tid("Object-Oriented Programming"), 20, "Which OOP concept enables a class to acquire methods and fields from a base class?", "Encapsulation", "Polymorphism", "Inheritance", "Abstraction", "C")
        ])

    # 2. DATA STRUCTURES
    ds_id = courses_map.get("Data Structures")
    if ds_id:
        def ds_tid(name):
            return topic_map.get((ds_id, name))

        all_questions.extend([
            (ds_id, ds_tid("Programming Basics"), 1, "Which data structure follows the LIFO (Last In First Out) principle?", "Queue", "Stack", "Array", "Linked List", "B"),
            (ds_id, ds_tid("Programming Basics"), 2, "What is the time complexity of accessing an array element using its index?", "O(n)", "O(log n)", "O(1)", "O(n²)", "C"),
            (ds_id, ds_tid("Programming Basics"), 3, "Which data structure stores elements in contiguous memory locations?", "Array", "Graph", "Tree", "Hash Table", "A"),
            (ds_id, ds_tid("Arrays and Strings"), 4, "What is the index of the first element in a typical 0-indexed array?", "1", "-1", "0", "2", "C"),
            (ds_id, ds_tid("Arrays and Strings"), 5, "Which operation adds an element to the end of a dynamic array list?", "append()", "remove()", "sort()", "pop()", "A"),
            (ds_id, ds_tid("Arrays and Strings"), 6, "Which of the following represents an immutable sequence of characters in Python?", "List", "Array", "String", "Set", "C"),
            (ds_id, ds_tid("Linked Lists"), 7, "What does a node in a singly linked list typically contain?", "Data and a link to the next node", "Only data", "Only an index", "Data and two array indices", "A"),
            (ds_id, ds_tid("Linked Lists"), 8, "Which operation is generally O(1) in a singly linked list if head is known?", "Searching for a value", "Inserting a node at the head", "Accessing the last node by index", "Sorting all nodes", "B"),
            (ds_id, ds_tid("Linked Lists"), 9, "What does the next pointer of the last node in a singly linked list usually contain?", "0", "The first node", "NULL / None", "The previous node", "C"),
            (ds_id, ds_tid("Stacks and Queues"), 10, "Which principle does a standard queue follow?", "LIFO", "FIFO", "Random order", "Sorted order", "B"),
            (ds_id, ds_tid("Stacks and Queues"), 11, "Which operation removes the top element from a stack?", "Push", "Peek", "Enqueue", "Pop", "D"),
            (ds_id, ds_tid("Stacks and Queues"), 12, "Which operation adds an element to the back of a queue?", "Dequeue", "Pop", "Enqueue", "Peek", "C"),
            (ds_id, ds_tid("Recursion Basics"), 13, "What is recursion in programming?", "A function calling itself", "A loop running once", "A variable changing its type", "A program ending immediately", "A"),
            (ds_id, ds_tid("Recursion Basics"), 14, "Why is a base case critical in recursive functions?", "It increases memory usage", "It prevents infinite recursion and stack overflow", "It sorts the input", "It allocates a linked list", "B"),
            (ds_id, ds_tid("Recursion Basics"), 15, "What is the mathematical result of factorial(4)?", "10", "16", "24", "12", "C"),
            (ds_id, ds_tid("Trees and Binary Search Trees"), 16, "What is the topmost node of a tree hierarchy called?", "Leaf", "Child", "Edge", "Root", "D"),
            (ds_id, ds_tid("Trees and Binary Search Trees"), 17, "In a Binary Search Tree (BST), values smaller than a node are placed in which subtree?", "Left subtree", "Right subtree", "Root only", "No subtree", "A"),
            (ds_id, ds_tid("Graphs and Hashing"), 18, "What are the two fundamental components that comprise a graph?", "Arrays and indices", "Stacks and queues", "Vertices (nodes) and edges", "Roots and leaves only", "C"),
            (ds_id, ds_tid("Graphs and Hashing"), 19, "Which traversal algorithm visits graph vertices level by level using a queue?", "Binary Search", "Breadth-First Search (BFS)", "Insertion Sort", "Quick Sort", "B"),
            (ds_id, ds_tid("Graphs and Hashing"), 20, "What is the primary role of a hash function in a hash table?", "To sort all elements", "To reverse the data", "To connect linked-list nodes", "To map a key to an index in constant average time", "D")
        ])

    # 3. JAVA PROGRAMMING
    java_id = courses_map.get("Java Programming")
    if java_id:
        def java_tid(name):
            return topic_map.get((java_id, name))

        all_questions.extend([
            (java_id, java_tid("Java Syntax and Data Types"), 1, "Which keyword is used to define a constant variable in Java?", "const", "static", "final", "immutable", "C"),
            (java_id, java_tid("Java Syntax and Data Types"), 2, "What is the size of an int data type in Java?", "8 bits", "16 bits", "32 bits", "64 bits", "C"),
            (java_id, java_tid("Java Syntax and Data Types"), 3, "Which entrypoint signature is required for a standalone Java application?", "public void main()", "public static void main(String[] args)", "void start(String[] args)", "static int main()", "B"),
            (java_id, java_tid("Operators and Control Statements"), 4, "Which operator is used for logical AND in Java conditional expressions?", "&", "&&", "AND", "|", "B"),
            (java_id, java_tid("Operators and Control Statements"), 5, "Which statement is used to jump to a specific label in a switch construct?", "case", "when", "match", "branch", "A"),
            (java_id, java_tid("Operators and Control Statements"), 6, "What is the result of ternary expression (5 > 2) ? 10 : 20 in Java?", "5", "2", "10", "20", "C"),
            (java_id, java_tid("Loops and Arrays"), 7, "How do you determine the number of elements in a Java array named arr?", "arr.length()", "arr.length", "arr.size()", "arr.count", "B"),
            (java_id, java_tid("Loops and Arrays"), 8, "Which loop evaluates its condition after executing the loop body at least once?", "for loop", "while loop", "do-while loop", "enhanced for loop", "C"),
            (java_id, java_tid("Loops and Arrays"), 9, "What exception is thrown if you access index 5 of an array with 3 elements?", "NullPointerException", "ArrayIndexOutOfBoundsException", "IllegalArgumentException", "IndexError", "B"),
            (java_id, java_tid("Methods and Classes"), 10, "Which keyword instantiates an object from a Java class?", "create", "alloc", "new", "make", "C"),
            (java_id, java_tid("Methods and Classes"), 11, "Which access modifier restricts visibility strictly to the declaring class?", "public", "protected", "default", "private", "D"),
            (java_id, java_tid("Methods and Classes"), 12, "What is method overloading in Java?", "Same method name with different parameter lists", "Subclass overriding parent method", "Running method concurrently", "Calling method recursively", "A"),
            (java_id, java_tid("Inheritance and Polymorphism"), 13, "Which keyword is used by a class to inherit from a superclass in Java?", "implements", "extends", "inherits", "super", "B"),
            (java_id, java_tid("Inheritance and Polymorphism"), 14, "Which keyword invokes the superclass constructor or method from a subclass?", "this", "base", "super", "parent", "C"),
            (java_id, java_tid("Inheritance and Polymorphism"), 15, "Can a Java class directly inherit from multiple classes?", "Yes, with extends A, B", "No, Java supports single class inheritance", "Yes, using final", "Only abstract classes can", "B"),
            (java_id, java_tid("Inheritance and Polymorphism"), 16, "Which annotation indicates that a method overrides a superclass declaration?", "@Override", "@Inherit", "@Super", "@Implement", "A"),
            (java_id, java_tid("Collections and Exception Handling"), 17, "Which interface does ArrayList implement in Java?", "Map", "List", "Queue", "Tree", "B"),
            (java_id, java_tid("Collections and Exception Handling"), 18, "Which collection class stores key-value pairs without maintaining key insertion order?", "ArrayList", "HashSet", "HashMap", "LinkedList", "C"),
            (java_id, java_tid("Collections and Exception Handling"), 19, "Which block in a try-catch construct always executes regardless of exceptions?", "catch", "throw", "finally", "finalize", "C"),
            (java_id, java_tid("Collections and Exception Handling"), 20, "Which keyword explicitly triggers a new exception in Java?", "throws", "throw", "raise", "catch", "B")
        ])

    # 4. ARTIFICIAL INTELLIGENCE AND MACHINE LEARNING (AI/ML)
    aiml_id = courses_map.get("Artificial Intelligence and Machine Learning (AI/ML)")
    if aiml_id:
        def aiml_tid(name):
            return topic_map.get((aiml_id, name))

        all_questions.extend([
            (aiml_id, aiml_tid("Python Fundamentals"), 1, "Which library is considered the foundational numerical computing engine in Python?", "NumPy", "Flask", "Django", "Requests", "A"),
            (aiml_id, aiml_tid("Python Fundamentals"), 2, "What is list comprehension in Python?", "A concise syntax to create lists based on existing iterables", "A sorting algorithm", "A debugger tool", "A method to merge lists", "A"),
            (aiml_id, aiml_tid("Python Fundamentals"), 3, "What does the lambda keyword define in Python?", "An anonymous inline function", "A class decorator", "A thread lock", "A complex number", "A"),
            (aiml_id, aiml_tid("Mathematics and Statistics Basics"), 4, "What measure of central tendency is most sensitive to extreme outliers?", "Median", "Mode", "Mean", "Interquartile range", "C"),
            (aiml_id, aiml_tid("Mathematics and Statistics Basics"), 5, "What does standard deviation quantify in a distribution?", "The average value", "The spread or dispersion around the mean", "The total count of items", "The maximum frequency", "B"),
            (aiml_id, aiml_tid("Mathematics and Statistics Basics"), 6, "In linear regression, what mathematical optimization method iteratively minimizes the loss function?", "Gradient Descent", "Breadth-First Search", "Quicksort", "PCA", "A"),
            (aiml_id, aiml_tid("NumPy and Pandas"), 7, "What is the primary multidimensional data container in NumPy?", "DataFrame", "ndarray", "Series", "TensorFlow", "B"),
            (aiml_id, aiml_tid("NumPy and Pandas"), 8, "Which Pandas structure represents a 2-dimensional labeled tabular dataset?", "Series", "Panel", "DataFrame", "Matrix", "C"),
            (aiml_id, aiml_tid("NumPy and Pandas"), 9, "Which Pandas method drops rows containing missing null values?", "fillna()", "dropna()", "isnull()", "drop()", "B"),
            (aiml_id, aiml_tid("NumPy and Pandas"), 10, "What operation allows NumPy to perform element-wise arithmetic on arrays with different shapes?", "Vectorization", "Broadcasting", "Reshaping", "Slicing", "B"),
            (aiml_id, aiml_tid("Data Preprocessing and Visualization"), 11, "Why is feature scaling (e.g. normalization/standardization) important for algorithms like KNN and SVM?", "It prevents features with larger magnitudes from dominating distance calculations", "It reduces dataset size", "It removes missing values", "It converts text to numbers", "A"),
            (aiml_id, aiml_tid("Data Preprocessing and Visualization"), 12, "Which technique converts categorical text labels into binary indicator columns?", "Label Encoding", "One-Hot Encoding", "Min-Max Scaling", "Principal Component Analysis", "B"),
            (aiml_id, aiml_tid("Data Preprocessing and Visualization"), 13, "Which Python library is standard for creating static, animated, and interactive visualizations?", "Matplotlib", "PyTorch", "Scikit-Learn", "NLTK", "A"),
            (aiml_id, aiml_tid("Machine Learning Algorithms"), 14, "What is the difference between supervised and unsupervised learning?", "Supervised uses labeled target outcomes; unsupervised learns patterns without targets", "Supervised uses no algorithms", "Unsupervised only works with images", "Supervised has no training step", "A"),
            (aiml_id, aiml_tid("Machine Learning Algorithms"), 15, "Which algorithm is an ensemble method combining multiple decision trees via bagging?", "Linear Regression", "K-Means", "Random Forest", "Logistic Regression", "C"),
            (aiml_id, aiml_tid("Machine Learning Algorithms"), 16, "What problem does Logistic Regression solve?", "Continuous numerical forecasting", "Classification (predicting category probabilities)", "Clustering unlabeled items", "Dimensionality reduction", "B"),
            (aiml_id, aiml_tid("Machine Learning Algorithms"), 17, "Which unsupervised algorithm partitions observations into K distinct clusters based on Euclidean distance?", "K-Means Clustering", "K-Nearest Neighbors", "Naive Bayes", "Linear Discriminant Analysis", "A"),
            (aiml_id, aiml_tid("Model Evaluation and AI Projects"), 18, "What does overfitting mean in a machine learning model?", "Model performs exceptionally well on training data but generalizes poorly to unseen test data", "Model performs poorly on both training and test data", "Model runs out of memory", "Model has too few parameters", "A"),
            (aiml_id, aiml_tid("Model Evaluation and AI Projects"), 19, "What metric represents the proportion of true positives among all instances predicted as positive?", "Recall", "Precision", "Accuracy", "F1 Score", "B"),
            (aiml_id, aiml_tid("Model Evaluation and AI Projects"), 20, "What is K-Fold Cross-Validation used for?", "To reliably assess model generalization across multiple partitioned folds of data", "To encrypt machine learning models", "To increase learning rate", "To compress data size", "A")
        ])

    c.execute("DELETE FROM assessment_questions")
    c.executemany("""
        INSERT INTO assessment_questions (
            course_id, topic_id, question_number, question_text,
            option_a, option_b, option_c, option_d, correct_answer
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, all_questions)

    conn.commit()
    conn.close()
    return len(all_questions)


if __name__ == "__main__":
    count = seed_default_questions()
    print(f"Seeded {count} assessment questions successfully.")
