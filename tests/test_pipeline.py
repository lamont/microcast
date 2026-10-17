from __future__ import annotations

from microcast import pipeline


def test_git_sha_prefers_the_image_env(monkeypatch):
    monkeypatch.setenv("MICROCAST_GIT_SHA", "abc1234")
    assert pipeline._git_sha() == "abc1234"
