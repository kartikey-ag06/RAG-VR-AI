"""PackLoader - finds capabilities and packs on disk, checks them, and plugs them in (Phase 5).

A module is a folder with:
    manifest.json   what it is and what it needs (core.sdk.manifest) - read BEFORE any module code runs
    __init__.py     def register(ctx): ...   (ctx = core.plugins.ModuleContext)

Start-up order for each module:
    1. manifest valid?             else state "invalid"
    2. Core SDK version in range?  else state "incompatible"
    3. dependencies loaded?        else state "missing_dependency"
    4. register(ctx) ran?          else state "failed"   (the error is kept for the admin page)
A module that fails never stops the server: the others still load, and its URLs answer 404.
"""
import importlib.util
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from core.modules import Module
from core.sdk.manifest import Manifest, load_manifest
from core.sdk.version import SDK_VERSION

from .context import Kernel, LegacyBridge, ModuleContext

log = logging.getLogger("vr_rag.loader")


class PackLoader:
    def __init__(self, app, kernel: Kernel, dirs: Iterable[Path], legacy_services: Optional[dict] = None,
                 allow_scopes=None):
        self.app, self.kernel = app, kernel
        self.dirs = [Path(d) for d in dirs]
        self.legacy_services = legacy_services or {}
        self.allow_scopes = allow_scopes                 # callback: pack scopes that API keys may now hold
        self.status: Dict[str, dict] = {}                # id -> {state, error, version, kind, path, ...}
        self.manifests: Dict[str, Manifest] = {}
        self.contexts: Dict[str, ModuleContext] = {}
        self.exports: Dict[str, object] = {}             # what legacy register() returned

    # ------------------------------------------------------------ discover
    def discover(self) -> List[tuple]:
        found, seen = [], {}
        for d in self.dirs:
            if not d.is_dir():
                continue
            for sub in sorted(p for p in d.iterdir() if p.is_dir() and (p / "manifest.json").exists()):
                try:
                    m = load_manifest(sub / "manifest.json")
                except ValueError as e:
                    self._set(sub.name, "invalid", str(e), path=sub)
                    continue
                if m.id != sub.name:
                    self._set(sub.name, "invalid", f"folder name {sub.name!r} must equal manifest id {m.id!r}", path=sub)
                    continue
                if m.id in seen:
                    self._set(m.id, "invalid", f"duplicate module id (also in {seen[m.id]})", m, sub)
                    continue
                seen[m.id] = sub
                found.append((m, sub))
        return found

    def _order(self, found):
        """Dependencies (hard) and load_after (soft) first; otherwise alphabetical. Cycles -> 'invalid'."""
        by_id = {m.id: (m, p) for m, p in found}
        order, state = [], {}

        def visit(i, chain):
            if state.get(i) == "done":
                return True
            if state.get(i) == "visiting":
                self._set(i, "invalid", "dependency cycle: " + " -> ".join(chain + [i]), *by_id[i])
                return False
            state[i] = "visiting"
            m = by_id[i][0]
            for dep in list(m.depends_on) + [x for x in m.load_after if x in by_id]:
                if dep in by_id and not visit(dep, chain + [i]):
                    state[i] = "done"
                    return False
            state[i] = "done"
            order.append(by_id[i])
            return True

        for i in sorted(by_id):
            visit(i, [])
        return order

    # ------------------------------------------------------------ load
    def load_all(self) -> Dict[str, dict]:
        for m, path in self._order(self.discover()):
            if self.status.get(m.id, {}).get("state") == "invalid":
                continue
            self.load(m, path)
        return self.status

    def load(self, m: Manifest, path: Path) -> bool:
        self.manifests[m.id] = m
        if not m.compatible(SDK_VERSION):
            return self._set(m.id, "incompatible", f"needs Core SDK {m.core}, this Core is {SDK_VERSION}", m, path)
        missing = [d for d in m.depends_on if self.status.get(d, {}).get("state") != "loaded"]
        if missing:
            return self._set(m.id, "missing_dependency", f"needs {', '.join(missing)} (not loaded)", m, path)
        # register the module (and its URL prefixes) BEFORE running its code, so its routes are gated from the start
        prefixes = (m.route_prefix,) + tuple(m.legacy_routes)
        try:
            self.kernel.modules.add(Module(m.id, m.name, m.kind, prefixes, m.description))
        except ValueError as e:
            return self._set(m.id, "invalid", str(e), m, path)
        for key, item in m.config.items():
            if not item.secret:
                full = f"packs.{m.id}.{key}"
                if full not in self.kernel.config.schema:
                    self.kernel.config.declare(full, item.type, item.default, item.description)
        legacy = LegacyBridge(None, self.legacy_services, self.exports) if m.legacy else None
        ctx = ModuleContext(m, self.kernel, legacy)
        if legacy:
            legacy._ctx = ctx
        try:
            mod = self._import(m.id, path)
            if not callable(getattr(mod, "register", None)):
                raise TypeError("__init__.py has no register(ctx) function")
            self.exports[m.id] = mod.register(ctx)
        except Exception as e:
            self.kernel.bus.unsubscribe_module(m.id)
            for name in [n for n, v in self.kernel.tools.items() if v[0] == m.id]:
                self.kernel.tools.pop(name)
            self.kernel.modules.mark_broken(m.id)                 # its routes answer 404 from now on
            log.warning("module %s failed to load: %s", m.id, e)
            return self._set(m.id, "failed", f"{type(e).__name__}: {e}", m, path,
                             trace=traceback.format_exc(limit=4)[-1500:])
        if self.allow_scopes and m.provides_scopes:
            self.allow_scopes({s: f"{m.name}: {s.split(':', 1)[1]}" for s in m.provides_scopes})
        missing_tools = [t.name for t in m.mcp_tools if t.name not in self.kernel.tools]
        if ctx.router.routes:
            self.app.include_router(ctx.router, prefix=m.route_prefix)
        self.contexts[m.id] = ctx
        return self._set(m.id, "loaded", "", m, path, routes=len(ctx.router.routes),
                         warnings=[f"MCP tool {t} declared but has no handler" for t in missing_tools])

    def _import(self, module_id: str, path: Path):
        name = f"vr_rag_modules.{module_id}"
        spec = importlib.util.spec_from_file_location(name, path / "__init__.py", submodule_search_locations=[str(path)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
        return mod

    def _set(self, module_id, state, error="", m: Optional[Manifest] = None, path=None, **extra) -> bool:
        error = "\n".join(line for line in str(error).splitlines()
                           if "errors.pydantic.dev" not in line)[:600]          # short enough for the admin page
        self.status[module_id] = {"id": module_id, "state": state, "error": error, "path": str(path or ""),
                                  "version": m.version if m else None, "kind": m.kind if m else None,
                                  "name": m.name if m else module_id, "core": m.core if m else None,
                                  "legacy": bool(m and m.legacy), "loaded_at": time.time(), **extra}
        if state != "loaded":
            log.warning("[modules] %s: %s - %s", module_id, state, error.splitlines()[0] if error else "")
        return state == "loaded"

    # ------------------------------------------------------------ admin view
    def describe(self, tenant_id: str) -> list:
        out = []
        for mid, st in sorted(self.status.items()):
            m = self.manifests.get(mid)
            out.append(dict(st, enabled=self.kernel.modules.enabled(mid, tenant_id) if st["state"] == "loaded" else False,
                            description=m.description if m else "",
                            routes_prefixes=[m.route_prefix] + list(m.legacy_routes) if m else [],
                            subscribes=list(m.subscribes) if m else [], publishes=list(m.publishes) if m else [],
                            mcp_tools=[t.name for t in m.mcp_tools] if m else [],
                            scopes=list(m.requires_scopes) if m else [],
                            config=sorted(m.config) if m else [], depends_on=list(m.depends_on) if m else [],
                            last_error=self.kernel.bus.errors.get(mid)))
        return out
