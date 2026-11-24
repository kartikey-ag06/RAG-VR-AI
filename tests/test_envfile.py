"""Tests for backend/envfile.py (the .env loader). Hermetic: uses a temporary file."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import envfile  # noqa: E402


def test_loads_values_and_ignores_comments(tmp_path, monkeypatch):
    for k in ("VRT_A", "VRT_B", "VRT_C", "VRT_D", "VRT_E"):
        monkeypatch.delenv(k, raising=False)
    f = tmp_path / ".env"
    f.write_text('# comment\n\nVRT_A=plain\nVRT_B="quoted value"\nexport VRT_C=exported\nVRT_D=x  # note\nVRT_E=\nbroken line\n',
                 encoding="utf-8")
    added = envfile.load(f)
    assert os.environ["VRT_A"] == "plain"
    assert os.environ["VRT_B"] == "quoted value"
    assert os.environ["VRT_C"] == "exported"
    assert os.environ["VRT_D"] == "x"
    assert "VRT_E" not in os.environ          # empty values are skipped (so an empty GROQ_API_KEY= does nothing)
    assert set(added) == {"VRT_A", "VRT_B", "VRT_C", "VRT_D"}


def test_real_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("VRT_KEEP", "from-render")
    f = tmp_path / ".env"
    f.write_text("VRT_KEEP=from-file\n", encoding="utf-8")
    envfile.load(f)
    assert os.environ["VRT_KEEP"] == "from-render"


def test_missing_file_is_fine(tmp_path):
    assert envfile.load(tmp_path / "nope.env") == []


def test_switched_off_for_tests(monkeypatch):
    monkeypatch.setenv("VR_RAG_NO_ENVFILE", "1")
    assert envfile.load() == []                # the project .env is never read during tests


def test_windows_bom_is_handled(tmp_path, monkeypatch):
    monkeypatch.delenv("VRT_BOM", raising=False)
    f = tmp_path / ".env"
    f.write_bytes("﻿VRT_BOM=ok\r\n".encode("utf-8"))   # Notepad saves with a BOM and CRLF
    envfile.load(f)
    assert os.environ["VRT_BOM"] == "ok"
