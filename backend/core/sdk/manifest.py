"""manifest.json - what a capability or pack declares about itself (ADR 0003).

The Core reads it BEFORE running any pack code, so it can refuse a pack that is incompatible or asks for things it
may not have. Example: architecture/examples/industrial_field_ops.manifest.json
"""
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .events import CORE_EVENTS, check_event_name
from .scopes import CORE_SCOPES, check_pack_scope
from .version import SDK_VERSION, parse_range, parse_version, satisfies

PACK_ID = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "list": list, "object": dict}


class ConfigItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "integer", "number", "boolean", "list", "object"]
    default: Any = None
    description: str = ""
    secret: bool = False            # value lives in env / encrypted storage; config only holds a reference

    @model_validator(mode="after")
    def _default_matches_type(self):
        if self.secret and self.default is not None:
            raise ValueError("a secret config item cannot have a default value")
        want = _TYPES[self.type]
        if self.default is not None and (not isinstance(self.default, want)
                                         or (self.type in ("integer", "number") and isinstance(self.default, bool))):
            raise ValueError(f"default {self.default!r} is not of type {self.type}")
        return self


class McpTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = Field(min_length=10)
    input_schema: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    read_only: bool = True


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = Field(min_length=3, max_length=80)
    version: str
    kind: Literal["capability", "pack"]
    core: str = Field(description="compatible Core SDK range, e.g. '>=1.0,<2.0'")
    description: str = ""
    depends_on: list[str] = Field(default_factory=list, description="ids of capabilities this needs")
    requires_scopes: list[str] = Field(default_factory=list, description="Core scopes the module calls with")
    provides_scopes: list[str] = Field(default_factory=list, description="new scopes, '<id>:<action>'")
    subscribes: list[str] = Field(default_factory=list, description="event names it listens to")
    publishes: list[str] = Field(default_factory=list, description="its own events, '<id>.<verb>'")
    config: dict[str, ConfigItem] = Field(default_factory=dict)
    mcp_tools: list[McpTool] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v):
        if not PACK_ID.match(v):
            raise ValueError("id must be 3-40 chars: lower-case letters, digits, '_', starting with a letter")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v):
        parse_version(v)
        return v

    @field_validator("core")
    @classmethod
    def _core_range(cls, v):
        parse_range(v)
        return v

    @field_validator("requires_scopes")
    @classmethod
    def _requires(cls, v):
        unknown = [s for s in v if s not in CORE_SCOPES]
        if unknown:
            raise ValueError(f"unknown Core scopes {unknown}; known: {sorted(CORE_SCOPES)}")
        return v

    @model_validator(mode="after")
    def _namespaced(self):
        for s in self.provides_scopes:
            check_pack_scope(self.id, s)
        for e in self.subscribes:
            check_event_name(e)
        for e in self.publishes:
            check_event_name(e)
            if e.split(".", 1)[0] != self.id:
                raise ValueError(f"pack {self.id!r} may only publish '{self.id}.<verb>' events (got {e!r})")
            if e in CORE_EVENTS:
                raise ValueError(f"{e!r} is a Core event; packs cannot publish it")
        if self.publishes and "events:publish" not in self.requires_scopes:
            raise ValueError("a module that publishes events must require the 'events:publish' scope")
        for k in self.config:
            if not CONFIG_KEY.match(k):
                raise ValueError(f"bad config key {k!r}: use dotted lower-case names, e.g. 'reports.kinds'")
        for t in self.mcp_tools:
            if not t.name.startswith(self.id + "_"):
                raise ValueError(f"MCP tool {t.name!r} must start with '{self.id}_' so tools from packs never clash")
        if self.id in self.depends_on:
            raise ValueError("a module cannot depend on itself")
        return self

    # ------------------------------------------------------------ used by the Core loader (Phase 5)
    def compatible(self, sdk_version: str = SDK_VERSION) -> bool:
        return satisfies(sdk_version, self.core)

    @property
    def route_prefix(self) -> str:
        return f"/api/v1/packs/{self.id}"

    def config_defaults(self) -> dict:
        return {k: c.default for k, c in self.config.items() if not c.secret}


def load_manifest(path) -> Manifest:
    """Read and validate a manifest.json file. Raises ValueError with every problem found."""
    p = Path(path)
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{p}: cannot read manifest: {e}") from e
    try:
        return Manifest.model_validate(data)
    except Exception as e:                    # pydantic.ValidationError lists every field that is wrong
        raise ValueError(f"{p}: invalid manifest:\n{e}") from e
