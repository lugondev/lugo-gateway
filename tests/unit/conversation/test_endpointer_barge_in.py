"""Barge-in grace: ignore an echo-driven speech onset right after the assistant
starts speaking, but allow a genuine barge-in once the grace window passes."""

from app.services.conversation.endpointer import barge_in_suppressed


def test_not_speaking_never_suppresses():
    # Assistant isn't speaking -> any detected speech is a real user turn.
    assert barge_in_suppressed(None, now=100.0, grace_ms=500) is False


def test_within_grace_is_suppressed():
    # 200ms after the assistant started speaking, < 500ms grace -> likely echo.
    assert barge_in_suppressed(speaking_since=100.0, now=100.2, grace_ms=500) is True


def test_after_grace_is_allowed():
    # 600ms in, past the 500ms grace -> a real barge-in, let it through.
    assert barge_in_suppressed(speaking_since=100.0, now=100.6, grace_ms=500) is False


def test_zero_grace_disables_suppression():
    # grace_ms=0 -> barge-in from the very first frame (opt-out).
    assert barge_in_suppressed(speaking_since=100.0, now=100.0, grace_ms=0) is False
