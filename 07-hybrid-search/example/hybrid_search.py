"""Lesson 07 - hybrid search: keyword and meaning, measured on your own questions.

Keyword search (BM25) finds exact tokens such as control IDs, but misses a
question asked in different words. Vector search finds meaning, but blurs IDs
that look alike. Blending the two scores fixes both, and the only way to know
the blend is right is to measure it on questions about your own documents.

Real systems get "meaning" from an embedding model. To stay dependency-free,
this script fakes one with a small concept map (several words -> one concept).
That is enough to show the behavior: it understands "bad debt" means
write-off, and it cannot tell CTL-BILL-003 from CTL-BILL-004.

Standard library only. Fictional documents. Run:  python hybrid_search.py
"""

from __future__ import annotations

import math
import re

DOCS = [
    ("CTL-BILL-003 Unbilled usage",
     "CTL-BILL-003 compares usage records with invoices every day. Usage with no invoice after two days is unbilled revenue."),
    ("CTL-BILL-004 Mis-rated usage",
     "CTL-BILL-004 re-rates a sample of usage against the tariff table. A charge that differs from the tariff is mis-rated."),
    ("WO-POL-2 Write-off approval",
     "Write-offs above 5,000 need the chief financial officer's approval before they are posted."),
    ("COL-PROC-1 Dunning",
     "Send a reminder after 30 days and a final notice after 60 days, then hand the account to collections."),
]

# (question, index of the right document, kind)
QUESTIONS = [
    ("What does CTL-BILL-004 check?", 1, "exact ID"),
    ("Explain control CTL-BILL-003", 0, "exact ID"),
    ("Who has to sign off when we forgive a large bad debt?", 2, "plain English"),
    ("A customer was overcharged compared to the price list. What catches that?", 1, "plain English"),
    ("How long before an overdue account goes to collections?", 3, "plain English"),
]

STOPWORDS = {"a", "an", "the", "to", "of", "is", "are", "was", "what", "who", "how", "when", "we",
             "does", "do", "has", "that", "with", "after", "before", "and", "then", "every", "they",
             "it", "its", "no", "from", "against", "above", "need", "goes", "long"}


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text.lower()) if t not in STOPWORDS]


# --- Keyword search: BM25 -----------------------------------------------------
def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    doc_tokens = [tokens(d) for d in docs]
    avg_len = sum(len(d) for d in doc_tokens) / len(doc_tokens)
    scores = []
    for d in doc_tokens:
        score = 0.0
        for term in set(tokens(query)):
            n = sum(1 for other in doc_tokens if term in other)
            if n == 0 or term not in d:
                continue
            idf = math.log(1 + (len(docs) - n + 0.5) / (n + 0.5))
            tf = d.count(term)
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg_len))
        scores.append(score)
    return scores


# --- "Vector" search: a stand-in for an embedding model ----------------------
CONCEPTS = {
    "billing": {"unbilled", "invoice", "invoices", "billed", "bill"},
    "usage": {"usage", "records", "traffic"},
    "rating": {"tariff", "rate", "rated", "re-rates", "mis-rated", "price", "charge", "overcharged"},
    "write_off": {"write-offs", "write-off", "forgive", "bad", "debt"},
    "approval": {"approval", "approve", "sign", "off"},
    "collection": {"reminder", "notice", "overdue", "collections", "dunning", "account"},
    "control": {"control", "check", "catches", "explain"},
}


def embed(text: str) -> dict[str, float]:
    vector: dict[str, float] = {}
    for t in tokens(text):
        if re.fullmatch(r"[a-z]+-[a-z]+-\d+", t):  # IDs all look alike to the "model"
            vector["control"] = vector.get("control", 0) + 1
            continue
        for concept, words in CONCEPTS.items():
            if t in words:
                vector[concept] = vector.get(concept, 0) + 1
    return vector


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(a[k] * b.get(k, 0) for k in a)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def vector_scores(query: str, docs: list[str]) -> list[float]:
    q = embed(query)
    return [cosine(q, embed(d)) for d in docs]


# --- Hybrid: normalize each, then blend ---------------------------------------
KEYWORD_WEIGHT = 0.5  # a starting point, not an answer: measure on your own questions


def normalize(scores: list[float]) -> list[float]:
    top = max(scores)
    return [s / top if top else 0.0 for s in scores]


def hybrid_scores(query: str, docs: list[str]) -> list[float]:
    k = normalize(bm25_scores(query, docs))
    v = normalize(vector_scores(query, docs))
    return [KEYWORD_WEIGHT * ks + (1 - KEYWORD_WEIGHT) * vs for ks, vs in zip(k, v)]


def best(scores: list[float]) -> int | None:
    top = max(scores)
    if top == 0:
        return None
    winners = [i for i, s in enumerate(scores) if s == top]
    return winners[0] if len(winners) == 1 else None  # a tie is not an answer


def main() -> None:
    texts = [d[1] for d in DOCS]
    retrievers = {"Keyword": bm25_scores, "Vector": vector_scores, "Hybrid": hybrid_scores}
    results = {name: {"exact ID": 0, "plain English": 0} for name in retrievers}
    totals = {"exact ID": 0, "plain English": 0}

    for question, right, kind in QUESTIONS:
        totals[kind] += 1
        print(f"\nQ ({kind}): {question}")
        print(f"   right answer: {DOCS[right][0]}")
        for name, fn in retrievers.items():
            pick = best(fn(question, texts))
            ok = pick == right
            results[name][kind] += ok
            found = DOCS[pick][0] if pick is not None else "nothing clear (tie or no match)"
            print(f"   {name:<8} {'OK  ' if ok else 'MISS'} {found}")

    print("\n" + "=" * 64)
    print(f"{'Retriever':<10} {'Exact-ID questions':>20} {'Plain-English':>16} {'All':>8}")
    print("=" * 64)
    for name, r in results.items():
        all_ok = r["exact ID"] + r["plain English"]
        print(f"{name:<10} {r['exact ID']:>17}/{totals['exact ID']} {r['plain English']:>13}/{totals['plain English']}"
              f" {all_ok:>6}/{len(QUESTIONS)}")
    print("\nEach retriever misses a different kind of question. The blend gets both.")


if __name__ == "__main__":
    main()
