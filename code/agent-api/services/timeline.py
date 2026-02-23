"""Timeline event creation, chemo grouping, response classification, and refresh logic."""
import json
import re
import time
import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime
from typing import Optional

from config import LLM_MODEL
from database import get_db_connection
from fhir.converter import (
    get_resource_title, get_resource_date,
    resolve_medication_for_statement,
    _extract_first_coding_display,
    _extract_medication_details,
    _extract_medication_statement_dose_route,
)

logger = logging.getLogger("medgemma-agent")

# Known chemotherapy drug name patterns for timeline grouping
_CHEMO_DRUG_PATTERNS = [
    "carboplatin", "cisplatin", "oxaliplatin", "paclitaxel", "docetaxel",
    "gemcitabine", "pemetrexed", "etoposide", "etoposid", "cyclophosphamide", "ifosfamide",
    "doxorubicin", "epirubicin", "fluorouracil", "5-fu", "capecitabine",
    "vincristine", "vinblastine", "irinotecan", "topotecan", "bleomycin",
    "methotrexate", "melphalan", "dacarbazine", "temozolomide",
    "nab-paclitaxel", "folfox", "folfiri", "folfirinox",
    "pembrolizumab", "nivolumab", "atezolizumab", "durvalumab",
    "bevacizumab", "trastuzumab", "cetuximab", "rituximab",
]


def _is_chemo_medication(resource: dict, fhir_base_url: str = None,
                         http_client=None, medication_cache_fn=None) -> bool:
    """Check if a MedicationStatement/MedicationRequest contains a known chemo drug."""
    resource_type = resource.get("resourceType", "")
    if resource_type == "MedicationStatement":
        med_name, _ = resolve_medication_for_statement(
            resource, fhir_base_url, http_client, medication_cache_fn
        )
    else:
        med_name = ""
        med_code = resource.get("medicationCodeableConcept", {})
        if med_code:
            med_name = med_code.get("text", "")
            if not med_name:
                for coding in med_code.get("coding", []):
                    med_name = coding.get("display", "")
                    if med_name:
                        break
        if not med_name:
            med_name = resource.get("medicationReference", {}).get("display", "")
    med_name_lower = (med_name or "").lower()
    return any(drug in med_name_lower for drug in _CHEMO_DRUG_PATTERNS)


_PROCEDURE_IMMUNOTHERAPY_KEYWORDS = [
    "immunotherapy", "immuntherapie", "immunotherapie",
    "atezolizumab", "nivolumab", "pembrolizumab", "durvalumab",
    "ipilimumab", "avelumab", "checkpoint", " ici ",
    "pd-1", "pd-l1", "ctla-4",
]

_PROCEDURE_EXCLUDED_KEYWORDS = [
    "psycho", "counseling", "beratung", "palliativ",
]


def map_resource_to_event_type(resource_type: str, resource: dict,
                               fhir_base_url: str = None, http_client=None,
                               medication_cache_fn=None) -> Optional[str]:
    """Map a FHIR resource type to a timeline event type.

    Returns None for Procedure resources that should be excluded from the timeline
    (e.g. psychotherapy, palliative counseling).
    """
    mapping = {
        "Condition": "Initial Diagnosis",
        "DiagnosticReport": "Imaging",
        "Observation": "Lab",
        "Procedure": "Surgery",
        "Medication": "Medication",
        "CarePlan": "Tumor Board",
    }

    if resource_type == "Procedure":
        # Collect display text from all codings + code.text to avoid missing
        # information when coding[0] is a code-only entry without a display.
        code = resource.get("code", {})
        displays = [code.get("text", "")]
        for coding in code.get("coding", []):
            displays.append(coding.get("display", ""))
        combined = " " + " ".join(filter(None, displays)).lower() + " "

        if "radiation" in combined or "radiotherapy" in combined or "bestrahlung" in combined:
            return "Radiation"
        if any(kw in combined for kw in _PROCEDURE_IMMUNOTHERAPY_KEYWORDS):
            return "Medication"
        if any(kw in combined for kw in _PROCEDURE_EXCLUDED_KEYWORDS):
            return None

    if resource_type == "DiagnosticReport":
        # 1. Explicit imagingStudy reference → Imaging
        if resource.get("imagingStudy"):
            return "Imaging"
        # 2. Category codings: SNOMED 413675001 or pathology-related display
        _PATHOLOGY_CAT_KW = ("tissue", "pathology", "histolog", "biopsy", "zytolog", "patho")
        for cat in resource.get("category", []):
            for coding in cat.get("coding", []):
                if coding.get("code") == "413675001":
                    return "Pathology"
                display = (coding.get("display") or "").lower()
                if any(kw in display for kw in _PATHOLOGY_CAT_KW):
                    return "Pathology"
        # 3. code.coding / code.text contains pathology keywords
        _PATHOLOGY_CODE_KW = ("patholog", "histolog", "biopsy", "biopsie", "zytolog", "gewebe")
        code = resource.get("code", {})
        code_text = (code.get("text") or "").lower()
        if any(kw in code_text for kw in _PATHOLOGY_CODE_KW):
            return "Pathology"
        for coding in code.get("coding", []):
            display = (coding.get("display") or "").lower()
            if any(kw in display for kw in _PATHOLOGY_CODE_KW):
                return "Pathology"
        # 4. Default fall-through → Imaging
        return "Imaging"

    if resource_type in ("MedicationStatement", "MedicationRequest"):
        if _is_chemo_medication(resource, fhir_base_url, http_client, medication_cache_fn):
            return "Chemotherapy"
        return "Medication"

    return mapping.get(resource_type, resource_type)


def create_timeline_event(source_id: str, patient_id: str, resource: dict,
                          fhir_base_url: str = None, http_client=None,
                          medication_cache_fn=None):
    """Create a timeline event from a FHIR resource."""
    resource_type = resource.get("resourceType", "Unknown")
    if resource_type in {"Medication", "ImagingStudy", "Observation"}:
        return

    event_type = map_resource_to_event_type(
        resource_type, resource, fhir_base_url, http_client, medication_cache_fn
    )
    if event_type is None:
        return
    event_date = get_resource_date(resource)
    title = get_resource_title(resource, fhir_base_url, http_client, medication_cache_fn)

    # Extract key insight based on resource type
    key_insight = None
    if resource_type == "DiagnosticReport" and resource.get("conclusion"):
        key_insight = resource["conclusion"]
    elif resource_type == "Observation" and resource.get("valueString"):
        key_insight = resource["valueString"]
    elif resource_type == "Condition":
        clinical_status = resource.get("clinicalStatus", {}).get("coding", [{}])[0].get("code", "")
        key_insight = f"Status: {clinical_status}"
    elif resource_type == "Medication":
        details = _extract_medication_details(resource)
        insight_parts = []
        if details.get("dose_form"):
            insight_parts.append(f"Form: {details['dose_form']}")
        if details.get("strength"):
            insight_parts.append(f"Strength: {details['strength']}")
        if insight_parts:
            key_insight = " | ".join(insight_parts)
    elif resource_type == "MedicationStatement":
        med_name, med_details = resolve_medication_for_statement(
            resource, fhir_base_url, http_client, medication_cache_fn
        )
        dosage_text, route_text = _extract_medication_statement_dose_route(resource)
        category = _extract_first_coding_display(resource.get("category"))
        status = resource.get("status")

        insight_parts = [f"Medication: {med_name}"]
        if status:
            insight_parts.append(f"Status: {status}")
        if category:
            insight_parts.append(f"Category: {category}")
        if dosage_text:
            insight_parts.append(f"Dose: {dosage_text}")
        if route_text:
            insight_parts.append(f"Route: {route_text}")
        if med_details.get("dose_form"):
            insight_parts.append(f"Form: {med_details['dose_form']}")
        if med_details.get("strength"):
            insight_parts.append(f"Strength: {med_details['strength']}")
        key_insight = " | ".join(insight_parts)

    conn = get_db_connection()
    existing = conn.execute(
        "SELECT id FROM timeline_events WHERE source_id = ? AND patient_id = ?",
        (source_id, patient_id)
    ).fetchone()
    if existing:
        has_keypoints = conn.execute(
            "SELECT 1 FROM keypoints WHERE source_id = ? LIMIT 1",
            (source_id,)
        ).fetchone()
        if has_keypoints:
            conn.execute(
                "UPDATE timeline_events SET event_type = ?, event_date = ?, title = ?, priority = ? WHERE id = ?",
                (event_type, event_date, title, 3, existing[0])
            )
        else:
            conn.execute(
                "UPDATE timeline_events SET event_type = ?, event_date = ?, title = ?, key_insight = ?, priority = ? WHERE id = ?",
                (event_type, event_date, title, key_insight, 3, existing[0])
            )
        conn.commit()
        conn.close()
        return

    conn.execute("""
        INSERT INTO timeline_events (source_id, patient_id, event_type, event_date, title, key_insight, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (source_id, patient_id, event_type, event_date, title, key_insight, 3))
    conn.commit()
    conn.close()


def is_chemo_event(event: dict) -> bool:
    """Check if a timeline event represents a chemotherapy administration."""
    event_type = (event.get("event_type") or "").lower()
    if "chemo" in event_type:
        return True
    title = (event.get("title") or "").lower()
    key_insight = (event.get("key_insight") or "").lower()
    combined = title + " " + key_insight
    return any(drug in combined for drug in _CHEMO_DRUG_PATTERNS)


def group_chemo_timeline_events(events: list[dict]) -> list[dict]:
    """Group individual chemotherapy medication events into cycles.

    Events within 5 days of each other are considered the same cycle.
    Non-chemo events pass through unchanged.
    Events without valid dates are dropped (supportive meds without dates).
    """
    chemo_events = []
    non_chemo_events = []
    for e in events:
        if is_chemo_event(e):
            if e.get("event_date"):
                chemo_events.append(e)
        else:
            non_chemo_events.append(e)

    if not chemo_events:
        return non_chemo_events

    chemo_events.sort(key=lambda e: (e.get("event_date") or "", str(e.get("id", ""))))

    cycles: list[list[dict]] = []
    current_cycle: list[dict] = []
    for event in chemo_events:
        if not current_cycle:
            current_cycle.append(event)
            continue
        try:
            last_date_str = current_cycle[-1].get("event_date") or ""
            this_date_str = event.get("event_date") or ""
            if not last_date_str or not this_date_str:
                raise ValueError("missing date")
            last_date = _parse_date_naive(last_date_str)
            this_date = _parse_date_naive(this_date_str)
            if not last_date or not this_date:
                raise ValueError("unparseable date")
            if abs((this_date - last_date).days) <= 5:
                current_cycle.append(event)
            else:
                cycles.append(current_cycle)
                current_cycle = [event]
        except (ValueError, TypeError):
            cycles.append(current_cycle)
            current_cycle = [event]

    if current_cycle:
        cycles.append(current_cycle)

    grouped = []
    for i, cycle in enumerate(cycles, 1):
        med_names = list(dict.fromkeys(e.get("title", "") for e in cycle))
        sub_source_ids = [e["source_id"] for e in cycle if e.get("source_id")]
        grouped.append({
            "id": cycle[0].get("id", ""),
            "source_id": cycle[0].get("source_id", ""),
            "event_type": "Chemotherapy",
            "event_date": cycle[0].get("event_date"),
            "title": f"Chemotherapy Cycle {i}",
            "key_insight": f"Medications: {', '.join(med_names)}",
            "priority": min((e.get("priority") or 3) for e in cycle),
            "sub_source_ids": sub_source_ids,
        })

    return non_chemo_events + grouped


# ---------------------------------------------------------------------------
# Treatment response classification
# ---------------------------------------------------------------------------

_STATUS_LABELS = {
    "PD": "Progressive Disease",
    "SD": "Stable Disease",
    "PR": "Partial Response",
    "CR": "Complete Response",
}


def _parse_date_naive(date_str: str) -> Optional[datetime]:
    """Parse an ISO date string into a timezone-naive datetime."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        # Strip timezone to avoid naive/aware comparison errors
        return dt.replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _find_imaging_for_cycles(
    chemo_cycles: list[dict],
    all_events: list[dict],
) -> list[tuple[dict, Optional[dict]]]:
    """For each chemo cycle, find the best nearby imaging event.

    Time windows:
    - First cycle: -30 to +60 days (captures baseline staging)
    - Later cycles: -7 to +60 days
    Prefers post-cycle imaging (restaging), nearest first.
    Each imaging event is assigned to at most one cycle.
    """
    imaging_events = [
        e for e in all_events
        if (e.get("event_type") or "").lower() in ("imaging", "diagnosticreport")
        and e.get("event_date")
    ]
    used_imaging_ids = set()
    pairs: list[tuple[dict, Optional[dict]]] = []

    sorted_cycles = sorted(chemo_cycles, key=lambda c: c.get("event_date") or "")

    for idx, cycle in enumerate(sorted_cycles):
        cycle_date_str = cycle.get("event_date")
        if not cycle_date_str:
            pairs.append((cycle, None))
            continue

        cycle_date = _parse_date_naive(cycle_date_str)
        if not cycle_date:
            pairs.append((cycle, None))
            continue

        pre_window = -30 if idx == 0 else -7
        candidates = []
        for img in imaging_events:
            if img.get("id") in used_imaging_ids:
                continue
            img_date = _parse_date_naive(img.get("event_date", ""))
            if not img_date:
                continue
            delta_days = (img_date - cycle_date).days
            if pre_window <= delta_days <= 60:
                candidates.append((img, delta_days))

        # Prefer post-cycle (positive delta), then nearest
        candidates.sort(key=lambda x: (0 if x[1] >= 0 else 1, abs(x[1])))

        if candidates:
            best_img = candidates[0][0]
            used_imaging_ids.add(best_img.get("id"))
            pairs.append((cycle, best_img))
        else:
            pairs.append((cycle, None))

    return pairs


_CT_KEYWORDS_RE = re.compile(r"\b(c?ct|computed\s+tomography|ct-?scan|computertomograph\w*)\b", flags=re.IGNORECASE)


def _normalize_fhir_reference_id(ref_id: str) -> str:
    """Normalize IDs like is04 -> is4 so mismatched padding still resolves."""
    if not ref_id:
        return ""
    rid = str(ref_id).strip()
    m = re.match(r"^([A-Za-z]+)0*([0-9]+)$", rid)
    if not m:
        return rid
    return f"{m.group(1)}{int(m.group(2))}"


def _extract_imaging_study_modality_codes(resource: dict) -> set[str]:
    """Extract modality codes/displays from ImagingStudy.modality and series.modality."""
    codes: set[str] = set()

    def _add_modality(modality_obj: dict):
        if not isinstance(modality_obj, dict):
            return
        direct_code = modality_obj.get("code")
        direct_display = modality_obj.get("display")
        if isinstance(direct_code, str) and direct_code.strip():
            codes.add(direct_code.strip().upper())
        if isinstance(direct_display, str) and direct_display.strip():
            codes.add(direct_display.strip().upper())
        for coding in modality_obj.get("coding", []) or []:
            if not isinstance(coding, dict):
                continue
            c_code = coding.get("code")
            c_display = coding.get("display")
            if isinstance(c_code, str) and c_code.strip():
                codes.add(c_code.strip().upper())
            if isinstance(c_display, str) and c_display.strip():
                codes.add(c_display.strip().upper())

    for modality in resource.get("modality", []) or []:
        _add_modality(modality)
    for series in resource.get("series", []) or []:
        if isinstance(series, dict):
            _add_modality(series.get("modality") or {})

    return codes


def _is_ct_from_codes(codes: set[str]) -> bool:
    for code in codes:
        upper = code.upper()
        if upper == "CT" or upper.startswith("CT "):
            return True
        if "COMPUTED TOMOGRAPHY" in upper:
            return True
    return False


def _find_ct_imaging_events(all_events: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """Return timeline imaging events that correspond to CT reports."""
    imaging_events = [
        e for e in all_events
        if (e.get("event_type") or "").lower() in ("imaging", "diagnosticreport")
        and e.get("source_id")
        and e.get("event_date")
    ]
    if not imaging_events:
        return []

    source_ids = list({e["source_id"] for e in imaging_events if e.get("source_id")})
    placeholders = ",".join(["?"] * len(source_ids))
    src_rows = conn.execute(
        f"SELECT source_id, title, content_markdown, metadata FROM sources WHERE source_id IN ({placeholders})",
        source_ids,
    ).fetchall()
    report_by_source_id = {row["source_id"]: row for row in src_rows}

    # Build ImagingStudy modality lookup by both raw and normalized FHIR IDs.
    img_rows = conn.execute(
        "SELECT metadata FROM sources WHERE resource_type = 'ImagingStudy'"
    ).fetchall()
    modality_by_imaging_id: dict[str, set[str]] = {}
    for row in img_rows:
        metadata_raw = row["metadata"]
        if not metadata_raw:
            continue
        try:
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        resource = metadata.get("fhir_resource") if isinstance(metadata, dict) else None
        if not isinstance(resource, dict):
            continue
        fhir_id = resource.get("id")
        if not isinstance(fhir_id, str) or not fhir_id.strip():
            continue
        modality_codes = _extract_imaging_study_modality_codes(resource)
        if not modality_codes:
            continue
        modality_by_imaging_id[fhir_id] = modality_codes
        modality_by_imaging_id[_normalize_fhir_reference_id(fhir_id)] = modality_codes

    ct_events: list[dict] = []

    for event in imaging_events:
        src_row = report_by_source_id.get(event["source_id"])
        if src_row is None:
            continue

        report_resource: dict = {}
        metadata_raw = src_row["metadata"]
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw)
                maybe_resource = metadata.get("fhir_resource") if isinstance(metadata, dict) else None
                if isinstance(maybe_resource, dict):
                    report_resource = maybe_resource
            except (json.JSONDecodeError, TypeError):
                report_resource = {}

        # 1) Prefer explicit modality from linked ImagingStudy references.
        is_ct = False
        for ref in report_resource.get("imagingStudy", []) or []:
            ref_value = ref.get("reference", "") if isinstance(ref, dict) else str(ref)
            if not ref_value:
                continue
            ref_id = ref_value.split("/", 1)[1] if "/" in ref_value else ref_value
            if not ref_id:
                continue
            modalities = (
                modality_by_imaging_id.get(ref_id)
                or modality_by_imaging_id.get(_normalize_fhir_reference_id(ref_id))
            )
            if modalities and _is_ct_from_codes(modalities):
                is_ct = True
                break

        # 2) Fallback for datasets where modality metadata is missing.
        if not is_ct:
            fallback_parts: list[str] = [str(src_row["title"] or "")]
            code = report_resource.get("code") or {}
            if isinstance(code, dict):
                fallback_parts.append(str(code.get("text") or ""))
                for coding in code.get("coding", []) or []:
                    if isinstance(coding, dict):
                        fallback_parts.append(str(coding.get("display") or ""))
                        fallback_parts.append(str(coding.get("code") or ""))
            fallback_parts.append(str((src_row["content_markdown"] or "")[:600]))
            fallback_blob = " ".join(fallback_parts)
            if _CT_KEYWORDS_RE.search(fallback_blob):
                is_ct = True

        if is_ct:
            ct_events.append(event)

    return ct_events


def _classify_response_with_llm(imaging_content: str, timeout_seconds: int = 120) -> dict:
    """Call LLM to classify treatment response from imaging report text.

    Returns dict with keys: status, confidence, basis.
    """
    from agent.model import llm_client

    prompt = (
        "You are an oncology expert. Based on the following imaging report, classify the "
        "treatment response using RECIST-like categories.\n\n"
        "Categories:\n"
        "- PD (Progressive Disease): new lesions, growth, enlargement, worsening\n"
        "- SD (Stable Disease): no significant change, unchanged\n"
        "- PR (Partial Response): shrinkage, partial regression, decrease in size\n"
        "- CR (Complete Response): no residual disease, complete resolution\n\n"
        f"Imaging Report:\n{imaging_content}\n\n"
        "Respond ONLY with a JSON object, no additional text:\n"
        '{"status": "PD or SD or PR or CR", "confidence": "high or medium or low", '
        '"basis": "one-sentence explanation of why this classification was chosen"}'
    )

    try:
        _start = time.monotonic()
        stream = llm_client.chat.completions.create(
            model=LLM_MODEL,
            timeout=timeout_seconds,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            stream=True,
        )
        chunks: list[str] = []
        for chunk in stream:
            if time.monotonic() - _start > timeout_seconds:
                logger.warning("Treatment response LLM classification timed out")
                try:
                    stream.close()
                except Exception:
                    pass
                break
            if chunk.choices and chunk.choices[0].delta.content:
                chunks.append(chunk.choices[0].delta.content)
        content = "".join(chunks).strip()
        # Strip <think>...</think> tags (some models include reasoning)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        # Strip markdown code fences if present
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        result = json.loads(content)
        status = result.get("status", "").upper()
        if status not in _STATUS_LABELS:
            logger.warning("Treatment response LLM returned unrecognized status: %r (raw: %s)", status, content[:200])
            return {"status": "Unknown", "confidence": "low", "basis": None}
        return {
            "status": status,
            "confidence": result.get("confidence", "medium"),
            "basis": result.get("basis"),
        }
    except Exception as e:
        logger.warning("Treatment response LLM classification failed: %s", e)
        return {"status": "Unknown", "confidence": "low", "basis": None}


def compute_treatment_responses(
    patient_id: str,
    all_events: list[dict],
) -> list[dict]:
    """Return cached treatment responses for CT reports, triggering
    background LLM generation for any missing/stale entries."""
    if not all_events:
        logger.info("compute_treatment_responses: no timeline events for patient %s", patient_id)
        return []

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    ct_events = _find_ct_imaging_events(all_events, conn)
    if not ct_events:
        logger.info("compute_treatment_responses: no CT imaging events for patient %s", patient_id)
        conn.close()
        return []

    event_ids = [str(e["id"]) for e in ct_events]
    cached_rows = conn.execute(
        "SELECT * FROM treatment_responses WHERE patient_id = ? ORDER BY created_at DESC, id DESC",
        (patient_id,),
    ).fetchall()

    # Exact cache: new format keyed by CT timeline event id.
    cached_exact: dict[str, dict] = {}
    # Legacy fallback: previous format can still be reused by imaging source id.
    cached_legacy_by_source_id: dict[str, dict] = {}
    for row in cached_rows:
        row_dict = dict(row)
        row_event_id = str(row["cycle_event_id"])
        if row_event_id not in cached_exact:
            cached_exact[row_event_id] = row_dict
        try:
            row_source_ids = json.loads(row_dict.get("imaging_source_ids") or "[]")
        except (json.JSONDecodeError, TypeError):
            row_source_ids = []
        for sid in row_source_ids:
            if sid and sid not in cached_legacy_by_source_id:
                cached_legacy_by_source_id[sid] = row_dict

    # Resolve per-CT-event cache entry, preferring exact IDs and falling back to legacy source-id matches.
    cached: dict[str, dict] = {}
    for ct_event in ct_events:
        event_id = str(ct_event["id"])
        source_id = ct_event.get("source_id")
        row = cached_exact.get(event_id)
        if row is None and source_id:
            row = cached_legacy_by_source_id.get(source_id)
        if row is not None:
            cached[event_id] = row

    logger.info(
        "compute_treatment_responses: %d CT reports, %d cached responses, event_ids=%s",
        len(ct_events), len(cached), event_ids,
    )

    needs_generation: list[tuple[dict, str]] = []  # (ct_event, content_hash)
    for ct_event in ct_events:
        event_id = str(ct_event["id"])
        source_id = ct_event.get("source_id")
        if not source_id:
            continue

        # Check if source content has changed (staleness)
        src_row = conn.execute(
            "SELECT content_hash FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        current_hash = src_row["content_hash"] if src_row else None

        if event_id in cached:
            # Retry Unknown classifications even if content hasn't changed.
            if cached[event_id].get("status") == "Unknown":
                needs_generation.append((ct_event, current_hash or ""))
                continue
            cached_hash = cached[event_id].get("content_hash")
            if cached_hash and current_hash and cached_hash == current_hash:
                continue  # still fresh

        needs_generation.append((ct_event, current_hash or ""))

    conn.close()

    if needs_generation:
        logger.info(
            "Triggering background treatment response generation for %d CT reports (patient %s)",
            len(needs_generation), patient_id,
        )
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_generate_treatment_responses_batch(patient_id, needs_generation))
        except RuntimeError:
            pass

    # Return cached results, filtering out Unknown
    results = []
    for ct_event in ct_events:
        event_id = str(ct_event["id"])
        row = cached.get(event_id)
        if not row:
            continue
        if row["status"] == "Unknown":
            continue
        try:
            imaging_source_ids = json.loads(row["imaging_source_ids"]) if row["imaging_source_ids"] else []
        except (json.JSONDecodeError, TypeError):
            imaging_source_ids = []
        if not imaging_source_ids and ct_event.get("source_id"):
            imaging_source_ids = [ct_event["source_id"]]
        results.append({
            "cycle_id": event_id,
            "status": row["status"],
            "status_label": row["status_label"],
            "confidence": row["confidence"] or "medium",
            "basis": row["basis"],
            "imaging_source_ids": imaging_source_ids,
            "imaging_date": row["imaging_date"] or ct_event.get("event_date"),
        })
    return results


async def _generate_treatment_responses_batch(
    patient_id: str,
    items: list[tuple[dict, str]],
):
    """Background task: LLM-classify treatment response for each CT imaging event."""
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        _start = time.monotonic()

        for event, content_hash in items:
            try:
                source_id = event.get("source_id")
                if not source_id:
                    continue

                # Fetch imaging source content
                source_row = conn.execute(
                    "SELECT content, content_markdown FROM sources WHERE source_id = ?",
                    (source_id,),
                ).fetchone()
                if not source_row:
                    continue
                imaging_text = source_row["content_markdown"] or source_row["content"] or ""
                if not imaging_text.strip():
                    continue

                # LLM classification (in thread to avoid blocking event loop)
                result = await asyncio.to_thread(_classify_response_with_llm, imaging_text)

                status = result["status"]
                status_label = _STATUS_LABELS.get(status, "Unknown")

                imaging_source_ids = json.dumps([source_id])

                conn.execute(
                    """INSERT OR REPLACE INTO treatment_responses
                    (patient_id, cycle_event_id, cycle_date, status, status_label,
                     confidence, basis, imaging_source_ids, imaging_date, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        patient_id,
                        str(event["id"]),
                        event.get("event_date"),
                        status,
                        status_label,
                        result.get("confidence", "medium"),
                        result.get("basis"),
                        imaging_source_ids,
                        event.get("event_date"),
                        content_hash,
                    ),
                )
                conn.commit()
                logger.info(
                    "Treatment response for CT report %s: %s (%s)",
                    event.get("title", event.get("id")), status, result.get("confidence"),
                )
            except Exception:
                logger.exception("Failed to classify response for CT report %s", event.get("id"))

        _elapsed = time.monotonic() - _start
        logger.info(
            "Completed treatment response generation for %d CT reports (patient %s) in %.1fs",
            len(items), patient_id, _elapsed,
        )
        conn.close()
    except Exception:
        logger.exception("Treatment response batch generation failed")
