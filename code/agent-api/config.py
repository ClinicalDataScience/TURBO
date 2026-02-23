"""Centralized configuration from environment variables."""
import os
import logging

logger = logging.getLogger("medgemma-agent")


def _env_required(name: str) -> str:
    """Read a required env var or raise immediately."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


def _env_int(name: str, default: int) -> int:
    """Read integer env vars safely with fallback."""
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid integer for %s=%r, using default %d", name, raw, default)
        return default


# Milvus
MILVUS_URI = os.getenv("MILVUS_URI", "http://milvus-standalone:19530")
MILVUS_TOKEN = _env_required("MILVUS_TOKEN")
NSCLC_COLLECTION = os.getenv("NSCLC_COLLECTION", "nsclc_guideline__markdown_header_length__qwen3_embedding_4b_fp16")
SCLC_COLLECTION = os.getenv("SCLC_COLLECTION", "sclc_guideline__markdown_header_length__qwen3_embedding_4b_fp16")

# LLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/default")
LLM_API_KEY = _env_required("LLM_API_KEY")

# Embedding
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL")
EMBEDDING_API_KEY = _env_required("EMBEDDING_API_KEY")

# FHIR
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL")
FHIR_PATIENT_IDS = [pid.strip() for pid in os.getenv("FHIR_PATIENT_IDS", "").split(",") if pid.strip()]

# Clinical
DEFAULT_CLINICAL_QUESTION = os.getenv("DEFAULT_CLINICAL_QUESTION", os.getenv("DEFAULT_FRAGESTELLUNG", ""))
BASE_CLINICAL_QUESTION = (
    "What are the next steps for the patient. Do we need additionall diagnostics? "
    "Is the current therapy adequate and seems to be working? Should we switch it, if yes, how?"
)

# Guideline data
GUIDELINE_PATH = os.getenv("GUIDELINE_PATH", "/app/data/nsclc_guideline.md")
SCLC_GUIDELINE_PATH = os.getenv("SCLC_GUIDELINE_PATH", "/app/data/sclc_guideline.md")

# SQLite
SQLITE_DB_PATH = os.getenv("SQLITE_DB_PATH", "./medgemma.db")
logger.info("SQLITE_DB_PATH resolved to: %s", os.path.abspath(SQLITE_DB_PATH))

# Context limits
MAX_CHUNK_CHARS = _env_int("MAX_CHUNK_CHARS", 30000)
MAX_CONTEXT_TOKENS = _env_int("MAX_CONTEXT_TOKENS", 45000)
MAX_HISTORY_MESSAGES = _env_int("MAX_HISTORY_MESSAGES", 10)
MAX_MESSAGE_CHARS = _env_int("MAX_MESSAGE_CHARS", 2000)

# Tool output summarisation (reduce agent context usage)
TOOL_SUMMARY_CHAR_THRESHOLD = _env_int("TOOL_SUMMARY_CHAR_THRESHOLD", 1500)

# Summary generation
SUMMARY_CORRECTION_LOOP_ENABLED = os.getenv("SUMMARY_CORRECTION_LOOP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
SUMMARY_CORRECTION_LOOP_PASSES = _env_int("SUMMARY_CORRECTION_LOOP_PASSES", 2)
SUMMARY_FIELD_MAX_RETRIES = _env_int("SUMMARY_FIELD_MAX_RETRIES", 2)
