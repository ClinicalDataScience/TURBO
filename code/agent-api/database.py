"""SQLite database schema, initialization, and CRUD helpers."""
import json
import uuid
import hashlib
import sqlite3
import logging
from config import SQLITE_DB_PATH

logger = logging.getLogger("medgemma-agent")


def get_db_connection():
    """Get a database connection with proper settings for concurrent access."""
    conn = sqlite3.connect(
        SQLITE_DB_PATH,
        timeout=30.0,
        check_same_thread=False,
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _compute_content_hash(content: str) -> str:
    """SHA-256 hash of JSON-normalized content for change detection."""
    try:
        normalized = json.dumps(json.loads(content), sort_keys=True, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        normalized = content
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _compute_patient_data_hash(source_hashes: list[str]) -> str:
    """Aggregate SHA-256 hash from sorted individual source hashes."""
    combined = "|".join(sorted(source_hashes))
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def init_db():
    """Initialize SQLite database with complete schema."""
    conn = get_db_connection()

    # Central source registry
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            source_id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            resource_type TEXT,
            fhir_id TEXT,
            milvus_document_id TEXT,
            patient_id TEXT,
            title TEXT,
            date TEXT,
            content TEXT,
            content_markdown TEXT,
            preview TEXT,
            metadata TEXT,
            is_relevant BOOLEAN DEFAULT FALSE,
            relevance_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_patient ON sources(patient_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_type ON sources(source_type, resource_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_relevant ON sources(is_relevant)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_fhir ON sources(fhir_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sources_milvus ON sources(milvus_document_id)")

    # Extracted keypoints
    conn.execute("""
        CREATE TABLE IF NOT EXISTS keypoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            fragestellung TEXT,
            keypoints TEXT,
            is_relevant BOOLEAN,
            relevance_reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        )
    """)

    # Summaries cache
    conn.execute("""
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            fragestellung TEXT,
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_patient ON summaries(patient_id)")

    # Timeline events
    conn.execute("""
        CREATE TABLE IF NOT EXISTS timeline_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            patient_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_date TEXT,
            title TEXT,
            key_insight TEXT,
            priority INTEGER,
            is_relevant BOOLEAN DEFAULT FALSE,
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_patient ON timeline_events(patient_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(event_date)")
    conn.execute("""
        DELETE FROM timeline_events
        WHERE id NOT IN (
            SELECT MIN(id) FROM timeline_events GROUP BY source_id, patient_id
        )
    """)

    # Chat conversations
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT UNIQUE NOT NULL,
            patient_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            sources TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
        )
    """)

    # Generation progress tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS generation_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            clinical_question TEXT,
            current_field TEXT,
            current_step INTEGER,
            total_steps INTEGER,
            status_message TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_progress_unique ON generation_progress(patient_id, COALESCE(clinical_question, ''))")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_progress_patient ON generation_progress(patient_id)")

    # Legacy documents table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            document_id TEXT PRIMARY KEY,
            document_name TEXT,
            text TEXT,
            metadata TEXT
        )
    """)

    # Treatment response assessments (LLM-classified, cached)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS treatment_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT NOT NULL,
            cycle_event_id TEXT NOT NULL,
            cycle_date TEXT,
            status TEXT NOT NULL,
            status_label TEXT NOT NULL,
            confidence TEXT DEFAULT 'medium',
            basis TEXT,
            imaging_source_ids TEXT DEFAULT '[]',
            imaging_date TEXT,
            content_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(patient_id, cycle_event_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_treatment_resp_patient ON treatment_responses(patient_id)")

    # Migrations: add hash columns
    for stmt in [
        "ALTER TABLE sources ADD COLUMN content_hash TEXT",
        "ALTER TABLE keypoints ADD COLUMN content_hash TEXT",
        "ALTER TABLE summaries ADD COLUMN fhir_data_hash TEXT",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass

    conn.execute("""
        CREATE TABLE IF NOT EXISTS patient_hashes (
            patient_id TEXT PRIMARY KEY,
            fhir_data_hash TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS patient_metadata (
            patient_id TEXT PRIMARY KEY,
            cancer_type_raw TEXT,
            guideline_cancer_types TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")


def upsert_patient_metadata(patient_id: str, cancer_type_raw: str, guideline_cancer_types: list) -> None:
    """Store or update cancer-type classification for a patient."""
    import json as _json
    conn = get_db_connection()
    conn.execute(
        """INSERT INTO patient_metadata (patient_id, cancer_type_raw, guideline_cancer_types, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(patient_id) DO UPDATE SET
               cancer_type_raw = excluded.cancer_type_raw,
               guideline_cancer_types = excluded.guideline_cancer_types,
               updated_at = CURRENT_TIMESTAMP""",
        (patient_id, cancer_type_raw, _json.dumps(guideline_cancer_types)),
    )
    conn.commit()
    conn.close()


def get_patient_metadata(patient_id: str) -> dict | None:
    """Return dict with cancer_type_raw and guideline_cancer_types list, or None if not set."""
    import json as _json
    conn = get_db_connection()
    row = conn.execute(
        "SELECT cancer_type_raw, guideline_cancer_types FROM patient_metadata WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {
        "cancer_type_raw": row[0],
        "guideline_cancer_types": _json.loads(row[1]) if row[1] else ["nsclc"],
    }


def register_source(
    source_type: str,
    resource_type: str,
    content: str,
    title: str = None,
    fhir_id: str = None,
    milvus_document_id: str = None,
    patient_id: str = None,
    date: str = None,
    metadata: dict = None,
    is_relevant: bool = False,
    relevance_reason: str = None,
) -> tuple[str, bool]:
    """Register a source in the central registry.

    Returns (source_id, content_changed).
    """
    conn = get_db_connection()
    new_hash = _compute_content_hash(content) if content else None

    if fhir_id:
        existing = conn.execute(
            "SELECT source_id, content_hash FROM sources WHERE source_type = ? AND fhir_id = ?",
            (source_type, fhir_id),
        ).fetchone()
        if existing:
            old_hash = existing[1]
            if old_hash == new_hash:
                conn.close()
                return existing[0], False

            preview = content[:200] if content else ""
            conn.execute("""
                UPDATE sources
                SET content = ?, content_markdown = ?, preview = ?, title = ?,
                    date = ?, metadata = ?, content_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_id = ?
            """, (
                content, content, preview, title, date,
                json.dumps(metadata) if metadata else None,
                new_hash, existing[0],
            ))
            conn.commit()
            conn.close()
            logger.info("Source %s (fhir_id=%s) content changed, updated", existing[0], fhir_id)
            return existing[0], True

    if milvus_document_id:
        existing = conn.execute(
            "SELECT source_id FROM sources WHERE source_type = ? AND milvus_document_id = ?",
            (source_type, milvus_document_id),
        ).fetchone()
        if existing:
            conn.close()
            return existing[0], False

    source_id = str(uuid.uuid4())
    preview = content[:200] if content else ""
    content_markdown = content

    conn.execute("""
        INSERT INTO sources (
            source_id, source_type, resource_type, fhir_id, milvus_document_id,
            patient_id, title, date, content, content_markdown, preview,
            metadata, is_relevant, relevance_reason, content_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source_id, source_type, resource_type, fhir_id, milvus_document_id,
        patient_id, title, date, content, content_markdown, preview,
        json.dumps(metadata) if metadata else None, is_relevant, relevance_reason,
        new_hash,
    ))
    conn.commit()
    conn.close()
    return source_id, True


def get_source(source_id: str) -> dict:
    """Fetch a source by its source_id, falling back to fhir_id lookup."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if not row:
        # Fallback: try treating the id as a fhir_id
        row = conn.execute(
            "SELECT * FROM sources WHERE fhir_id = ?", (source_id,)
        ).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)
