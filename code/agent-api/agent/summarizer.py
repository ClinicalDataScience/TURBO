"""Summarize large tool outputs to reduce agent context usage.

After each tool call (FHIR data, guideline search, etc.), the raw output is
checked against a character threshold.  If it exceeds the limit, a fast LLM
call produces a concise, query-aware summary that preserves all identifiers
(fhir_id, document_id) so that source mapping still works downstream.
"""
import logging
import re

from agent.model import llm_client
from config import LLM_MODEL, TOOL_SUMMARY_CHAR_THRESHOLD

logger = logging.getLogger("medgemma-agent")

SUMMARY_CHAR_THRESHOLD = TOOL_SUMMARY_CHAR_THRESHOLD

# Hard cap on raw text sent to the summariser (avoids blowing its own context)
_MAX_SUMMARISER_INPUT_CHARS = 25000

_SUMMARISE_PROMPT = """\
You are a medical data summarizer. Condense the following tool output into a
concise summary that a medical AI assistant can use to answer the user's
question.

RULES:
- Preserve ALL fhir_id and document_id values exactly.
- Include key clinical facts: diagnoses, staging, TNM, treatments, medications,
  lab values, imaging findings, dates.
- For guideline chunks, keep the specific recommendations and evidence levels.
- Omit boilerplate, empty fields, and redundant metadata.
- Use compact bullet points.
- Keep the summary under 600 words.

User's question: {query}

Tool output:
{tool_output}

Concise summary:"""


def summarize_tool_output(raw_output: str, query: str) -> str:
    """Return a compact summary of *raw_output*, or the original if it's short."""
    if len(raw_output) <= SUMMARY_CHAR_THRESHOLD:
        return raw_output

    # Truncate extremely large outputs before sending to summariser
    if len(raw_output) > _MAX_SUMMARISER_INPUT_CHARS:
        truncated = (
            raw_output[:_MAX_SUMMARISER_INPUT_CHARS]
            + f"\n... [truncated, {len(raw_output)} chars total]"
        )
    else:
        truncated = raw_output

    prompt = _SUMMARISE_PROMPT.format(query=query, tool_output=truncated)

    try:
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=1500,
        )
        summary = response.choices[0].message.content.strip()
        logger.info(
            "Summarised tool output: %d chars -> %d chars (%.0f%% reduction)",
            len(raw_output),
            len(summary),
            (1 - len(summary) / len(raw_output)) * 100,
        )
        return summary
    except Exception as e:
        logger.error("Summarisation failed, falling back to truncation: %s", e)
        return (
            raw_output[:3000]
            + f"\n... [truncated to fit context, {len(raw_output)} chars total]"
        )


def extract_milvus_doc_ids(text: str) -> set[str]:
    """Extract Milvus document_id values from a tool output string."""
    return set(re.findall(r'"document_id"\s*:\s*"([^"]+)"', text))
