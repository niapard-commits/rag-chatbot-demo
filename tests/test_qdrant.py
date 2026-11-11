"""The Qdrant retriever must behave like the BM25 one. Runs in Qdrant's embedded in-memory mode: no server needed."""
import zlib

import pytest

pytest.importorskip("qdrant_client")

from app.rag.qdrant_retriever import QdrantRetriever
from app.rag.retriever import BM25Retriever, FallbackRetriever, build_retriever

QUESTIONS = [
    "how much is teeth whitening", "what are your opening hours", "do you treat children", "where is the clinic",
    "can I pay in instalments", "price of filling", "how can I cancel my appointment", "do you accept cards",
    "what should I bring to the first visit", "emergency pain broken tooth", "Do you sell used cars?",
    "quantum computing mortgage",
]


@pytest.fixture
def qdrant(settings):
    return QdrantRetriever(settings.knowledge_dir)


def test_indexes_all_sections(settings, qdrant):
    assert len(qdrant.chunks) == len(BM25Retriever(settings.knowledge_dir).chunks) > 10


@pytest.mark.parametrize("question", QUESTIONS)
def test_same_results_as_bm25(settings, qdrant, question):
    expected = [(c.source, c.title) for c in BM25Retriever(settings.knowledge_dir).search(question)]
    got = [(c.source, c.title) for c in qdrant.search(question)]
    assert got == expected


def test_finds_the_right_section(qdrant):
    assert qdrant.search("how much is teeth whitening")[0].title == "Teeth whitening"
    assert qdrant.search("quantum computing mortgage") == []


def test_reindex_picks_up_new_files(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\n## Parking\nFree parking behind the building.\n", encoding="utf-8")
    r = QdrantRetriever(tmp_path)
    assert r.search("parking")[0].title == "Parking"
    (tmp_path / "b.md").write_text("# B\n\n## Wifi\nGuests can use free wifi.\n", encoding="utf-8")
    assert r.search("wifi") == []
    assert r.reindex() == 2
    assert r.search("wifi")[0].title == "Wifi"


def test_empty_knowledge_folder_is_fine(tmp_path):
    r = QdrantRetriever(tmp_path)
    assert r.reindex() == 0 and r.search("anything") == []


def test_data_survives_a_restart_in_embedded_disk_mode(settings, tmp_path):
    first = QdrantRetriever(settings.knowledge_dir, path=str(tmp_path / "qd"))
    first.client.close()
    second = QdrantRetriever(settings.knowledge_dir, path=str(tmp_path / "qd"))
    assert second.search("how much is teeth whitening")[0].title == "Teeth whitening"


def test_build_retriever_chooses_by_setting(settings):
    settings.retriever = "qdrant"
    r = build_retriever(settings)
    assert isinstance(r, FallbackRetriever) and isinstance(r.primary, QdrantRetriever)
    settings.retriever = "bm25"
    assert isinstance(build_retriever(settings), BM25Retriever)


# ------------------------------------------------------------ safety net when Qdrant is down
class BrokenPrimary:
    def __init__(self):
        self.calls = 0

    def search(self, *args, **kwargs):
        self.calls += 1
        raise ConnectionError("qdrant down")

    def reindex(self):
        raise ConnectionError("qdrant down")


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_search_falls_back_to_bm25_when_qdrant_fails(settings):
    r = FallbackRetriever(BrokenPrimary(), BM25Retriever(settings.knowledge_dir))
    assert r.search("how much is teeth whitening")[0].title == "Teeth whitening"


def test_dead_qdrant_is_skipped_during_the_cooldown_then_tried_again(settings):
    clock, primary = Clock(), BrokenPrimary()
    r = FallbackRetriever(primary, BM25Retriever(settings.knowledge_dir), cooldown=30, clock=clock)
    r.search("opening hours"); r.search("opening hours"); r.search("opening hours")
    assert primary.calls == 1                       # not asked again while it is known to be down
    clock.now = 31
    r.search("opening hours")
    assert primary.calls == 2                       # after the cooldown it gets another chance


def test_reindex_survives_a_dead_qdrant(settings):
    r = FallbackRetriever(BrokenPrimary(), BM25Retriever(settings.knowledge_dir))
    assert r.reindex() > 10


def test_starts_with_bm25_when_qdrant_is_unreachable(settings):
    settings.retriever, settings.qdrant_url = "qdrant", "http://127.0.0.1:9"
    assert isinstance(build_retriever(settings), BM25Retriever)


# ------------------------------------------------------------ dense and hybrid search
CONCEPTS = {  # a tiny hand-made "embedding model": words that mean the same share one dimension
    "whitening": ("whiten", "bleach", "lighter", "brighter", "shade"),
    "price": ("price", "cost", "much", "eur", "fee", "set me back", "pay"),
    "children": ("child", "kid", "daughter", "son", "toddler"),
    "hours": ("opening", "hours", "open", "closed"),
}


class FakeEmbedder:
    NOISE = 16
    dim = len(CONCEPTS) + NOISE

    def __init__(self):
        self.fail = False

    def embed(self, texts):
        if self.fail:
            raise ConnectionError("embedding service down")
        out = []
        for text in texts:
            low = text.lower()
            vec = [5.0 * min(1, sum(low.count(w) for w in words)) for words in CONCEPTS.values()]
            noise = [0.0] * self.NOISE   # every other word adds a little random-looking weight, like a real model
            for word in low.split():
                noise[zlib.crc32(word.encode()) % self.NOISE] += 1.0
            out.append(vec + noise)
        return out


@pytest.fixture
def hybrid(settings):
    return QdrantRetriever(settings.knowledge_dir, embedder=FakeEmbedder(), dense_min_score=0.6)


def titles(chunks):
    return [c.title for c in chunks]


def test_dense_finds_a_section_that_shares_no_word_with_the_question(settings, hybrid):
    question = "how can I make my teeth lighter"
    assert BM25Retriever(settings.knowledge_dir).search(question) == []          # word search is blind here
    assert "Teeth whitening" in titles(hybrid.search(question))                  # meaning search finds it


def test_hybrid_still_answers_exact_word_questions(settings, hybrid):
    assert "Teeth whitening" in titles(hybrid.search("how much is teeth whitening"))
    assert "Do you treat children?" in titles(hybrid.search("do you treat children"))


def test_off_topic_question_still_finds_nothing_in_hybrid_mode(hybrid):
    assert hybrid.search("Do you sell used cars?") == []
    assert hybrid.search("quantum computing mortgage") == []


def test_dense_threshold_controls_what_counts_as_related(settings):
    strict = QdrantRetriever(settings.knowledge_dir, embedder=FakeEmbedder(), dense_min_score=0.999)
    assert strict.search("how can I make my teeth lighter") == []


def test_embedding_failure_at_search_time_degrades_to_word_search(settings, hybrid):
    hybrid.embedder.fail = True
    assert "Teeth whitening" in titles(hybrid.search("how much is teeth whitening"))
    assert hybrid.search("how can I make my teeth lighter") == []   # no crash, just no meaning search


def test_dense_vectors_are_stored_in_the_collection(hybrid):
    info = hybrid.client.get_collection(hybrid.collection)
    assert info.config.params.vectors["dense"].size == FakeEmbedder.dim
    assert "bm25" in info.config.params.sparse_vectors


def test_embedder_selection_from_settings(settings):
    from app.rag.embedders import build_embedder
    settings.embedder = ""
    assert build_embedder(settings) is None
    settings.embedder = "nonsense"
    with pytest.raises(ValueError):
        build_embedder(settings)
    settings.embedder, settings.openai_api_key = "openai", ""
    with pytest.raises(ValueError):
        build_embedder(settings)


def test_missing_embedding_library_means_bm25_not_a_crash(settings):
    settings.retriever, settings.embedder = "qdrant", "fastembed"
    try:
        import fastembed  # noqa: F401
        pytest.skip("fastembed is installed; it would try to download a model")
    except ImportError:
        assert isinstance(build_retriever(settings), BM25Retriever)
