"""FHIR client and resource conversion utilities.

Shared by the main API and the MCP tool server.
"""
from fhir.converter import (
    fhir_resource_to_markdown,
    get_resource_title,
    get_resource_date,
    resolve_medication_for_statement,
    _extract_reference_id,
    _extract_first_coding_display,
    _extract_medication_statement_dose_route,
    _extract_medication_reference_id,
    _extract_medication_details,
)
from fhir.client import (
    DirectFHIRClient,
    retry_with_backoff,
)
