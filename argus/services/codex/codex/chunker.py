"""Split Markdown documents into retrievable chunks -- pure, no I/O.

Chunks follow the document's own structure: a chunk never spans two sections,
and every chunk carries its heading. That is what lets an answer cite
"Write-off Policy > WO-POL-2 Approval limits" instead of just a filename, and
it keeps one policy clause from bleeding into the next.

Sizes are in characters, not tokens, so chunking does not depend on whichever
tokenizer the embedding model happens to use.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Section:
    heading: str  # the section's own heading, e.g. "WO-POL-2 Approval limits"
    path: str  # "Write-off Policy (WO-POL) > WO-POL-2 Approval limits"
    text: str


@dataclass(frozen=True)
class Chunk:
    index: int
    heading: str
    path: str
    content: str

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.content) // 4)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def title_of(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        m = HEADING.match(line)
        if m and len(m.group(1)) == 1:
            return m.group(2)
    return fallback


def sections(markdown: str, title: str) -> list[Section]:
    """Split on headings, keeping the heading trail for citations."""
    trail: list[tuple[int, str]] = []
    out: list[Section] = []
    buffer: list[str] = []

    def flush() -> None:
        text = "\n".join(buffer).strip()
        if text:
            heading = trail[-1][1] if trail else title
            path = " > ".join(h for _, h in trail) or title
            out.append(Section(heading=heading, path=path, text=text))
        buffer.clear()

    for line in markdown.splitlines():
        m = HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            trail = [(lvl, h) for lvl, h in trail if lvl < level] + [(level, m.group(2))]
            continue
        buffer.append(line)
    flush()
    return out


def chunk_document(markdown: str, title: str, size: int, overlap: int) -> list[Chunk]:
    """Sections -> chunks no larger than `size`, overlapping by about `overlap`.

    Long sections are split at paragraph boundaries where possible, then at
    sentence boundaries, and only as a last resort mid-sentence.
    """
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    chunks: list[Chunk] = []
    for section in sections(markdown, title):
        for piece in _split(section.text, size, overlap):
            chunks.append(
                Chunk(index=len(chunks), heading=section.heading, path=section.path,
                      content=piece)
            )
    return chunks


def _split(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    units = _units(text, size)
    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            pieces.append(current)
            tail = current[-overlap:] if overlap else ""
            # Start the overlap at a word boundary so no chunk begins mid-word.
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = f"{tail} {unit}".strip() if tail else unit
        else:
            current = unit
    if current:
        pieces.append(current)
    return pieces


def _units(text: str, size: int) -> list[str]:
    """Paragraphs, broken into sentences (then hard slices) when too long."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            out.append(para)
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", para):
            while len(sentence) > size:
                out.append(sentence[:size])
                sentence = sentence[size:]
            if sentence:
                out.append(sentence)
    return out
