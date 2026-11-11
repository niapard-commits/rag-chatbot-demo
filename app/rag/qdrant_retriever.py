"""Retriever that stores the knowledge base in Qdrant and searches it there.

Two kinds of vectors, both stored in one collection:
  sparse "bm25"   a BM25 weight for every word of a section: exact words, product names, numbers.
  dense  "dense"  an embedding of the section (only when an embedder is given): finds meaning, synonyms,
                  other wording ("whitening" ~ "bleaching", "how much" ~ "price").
Without an embedder only the sparse vector exists and the ranking equals BM25Retriever.

With an embedder the two result lists are merged by Reciprocal Rank Fusion (hybrid search). The dense list keeps
only sections whose cosine score is at least `dense_min_score`, so an off-topic question still finds nothing and
the bot says it does not know instead of guessing. The right threshold depends on the embedding model: tune it
with scripts/eval_retrieval.py. If the embedder fails at search time (API down) the sparse results are used.

It has the same two methods as BM25Retriever (reindex, search), so the agent does not know the difference.

Where Qdrant runs (first match wins):
  QDRANT_URL        a Qdrant server or Qdrant Cloud, e.g. http://localhost:6333  (+ QDRANT_API_KEY)
  QDRANT_PATH       a folder: embedded local mode, data kept on disk
  neither           embedded local mode in memory (nothing is saved, the index is rebuilt at start)
"""
import logging
import math
from collections import Counter
from pathlib import Path

from .loader import Chunk, load_knowledge
from .retriever import tokenize

SPARSE = "bm25"
DENSE = "dense"
RRF_K = 60

log = logging.getLogger("chatbot.qdrant")


class QdrantRetriever:
    def __init__(self, knowledge_dir: Path, url: str = "", api_key: str = "", path: str = "",
                 collection: str = "knowledge", k1: float = 1.5, b: float = 0.75,
                 embedder=None, dense_min_score: float = 0.6):
        from qdrant_client import QdrantClient  # lazy: the project runs without it when BM25 is used
        if url:
            self.client = QdrantClient(url=url, api_key=api_key or None, timeout=10)
        elif path:
            self.client = QdrantClient(path=path)
        else:
            self.client = QdrantClient(":memory:")
        self.knowledge_dir, self.collection, self.k1, self.b = Path(knowledge_dir), collection, k1, b
        self.embedder, self.dense_min_score = embedder, dense_min_score
        self.chunks: list[Chunk] = []
        self._vocab: dict[str, int] = {}
        self.reindex()

    def reindex(self) -> int:
        """Re-read the knowledge folder and rebuild the Qdrant collection."""
        from qdrant_client import models
        chunks = load_knowledge(self.knowledge_dir)
        tfs = [Counter(tokenize(c.title + " " + c.title + " " + c.text)) for c in chunks]
        df: Counter = Counter()
        for tf in tfs:
            df.update(tf.keys())
        n = len(chunks)
        avgdl = (sum(sum(tf.values()) for tf in tfs) / n) if n else 0.0
        vocab = {term: i for i, term in enumerate(sorted(df))}

        dense = self.embedder.embed([f"{c.title}\n{c.text}" for c in chunks]) if self.embedder and chunks else []
        points = []
        for i, (chunk, tf) in enumerate(zip(chunks, tfs)):
            dl = sum(tf.values()) or 1
            weights = {}
            for term, f in tf.items():
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                weights[vocab[term]] = idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / avgdl))
            vector = {SPARSE: models.SparseVector(indices=list(weights), values=list(weights.values()))}
            if dense:
                vector[DENSE] = dense[i]
            points.append(models.PointStruct(
                id=i, vector=vector,
                payload={"source": chunk.source, "title": chunk.title, "text": chunk.text, "terms": sorted(tf)}))

        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        vectors = {DENSE: models.VectorParams(size=self.embedder.dim, distance=models.Distance.COSINE)} \
            if self.embedder else {}
        self.client.create_collection(self.collection, vectors_config=vectors,
                                      sparse_vectors_config={SPARSE: models.SparseVectorParams()})
        if points:
            self.client.upsert(self.collection, points=points, wait=True)
        self.chunks, self._vocab = chunks, vocab
        return len(chunks)

    def search(self, query: str, k: int = 3, min_score: float = 0.1) -> list[Chunk]:
        if not self.chunks:
            return []
        sparse = self._search_sparse(query, k, min_score)
        dense = self._search_dense(query, k) if self.embedder else []
        if not dense:
            return [self.chunks[i] for i in sparse[:k]]
        # Reciprocal Rank Fusion: a section ranked high in either list (or in both) comes first.
        scores: dict[int, float] = {}
        for ranking in (sparse, dense):
            for rank, idx in enumerate(ranking):
                scores[idx] = scores.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
        return [self.chunks[i] for i in sorted(scores, key=scores.get, reverse=True)[:k]]

    def _search_sparse(self, query: str, k: int, min_score: float) -> list[int]:
        from qdrant_client import models
        terms = set(tokenize(query))
        known = sorted(self._vocab[t] for t in terms if t in self._vocab)
        if not known:
            return []
        # The sparse query vector has weight 1 for every query word, so the score is the BM25 score.
        result = self.client.query_points(
            self.collection, query=models.SparseVector(indices=known, values=[1.0] * len(known)),
            using=SPARSE, limit=max(k * 3, 10), with_payload=True)
        # Same rule as BM25Retriever: a longer question must match at least two different words.
        needed = 1 if len(terms) <= 2 else 2
        return [p.id for p in result.points
                if p.score >= min_score and len(terms & set(p.payload["terms"])) >= needed][:k]

    def _search_dense(self, query: str, k: int) -> list[int]:
        try:
            vector = self.embedder.embed([query])[0]
            result = self.client.query_points(self.collection, query=vector, using=DENSE, limit=max(k * 3, 10))
        except Exception as exc:  # embedding service down: keep answering with the word search only
            log.warning("Dense search failed, using the word search only: %s", exc)
            return []
        return [p.id for p in result.points if p.score >= self.dense_min_score][:k]
