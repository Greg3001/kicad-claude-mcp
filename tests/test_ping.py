"""Smoke test for the Phase 0 ping tool."""

from server import ping


def test_ping_returns_pong():
    assert ping() == "pong"
