"""Health and diagnostics endpoints."""
import logging

from fastapi import APIRouter

from services.documents import _get_milvus

logger = logging.getLogger("medgemma-agent")

router = APIRouter(tags=["health"])


@router.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "1.0.0"}


@router.get("/collections")
def collections():
    """List all Milvus collections."""
    return {"collections": _get_milvus().list_collections()}
