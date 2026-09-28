"""Safe custom extension loader and runtime registry.

Extensions live in ``custom_extensions/<name>.py`` and expose either
``setup(registry)`` or ``async setup(registry)``. A broken extension is isolated:
its registrations are rolled back and the bot continues to start.
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

log = logging.getLogger(__name__)

KINDS = (
    "actions", "action_policies", "guards", "hooks", "pricing",
    "promo_rewards", "referral_rewards", "key_lifecycle", "completion_handlers",
    "core_events", "delayed_tasks", "payment_providers", "callbacks",
    "command_handlers", "user_access", "settings", "pages",
)


@dataclass(slots=True)
class ExtensionError:
    name: str
    stage: str
    error: str


@dataclass(slots=True)
class RuntimeMetric:
    registered: int = 0
    calls: int = 0
    failures: int = 0
    timeouts: int = 0
    rejected: int = 0
    skipped: int = 0


@dataclass(slots=True)
class ExtensionReport:
    enabled: bool = True
    folder_found: bool = False
    attempted: int = 0
    loaded: list[str] = field(default_factory=list)
    private_skipped: int = 0
    errors: list[ExtensionError] = field(default_factory=list)
    registrations: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    last_load_at: float | None = None


class ExtensionRegistry:
    """Typed-by-kind registry with ownership, duplicate checks and metrics."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {kind: {} for kind in KINDS}
        self._owners: dict[tuple[str, str], str] = {}
        self.metrics: dict[str, RuntimeMetric] = {kind: RuntimeMetric() for kind in KINDS}
        self._current_owner = "core"

    def register(self, kind: str, name: str, value: Any, *, replace: bool = False) -> Any:
        if kind not in self._items:
            raise ValueError(f"Unknown registry kind: {kind}")
        if not name or not isinstance(name, str):
            raise ValueError("Registration name must be a non-empty string")
        if name in self._items[kind] and not replace:
            raise ValueError(f"Duplicate registration: {kind}:{name}")
        self._items[kind][name] = value
        self._owners[kind, name] = self._current_owner
        self.metrics[kind].registered = len(self._items[kind])
        return value

    def unregister_owner(self, owner: str) -> None:
        for (kind, name), item_owner in list(self._owners.items()):
            if item_owner == owner:
                self._items[kind].pop(name, None)
                self._owners.pop((kind, name), None)
                self.metrics[kind].registered = len(self._items[kind])

    def get(self, kind: str, name: str, default: Any = None) -> Any:
        return self._items.get(kind, {}).get(name, default)

    def items(self, kind: str) -> tuple[tuple[str, Any], ...]:
        return tuple(self._items.get(kind, {}).items())

    def counts(self) -> dict[str, int]:
        return {kind: len(values) for kind, values in self._items.items()}

    def owner_counts(self, owner: str) -> Counter[str]:
        result: Counter[str] = Counter()
        for (kind, _), item_owner in self._owners.items():
            if item_owner == owner:
                result[kind] += 1
        return result

    async def invoke(self, kind: str, name: str, *args: Any, timeout: float = 10, **kwargs: Any) -> Any:
        metric = self.metrics[kind]
        handler = self.get(kind, name)
        if handler is None:
            metric.skipped += 1
            raise KeyError(f"Not registered: {kind}:{name}")
        metric.calls += 1
        try:
            result = handler(*args, **kwargs) if callable(handler) else handler
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            metric.timeouts += 1
            raise
        except Exception:
            metric.failures += 1
            raise

    # Convenience API used by extension authors.
    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("register_"):
            kind = name[9:]
            if kind in self._items:
                return lambda item_name, value, **kw: self.register(kind, item_name, value, **kw)
        raise AttributeError(name)


class ExtensionManager:
    def __init__(self, folder: str | Path = "custom_extensions") -> None:
        self.folder = Path(folder)
        self.registry = ExtensionRegistry()
        self.report = ExtensionReport()
        self.modules: dict[str, ModuleType] = {}

    async def load(self, enabled: bool = True) -> ExtensionReport:
        self.report = ExtensionReport(enabled=enabled, folder_found=self.folder.is_dir())
        if not enabled or not self.folder.is_dir():
            self.report.last_load_at = time.time()
            return self.report
        for path in sorted(self.folder.glob("*.py")):
            if path.name.startswith("_"):
                self.report.private_skipped += 1
                continue
            self.report.attempted += 1
            await self._load_one(path)
        self.report.last_load_at = time.time()
        return self.report

    async def _load_one(self, path: Path) -> None:
        name = path.stem
        module_name = f"custom_extensions.{name}"
        self.registry._current_owner = name
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("Cannot create import specification")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError("Extension must export setup(registry)")
            result = setup(self.registry)
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=15)
            self.modules[name] = module
            self.report.loaded.append(name)
            self.report.registrations[name] = self.registry.owner_counts(name)
        except Exception as exc:  # isolate extension failure
            self.registry.unregister_owner(name)
            self.report.errors.append(ExtensionError(name, "load", f"{type(exc).__name__}: {exc}"))
            log.exception("Extension %s failed to load", name)
        finally:
            self.registry._current_owner = "core"

    async def disable(self, name: str) -> bool:
        module = self.modules.pop(name, None)
        if module is None:
            return False
        teardown = getattr(module, "teardown", None)
        if callable(teardown):
            try:
                result = teardown(self.registry)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=10)
            except Exception:
                log.exception("Extension %s teardown failed", name)
        self.registry.unregister_owner(name)
        if name in self.report.loaded:
            self.report.loaded.remove(name)
        self.report.registrations.pop(name, None)
        return True

    def classify_pages(self) -> Counter[str]:
        result: Counter[str] = Counter()
        for name, page in self.registry.items("pages"):
            category = getattr(page, "category", None)
            if category not in {"core", "custom", "legacy"}:
                category = "custom" if self.registry._owners.get(("pages", name)) != "core" else "core"
            result[category] += 1
        result.setdefault("unknown", 0)
        return result


extension_manager = ExtensionManager()
