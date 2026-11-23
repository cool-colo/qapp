from collections.abc import Callable


class ComponentRegistry:
    """Explicit composition registry; configuration never imports arbitrary code."""

    def __init__(self):
        self._factories: dict[tuple[str, str], Callable] = {}

    def register(self, category: str, name: str, factory: Callable) -> None:
        key = category, name
        if key in self._factories:
            raise ValueError(f"duplicate component: {category}/{name}")
        self._factories[key] = factory

    def build(self, category: str, name: str, **dependencies):
        key = category, name
        if key not in self._factories:
            raise ValueError(f"unknown component: {category}/{name}")
        return self._factories[key](**dependencies)

