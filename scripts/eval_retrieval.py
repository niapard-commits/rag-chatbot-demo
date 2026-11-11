"""Compare word search (BM25) with hybrid search (BM25 + embeddings) on questions written in different words,
and show the cosine scores so you can choose DENSE_MIN_SCORE for your embedding model.

  EMBEDDER=fastembed python scripts/eval_retrieval.py
  EMBEDDER=openai OPENAI_API_KEY=... python scripts/eval_retrieval.py
  python scripts/eval_retrieval.py --spacy     # offline experiment with small spaCy word vectors (no download of HF models)

Each case is (question, title of the section that must be found, or None when nothing should be found).
Edit CASES for your own knowledge base.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.rag.embedders import build_embedder
from app.rag.loader import load_knowledge
from app.rag.qdrant_retriever import QdrantRetriever
from app.rag.retriever import BM25Retriever

CASES = [
    # same words as the documents
    ("how much is teeth whitening", "Price list"),
    ("what are your opening hours", "What are your opening hours?"),
    ("do you treat children", "Do you treat children?"),
    # other words, same meaning
    ("how can I make my teeth lighter", "Teeth whitening"),
    ("my daughter is 6, can she come to you", "Do you treat children?"),
    ("what will it set me back", "Price list"),
    ("when do you close in the evening", "What are your opening hours?"),
    ("can I split the payment over months", "Payment"),
    ("I have a terrible toothache right now", "Emergency care"),
    ("do I have to book in advance", "Do I need an appointment?"),
    # nothing in the documents about it: the bot must say it does not know
    ("do you sell used cars", None),
    ("who won the football match yesterday", None),
    ("quantum computing mortgage", None),
]


class SpacyEmbedder:
    def __init__(self):
        import spacy
        self.nlp = spacy.load("en_core_web_md", disable=["parser", "ner", "tagger", "lemmatizer", "attribute_ruler"])
        self.dim = 300

    def embed(self, texts):
        return [list(map(float, self.nlp(t).vector)) for t in texts]


def cosine(a, b):
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def main():
    s = Settings()
    embedder = SpacyEmbedder() if "--spacy" in sys.argv else build_embedder(s)
    if embedder is None:
        sys.exit("Set EMBEDDER=fastembed (or openai), or use --spacy")
    bm25 = BM25Retriever(s.knowledge_dir)
    hybrid = QdrantRetriever(s.knowledge_dir, url=s.qdrant_url, api_key=s.qdrant_api_key, path=s.qdrant_path,
                             collection="eval_retrieval", embedder=embedder, dense_min_score=s.dense_min_score)
    chunks = load_knowledge(s.knowledge_dir)
    doc_vecs = embedder.embed([f"{c.title}\n{c.text}" for c in chunks])

    wins = {"bm25": 0, "hybrid": 0}
    on_topic, off_topic = [], []
    print(f"embedder: {type(embedder).__name__}  DENSE_MIN_SCORE={s.dense_min_score}\n")
    for question, expected in CASES:
        qv = embedder.embed([question])[0]
        sims = sorted(((cosine(qv, v), c.title) for v, c in zip(doc_vecs, chunks)), reverse=True)
        best = sims[0]
        row = {}
        for name, retriever in (("bm25", bm25), ("hybrid", hybrid)):
            found = [c.title for c in retriever.search(question, k=3)]
            ok = (not found) if expected is None else (expected in found)
            row[name] = ("ok " if ok else "FAIL") + f" {found}"
            wins[name] += ok
        (off_topic if expected is None else on_topic).append(best[0])
        print(f"{question!r}\n   expected: {expected}\n   bm25:   {row['bm25']}\n   hybrid: {row['hybrid']}\n"
              f"   best cosine {best[0]:.2f} ({best[1]})")
    print(f"\ncorrect: bm25 {wins['bm25']}/{len(CASES)}   hybrid {wins['hybrid']}/{len(CASES)}")
    print(f"best cosine on-topic: {min(on_topic):.2f} to {max(on_topic):.2f}   "
          f"off-topic: {min(off_topic):.2f} to {max(off_topic):.2f}")
    print("A good DENSE_MIN_SCORE lies between the highest off-topic and the lowest on-topic score.")


if __name__ == "__main__":
    main()
