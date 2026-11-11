"""Small BM25 retriever. No model download and no API key are needed.

The Retriever interface (reindex / search) is deliberately tiny: to use a vector
database such as Qdrant or Pinecone, write a class with the same two methods.
"""
import logging
import math
import re
import time
from collections import Counter
from pathlib import Path

from .loader import Chunk, load_knowledge

log = logging.getLogger("chatbot.retriever")

_TOKEN = re.compile(r"[\w']+", re.UNICODE)
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "do", "does",
    "you", "your", "we", "i", "it", "at", "with", "how", "what", "can", "be", "my", "me", "have",
    "when", "where", "who", "which", "will", "would", "there", "this", "that", "please", "could",
}


def tokenize(text: str) -> list[str]:
    tokens = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP or len(t) < 2:
            continue
        # very light stemming so "whitening" matches "whiten" and "prices" matches "price"
        for suffix in ("ing", "es", "s"):
            if t.endswith(suffix) and len(t) - len(suffix) >= 4:
                t = t[: -len(suffix)]
                break
        tokens.append(t)
    return tokens


class BM25Retriever:
    def __init__(self, knowledge_dir: Path, k1: float = 1.5, b: float = 0.75):
        self.knowledge_dir = Path(knowledge_dir)
        self.k1, self.b = k1, b
        self.chunks: list[Chunk] = []
        self._tf: list[Counter] = []
        self._df: Counter = Counter()
        self._avgdl = 0.0
        self.reindex()

    def reindex(self) -> int:
        """Re-read the knowledge folder. Call it after editing files: this is the 'updatable knowledge base'."""
        self.chunks = load_knowledge(self.knowledge_dir)
        self._tf = [Counter(tokenize(c.title + " " + c.title + " " + c.text)) for c in self.chunks]
        self._df = Counter()
        for tf in self._tf:
            self._df.update(tf.keys())
        total = sum(sum(tf.values()) for tf in self._tf)
        self._avgdl = total / len(self._tf) if self._tf else 0.0
        return len(self.chunks)

    def search(self, query: str, k: int = 3, min_score: float = 0.1) -> list[Chunk]:
        terms = tokenize(query)
        if not terms or not self.chunks:
            return []
        n = len(self.chunks)
        # A longer question must match at least two different words. This stops one accidental
        # word ("used", "free") from pulling in an unrelated section for an off-topic question.
        needed = 1 if len(set(terms)) <= 2 else 2
        scored = []
        for i, tf in enumerate(self._tf):
            dl = sum(tf.values()) or 1
            score, matched = 0.0, 0
            for term in set(terms):
                f = tf.get(term, 0)
                if not f:
                    continue
                matched += 1
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            if matched >= needed and score >= min_score:
                scored.append((score, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]


class FallbackRetriever:
    """Uses the primary retriever (Qdrant). If it fails, the in-memory BM25 retriever answers instead, so the chat
    keeps working with the same knowledge base. After a failure the primary is skipped for `cooldown` seconds
    so that a dead database does not slow down every message."""

    def __init__(self, primary, fallback, cooldown: float = 30.0, clock=time.monotonic):
        self.primary, self.fallback, self.cooldown, self._clock = primary, fallback, cooldown, clock
        self._skip_until = 0.0

    @property
    def chunks(self):
        return self.fallback.chunks

    @property
    def primary_active(self) -> bool:
        return self._clock() >= self._skip_until

    def reindex(self) -> int:
        count = self.fallback.reindex()
        try:
            self.primary.reindex()
            self._skip_until = 0.0
        except Exception as exc:
            log.warning("Qdrant reindex failed, using the in-memory search for now: %s", exc)
            self._skip_until = self._clock() + self.cooldown
        return count

    def search(self, query: str, k: int = 3, min_score: float = 0.1):
        if self.primary_active:
            try:
                return self.primary.search(query, k, min_score)
            except Exception as exc:
                log.warning("Qdrant search failed, switching to the in-memory search for %.0f s: %s",
                            self.cooldown, exc)
                self._skip_until = self._clock() + self.cooldown
        return self.fallback.search(query, k, min_score)


def build_retriever(settings):
    """BM25 in memory (default) or Qdrant with BM25 as the safety net. Both have reindex() and search()."""
    bm25 = BM25Retriever(settings.knowledge_dir)
    if settings.retriever != "qdrant":
        return bm25
    try:
        from .embedders import build_embedder
        from .qdrant_retriever import QdrantRetriever
        qdrant = QdrantRetriever(settings.knowledge_dir, url=settings.qdrant_url, api_key=settings.qdrant_api_key,
                                 path=settings.qdrant_path, collection=settings.qdrant_collection,
                                 embedder=build_embedder(settings), dense_min_score=settings.dense_min_score)
    except Exception as exc:  # Qdrant not reachable at start: run anyway, with the in-memory search
        log.error("Qdrant is not available (%s). Starting with the in-memory BM25 search.", exc)
        return bm25
    return FallbackRetriever(qdrant, bm25)
