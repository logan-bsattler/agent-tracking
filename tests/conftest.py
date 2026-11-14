"""The landing checks for Ben's queue (queue.check_landing, settle_landing,
2026-11-14) are on by default on a real board. Most tests here hand Ben
synthetic items ("step 0", "Approve the plan") to exercise something else, so
they run with those checks off; a test about the checks themselves takes the
`ben_checks` marker and gets the board's real defaults."""

from __future__ import annotations

import pytest

from coord_mcp import queue


def pytest_configure(config):
    config.addinivalue_line("markers", "ben_checks: run with the Ben-queue landing checks at their real defaults")


@pytest.fixture(autouse=True)
def _ben_checks_off(request, monkeypatch):
    if request.node.get_closest_marker("ben_checks") is None:
        monkeypatch.setattr(queue, "SWITCHES", {k: False for k in queue.SWITCHES})
