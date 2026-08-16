"""The package root stays a deliberately small runtime front door."""

import lup


def test_every_export_resolves() -> None:
    missing = [name for name in lup.__all__ if getattr(lup, name, None) is None]
    assert missing == []


def test_root_exports_only_portable_runtime_conveniences() -> None:
    assert set(lup.__all__) == {  # lup: ignore[set-shape] — exact export comparison
        "SessionFactory",
        "SessionHandle",
        "TurnHandle",
        "TurnInput",
        "TurnRequest",
        "TurnResult",
        "query",
        "turn_request",
    }
