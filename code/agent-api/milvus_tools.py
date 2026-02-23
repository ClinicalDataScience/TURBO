"""Milvus database tools for vector search and queries."""
import httpx
from pymilvus import MilvusClient


class MilvusTools:
    """Tools for interacting with Milvus vector database."""

    def __init__(self, milvus_uri: str, milvus_token: str,
                 embedding_base_url: str, embedding_model: str,
                 embedding_api_key: str = "none"):
        """Initialize Milvus tools with database and embedding configuration.

        Args:
            milvus_uri: URI for Milvus connection.
            milvus_token: Authentication token for Milvus.
            embedding_base_url: Base URL for embedding API (OpenAI-compat ``/v1``).
            embedding_model: Name of the embedding model to use.
            embedding_api_key: API key for the embedding endpoint.
        """
        self.milvus = MilvusClient(uri=milvus_uri, token=milvus_token, db_name="default")
        self.embedding_model = embedding_model
        self.embedding_api_key = embedding_api_key

        # Derive Ollama native base from the OpenAI-compat URL
        # e.g. "https://host/ollama/v1" → "https://host/ollama"
        base = embedding_base_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self._ollama_embed_url = f"{base}/api/embed"
        self._http = httpx.Client(timeout=120.0)

    def list_collections(self) -> str:
        """List all collections in the Milvus database."""
        collections = self.milvus.list_collections()
        return f"Collections: {collections}"

    def get_collection_info(self, collection_name: str) -> str:
        """Get schema and stats for a Milvus collection.

        Args:
            collection_name: Name of the collection to inspect.
        """
        try:
            schema = self.milvus.describe_collection(collection_name)
            stats = self.milvus.get_collection_stats(collection_name)
            return f"Schema: {schema}\nStats: {stats}"
        except Exception as e:
            return f"Error: {e}"

    def search_collection(self, collection_name: str, query_text: str, limit: int = 5) -> str:
        """Search a Milvus collection using semantic similarity (cosine similarity).

        This is the PRIMARY tool for finding relevant documents. It converts the query
        text into an embedding vector and finds the most semantically similar documents
        using cosine similarity. Use this for any search involving names, concepts,
        topics, or when looking for related content.

        Args:
            collection_name: Name of the collection to search.
            query_text: Text query to search for (will be embedded and matched semantically).
            limit: Maximum number of results to return.
        """
        try:
            resp = self._http.post(
                self._ollama_embed_url,
                json={"model": self.embedding_model, "input": query_text},
                headers={"Authorization": f"Bearer {self.embedding_api_key}"},
            )
            resp.raise_for_status()
            embedding = resp.json()["embeddings"][0]

            results = self.milvus.search(
                collection_name=collection_name,
                data=[embedding],
                limit=limit,
                output_fields=["content", "metadata_json", "document_name", "chunk_index"],
                search_params={"metric_type": "COSINE"}
            )
            # Format results for readability
            formatted = []
            for hits in results:
                for hit in hits:
                    entity = hit.get("entity", {})
                    formatted.append({
                        "id": hit.get("id"),
                        "content": entity.get("content"),
                        "metadata_json": entity.get("metadata_json"),
                        "document_name": entity.get("document_name"),
                        "chunk_index": entity.get("chunk_index"),
                        "score": hit.get("distance")
                    })
            return f"Results (cosine similarity): {formatted}"
        except Exception as e:
            return f"Error: {e}"

    def query_collection(self, collection_name: str, filter_expr: str, limit: int = 10) -> str:
        """Query a Milvus collection with an exact filter expression.

        Use this ONLY for exact field matching or numeric comparisons, NOT for
        searching text content. For finding documents by content or meaning,
        use search_collection instead.

        Args:
            collection_name: Name of the collection to query.
            filter_expr: Filter expression for exact matches (e.g., 'chunk_index > 10').
            limit: Maximum number of results to return.
        """
        try:
            results = self.milvus.query(
                collection_name=collection_name,
                filter=filter_expr,
                limit=limit,
                output_fields=["content", "metadata_json", "document_name", "chunk_index"]
            )
            return f"Results: {results}"
        except Exception as e:
            return f"Error: {e}"
