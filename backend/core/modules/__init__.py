"""core.modules - the module registry and the per-tenant on/off gate (Phase 4)."""
from .registry import Module, ModuleGate, ModuleRegistry

__all__ = ["Module", "ModuleRegistry", "ModuleGate"]
