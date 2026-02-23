"""Convert FHIR resources to markdown and extract metadata."""
import json
import logging
from typing import Optional

logger = logging.getLogger("medgemma-agent")


# ---------------------------------------------------------------------------
# Low-level extraction helpers
# ---------------------------------------------------------------------------

def _extract_reference_id(reference: str, expected_type: str) -> Optional[str]:
    """Extract resource id from references like Medication/med01 or full URLs."""
    if not reference:
        return None
    marker = f"/{expected_type}/"
    if marker in reference:
        return reference.split(marker, 1)[1].split("/", 1)[0]
    if reference.startswith(f"{expected_type}/"):
        return reference.split("/", 1)[1]
    return None


def _extract_first_coding_display(concept) -> Optional[str]:
    """Return first coding display/code from CodeableConcept variants."""
    if isinstance(concept, list):
        concept = concept[0] if concept else {}
    if not isinstance(concept, dict):
        return None
    coding = concept.get("coding")
    if isinstance(coding, list):
        coding = coding[0] if coding else {}
    elif not isinstance(coding, dict):
        coding = {}
    if not isinstance(coding, dict):
        return None
    return coding.get("display") or coding.get("code")


def _extract_medication_statement_dose_route(resource: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract dose text and route from MedicationStatement."""
    dosage = resource.get("dosage")
    entries = dosage if isinstance(dosage, list) else [dosage] if isinstance(dosage, dict) else []
    for d in entries:
        if not isinstance(d, dict):
            continue
        simple = d.get("simple", d)
        dosage_text = simple.get("text") or d.get("text")
        route = _extract_first_coding_display(simple.get("route") or d.get("route"))
        return dosage_text, route
    return None, None


def _extract_medication_reference_id(resource: dict) -> Optional[str]:
    """Extract referenced Medication id from MedicationStatement variants."""
    med_ref = resource.get("medicationReference", {}).get("reference", "")
    if not med_ref:
        med_ref = resource.get("medicationCodeableReference", {}).get("reference", {}).get("reference", "")
    if not med_ref:
        med_ref = resource.get("medication", {}).get("reference", {}).get("reference", "")
    return _extract_reference_id(med_ref, "Medication")


def _extract_medication_details(medication_resource: dict) -> dict:
    """Extract key medication details from a Medication resource."""
    dose_form = medication_resource.get("doseForm", {}).get("coding", [{}])[0].get("display", "")
    dose_form = dose_form.strip() if dose_form else None
    ingredient = (medication_resource.get("ingredient") or [{}])[0]
    numerator = ingredient.get("strengthRatio", {}).get("numerator", {})
    strength = None
    if numerator.get("value") is not None:
        unit = numerator.get("code") or numerator.get("unit") or ""
        strength = f"{numerator.get('value')} {unit}".strip()
    return {"dose_form": dose_form, "strength": strength}


# ---------------------------------------------------------------------------
# Medication resolution (parameterized for app.py vs mcp_server.py)
# ---------------------------------------------------------------------------

def resolve_medication_for_statement(
    resource: dict,
    fhir_base_url: str = None,
    http_client=None,
    medication_cache_fn=None,
) -> tuple[str, dict]:
    """Resolve medication name/details for a MedicationStatement.

    Args:
        resource: The MedicationStatement FHIR resource.
        fhir_base_url: FHIR server base URL for HTTP fallback.
        http_client: httpx.Client for direct FHIR lookup (optional).
        medication_cache_fn: callable(med_id) -> dict|None for cached lookup (optional).
    """
    concept = resource.get("medicationCodeableConcept", {})
    concept_display = concept.get("coding", [{}])[0].get("display") or concept.get("text")
    if concept_display:
        return concept_display, {}

    med_id = _extract_medication_reference_id(resource)
    if not med_id:
        return "Unknown", {}

    # Try cache (app.py passes SQLite lookup; mcp_server.py skips)
    if medication_cache_fn:
        cached = medication_cache_fn(med_id)
        if cached:
            code = cached.get("code", {})
            med_name = code.get("coding", [{}])[0].get("display") or code.get("text") or med_id
            return med_name, _extract_medication_details(cached)

    # Fallback: HTTP lookup
    if http_client is not None and fhir_base_url:
        try:
            fhir_url = fhir_base_url.rstrip("/")
            response = http_client.get(f"{fhir_url}/Medication/{med_id}")
            if response.status_code == 200:
                med_resource = response.json()
                code = med_resource.get("code", {})
                med_name = code.get("coding", [{}])[0].get("display") or code.get("text") or med_id
                return med_name, _extract_medication_details(med_resource)
        except Exception:
            pass

    return med_id, {}


# ---------------------------------------------------------------------------
# FHIR resource → markdown
# ---------------------------------------------------------------------------

def fhir_resource_to_markdown(
    resource: dict,
    fhir_base_url: str = None,
    http_client=None,
    medication_cache_fn=None,
) -> str:
    """Convert a FHIR resource to a markdown representation.

    The medication-resolution parameters are forwarded to
    resolve_medication_for_statement when handling MedicationStatement resources.
    """
    resource_type = resource.get("resourceType", "Unknown")
    lines = [f"# {resource_type}"]

    if resource_type == "Patient":
        name = resource.get("name", [{}])[0]
        lines.append(f"**Name:** {name.get('given', [''])[0]} {name.get('family', '')}")
        lines.append(f"**Birth Date:** {resource.get('birthDate', 'Unknown')}")
        lines.append(f"**Gender:** {resource.get('gender', 'Unknown')}")

    elif resource_type == "Condition":
        code = resource.get("code", {}).get("coding", [{}])[0]
        lines.append(f"**Condition:** {code.get('display', resource.get('code', {}).get('text', 'Unknown'))}")
        lines.append(f"**Clinical Status:** {resource.get('clinicalStatus', {}).get('coding', [{}])[0].get('code', 'Unknown')}")
        if resource.get("onsetDateTime"):
            lines.append(f"**Onset:** {resource.get('onsetDateTime')}")

    elif resource_type == "DiagnosticReport":
        codings = resource.get("code", {}).get("coding", [])
        code_displays = [
            c.get("display") or c.get("code")
            for c in codings
            if isinstance(c, dict) and (c.get("display") or c.get("code"))
        ]
        if code_displays:
            lines.append(f"**Report:** {code_displays[0]}")
            if len(code_displays) > 1:
                lines.append(f"**Body Regions:** {', '.join(code_displays)}")
        else:
            lines.append("**Report:** Unknown")
        lines.append(f"**Status:** {resource.get('status', 'Unknown')}")
        report_date = resource.get("effectiveDateTime") or resource.get("issued")
        if report_date:
            lines.append(f"**Date:** {report_date}")
        imaging_refs = []
        for ref in resource.get("imagingStudy", []) or []:
            if isinstance(ref, dict) and ref.get("reference"):
                imaging_refs.append(ref["reference"])
        if imaging_refs:
            lines.append(f"**ImagingStudy Refs:** {', '.join(imaging_refs)}")
        if resource.get("conclusion"):
            lines.append(f"\n**Conclusion:**\n{resource.get('conclusion')}")

    elif resource_type == "ImagingStudy":
        lines.append(f"**Status:** {resource.get('status', 'Unknown')}")
        if resource.get("started"):
            lines.append(f"**Date:** {resource.get('started')}")
        modalities = []
        body_sites = []
        series_descriptions = []
        for series in resource.get("series", []) or []:
            if not isinstance(series, dict):
                continue
            modality = series.get("modality", {}).get("coding", [{}])[0].get("code")
            if modality:
                modalities.append(modality)
            body_site = series.get("bodySite", {}).get("concept", {}).get("coding", [{}])[0].get("display")
            if body_site:
                body_sites.append(body_site)
            if series.get("description"):
                series_descriptions.append(series.get("description"))
        if modalities:
            lines.append(f"**Modality:** {', '.join(dict.fromkeys(modalities))}")
        if body_sites:
            lines.append(f"**Body Site:** {', '.join(dict.fromkeys(body_sites))}")
        if series_descriptions:
            lines.append(f"**Series Description:** {' | '.join(series_descriptions)}")

    elif resource_type == "Observation":
        code = resource.get("code", {}).get("coding", [{}])[0]
        lines.append(f"**Observation:** {code.get('display', 'Unknown')}")
        if resource.get("valueQuantity"):
            vq = resource.get("valueQuantity")
            lines.append(f"**Value:** {vq.get('value')} {vq.get('unit', '')}")
        elif resource.get("valueString"):
            lines.append(f"**Value:** {resource.get('valueString')}")
        if resource.get("effectiveDateTime"):
            lines.append(f"**Date:** {resource.get('effectiveDateTime')}")

    elif resource_type == "Procedure":
        code = resource.get("code", {}).get("coding", [{}])[0]
        lines.append(f"**Procedure:** {code.get('display', 'Unknown')}")
        lines.append(f"**Status:** {resource.get('status', 'Unknown')}")
        if resource.get("performedDateTime"):
            lines.append(f"**Date:** {resource.get('performedDateTime')}")
        elif resource.get("performedPeriod"):
            period = resource.get("performedPeriod", {})
            lines.append(f"**Period:** {period.get('start', '')} - {period.get('end', '')}")

    elif resource_type == "Medication":
        code = resource.get("code", {})
        med_name = code.get("coding", [{}])[0].get("display") or code.get("text") or "Unknown"
        lines.append(f"**Medication:** {med_name}")
        dose_form = resource.get("doseForm", {}).get("coding", [{}])[0].get("display")
        if dose_form:
            lines.append(f"**Dose Form:** {dose_form.strip()}")
        ingredient = (resource.get("ingredient") or [{}])[0]
        numerator = ingredient.get("strengthRatio", {}).get("numerator", {})
        if numerator.get("value") is not None:
            unit = numerator.get("code") or numerator.get("unit") or ""
            lines.append(f"**Strength:** {numerator.get('value')} {unit}".strip())

    elif resource_type == "MedicationStatement":
        med_name, med_details = resolve_medication_for_statement(
            resource, fhir_base_url, http_client, medication_cache_fn
        )
        lines.append(f"**Medication:** {med_name}")
        lines.append(f"**Status:** {resource.get('status', 'Unknown')}")
        if resource.get("effectiveDateTime"):
            lines.append(f"**Date:** {resource.get('effectiveDateTime')}")
        elif resource.get("effectivePeriod"):
            period = resource.get("effectivePeriod", {})
            lines.append(f"**Period:** {period.get('start', '')} - {period.get('end', '')}")
        category = _extract_first_coding_display(resource.get("category"))
        if category:
            lines.append(f"**Category:** {category}")
        dosage_text, route_text = _extract_medication_statement_dose_route(resource)
        if dosage_text:
            lines.append(f"**Dosage:** {dosage_text}")
        if route_text:
            lines.append(f"**Route:** {route_text}")
        if med_details:
            if med_details.get("dose_form"):
                lines.append(f"**Dose Form:** {med_details['dose_form']}")
            if med_details.get("strength"):
                lines.append(f"**Strength:** {med_details['strength']}")

    else:
        lines.append(f"```json\n{json.dumps(resource, indent=2)}\n```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def get_resource_title(
    resource: dict,
    fhir_base_url: str = None,
    http_client=None,
    medication_cache_fn=None,
) -> str:
    """Extract a human-readable title from a FHIR resource."""
    resource_type = resource.get("resourceType", "Unknown")

    if resource_type == "Patient":
        name = resource.get("name", [{}])[0]
        return f"{name.get('given', [''])[0]} {name.get('family', '')}"
    elif resource_type in ("Condition", "Observation", "Procedure", "DiagnosticReport"):
        code = resource.get("code", {})
        coding = code.get("coding", [{}])[0]
        return coding.get("display", code.get("text", resource_type))
    elif resource_type == "ImagingStudy":
        first_series = resource.get("series", [{}])[0] if isinstance(resource.get("series"), list) else {}
        if not isinstance(first_series, dict):
            return "ImagingStudy"
        modality = first_series.get("modality", {}).get("coding", [{}])[0].get("code")
        body_site = first_series.get("bodySite", {}).get("concept", {}).get("coding", [{}])[0].get("display")
        return " ".join([part for part in [modality, body_site] if part]) or "ImagingStudy"
    elif resource_type == "MedicationStatement":
        med_name, _ = resolve_medication_for_statement(
            resource, fhir_base_url, http_client, medication_cache_fn
        )
        return med_name
    elif resource_type == "Medication":
        code = resource.get("code", {})
        return code.get("coding", [{}])[0].get("display") or code.get("text") or "Medication"

    return resource_type


def get_resource_date(resource: dict) -> str:
    """Extract the primary date from a FHIR resource."""
    date_fields = [
        "effectiveDateTime", "effectivePeriod.start", "onsetDateTime",
        "recordedDate", "performedDateTime", "performedPeriod.start",
        "issued", "authoredOn", "dateAsserted", "started",
    ]
    for field in date_fields:
        if "." in field:
            parent, child = field.split(".")
            if parent in resource and child in resource.get(parent, {}):
                return resource[parent][child]
        elif field in resource:
            return resource[field]
    return None
