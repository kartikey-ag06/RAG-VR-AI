"""Stop-words: words too common to help search, dropped by the tokenizer.

The Core holds only GENERIC English (and general legal-English) words. Corpus-specific words live in data files,
one set per sector, e.g. backend/config/stopwords/legal_compliance.txt, so a new customer's corpus does not inherit them.

    VR_RAG_STOPWORDS_FILES   comma-separated list of extra word files (paths relative to the repo root allowed)
                             default: backend/config/stopwords/legal_compliance.txt  (keeps today's demo behaviour)
                             set it to "" (empty) to use only the generic words

The search index and every client that tokenizes locally (glasses_app/src/rag.js) must use the SAME set, or local
search silently misses words. tests/test_stopwords.py checks that.
"""
import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

BASE_STOPWORDS = frozenset("""a an the and or of to in on for by with at from as is are was were be been being this
that these those it its which who whom whose what when where why how not no nor but if then than so such into upon
under over above below shall will would should may might can could must do does did done have has had having he she
they them his her their i we you me us our your said also any all each other same per vide hereinafter thereof
therein herein whereas page""".split())

DEFAULT_FILES = "backend/config/stopwords/legal_compliance.txt"   # inside backend/: Render (rootDir: backend) ships only that
ENV = "VR_RAG_STOPWORDS_FILES"


def read_word_file(path: Path) -> set:
    words = set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        words.update(w for w in line.split() if w)
    return words


def load_stopwords(root: Path, env: Optional[Mapping[str, str]] = None, files: Optional[Iterable] = None) -> frozenset:
    """BASE_STOPWORDS + the words in each extra file. A missing file is an error: a silent fallback would change
    search results without anyone noticing."""
    if files is None:
        env = os.environ if env is None else env
        raw = env.get(ENV, DEFAULT_FILES)
        files = [f.strip() for f in raw.split(",") if f.strip()]
    words = set(BASE_STOPWORDS)
    for f in files:
        p = Path(f)
        p = p if p.is_absolute() else Path(root) / p
        if not p.exists():
            raise FileNotFoundError(f"stop-word file not found: {p} (check {ENV})")
        words |= read_word_file(p)
    return frozenset(words)
