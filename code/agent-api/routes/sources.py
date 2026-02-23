"""Source list, timeline, keypoint extraction, and source detail endpoints."""
import json
import time
import asyncio
import logging
import sqlite3
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from database import get_db_connection, get_source
from models import (
    SourceItem, GetListResponse,
    TimelineEventItem, TimelineResponse, TreatmentResponseItem,
    SourceDetailResponse,
    KeypointItem, KeypointResult, AddKeypointsRequest,
)
from services.fhir_sync import fetch_and_register_fhir_resources
from services.timeline import group_chemo_timeline_events, compute_treatment_responses
from services.keypoints import (
    extract_keypoints_for_chunk,
    generate_keypoints_for_source,
    generate_keypoints_batch,
)
from services.llm_utils import chunk_text
from config import FHIR_PATIENT_IDS, BASE_CLINICAL_QUESTION

logger = logging.getLogger("medgemma-agent")

router = APIRouter(tags=["sources"])


# ---------------------------------------------------------------------------
# GET /get_list
# ---------------------------------------------------------------------------

@router.get("/get_list", response_model=GetListResponse)
async def get_list(
    patient_id: Optional[str] = Query(None, description="Filter by patient ID"),
    source_type: Optional[str] = Query(None, description="Filter by source type (fhir, milvus)")
):
    """Return list of ALL FHIR resources and Milvus documents."""
    try:
        # If patient_id provided, ensure we have their FHIR data (skip if already cached)
        if patient_id:
            conn_check = get_db_connection()
            has_patient = conn_check.execute(
                "SELECT 1 FROM sources WHERE patient_id = ? AND source_type = 'fhir' LIMIT 1",
                (patient_id,),
            ).fetchone()
            conn_check.close()
            if not has_patient:
                await fetch_and_register_fhir_resources(patient_id, retry=True)
        elif source_type == "fhir" and FHIR_PATIENT_IDS:
            # No patient_id: ensure ALL configured patients are registered
            conn_check = get_db_connection()
            cached_pids = {
                row[0] for row in conn_check.execute(
                    "SELECT DISTINCT patient_id FROM sources WHERE source_type = 'fhir' AND resource_type = 'Patient'"
                ).fetchall()
            }
            conn_check.close()
            missing_pids = [pid for pid in FHIR_PATIENT_IDS if pid not in cached_pids]
            for pid in missing_pids:
                try:
                    await fetch_and_register_fhir_resources(pid, retry=True)
                except Exception as e:
                    logger.warning("Failed to register patient %s: %s", pid, e)

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        query = "SELECT * FROM sources WHERE 1=1"
        params: list = []

        if patient_id:
            query += " AND (patient_id = ? OR patient_id IS NULL)"
            params.append(patient_id)

        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)

        query += " ORDER BY date DESC, created_at DESC"

        rows = conn.execute(query, params).fetchall()

        items = []
        for row in rows:
            items.append(SourceItem(
                source_id=row["source_id"],
                source_type=row["source_type"],
                resource_type=row["resource_type"] or "",
                fhir_id=row["fhir_id"],
                milvus_document_id=row["milvus_document_id"],
                title=row["title"],
                date=row["date"],
                preview=row["preview"],
                content_markdown=row["content_markdown"]
            ))

        conn.close()

        return GetListResponse(
            items=items,
            total=len(items)
        )

    except Exception as e:
        logger.exception("get_list failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /timeline/{patient_id}
# ---------------------------------------------------------------------------

@router.get("/timeline/{patient_id}", response_model=TimelineResponse)
async def get_timeline(patient_id: str):
    """Returns chronologically ordered patient events for timeline view."""
    try:
        # Keep timeline responsive: only trigger expensive FHIR fetch on first load.
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        existing_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM timeline_events WHERE patient_id = ?",
            (patient_id,),
        ).fetchone()["cnt"]
        conn.close()

        await fetch_and_register_fhir_resources(patient_id, retry=True)

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        # Event types excluded from LLM keypoint extraction
        _KP_SKIP_TYPES_TL = ('Lab', 'Chemotherapy', 'Medication')
        events = conn.execute("""
            SELECT * FROM timeline_events
            WHERE patient_id = ? AND event_type NOT IN (?, ?, ?)
            ORDER BY event_date DESC, id DESC
        """, (patient_id, *_KP_SKIP_TYPES_TL)).fetchall()

        # Also fetch chemo + medication events (Medication events may contain
        # chemo drugs; is_chemo_event() in group_chemo_timeline_events handles
        # filtering).  Must match the summary generation query to produce
        # identical cycle IDs for treatment_responses cache lookups.
        chemo_events = conn.execute("""
            SELECT * FROM timeline_events
            WHERE patient_id = ? AND event_type IN ('Chemotherapy', 'Medication')
            ORDER BY event_date DESC, id DESC
        """, (patient_id,)).fetchall()

        # Check if keypoints exist for non-chemo/non-lab events and generate missing ones
        source_ids = [e["source_id"] for e in events if e["source_id"]]
        llm_insights: dict[str, str] = {}
        if source_ids:
            placeholders = ",".join(["?" for _ in source_ids])
            keypoint_rows = conn.execute(
                f"SELECT k.source_id, k.keypoints, k.content_hash AS kp_hash, s.content_hash AS src_hash "
                f"FROM keypoints k JOIN sources s ON k.source_id = s.source_id "
                f"WHERE k.source_id IN ({placeholders})",
                source_ids,
            ).fetchall()
            keypoint_sources = set()
            stale_keypoint_sources = []
            for row in keypoint_rows:
                keypoint_sources.add(row["source_id"])
                kp_hash = row["kp_hash"]
                src_hash = row["src_hash"]
                if kp_hash and src_hash and kp_hash != src_hash:
                    stale_keypoint_sources.append(row["source_id"])
                    continue
                try:
                    kps = json.loads(row["keypoints"]) if row["keypoints"] else []
                    if kps:
                        sorted_kps = sorted(
                            kps,
                            key=lambda kp: (kp.get("priority", 3) if isinstance(kp, dict) else 3),
                        )
                        top_texts = [
                            (kp["text"] if isinstance(kp, dict) else str(kp))
                            for kp in sorted_kps[:3]
                        ]
                        llm_insights[row["source_id"]] = "; ".join(top_texts)
                except (json.JSONDecodeError, TypeError):
                    pass

            missing_keypoint_sources = [sid for sid in source_ids if sid not in keypoint_sources]
            sources_needing_keypoints = missing_keypoint_sources + stale_keypoint_sources
            if sources_needing_keypoints:
                logger.info(
                    "Generating keypoints for %d timeline sources (%d missing, %d stale)",
                    len(sources_needing_keypoints),
                    len(missing_keypoint_sources),
                    len(stale_keypoint_sources),
                )
                asyncio.create_task(generate_keypoints_batch(patient_id, sources_needing_keypoints))

        conn.close()

        # Convert to dicts for grouping; prefer LLM-generated insights over raw FHIR strings
        all_timeline_events = list(events) + list(chemo_events)
        raw_events = [
            {
                "id": str(e["id"]),
                "source_id": e["source_id"],
                "event_type": e["event_type"],
                "event_date": e["event_date"],
                "title": e["title"],
                "key_insight": llm_insights.get(e["source_id"]) or e["key_insight"],
                "priority": e["priority"] or 3
            }
            for e in all_timeline_events
        ]

        # Group chemotherapy events into cycles
        grouped_events = group_chemo_timeline_events(raw_events)

        # Compute treatment responses for CT imaging reports (cached + background LLM)
        treatment_responses = compute_treatment_responses(
            patient_id,
            grouped_events,
        )

        return TimelineResponse(
            patient_id=patient_id,
            events=[
                TimelineEventItem(
                    id=str(e["id"]),
                    source_id=e["source_id"],
                    type=e["event_type"],
                    date=e["event_date"],
                    title=e["title"],
                    key_insight=e["key_insight"],
                    priority=e["priority"] or 3,
                    sub_source_ids=e.get("sub_source_ids", []),
                )
                for e in grouped_events
            ],
            treatment_responses=[
                TreatmentResponseItem(**r) for r in treatment_responses
            ],
        )

    except Exception as e:
        logger.exception("get_timeline failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /source/{source_id}
# ---------------------------------------------------------------------------

@router.get("/source/{source_id}", response_model=SourceDetailResponse)
async def get_source_detail(source_id: str):
    """Fetch full details of any source for hover popup display."""
    source = get_source(source_id)

    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    return SourceDetailResponse(
        source_id=source["source_id"],
        source_type=source["source_type"],
        resource_type=source["resource_type"] or "",
        title=source["title"],
        date=source["date"],
        content_markdown=source["content_markdown"] or source["content"] or "",
    )


# ---------------------------------------------------------------------------
# POST /add_keypoints
# ---------------------------------------------------------------------------

@router.post("/add_keypoints", response_model=list[KeypointResult])
async def add_keypoints(request: AddKeypointsRequest):
    """Extract keypoints from sources and assess relevance to a clinical question."""
    try:
        clinical_question = request.clinical_question or request.fragestellung
        if not clinical_question:
            raise HTTPException(status_code=400, detail="clinical_question is required")

        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        if request.all:
            sources = conn.execute(
                "SELECT source_id, content, content_markdown FROM sources WHERE content IS NOT NULL"
            ).fetchall()
        elif request.source_ids:
            placeholders = ",".join(["?" for _ in request.source_ids])
            sources = conn.execute(
                f"SELECT source_id, content, content_markdown FROM sources WHERE source_id IN ({placeholders})",
                request.source_ids,
            ).fetchall()
        else:
            conn.close()
            return []

        results = []
        for source in sources:
            source_id = source["source_id"]
            content = source["content_markdown"] or source["content"]

            if not content:
                continue

            chunks = chunk_text(content)
            all_keypoints = []
            relevance_reason = None

            for chunk in chunks:
                result = await asyncio.to_thread(extract_keypoints_for_chunk, chunk, clinical_question)
                keypoints = result.get("keypoints", [])
                if keypoints and isinstance(keypoints[0], str):
                    keypoints = [{"text": kp, "priority": 3} for kp in keypoints]
                all_keypoints.extend(keypoints)


            # Store keypoints
            conn.execute(
                "INSERT INTO keypoints (source_id, fragestellung, keypoints) VALUES (?, ?, ?)",
                (source_id, clinical_question, json.dumps(all_keypoints)),
            )

            # Update timeline event key_insight with top LLM-generated keypoints
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

            results.append(KeypointResult(
                source_id=source_id,
                keypoints=[
                    KeypointItem(**kp) if isinstance(kp, dict) else KeypointItem(text=str(kp), priority=3)
                    for kp in all_keypoints
                ]            ))

        conn.commit()
        conn.close()

        return results

    except Exception as e:
        logger.exception("add_keypoints failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /regenerate_keypoints/{patient_id}
# ---------------------------------------------------------------------------

@router.post("/regenerate_keypoints/{patient_id}")
async def regenerate_keypoints(
    patient_id: str,
    clinical_question: Optional[str] = Query(None, description="Clinical question for relevance assessment"),
    background: bool = Query(True, description="Run in background (true) or wait for completion (false)"),
):
    """Delete and regenerate all keypoints for a patient's timeline sources.

    Clears existing keypoints for the patient, then re-extracts them from FHIR
    sources that have timeline events (excluding Lab/Chemotherapy/Medication).
    """
    _KP_SKIP = {'Lab', 'Chemotherapy', 'Medication'}
    clinical_question_text = BASE_CLINICAL_QUESTION
    if clinical_question:
        clinical_question_text += f"\n\nAdditional clinical question: {clinical_question}"

    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row

        # Find timeline source_ids (excluding types that use structured insights)
        tl_source_ids = [
            r["source_id"] for r in conn.execute(
                "SELECT DISTINCT source_id FROM timeline_events WHERE patient_id = ? AND event_type NOT IN ({})".format(
                    ",".join("?" for _ in _KP_SKIP)
                ),
                (patient_id, *_KP_SKIP),
            ).fetchall()
        ]

        if not tl_source_ids:
            conn.close()
            return {"status": "no_sources", "message": "No eligible timeline sources found", "count": 0}

        # Delete existing keypoints for these sources
        ph = ",".join("?" for _ in tl_source_ids)
        deleted = conn.execute(
            f"DELETE FROM keypoints WHERE source_id IN ({ph})", tl_source_ids
        ).rowcount
        # Clear LLM-generated key_insights from timeline events
        conn.execute(
            f"UPDATE timeline_events SET key_insight = NULL WHERE source_id IN ({ph})",
            tl_source_ids,
        )
        conn.commit()
        conn.close()

        logger.info(
            "Cleared %d keypoints for %d timeline sources (patient %s), regenerating...",
            deleted, len(tl_source_ids), patient_id,
        )

        if background:
            asyncio.create_task(generate_keypoints_batch(patient_id, tl_source_ids, clinical_question_text))
            return {
                "status": "started",
                "message": f"Regenerating keypoints for {len(tl_source_ids)} sources in background",
                "count": len(tl_source_ids),
                "deleted": deleted,
            }
        else:
            # Synchronous: wait for completion
            _start = time.monotonic()
            conn = get_db_connection()
            conn.row_factory = sqlite3.Row
            generated = 0
            for source_id in tl_source_ids:
                try:
                    result = await generate_keypoints_for_source(source_id, clinical_question_text, conn)
                    if result is not None:
                        generated += 1
                except Exception as e:
                    logger.error("Failed to regenerate keypoints for %s: %s", source_id, e)
            conn.close()
            _elapsed = time.monotonic() - _start
            return {
                "status": "completed",
                "message": f"Regenerated keypoints for {generated}/{len(tl_source_ids)} sources in {_elapsed:.1f}s",
                "count": len(tl_source_ids),
                "generated": generated,
                "deleted": deleted,
                "elapsed_seconds": round(_elapsed, 1),
            }

    except Exception as e:
        logger.exception("regenerate_keypoints failed")
        raise HTTPException(status_code=500, detail=str(e))
