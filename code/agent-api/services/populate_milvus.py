"""Auto-populate Milvus collection with guideline chunks on startup.

Runs once at app startup if the target collection is missing or empty.
"""
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from pymilvus import (
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
)

import tiktoken

from config import (
    MILVUS_URI,
    MILVUS_TOKEN,
    NSCLC_COLLECTION,
    SCLC_COLLECTION,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_API_KEY,
)

logger = logging.getLogger("medgemma-agent")

# ---------------------------------------------------------------------------
# Tiktoken encoder
# ---------------------------------------------------------------------------
try:
    _enc = tiktoken.get_encoding("cl100k_base")
except Exception:
    class _FallbackEncoding:
        def encode(self, text: str) -> list:
            return text.split()
    _enc = _FallbackEncoding()


def _token_len(text: str) -> int:
    return len(_enc.encode(text))


# ---------------------------------------------------------------------------
# Embedding URL — derive Ollama /api/embed from the OpenAI-compat base URL
# (same pattern as milvus_tools.py)
# ---------------------------------------------------------------------------
def _get_ollama_embed_url() -> str:
    base = EMBEDDING_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/api/embed"


# ---------------------------------------------------------------------------
# Chunk dataclass
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    content: str
    chunk_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Chunking pipeline
# ---------------------------------------------------------------------------
def _split_by_headers(content: str, max_level: int = 6) -> List[str]:
    header_re = re.compile(
        r"^(#{1," + str(max_level) + r"})\s+(.+)$", re.MULTILINE
    )
    matches = list(header_re.finditer(content))
    if not matches:
        return [content]

    sections: List[str] = []
    pre = content[: matches[0].start()].strip()
    if pre:
        sections.append(pre)

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        section = content[m.start(): end].strip()
        if section:
            sections.append(section)

    return sections


def _merge_short_sections(sections: List[str], min_tokens: int = 800) -> List[str]:
    merged: List[str] = []
    buf: Optional[str] = None

    for sec in sections:
        if buf is None:
            buf = sec
            continue
        if _token_len(buf) < min_tokens:
            buf = buf.rstrip() + "\n\n" + sec.lstrip()
        else:
            merged.append(buf)
            buf = sec

    if buf is not None:
        merged.append(buf)

    return merged


_SEPARATOR_RE = re.compile(r"^\s*\|[-:\s|]+\|\s*$")


def _split_with_overlap(
    text: str,
    chunk_size: int = 400,
    overlap: int = 50,
    keep_table_header: bool = True,
) -> List[str]:
    # Pre-split lines that exceed chunk_size so a single long paragraph never
    # forces a break immediately after a header.
    raw_lines = text.split("\n")
    lines: List[str] = []
    for raw in raw_lines:
        if _token_len(raw) > chunk_size:
            words = raw.split(" ")
            sub: List[str] = []
            sub_tokens = 0
            for word in words:
                wt = _token_len(word)
                if sub_tokens + wt > chunk_size and sub:
                    lines.append(" ".join(sub))
                    sub = [word]
                    sub_tokens = wt
                else:
                    sub.append(word)
                    sub_tokens += wt
            if sub:
                lines.append(" ".join(sub))
        else:
            lines.append(raw)

    chunks: List[str] = []

    table_header_lines: Optional[List[str]] = None
    current_lines: List[str] = []
    current_tokens = 0

    for idx, line in enumerate(lines):
        line_tokens = _token_len(line)

        if (
            line.lstrip().startswith("|")
            and idx + 1 < len(lines)
            and _SEPARATOR_RE.match(lines[idx + 1])
        ):
            table_header_lines = [line.lstrip(), lines[idx + 1].lstrip()]
        elif not line.lstrip().startswith("|"):
            table_header_lines = None

        if current_tokens + line_tokens > chunk_size and current_lines:
            chunks.append("\n".join(current_lines).strip())

            if overlap > 0:
                overlap_lines: List[str] = []
                overlap_tokens = 0
                for prev in reversed(current_lines):
                    pt = _token_len(prev)
                    if overlap_tokens + pt <= overlap:
                        overlap_lines.insert(0, prev)
                        overlap_tokens += pt
                    else:
                        break

                if keep_table_header and table_header_lines:
                    for hdr in reversed(table_header_lines):
                        if hdr not in overlap_lines:
                            overlap_lines.insert(0, hdr)
                            overlap_tokens += _token_len(hdr)

                current_lines = list(overlap_lines)
                current_tokens = overlap_tokens
            else:
                current_lines = []
                current_tokens = 0

        current_lines.append(line)
        current_tokens += line_tokens

    if current_lines:
        text_chunk = "\n".join(current_lines).strip()
        if text_chunk:
            chunks.append(text_chunk)

    return chunks


def _chunk_document(content: str, document_id: str, document_name: str) -> List[Chunk]:
    sections = _split_by_headers(content)
    merged = _merge_short_sections(sections, min_tokens=800)

    all_chunks: List[Chunk] = []
    idx = 0
    for section in merged:
        if _token_len(section) <= 800:
            all_chunks.append(
                Chunk(
                    content=section,
                    metadata={
                        "document_id": document_id,
                        "document_name": document_name,
                        "chunk_index": idx,
                        "chunking_technique": "markdown_header+length",
                        "embedding_model": EMBEDDING_MODEL,
                    },
                )
            )
            idx += 1
        else:
            sub_chunks = _split_with_overlap(
                section, chunk_size=800, overlap=100, keep_table_header=True
            )
            for sc in sub_chunks:
                all_chunks.append(
                    Chunk(
                        content=sc,
                        metadata={
                            "document_id": document_id,
                            "document_name": document_name,
                            "chunk_index": idx,
                            "chunking_technique": "markdown_header+length",
                            "embedding_model": EMBEDDING_MODEL,
                        },
                    )
                )
                idx += 1

    return all_chunks


# ---------------------------------------------------------------------------
# Embedding (sync httpx — runs during startup, not in async context)
# ---------------------------------------------------------------------------
def _embed_texts(texts: List[str]) -> List[List[float]]:
    url = _get_ollama_embed_url()
    headers = {"Content-Type": "application/json"}
    if EMBEDDING_API_KEY and EMBEDDING_API_KEY != "none":
        headers["Authorization"] = f"Bearer {EMBEDDING_API_KEY}"

    all_embeddings: List[List[float]] = []
    batch_size = 32

    with httpx.Client(timeout=300.0) as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i: i + batch_size]
            logger.info("  Embedding batch %d/%d (%d texts)...",
                        i // batch_size + 1,
                        (len(texts) + batch_size - 1) // batch_size,
                        len(batch))
            resp = client.post(
                url,
                headers=headers,
                json={"model": EMBEDDING_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            all_embeddings.extend(data["embeddings"])

    return all_embeddings


# ---------------------------------------------------------------------------
# Collection creation
# ---------------------------------------------------------------------------
def _create_collection(client: MilvusClient, name: str, dim: int) -> None:
    fields = [
        FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
        FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=64000),
        FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="document_name", dtype=DataType.VARCHAR, max_length=512),
        FieldSchema(name="chunk_index", dtype=DataType.INT64),
        FieldSchema(name="chunking_technique", dtype=DataType.VARCHAR, max_length=64),
        FieldSchema(name="embedding_model", dtype=DataType.VARCHAR, max_length=128),
        FieldSchema(name="metadata_json", dtype=DataType.VARCHAR, max_length=64000),
        FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=dim),
    ]

    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="embedding",
        metric_type="COSINE",
        index_type="AUTOINDEX",
        index_name="embedding_index",
    )

    schema = CollectionSchema(fields=fields, description=f"NSCLC guideline chunks ({EMBEDDING_MODEL})")
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    logger.info("Created Milvus collection '%s' (dim=%d).", name, dim)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def populate_milvus_if_needed(guideline_path: str, collection_name: str = None) -> None:
    """Check if the Milvus collection exists and has data; populate if not.

    Args:
        guideline_path: Path to the guideline markdown file.
        collection_name: Target Milvus collection name. Defaults to NSCLC_COLLECTION.
    """
    if collection_name is None:
        collection_name = NSCLC_COLLECTION

    path = Path(guideline_path)
    if not path.exists():
        logger.warning("Guideline file not found at %s — skipping Milvus population.", guideline_path)
        return

    client = MilvusClient(uri=MILVUS_URI, token=MILVUS_TOKEN, db_name="default")

    # Check if collection already has data
    if client.has_collection(collection_name):
        stats = client.get_collection_stats(collection_name)
        row_count = stats.get("row_count", 0)
        if row_count > 0:
            logger.info("Milvus collection '%s' already has %s rows — skipping population.",
                        collection_name, row_count)
            return
        logger.info("Milvus collection '%s' exists but is empty — repopulating.", collection_name)
        client.drop_collection(collection_name)

    # 1. Read guideline
    logger.info("Reading guideline from %s ...", guideline_path)
    content = path.read_text(encoding="utf-8")
    document_id = hashlib.sha256(content.encode()).hexdigest()[:16]
    document_name = path.name

    # 2. Chunk
    chunks = _chunk_document(content, document_id, document_name)
    logger.info("Created %d chunks from guideline.", len(chunks))

    # 3. Embed
    logger.info("Embedding %d chunks with %s (url: %s) ...",
                len(chunks), EMBEDDING_MODEL, _get_ollama_embed_url())
    texts = [c.content for c in chunks]
    embeddings = _embed_texts(texts)
    actual_dim = len(embeddings[0])
    logger.info("Embedding dimension: %d", actual_dim)

    # 4. Create collection
    _create_collection(client, collection_name, dim=actual_dim)

    # 5. Insert
    rows = []
    for i, chunk in enumerate(chunks):
        meta = {k: v for k, v in chunk.metadata.items()
                if k not in ("document_id", "document_name", "chunk_index",
                             "chunking_technique", "embedding_model")}
        rows.append({
            "chunk_id": chunk.chunk_id,
            "content": chunk.content[:64000],
            "document_id": chunk.metadata.get("document_id", document_id),
            "document_name": chunk.metadata.get("document_name", document_name)[:500],
            "chunk_index": chunk.metadata.get("chunk_index", i),
            "chunking_technique": str(chunk.metadata.get("chunking_technique", ""))[:60],
            "embedding_model": EMBEDDING_MODEL[:120],
            "metadata_json": json.dumps(meta)[:64000],
            "embedding": embeddings[i],
        })

    batch_size = 500
    for i in range(0, len(rows), batch_size):
        client.insert(collection_name=collection_name, data=rows[i: i + batch_size])
        logger.info("  Inserted batch %d (%d/%d)",
                     i // batch_size + 1, min(i + batch_size, len(rows)), len(rows))

    stats = client.get_collection_stats(collection_name)
    logger.info("Milvus population complete. Collection '%s' has %s rows.",
                collection_name, stats.get("row_count", "?"))
