"""Document store + retrieval: load PDFs/TXT/MD, split into chunks, rank chunks with BM25.

No vector database or embeddings needed, so it runs anywhere with zero setup. Good enough for a
business's FAQ, policies, menus and manuals (tens to a few thousand pages).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

WORD = re.compile(r"[\w']+", re.UNICODE)
STOP = set("a an and are as at be by for from has have i in is it its of on or that the this to was were will with "
           "you your we our can do does what how when where which who why".split())


def tokens(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1]


@dataclass
class Chunk:
    source: str      # file name
    page: int        # 1-based page (1 for text files)
    text: str


def read_file(path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text), ...]."""
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
    return [(1, path.read_text(encoding="utf-8", errors="replace"))]


def split(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split on paragraph boundaries into ~size-character chunks with a little overlap."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = f"{cur}\n{p}" if cur else p
            continue
        if cur:
            out.append(cur)
        while len(p) > size:                 # very long paragraph: hard-cut it
            out.append(p[:size])
            p = p[size - overlap:]
        cur = (out[-1][-overlap:] + "\n" + p) if out else p
    if cur:
        out.append(cur)
    return out


class Store:
    """In-memory BM25 index, persisted as JSON so restarts don't re-read every file."""

    def __init__(self, index_file: Path) -> None:
        self.index_file = index_file
        self.chunks: list[Chunk] = []
        if index_file.exists():
            self.chunks = [Chunk(**c) for c in json.loads(index_file.read_text(encoding="utf-8"))]
        self._build()

    # ---- indexing
    def add_file(self, path: Path) -> int:
        self.chunks = [c for c in self.chunks if c.source != path.name]   # re-upload replaces
        new = [Chunk(path.name, page, t) for page, text in read_file(path) for t in split(text)]
        self.chunks.extend(new)
        self._save()
        return len(new)

    def remove(self, source: str) -> None:
        self.chunks = [c for c in self.chunks if c.source != source]
        self._save()

    def sources(self) -> dict[str, int]:
        return dict(Counter(c.source for c in self.chunks))

    def _save(self) -> None:
        self.index_file.parent.mkdir(parents=True, exist_ok=True)
        self.index_file.write_text(json.dumps([asdict(c) for c in self.chunks], ensure_ascii=False), encoding="utf-8")
        self._build()

    def _build(self) -> None:
        self.tf = [Counter(tokens(c.text)) for c in self.chunks]
        self.lens = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0
        df = Counter(w for t in self.tf for w in t)
        n = len(self.chunks)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    # ---- search
    def search(self, query: str, k: int = 6, k1: float = 1.5, b: float = 0.75) -> list[Chunk]:
        q = tokens(query)
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w in q:
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * self.lens[i] / (self.avg or 1)))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [self.chunks[i] for _, i in scored[:k]]
