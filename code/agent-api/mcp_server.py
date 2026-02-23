"""MCP server exposing Milvus and medical tools via Model Context Protocol."""
import os
import httpx
from fastmcp import FastMCP
from milvus_tools import MilvusTools
from fhir.converter import fhir_resource_to_markdown, get_resource_title, get_resource_date

# Initialize FastMCP server
mcp = FastMCP("MedGemma Agent Tools")

# Initialize Milvus tools with configuration from environment
tools = MilvusTools(
    milvus_uri=os.getenv("MILVUS_URI", "http://milvus-standalone:19530"),
    milvus_token=os.environ["MILVUS_TOKEN"],
    embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
    embedding_model=os.getenv("EMBEDDING_MODEL", ""),
    embedding_api_key=os.getenv("EMBEDDING_API_KEY", "none"),
)

NSCLC_COLLECTION = os.getenv("NSCLC_COLLECTION", "nsclc_guideline__markdown_header_length__qwen3_embedding_4b_fp16")
SCLC_COLLECTION = os.getenv("SCLC_COLLECTION", "sclc_guideline__markdown_header_length__qwen3_embedding_4b_fp16")

# FHIR configuration
FHIR_BASE_URL = os.getenv("FHIR_BASE_URL", "http://hapi-r4:8080/hapi-fhir-jpaserver/fhir")
fhir_client = httpx.Client(timeout=30.0)

_GUIDELINE_COLLECTIONS = {
    "nsclc": [NSCLC_COLLECTION],
    "sclc": [SCLC_COLLECTION],
    "both": [NSCLC_COLLECTION, SCLC_COLLECTION],
}


# ============ Guideline Search Tools ============

@mcp.tool()
def search_guidelines(query: str, cancer_type: str = "nsclc", top_k: int = 5) -> str:
    """Search medical guidelines using semantic similarity.

    This is the PRIMARY tool for finding relevant medical guidelines. It converts
    the query text into an embedding vector and finds the most semantically similar
    guideline documents using cosine similarity.

    Use this for any search involving:
    - Treatment recommendations
    - Clinical protocols
    - Disease-specific guidelines
    - Drug interactions and dosing
    - Staging and classification criteria

    Args:
        query: Natural language query describing the guideline information needed.
        cancer_type: Which guideline collection to search. One of: "nsclc", "sclc", or "both".
            Use the value specified in the patient's cancer type context. Defaults to "nsclc".
        top_k: Maximum number of results to return (default 5, max 10 to prevent context overflow).

    Returns:
        Search results with matching guideline chunks including document_id, content,
        and relevance scores.
    """
    top_k = min(top_k, 10)
    collections = _GUIDELINE_COLLECTIONS.get(cancer_type.lower(), [NSCLC_COLLECTION])
    if len(collections) == 1:
        return tools.search_collection(collections[0], query, top_k)
    parts = []
    for col in collections:
        label = "NSCLC" if col == NSCLC_COLLECTION else "SCLC"
        parts.append(f"--- {label} Guidelines ---\n{tools.search_collection(col, query, top_k)}")
    return "\n\n".join(parts)


# ============ FHIR Helper Functions ============

def _search_fhir(resource_type: str, params: dict) -> list:
    """Search FHIR resources with pagination."""
    fhir_url = FHIR_BASE_URL.rstrip("/")
    all_entries = []
    url = f"{fhir_url}/{resource_type}"
    current_params = params
    while url:
        response = fhir_client.get(url, params=current_params)
        response.raise_for_status()
        bundle = response.json()
        all_entries.extend(bundle.get("entry", []))
        next_url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                next_url = link.get("url")
                break
        url = next_url
        current_params = None
    return all_entries


# ============ FHIR Tools ============

ALL_FHIR_TYPES = ["Patient", "Condition", "DiagnosticReport", "ImagingStudy",
                  "Observation", "Procedure", "Medication", "MedicationStatement"]


@mcp.tool()
def get_patient_data(patient_id: str) -> str:
    """Fetch ALL available clinical data for a patient from the FHIR server.

    Always returns the full set of resources: demographics, conditions,
    diagnostic reports, imaging studies, observations, procedures, and medications.
    To fetch a single specific resource, use get_fhir_resource instead.

    Args:
        patient_id: The patient ID to look up data for.
    """
    resource_types = ALL_FHIR_TYPES
    fhir_url = FHIR_BASE_URL.rstrip("/")
    lines = []
    total = 0

    for rt in resource_types:
        try:
            if rt == "Patient":
                resp = fhir_client.get(f"{fhir_url}/Patient/{patient_id}")
                entries = [{"resource": resp.json()}] if resp.status_code == 200 else []
            else:
                entries = _search_fhir(rt, {"patient": patient_id})

            for entry in entries:
                resource = entry.get("resource", entry)
                fhir_id = resource.get("id", "unknown")
                total += 1
                # Use shared converter with FHIR HTTP client for medication resolution
                markdown = fhir_resource_to_markdown(
                    resource, fhir_base_url=FHIR_BASE_URL, http_client=fhir_client
                )
                title = get_resource_title(
                    resource, fhir_base_url=FHIR_BASE_URL, http_client=fhir_client
                )
                dt = get_resource_date(resource) or "N/A"
                header = f"- [{rt}] {title} (date: {dt}) | fhir_id: {fhir_id}"
                header += f"\n  {markdown}"
                lines.append(header)
        except Exception as e:
            lines.append(f"- [{rt}] Error fetching: {e}")

    if not lines:
        return "No data found for this patient on the FHIR server."

    return f"Found {total} resources:\n" + "\n".join(lines)


@mcp.tool()
def get_fhir_resource(resource_type: str, fhir_id: str) -> str:
    """Get the full content of a specific FHIR resource by its type and fhir_id.

    Use this after get_patient_data to read the full text of a truncated resource
    (e.g. a long DiagnosticReport).

    Args:
        resource_type: REQUIRED. The FHIR resource type (e.g. "DiagnosticReport", "Observation").
            Must be a valid non-empty string. NEVER pass null or None.
        fhir_id: REQUIRED. The FHIR resource ID to retrieve. Must be a valid non-empty string.
            NEVER pass null or None.
    """
    # Validate parameters
    if not resource_type or resource_type.lower() in ("none", "null"):
        return "Error: resource_type is required and must be a valid FHIR type (e.g. 'DiagnosticReport', 'Observation'). Do not pass null."
    if not fhir_id or fhir_id.lower() in ("none", "null"):
        return "Error: fhir_id is required and must be a valid resource ID. Do not pass null."
    fhir_url = FHIR_BASE_URL.rstrip("/")
    try:
        response = fhir_client.get(f"{fhir_url}/{resource_type}/{fhir_id}")
        response.raise_for_status()
        resource = response.json()
        markdown = fhir_resource_to_markdown(
            resource, fhir_base_url=FHIR_BASE_URL, http_client=fhir_client
        )
        title = get_resource_title(
            resource, fhir_base_url=FHIR_BASE_URL, http_client=fhir_client
        )
        return f"[{resource_type}] {title} (fhir_id: {fhir_id})\n\n{markdown}"
    except httpx.HTTPStatusError as e:
        return f"Resource not found: {resource_type}/{fhir_id} (HTTP {e.response.status_code})"
    except Exception as e:
        return f"Error fetching resource: {e}"


if __name__ == "__main__":
    mcp.run()
