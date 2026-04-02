"""Lazy loader for deferred module loading to reduce startup time."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

T = TypeVar("T")


class LazyLoader:
    """Lazy loader - defers loading until first access to reduce startup time.

    Usage:
        provider = LazyLoader(lambda: OpenAIProvider(config))
        # Provider not created yet
        result = await provider().chat(messages)  # Created here on first call
    """

    __slots__ = ("_loader", "_loaded")

    def __init__(self, loader: Callable[[], T]):
        self._loader = loader
        self._loaded: T | None = None

    def __call__(self) -> T:
        """Load and return the object on first call."""
        if self._loaded is None:
            self._loaded = self._loader()
        return self._loaded

    @property
    def is_loaded(self) -> bool:
        """Check if the object has been loaded."""
        return self._loaded is not None

    def preload(self) -> T:
        """Force load the object immediately."""
        return self()


class LazyImport:
    """Lazy import wrapper - defers import until first attribute access.

    Usage:
        MyClass = LazyImport("mymodule", "MyClass")
        obj = MyClass()  # Actually imports here
    """

    __slots__ = ("_module_name", "_attr_name", "_imported")

    def __init__(self, module_name: str, attr_name: str):
        self._module_name = module_name
        self._attr_name = attr_name
        self._imported: Any = None

    def __getattr__(self, name: str) -> Any:
        if self._imported is None:
            import importlib
            mod = importlib.import_module(self._module_name)
            self._imported = getattr(mod, self._attr_name)
        return getattr(self._imported, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._imported is None:
            import importlib
            mod = importlib.import_module(self._module_name)
            self._imported = getattr(mod, self._attr_name)
        return self._imported(*args, **kwargs)
