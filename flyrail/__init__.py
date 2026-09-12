"""flyrail - BYOF server-driven UI core.

Python API is reactpy-style but transport/V DOM agnostic:
  - you build dict trees with helpers (Stack, Text, Button, ...)
  - Layout.render(state) replaces callables with {"handlerId": ...} descriptors
"""
from .core import component, pure, Slot, Stack, Text, Button, TextField
from .layout import Layout
from .driver import Driver
from .hooks import use_state, use_memo, use_effect
from .asgi import create_ws_app

__all__ = ["component", "pure", "Slot", "Stack", "Text", "Button", "TextField", "Layout", "Driver", "use_state", "use_memo", "use_effect", "create_ws_app"]
