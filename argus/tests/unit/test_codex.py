"""Tests for Codex's chunking and hybrid scoring -- pure functions only.

The embedding model and the database are exercised by the retrieval
evaluation against the running stack; these tests pin down the decisions that
do not need either: chunks follow headings, IDs survive tokenisation, and
fusion and the "no answer" threshold behave as documented.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "codex"))

from argus_core.config import load_collections  # noqa: E402
from codex import chunker, search  # noqa: E402

DOC = """# Write-off Policy (WO-POL)

Intro paragraph.

## WO-POL-2 Approval limits

Up to 500: the agent approves.

## WO-POL-3 Re-rating and credits

Re-rating needs two approvals.
"""


# --------------------------------------------------------------------------
# Shipped config and documents
# --------------------------------------------------------------------------


def test_shipped_collections_load_and_find_their_documents():
    specs = load_collections(REPO_ROOT / "config" / "codex" / "collections")
    docs = REPO_ROOT / "config" / "codex" / "docs"
    for spec in specs.values():
        files = [p for pattern in spec.sources for p in docs.glob(pattern)]
        assert files, f"collection {spec.name} matches no documents"


def test_every_evaluation_answer_exists_as_a_heading():
    """A question whose expected section does not exist can never pass."""
    import yaml

    spec = yaml.safe_load((REPO_ROOT / "config/codex/eval/retrieval-questions.yaml").read_text())
    headings = set()
    for path in (REPO_ROOT / "config" / "codex" / "docs").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        headings.update(s.heading for s in chunker.sections(text, chunker.title_of(text, path.stem)))
    missing = [q["expect"] for q in spec["questions"] if q["expect"] not in headings]
    assert not missing, missing
    assert {q["kind"] for q in spec["questions"]} == {"exact", "prose"}


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def test_chunks_follow_headings_and_carry_the_trail():
    chunks = chunker.chunk_document(DOC, "Write-off Policy (WO-POL)", 900, 100)
    assert [c.heading for c in chunks] == [
        "Write-off Policy (WO-POL)", "WO-POL-2 Approval limits", "WO-POL-3 Re-rating and credits",
    ]
    assert chunks[1].path == "Write-off Policy (WO-POL) > WO-POL-2 Approval limits"
    assert "Re-rating" not in chunks[1].content  # no clause bleeds into the next


def test_long_sections_split_with_overlap_and_within_size():
    para = " ".join(f"Sentence number {i} about approval limits." for i in range(60))
    text = f"# T\n\n## Long\n\n{para}"
    chunks = chunker.chunk_document(text, "T", 400, 80)
    assert len(chunks) > 1
    assert all(len(c.content) <= 400 + 80 for c in chunks)
    assert all(c.heading == "Long" for c in chunks)
    # consecutive chunks share some text
    assert set(chunks[0].content.split()[-5:]) & set(chunks[1].content.split()[:15])


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        chunker.chunk_document(DOC, "T", 100, 100)


def test_content_hash_changes_with_content():
    assert chunker.content_hash("a") != chunker.content_hash("b")


# --------------------------------------------------------------------------
# Keyword terms
# --------------------------------------------------------------------------


def test_ids_stay_whole_and_stopwords_go():
    assert search.keyword_terms("What does CTL-REV-004 check?") == ["ctl-rev-004", "check"]
    assert search.keyword_terms("Who approves under WO-POL-3?") == ["approves", "under", "wo-pol-3"]


# --------------------------------------------------------------------------
# Fusion and threshold
# --------------------------------------------------------------------------


def hit(i: int, v: float = 0.0, k: float = 0.0) -> search.Hit:
    return search.Hit(chunk_id=i, source_path="p.md", title="T", heading=f"H{i}", path=f"T > H{i}",
                      content="c", vector_score=v, keyword_score=k)


def test_fusion_lets_an_exact_keyword_match_beat_a_near_vector_neighbour():
    """The CTL-REV-004 case: vector search ranks its neighbour slightly higher."""
    vector = [hit(3, v=0.70), hit(4, v=0.67)]
    keyword = [hit(4, k=2.0), hit(3, k=1.1)]
    fused = search.fuse(vector, keyword, keyword_weight=0.6)
    assert fused[0].chunk_id == 4
    assert fused[0].keyword_score == pytest.approx(1.0)  # normalised to the best


def test_weight_zero_is_pure_vector_and_one_is_pure_keyword():
    vector = [hit(1, v=0.9), hit(2, v=0.1)]
    keyword = [hit(2, k=5.0)]
    assert search.fuse(vector, keyword, 0.0)[0].chunk_id == 1
    assert search.fuse(vector, keyword, 1.0)[0].chunk_id == 2


def test_threshold_drops_weak_semantic_only_matches_but_keeps_keyword_matches():
    hits = [hit(1, v=0.42), hit(2, v=0.30, k=0.5), hit(3, v=0.66)]
    kept = [h.chunk_id for h in search.apply_threshold(hits, 0.5)]
    assert kept == [2, 3]


def test_rerank_reorders_by_cross_encoder_score():
    hits = [hit(1), hit(2)]
    assert [h.chunk_id for h in search.rerank_order(hits, [0.1, 3.0])] == [2, 1]
