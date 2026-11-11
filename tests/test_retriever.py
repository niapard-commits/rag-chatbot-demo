from app.rag.loader import split_markdown
from app.rag.retriever import BM25Retriever


def test_split_markdown_makes_one_chunk_per_section():
    chunks = split_markdown("a.md", "# Doc\n\n## One\ntext one\n\n## Two\ntext two\n")
    assert [c.title for c in chunks] == ["One", "Two"]
    assert chunks[0].text == "text one"


def test_search_finds_the_right_section(settings):
    r = BM25Retriever(settings.knowledge_dir)
    assert r.search("how much is teeth whitening")[0].title == "Teeth whitening"
    assert r.search("what are your opening hours")[0].title == "What are your opening hours?"
    assert r.search("do you treat children")[0].title == "Do you treat children?"


def test_unrelated_question_returns_nothing(settings):
    assert BM25Retriever(settings.knowledge_dir).search("quantum computing mortgage") == []


def test_reindex_picks_up_new_files(tmp_path):
    (tmp_path / "a.md").write_text("# A\n\n## Parking\nFree parking behind the building.\n", encoding="utf-8")
    r = BM25Retriever(tmp_path)
    assert r.search("parking")[0].title == "Parking"
    (tmp_path / "b.md").write_text("# B\n\n## Wifi\nGuests can use free wifi.\n", encoding="utf-8")
    assert r.search("wifi") == []          # not indexed yet
    assert r.reindex() == 2                # the knowledge base is updatable without restarting
    assert r.search("wifi")[0].title == "Wifi"
