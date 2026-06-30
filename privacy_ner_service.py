from flask import Flask, request, jsonify
from flask_cors import CORS
from pythainlp.tag import NER
import threading, sqlite3, json, os

app = Flask(__name__)
CORS(app)

# ── SQLite session store ──────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'episteme_sessions.db')

def _get_db():
    """Return a new per-request SQLite connection (check_same_thread=False for Flask threaded)."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = _get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                psychiatric_category TEXT,
                severity TEXT,
                language TEXT,
                drug_allergy TEXT,
                previous_treatment TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                quantum_overall INTEGER,
                verification_scores TEXT,
                is_report INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            );
        """)
        # Idempotent migration for pre-existing DBs created before the two
        # session-level columns existed. ADD COLUMN throws "duplicate column
        # name" if already present, so each is guarded independently.
        for col in ("drug_allergy", "previous_treatment"):
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN %s TEXT" % col)
            except sqlite3.OperationalError:
                pass  # column already exists — nothing to do
        conn.commit()
    finally:
        conn.close()

ner_engine = NER("thainer")
SENSITIVE_TAGS = {"PERSON", "LOCATION", "ORGANIZATION", "DATE"}

# In-memory score store keyed by session_id
_score_lock = threading.Lock()
_score_store = {}   # { session_id: { scores, quantum, category, severity, prev_quantum, deltas, ts } }

@app.route("/scan", methods=["POST"])
def scan():
    text = request.json.get("text", "")
    tagged = ner_engine.tag(text)
    detected_entities = []
    for word, tag in tagged:
        clean_tag = tag.replace("B-", "").replace("I-", "")
        if clean_tag in SENSITIVE_TAGS:
            detected_entities.append({"word": word, "type": clean_tag})
    return jsonify({
        "pii_detected": len(detected_entities) > 0,
        "entities": detected_entities,
        "entity_count": len(detected_entities)
    })

@app.route("/score/update", methods=["POST"])
def score_update():
    """Called by n8n after Quantum Score Calculator runs."""
    data = request.get_json(force=True) or {}
    session_id = str(data.get("session_id", "default"))
    scores     = data.get("verification_scores", {})
    quantum    = int(data.get("quantum_overall", 0))
    category   = str(data.get("psychiatric_category", "unknown"))
    severity   = str(data.get("severity", "unknown"))

    with _score_lock:
        prev = _score_store.get(session_id, {})
        prev_quantum = prev.get("quantum_overall", None)
        prev_scores  = prev.get("verification_scores", {})

        # Per-dimension deltas
        deltas = {}
        for k in ["ethics", "research", "accuracy", "completeness", "confidence"]:
            cur = scores.get(k, 0)
            old = prev_scores.get(k, None)
            deltas[k] = (cur - old) if old is not None else None

        quantum_delta = (quantum - prev_quantum) if prev_quantum is not None else None

        _score_store[session_id] = {
            "verification_scores": scores,
            "quantum_overall": quantum,
            "quantum_delta": quantum_delta,
            "score_deltas": deltas,
            "psychiatric_category": category,
            "severity": severity,
        }

    return jsonify({"ok": True, "session_id": session_id, "quantum": quantum, "delta": quantum_delta})

@app.route("/score", methods=["GET"])
def score_get():
    """Polled by the dashboard sidebar."""
    session_id = request.args.get("session_id", "default")
    with _score_lock:
        data = _score_store.get(session_id, {})
    return jsonify(data)

@app.route("/score/all", methods=["GET"])
def score_all():
    """Returns all active sessions (for multi-patient dashboard)."""
    with _score_lock:
        return jsonify(_score_store)

@app.route("/session/save", methods=["POST"])
def session_save():
    """Upsert session row, insert message row. Never raises — failure is logged, not thrown."""
    try:
        body = request.get_json(force=True) or {}
        sid      = str(body.get("session_id", "")).strip()
        role     = str(body.get("role", "user"))
        content  = str(body.get("content", ""))
        is_report = 1 if body.get("is_report") else 0
        cat      = body.get("psychiatric_category") or None
        sev      = body.get("severity") or None
        lang     = body.get("language") or None
        allergy  = body.get("drug_allergy") or None
        prev_tx  = body.get("previous_treatment") or None
        quantum  = body.get("quantum_overall")
        vscores  = body.get("verification_scores")
        if isinstance(vscores, dict):
            vscores = json.dumps(vscores)

        if not sid or not content:
            return jsonify({"saved": False, "error": "session_id and content are required"}), 400

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()

        conn = _get_db()
        try:
            conn.execute("""
                INSERT INTO sessions (session_id, psychiatric_category, severity, language, drug_allergy, previous_treatment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    psychiatric_category = COALESCE(excluded.psychiatric_category, sessions.psychiatric_category),
                    severity             = COALESCE(excluded.severity,             sessions.severity),
                    language             = COALESCE(excluded.language,             sessions.language),
                    drug_allergy         = COALESCE(excluded.drug_allergy,         sessions.drug_allergy),
                    previous_treatment   = COALESCE(excluded.previous_treatment,   sessions.previous_treatment),
                    updated_at           = excluded.updated_at
            """, (sid, cat, sev, lang, allergy, prev_tx, now, now))
            conn.execute("""
                INSERT INTO messages (session_id, role, content, quantum_overall, verification_scores, is_report, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (sid, role, content, quantum, vscores, is_report, now))
            conn.commit()
        finally:
            conn.close()

        return jsonify({"saved": True})
    except Exception as e:
        return jsonify({"saved": False, "error": str(e)}), 500

@app.route("/session/<session_id>", methods=["GET"])
def session_get(session_id):
    conn = _get_db()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        msgs = conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC", (session_id,)
        ).fetchall()
        return jsonify({
            "session": dict(row),
            "messages": [dict(m) for m in msgs]
        })
    finally:
        conn.close()

@app.route("/sessions", methods=["GET"])
def sessions_list():
    limit = min(int(request.args.get("limit", 20)), 100)
    conn = _get_db()
    try:
        rows = conn.execute("""
            SELECT session_id, psychiatric_category, severity, language, created_at, updated_at
            FROM sessions ORDER BY updated_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()

if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5005, threaded=True)
