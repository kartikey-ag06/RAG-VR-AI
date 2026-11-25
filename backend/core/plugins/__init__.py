"""core.plugins - how capabilities and packs plug into the Core (Phase 5): manifest loader, module context, event bus."""
from .bus import EventBus
from .context import Kernel, LegacyBridge, ModuleContext
from .loader import PackLoader

__all__ = ["EventBus", "Kernel", "ModuleContext", "LegacyBridge", "PackLoader"]
