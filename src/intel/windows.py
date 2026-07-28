"""Analysis time windows, shared by the view, the aggregator and the killmail store.

These used to live in ``mock_data``; they are not mock-specific, so they moved out
when the view switched to real killmails.
"""

WINDOWS: list[str] = ["30", "90", "180", "365"]
WINDOW_DAYS: dict[str, int] = {"30": 30, "90": 90, "180": 180, "365": 365}
WINDOW_LABELS: dict[str, str] = {"30": "30 days", "90": "90 days", "180": "180 days", "365": "365 days"}
WINDOW_BTN: dict[str, str] = {"30": "30d", "90": "90d", "180": "180d", "365": "365d"}
DEFAULT_WINDOW: str = "90"

# Rendered wherever the store has no source for a value (see TODO.md).
UNAVAILABLE: str = "-"
