"""Patient summary generation, caching, and update endpoints."""
import json
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import StreamingResponse

from database import get_db_connection, register_source, get_patient_metadata
from models import (
    SummaryResponse, UpdateSummaryRequest, StartGenerationRequest,
    RegenerateFieldRequest,
)
from services.summary import (
    generate_summary_with_llm,
    regenerate_single_field,
    _active_generations,
    _generation_key,
)
from services.fhir_sync import fetch_and_register_fhir_resources
from config import LLM_MODEL
from agent.model import llm_client

logger = logging.getLogger("medgemma-agent")

router = APIRouter(tags=["patients"])


def _get_current_fhir_hash(patient_id: str) -> str | None:
    """Return the current FHIR data hash for a patient, or None."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT fhir_data_hash FROM patient_hashes WHERE patient_id = ?",
        (patient_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# POST /start_generation
# ---------------------------------------------------------------------------

@router.post("/start_generation")
async def start_generation(request: StartGenerationRequest):
    """Fire-and-forget summary generation that survives frontend disconnects."""
    patient_id = request.patient_id
    clinical_question = request.clinical_question or None
    key = _generation_key(patient_id, clinical_question)

    # Already running?
    if key in _active_generations:
        task = _active_generations[key].get("task")
        if task and not task.done():
            return {"status": "running", "started_at": _active_generations[key]["started_at"]}
        else:
            # Stale entry -- clean up
            _active_generations.pop(key, None)

    # Check cache first (unless skip_cache is set)
    if not request.skip_cache:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        if clinical_question:
            cached = conn.execute(
                "SELECT id, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung = ? ORDER BY created_at DESC LIMIT 1",
                (patient_id, clinical_question),
            ).fetchone()
        else:
            cached = conn.execute(
                "SELECT id, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung IS NULL ORDER BY created_at DESC LIMIT 1",
                (patient_id,),
            ).fetchone()
        conn.close()
        if cached:
            current_hash = _get_current_fhir_hash(patient_id)
            cached_hash = cached["fhir_data_hash"]
            if current_hash and cached_hash and current_hash != cached_hash:
                logger.info(
                    "start_generation: stale cache for patient %s (hash %s→%s), regenerating",
                    patient_id, cached_hash[:8], current_hash[:8],
                )
            else:
                return {"status": "cached"}

    # Clear stale progress and insert initial row so polling never sees "not_started"
    try:
        conn = get_db_connection()
        conn.execute("""
            DELETE FROM generation_progress
            WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
        """, (patient_id, clinical_question or ''))
        conn.execute("""
            INSERT INTO generation_progress
                (patient_id, clinical_question, current_field, current_step, total_steps, status_message)
            VALUES (?, ?, 'initializing', 0, 1, 'Starting generation...')
        """, (patient_id, clinical_question))
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Launch background generation
    started_at = datetime.utcnow().isoformat() + "Z"

    async def _run_generation():
        try:
            logger.info("Background generation started for %s (question=%s)", patient_id, clinical_question)
            result = await generate_summary_with_llm(
                patient_id=patient_id,
                fragestellung=clinical_question,
            )
            is_failed = any(m.field == "all" for m in result.missing_info)
            if not is_failed:
                try:
                    conn = get_db_connection()
                    ph_row = conn.execute(
                        "SELECT fhir_data_hash FROM patient_hashes WHERE patient_id = ?", (patient_id,)
                    ).fetchone()
                    cur_hash = ph_row[0] if ph_row else None
                    conn.execute(
                        "INSERT INTO summaries (patient_id, fragestellung, summary, fhir_data_hash) VALUES (?, ?, ?, ?)",
                        (patient_id, clinical_question, json.dumps(result.dict()), cur_hash),
                    )
                    conn.commit()
                    conn.close()
                    logger.info("Background generation completed and cached for %s", patient_id)
                except Exception as cache_err:
                    logger.warning("Failed to cache background summary: %s", cache_err)
            else:
                logger.warning("Background generation for %s produced a failed summary", patient_id)
        except Exception:
            logger.exception("Background generation failed for %s", patient_id)
        finally:
            _active_generations.pop(key, None)

    task = asyncio.create_task(_run_generation())
    _active_generations[key] = {
        "patient_id": patient_id,
        "clinical_question": clinical_question,
        "started_at": started_at,
        "task": task,
    }
    return {"status": "started", "started_at": started_at}


# ---------------------------------------------------------------------------
# GET /generation_status
# ---------------------------------------------------------------------------

@router.get("/generation_status")
async def generation_status(
    patient_id: str = Query(..., description="Patient ID"),
):
    """Return active background generations for a patient."""
    active = []
    for key, info in list(_active_generations.items()):
        if info["patient_id"] != patient_id:
            continue
        task = info.get("task")
        if task and task.done():
            _active_generations.pop(key, None)
            continue
        active.append({
            "clinical_question": info["clinical_question"],
            "started_at": info["started_at"],
        })
    return {"active": active}


# ---------------------------------------------------------------------------
# POST /cancel_generation
# ---------------------------------------------------------------------------

@router.post("/cancel_generation")
async def cancel_generation(
    patient_id: str = Query(..., description="Patient ID"),
    clinical_question: Optional[str] = Query(None, description="Clinical question"),
):
    """Cancel a running background generation task."""
    key = _generation_key(patient_id, clinical_question)
    info = _active_generations.pop(key, None)
    if not info:
        return {"status": "not_found"}
    task = info.get("task")
    if task and not task.done():
        task.cancel()
    # Clean up progress row
    try:
        conn = get_db_connection()
        conn.execute(
            "DELETE FROM generation_progress WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')",
            (patient_id, clinical_question or ''),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return {"status": "cancelled"}


# ---------------------------------------------------------------------------
# GET /get_summary
# ---------------------------------------------------------------------------

@router.get("/get_summary", response_model=SummaryResponse)
async def get_summary(
    patient_id: str = Query(..., description="Patient ID"),
    clinical_question: Optional[str] = Query(None, description="Clinical question"),
    fragestellung: Optional[str] = Query(None, description="Clinical question (legacy)", deprecated=True),
    skip_cache: bool = Query(False, description="Force regeneration, ignoring cache"),
    correction_loop_enabled: Optional[bool] = Query(
        None,
        description="Enable/disable per-category summary correction loop (defaults to env setting)",
    ),
    correction_loop_passes: Optional[int] = Query(
        None,
        ge=0,
        le=5,
        description="Number of correction passes per category (defaults to env setting, usually 2)",
    ),
):
    """Generate a comprehensive clinical summary using the LLM agent."""
    try:
        effective_clinical_question = clinical_question or fragestellung
        custom_correction_requested = (
            correction_loop_enabled is not None or correction_loop_passes is not None
        )
        if custom_correction_requested and not skip_cache:
            logger.info("Bypassing summary cache because custom correction loop settings were requested")
            skip_cache = True

        # Check cache first (unless explicitly skipped)
        if not skip_cache:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            if effective_clinical_question:
                cached = conn.execute(
                    "SELECT id, summary, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung = ? ORDER BY created_at DESC LIMIT 1",
                    (patient_id, effective_clinical_question),
                ).fetchone()
            else:
                cached = conn.execute(
                    "SELECT id, summary, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung IS NULL ORDER BY created_at DESC LIMIT 1",
                    (patient_id,),
                ).fetchone()

            conn.close()

            if cached and cached["summary"]:
                # Invalidate cache when FHIR data has changed since the summary was generated
                current_hash = _get_current_fhir_hash(patient_id)
                cached_hash = cached["fhir_data_hash"]
                if current_hash and cached_hash and current_hash != cached_hash:
                    logger.info(
                        "Cached summary for patient %s is stale (hash %s→%s), regenerating",
                        patient_id, cached_hash[:8], current_hash[:8],
                    )
                else:
                    logger.info("Returning cached summary for patient %s", patient_id)
                    # Mark any in-progress generation as complete
                    try:
                        conn = get_db_connection()
                        conn.execute("""
                            UPDATE generation_progress
                            SET completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                            WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                            AND completed_at IS NULL
                        """, (patient_id, effective_clinical_question or ''))
                        conn.commit()
                        conn.close()
                    except Exception as e:
                        logger.warning("Failed to mark cached summary progress as complete: %s", e)

                    return SummaryResponse(**json.loads(cached["summary"]))

        # Generate fresh summary
        result = await generate_summary_with_llm(
            patient_id=patient_id,
            fragestellung=effective_clinical_question,
            correction_enabled=correction_loop_enabled,
            correction_passes=correction_loop_passes,
        )

        # Only cache if the summary was actually generated successfully
        is_failed = any(m.field == "all" for m in result.missing_info)
        if not is_failed:
            try:
                conn = get_db_connection()
                ph_row = conn.execute(
                    "SELECT fhir_data_hash FROM patient_hashes WHERE patient_id = ?", (patient_id,)
                ).fetchone()
                cur_hash = ph_row[0] if ph_row else None
                conn.execute(
                    "INSERT INTO summaries (patient_id, fragestellung, summary, fhir_data_hash) VALUES (?, ?, ?, ?)",
                    (patient_id, effective_clinical_question, json.dumps(result.dict()), cur_hash),
                )
                conn.commit()
                conn.close()
                logger.info(
                    "Cached summary for patient %s with clinical_question=%s",
                    patient_id,
                    effective_clinical_question,
                )
            except Exception as cache_err:
                logger.warning("Failed to cache summary: %s", cache_err)
        else:
            logger.warning("Skipping cache for patient %s - summary generation failed", patient_id)

        return result
    except Exception as e:
        logger.exception("get_summary failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /get_summary/stream
# ---------------------------------------------------------------------------

@router.get("/get_summary/stream")
async def get_summary_stream(
    patient_id: str = Query(..., description="Patient ID"),
    clinical_question: Optional[str] = Query(None, description="Clinical question"),
    fragestellung: Optional[str] = Query(None, description="Clinical question (legacy)", deprecated=True),
    skip_cache: bool = Query(False, description="Force regeneration, ignoring cache"),
    correction_loop_enabled: Optional[bool] = Query(None),
    correction_loop_passes: Optional[int] = Query(None, ge=0, le=5),
):
    """SSE streaming version of /get_summary. Sends keepalive status events
    during long LLM generation to prevent proxy/browser timeouts."""

    async def event_generator():
        def _sse(payload: dict) -> str:
            return f"data: {json.dumps(payload)}\n\n"

        try:
            effective_clinical_question = clinical_question or fragestellung
            custom_correction_requested = (
                correction_loop_enabled is not None or correction_loop_passes is not None
            )
            effective_skip_cache = skip_cache or custom_correction_requested

            yield _sse({"type": "status", "message": "Checking cache..."})

            # Check cache first
            if not effective_skip_cache:
                conn = get_db_connection()
                conn.row_factory = sqlite3.Row
                if effective_clinical_question:
                    cached = conn.execute(
                        "SELECT id, summary, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung = ? ORDER BY created_at DESC LIMIT 1",
                        (patient_id, effective_clinical_question),
                    ).fetchone()
                else:
                    cached = conn.execute(
                        "SELECT id, summary, fhir_data_hash FROM summaries WHERE patient_id = ? AND fragestellung IS NULL ORDER BY created_at DESC LIMIT 1",
                        (patient_id,),
                    ).fetchone()

                conn.close()

                if cached and cached["summary"]:
                    # Invalidate cache when FHIR data has changed since the summary was generated
                    current_hash = _get_current_fhir_hash(patient_id)
                    cached_hash = cached["fhir_data_hash"]
                    cache_is_fresh = not (current_hash and cached_hash and current_hash != cached_hash)

                    if cache_is_fresh:
                        logger.info("Returning cached summary for patient %s", patient_id)
                        # Mark any in-progress generation as complete
                        try:
                            conn = get_db_connection()
                            conn.execute("""
                                UPDATE generation_progress
                                SET completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                                AND completed_at IS NULL
                            """, (patient_id, effective_clinical_question or ''))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            logger.warning("Failed to mark cached summary progress as complete: %s", e)

                        summary = SummaryResponse(**json.loads(cached["summary"]))
                        yield _sse({"type": "complete", "summary": summary.dict()})
                        return
                    else:
                        logger.info(
                            "Cached summary for patient %s is stale (hash %s→%s), regenerating",
                            patient_id, cached_hash[:8], current_hash[:8],
                        )

            yield _sse({"type": "status", "message": "Fetching patient data..."})

            # Run generation with a status queue for per-field progress events
            _summary_status_q: asyncio.Queue = asyncio.Queue()
            gen_task = asyncio.create_task(generate_summary_with_llm(
                patient_id=patient_id,
                fragestellung=effective_clinical_question,
                correction_enabled=correction_loop_enabled,
                correction_passes=correction_loop_passes,
                status_queue=_summary_status_q,
            ))

            # Stream per-field status events as they arrive
            while not gen_task.done():
                try:
                    item = await asyncio.wait_for(_summary_status_q.get(), timeout=15.0)
                    if item is not None:
                        yield _sse(item)
                except asyncio.TimeoutError:
                    # No status event yet -- send keepalive
                    yield _sse({"type": "status", "message": "Processing..."})

            # Drain any remaining status events
            while not _summary_status_q.empty():
                item = _summary_status_q.get_nowait()
                if item is not None:
                    yield _sse(item)

            # Retrieve result; re-raise if the task failed unexpectedly
            try:
                result = gen_task.result()
            except Exception as task_err:
                logger.exception("generate_summary_with_llm task raised unexpectedly")
                yield _sse({"type": "error", "detail": f"Summary generation failed: {task_err}"})
                return

            # Cache the result
            is_failed = any(m.field == "all" for m in result.missing_info)
            if not is_failed:
                try:
                    conn = get_db_connection()
                    ph_row = conn.execute(
                        "SELECT fhir_data_hash FROM patient_hashes WHERE patient_id = ?", (patient_id,)
                    ).fetchone()
                    cur_hash = ph_row[0] if ph_row else None
                    conn.execute(
                        "INSERT INTO summaries (patient_id, fragestellung, summary, fhir_data_hash) VALUES (?, ?, ?, ?)",
                        (patient_id, effective_clinical_question, json.dumps(result.dict()), cur_hash),
                    )
                    conn.commit()
                    conn.close()
                except Exception as cache_err:
                    logger.warning("Failed to cache summary: %s", cache_err)

            yield _sse({"type": "complete", "summary": result.dict()})

        except Exception as e:
            logger.exception("get_summary stream failed")
            detail = str(e)
            if "input stream" in detail.lower() or "stream" in detail.lower():
                detail = f"LLM connection error: {detail}. The LLM server may be overloaded or unreachable. Please try again."
            yield _sse({"type": "error", "detail": detail})

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# GET /check_summary_cache
# ---------------------------------------------------------------------------

@router.get("/check_summary_cache")
async def check_summary_cache(
    patient_id: str = Query(..., description="Patient ID"),
):
    """Return all cached summaries for a patient."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, fragestellung, created_at FROM summaries WHERE patient_id = ? ORDER BY created_at DESC",
        (patient_id,),
    ).fetchall()
    conn.close()

    entries = [
        {
            "id": row["id"],
            "clinical_question": row["fragestellung"],
            "fragestellung": row["fragestellung"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return {"entries": entries}


# ---------------------------------------------------------------------------
# DELETE /delete_summary_cache
# ---------------------------------------------------------------------------

@router.delete("/delete_summary_cache")
async def delete_summary_cache(
    patient_id: str = Query(..., description="Patient ID"),
    entry_id: Optional[int] = Query(None, description="Specific cache entry ID to delete (omit to delete all)"),
):
    """Delete cached summaries for a patient. If entry_id given, delete only that entry."""
    conn = get_db_connection()
    if entry_id is not None:
        cursor = conn.execute(
            "DELETE FROM summaries WHERE id = ? AND patient_id = ?",
            (entry_id, patient_id),
        )
    else:
        cursor = conn.execute(
            "DELETE FROM summaries WHERE patient_id = ?",
            (patient_id,),
        )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    logger.info("Deleted %d cached summaries for patient %s", deleted, patient_id)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# GET /generation_progress/{patient_id}
# ---------------------------------------------------------------------------

@router.get("/generation_progress/{patient_id}")
async def get_generation_progress(
    patient_id: str,
    clinical_question: Optional[str] = Query(None, description="Clinical question / fragestellung"),
):
    """Get current progress of summary generation (for resume on page reload).

    Returns:
        - status: "not_started" | "in_progress" | "completed" | "stale"
        - If in_progress: current_field, current_step, total_steps, message
        - If completed: completed_at timestamp
        - If stale: last_update timestamp (updated >2 min ago)
    """
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        if clinical_question is not None:
            row = conn.execute("""
                SELECT * FROM generation_progress
                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                ORDER BY started_at DESC LIMIT 1
            """, (patient_id, clinical_question or "")).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM generation_progress
                WHERE patient_id = ?
                ORDER BY started_at DESC LIMIT 1
            """, (patient_id,)).fetchone()

        conn.close()

        if not row:
            return {"status": "not_started"}

        row_clinical_question = row["clinical_question"]
        gen_key = _generation_key(patient_id, row_clinical_question)
        info = _active_generations.get(gen_key)
        task = info.get("task") if info else None
        if task and task.done():
            _active_generations.pop(gen_key, None)
            task = None
        is_task_active = bool(task and not task.done())

        # If completed, return completion info
        if row["completed_at"]:
            return {
                "status": "completed",
                "clinical_question": row_clinical_question,
                "completed_at": row["completed_at"],
            }

        # Reconcile stale/incomplete progress rows:
        # if no active task but a cache entry exists that is newer than this run's
        # start time, treat it as completed and self-heal the progress row.
        if not is_task_active:
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            if row_clinical_question:
                cached = conn.execute(
                    """
                    SELECT created_at
                    FROM summaries
                    WHERE patient_id = ? AND fragestellung = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (patient_id, row_clinical_question),
                ).fetchone()
            else:
                cached = conn.execute(
                    """
                    SELECT created_at
                    FROM summaries
                    WHERE patient_id = ? AND fragestellung IS NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (patient_id,),
                ).fetchone()

            if cached and row["started_at"] and cached["created_at"] >= row["started_at"]:
                try:
                    conn.execute(
                        """
                        UPDATE generation_progress
                        SET completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND completed_at IS NULL
                        """,
                        (row["id"],),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("Failed to reconcile generation_progress completion: %s", e)
                finally:
                    conn.close()

                return {
                    "status": "completed",
                    "clinical_question": row_clinical_question,
                    "completed_at": cached["created_at"],
                }

            conn.close()

        # If generation failed and no task is active, surface as stale immediately.
        if row["error"] and not is_task_active:
            return {
                "status": "stale",
                "clinical_question": row_clinical_question,
                "last_update": row["updated_at"],
                "message": row["status_message"] or row["error"],
            }

        # If updated more than 20 minutes ago, consider it stale
        try:
            updated_at = datetime.fromisoformat(row["updated_at"])
            if (not is_task_active) and (datetime.now() - updated_at > timedelta(minutes=20)):
                return {
                    "status": "stale",
                    "clinical_question": row_clinical_question,
                    "last_update": row["updated_at"],
                    "message": row["status_message"],
                }
        except (ValueError, TypeError):
            pass

        # Still in progress
        return {
            "status": "in_progress",
            "clinical_question": row_clinical_question,
            "current_field": row["current_field"],
            "current_step": row["current_step"],
            "total_steps": row["total_steps"],
            "message": row["status_message"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
        }

    except Exception as e:
        logger.exception("get_generation_progress failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /update_summary
# ---------------------------------------------------------------------------

@router.post("/update_summary", response_model=SummaryResponse)
async def update_summary(request: UpdateSummaryRequest):
    """Update a cached patient summary with user-provided information for missing fields.

    Uses LLM to extract structured data from user input and patch the specific fields
    in the cached summary without regenerating the entire summary.
    """
    try:
        effective_clinical_question = request.clinical_question or request.fragestellung
        # Load cached summary
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        if effective_clinical_question:
            cached = conn.execute(
                "SELECT id, summary FROM summaries WHERE patient_id = ? AND fragestellung = ? ORDER BY created_at DESC LIMIT 1",
                (request.patient_id, effective_clinical_question),
            ).fetchone()
        else:
            cached = conn.execute(
                "SELECT id, summary FROM summaries WHERE patient_id = ? AND fragestellung IS NULL ORDER BY created_at DESC LIMIT 1",
                (request.patient_id,),
            ).fetchone()
        conn.close()

        if not cached or not cached["summary"]:
            raise HTTPException(status_code=404, detail="No cached summary found for this patient")

        summary_data = json.loads(cached["summary"])
        cache_id = cached["id"]

        # Register user input as a source
        source_id, _ = register_source(
            source_type="user_input",
            resource_type="UserInput",
            content=request.user_input,
            title="Clinician-provided information",
            patient_id=request.patient_id,
        )

        # Build LLM prompt to extract structured updates
        missing_fields_str = ", ".join(request.missing_fields) if request.missing_fields else "any relevant fields"

        prompt = f"""You are updating a patient's clinical summary with new information provided by a clinician.

The clinician said: "{request.user_input}"

The following fields were flagged as missing: {missing_fields_str}

Based on the clinician's input, return a JSON object with ONLY the fields that should be updated.
Use the exact field structure from the summary schema:

- "chemo": [{{"type": "...", "name": "...", "description": "...", "start_date": null, "end_date": null, "cycles": null, "efficacy": null, "intolerance": null, "source_ids": ["{source_id}"]}}]
- "radiation": [{{"type": "...", "name": "...", "description": "...", "start_date": null, "end_date": null, "efficacy": null, "intolerance": null, "source_ids": ["{source_id}"]}}]
- "imaging": [{{"type": "...", "modality": null, "organ_system": null, "date": null, "key_findings": "...", "assessment": null, "progression": null, "metastatic_pattern": null, "disease_evolution": null, "comparison_to_prior_staging": null, "source_ids": ["{source_id}"]}}]
- "initial_diagnosis": {{"value": "...", "source_ids": ["{source_id}"]}}
- "staging": {{"value": "...", "source_ids": ["{source_id}"]}}
- "pathology": {{"key_findings": [...], "mutations": [...], "source_ids": ["{source_id}"]}}
- "comorbidities": {{"conditions": [...], "risk_factors": [...], "source_ids": ["{source_id}"]}}
- "contraindications": {{"items": [...], "source_ids": ["{source_id}"]}}
- "general_condition": {{"ecog": null, "description": "...", "source_ids": ["{source_id}"]}}
- "symptoms": {{"items": [...], "source_ids": ["{source_id}"]}}
- "patient_wishes": {{"text": "...", "therapy_goal": null, "needs_clarification": false, "source_ids": ["{source_id}"]}}

IMPORTANT:
- Only include fields that the clinician actually provided information for.
- If the input does not contain actionable medical information, return an empty JSON object {{}}.
- Always include "{source_id}" in the source_ids for updated fields.
- For list fields (chemo, radiation, imaging), return NEW items to ADD (they will be appended).
- For object fields (general_condition, etc.), return the full updated object.
- Return ONLY valid JSON, no extra text."""

        def _stream_update():
            stream = llm_client.chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                response_format={"type": "json_object"},
                stream=True,
            )
            parts: list[str] = []
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    parts.append(chunk.choices[0].delta.content)
            return "".join(parts)

        content = (await asyncio.to_thread(_stream_update)).strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        updates = json.loads(content)
        logger.info("LLM extracted updates for patient %s: %s", request.patient_id, list(updates.keys()))

        if not updates:
            logger.info("No actionable updates extracted from user input")
            return SummaryResponse(**summary_data)

        # Merge updates into cached summary
        for field, value in updates.items():
            if field in ("chemo", "radiation"):
                # Append new items to therapy subfields
                therapies = summary_data.setdefault("therapies", {"chemo": [], "radiation": []})
                if isinstance(therapies, dict):
                    existing = therapies.get(field, [])
                    if isinstance(value, list):
                        existing.extend(value)
                    therapies[field] = existing
            elif field == "imaging":
                # Append new items to list fields
                existing = summary_data.get(field, [])
                if isinstance(value, list):
                    existing.extend(value)
                summary_data[field] = existing
            elif field in summary_data:
                if isinstance(value, dict) and isinstance(summary_data[field], dict):
                    for k, v in value.items():
                        if k == "source_ids":
                            existing_ids = summary_data[field].get("source_ids", [])
                            summary_data[field]["source_ids"] = list(set(existing_ids + v))
                        elif k == "items" and isinstance(v, list):
                            existing_items = summary_data[field].get("items", [])
                            summary_data[field]["items"] = list(set(existing_items + v))
                        elif k == "conditions" and isinstance(v, list):
                            existing_conds = summary_data[field].get("conditions", [])
                            summary_data[field]["conditions"] = list(set(existing_conds + v))
                        elif k == "risk_factors" and isinstance(v, list):
                            existing_rf = summary_data[field].get("risk_factors", [])
                            summary_data[field]["risk_factors"] = list(set(existing_rf + v))
                        elif k == "key_findings" and isinstance(v, list):
                            existing_kf = summary_data[field].get("key_findings", [])
                            summary_data[field]["key_findings"] = list(set(existing_kf + v))
                        elif k == "mutations" and isinstance(v, list):
                            existing_mut = summary_data[field].get("mutations", [])
                            summary_data[field]["mutations"] = list(set(existing_mut + v))
                        else:
                            summary_data[field][k] = v
                else:
                    summary_data[field] = value

        # Update missing_info: remove fields that were just filled
        updated_fields = set(updates.keys())
        summary_data["missing_info"] = [
            mi for mi in summary_data.get("missing_info", [])
            if mi.get("field") not in updated_fields
        ]

        # Update timestamp
        summary_data["generated_at"] = datetime.utcnow().isoformat()

        # Save updated summary back to cache (replace existing entry)
        conn = get_db_connection()
        conn.execute(
            "UPDATE summaries SET summary = ? WHERE id = ?",
            (json.dumps(summary_data), cache_id),
        )
        conn.commit()
        conn.close()

        logger.info("Updated cached summary for patient %s, fields: %s", request.patient_id, list(updates.keys()))
        return SummaryResponse(**summary_data)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("update_summary failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /patient/{patient_id}/metadata
# ---------------------------------------------------------------------------

@router.get("/patient/{patient_id}/metadata")
async def get_patient_metadata_endpoint(patient_id: str):
    """Return stored cancer-type classification and guideline collection preference for a patient."""
    meta = get_patient_metadata(patient_id)
    if meta is None:
        return {
            "patient_id": patient_id,
            "cancer_type_raw": None,
            "guideline_cancer_types": ["nsclc"],
        }
    return {"patient_id": patient_id, **meta}


# ---------------------------------------------------------------------------
# POST /regenerate_field
# ---------------------------------------------------------------------------

@router.post("/regenerate_field", response_model=SummaryResponse)
async def regenerate_field(request: RegenerateFieldRequest):
    """Regenerate a single summary field using FHIR data + clinician feedback."""
    try:
        effective_cq = request.clinical_question

        # Load cached summary
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        if effective_cq:
            cached = conn.execute(
                "SELECT id, summary FROM summaries WHERE patient_id = ? AND fragestellung = ? ORDER BY created_at DESC LIMIT 1",
                (request.patient_id, effective_cq),
            ).fetchone()
        else:
            cached = conn.execute(
                "SELECT id, summary FROM summaries WHERE patient_id = ? AND fragestellung IS NULL ORDER BY created_at DESC LIMIT 1",
                (request.patient_id,),
            ).fetchone()
        conn.close()

        if not cached or not cached["summary"]:
            raise HTTPException(status_code=404, detail="No cached summary found for this patient")

        summary_data = json.loads(cached["summary"])
        cache_id = cached["id"]

        # Register clinician feedback as a source
        source_id, _ = register_source(
            source_type="user_input",
            resource_type="UserFeedback",
            content=request.feedback,
            title=f"Clinician feedback on {request.field_name}",
            patient_id=request.patient_id,
        )

        # Regenerate the single field
        regenerated = await regenerate_single_field(
            patient_id=request.patient_id,
            field_name=request.field_name,
            feedback=request.feedback,
            clinical_question=effective_cq,
            summary_data=summary_data,
        )

        # Replace field in summary (chemo/radiation are nested under therapies)
        if request.field_name == "therapies":
            summary_data["therapies"] = regenerated
        elif request.field_name in ("chemo", "radiation"):
            therapies = summary_data.setdefault("therapies", {"chemo": [], "radiation": []})
            if isinstance(therapies, dict):
                therapies[request.field_name] = regenerated
        else:
            summary_data[request.field_name] = regenerated

        # Add feedback source_id to the field's source_ids
        if request.field_name == "therapies" and isinstance(regenerated, dict):
            # Composite therapies: add source_id to each item in both sub-lists
            for sub_key in ("chemo", "radiation"):
                for item in regenerated.get(sub_key, []):
                    if isinstance(item, dict) and "source_ids" in item:
                        if source_id not in item["source_ids"]:
                            item["source_ids"].append(source_id)
        elif isinstance(regenerated, dict) and "source_ids" in regenerated:
            if source_id not in regenerated["source_ids"]:
                regenerated["source_ids"].append(source_id)
        elif isinstance(regenerated, list):
            # For list fields (chemo, radiation, imaging), add to each item
            for item in regenerated:
                if isinstance(item, dict) and "source_ids" in item:
                    if source_id not in item["source_ids"]:
                        item["source_ids"].append(source_id)

        summary_data["generated_at"] = datetime.utcnow().isoformat()

        # Save back to cache
        conn = get_db_connection()
        conn.execute(
            "UPDATE summaries SET summary = ? WHERE id = ?",
            (json.dumps(summary_data), cache_id),
        )
        conn.commit()
        conn.close()

        logger.info("Regenerated field '%s' for patient %s", request.field_name, request.patient_id)
        return SummaryResponse(**summary_data)

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("regenerate_field failed")
        raise HTTPException(status_code=500, detail=str(e))
