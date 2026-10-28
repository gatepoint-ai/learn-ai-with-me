"""
Lesson 04 - RAG for executives.

Run:  python rag.py      (Python 3.8+, standard library only, fictional policies)

Retrieval-Augmented Generation in four steps:
  1. Split documents into small passages, keeping track of where each came from.
  2. Find the passages most relevant to the question (here: simple keyword
     scoring with TF-IDF; real systems use embeddings and a vector database).
  3. If nothing is relevant enough, say "I don't know" instead of guessing.
  4. Answer ONLY from the retrieved passages, and cite the source.

The "answer" step is a stand-in that quotes the best passage. In a real system
you would send the question and the retrieved passages to a language model,
using the prompt this script prints.
"""

import math
import re
from collections import Counter
from pathlib import Path

DOCS_DIR = Path(__file__).parent / "docs"
TOP_K = 2
MIN_SCORE = 0.10  # below this, nothing is relevant enough -> "I don't know"

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "by", "it", "at", "as", "with", "who", "what", "how", "do", "does",
    "can", "we", "our", "my", "i", "must", "may", "if", "then", "than", "that",
    "this", "each", "any", "from", "into", "after", "within", "long", "much",
    # Generic words that appear in every policy document carry no meaning for
    # retrieval - left in, they make unrelated questions look relevant.
    "policy", "policies",
}


def tokenize(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]


# ---------------------------------------------------------------------------
# 1. Load and split: one passage per "## section", with a citation label
# ---------------------------------------------------------------------------
def load_passages():
    passages = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        title, section, lines = path.stem, None, []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                if section and lines:
                    passages.append((f"{title} / {section}", " ".join(lines)))
                section, lines = line[3:].strip(), []
            elif line.strip() and not line.startswith("# ") and section:
                lines.append(line.strip())
        if section and lines:
            passages.append((f"{title} / {section}", " ".join(lines)))
    return passages


# ---------------------------------------------------------------------------
# 2. Retrieve: TF-IDF vectors and cosine similarity
# ---------------------------------------------------------------------------
class Index:
    def __init__(self, passages):
        self.passages = passages
        # Index the passage text only; the source label is kept for the citation.
        docs = [tokenize(text) for _, text in passages]
        n = len(docs)
        df = Counter(term for doc in docs for term in set(doc))
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self.vectors = [self._vector(doc) for doc in docs]

    def _vector(self, tokens):
        tf = Counter(tokens)
        vec = {t: c * self.idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, question, k=TOP_K):
        q = self._vector(tokenize(question))
        scored = []
        for (source, text), vec in zip(self.passages, self.vectors):
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            scored.append((score, source, text))
        scored.sort(reverse=True)
        return scored[:k]


# ---------------------------------------------------------------------------
# 3 + 4. Answer only from evidence, with a citation - or decline
# ---------------------------------------------------------------------------
def build_prompt(question, hits):
    context = "\n".join(f"[{source}] {text}" for _, source, text in hits)
    return (
        "Answer the question using ONLY the passages below. "
        "Cite the source in brackets. If the passages do not contain the answer, "
        "say you don't know.\n\n"
        f"Passages:\n{context}\n\nQuestion: {question}\n"
    )


def answer(index, question):
    hits = index.search(question)
    relevant = [h for h in hits if h[0] >= MIN_SCORE]
    if not relevant:
        best = hits[0][0] if hits else 0.0
        return (f"I don't know - nothing in our documents covers this "
                f"(best match score {best:.2f}, minimum {MIN_SCORE:.2f})."), None
    score, source, text = relevant[0]
    # Stand-in for the language model: quote the best passage with its source.
    return f"{text} [source: {source}, score {score:.2f}]", build_prompt(question, relevant)


def main():
    index = Index(load_passages())
    questions = [
        "Who approves a write-off of 3,000 dollars?",
        "How quickly must a customer dispute be acknowledged?",
        "How long do we keep backups?",
        "What is our policy on remote work?",  # not covered: should say "I don't know"
    ]
    for q in questions:
        reply, prompt = answer(index, q)
        print("Q:", q)
        print("A:", reply)
        print("-" * 72)
    print("\nExample prompt a real system would send to the language model:\n")
    print(answer(index, questions[0])[1])


if __name__ == "__main__":
    main()
