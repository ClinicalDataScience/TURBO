"""Per-field patient summary generation with LLM correction loop."""
import json
import time
import asyncio
import logging
import sqlite3
from datetime import datetime
from typing import Optional

import httpx
from openai import APIConnectionError, APITimeoutError, InternalServerError

from config import (
    LLM_MODEL, BASE_CLINICAL_QUESTION,
    SUMMARY_CORRECTION_LOOP_ENABLED, SUMMARY_CORRECTION_LOOP_PASSES,
    SUMMARY_FIELD_MAX_RETRIES,
)
from database import get_db_connection
from models import (
    SummaryResponse, DemographicsField, FieldWithSources,
    StagingField, PathologyField,
    ComorbiditiesField, ListFieldWithSources, GeneralConditionField,
    PatientWishesField, CourseOfDiseaseField, MissingInfoItem,
)
from agent.model import llm_client
from services.llm_utils import chunk_text
from services.keypoints import extract_keypoints_for_chunk
from services.fhir_sync import fetch_and_register_fhir_resources
from services.timeline import (
    group_chemo_timeline_events, _find_imaging_for_cycles,
    _classify_response_with_llm, _STATUS_LABELS,
)
from services.summary_fields import (
    FIELD_RESOURCE_TYPES, FIELD_GENERATION_ORDER,
    FIELD_SCHEMAS, FIELD_INSTRUCTIONS, FIELD_VALIDATORS,
)

logger = logging.getLogger("medgemma-agent")

# ---------------------------------------------------------------------------
# In-memory tracker for background summary generation tasks
# ---------------------------------------------------------------------------
# Key: "{patient_id}::{clinical_question or ''}"
# Value: {"patient_id": str, "clinical_question": str|None, "started_at": str, "task": asyncio.Task}
_active_generations: dict[str, dict] = {}


def _generation_key(patient_id: str, clinical_question: str | None) -> str:
    return f"{patient_id}::{clinical_question or ''}"


async def generate_summary_with_llm(
    patient_id: str,
    fragestellung: str = None,
    correction_enabled: Optional[bool] = None,
    correction_passes: Optional[int] = None,
    status_queue: Optional[asyncio.Queue] = None,
) -> SummaryResponse:
    """Generate a comprehensive patient summary using LLM agent.

    Generates each summary field individually with focused FHIR context,
    retrying up to SUMMARY_FIELD_MAX_RETRIES times on malformed output.

    Args:
        status_queue: Optional asyncio.Queue for streaming per-field status events.
    """
    try:
        # Fetch FHIR resources with retry to handle FHIR server startup
        await fetch_and_register_fhir_resources(patient_id, retry=True)

        # Get all sources for this patient
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        sources = conn.execute(
            "SELECT * FROM sources WHERE patient_id = ?",
            (patient_id,)
        ).fetchall()
        conn.close()

        source_ids = [source["source_id"] for source in sources]
        known_ids = set(source_ids)

        # Index sources by resource type for per-field context building
        sources_by_type: dict[str, list[sqlite3.Row]] = {}
        for src in sources:
            sources_by_type.setdefault(src["resource_type"], []).append(src)

        # Raw clinical question (matches key used by start_generation / generation_progress)
        clinical_question = fragestellung

        # Build clinical question: base is always present, user's question is optional
        clinical_question_text = BASE_CLINICAL_QUESTION
        if fragestellung:
            clinical_question_text += f"\n\nAdditional clinical question: {fragestellung}"

        def _normalize_correction_passes(passes: int) -> int:
            if passes < 0:
                return 0
            return min(passes, 5)

        def _extract_json_content(raw_content: str) -> dict:
            content = raw_content.strip()
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            return json.loads(content)

        def _normalize_item_with_sources(items) -> list[dict]:
            """Normalize list items to ItemWithSources format ({"text": ..., "source_ids": [...]})."""
            if not isinstance(items, list):
                return []
            normalized = []
            for item in items:
                if isinstance(item, str):
                    normalized.append({"text": item, "source_ids": []})
                elif isinstance(item, dict) and "text" in item:
                    normalized.append(item)
                elif isinstance(item, dict):
                    text = item.get("item") or item.get("value") or item.get("name") or str(item)
                    normalized.append({"text": text, "source_ids": item.get("source_ids", [])})
            return normalized

        def _normalize_list_fields(summary_data: dict) -> None:
            """Normalize all list-based fields to use ItemWithSources format."""
            for field_name in ("contraindications", "symptoms"):
                field = summary_data.get(field_name)
                if isinstance(field, dict) and "items" in field:
                    field["items"] = _normalize_item_with_sources(field["items"])
            pathology = summary_data.get("pathology")
            if isinstance(pathology, dict):
                for key in ("key_findings", "mutations", "molecular_markers"):
                    if key in pathology:
                        pathology[key] = _normalize_item_with_sources(pathology[key])
            comorbidities = summary_data.get("comorbidities")
            if isinstance(comorbidities, dict):
                for key in ("conditions", "previous_surgeries", "previous_oncologic_diseases", "risk_factors"):
                    if key in comorbidities:
                        comorbidities[key] = _normalize_item_with_sources(comorbidities[key])

        def _normalize_field_data(field_name: str, data):
            """Normalize parsed LLM output for a summary field.

            List fields (chemo, radiation, imaging) need special handling because
            response_format=json_object forces the LLM to wrap arrays in a
            dict (e.g. {"imaging": [...]}). We unwrap the inner list.
            """
            if field_name in ("chemo", "radiation", "imaging"):
                # Already a list — use directly
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    # Dict wrapper from response_format=json_object → unwrap
                    # Try the field name key first, then any list value
                    if field_name in data and isinstance(data[field_name], list):
                        items = data[field_name]
                    else:
                        # Look for a list of dicts (actual items), NOT lists of
                        # strings like source_ids which would fail validation.
                        items = None
                        for v in data.values():
                            if isinstance(v, list) and v and isinstance(v[0], dict):
                                items = v
                                break
                        if items is None:
                            # The LLM returned a single item as a flat dict (no
                            # wrapper key).  Detect by checking for expected item
                            # keys and wrap it in a list.
                            _item_keys = {"type", "name", "description", "source_ids"}
                            if _item_keys & set(data.keys()):
                                logger.info(
                                    "Wrapping single %s dict item into a list",
                                    field_name,
                                )
                                items = [data]
                            else:
                                items = []
                elif isinstance(data, str):
                    try:
                        parsed = json.loads(data)
                        return _normalize_field_data(field_name, parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    return []
                else:
                    return []

                # Imaging: flatten key_findings from list-of-objects to
                # markdown bullet string.  The LLM sometimes returns
                # [{"text": "...", "source_ids": [...]}, ...] instead of a
                # plain string because the system prompt instructs it to use
                # {"text","source_ids"} objects for list items.
                if field_name == "imaging":
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        kf = item.get("key_findings")
                        if isinstance(kf, list):
                            bullets: list[str] = []
                            extra_sids: list[str] = []
                            for entry in kf:
                                if isinstance(entry, dict):
                                    text = entry.get("text", "")
                                    if text:
                                        bullets.append(f"- {text}")
                                    for sid in entry.get("source_ids", []):
                                        if sid not in extra_sids:
                                            extra_sids.append(sid)
                                elif isinstance(entry, str):
                                    bullets.append(f"- {entry}")
                            item["key_findings"] = "\n".join(bullets) if bullets else None
                            # Merge extracted source_ids into the item
                            if extra_sids:
                                existing = item.get("source_ids", [])
                                for sid in extra_sids:
                                    if sid not in existing:
                                        existing.append(sid)
                                item["source_ids"] = existing
                            logger.info(
                                "Normalized imaging key_findings from list (%d entries) to bullet string",
                                len(kf),
                            )

                return items
            # Dict fields — unwrap if LLM wrapped in a field-name key
            if isinstance(data, dict):
                if field_name in data and isinstance(data[field_name], dict):
                    return data[field_name]
                return data
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, dict):
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    pass
            return {}

        def _filter_item_source_ids(items: list, known_ids: set[str]) -> None:
            """Filter source_ids in ItemWithSources lists."""
            for item in items:
                if isinstance(item, dict) and "source_ids" in item:
                    item["source_ids"] = [sid for sid in item["source_ids"] if sid in known_ids]

        def _filter_summary_source_ids(summary_data: dict, known_ids: set[str]) -> None:
            for field in ["demographics", "tumor_board_question", "initial_diagnosis",
                          "staging", "pathology",
                          "comorbidities",
                          "contraindications", "general_condition", "symptoms",
                          "patient_wishes", "course_of_disease"]:
                if field in summary_data and isinstance(summary_data[field], dict):
                    field_ids = summary_data[field].get("source_ids", [])
                    summary_data[field]["source_ids"] = [
                        sid for sid in field_ids if sid in known_ids
                    ]
                    if field == "pathology":
                        seq_ids = summary_data[field].get("sequencing_source_ids", [])
                        summary_data[field]["sequencing_source_ids"] = [
                            sid for sid in seq_ids if sid in known_ids
                        ]
            for field_name in ("contraindications", "symptoms"):
                field = summary_data.get(field_name)
                if isinstance(field, dict):
                    _filter_item_source_ids(field.get("items", []), known_ids)
            pathology = summary_data.get("pathology")
            if isinstance(pathology, dict):
                for key in ("key_findings", "mutations", "molecular_markers"):
                    _filter_item_source_ids(pathology.get(key, []), known_ids)
            comorbidities = summary_data.get("comorbidities")
            if isinstance(comorbidities, dict):
                for key in ("conditions", "previous_surgeries", "previous_oncologic_diseases", "risk_factors"):
                    _filter_item_source_ids(comorbidities.get(key, []), known_ids)
            therapies_data = summary_data.get("therapies", {})
            if isinstance(therapies_data, dict):
                for _sub in ("chemo", "radiation"):
                    for therapy in therapies_data.get(_sub, []):
                        if isinstance(therapy, dict):
                            field_ids = therapy.get("source_ids", [])
                            therapy["source_ids"] = [sid for sid in field_ids if sid in known_ids]
            for imaging in summary_data.get("imaging", []):
                if isinstance(imaging, dict):
                    field_ids = imaging.get("source_ids", [])
                    imaging["source_ids"] = [sid for sid in field_ids if sid in known_ids]

        def _category_source_ids(category_name: str, value) -> list[str]:
            if isinstance(value, dict):
                ids = [sid for sid in value.get("source_ids", []) if isinstance(sid, str)]
                for key in ("items", "conditions", "previous_surgeries",
                            "previous_oncologic_diseases", "risk_factors",
                            "key_findings", "mutations", "molecular_markers"):
                    for item in value.get(key, []):
                        if isinstance(item, dict):
                            ids.extend([sid for sid in item.get("source_ids", []) if isinstance(sid, str)])
                ids.extend([sid for sid in value.get("sequencing_source_ids", []) if isinstance(sid, str)])
                return list(dict.fromkeys(ids))
            if category_name == "therapies" and isinstance(value, dict):
                merged_ids = []
                for _sub in ("chemo", "radiation"):
                    for item in value.get(_sub, []):
                        if isinstance(item, dict):
                            merged_ids.extend([sid for sid in item.get("source_ids", []) if isinstance(sid, str)])
                return list(dict.fromkeys(merged_ids))
            if category_name in {"chemo", "radiation", "imaging"} and isinstance(value, list):
                merged_ids = []
                for item in value:
                    if isinstance(item, dict):
                        merged_ids.extend([sid for sid in item.get("source_ids", []) if isinstance(sid, str)])
                return list(dict.fromkeys(merged_ids))
            return []

        def _build_fhir_context_for_ids(source_rows: dict[str, sqlite3.Row], ids: list[str], max_chars: int = 7000) -> str:
            parts = []
            total_chars = 0
            for sid in ids:
                row = source_rows.get(sid)
                if not row:
                    continue
                content = row["content_markdown"] or row["content"]
                if not content:
                    continue
                block = f"### [SOURCE: {sid}] {row['title']} ({row['resource_type']})\n{content}\n"
                if total_chars + len(block) > max_chars:
                    remaining = max_chars - total_chars
                    if remaining > 0:
                        parts.append(block[:remaining])
                    break
                parts.append(block)
                total_chars += len(block)
            return "\n\n".join(parts)

        # ---------------------------------------------------------------
        # Shared system prompts (cached by llama.cpp across calls)
        # ---------------------------------------------------------------

        _FIELD_GEN_SYSTEM_PROMPT = f"""You are a medical expert preparing a concise patient summary for a tumor board presentation.

Patient ID: {patient_id}
Clinical Question: {clinical_question_text}

Summary Style Rules:
- CLINICAL QUESTION FOCUS: Every piece of information you include must be relevant to answering or contextualizing the clinical question above. If a finding does not help the tumor board make a decision about the clinical question, omit it.
- Be CONCISE. Each field should contain only clinically relevant information.
- Use bullet points (markdown "- " syntax) in free-text fields. Maximum 10 bullet points per field.
- CLINICAL RELEVANCE: Recent changes and active findings are relevant. Old, static, unchanged findings are less relevant and can be omitted if space is needed. Relevance is defined by the clinical question.
- CHANGE DETECTION: Where the field instructions say to include a change summary, make the first bullet point a bold statement about whether there have been recent changes (e.g., "**No significant changes since last assessment**" or "**New: pulmonary nodule identified on 2024-03-15 CT**").
- Do NOT repeat information across fields. Each field covers its specific domain only.

JSON Rules:
- Each data source is tagged with a [SOURCE: <id>] identifier.
- Include a "source_ids" array containing ONLY the source IDs actually used.
- For list items, use {{"text": "...", "source_ids": ["<id>"]}} objects, NOT plain strings.
- Return ONLY valid JSON (no additional text)."""

        _CORRECTION_SYSTEM_PROMPT = """You are validating one category of a tumor board summary.
Compare the current category summary with the referenced FHIR data and revise for:
1) correctness (no unsupported claims),
2) completeness (include important facts present in the provided FHIR data).

Rules:
- Use ONLY the provided FHIR data.
- Keep the category JSON structure compatible with the current value.
- Do not invent values; use null/empty if unknown.
- Keep/adjust source_ids so they only contain allowed IDs that support the claim.
- Return JSON only in this exact envelope:
{"revised_category": <category_json>}"""

        # ---------------------------------------------------------------
        # Per-field generation helpers
        # ---------------------------------------------------------------

        def _build_field_context(field_name: str, max_chars: int = 12000) -> str:
            """Build FHIR context string containing only sources relevant to a field."""
            resource_types = FIELD_RESOURCE_TYPES.get(field_name, [])
            parts: list[str] = []
            total_chars = 0
            for rt in resource_types:
                for src in sources_by_type.get(rt, []):
                    content = src["content_markdown"] or src["content"]
                    if not content:
                        continue
                    block = f"### [SOURCE: {src['source_id']}] {src['title']} ({src['resource_type']})\n{content}"
                    if total_chars + len(block) > max_chars:
                        remaining = max_chars - total_chars
                        if remaining > 200:
                            parts.append(block[:remaining])
                        total_chars = max_chars
                        break
                    parts.append(block)
                    total_chars += len(block)
                if total_chars >= max_chars:
                    break
            return "\n\n---\n\n".join(parts)

        def _build_field_prompt(field_name: str, fhir_context: str, extra_context: str = None) -> str:
            """Build the user-message portion of a field generation prompt."""
            schema = FIELD_SCHEMAS[field_name]
            instructions = FIELD_INSTRUCTIONS.get(field_name, "")
            strict_note = (
                "CRITICAL: This must be a strict summary of explicit source content. Do not infer or speculate."
                if field_name != "course_of_disease"
                else "This field allows LLM inference from the provided data."
            )

            extra_section = ""
            if extra_context:
                extra_section = f"\n\nAlready-generated summary fields for reference:\n{extra_context}\n"

            return f"""Generate ONLY the "{field_name}" field.

Patient Data:
{fhir_context}
{extra_section}
{strict_note}

{instructions}

{f'Wrap the array in a JSON object: {{"{field_name}": {schema}}}' if schema.strip().startswith('[') else f'Use this exact structure: {schema}'}"""

        _LLM_STREAM_RETRIES = 6

        def _stream_llm_call(prompt_text: str, temperature: float = 0.3, system_prompt: str | None = None) -> str:
            """Call the LLM with streaming, retrying on connection/server errors."""
            logger.debug("LLM field generation prompt: %d chars (system: %d chars)",
                         len(prompt_text), len(system_prompt) if system_prompt else 0)
            messages: list[dict] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt_text})
            last_err: Exception | None = None
            for attempt in range(_LLM_STREAM_RETRIES):
                try:
                    stream = llm_client.chat.completions.create(
                        model=LLM_MODEL,
                        messages=messages,
                        temperature=temperature,
                        response_format={"type": "json_object"},
                        stream=True,
                    )
                    chunks: list[str] = []
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            chunks.append(chunk.choices[0].delta.content)
                    return "".join(chunks)
                except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError,
                        httpx.TimeoutException, APIConnectionError, APITimeoutError,
                        InternalServerError, ConnectionError, OSError) as e:
                    last_err = e
                    if attempt < _LLM_STREAM_RETRIES - 1:
                        wait = 2 ** attempt  # 1s, 2s
                        logger.warning("LLM stream error (attempt %d/%d): %s — retrying in %ds",
                                       attempt + 1, _LLM_STREAM_RETRIES, e, wait)
                        time.sleep(wait)
                    else:
                        logger.error("LLM stream failed after %d attempts: %s", _LLM_STREAM_RETRIES, e)
            raise last_err  # type: ignore[misc]

        def _validate_field(field_name: str, data: dict) -> None:
            """Validate parsed field data against its Pydantic model. Raises on failure."""
            validator = FIELD_VALIDATORS.get(field_name)
            if validator is None:
                return
            if isinstance(validator, list):
                # List field (therapies, imaging) — validate each item
                if not isinstance(data, list):
                    raise ValueError(f"Expected list for {field_name}, got {type(data).__name__}")
                item_model = validator[0]
                for i, item in enumerate(data):
                    if not isinstance(item, dict):
                        raise ValueError(
                            f"{field_name}[{i}]: expected dict, got {type(item).__name__} ({str(item)[:80]})"
                        )
                    item_model(**item)
            else:
                validator(**data)

        _total_fields = len(FIELD_GENERATION_ORDER)
        async def _emit_field_status(field_name: str, field_index: int, phase: str = "generating") -> None:
            """Emit an SSE status event with progress for the field being generated/corrected."""
            label = field_name.replace("_", " ").title()

            # Calculate current step based on phase
            steps_per_field = 1 + effective_correction_passes

            if phase == "generating":
                current_step = field_index * steps_per_field + 1
                message = f"Generating {label} Summary..."
            elif phase.startswith("correcting"):
                pass_num = 1
                if "pass " in phase:
                    try:
                        pass_num = int(phase.split("pass ")[1].split("/")[0])
                    except (IndexError, ValueError):
                        pass_num = 1
                current_step = field_index * steps_per_field + 1 + pass_num
                message = f"Verifying {label} ({phase.split('(')[1].rstrip(')')}..."
            else:
                current_step = field_index * steps_per_field + 1
                message = f"{phase.title()} {label}..."

            # Emit SSE event
            if status_queue is not None:
                await status_queue.put({
                    "type": "status",
                    "message": message,
                    "progress": {
                        "current": current_step,
                        "total": _total_steps,
                        "field": field_name,
                        "phase": phase,
                    },
                })

            # Persist progress to database for recovery on page reload
            try:
                def _persist_progress():
                    conn = get_db_connection()
                    conn.row_factory = sqlite3.Row

                    existing = conn.execute("""
                        SELECT id, started_at FROM generation_progress
                        WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                    """, (patient_id, clinical_question or '')).fetchone()

                    if existing:
                        conn.execute("""
                            UPDATE generation_progress
                            SET current_field = ?, current_step = ?, total_steps = ?,
                                status_message = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (field_name, current_step, _total_steps, message, existing['id']))
                    else:
                        conn.execute("""
                            INSERT INTO generation_progress
                                (patient_id, clinical_question, current_field, current_step, total_steps, status_message)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (patient_id, clinical_question, field_name, current_step, _total_steps, message))

                    conn.commit()
                    conn.close()
                    logger.debug(f"Persisted progress: {field_name} step {current_step}/{_total_steps}")

                await asyncio.to_thread(_persist_progress)
            except Exception as e:
                logger.warning(f"Failed to persist progress for {field_name}: {e}")

        def _default_for_field(field_name: str):
            """Return the empty default value for a field on total generation failure."""
            if field_name in ("chemo", "radiation", "imaging"):
                return []
            return {}

        effective_correction_enabled = (
            SUMMARY_CORRECTION_LOOP_ENABLED if correction_enabled is None else correction_enabled
        )
        effective_correction_passes = _normalize_correction_passes(
            SUMMARY_CORRECTION_LOOP_PASSES if correction_passes is None else correction_passes
        )
        # Zero out passes when correction is disabled so step-counting stays consistent
        if not effective_correction_enabled:
            effective_correction_passes = 0

        # Calculate total steps (field generation + correction + keypoint extraction).
        _generation_steps = _total_fields
        _correction_steps = _generation_steps * effective_correction_passes if effective_correction_enabled else 0
        _KP_SKIP_EVENT_TYPES = {'Lab', 'Chemotherapy', 'Medication'}
        _tl_conn = get_db_connection()
        _tl_conn.row_factory = sqlite3.Row
        _tl_source_ids = {
            r[0] for r in _tl_conn.execute(
                "SELECT DISTINCT source_id FROM timeline_events WHERE patient_id = ? AND event_type NOT IN ({})".format(
                    ",".join("?" for _ in _KP_SKIP_EVENT_TYPES)
                ),
                (patient_id, *_KP_SKIP_EVENT_TYPES)
            ).fetchall()
        }
        _candidate_sids = [s["source_id"] for s in sources if s["content"] and s["source_id"] in _tl_source_ids]
        _fresh_kp_sids: set[str] = set()
        if _candidate_sids:
            _ph = ",".join("?" for _ in _candidate_sids)
            for _r in _tl_conn.execute(
                f"SELECT k.source_id FROM keypoints k "
                f"JOIN sources s ON k.source_id = s.source_id "
                f"WHERE k.source_id IN ({_ph}) "
                f"AND k.content_hash IS NOT NULL AND k.content_hash = s.content_hash",
                _candidate_sids,
            ).fetchall():
                _fresh_kp_sids.add(_r["source_id"])
        # Pre-compute chemo cycles + imaging pairs for treatment response classification
        _chemo_tl_rows = _tl_conn.execute(
            "SELECT id, source_id, event_type, event_date, title, key_insight, priority "
            "FROM timeline_events WHERE patient_id = ? AND event_type IN ('Chemotherapy', 'Medication')",
            (patient_id,),
        ).fetchall()
        _non_chemo_tl_rows = _tl_conn.execute(
            "SELECT id, source_id, event_type, event_date, title, key_insight, priority "
            "FROM timeline_events WHERE patient_id = ? AND event_type NOT IN ('Chemotherapy', 'Medication', 'Lab')",
            (patient_id,),
        ).fetchall()
        # Convert id to str to match timeline endpoint (which does str(e["id"]))
        _chemo_raw = [{**dict(r), "id": str(r["id"])} for r in _chemo_tl_rows]
        _non_chemo_raw = [{**dict(r), "id": str(r["id"])} for r in _non_chemo_tl_rows]
        _grouped_for_resp = group_chemo_timeline_events(_chemo_raw + _non_chemo_raw)
        _chemo_cycles_for_resp = [e for e in _grouped_for_resp if e.get("event_type") == "Chemotherapy"]
        _non_chemo_for_resp = [e for e in _grouped_for_resp if e.get("event_type") != "Chemotherapy"]
        logger.info(
            "Treatment response pre-compute: %d chemo events, %d non-chemo events, %d chemo cycles, %d imaging events",
            len(_chemo_raw), len(_non_chemo_raw), len(_chemo_cycles_for_resp),
            len([e for e in _non_chemo_for_resp if (e.get("event_type") or "").lower() in ("imaging", "diagnosticreport")]),
        )
        _resp_pairs = _find_imaging_for_cycles(_chemo_cycles_for_resp, _non_chemo_for_resp)
        # Only count cycles that actually have a paired imaging event
        _resp_pairs_with_imaging = [(c, img) for c, img in _resp_pairs if img and img.get("source_id")]
        logger.info(
            "Treatment response pairs: %d total, %d with imaging",
            len(_resp_pairs), len(_resp_pairs_with_imaging),
        )

        # Check for already-fresh cached treatment responses
        _fresh_resp_cycle_ids: set[str] = set()
        if _resp_pairs_with_imaging:
            _resp_cycle_ids = [c["id"] for c, _ in _resp_pairs_with_imaging]
            _rph = ",".join("?" for _ in _resp_cycle_ids)
            for _rr in _tl_conn.execute(
                f"SELECT tr.cycle_event_id, tr.status FROM treatment_responses tr "
                f"WHERE tr.patient_id = ? AND tr.cycle_event_id IN ({_rph}) "
                f"AND tr.content_hash IS NOT NULL",
                [patient_id] + _resp_cycle_ids,
            ).fetchall():
                # Retry Unknown classifications even if content hasn't changed
                if _rr["status"] == "Unknown":
                    continue
                _fresh_resp_cycle_ids.add(_rr["cycle_event_id"])
        _treatment_resp_count = len(_resp_pairs_with_imaging) - len(_fresh_resp_cycle_ids)

        _tl_conn.close()
        _keypoint_source_count = len(_candidate_sids) - len(_fresh_kp_sids)
        if _fresh_kp_sids:
            logger.info("Skipping %d/%d sources with fresh keypoints", len(_fresh_kp_sids), len(_candidate_sids))
        if _fresh_resp_cycle_ids:
            logger.info("Skipping %d/%d chemo cycles with fresh treatment responses", len(_fresh_resp_cycle_ids), len(_resp_pairs_with_imaging))
        _total_steps = _generation_steps + _correction_steps + _keypoint_source_count + _treatment_resp_count

        # Prepare source_rows for per-field correction
        source_rows = {row["source_id"]: row for row in sources}

        async def _correct_single_field(
            category: str,
            summary_data: dict,
            source_rows: dict[str, sqlite3.Row],
            known_ids: set[str]
        ) -> None:
            """Run correction on a single field by comparing against its referenced FHIR sources."""
            if category not in summary_data:
                return

            current_value = summary_data.get(category)
            category_ids = [sid for sid in _category_source_ids(category, current_value) if sid in known_ids]
            if not category_ids:
                return

            fhir_context = _build_fhir_context_for_ids(source_rows, category_ids)
            if not fhir_context.strip():
                return

            correction_user_prompt = f"""Category: {category}
Allowed source IDs: {json.dumps(category_ids)}
Current category JSON:
{json.dumps(current_value, ensure_ascii=True)}

FHIR data:
{fhir_context}
"""

            logger.info("LLM correction prompt for '%s': %d chars (+ system %d chars)",
                        category, len(correction_user_prompt), len(_CORRECTION_SYSTEM_PROMPT))
            try:
                def _stream_correction(cp=correction_user_prompt, sp=_CORRECTION_SYSTEM_PROMPT):
                    last_err: Exception | None = None
                    for attempt in range(_LLM_STREAM_RETRIES):
                        try:
                            s = llm_client.chat.completions.create(
                                model=LLM_MODEL,
                                messages=[
                                    {"role": "system", "content": sp},
                                    {"role": "user", "content": cp},
                                ],
                                temperature=0.0,
                                response_format={"type": "json_object"},
                                stream=True,
                            )
                            parts: list[str] = []
                            for c in s:
                                if c.choices and c.choices[0].delta.content:
                                    parts.append(c.choices[0].delta.content)
                            return "".join(parts)
                        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError,
                                httpx.TimeoutException, ConnectionError, OSError) as e:
                            last_err = e
                            if attempt < _LLM_STREAM_RETRIES - 1:
                                time.sleep(2 ** attempt)
                                logger.warning("Correction stream retry %d/%d: %s",
                                               attempt + 1, _LLM_STREAM_RETRIES, e)
                            else:
                                logger.error("Correction stream failed after %d attempts: %s",
                                             _LLM_STREAM_RETRIES, e)
                    raise last_err  # type: ignore[misc]

                correction_raw = await asyncio.to_thread(_stream_correction)
                correction_data = _extract_json_content(correction_raw)
                revised_category = correction_data.get("revised_category")
                if revised_category is not None:
                    revised_category = _normalize_field_data(category, revised_category)
                    _validate_field(category, revised_category)

                    # Prevent correction from emptying list fields when original had valid entries
                    if category in ("imaging", "chemo", "radiation"):
                        original_count = len(current_value) if isinstance(current_value, list) else 0
                        revised_count = len(revised_category) if isinstance(revised_category, list) else 0
                        if original_count > 0 and revised_count == 0:
                            logger.warning(
                                "Correction attempted to empty '%s' field (had %d entries). Keeping original.",
                                category,
                                original_count
                            )
                            return  # Don't apply this correction

                    summary_data[category] = revised_category
                    _filter_summary_source_ids(summary_data, known_ids)
            except Exception:
                logger.exception("Category correction failed for '%s'", category)

        # ---------------------------------------------------------------
        # Per-field generation loop
        # ---------------------------------------------------------------
        max_retries = SUMMARY_FIELD_MAX_RETRIES
        summary_data: dict = {}

        for _field_idx, field_name in enumerate(FIELD_GENERATION_ORDER):
            await _emit_field_status(field_name, _field_idx)

            if field_name == "tumor_board_question":
                summary_data["tumor_board_question"] = {
                    "value": clinical_question_text,
                    "source_ids": [],
                }
                logger.info("Field 'tumor_board_question' set from clinical question")
                continue

            if field_name == "demographics":
                demo: dict = {"name": None, "age": None, "gender": None, "source_ids": []}
                for src in sources:
                    if src["resource_type"] == "Patient":
                        raw_content = src["content"]
                        if raw_content:
                            try:
                                patient_res = json.loads(raw_content)
                            except (json.JSONDecodeError, TypeError):
                                patient_res = {}
                            names = patient_res.get("name", [])
                            if names and isinstance(names, list):
                                n = names[0]
                                given = " ".join(n.get("given", []))
                                family = n.get("family", "")
                                full_name = f"{given} {family}".strip()
                                if full_name:
                                    demo["name"] = full_name
                            gender = patient_res.get("gender")
                            if gender:
                                demo["gender"] = gender.capitalize()
                            birth_date_str = patient_res.get("birthDate")
                            if birth_date_str:
                                try:
                                    from datetime import date
                                    bd = date.fromisoformat(birth_date_str)
                                    today = date.today()
                                    demo["age"] = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
                                except (ValueError, TypeError):
                                    pass
                            demo["source_ids"] = [src["source_id"]]
                        break
                summary_data["demographics"] = demo
                logger.info("Field 'demographics' built from FHIR Patient resource")
                continue

            _field_max_chars = 16000 if field_name == "imaging" else 12000
            fhir_context = _build_field_context(field_name, max_chars=_field_max_chars)

            if field_name == "imaging":
                context_size = len(fhir_context.strip())
                resource_count = fhir_context.count('"resourceType"')
                logger.info(
                    "Imaging field context: %d chars, %d FHIR resources",
                    context_size,
                    resource_count
                )

            if not fhir_context.strip() and field_name not in ("course_of_disease",):
                logger.info("No FHIR data for field '%s', using empty default", field_name)
                summary_data[field_name] = _default_for_field(field_name)
                continue

            extra_context = None
            if field_name == "course_of_disease":
                _cod_fields = ["staging", "pathology", "chemo", "radiation", "imaging", "initial_diagnosis"]
                _cod_parts = []
                for cf in _cod_fields:
                    if cf in summary_data and summary_data[cf]:
                        _cod_parts.append(f"{cf}: {json.dumps(summary_data[cf], ensure_ascii=False)}")
                if _cod_parts:
                    extra_context = "\n".join(_cod_parts)

            field_prompt = _build_field_prompt(field_name, fhir_context, extra_context)
            logger.info(
                "Field '%s': FHIR context %d chars, full prompt %d chars",
                field_name, len(fhir_context), len(field_prompt),
            )

            generated = False
            for attempt in range(1 + max_retries):
                try:
                    raw = await asyncio.to_thread(_stream_llm_call, field_prompt, 0.3, _FIELD_GEN_SYSTEM_PROMPT)

                    if field_name == "imaging":
                        logger.info("Imaging raw LLM response (first 500 chars): %s", raw[:500])

                    parsed = _extract_json_content(raw)

                    if field_name == "imaging":
                        logger.info("Imaging parsed data: %s", json.dumps(parsed, ensure_ascii=False)[:500])

                    parsed = _normalize_field_data(field_name, parsed)
                    _validate_field(field_name, parsed)

                    if field_name == "imaging":
                        entry_count = len(parsed) if isinstance(parsed, list) else 0
                        logger.info("Imaging validation passed: %d entries", entry_count)

                    summary_data[field_name] = parsed
                    generated = True
                    if attempt > 0:
                        logger.info("Field '%s' succeeded on retry %d", field_name, attempt)
                    break
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(
                            "Field '%s' attempt %d/%d failed (%s), retrying...",
                            field_name, attempt + 1, max_retries + 1, e,
                        )
                    else:
                        logger.warning(
                            "Field '%s' failed after %d attempts (%s), using default",
                            field_name, max_retries + 1, e,
                        )

            if not generated:
                summary_data[field_name] = _default_for_field(field_name)

            if field_name == "imaging":
                if isinstance(summary_data.get("imaging"), list) and len(summary_data["imaging"]) == 0:
                    if fhir_context.strip() and ('"DiagnosticReport"' in fhir_context or '"ImagingStudy"' in fhir_context):
                        logger.warning(
                            "Imaging field is empty despite FHIR context containing DiagnosticReport/ImagingStudy data. "
                            "Context size: %d chars. This may indicate LLM generation or validation issues.",
                            len(fhir_context)
                        )

            # CORRECTION PHASE (immediately after each field generation, if enabled)
            if effective_correction_enabled and effective_correction_passes > 0 and field_name in summary_data:
                for iteration in range(effective_correction_passes):
                    await _emit_field_status(field_name, _field_idx, phase=f"correcting (pass {iteration+1}/{effective_correction_passes})")
                    await _correct_single_field(field_name, summary_data, source_rows, known_ids)

            # After pathology is finalized, classify cancer type and persist guideline selection
            if field_name == "pathology":
                pathology_result = summary_data.get("pathology")
                if isinstance(pathology_result, dict):
                    cancer_type_raw = (pathology_result.get("cancer_type") or "").lower()
                    if any(kw in cancer_type_raw for kw in ["small cell", "sclc", "kleinzellig"]):
                        guideline_types = ["sclc"]
                    else:
                        guideline_types = ["nsclc"]
                    from database import upsert_patient_metadata
                    upsert_patient_metadata(patient_id, cancer_type_raw, guideline_types)
                    logger.info(
                        "Patient %s classified as %s guidelines (cancer_type_raw=%r)",
                        patient_id, guideline_types, cancer_type_raw,
                    )

        # Assemble chemo + radiation into nested therapies field
        summary_data["therapies"] = {
            "chemo": summary_data.pop("chemo", []),
            "radiation": summary_data.pop("radiation", []),
        }

        # Normalize list items (plain strings → ItemWithSources format)
        _normalize_list_fields(summary_data)

        # Validate source_ids per field
        _filter_summary_source_ids(summary_data, known_ids)

        # Backfill source_ids for sections the LLM leaves empty
        _MAX_BACKFILL_SOURCES = 5

        _resource_type_source_ids: dict[str, list[str]] = {}
        for src in sources:
            rt = src["resource_type"]
            _resource_type_source_ids.setdefault(rt, []).append(src["source_id"])

        _section_backfill_map = [
            ("initial_diagnosis", ["Condition"]),
            ("pathology", ["DiagnosticReport"]),
            ("course_of_disease", ["Condition", "DiagnosticReport", "Observation"]),
        ]
        for _bf_name, _bf_resource_types in _section_backfill_map:
            section = summary_data.get(_bf_name)
            if not isinstance(section, dict):
                continue
            existing = set(section.get("source_ids") or [])
            if existing:
                continue
            for rt in _bf_resource_types:
                for sid in _resource_type_source_ids.get(rt, []):
                    if sid in known_ids:
                        existing.add(sid)
                    if len(existing) >= _MAX_BACKFILL_SOURCES:
                        break
                if len(existing) >= _MAX_BACKFILL_SOURCES:
                    break
            if existing:
                section["source_ids"] = list(existing)

        # Backfill source_ids for therapy list items the LLM left empty
        _chemo_resource_types = ["MedicationStatement", "MedicationRequest"]
        _chemo_fallback_ids: list[str] = []
        for rt in _chemo_resource_types:
            for sid in _resource_type_source_ids.get(rt, []):
                if sid in known_ids:
                    _chemo_fallback_ids.append(sid)
                if len(_chemo_fallback_ids) >= _MAX_BACKFILL_SOURCES:
                    break
            if len(_chemo_fallback_ids) >= _MAX_BACKFILL_SOURCES:
                break

        _radiation_fallback_ids: list[str] = []
        for sid in _resource_type_source_ids.get("Procedure", []):
            if sid in known_ids:
                _radiation_fallback_ids.append(sid)
            if len(_radiation_fallback_ids) >= _MAX_BACKFILL_SOURCES:
                break

        _therapies_data = summary_data.get("therapies", {})
        if isinstance(_therapies_data, dict):
            for item in _therapies_data.get("chemo", []):
                if isinstance(item, dict) and not item.get("source_ids"):
                    item["source_ids"] = _chemo_fallback_ids[:]
            for item in _therapies_data.get("radiation", []):
                if isinstance(item, dict) and not item.get("source_ids"):
                    item["source_ids"] = _radiation_fallback_ids[:]

        # Backfill section-level source_ids from item-level source_ids
        _backfill_fields = [
            ("contraindications", ["items"]),
            ("symptoms", ["items"]),
            ("comorbidities", ["conditions", "previous_surgeries",
                               "previous_oncologic_diseases", "risk_factors"]),
            ("pathology", ["key_findings", "mutations", "molecular_markers"]),
        ]
        for _bf_field, _bf_keys in _backfill_fields:
            section = summary_data.get(_bf_field)
            if not isinstance(section, dict):
                continue
            existing = set(section.get("source_ids") or [])
            for _bf_key in _bf_keys:
                for item in section.get(_bf_key, []):
                    if isinstance(item, dict):
                        for sid in item.get("source_ids", []):
                            existing.add(sid)
            for sid in section.get("sequencing_source_ids", []):
                existing.add(sid)
            section["source_ids"] = list(existing)

        # Auto-detect empty fields and populate missing_info
        if not summary_data.get("missing_info"):
            _auto_missing: list[dict] = []
            _field_checks: list[tuple[str, str, str]] = [
                ("initial_diagnosis", "value", "What is the primary diagnosis?"),
                ("staging", "tnm", "What is the current tumor staging (TNM/UICC)?"),
                ("course_of_disease", "assessment", "What is the current disease course and progression status?"),
                ("patient_wishes", "text", "What are the patient's wishes regarding treatment?"),
                ("general_condition", "description", "What is the patient's general condition / ECOG status?"),
            ]
            for _fname, _key, _question in _field_checks:
                field_data = summary_data.get(_fname)
                if isinstance(field_data, dict) and not field_data.get(_key):
                    _auto_missing.append({"field": _fname, "question": _question, "priority": "high"})

            for _fname, _question in [
                ("symptoms", "What symptoms is the patient currently experiencing?"),
            ]:
                field_data = summary_data.get(_fname)
                if isinstance(field_data, dict) and not field_data.get("items"):
                    _auto_missing.append({"field": _fname, "question": _question, "priority": "medium"})

            if _auto_missing:
                summary_data["missing_info"] = _auto_missing

        # Safety net: strip non-dict items from list fields
        for _list_field in ("imaging",):
            items = summary_data.get(_list_field)
            if isinstance(items, list):
                summary_data[_list_field] = [i for i in items if isinstance(i, dict)]
        _therapies_safety = summary_data.get("therapies", {})
        if isinstance(_therapies_safety, dict):
            for _sub in ("chemo", "radiation"):
                items = _therapies_safety.get(_sub)
                if isinstance(items, list):
                    _therapies_safety[_sub] = [i for i in items if isinstance(i, dict)]

        # ---------------------------------------------------------------
        # Generate LLM keypoints for timeline key-insights (inline, with progress)
        # ---------------------------------------------------------------
        if _keypoint_source_count > 0:
            _kp_base_step = _generation_steps + _correction_steps

            try:
                conn = get_db_connection()
                conn.row_factory = sqlite3.Row

                _te_rows = conn.execute(
                    "SELECT source_id, title FROM timeline_events WHERE patient_id = ? AND event_type NOT IN ({})".format(
                        ",".join("?" for _ in _KP_SKIP_EVENT_TYPES)
                    ),
                    (patient_id, *_KP_SKIP_EVENT_TYPES)
                ).fetchall()
                _source_titles = {r["source_id"]: r["title"] for r in _te_rows}

                all_sources = [s for s in sources if s["content"] and s["source_id"] in _source_titles]
                logger.info(
                    "Keypoint generation: %d sources to process (%d fresh skipped)",
                    _keypoint_source_count, len(_fresh_kp_sids),
                )

                kp_count = 0
                for src in all_sources:
                    sid = src["source_id"]
                    if sid in _fresh_kp_sids:
                        continue

                    content = src["content_markdown"] or src["content"]
                    if not content:
                        continue

                    kp_step = _kp_base_step + kp_count + 1
                    _title = _source_titles.get(sid, "")
                    _kp_label = f"Timeline Insights ({kp_count + 1}/{_keypoint_source_count})"
                    if _title:
                        _kp_label += f" — {_title}"
                    if status_queue is not None:
                        await status_queue.put({
                            "type": "status",
                            "message": f"Generating {_kp_label}...",
                            "progress": {
                                "current": kp_step,
                                "total": _total_steps,
                                "field": "timeline_insights",
                                "phase": "generating",
                            },
                        })
                    try:
                        def _persist_kp_progress(step=kp_step, msg=f"Generating {_kp_label}..."):
                            pconn = get_db_connection()
                            pconn.row_factory = sqlite3.Row
                            existing = pconn.execute("""
                                SELECT id FROM generation_progress
                                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                            """, (patient_id, clinical_question or '')).fetchone()
                            if existing:
                                pconn.execute("""
                                    UPDATE generation_progress
                                    SET current_field = 'timeline_insights', current_step = ?, total_steps = ?,
                                        status_message = ?, updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                """, (step, _total_steps, msg, existing['id']))
                            pconn.commit()
                            pconn.close()
                        await asyncio.to_thread(_persist_kp_progress)
                    except Exception as e:
                        logger.warning(f"Failed to persist keypoints progress: {e}")

                    chunks = chunk_text(content)
                    all_keypoints = []

                    _src_start = time.monotonic()
                    for chunk in chunks:
                        result = await asyncio.to_thread(
                            extract_keypoints_for_chunk, chunk, clinical_question_text
                        )
                        keypoints = result.get("keypoints", [])
                        if keypoints and isinstance(keypoints[0], str):
                            keypoints = [{"text": kp, "priority": 3} for kp in keypoints]
                        all_keypoints.extend(keypoints)
                    _src_elapsed = time.monotonic() - _src_start
                    logger.info(
                        "Keypoint extraction for source %s (%s) took %.1fs (%d chunks, %d chars, %d keypoints)",
                        sid, _title[:40], _src_elapsed, len(chunks), len(content), len(all_keypoints),
                    )

                    _src_content_hash = src["content_hash"]
                    conn.execute("DELETE FROM keypoints WHERE source_id = ?", (sid,))
                    conn.execute(
                        "INSERT INTO keypoints (source_id, fragestellung, keypoints, content_hash) VALUES (?, ?, ?, ?)",
                        (sid, clinical_question_text, json.dumps(all_keypoints), _src_content_hash)
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
                            (llm_insight, sid),
                        )
                    conn.commit()
                    kp_count += 1

                conn.close()
                logger.info(f"Generated keypoints for {kp_count} sources during summary generation")
            except Exception as e:
                logger.exception(f"Keypoint generation failed for patient {patient_id}: {e}")

        # ---------------------------------------------------------------
        # Classify treatment responses for chemo cycles (inline, with progress)
        # ---------------------------------------------------------------
        if _treatment_resp_count > 0:
            _resp_base_step = _generation_steps + _correction_steps + _keypoint_source_count

            try:
                resp_conn = get_db_connection()
                resp_conn.row_factory = sqlite3.Row
                resp_count = 0

                for cycle, img_event in _resp_pairs_with_imaging:
                    cid = cycle["id"]
                    if cid in _fresh_resp_cycle_ids:
                        continue

                    # Fetch imaging content
                    src_row = resp_conn.execute(
                        "SELECT content, content_markdown, content_hash FROM sources WHERE source_id = ?",
                        (img_event["source_id"],),
                    ).fetchone()
                    if not src_row:
                        continue
                    imaging_text = src_row["content_markdown"] or src_row["content"] or ""
                    if not imaging_text.strip():
                        continue
                    content_hash = src_row["content_hash"]

                    resp_step = _resp_base_step + resp_count + 1
                    _cycle_label = cycle.get("title", f"Cycle {resp_count + 1}")
                    _resp_label = f"Treatment Response ({resp_count + 1}/{_treatment_resp_count}) — {_cycle_label}"
                    if status_queue is not None:
                        await status_queue.put({
                            "type": "status",
                            "message": f"Classifying {_resp_label}...",
                            "progress": {
                                "current": resp_step,
                                "total": _total_steps,
                                "field": "treatment_response",
                                "phase": "generating",
                            },
                        })
                    try:
                        def _persist_resp_progress(step=resp_step, msg=f"Classifying {_resp_label}..."):
                            pconn = get_db_connection()
                            pconn.row_factory = sqlite3.Row
                            existing = pconn.execute("""
                                SELECT id FROM generation_progress
                                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                            """, (patient_id, clinical_question or '')).fetchone()
                            if existing:
                                pconn.execute("""
                                    UPDATE generation_progress
                                    SET current_field = 'treatment_response', current_step = ?, total_steps = ?,
                                        status_message = ?, updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                """, (step, _total_steps, msg, existing['id']))
                            pconn.commit()
                            pconn.close()
                        await asyncio.to_thread(_persist_resp_progress)
                    except Exception as e:
                        logger.warning(f"Failed to persist treatment response progress: {e}")

                    _resp_start = time.monotonic()
                    result = await asyncio.to_thread(_classify_response_with_llm, imaging_text)
                    _resp_elapsed = time.monotonic() - _resp_start

                    status = result["status"]
                    status_label = _STATUS_LABELS.get(status, "Unknown")

                    resp_conn.execute(
                        """INSERT OR REPLACE INTO treatment_responses
                        (patient_id, cycle_event_id, cycle_date, status, status_label,
                         confidence, basis, imaging_source_ids, imaging_date, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            patient_id,
                            cid,
                            cycle.get("event_date"),
                            status,
                            status_label,
                            result.get("confidence", "medium"),
                            result.get("basis"),
                            json.dumps([img_event["source_id"]]),
                            img_event.get("event_date"),
                            content_hash,
                        ),
                    )
                    resp_conn.commit()
                    logger.info(
                        "Treatment response for %s: %s (%s) in %.1fs",
                        _cycle_label, status, result.get("confidence"), _resp_elapsed,
                    )
                    resp_count += 1

                resp_conn.close()
                logger.info(f"Classified treatment responses for {resp_count} chemo cycles during summary generation")
            except Exception as e:
                logger.exception(f"Treatment response classification failed for patient {patient_id}: {e}")

        # Mark progress as complete in database
        try:
            conn = get_db_connection()
            conn.execute("""
                UPDATE generation_progress
                SET completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
            """, (patient_id, clinical_question or ''))
            conn.commit()
            conn.close()
            logger.info(f"Marked summary generation as complete for patient {patient_id}")
        except Exception as e:
            logger.warning(f"Failed to mark progress as complete: {e}")

        return SummaryResponse(
            patient_id=patient_id,
            generated_at=datetime.utcnow().isoformat(),
            **summary_data
        )

    except Exception as e:
        logger.exception("Failed to generate summary with LLM")
        # Persist terminal failure so polling can surface the error immediately
        # instead of waiting until the row turns "stale".
        try:
            conn = get_db_connection()
            conn.execute(
                """
                UPDATE generation_progress
                SET status_message = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE patient_id = ? AND COALESCE(clinical_question, '') = COALESCE(?, '')
                """,
                (
                    f"Generation failed: {type(e).__name__}",
                    str(e)[:2000],
                    patient_id,
                    fragestellung or "",
                ),
            )
            conn.commit()
            conn.close()
        except Exception as progress_err:
            logger.warning("Failed to persist generation failure state: %s", progress_err)

        return SummaryResponse(
            patient_id=patient_id,
            generated_at=datetime.utcnow().isoformat(),
            demographics=DemographicsField(),
            tumor_board_question=FieldWithSources(),
            initial_diagnosis=FieldWithSources(),
            staging=StagingField(),
            pathology=PathologyField(),
            comorbidities=ComorbiditiesField(),
            contraindications=ListFieldWithSources(),
            general_condition=GeneralConditionField(),
            symptoms=ListFieldWithSources(),
            patient_wishes=PatientWishesField(needs_clarification=True),
            course_of_disease=CourseOfDiseaseField(),
            missing_info=[MissingInfoItem(
                field="all",
                question="Unable to generate summary - please check data sources",
                priority="high"
            )]
        )


# ---------------------------------------------------------------------------
# Single-field regeneration with clinician feedback
# ---------------------------------------------------------------------------

_REGEN_EXCLUDED_FIELDS = {"demographics", "tumor_board_question"}

async def regenerate_single_field(
    patient_id: str,
    field_name: str,
    feedback: str,
    clinical_question: str | None = None,
    summary_data: dict | None = None,
) -> dict | list:
    """Regenerate a single summary field using FHIR data + clinician feedback.

    Returns the regenerated field data (dict or list depending on field type).
    Raises ValueError for invalid field names.
    """
    # Handle composite "therapies" field: regenerate both chemo and radiation
    if field_name == "therapies":
        chemo_result = await regenerate_single_field(
            patient_id=patient_id,
            field_name="chemo",
            feedback=feedback,
            clinical_question=clinical_question,
            summary_data=summary_data,
        )
        radiation_result = await regenerate_single_field(
            patient_id=patient_id,
            field_name="radiation",
            feedback=feedback,
            clinical_question=clinical_question,
            summary_data=summary_data,
        )
        return {"chemo": chemo_result, "radiation": radiation_result}

    if field_name not in FIELD_GENERATION_ORDER:
        raise ValueError(f"Unknown field: {field_name}")
    if field_name in _REGEN_EXCLUDED_FIELDS:
        raise ValueError(f"Field '{field_name}' cannot be regenerated")

    # -- Fetch FHIR sources from SQLite --
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    sources = conn.execute(
        "SELECT * FROM sources WHERE patient_id = ?", (patient_id,)
    ).fetchall()
    conn.close()

    known_ids = {src["source_id"] for src in sources}

    sources_by_type: dict[str, list[sqlite3.Row]] = {}
    for src in sources:
        sources_by_type.setdefault(src["resource_type"], []).append(src)

    clinical_question_text = BASE_CLINICAL_QUESTION
    if clinical_question:
        clinical_question_text += f"\n\nAdditional clinical question: {clinical_question}"

    # -- Local helpers (same logic as closures in generate_summary_with_llm) --

    def _extract_json(raw: str) -> dict:
        content = raw.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        return json.loads(content)

    def _normalize(field: str, data):
        if field in ("chemo", "radiation", "imaging"):
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                if field in data and isinstance(data[field], list):
                    return data[field]
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v
                _item_keys = {"type", "name", "description", "source_ids"}
                if _item_keys & set(data.keys()):
                    return [data]
                return []
            return []
        if isinstance(data, dict):
            if field in data and isinstance(data[field], dict):
                return data[field]
            return data
        return {}

    def _validate(field: str, data):
        """Validate and normalize via Pydantic model, returning model_dump() output.

        This ensures the returned dict has the correct field names and types,
        even if the LLM used slightly different key names. Pydantic's model
        validators (e.g. _migrate_legacy_items) also run, normalizing plain
        strings into ItemWithSources format.
        """
        validator = FIELD_VALIDATORS.get(field)
        if validator is None:
            return data
        if isinstance(validator, list):
            if not isinstance(data, list):
                raise ValueError(f"Expected list for {field}")
            item_model = validator[0]
            result = []
            for i, item in enumerate(data):
                if not isinstance(item, dict):
                    raise ValueError(f"{field}[{i}]: expected dict")
                result.append(item_model(**item).model_dump())
            return result
        else:
            return validator(**data).model_dump()

    def _build_context(field: str, max_chars: int = 12000) -> str:
        resource_types = FIELD_RESOURCE_TYPES.get(field, [])
        parts: list[str] = []
        total = 0
        for rt in resource_types:
            for src in sources_by_type.get(rt, []):
                content = src["content_markdown"] or src["content"]
                if not content:
                    continue
                block = f"### [SOURCE: {src['source_id']}] {src['title']} ({src['resource_type']})\n{content}"
                if total + len(block) > max_chars:
                    remaining = max_chars - total
                    if remaining > 200:
                        parts.append(block[:remaining])
                    return "\n\n---\n\n".join(parts)
                parts.append(block)
                total += len(block)
        return "\n\n---\n\n".join(parts)

    def _normalize_items(items) -> list[dict]:
        """Normalize list items to ItemWithSources format."""
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if isinstance(item, str):
                result.append({"text": item, "source_ids": []})
            elif isinstance(item, dict) and "text" in item:
                result.append(item)
            elif isinstance(item, dict):
                text = item.get("item") or item.get("value") or item.get("name") or str(item)
                result.append({"text": text, "source_ids": item.get("source_ids", [])})
        return result

    def _post_process(field: str, data, valid_ids: set[str]):
        """Normalize ItemWithSources lists and filter source_ids for a single field."""
        # Normalize list items to ItemWithSources format
        if field in ("contraindications", "symptoms"):
            if isinstance(data, dict) and "items" in data:
                data["items"] = _normalize_items(data["items"])
        elif field == "pathology":
            if isinstance(data, dict):
                for key in ("key_findings", "mutations", "molecular_markers"):
                    if key in data:
                        data[key] = _normalize_items(data[key])
        elif field == "comorbidities":
            if isinstance(data, dict):
                for key in ("conditions", "previous_surgeries", "previous_oncologic_diseases", "risk_factors"):
                    if key in data:
                        data[key] = _normalize_items(data[key])

        # Filter source_ids against known valid IDs
        def _filt(ids):
            return [sid for sid in ids if sid in valid_ids]

        if isinstance(data, dict):
            if "source_ids" in data:
                data["source_ids"] = _filt(data["source_ids"])
            if field == "pathology" and "sequencing_source_ids" in data:
                data["sequencing_source_ids"] = _filt(data["sequencing_source_ids"])
            for key in ("items", "conditions", "previous_surgeries", "previous_oncologic_diseases",
                        "risk_factors", "key_findings", "mutations", "molecular_markers"):
                if key in data and isinstance(data[key], list):
                    for item in data[key]:
                        if isinstance(item, dict) and "source_ids" in item:
                            item["source_ids"] = _filt(item["source_ids"])
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "source_ids" in item:
                    item["source_ids"] = _filt(item["source_ids"])

    _LLM_RETRIES = 3

    def _llm_call(prompt: str, temperature: float = 0.3, system: str | None = None) -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        last_err: Exception | None = None
        for attempt in range(_LLM_RETRIES):
            try:
                stream = llm_client.chat.completions.create(
                    model=LLM_MODEL,
                    messages=messages,
                    temperature=temperature,
                    response_format={"type": "json_object"},
                    stream=True,
                )
                chunks: list[str] = []
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        chunks.append(chunk.choices[0].delta.content)
                return "".join(chunks)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError,
                    httpx.TimeoutException, APIConnectionError, APITimeoutError,
                    InternalServerError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < _LLM_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    logger.warning("Regen LLM retry %d/%d: %s", attempt + 1, _LLM_RETRIES, e)
        raise last_err  # type: ignore[misc]

    # -- Build the generation prompt with feedback --

    system_prompt = f"""You are a medical expert preparing a concise patient summary for a tumor board presentation.

Patient ID: {patient_id}
Clinical Question: {clinical_question_text}

Summary Style Rules:
- CLINICAL QUESTION FOCUS: Every piece of information you include must be relevant to answering or contextualizing the clinical question above. If a finding does not help the tumor board make a decision about the clinical question, omit it.
- Be CONCISE. Each field should contain only clinically relevant information.
- Use bullet points (markdown "- " syntax) in free-text fields. Maximum 10 bullet points per field.
- CLINICAL RELEVANCE: Recent changes and active findings are relevant. Old, static, unchanged findings are less relevant and can be omitted if space is needed. Relevance is defined by the clinical question.
- CHANGE DETECTION: Where the field instructions say to include a change summary, make the first bullet point a bold statement about whether there have been recent changes (e.g., "**No significant changes since last assessment**" or "**New: pulmonary nodule identified on 2024-03-15 CT**").
- Do NOT repeat information across fields. Each field covers its specific domain only.

JSON Rules:
- Each data source is tagged with a [SOURCE: <id>] identifier.
- Include a "source_ids" array containing ONLY the source IDs actually used.
- For list items, use {{"text": "...", "source_ids": ["<id>"]}} objects, NOT plain strings.
- Return ONLY valid JSON (no additional text)."""

    max_chars = 16000 if field_name == "imaging" else 12000
    fhir_context = _build_context(field_name, max_chars=max_chars)

    if not fhir_context.strip() and field_name != "course_of_disease":
        logger.info("No FHIR data for field '%s' during regeneration; relying on clinician feedback", field_name)

    # Extra context for course_of_disease
    extra_context = None
    if field_name == "course_of_disease" and summary_data:
        cod_fields = ["staging", "pathology", "therapies", "imaging", "initial_diagnosis"]
        cod_parts = []
        for cf in cod_fields:
            if cf in summary_data and summary_data[cf]:
                cod_parts.append(f"{cf}: {json.dumps(summary_data[cf], ensure_ascii=False)}")
        if cod_parts:
            extra_context = "\n".join(cod_parts)

    schema = FIELD_SCHEMAS[field_name]
    instructions = FIELD_INSTRUCTIONS.get(field_name, "")
    strict_note = (
        "CRITICAL: This must be a strict summary of explicit source content. Do not infer or speculate."
        if field_name != "course_of_disease"
        else "This field allows LLM inference from the provided data."
    )

    extra_section = ""
    if extra_context:
        extra_section = f"\n\nAlready-generated summary fields for reference:\n{extra_context}\n"

    feedback_section = f"""

CLINICIAN FEEDBACK ON PREVIOUS GENERATION:
The clinician provided the following correction/feedback about this section:
"{feedback}"

Take this feedback into account when regenerating this field. If the feedback
contradicts the source data, prefer the clinician's feedback as they have
direct patient knowledge."""

    field_prompt = f"""Generate ONLY the "{field_name}" field.

Patient Data:
{fhir_context}
{extra_section}
{strict_note}

{instructions}
{feedback_section}

{f'Wrap the array in a JSON object: {{"{field_name}": {schema}}}' if schema.strip().startswith('[') else f'Use this exact structure: {schema}'}"""

    logger.info(
        "Regenerating field '%s' for patient %s (feedback: %d chars, context: %d chars)",
        field_name, patient_id, len(feedback), len(fhir_context),
    )

    # -- Call LLM with retry --
    max_retries = SUMMARY_FIELD_MAX_RETRIES
    for attempt in range(1 + max_retries):
        try:
            raw = await asyncio.to_thread(_llm_call, field_prompt, 0.3, system_prompt)
            parsed = _extract_json(raw)
            parsed = _normalize(field_name, parsed)
            parsed = _validate(field_name, parsed)
            _post_process(field_name, parsed, known_ids)
            logger.info("Field '%s' regenerated successfully (attempt %d)", field_name, attempt + 1)
            return parsed
        except Exception as e:
            if attempt < max_retries:
                logger.warning("Regen field '%s' attempt %d failed: %s", field_name, attempt + 1, e)
            else:
                logger.error("Regen field '%s' failed after %d attempts: %s", field_name, max_retries + 1, e)
                raise
