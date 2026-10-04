"""
Import the official `swebench` package's test specs and log parsers without
installing its heavy optional deps (`datasets`, `modal`).

We only use pure functions from swebench (make_test_spec, eval scripts, log
parsers, get_eval_report). Those modules import `datasets` / `modal` at module
level for unrelated features (HF loading, Modal cloud runs). This installs inert
placeholder modules for exactly those two names, and only if they are absent.
Nothing we call touches them.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types

_STUB_ROOTS = ("datasets", "modal")


class _Any:
    """Absorbs any attribute access / call / decoration at import time."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()

    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]  # used as a bare decorator
        return _Any()

    def __mro_entries__(self, bases):
        return (object,)


class _Stub(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Any()


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in _STUB_ROOTS:
            return importlib.machinery.ModuleSpec(fullname, self, is_package=True)
        return None

    def create_module(self, spec):
        m = _Stub(spec.name)
        m.__path__ = []
        return m

    def exec_module(self, module):
        return None


def install() -> None:
    missing = []
    for root in _STUB_ROOTS:
        try:
            __import__(root)
        except ImportError:
            missing.append(root)
    if missing and not any(isinstance(f, _Finder) for f in sys.meta_path):
        sys.meta_path.append(_Finder())


install()
