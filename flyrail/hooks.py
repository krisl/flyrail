"""Minimal React-style hooks for component-local UI state. Sync only.

Rules (React's, enforced by convention + loud errors):
- call hooks unconditionally at the top of a @component body, same order
  every render;
- never call hooks outside a @component body;
- give repeated components distinct key= values.

Renders are serial per Layout. Two Layouts on two threads are fine: the frame
stack is thread-local, so neither can see the other's hook slots.
"""
from __future__ import annotations
import threading
from typing import Any, Callable


class _Stack(threading.local):
    """Frame stack, innermost last, private to the thread that renders.

    One Layout renders serially, but a host with more than one Layout may well
    drive them from different threads -- a tick loop per session is an ordinary
    shape. A module-global stack lets those renders interleave and hands a
    component another component's slots, which surfaces later as impossible
    state rather than as an error.
    """

    def __init__(self):
        self.frames: list = []


_stack = _Stack()


class _Frame:
    __slots__ = ("slots", "index", "schedule")

    def __init__(self, slots: list, schedule: Callable[[], None]):
        self.slots = slots
        self.index = 0
        self.schedule = schedule


def enter(slots: list, schedule: Callable[[], None]) -> _Frame:
    frame = _Frame(slots, schedule)
    _stack.frames.append(frame)
    return frame


def exit() -> None:
    if not _stack.frames:
        raise RuntimeError("hook frames unbalanced (concurrent renders?)")
    _stack.frames.pop()


def _frame() -> _Frame:
    if not _stack.frames:
        raise RuntimeError("hooks may only be called inside a @component body")
    return _stack.frames[-1]


def use_state(initial: Any = None):
    """Component-local state. Returns (value, setter).

    Setter accepts a value or an updater fn. Re-renders are scheduled via
    Layout.invalidate; identical values skip the schedule. Initializer
    callables run once (to store a function itself, wrap it in lambda).
    """
    frame = _frame()
    i = frame.index
    frame.index += 1
    if i >= len(frame.slots):
        frame.slots.append(["state", initial() if callable(initial) else initial])
    cell = frame.slots[i]
    if cell[0] != "state":
        raise RuntimeError(
            "hook order changed between renders: call hooks unconditionally "
            "in the same order every render")
    def set_state(new: Any) -> None:
        old = cell[1]
        nxt = new(old) if callable(new) else new
        try:
            changed = bool(nxt != old)
        except Exception:
            changed = True
        if changed:
            cell[1] = nxt
            frame.schedule()
    return cell[1], set_state


def use_memo(fn: Callable[[], Any], deps: list | tuple):
    """Cached derived value; recomputed only when deps change."""
    frame = _frame()
    i = frame.index
    frame.index += 1
    if i >= len(frame.slots):
        value = fn()
        frame.slots.append(["memo", list(deps), value])
        return value
    cell = frame.slots[i]
    if cell[0] != "memo":
        raise RuntimeError(
            "hook order changed between renders: call hooks unconditionally "
            "in the same order every render")
    if _deps_changed(cell[1], deps):
        cell[1] = list(deps)
        cell[2] = fn()
    return cell[2]


def use_effect(fn: Callable[[], Any], deps: list | tuple) -> None:
    """Run fn after the render commits; re-run it when deps change.

    fn may return a cleanup, which runs before the next run of that effect and
    once more when the component leaves the tree -- which is what makes an
    effect the right place for anything that has to be undone: a highlight, a
    subscription, a lock.

    Synchronous, like the rest of these hooks. A tick host has nowhere to await
    and an async effect would need a loop and a task per effect; running on the
    commit keeps the ordering obvious (cleanup, then run) and keeps flyrail
    free of asyncio in the render path.
    """
    frame = _frame()
    i = frame.index
    frame.index += 1
    if i >= len(frame.slots):
        frame.slots.append(["effect", list(deps), None, fn])
        return
    cell = frame.slots[i]
    if cell[0] != "effect":
        raise RuntimeError(
            "hook order changed between renders: call hooks unconditionally "
            "in the same order every render")
    if _deps_changed(cell[1], deps):
        cell[1] = list(deps)
        cell[3] = fn


def run_effects(slots: list) -> None:
    """Run whatever this component's render scheduled, cleaning up first."""
    for cell in slots:
        if cell[0] != "effect" or cell[3] is None:
            continue
        pending, cell[3] = cell[3], None
        cleanup, cell[2] = cell[2], None
        if cleanup is not None:
            cleanup()
        cell[2] = pending()


def run_cleanups(slots: list) -> None:
    """Unwind this component's effects, for a component leaving the tree."""
    for cell in slots:
        if cell[0] == "effect" and cell[2] is not None:
            cleanup, cell[2] = cell[2], None
            cleanup()


def _deps_changed(old: list, new: list | tuple) -> bool:
    new = list(new)
    if len(old) != len(new):
        return True
    for a, b in zip(old, new):
        if a is b:
            continue
        try:
            if a != b:
                return True
        except Exception:
            return True  # exotic values (ndarray): recompute rather than lie
    return False
