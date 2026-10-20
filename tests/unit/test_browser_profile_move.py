import shutil
import sys
from types import SimpleNamespace

import pytest

from aistudio_api.infrastructure.browser import browser_engine


def test_moved_profile_keeps_the_original_browser_identity(tmp_path, monkeypatch):
    original_profile = tmp_path / "old-location" / "profile"
    moved_profile = tmp_path / "new-location" / "profile"
    expected = browser_engine._derive_stable_fingerprint_seed(str(original_profile))
    assert browser_engine._profile_fingerprint_seed(str(original_profile)) == expected
    moved_profile.parent.mkdir()
    shutil.move(str(original_profile), str(moved_profile))
    assert browser_engine._profile_fingerprint_seed(str(moved_profile)) == expected

    launch_options = {}

    def launch(**kwargs):
        launch_options.update(kwargs)
        return "context"

    monkeypatch.setattr(browser_engine.settings, "browser_engine", "chromium")
    monkeypatch.setitem(sys.modules, "cloakbrowser", SimpleNamespace(launch_persistent_context=launch))
    assert browser_engine.sync_launch_persistent_context(str(moved_profile)) == "context"
    assert f"--fingerprint={expected}" in launch_options["args"]
    assert launch_options["user_data_dir"] == str(moved_profile)


def test_invalid_saved_identity_fails_instead_of_silently_changing_it(tmp_path):
    (tmp_path / ".aistudio-fingerprint-seed").write_text("0", encoding="ascii")
    with pytest.raises(ValueError, match="Invalid browser fingerprint seed"):
        browser_engine._profile_fingerprint_seed(str(tmp_path))
