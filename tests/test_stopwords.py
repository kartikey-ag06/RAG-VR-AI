"""Stop-words (Phase 3 step 5): generic words in the Core, corpus words in backend/config/stopwords/*.txt.

Most important check: the glasses app tokenizes on the device (glasses_app/src/rag.js) to search the downloaded
index. If its stop-word list differs from the server's, local search silently misses or mis-scores words.
"""
import re
import sys
from pathlib import Path

import pytest

import indexer
from core.knowledge.stopwords import BASE_STOPWORDS, DEFAULT_FILES, ENV, load_stopwords, read_word_file

ROOT = Path(__file__).resolve().parent.parent


def js_stopwords():
    src = (ROOT / "glasses_app" / "src" / "rag.js").read_text("utf-8")
    m = re.search(r"const STOP = new Set\(`(.*?)`\.split", src, re.S)
    assert m, "could not find the STOP list in rag.js"
    return set(m.group(1).split())


def test_server_default_is_unchanged():
    """Same 102 words as before Phase 3 step 5 (the tokenize + search golden files also prove this)."""
    assert len(indexer.STOP) == 102 and {"sebi", "noticee", "noticees", "order", "page"} <= indexer.STOP


def test_glasses_client_matches_server():
    js = js_stopwords()
    assert js == set(indexer.STOP), (f"rag.js and the server differ - only in rag.js: {sorted(js - indexer.STOP)}, "
                                     f"only on the server: {sorted(indexer.STOP - js)}")


def test_pipeline_script_matches_server():
    sys.path.insert(0, str(ROOT / "rag_pipeline"))
    import build_index
    assert set(build_index.STOP) == set(indexer.STOP)


def test_core_base_has_no_corpus_words():
    assert not {"sebi", "noticee", "noticees", "order"} & BASE_STOPWORDS
    assert "legal_compliance" in DEFAULT_FILES


def test_loader_env_and_files(tmp_path):
    assert load_stopwords(ROOT, env={ENV: ""}) == BASE_STOPWORDS                       # generic only
    f = tmp_path / "medical.txt"
    f.write_text("﻿# hospital corpus\nPatient  ward # trailing comment\n\nmg\n", "utf-8")
    assert read_word_file(f) == {"patient", "ward", "mg"}
    both = load_stopwords(ROOT, env={ENV: f"{f}, {DEFAULT_FILES}"})
    assert {"patient", "sebi"} <= both and BASE_STOPWORDS <= both
    with pytest.raises(FileNotFoundError, match="VR_RAG_STOPWORDS_FILES"):
        load_stopwords(ROOT, env={ENV: "backend/config/stopwords/missing.txt"})


def test_runtime_files_live_inside_backend():
    """Render deploys with rootDir: backend - files outside backend/ do not exist in production
    (https://render.com/docs/monorepo-support). Anything the server must read at start-up has to be in backend/."""
    for f in DEFAULT_FILES.split(","):
        p = Path(f.strip())
        assert p.parts[0] == "backend", f"{f} is outside backend/ and would be missing on Render"
        assert (ROOT / p).exists()
