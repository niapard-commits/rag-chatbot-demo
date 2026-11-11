"""Load markdown files and cut them into chunks (one chunk per second-level section)."""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Chunk:
    source: str   # file name
    title: str    # section heading
    text: str     # section text

    def as_context(self) -> str:
        return f"[{self.source} / {self.title}]\n{self.text}"


def split_markdown(source: str, content: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    doc_title = source
    title = None
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            chunks.append(Chunk(source, title or doc_title, text))

    for line in content.splitlines():
        if line.startswith("# "):
            doc_title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            buf = []
            title = line[3:].strip()
        else:
            buf.append(line)
    flush()
    return chunks


def load_knowledge(folder: Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(folder).glob("*.md")):
        chunks.extend(split_markdown(path.name, path.read_text(encoding="utf-8")))
    return chunks
