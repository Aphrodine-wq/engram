"""
semantic.py — TF-IDF semantic search and topic modeling.

Goes beyond FTS5 keyword matching. Finds conceptually related captures
even when exact terms don't match. Builds a topic map of your screen
activity over time.

No external ML libs — pure Python TF-IDF with cosine similarity.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Optional

from engram._stop_words import STOP_WORDS as _SEMANTIC_STOP


@dataclass
class SemanticResult:
    """A semantically similar capture result."""
    entry_id: int
    timestamp: float
    app_name: str
    window_title: str
    text_preview: str
    similarity: float      # 0.0 - 1.0
    shared_concepts: list[str]


@dataclass
class TopicCluster:
    """A cluster of related screen activity."""
    topic_id: int
    label: str                  # auto-generated topic label
    keywords: list[str]         # top terms defining this topic
    frame_count: int
    apps: list[str]             # apps associated with this topic
    time_range: tuple[float, float]  # (earliest, latest) timestamp
    coherence: float            # how tightly clustered (0-1)


class TFIDFIndex:
    """
    In-memory TF-IDF index for semantic search over screen captures.
    Rebuilt on demand from the database. Lightweight — no numpy/scipy.
    """

    def __init__(self):
        self.documents: dict[int, list[str]] = {}  # doc_id -> tokens
        self.idf: dict[str, float] = {}
        self.tfidf: dict[int, dict[str, float]] = {}
        self.norms: dict[int, float] = {}
        self._built = False

    def build(self, entries: list) -> None:
        """Build the TF-IDF index from a list of ScreenEntry objects."""
        self.documents.clear()
        self.idf.clear()
        self.tfidf.clear()
        self.norms.clear()

        if not entries:
            self._built = True
            return

        # Tokenize all documents
        doc_freq = Counter()
        for entry in entries:
            tokens = _tokenize(f"{entry.window_title} {entry.text}")
            self.documents[entry.id] = tokens
            unique_tokens = set(tokens)
            for token in unique_tokens:
                doc_freq[token] += 1

        n_docs = len(self.documents)
        if n_docs == 0:
            self._built = True
            return

        # Compute IDF
        for term, df in doc_freq.items():
            self.idf[term] = math.log((n_docs + 1) / (df + 1)) + 1  # smoothed IDF

        # Compute TF-IDF vectors and norms
        for doc_id, tokens in self.documents.items():
            tf = Counter(tokens)
            total = len(tokens) or 1
            vec = {}
            norm_sq = 0.0

            for term, count in tf.items():
                tf_val = count / total
                tfidf_val = tf_val * self.idf.get(term, 0)
                if tfidf_val > 0:
                    vec[term] = tfidf_val
                    norm_sq += tfidf_val ** 2

            self.tfidf[doc_id] = vec
            self.norms[doc_id] = math.sqrt(norm_sq) if norm_sq > 0 else 1.0

        self._built = True

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float, list[str]]]:
        """
        Search the index with a query string.
        Returns list of (doc_id, similarity, shared_terms).
        """
        if not self._built or not self.tfidf:
            return []

        # Tokenize and compute query TF-IDF
        query_tokens = _tokenize(query)
        query_tf = Counter(query_tokens)
        total = len(query_tokens) or 1

        query_vec = {}
        query_norm_sq = 0.0
        for term, count in query_tf.items():
            tf_val = count / total
            tfidf_val = tf_val * self.idf.get(term, 1.0)
            query_vec[term] = tfidf_val
            query_norm_sq += tfidf_val ** 2

        query_norm = math.sqrt(query_norm_sq) if query_norm_sq > 0 else 1.0

        # Cosine similarity against all documents
        scores = []
        for doc_id, doc_vec in self.tfidf.items():
            dot = 0.0
            shared = []
            for term, q_val in query_vec.items():
                d_val = doc_vec.get(term, 0)
                if d_val > 0:
                    dot += q_val * d_val
                    shared.append(term)

            if dot > 0:
                sim = dot / (query_norm * self.norms[doc_id])
                scores.append((doc_id, sim, shared))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def find_similar(self, doc_id: int, top_k: int = 5) -> list[tuple[int, float, list[str]]]:
        """Find documents similar to a given document."""
        if doc_id not in self.tfidf:
            return []

        target_vec = self.tfidf[doc_id]
        target_norm = self.norms[doc_id]

        scores = []
        for other_id, other_vec in self.tfidf.items():
            if other_id == doc_id:
                continue

            dot = 0.0
            shared = []
            for term, val in target_vec.items():
                other_val = other_vec.get(term, 0)
                if other_val > 0:
                    dot += val * other_val
                    shared.append(term)

            if dot > 0:
                sim = dot / (target_norm * self.norms[other_id])
                scores.append((other_id, sim, shared))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def get_top_terms(self, n: int = 30) -> list[tuple[str, float]]:
        """Get the most important terms across all documents."""
        term_scores = defaultdict(float)
        for doc_vec in self.tfidf.values():
            for term, score in doc_vec.items():
                term_scores[term] += score

        sorted_terms = sorted(term_scores.items(), key=lambda x: -x[1])
        return sorted_terms[:n]


class TopicModeler:
    """
    Lightweight topic clustering using term co-occurrence.
    Groups captures into topic clusters without ML dependencies.
    """

    def __init__(self, index: TFIDFIndex):
        self.index = index

    def discover_topics(self, entries: list, n_topics: int = 8) -> list[TopicCluster]:
        """
        Discover topic clusters from screen captures.
        Uses a simple greedy clustering approach:
        1. Find the highest-TF-IDF term as a seed
        2. Cluster all docs with that term
        3. Remove clustered docs, repeat
        """
        if not entries or not self.index._built:
            return []

        # Build entry lookup
        entry_map = {e.id: e for e in entries}
        remaining = set(entry_map.keys())
        topics = []

        for topic_id in range(n_topics):
            if not remaining:
                break

            # Find best seed term from remaining documents
            term_scores = Counter()
            for doc_id in remaining:
                vec = self.index.tfidf.get(doc_id, {})
                for term, score in vec.items():
                    term_scores[term] += score

            if not term_scores:
                break

            seed_term = term_scores.most_common(1)[0][0]

            # Cluster: all remaining docs containing the seed term
            cluster_ids = []
            for doc_id in list(remaining):
                vec = self.index.tfidf.get(doc_id, {})
                if seed_term in vec:
                    cluster_ids.append(doc_id)
                    remaining.discard(doc_id)

            if len(cluster_ids) < 2:
                continue

            # Get top terms for this cluster
            cluster_terms = Counter()
            for doc_id in cluster_ids:
                vec = self.index.tfidf.get(doc_id, {})
                for term, score in vec.items():
                    cluster_terms[term] += score

            top_terms = [t for t, _ in cluster_terms.most_common(8)]

            # Get apps and time range
            cluster_entries = [entry_map[d] for d in cluster_ids if d in entry_map]
            apps = list(set(e.app_name for e in cluster_entries))
            timestamps = [e.timestamp for e in cluster_entries]

            # Generate label from top 2-3 terms
            label = " / ".join(top_terms[:3])

            # Coherence: avg pairwise similarity in cluster
            coherence = self._cluster_coherence(cluster_ids)

            topics.append(TopicCluster(
                topic_id=topic_id,
                label=label,
                keywords=top_terms,
                frame_count=len(cluster_ids),
                apps=apps[:5],
                time_range=(min(timestamps), max(timestamps)),
                coherence=round(coherence, 2),
            ))

        return topics

    def _cluster_coherence(self, doc_ids: list[int]) -> float:
        """Average pairwise similarity within a cluster."""
        if len(doc_ids) < 2:
            return 1.0

        # Sample for large clusters
        sample = doc_ids[:20]
        total_sim = 0.0
        pairs = 0

        for i in range(len(sample)):
            vec_i = self.index.tfidf.get(sample[i], {})
            norm_i = self.index.norms.get(sample[i], 1.0)
            for j in range(i + 1, len(sample)):
                vec_j = self.index.tfidf.get(sample[j], {})
                norm_j = self.index.norms.get(sample[j], 1.0)

                dot = sum(vec_i.get(t, 0) * vec_j.get(t, 0) for t in vec_i if t in vec_j)
                if dot > 0:
                    total_sim += dot / (norm_i * norm_j)
                pairs += 1

        return total_sim / pairs if pairs > 0 else 0.0


def _tokenize(text: str) -> list[str]:
    """Tokenize text for TF-IDF. Extracts meaningful terms."""
    if not text:
        return []

    text = text.lower()

    # Extract various term types
    tokens = []

    # Regular words (3+ chars)
    tokens.extend(re.findall(r'\b[a-z]{3,25}\b', text))

    # CamelCase split
    for match in re.findall(r'[A-Z][a-z]+', text):
        tokens.append(match.lower())

    # snake_case preserved
    tokens.extend(re.findall(r'[a-z]+_[a-z]+', text))

    # File extensions
    tokens.extend(re.findall(r'\.\w{1,6}\b', text))

    # Filter stop words
    return [t for t in tokens if t not in _SEMANTIC_STOP and len(t) >= 3]


