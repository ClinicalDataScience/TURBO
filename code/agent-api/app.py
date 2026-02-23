"""FastAPI app with smolagents and MCP for LLM-powered medical queries."""
import logging
import os
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, Request, Response

from config import (
    DEFAULT_CLINICAL_QUESTION, NSCLC_COLLECTION, SCLC_COLLECTION,
    FHIR_PATIENT_IDS, GUIDELINE_PATH, SCLC_GUIDELINE_PATH,
)
from database import init_db
from services.fhir_sync import init_fhir_clients, close_fhir_clients, fetch_and_register_fhir_resources
from services.documents import fetch_documents_from_milvus, store_guidelines_as_sources
from services.populate_milvus import populate_milvus_if_needed
from services.keypoints import run_keypoint_extraction_from_sources
from agent.setup import initialize_agent, shutdown_agent

from routes import health, sources, patients, query, legacy

# Configure detailed logging
LOG_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "agent-api.log")

log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.basicConfig(
    level=logging.DEBUG,
    format=log_format,
)

if os.getenv("LOG_TO_FILE", "true").lower() in ("true", "1", "yes"):
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("medgemma-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage MCP connection and FHIR client lifecycle."""
    # Initialize database
    init_db()

    # Initialize FHIR clients (async for backend operations, sync for agent tools)
    init_fhir_clients()

    # Initialize MCP tool collection and agent
    initialize_agent()

    # Auto-populate Milvus if collections are missing or empty
    try:
        populate_milvus_if_needed(GUIDELINE_PATH, NSCLC_COLLECTION)
    except Exception as e:
        logger.exception("Milvus NSCLC auto-population failed: %s", e)

    try:
        populate_milvus_if_needed(SCLC_GUIDELINE_PATH, SCLC_COLLECTION)
    except Exception as e:
        logger.exception("Milvus SCLC auto-population failed: %s", e)

    # Startup: populate guideline list
    try:
        logger.info("Fetching guidelines from Milvus collection '%s'...", NSCLC_COLLECTION)
        docs = fetch_documents_from_milvus()
        store_guidelines_as_sources(docs)
        logger.info("Registered %d guideline documents in sources.", len(docs))

        if DEFAULT_CLINICAL_QUESTION:
            logger.info("Running keypoint extraction with clinical question: %s", DEFAULT_CLINICAL_QUESTION)
            run_keypoint_extraction_from_sources(DEFAULT_CLINICAL_QUESTION)
    except Exception as e:
        logger.exception("Startup document population failed: %s", e)

    # Pre-fetch FHIR patient data so the patient list is available immediately
    if FHIR_PATIENT_IDS:
        logger.info("Pre-fetching FHIR data for %d configured patients...", len(FHIR_PATIENT_IDS))
        for pid in FHIR_PATIENT_IDS:
            try:
                await fetch_and_register_fhir_resources(pid, retry=True)
                logger.info("Pre-fetched FHIR data for patient %s", pid)
            except Exception as e:
                logger.warning("Failed to pre-fetch FHIR data for patient %s: %s", pid, e)
        logger.info("FHIR patient pre-fetch complete.")

    yield

    # Shutdown
    shutdown_agent()
    await close_fhir_clients()


app = FastAPI(
    title="MedGemma Medical Dashboard API",
    version="1.0.0",
    lifespan=lifespan,
)


# CORS middleware (runs before routing)
@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    # Handle preflight OPTIONS request
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": request.headers.get("origin", "*"),
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS, PATCH",
                "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "*"),
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Max-Age": "86400",
            }
        )

    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = request.headers.get("origin", "*")
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


# Register route modules
app.include_router(health.router)
app.include_router(sources.router)
app.include_router(patients.router)
app.include_router(query.router)
app.include_router(legacy.router)

# Initialize database on module load
init_db()
