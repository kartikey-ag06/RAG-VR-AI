"""Container - one place that builds and holds the backend's shared services (database, store, auth, ...).

Why: app.py used to create them as loose module globals at import time, so nothing could swap one out (for a test,
or later per tenant) and every module received a different hand-picked list of them.

How it works
  - register(name, factory): factory(container) builds the service; it can ask the container for others.
  - get(name): builds the service the first time (once, thread-safe), then returns the same object.
  - override(name, obj): replace a service (tests). Overrides win over factories.
"""
import threading
from typing import Any, Callable, Dict

Factory = Callable[["Container"], Any]


class ServiceError(RuntimeError):
    pass


class Container:
    def __init__(self):
        self._factories: Dict[str, Factory] = {}
        self._instances: Dict[str, Any] = {}
        self._building: list = []
        self._lock = threading.RLock()

    def register(self, name: str, factory: Factory) -> None:
        with self._lock:
            if name in self._factories:
                raise ServiceError(f"service {name!r} is already registered")
            self._factories[name] = factory

    def override(self, name: str, obj: Any) -> None:
        with self._lock:
            self._instances[name] = obj

    def has(self, name: str) -> bool:
        return name in self._factories or name in self._instances

    def built(self, name: str) -> bool:
        return name in self._instances

    def get(self, name: str) -> Any:
        with self._lock:
            if name in self._instances:
                return self._instances[name]
            if name not in self._factories:
                raise ServiceError(f"unknown service {name!r}; registered: {sorted(self._factories)}")
            if name in self._building:
                chain = " -> ".join(self._building + [name])
                raise ServiceError(f"services depend on each other in a loop: {chain}")
            self._building.append(name)
            try:
                obj = self._factories[name](self)
            finally:
                self._building.pop()
            self._instances[name] = obj
            return obj

    def names(self) -> list:
        return sorted(set(self._factories) | set(self._instances))
