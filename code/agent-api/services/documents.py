"""Milvus guideline document fetching and source registration."""
import json
import logging
from collections import OrderedDict
from pymilvus import MilvusClient

from config import MILVUS_URI, MILVUS_TOKEN, NSCLC_COLLECTION
from database import register_source

logger = logging.getLogger("medgemma-agent")

# Lazy Milvus client — initialized on first use to avoid crash if Milvus isn't ready at import time
_milvus: MilvusClient | None = None


def _get_milvus() -> MilvusClient:
    global _milvus
    if _milvus is None:
        _milvus = MilvusClient(uri=MILVUS_URI, token=MILVUS_TOKEN, db_name="default")
    return _milvus


def fetch_documents_from_milvus() -> list[dict]:
    """Fetch all chunks from Milvus and group them by document_id into documents."""
    milvus = _get_milvus()
    schema = milvus.describe_collection(NSCLC_COLLECTION)
    pk_field = None
    field_names = []
    for field in schema.get("fields", []):
        field_names.append(field["name"])
        if field.get("is_primary", False) or field.get("auto_id", False):
            pk_field = field["name"]

    output_fields = [f for f in field_names if f not in ("vector", "embedding", "dense_vector")]
    logger.info("Collection schema — pk: %s, output_fields: %s", pk_field, output_fields)

    pk_type = None
    for field in schema.get("fields", []):
        if field["name"] == pk_field:
            pk_type = field.get("type")
            break

    is_int_pk = str(pk_type) in ("5", "DataType.INT64", "INT64", "Int64")
    if pk_field and is_int_pk:
        filter_expr = f"{pk_field} >= 0"
    elif pk_field:
        filter_expr = f'{pk_field} != ""'
    else:
        filter_expr = ""
    logger.info("Using filter: %s", filter_expr)

    chunks = milvus.query(
        collection_name=NSCLC_COLLECTION,
        filter=filter_expr,
        limit=16384,
        output_fields=output_fields,
    )
    logger.info("Fetched %d chunks from Milvus.", len(chunks))

    doc_map: dict[str, dict] = OrderedDict()
    for chunk in chunks:
        doc_id = str(chunk.get("document_id", ""))
        if doc_id not in doc_map:
            doc_map[doc_id] = {
                "document_id": doc_id,
                "document_name": chunk.get("document_name", ""),
                "chunks": [],
                "metadata": {
                    "chunking_technique": chunk.get("chunking_technique", ""),
                    "embedding_model": chunk.get("embedding_model", ""),
                },
            }
        doc_map[doc_id]["chunks"].append({
            "chunk_index": chunk.get("chunk_index", 0),
            "content": chunk.get("content", ""),
            "metadata_json": chunk.get("metadata_json", ""),
        })

    documents = []
    for doc in doc_map.values():
        doc["chunks"].sort(key=lambda c: c["chunk_index"])
        doc["text"] = "\n\n".join(c["content"] for c in doc["chunks"] if c["content"])
        chunk_metadata = []
        for c in doc["chunks"]:
            try:
                chunk_metadata.append(json.loads(c["metadata_json"]) if c["metadata_json"] else {})
            except (json.JSONDecodeError, TypeError):
                pass
        if chunk_metadata:
            doc["metadata"]["chunk_metadata"] = chunk_metadata
        del doc["chunks"]
        documents.append(doc)

    logger.info("Grouped into %d documents.", len(documents))
    return documents


def store_guidelines_as_sources(documents: list[dict]):
    """Register guideline documents in the sources table."""
    for doc in documents:
        register_source(
            source_type="milvus",
            resource_type="guideline",
            content=doc.get("text", ""),
            title=doc.get("document_name", ""),
            milvus_document_id=doc.get("document_id", ""),
            metadata=doc.get("metadata", {}),
        )
