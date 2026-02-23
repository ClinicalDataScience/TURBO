"""Fetch FHIR resources and register them as SQLite sources."""
import json
import logging
import httpx

from config import FHIR_BASE_URL
from database import get_db_connection, register_source, _compute_content_hash, _compute_patient_data_hash
from fhir.converter import (
    fhir_resource_to_markdown, get_resource_title, get_resource_date,
    _extract_medication_reference_id,
)
from fhir.client import DirectFHIRClient
from services.timeline import create_timeline_event

logger = logging.getLogger("medgemma-agent")

# Global FHIR clients — initialized by init_fhir_clients()
fhir_client: DirectFHIRClient = None
fhir_sync_client: httpx.Client = None


def init_fhir_clients():
    """Initialize async and sync FHIR clients. Call during app startup."""
    global fhir_client, fhir_sync_client
    fhir_client = DirectFHIRClient(FHIR_BASE_URL)
    fhir_sync_client = httpx.Client(timeout=30.0)
    logger.info("FHIR clients initialized with base URL: %s", FHIR_BASE_URL)


async def close_fhir_clients():
    """Close FHIR clients. Call during app shutdown."""
    global fhir_client, fhir_sync_client
    if fhir_client:
        await fhir_client.close()
    if fhir_sync_client:
        fhir_sync_client.close()


def _medication_cache_fn(med_id: str):
    """SQLite-backed medication cache lookup for resolve_medication_for_statement."""
    import sqlite3
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT metadata FROM sources WHERE source_type = 'fhir' AND resource_type = 'Medication' AND fhir_id = ?",
        (med_id,)
    ).fetchone()
    conn.close()
    if row and row["metadata"]:
        try:
            metadata = json.loads(row["metadata"])
            return metadata.get("fhir_resource")
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    return None


async def fetch_and_register_fhir_resources(patient_id: str, retry: bool = False) -> list[str]:
    """Fetch all FHIR resources for a patient and register them as sources.

    Computes content hashes for each resource and an aggregate patient hash.
    If FHIR data has changed since last fetch, stale keypoints and summaries
    are automatically invalidated.
    """
    if not fhir_client:
        logger.warning("FHIR client not available")
        return []

    source_ids = []
    changed_source_ids = []
    all_content_hashes = []

    resource_types = [
        "Patient", "Condition", "DiagnosticReport", "ImagingStudy", "Observation",
        "Procedure", "Medication", "MedicationStatement", "MedicationRequest",
        "CarePlan", "AllergyIntolerance", "Immunization",
    ]

    for resource_type in resource_types:
        try:
            if resource_type == "Patient":
                resource = await fhir_client.get_resource("Patient", patient_id, retry=retry)
                resources = [{"resource": resource}] if resource else []
            else:
                resources = await fhir_client.search_resources(resource_type, patient_id, retry=retry)

            for entry in resources:
                resource = entry.get("resource", entry)
                fhir_id = resource.get("id")

                resource_json = json.dumps(resource, indent=2)
                source_id, content_changed = register_source(
                    source_type="fhir",
                    resource_type=resource.get("resourceType", resource_type),
                    content=resource_json,
                    title=get_resource_title(resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn),
                    fhir_id=fhir_id,
                    patient_id=patient_id,
                    date=get_resource_date(resource),
                    metadata={"fhir_resource": resource},
                )

                content_hash = _compute_content_hash(resource_json)
                all_content_hashes.append(content_hash)
                if content_changed:
                    changed_source_ids.append(source_id)

                # Update content_markdown
                markdown = fhir_resource_to_markdown(
                    resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn
                )
                conn = get_db_connection()
                conn.execute(
                    "UPDATE sources SET content_markdown = ? WHERE source_id = ?",
                    (markdown, source_id)
                )
                conn.commit()
                conn.close()

                source_ids.append(source_id)

                # Create timeline event (skip raw ImagingStudy)
                if resource.get("resourceType") != "ImagingStudy":
                    create_timeline_event(
                        source_id, patient_id, resource,
                        FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn,
                    )

                # Ensure referenced ImagingStudy resources are available
                if resource_type == "DiagnosticReport":
                    for study_ref in resource.get("imagingStudy", []) or []:
                        if not isinstance(study_ref, dict):
                            continue
                        from fhir.converter import _extract_reference_id
                        ref = study_ref.get("reference", "")
                        study_id = _extract_reference_id(ref, "ImagingStudy")
                        if not study_id:
                            continue
                        study_resource = await fhir_client.get_resource("ImagingStudy", study_id, retry=retry)
                        if not study_resource:
                            continue
                        study_source_id, _ = register_source(
                            source_type="fhir",
                            resource_type=study_resource.get("resourceType", "ImagingStudy"),
                            content=json.dumps(study_resource, indent=2),
                            title=get_resource_title(study_resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn),
                            fhir_id=study_id,
                            patient_id=patient_id,
                            date=get_resource_date(study_resource),
                            metadata={"fhir_resource": study_resource},
                        )
                        study_markdown = fhir_resource_to_markdown(
                            study_resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn
                        )
                        study_conn = get_db_connection()
                        study_conn.execute(
                            "UPDATE sources SET content_markdown = ? WHERE source_id = ?",
                            (study_markdown, study_source_id)
                        )
                        study_conn.commit()
                        study_conn.close()
                        source_ids.append(study_source_id)

                # Fetch referenced Medication for MedicationStatements
                if resource_type == "MedicationStatement":
                    med_id = _extract_medication_reference_id(resource)
                    if med_id:
                        med_resource = await fhir_client.get_resource("Medication", med_id, retry=retry)
                        if med_resource:
                            med_source_id, _ = register_source(
                                source_type="fhir",
                                resource_type=med_resource.get("resourceType", "Medication"),
                                content=json.dumps(med_resource, indent=2),
                                title=get_resource_title(med_resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn),
                                fhir_id=med_id,
                                patient_id=patient_id,
                                date=get_resource_date(med_resource),
                                metadata={"fhir_resource": med_resource},
                            )
                            med_markdown = fhir_resource_to_markdown(
                                med_resource, FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn
                            )
                            med_conn = get_db_connection()
                            med_conn.execute(
                                "UPDATE sources SET content_markdown = ? WHERE source_id = ?",
                                (med_markdown, med_source_id)
                            )
                            med_conn.commit()
                            med_conn.close()
                            source_ids.append(med_source_id)
                            create_timeline_event(
                                med_source_id, patient_id, med_resource,
                                FHIR_BASE_URL, fhir_sync_client, _medication_cache_fn,
                            )

        except Exception as e:
            logger.error("Failed to fetch %s for patient %s: %s", resource_type, patient_id, e)

    # Aggregate hash: detect patient-level data changes
    if all_content_hashes:
        new_patient_hash = _compute_patient_data_hash(all_content_hashes)

        conn = get_db_connection()
        existing = conn.execute(
            "SELECT fhir_data_hash FROM patient_hashes WHERE patient_id = ?",
            (patient_id,)
        ).fetchone()
        old_hash = existing[0] if existing else None

        if old_hash != new_patient_hash:
            conn.execute("""
                INSERT INTO patient_hashes (patient_id, fhir_data_hash, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(patient_id) DO UPDATE SET
                    fhir_data_hash = excluded.fhir_data_hash,
                    updated_at = CURRENT_TIMESTAMP
            """, (patient_id, new_patient_hash))

            if changed_source_ids:
                placeholders = ",".join(["?" for _ in changed_source_ids])
                conn.execute(
                    f"DELETE FROM keypoints WHERE source_id IN ({placeholders})",
                    changed_source_ids,
                )
                conn.execute(
                    f"UPDATE timeline_events SET key_insight = NULL WHERE source_id IN ({placeholders})",
                    changed_source_ids,
                )
                logger.info(
                    "Invalidated keypoints for %d changed sources (patient %s)",
                    len(changed_source_ids), patient_id,
                )

            conn.commit()
        conn.close()

    return source_ids
