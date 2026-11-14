"""Capture: a Playwright recorder for demonstrations (DOM + same-origin network + navigation).

Control-plane entry points live in `capture.session`.
"""

from capture.session import RecordingSession, get_session, list_recordings, start_recording

__all__ = ["RecordingSession", "get_session", "list_recordings", "start_recording"]
