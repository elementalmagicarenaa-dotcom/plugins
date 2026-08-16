"""
Modmail loader entrypoint for the event request plugin.

Keep this file beside event_requests.py when installing the plugin, or rename
event_requests.py to event.py and use that file directly.
"""

from .event_requests import setup


__all__ = ["setup"]
