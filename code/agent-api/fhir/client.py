"""FHIR HTTP client for direct access."""
import logging
import asyncio
import httpx

logger = logging.getLogger("medgemma-agent")


async def retry_with_backoff(func, max_retries=10, initial_delay=1.0, backoff_factor=1.5, max_delay=30.0):
    """Retry an async function with exponential backoff."""
    delay = initial_delay
    last_exception = None

    for attempt in range(max_retries):
        try:
            return await func()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
            last_exception = e
            if attempt < max_retries - 1:
                logger.debug("FHIR connection attempt %d/%d failed: %s. Retrying in %.1fs...",
                             attempt + 1, max_retries, e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * backoff_factor, max_delay)
            else:
                logger.warning("FHIR connection failed after %d attempts: %s", max_retries, e)
        except Exception:
            raise

    raise last_exception


class DirectFHIRClient:
    """Direct FHIR HTTP client."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=30.0)

    async def search_resources(self, resource_type: str, patient_id: str = None,
                               params: dict = None, retry: bool = False) -> list:
        search_params = params or {}
        if patient_id:
            if resource_type == "Patient":
                search_params["_id"] = patient_id
            else:
                search_params["patient"] = patient_id

        async def _do_search():
            all_entries = []
            url = f"{self.base_url}/{resource_type}"
            current_params = search_params
            while url:
                response = await self.client.get(url, params=current_params)
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

        try:
            if retry:
                return await retry_with_backoff(_do_search)
            else:
                return await _do_search()
        except httpx.HTTPError as e:
            logger.error("Direct FHIR search failed: %s", e)
            return []

    async def get_resource(self, resource_type: str, resource_id: str, retry: bool = False) -> dict:
        async def _do_get():
            response = await self.client.get(f"{self.base_url}/{resource_type}/{resource_id}")
            response.raise_for_status()
            return response.json()

        try:
            if retry:
                return await retry_with_backoff(_do_get)
            else:
                return await _do_get()
        except httpx.HTTPError as e:
            logger.error("Direct FHIR read failed: %s", e)
            return None

    async def get_patient_everything(self, patient_id: str, retry: bool = False) -> dict:
        async def _do_everything():
            response = await self.client.get(f"{self.base_url}/Patient/{patient_id}/$everything")
            response.raise_for_status()
            return response.json()

        try:
            if retry:
                return await retry_with_backoff(_do_everything)
            else:
                return await _do_everything()
        except httpx.HTTPError as e:
            logger.error("Patient $everything failed: %s", e)
            return {"entry": []}

    async def close(self):
        await self.client.aclose()
