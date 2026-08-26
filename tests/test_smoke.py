"""
Smoke tests: does the application actually load?

These are deliberately shallow and deliberately broad. They don't test
behaviour -- they catch the class of breakage that unit tests miss
entirely and that only shows up when you launch the app: a module that
no longer imports, a circular import, a name that moved during a
refactor, a QWidget that raises in __init__.

This is the test that would have caught the settings/__init__.py
regression during the package restructure, where the re-export block
had been swallowed into the module docstring and every import of
SettingsPanel failed.

Qt tests are skipped automatically if PyQt6 isn't installed, so the
suite still runs in a lint-only environment.
"""

import glob
import importlib
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _all_modules():
    """Every module in the cctv package, as dotted paths."""
    pattern = os.path.join(REPO_ROOT, "core", "**", "*.py")
    mods = []
    for path in sorted(glob.glob(pattern, recursive=True)):
        rel = os.path.relpath(path, REPO_ROOT)
        mod = rel[:-3].replace(os.sep, ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        if mod:
            mods.append(mod)
    return mods


ALL_MODULES = _all_modules()


def test_module_list_is_not_empty():
    """Guards the test itself: a broken glob would make every
    parametrised import test below silently pass by not running."""
    assert len(ALL_MODULES) > 20, ALL_MODULES


@pytest.mark.parametrize("module", ALL_MODULES)
def test_module_imports(module):
    importlib.import_module(module)


# --- Qt-dependent -----------------------------------------------------

pyqt6 = pytest.importorskip("PyQt6", reason="PyQt6 not installed")


@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the module. Qt does not support creating a
    second one in the same process, so this must not be function-scoped."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_constructs(qapp, tmp_path, monkeypatch):
    """Builds the real MainWindow against a throwaway data directory.

    CCTV_DATA_DIR is redirected so the test never touches a developer's
    actual cameras.db -- and so a failure here can't be caused by
    whatever happens to be in it.
    """
    monkeypatch.setenv("CCTV_DATA_DIR", str(tmp_path))

    from core.ui.app import MainWindow

    window = MainWindow()
    try:
        window.show()
        assert window.isVisible()
    finally:
        window.close()


def test_settings_panel_exports(qapp):
    """The settings package re-exports every section panel.

    Directly regression-guards the docstring-swallowed-the-imports bug.
    """
    import core.ui.settings as settings

    for name in settings.__all__:
        assert hasattr(settings, name), f"{name} missing from core.ui.settings"
