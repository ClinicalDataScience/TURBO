"""LLM keypoint extraction for sources and timeline events."""
import json
import time
import asyncio
import logging
import sqlite3
from typing import Optional

from config import LLM_MODEL, BASE_CLINICAL_QUESTION
from database import get_db_connection
from agent.model import llm_client
from services.llm_utils import chunk_text

logger = logging.getLogger("medgemma-agent")


def extract_keypoints_for_chunk(chunk_text_content: str, fragestellung: str,
                                timeout_seconds: int = 120) -> dict:
    """Call LLM to extract keypoints from a single text chunk."""
    clinical_question_text = BASE_CLINICAL_QUESTION
    if fragestellung:
        clinical_question_text += f"\n\nAdditional clinical question: {fragestellung}"
    prompt = (
        "You are a medical expert. Analyze the following document text and "
        "extract the most important key points (maximum 3). Also assess whether the "
        "document is relevant to the following clinical question.\n\n"
        f"Clinical Question: {clinical_question_text}\n\n"
        f"Document Text:\n{chunk_text_content}\n\n"
        "Respond ONLY in the following JSON format, without additional text:\n"
        '{"keypoints": [{"text": "keypoint1", "priority": 1-5}] }'
    )
    logger.info("LLM keypoint extraction prompt: %d chars", len(prompt))
    try:
        _kp_start = time.monotonic()
        stream = llm_client.chat.completions.create(
            model=LLM_MODEL,
            timeout=timeout_seconds,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            stream=True,
        )
        chunks: list[str] = []
        for chunk in stream:
            if time.monotonic() - _kp_start > timeout_seconds:
                logger.warning("LLM keypoint extraction timed out after %ds", timeout_seconds)
                try:
                    stream.close()
                except Exception:
                    pass
                break
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)
        content = "".join(chunks).strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content)
    except Exception as e:
        logger.warning("LLM keypoint extraction failed for chunk: %s", e)
        return {"keypoints": []}


async def generate_keypoints_for_source(
    source_id: str,
    clinical_question: str = None,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[list[dict]]:
    """Generate LLM keypoints for a single source and update timeline event."""
    should_close = conn is None
    if conn is None:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

    try:
        source = conn.execute(
            "SELECT source_id, content, content_markdown, content_hash FROM sources WHERE source_id = ?",
            (source_id,)
        ).fetchone()

        if not source or not source["content"]:
            return None

        content = source["content_markdown"] or source["content"]
        source_content_hash = source["content_hash"]
        chunks = chunk_text(content)
        all_keypoints = []

        for chunk in chunks:
            result = await asyncio.to_thread(
                extract_keypoints_for_chunk, chunk, clinical_question or "General tumor board review"
            )
            keypoints = result.get("keypoints", [])
            if keypoints and isinstance(keypoints[0], str):
                keypoints = [{"text": kp, "priority": 3} for kp in keypoints]
            all_keypoints.extend(keypoints)

        conn.execute(
            "INSERT OR REPLACE INTO keypoints (source_id, fragestellung, keypoints, content_hash) VALUES (?, ?, ?, ?)",
            (source_id, clinical_question or "General", json.dumps(all_keypoints), source_content_hash)
        )

        if all_keypoints:
            sorted_kps = sorted(
                all_keypoints,
                key=lambda kp: (kp.get("priority", 3) if isinstance(kp, dict) else 3),
            )
            top_texts = [
                (kp["text"] if isinstance(kp, dict) else str(kp))
                for kp in sorted_kps[:3]
            ]
            llm_insight = "; ".join(top_texts)
            conn.execute(
                "UPDATE timeline_events SET key_insight = ? WHERE source_id = ?",
                (llm_insight, source_id),
            )

        conn.commit()
        return all_keypoints

    except Exception:
        logger.exception("Failed to generate keypoints for source %s", source_id)
        return None
    finally:
        if should_close:
            conn.close()


async def generate_keypoints_batch(patient_id: str, source_ids: list[str],
                                   clinical_question: str = None):
    """Generate keypoints for a batch of sources in background."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        _start = time.monotonic()

        for source_id in source_ids:
            try:
                await generate_keypoints_for_source(source_id, clinical_question, conn)
            except Exception as e:
                logger.error("Failed to generate keypoints for %s: %s", source_id, e)

        conn.close()
        _elapsed = time.monotonic() - _start
        logger.info(
            "Completed background keypoint generation for %d sources (patient %s) in %.1fs",
            len(source_ids), patient_id, _elapsed,
        )
    except Exception:
        logger.exception("Batch keypoint generation failed")


def run_keypoint_extraction_from_sources(fragestellung: str) -> list[dict]:
    """Extract keypoints for all sources and store results (synchronous)."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    sources = conn.execute(
        "SELECT source_id, content, content_markdown, content_hash FROM sources WHERE content IS NOT NULL"
    ).fetchall()

    results = []
    for source in sources:
        source_id = source["source_id"]
        content = source["content_markdown"] or source["content"]
        src_content_hash = source["content_hash"]
        if not content:
            continue

        chunks = chunk_text(content)
        all_keypoints = []

        for chunk in chunks:
            result = extract_keypoints_for_chunk(chunk, fragestellung)
            all_keypoints.extend(result.get("keypoints", []))

        conn.execute(
            "INSERT INTO keypoints (source_id, fragestellung, keypoints, content_hash) VALUES (?, ?, ?, ?)",
            (source_id, fragestellung, json.dumps(all_keypoints), src_content_hash)
        )

        if all_keypoints:
            sorted_kps = sorted(
                all_keypoints,
                key=lambda kp: (kp.get("priority", 3) if isinstance(kp, dict) else 3),
            )
            top_texts = [
                (kp["text"] if isinstance(kp, dict) else str(kp))
                for kp in sorted_kps[:3]
            ]
            llm_insight = "; ".join(top_texts)
            conn.execute(
                "UPDATE timeline_events SET key_insight = ? WHERE source_id = ?",
                (llm_insight, source_id),
            )

        results.append({
            "source_id": source_id,
            "keypoints": all_keypoints
        })

    conn.commit()
    conn.close()
    return results
