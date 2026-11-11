import sqlite3
import hashlib
import json

def get_db_fingerprint(db_path="learnly.db"):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    tables = ["users", "courses", "topics", "topic_prerequisites", "assessment_questions", "learner_assessment_results", "learner_topic_progress"]
    fingerprint = {}
    
    for t in tables:
        rows = cursor.execute(f"SELECT * FROM {t} ORDER BY id ASC").fetchall()
        row_dicts = [dict(r) for r in rows]
        # Hash row dicts representation
        raw_json = json.dumps(row_dicts, sort_keys=True, default=str)
        t_hash = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        fingerprint[t] = {
            "count": len(rows),
            "sha256": t_hash
        }
    
    conn.close()
    return fingerprint

if __name__ == "__main__":
    fp = get_db_fingerprint()
    print("Database Baseline Fingerprint:")
    for t, data in fp.items():
        print(f"  {t:<26}: {data['count']:<4} rows | Hash: {data['sha256'][:16]}...")

