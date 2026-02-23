"""Thread-local streaming status events during agent execution.

Thread-local attributes on ``_tls`` (set/read per-request):
    status_queue   – queue.Queue for SSE status messages (set by query route)
    current_query  – the user's question, used for query-aware summarisation
    collected_milvus_doc_ids – set[str] accumulated across tool calls
"""
import logging
import os
import threading
from logging.handlers import RotatingFileHandler

# Summarizer is lazy-imported inside wrapped_forward() to avoid circular imports.

_tls = threading.local()

# Dedicated tool-call logger (writes to data/tool-calls.log)
tool_logger = logging.getLogger("tool-calls")
tool_logger.propagate = False  # don't duplicate into main log

if os.getenv("LOG_TOOL_CALLS", "true").lower() in ("true", "1", "yes"):
    tool_logger.setLevel(logging.INFO)
    _tool_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(_tool_log_dir, exist_ok=True)
    _tool_fh = RotatingFileHandler(
        os.path.join(_tool_log_dir, "tool-calls.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    _tool_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    tool_logger.addHandler(_tool_fh)
else:
    tool_logger.setLevel(logging.CRITICAL)  # effectively disabled

_TOOL_STATUS_MESSAGES = {
    "get_patient_data": "Fetching patient data...",
    "get_fhir_resource": "Reading {resource_type} details...",
    "search_guidelines": "Searching clinical guidelines...",
    "list_guidelines": "Loading available guidelines...",
    "get_guideline": "Reading guideline document...",
    "search_collection": "Searching collection...",
    "query_collection": "Querying collection...",
}


def _tool_status_message(tool_name: str, kwargs: dict) -> str:
    """Build a human-readable status message for a tool call."""
    template = _TOOL_STATUS_MESSAGES.get(tool_name, f"Using {tool_name}...")
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template.split("{")[0].rstrip() + "..."


def _wrap_tool_forward(tool):
    """Wrap a tool's forward() to emit status events and summarise large outputs.

    After the original tool executes:
    1. Milvus document IDs are extracted and stored in ``_tls``.
    2. If the output exceeds the character threshold it is replaced by a
       concise LLM-generated summary (query-aware) before being returned to
       the agent, keeping the conversation history small.
    """
    original_forward = tool.forward
    tool_name = tool.name

    def wrapped_forward(*args, **kwargs):
        # --- streaming status ---
        q = getattr(_tls, "status_queue", None)
        if q is not None:
            msg = _tool_status_message(tool_name, kwargs)
            q.put({"type": "status", "message": msg})

        tool_logger.info("CALL %s | args=%s kwargs=%s", tool_name, args, kwargs)
        result = original_forward(*args, **kwargs)
        raw_str = str(result)

        # --- log raw output (truncated for the log file) ---
        log_str = raw_str
        if len(log_str) > 2000:
            log_str = log_str[:2000] + f"... [truncated, {len(raw_str)} chars total]"
        tool_logger.info("RESULT %s | %s", tool_name, log_str)

        # --- collect Milvus document IDs before summarisation ---
        from agent.summarizer import summarize_tool_output, extract_milvus_doc_ids, SUMMARY_CHAR_THRESHOLD
        milvus_ids = extract_milvus_doc_ids(raw_str)
        if milvus_ids:
            collected = getattr(_tls, "collected_milvus_doc_ids", None)
            if collected is not None:
                collected.update(milvus_ids)

        # --- summarise large outputs ---
        query = getattr(_tls, "current_query", "")
        if len(raw_str) > SUMMARY_CHAR_THRESHOLD and q is not None:
            q.put({"type": "status", "message": "Summarizing retrieved data..."})
        summary = summarize_tool_output(raw_str, query)
        if summary != raw_str:
            tool_logger.info(
                "SUMMARISED %s | %d -> %d chars", tool_name, len(raw_str), len(summary),
            )
        return summary

    tool.forward = wrapped_forward
