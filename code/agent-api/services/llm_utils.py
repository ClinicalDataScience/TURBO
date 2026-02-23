"""LLM utility functions: token estimation, response cleaning, chunking."""
import re

from config import MAX_CHUNK_CHARS


def estimate_tokens(text: str) -> int:
    """Roughly estimate token count (1 token ~ 3 characters for medical/technical text)."""
    return len(text) // 3


def clean_agent_response(text: str) -> str:
    """Remove internal reasoning tokens and thinking markers from agent output."""
    text = re.sub(r'<unused94>thought.*?<unused94>', '', text, flags=re.DOTALL)
    text = re.sub(r'<unused\d+>', '', text)
    text = re.sub(r'^thought\s+.*?(?=\n\n|\n[A-Z])', '', text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def extract_sources_used(text: str) -> tuple[str, list[str]]:
    """Extract SOURCES_USED: footer from LLM answer and return (clean_text, source_ids).

    The LLM is instructed to append a line like:
        SOURCES_USED: id1, id2, id3
    This function strips that line and returns the referenced source IDs.
    """
    pattern = r'\n*\s*SOURCES_USED:\s*(.+?)$'
    match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if match:
        ids_str = match.group(1).strip()
        raw_ids = [sid.strip().strip('"').strip("'") for sid in ids_str.split(",")]
        source_ids = [sid for sid in raw_ids if sid and sid != "none"]
        clean_text = text[:match.start()].rstrip()
        return clean_text, source_ids
    return text, []


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks for LLM processing."""
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        break_at = text.rfind("\n\n", start, end)
        if break_at <= start:
            break_at = text.rfind("\n", start, end)
        if break_at <= start:
            break_at = end
        chunks.append(text[start:break_at])
        start = break_at
    return chunks
