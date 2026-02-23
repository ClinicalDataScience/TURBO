"""LLM chat query endpoints (agent-based and streaming)."""
import json
import re
import uuid
import asyncio
import logging
import threading
import queue
from typing import Optional

from fastapi import APIRouter, HTTPException
from starlette.responses import StreamingResponse

from database import get_db_connection, get_source, get_patient_metadata
from models import QueryRequest, QueryResponse, QuerySourceRef
from services.fhir_sync import fetch_and_register_fhir_resources
from services.llm_utils import (
    extract_sources_used,
    clean_agent_response,
    estimate_tokens,
)
from agent import setup as agent_setup
from agent.streaming import _tls
from agent.model import llm_client
from config import (
    LLM_MODEL,
    MAX_CONTEXT_TOKENS,
    MAX_HISTORY_MESSAGES,
    MAX_MESSAGE_CHARS,
)

logger = logging.getLogger("medgemma-agent")

router = APIRouter(tags=["query"])


def _extract_milvus_doc_ids_from_history(agent_history) -> set[str]:
    """Extract Milvus document IDs from agent execution history entries."""
    milvus_doc_ids: set[str] = set()
    if not agent_history:
        return milvus_doc_ids

    for entry in agent_history:
        if isinstance(entry, dict):
            for key in ['tool_output', 'output', 'result', 'observation']:
                if key in entry:
                    try:
                        output = entry[key]
                        if isinstance(output, str) and '"document_id"' in output:
                            output_data = json.loads(output)
                            if 'results' in output_data:
                                for res in output_data.get('results', []):
                                    if 'document_id' in res:
                                        milvus_doc_ids.add(res['document_id'])
                            if 'document_id' in output_data:
                                milvus_doc_ids.add(output_data['document_id'])
                    except (json.JSONDecodeError, TypeError, KeyError):
                        pass
        elif isinstance(entry, str) and '"document_id"' in entry:
            doc_id_matches = re.findall(r'"document_id"\s*:\s*"([^"]+)"', entry)
            milvus_doc_ids.update(doc_id_matches)

    return milvus_doc_ids


def _get_agent_history(agent):
    """Try different attributes to access agent execution history."""
    if hasattr(agent, 'logs') and agent.logs:
        return agent.logs
    if hasattr(agent, 'step_history') and agent.step_history:
        return agent.step_history
    if hasattr(agent, 'history') and agent.history:
        return agent.history
    return None


def _build_fallback_prompt(fallback_context: str, history_text: str, query: str) -> str:
    """Build fallback prompt for direct LLM call when agent fails."""
    return f"""You are a medical AI assistant helping with tumor board preparation.

Patient Context:
{fallback_context}

Conversation History:
{history_text}

Current Query: {query}

Each data source in the Patient Context is tagged with a [fhir_id: <id>] identifier.
Answer the user's question directly and concisely using the Patient Context above.
Do NOT include source citations inline. Keep the answer focused on what was asked.
At the very end of your answer, append a single line: SOURCES_USED: <comma-separated fhir_ids you used>
Only include sources that actually contributed to your answer. If none, write: SOURCES_USED: none"""


def _build_fallback_context(conn, patient_id: str) -> str:
    """Build compact patient context from SQLite for fallback LLM call."""
    fb_sources = conn.execute(
        "SELECT fhir_id, title, content_markdown, content FROM sources WHERE patient_id = ? AND fhir_id IS NOT NULL",
        (patient_id,),
    ).fetchall()
    fb_parts = []
    for s in fb_sources:
        content = s[2] or s[3]
        if content:
            fb_parts.append(f"### [fhir_id: {s[0]}] {s[1]}\n{content[:2000]}")
    return "\n\n".join(fb_parts[:10])


def _build_agent_prompt(
    query: str,
    patient_id: str | None,
    history_text: str,
    guideline_cancer_types: list[str] | None = None,
) -> str:
    """Build lean prompt for agent execution."""
    patient_line = f"\nPatient ID: {patient_id}" if patient_id else ""
    history_block = f"\n\nConversation History:\n{history_text}" if history_text else ""

    if guideline_cancer_types and len(guideline_cancer_types) >= 2:
        guideline_hint = "both"
    elif guideline_cancer_types:
        guideline_hint = guideline_cancer_types[0]
    else:
        guideline_hint = "nsclc"

    return f"""You are a medical AI assistant helping with tumor board preparation.{patient_line}{history_block}

Current Query: {query}

INSTRUCTIONS:
1. Use get_patient_data to retrieve patient data from the FHIR server. This returns FULL content for all resources.
2. For guideline/protocol questions, use search_guidelines with cancer_type='{guideline_hint}'.
3. At the very end of your answer, append: SOURCES_USED: <comma-separated fhir_ids you referenced>
   If you used no patient data, write: SOURCES_USED: none
4. Keep your answer focused and concise. Avoid unnecessary tool calls.

TOOL CALL RULES (MUST follow):
- NEVER pass null, None, or Null as any parameter value. Always pass valid non-empty strings.
- get_patient_data takes only patient_id. It always returns ALL resource types.
- get_fhir_resource requires both resource_type (e.g. "DiagnosticReport") and fhir_id as non-empty strings.
- search_guidelines requires a non-empty query string."""


def _map_cited_fhir_ids(conn, cited_fhir_ids: list[str]) -> list[str]:
    """Map FHIR IDs to source_ids via SQLite lookup."""
    source_ids = []
    for fid in cited_fhir_ids:
        row = conn.execute(
            "SELECT source_id FROM sources WHERE fhir_id = ? LIMIT 1", (fid,)
        ).fetchone()
        if row:
            source_ids.append(row[0])
        else:
            logger.debug("Cited fhir_id '%s' not found in sources table, skipping", fid)
    return source_ids


def _map_milvus_doc_ids(conn, milvus_doc_ids: set[str]) -> list[str]:
    """Map Milvus document IDs to source_ids via SQLite lookup."""
    if not milvus_doc_ids:
        return []
    placeholders = ','.join(['?' for _ in milvus_doc_ids])
    milvus_sources = conn.execute(
        f"SELECT source_id FROM sources WHERE milvus_document_id IN ({placeholders})",
        list(milvus_doc_ids),
    ).fetchall()
    return [row[0] for row in milvus_sources]


def _build_history_text(all_history: list) -> str:
    """Build compact conversation history string."""
    history_messages = []
    for role, content in all_history:
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + "... [truncated]"
        history_messages.append(f"{role}: {content}")
    return "\n".join(history_messages)


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------

@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest):
    """LLM chat endpoint using FastMCP tools with mandatory source referencing."""
    conn = None
    try:
        agent = agent_setup.agent
        if agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Create or get conversation
        conversation_id = request.conversation_id or str(uuid.uuid4())

        conn = get_db_connection()

        # Store conversation if new
        existing = conn.execute(
            "SELECT id FROM conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO conversations (conversation_id, patient_id) VALUES (?, ?)",
                (conversation_id, request.patient_id),
            )
            conn.commit()

        # Get conversation history (limited to most recent messages)
        all_history = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
            (conversation_id, MAX_HISTORY_MESSAGES),
        ).fetchall()
        all_history = list(reversed(all_history))

        # Cache FHIR data in SQLite for frontend source badges
        source_ids: list[str] = []
        if request.patient_id:
            has_patient_data = conn.execute(
                "SELECT 1 FROM sources WHERE patient_id = ? AND source_type = 'fhir' LIMIT 1",
                (request.patient_id,),
            ).fetchone()
            if not has_patient_data:
                await fetch_and_register_fhir_resources(request.patient_id, retry=True)

        history_text = _build_history_text(all_history)

        # Resolve guideline collection preference (request overrides DB)
        guideline_cancer_types = request.guideline_cancer_types
        if not guideline_cancer_types and request.patient_id:
            meta = get_patient_metadata(request.patient_id)
            if meta:
                guideline_cancer_types = meta["guideline_cancer_types"]

        full_prompt = _build_agent_prompt(
            request.query, request.patient_id, history_text, guideline_cancer_types
        )

        # Log token usage for monitoring
        final_tokens = estimate_tokens(full_prompt)
        logger.info("=" * 80)
        logger.info("QUERY RECEIVED: %s", request.query)
        logger.info("ESTIMATED TOKENS: %d / %d", final_tokens, MAX_CONTEXT_TOKENS)
        logger.info("HISTORY MESSAGES: %d", len(all_history))
        logger.info("=" * 80)

        # Final safety check
        if final_tokens > MAX_CONTEXT_TOKENS:
            logger.error("Context still exceeds limit after truncation: %d > %d", final_tokens, MAX_CONTEXT_TOKENS)
            raise HTTPException(
                status_code=400,
                detail=f"Query context too large ({final_tokens} tokens). Please try a shorter query or start a new conversation.",
            )

        agent_ran_successfully = False
        try:
            def _run_agent_with_context(prompt=full_prompt):
                _tls.current_query = request.query
                _tls.collected_milvus_doc_ids = set()
                try:
                    return agent.run(prompt)
                finally:
                    _tls.current_query = None

            result = await asyncio.to_thread(_run_agent_with_context)

            logger.info("=" * 80)
            logger.info("AGENT FINAL RESULT: %s", result)
            logger.info("=" * 80)

            answer = clean_agent_response(str(result))
            agent_ran_successfully = True

            logger.info("CLEANED ANSWER: %s", answer)

        except Exception as agent_error:
            error_msg = str(agent_error)
            logger.error("Agent execution failed: %s", error_msg)

            if "exceed_context_size_error" in error_msg or "exceeds the available context size" in error_msg:
                match = re.search(r'request \((\d+) tokens\).*?context size \((\d+) tokens\)', error_msg)
                if match:
                    req_tokens, ctx_size = match.groups()
                    logger.error("Backend LLM context exceeded: %s tokens requested, %s available", req_tokens, ctx_size)

                raise HTTPException(
                    status_code=400,
                    detail=(
                        "The conversation has become too long for the AI to process. "
                        "Please start a new conversation or ask a shorter question. "
                        "(Context limit exceeded)"
                    ),
                )

            # Agent failed -- fall back to direct LLM completion
            logger.warning("Agent failed (%s). Falling back to direct LLM call.", type(agent_error).__name__)

            fallback_context = ""
            if request.patient_id:
                fallback_context = _build_fallback_context(conn, request.patient_id)

            fallback_prompt = _build_fallback_prompt(fallback_context, history_text, request.query)

            try:
                def _stream_fallback(fp=fallback_prompt):
                    s = llm_client.chat.completions.create(
                        model=LLM_MODEL,
                        messages=[{"role": "user", "content": fp}],
                        temperature=0.3,
                        stream=True,
                    )
                    parts: list[str] = []
                    for c in s:
                        if c.choices and c.choices[0].delta.content:
                            parts.append(c.choices[0].delta.content)
                    return "".join(parts)

                raw = await asyncio.to_thread(_stream_fallback)
                answer = clean_agent_response(raw.strip())
                logger.info("DIRECT LLM FALLBACK ANSWER: %s", answer)
            except Exception as fallback_error:
                logger.error("Direct LLM fallback also failed: %s", fallback_error)
                raise agent_error

        # Extract cited fhir_ids from the LLM answer (SOURCES_USED: footer)
        answer, cited_fhir_ids = extract_sources_used(answer)
        source_ids.extend(_map_cited_fhir_ids(conn, cited_fhir_ids))
        logger.info("LLM cited %d fhir_ids, %d mapped to source_ids", len(cited_fhir_ids), len(source_ids))

        # Collect Milvus document IDs stored by tool wrappers during agent run
        if agent_ran_successfully:
            milvus_doc_ids = getattr(_tls, "collected_milvus_doc_ids", set())
            # Fallback: also try parsing agent history
            if not milvus_doc_ids:
                agent_history = _get_agent_history(agent)
                milvus_doc_ids = _extract_milvus_doc_ids_from_history(agent_history)
            milvus_source_ids = _map_milvus_doc_ids(conn, milvus_doc_ids)
            source_ids.extend(milvus_source_ids)
            if milvus_doc_ids:
                logger.info("Extracted Milvus document IDs: %s", milvus_doc_ids)
                logger.info("Found %d Milvus sources from agent tool calls", len(milvus_source_ids))

        # Store messages
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (conversation_id, "user", request.query),
        )
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, sources) VALUES (?, ?, ?, ?)",
            (conversation_id, "assistant", answer, json.dumps(source_ids)),
        )
        conn.commit()

        # Build source references (deduplicate and limit)
        unique_source_ids = list(dict.fromkeys(source_ids))
        sources_refs = []
        for sid in unique_source_ids[:10]:
            source = get_source(sid)
            if source:
                sources_refs.append(QuerySourceRef(
                    source_id=sid,
                    source_type=source["source_type"],
                    resource_type=source["resource_type"] or "",
                    title=source["title"],
                    excerpt=source["preview"],
                    content_markdown=source["content_markdown"],
                ))

        return QueryResponse(
            answer=answer,
            sources=sources_refs,
            conversation_id=conversation_id,
            follow_up_questions=[
                "What are the treatment options?",
                "What does the latest imaging show?",
                "Are there any relevant clinical guidelines?",
            ],
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Query failed with error")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            try:
                conn.close()
            except Exception as e:
                logger.error("Error closing database connection: %s", e)


# ---------------------------------------------------------------------------
# POST /query/stream
# ---------------------------------------------------------------------------

@router.post("/query/stream")
async def query_stream(request: QueryRequest):
    """Streaming version of /query that emits SSE status events during agent execution."""
    agent = agent_setup.agent
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    conversation_id = request.conversation_id or str(uuid.uuid4())

    async def event_generator():
        conn = None
        try:
            def _sse(payload: dict) -> str:
                return f"data: {json.dumps(payload)}\n\n"

            yield _sse({"type": "status", "message": "Analyzing your question..."})

            conn = get_db_connection()

            # Store conversation if new
            existing = conn.execute(
                "SELECT id FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO conversations (conversation_id, patient_id) VALUES (?, ?)",
                    (conversation_id, request.patient_id),
                )
                conn.commit()

            # Get conversation history
            all_history = conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
                (conversation_id, MAX_HISTORY_MESSAGES),
            ).fetchall()
            all_history = list(reversed(all_history))

            # Cache FHIR data in SQLite for frontend source badges
            source_ids: list[str] = []
            if request.patient_id:
                has_patient_data = conn.execute(
                    "SELECT 1 FROM sources WHERE patient_id = ? AND source_type = 'fhir' LIMIT 1",
                    (request.patient_id,),
                ).fetchone()
                if not has_patient_data:
                    await fetch_and_register_fhir_resources(request.patient_id, retry=True)

            history_text = _build_history_text(all_history)

            # Resolve guideline collection preference (request overrides DB)
            guideline_cancer_types = request.guideline_cancer_types
            if not guideline_cancer_types and request.patient_id:
                meta = get_patient_metadata(request.patient_id)
                if meta:
                    guideline_cancer_types = meta["guideline_cancer_types"]

            full_prompt = _build_agent_prompt(
                request.query, request.patient_id, history_text, guideline_cancer_types
            )

            final_tokens = estimate_tokens(full_prompt)
            logger.info(
                "STREAM QUERY: %s | tokens: %d | guidelines: %s",
                request.query, final_tokens, guideline_cancer_types,
            )

            if final_tokens > MAX_CONTEXT_TOKENS:
                yield _sse({"type": "error", "detail": f"Query context too large ({final_tokens} tokens). Please try a shorter query or start a new conversation."})
                return

            # Run agent in a thread with a status queue
            status_q: queue.Queue = queue.Queue()
            agent_result: dict = {}

            def _run_agent():
                _tls.status_queue = status_q
                _tls.current_query = request.query
                _tls.collected_milvus_doc_ids = set()
                try:
                    result = agent.run(full_prompt)
                    agent_result["answer"] = clean_agent_response(str(result))
                    agent_result["success"] = True
                except Exception as e:
                    agent_result["error"] = e
                    agent_result["success"] = False
                finally:
                    _tls.current_query = None
                    _tls.status_queue = None
                    status_q.put(None)  # sentinel

            agent_thread = threading.Thread(target=_run_agent, daemon=True)
            agent_thread.start()

            # Yield status events as tools are called
            while True:
                try:
                    item = await asyncio.to_thread(status_q.get, timeout=30)
                except Exception:
                    if not agent_thread.is_alive():
                        break
                    continue
                if item is None:
                    break
                yield _sse(item)

            agent_thread.join(timeout=5)

            # Handle agent failure -> fallback
            answer = ""
            agent_ran_successfully = agent_result.get("success", False)

            if agent_ran_successfully:
                answer = agent_result["answer"]
            else:
                error = agent_result.get("error")
                error_msg = str(error) if error else "Unknown error"

                if "exceed_context_size_error" in error_msg or "exceeds the available context size" in error_msg:
                    yield _sse({"type": "error", "detail": "The conversation has become too long for the AI to process. Please start a new conversation or ask a shorter question. (Context limit exceeded)"})
                    return

                yield _sse({"type": "status", "message": "Retrying with direct analysis..."})
                logger.warning("Agent failed (%s). Falling back to direct LLM call.", type(error).__name__)

                fallback_context = ""
                if request.patient_id:
                    fallback_context = _build_fallback_context(conn, request.patient_id)

                fallback_prompt = _build_fallback_prompt(fallback_context, history_text, request.query)

                try:
                    def _stream_fallback_sse(fp=fallback_prompt):
                        s = llm_client.chat.completions.create(
                            model=LLM_MODEL,
                            messages=[{"role": "user", "content": fp}],
                            temperature=0.3,
                            stream=True,
                        )
                        parts: list[str] = []
                        for c in s:
                            if c.choices and c.choices[0].delta.content:
                                parts.append(c.choices[0].delta.content)
                        return "".join(parts)

                    raw = await asyncio.to_thread(_stream_fallback_sse)
                    answer = clean_agent_response(raw.strip())
                except Exception as fallback_error:
                    logger.error("Direct LLM fallback also failed: %s", fallback_error)
                    yield _sse({"type": "error", "detail": str(error)})
                    return

            # Extract sources (same logic as /query)
            answer, cited_fhir_ids = extract_sources_used(answer)
            source_ids.extend(_map_cited_fhir_ids(conn, cited_fhir_ids))

            # Collect Milvus document IDs stored by tool wrappers during agent run
            if agent_ran_successfully:
                milvus_doc_ids = getattr(_tls, "collected_milvus_doc_ids", set())
                if not milvus_doc_ids:
                    agent_history = _get_agent_history(agent)
                    milvus_doc_ids = _extract_milvus_doc_ids_from_history(agent_history)
                milvus_source_ids = _map_milvus_doc_ids(conn, milvus_doc_ids)
                source_ids.extend(milvus_source_ids)

            # Store messages
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
                (conversation_id, "user", request.query),
            )
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content, sources) VALUES (?, ?, ?, ?)",
                (conversation_id, "assistant", answer, json.dumps(source_ids)),
            )
            conn.commit()

            # Build source references
            unique_source_ids = list(dict.fromkeys(source_ids))
            sources_refs = []
            for sid in unique_source_ids[:10]:
                source = get_source(sid)
                if source:
                    sources_refs.append({
                        "source_id": sid,
                        "source_type": source["source_type"],
                        "resource_type": source["resource_type"] or "",
                        "title": source["title"],
                        "excerpt": source["preview"],
                        "content_markdown": source["content_markdown"],
                    })

            yield _sse({
                "type": "complete",
                "answer": answer,
                "sources": sources_refs,
                "conversation_id": conversation_id,
                "follow_up_questions": [
                    "What are the treatment options?",
                    "What does the latest imaging show?",
                    "Are there any relevant clinical guidelines?",
                ],
            })

        except Exception as e:
            logger.exception("Stream query failed")
            yield _sse({"type": "error", "detail": str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
