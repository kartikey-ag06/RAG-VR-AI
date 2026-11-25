"""core.knowledge - document search (TF-IDF + embeddings + RRF). Later: the document store and revisions."""
from .search import LOW_CONFIDENCE, SearchEngine, norm_code, ranks, tag_positions, tfidf_scores

__all__ = ["SearchEngine", "LOW_CONFIDENCE", "norm_code", "ranks", "tag_positions", "tfidf_scores"]
