# SPDX-License-Identifier: Apache-2.0

"""Post-import hooks on the standard library only.

wrapt offers the same, but importing it costs ~100 ms and this package starts in every Python process of the
container (exec probes included). The finder below sits first on sys.meta_path and only acts for the modules it was
asked to watch: it asks the other finders for the real spec, lets the real loader execute the module (restoring it
before anything runs, so the module and the import system only ever see the real loader), and then calls the hooks. A
failing hook never reaches the import.

A watched module is a top-level module (`openai`) or a package inside another one (`google.genai`, whose parent `google`
is a namespace package that many distributions share and that has no loader to wait for).
"""

from __future__ import annotations

import sys


class _Loader:
    """Stands in for the real loader of a watched module until the moment it executes."""

    def __init__(self, loader, finder: Finder, name: str) -> None:
        self._loader = loader
        self._finder = finder
        self._name = name

    def create_module(self, spec):
        create = getattr(self._loader, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module) -> None:
        loader = self._loader
        module.__loader__ = loader
        if getattr(module, "__spec__", None) is not None:
            module.__spec__.loader = loader
        loader.exec_module(module)
        self._finder.fire(self._name, module)

    def __getattr__(self, attribute: str):
        return getattr(self._loader, attribute)


class Finder:
    """A meta path finder that calls hooks once, after a watched module has been imported."""

    def __init__(self) -> None:
        self._hooks: dict[str, list] = {}
        self._fired: set[str] = set()

    def watch(self, name: str, hook) -> None:
        """Call hook(module) after `name` (a module's full dotted name) is imported; at once if it already is."""
        self._hooks.setdefault(name, []).append(hook)
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        if module is not None and not getattr(spec, "_initializing", False):
            self.fire(name, module)

    def fire(self, name: str, module) -> None:
        if name in self._fired:
            return
        self._fired.add(name)
        for hook in self._hooks.get(name, ()):
            try:
                hook(module)
            except Exception:  # noqa: BLE001, S110 - a hook must never break the application's import
                pass

    def find_spec(self, fullname, path=None, target=None):
        # `path` is None for a top-level module and the parent's __path__ for a submodule: either way it goes to the
        # other finders unchanged.
        if fullname not in self._hooks or fullname in self._fired:
            return None
        for finder in sys.meta_path:
            if finder is self:
                continue
            find = getattr(finder, "find_spec", None)
            if find is None:
                continue
            spec = find(fullname, path, target)
            if spec is None:
                continue
            if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                spec.loader = _Loader(spec.loader, self, fullname)
            return spec
        return None


def install() -> Finder:
    """The process-wide finder, put first on sys.meta_path on first use."""
    for finder in sys.meta_path:
        if isinstance(finder, Finder):
            return finder
    finder = Finder()
    sys.meta_path.insert(0, finder)
    return finder
