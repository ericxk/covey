"""Old import name: ``harness_bridge.X`` is the same module as ``covey.X`` (kept for one release for extensions).

The product and its package were renamed Covey. This single file (not a package, so covey/__init__.py stays
empty) installs a meta path finder: ``import harness_bridge.core.state`` returns the already-loaded
``covey.core.state`` module object, so patches, isinstance checks and module globals are shared between both
names. The first use prints one deprecation line to stderr. New code imports ``covey``.
"""
import importlib
import importlib.abc
import importlib.machinery
import sys

OLD = "harness_bridge"
NEW = "covey"
__path__: list = []  # Makes this module a package for the import system; the finder does the real work.
_warned = False


def _warn_once():
    global _warned
    if not _warned:
        _warned = True
        print("harness_bridge is the old name of the covey package: import covey instead "
              "(the old name is kept for one release)", file=sys.stderr)


class _Alias(importlib.abc.Loader):
    def __init__(self, real):
        self.real = real
        self.name, self.spec = real.__name__, real.__spec__

    def create_module(self, spec):
        return self.real

    def exec_module(self, module):
        # The import system has just overwritten __spec__ etc. with the alias spec: put the real ones back.
        spec = self.spec
        module.__spec__, module.__name__, module.__loader__ = spec, self.name, spec.loader


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(OLD + "."):
            return None
        _warn_once()
        try:
            real = importlib.import_module(NEW + fullname[len(OLD):])
        except ModuleNotFoundError as error:
            if error.name == NEW + fullname[len(OLD):]:
                return None
            raise
        spec = importlib.machinery.ModuleSpec(fullname, _Alias(real), is_package=hasattr(real, "__path__"))
        return spec


if not any(isinstance(finder, _Finder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _Finder())
_warn_once()
