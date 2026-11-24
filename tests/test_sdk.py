"""Tests for the draft Core SDK (backend/core/sdk) and the Core boundary rules (ADR 0001, 0003).
Pure unit tests: no app, no network."""
import ast
import copy
import json
import re
from pathlib import Path

import pytest

from core.sdk import (CORE_EVENTS, SDK_VERSION, CoreContext, Event, Manifest, PolicyDenied, Principal,
                      load_manifest, satisfies)
from core.sdk.context import BudgetExceeded, ModuleDisabled
from core.sdk.version import parse_range, parse_version

ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = ROOT / "backend" / "core"
EXAMPLE = ROOT / "architecture" / "examples" / "industrial_field_ops.manifest.json"


def example():
    return json.loads(EXAMPLE.read_text("utf-8"))


def invalid(data, needle):
    with pytest.raises(Exception) as e:
        Manifest.model_validate(data)
    assert needle in str(e.value), str(e.value)


# ---------------------------------------------------------------- versions
def test_version_ranges():
    assert parse_version("1.2") == (1, 2, 0)
    assert satisfies("1.0.0", ">=1.0,<2.0") and satisfies("1.9.9", ">=1.0,<2.0")
    assert not satisfies("2.0.0", ">=1.0,<2.0") and not satisfies("0.9", ">=1.0")
    assert satisfies("1.2.0", "==1.2") and not satisfies("1.2.0", "!=1.2.0")
    for bad in ("", "~1.0", ">=1.0,,<2", "1.0"):
        with pytest.raises(ValueError):
            parse_range(bad)
    with pytest.raises(ValueError):
        parse_version("1.x")


# ---------------------------------------------------------------- manifest
def test_example_manifest_is_valid_and_compatible():
    m = load_manifest(EXAMPLE)
    assert m.id == "industrial_field_ops" and m.kind == "pack"
    assert m.compatible() and not m.compatible("2.0.0")
    assert m.route_prefix == "/api/v1/packs/industrial_field_ops"
    assert m.config_defaults()["reports.kinds"] == ["inspection", "incident", "workorder"]


def test_manifest_rejects_bad_ids_and_unknown_fields():
    d = example(); d["id"] = "Bad-Id"
    invalid(d, "id must be")
    d = example(); d["surprise"] = 1
    invalid(d, "surprise")


def test_manifest_scopes_must_be_known_or_namespaced():
    d = example(); d["requires_scopes"].append("root:everything")
    invalid(d, "unknown Core scopes")
    d = example(); d["provides_scopes"] = ["other_pack:approve"]
    invalid(d, "may only define scopes")


def test_manifest_events_rules():
    d = example(); d["publishes"] = ["question.asked"]
    invalid(d, "may only publish")
    d = example(); d["publishes"] = ["industrial_field_ops.Bad Name"]
    invalid(d, "bad event name")
    d = example(); d["requires_scopes"].remove("events:publish")
    invalid(d, "events:publish")


def test_manifest_config_and_mcp_rules():
    d = example(); d["config"]["heatmap.min_questions"]["default"] = "three"
    invalid(d, "not of type integer")
    d = example(); d["config"]["api.password"] = {"type": "string", "secret": True, "default": "x"}
    invalid(d, "secret config item")
    d = example(); d["config"]["Bad Key"] = {"type": "string"}
    invalid(d, "bad config key")
    d = example(); d["mcp_tools"][0]["name"] = "find_machine_docs"
    invalid(d, "must start with")
    d = example(); d["core"] = "latest"
    invalid(d, "bad version condition")


def test_load_manifest_reports_file(tmp_path):
    f = tmp_path / "manifest.json"
    f.write_text("{not json", "utf-8")
    with pytest.raises(ValueError, match="cannot read manifest"):
        load_manifest(f)
    f.write_text(json.dumps({"id": "x"}), "utf-8")
    with pytest.raises(ValueError, match="invalid manifest"):
        load_manifest(f)


# ---------------------------------------------------------------- events, principals, errors
def test_events():
    e = Event("question.asked", tenant_id="default", data={"page": 3})
    assert e.to_dict()["name"] == "question.asked" and len(e.id) == 32
    with pytest.raises(ValueError):
        Event("QuestionAsked", tenant_id="default")
    with pytest.raises(ValueError):
        Event("question.asked", tenant_id="")
    assert all(re.match(r"^[a-z_]+\.[a-z_]+$", n) for n in CORE_EVENTS)


def test_principal_and_errors():
    p = Principal(tenant_id="acme", kind="user", scopes=frozenset({"ai:ask"}))
    assert p.has("ai:ask") and not p.has("admin")
    assert Principal(tenant_id="acme", kind="user", scopes=frozenset({"admin"})).has("knowledge:write")
    assert PolicyDenied("no").status == 403 and ModuleDisabled("x").status == 404 and BudgetExceeded().status == 429


def test_core_context_is_a_protocol():
    class Fake:                                     # anything with these attributes satisfies the protocol
        module_id = "demo_pack"; sdk_version = SDK_VERSION; router = llm = kv = config = events = log = None
        def principal(self, r): ...
        def require(self, r, s): ...
        def search(self, r, q, **k): ...
        def answer(self, r, q, **k): ...
        def call(self, r, f, **k): ...
    assert isinstance(Fake(), CoreContext)


# ---------------------------------------------------------------- boundary guards (ADR 0001)
def _imports(py: Path):
    tree = ast.parse(py.read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module


BACKEND_MODULES = {p.stem for p in (ROOT / "backend").glob("*.py")}       # app, indexer, procops, ...


def test_core_never_imports_packs_capabilities_or_old_modules():
    for py in CORE_DIR.rglob("*.py"):
        for mod in _imports(py):
            top = mod.split(".")[0]
            assert top not in ("packs", "capabilities"), f"{py.relative_to(ROOT)} imports {mod}"
            assert top not in BACKEND_MODULES, f"{py.relative_to(ROOT)} imports old module {mod}"


SECTOR_WORDS = re.compile(r"\b(sebi|noticees?|reliance|procops|penalt(y|ies)|machine_id|workorder)\b", re.I)


def test_core_has_no_sector_words():
    for py in CORE_DIR.rglob("*.py"):
        for n, line in enumerate(py.read_text("utf-8").splitlines(), 1):
            assert not SECTOR_WORDS.search(line), f"sector word in {py.relative_to(ROOT)}:{n}: {line.strip()}"
