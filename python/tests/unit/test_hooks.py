# SPDX-License-Identifier: Apache-2.0

"""Post-import hooks without wrapt: wrapt's import alone costs ~100 ms in every Python process of the container, and
this package has to start in a few milliseconds. The finder must be invisible to the import system otherwise."""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path

import pytest

from impact_instrumentation import hooks


@pytest.fixture
def modules(tmp_path):
    """A directory on sys.path for throwaway modules; forgets them (and our finder) afterwards."""
    sys.path.insert(0, str(tmp_path))
    before = set(sys.modules)
    finders = list(sys.meta_path)
    importlib.invalidate_caches()
    yield tmp_path
    sys.path.remove(str(tmp_path))
    sys.meta_path[:] = finders
    for name in set(sys.modules) - before:
        del sys.modules[name]


def write(directory: Path, name: str, source: str = "VALUE = 1\n") -> None:
    (directory / f"{name}.py").write_text(source)
    importlib.invalidate_caches()


def test_a_hook_fires_after_the_module_is_imported(modules):
    write(modules, "hooked_a")
    seen = []
    finder = hooks.install()
    finder.watch("hooked_a", lambda module: seen.append((module.__name__, module.VALUE)))
    assert seen == []
    import hooked_a  # noqa: F401

    assert seen == [("hooked_a", 1)]


def test_a_hook_fires_only_once(modules):
    write(modules, "hooked_b")
    seen = []
    finder = hooks.install()
    finder.watch("hooked_b", seen.append)
    import hooked_b  # noqa: F401

    importlib.import_module("hooked_b")
    importlib.reload(sys.modules["hooked_b"])
    assert len(seen) == 1


def test_other_modules_are_not_touched(modules):
    write(modules, "hooked_c")
    write(modules, "unrelated_c")
    finder = hooks.install()
    finder.watch("hooked_c", lambda module: None)
    import unrelated_c

    assert unrelated_c.__spec__.loader.__class__.__name__ == "SourceFileLoader"


def test_a_module_that_is_already_imported_fires_at_once(modules):
    write(modules, "hooked_d")
    import hooked_d

    seen = []
    hooks.install().watch("hooked_d", seen.append)
    assert seen == [hooked_d]


def test_the_real_loader_is_back_once_the_module_has_executed(modules):
    write(modules, "hooked_e", "import sys\nLOADER_DURING = type(sys.modules[__name__].__loader__).__name__\n")
    hooks.install().watch("hooked_e", lambda module: None)
    import hooked_e

    assert hooked_e.LOADER_DURING == "SourceFileLoader"  # the module itself never sees our wrapper
    assert type(hooked_e.__loader__).__name__ == "SourceFileLoader"
    assert type(hooked_e.__spec__.loader).__name__ == "SourceFileLoader"


def test_reload_still_works(modules):
    write(modules, "hooked_f", "VALUE = 1\n")
    hooks.install().watch("hooked_f", lambda module: None)
    import hooked_f

    write(modules, "hooked_f", "VALUE = 22  # a different size, so a cached .pyc cannot be mistaken for current\n")
    assert importlib.reload(hooked_f).VALUE == 22


def test_a_package_fires_once_for_the_package_not_for_its_submodules(modules):
    package = modules / "hooked_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "sub.py").write_text("X = 1\n")
    (package / "data.txt").write_text("hello")
    importlib.invalidate_caches()
    seen = []
    hooks.install().watch("hooked_pkg", seen.append)
    import hooked_pkg.sub

    assert hooked_pkg.sub.X == 1
    assert [m.__name__ for m in seen] == ["hooked_pkg"]
    assert importlib.resources.files("hooked_pkg").joinpath("data.txt").read_text() == "hello"


def test_a_module_that_fails_to_import_does_not_fire_and_can_be_imported_later(modules):
    write(modules, "hooked_g", "raise RuntimeError('broken on purpose')\n")
    seen = []
    hooks.install().watch("hooked_g", seen.append)
    with pytest.raises(RuntimeError, match="broken on purpose"):
        import hooked_g  # noqa: F401
    assert seen == []
    write(modules, "hooked_g", "VALUE = 3\n")
    import hooked_g  # noqa: F811

    assert hooked_g.VALUE == 3
    assert len(seen) == 1


def test_a_failing_hook_never_breaks_the_import(modules):
    write(modules, "hooked_h")
    order = []

    def bad(module):
        order.append("bad")
        raise RuntimeError("hook bug")

    finder = hooks.install()
    finder.watch("hooked_h", bad)
    finder.watch("hooked_h", lambda module: order.append("good"))
    import hooked_h

    assert hooked_h.VALUE == 1
    assert order == ["bad", "good"]  # one hook failing does not stop the next


def test_a_missing_module_still_raises_the_usual_error(modules):
    hooks.install().watch("never_written_i", lambda module: None)
    with pytest.raises(ModuleNotFoundError):
        import never_written_i  # noqa: F401


def test_a_namespace_package_is_left_alone(modules):
    (modules / "hooked_ns").mkdir()
    (modules / "hooked_ns" / "part.py").write_text("Y = 2\n")
    importlib.invalidate_caches()
    hooks.install().watch("hooked_ns", lambda module: None)
    import hooked_ns.part

    assert hooked_ns.part.Y == 2


def test_concurrent_imports_fire_the_hook_once(modules):
    write(modules, "hooked_j", "import time\ntime.sleep(0.2)\nVALUE = 1\n")
    seen = []
    hooks.install().watch("hooked_j", seen.append)
    threads = [threading.Thread(target=importlib.import_module, args=("hooked_j",)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 1


def test_install_twice_returns_the_same_finder(modules):
    assert hooks.install() is hooks.install()
    assert sum(isinstance(f, hooks.Finder) for f in sys.meta_path) == 1
