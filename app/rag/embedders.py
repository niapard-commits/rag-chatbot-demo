"""Embedding models for meaning-based (dense) search.

An embedder is any object with  `dim: int`  and  `embed(texts: list[str]) -> list[list[float]]`.
Two are included:

  FastEmbedEmbedder   runs locally on the CPU (ONNX), no API key. Downloads the model on first use (about 130 MB).
  OpenAIEmbedder      calls the OpenAI embeddings API (needs OPENAI_API_KEY).

Both import their library lazily, so the project runs without them when EMBEDDER is not set.
"""


class FastEmbedEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5", cache_dir: str | None = None):
        from fastembed import TextEmbedding
        self.model_name = model
        self._model = TextEmbedding(model, cache_dir=cache_dir)
        self.dim = len(next(iter(self._model.embed(["dimension probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]


class OpenAIEmbedder:
    DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072, "text-embedding-ada-002": 1536}

    def __init__(self, api_key: str, model: str = "text-embedding-3-small"):
        from openai import OpenAI
        self.model_name, self._client = model, OpenAI(api_key=api_key, timeout=15)
        self.dim = self.DIMS.get(model) or len(self.embed(["dimension probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(model=self.model_name, input=texts)
        return [item.embedding for item in response.data]


def build_embedder(settings):
    """None when EMBEDDER is empty (sparse BM25 search only)."""
    kind = settings.embedder
    if not kind or kind == "none":
        return None
    if kind == "fastembed":
        return FastEmbedEmbedder(settings.embedding_model or "BAAI/bge-small-en-v1.5")
    if kind == "openai":
        if not settings.openai_api_key:
            raise ValueError("EMBEDDER=openai needs OPENAI_API_KEY")
        return OpenAIEmbedder(settings.openai_api_key, settings.embedding_model or "text-embedding-3-small")
    raise ValueError(f"unknown EMBEDDER '{kind}' (use fastembed, openai or leave empty)")
