"""Legacy endpoints for backwards compatibility."""
import json
import logging

from fastapi import APIRouter, HTTPException

from database import get_db_connection
from models import DocumentItem

logger = logging.getLogger("medgemma-agent")

router = APIRouter(tags=["legacy"])


@router.get("/legacy/get_list", response_model=list[DocumentItem])
def legacy_get_list():
    """Return all documents with metadata (legacy format)."""
    try:
        conn = get_db_connection()
        rows = conn.execute("""
            SELECT d.document_id, d.document_name, d.text, d.metadata,
                   k.keypoints, k.fragestellung
            FROM documents d
            LEFT JOIN keypoints k ON d.document_id = k.source_id
        """).fetchall()
        conn.close()

        if rows:
            return [
                DocumentItem(
                    document_id=r[0],
                    document_name=r[1],
                    text=r[2],
                    metadata=json.loads(r[3]) if r[3] else {},
                    keypoints=json.loads(r[4]) if r[4] else None,
                    fragestellung=r[6],
                )
                for r in rows
            ]
        return []
    except Exception as e:
        logger.exception("legacy get_list failed")
        raise HTTPException(status_code=500, detail=str(e))
