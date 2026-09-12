"""Transport-agnostic layout: handler registry, wire serialization, diffing."""
from __future__ import annotations
import copy
import functools
import hashlib
import inspect
import json
from typing import Any, Callable

from . import hooks as _hooks


def _hash(tree: Any) -> str:
    return hashlib.sha256(json.dumps(tree, sort_keys=True, default=str).encode()).hexdigest()


def _is_async(fn: Any) -> bool:
    """Whether calling fn starts a coroutine, without calling it.

    functools.partial and callable objects wrap the real function, so unwrap
    before asking; otherwise a partial around an async def reads as sync.
    """
    target = fn
    while isinstance(target, functools.partial):
        target = target.func
    if inspect.iscoroutinefunction(target):
        return True
    call = getattr(target, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


def _escape(path: str) -> str:
    return path.replace("~", "~0").replace("/", "~1")


#: Wire defaults for event descriptors. preventDefault is True because a
#: socket-driven control must never trigger browser navigation (reload =
#: dead session); opt out explicitly with prevent_default=False. Shape
#: mirrors reactpy's eventHandlers entries for cross-compat.
EVENT_DEFAULTS = {"preventDefault": True, "stopPropagation": False}


def _diff(old: Any, new: Any, path: str = "") -> list[dict]:
    """Minimal RFC6902 diff. Dicts recurse; lists replace wholesale (React
    reconciles arrays via `key` client-side, so index patches would be waste).
    Hot per-tick values bypass this entirely via Slots (next commit)."""
    ops: list[dict] = []
    if old == new:
        return ops
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old:
            if k not in new:
                ops.append({"op": "remove", "path": f"{path}/{_escape(k)}" or "/"})
        for k, v in new.items():
            p = f"{path}/{_escape(k)}"
            if k not in old:
                ops.append({"op": "add", "path": p, "value": v})
            else:
                ops.extend(_diff(old[k], v, p))
        return ops
    return [{"op": "replace", "path": path or "/", "value": new}]


class Layout:
    """One per session. Bring your own socket/tick.

    ``render(state)`` serializes callables to ``{"handlerId": ...}``;
    ``diff_and_commit(tree)`` returns RFC6902-ish ops, or ``[]`` when nothing
    changed so the tick loop sends nothing; ``tick(state)`` does both.
    ``dispatch(handlerId, state, event)`` routes client actions back to the
    registered Python callable; ``set_slot(name, value)`` pushes hot per-tick
    values past the diff entirely.
    """

    def __init__(self, render_fn: Callable[[Any], dict], allowed_types: set[str] | None = None,
                 strict: bool = False):
        self.render_fn = render_fn
        self.allowed_types = allowed_types
        self.strict = strict
        self.registry: dict[str, Callable] = {}
        self._last_tree: Any = None
        self._last_hash: str | None = None
        self._slots: dict[str, Any] = {}
        self._hooks: dict = {}
        self._hooks_seen: dict = {}
        self._hooks_visited: set = set()
        self._hooks_arity: dict = {}
        self._version: Any = None
        self._dirty = True

    def invalidate(self) -> None:
        """Mark dirty: the next tick() re-renders regardless of version.
        Scheduling seam for hook dispatch and the future Driver."""
        self._dirty = True

    def render(self, state: Any) -> dict:
        self.registry.clear()
        tree = self._render_once(state)
        if self.strict and getattr(self.render_fn, "_flyrail_pure", False):
            again = self._render_once(state)
            if again != tree:
                raise AssertionError(
                    "render_fn marked @pure produced different trees across "
                    "two immediate renders; remove @pure or eliminate the "
                    "nondeterminism (time, random, counters, unversioned reads)")
        self._dirty = False
        return tree

    def _render_once(self, state: Any) -> dict:
        self._hooks_seen = {}
        self._hooks_visited = set()
        expanded = self._expand(self.render_fn(state))
        for k in list(self._hooks):
            if k not in self._hooks_visited:
                del self._hooks[k]
                self._hooks_arity.pop(k, None)
        tree = copy.deepcopy(expanded)
        self._serialize(tree, path="0")
        if self.allowed_types:
            self._check_allowlist(tree)
        return tree

    def _expand(self, node: Any, path: str = "0") -> Any:
        if isinstance(node, dict) and node.get("type") == "__Component__":
            fn = node["fn"]
            key = node.get("key")
            slot_id = (id(fn), key if key is not None else path)
            count = self._hooks_seen.get(slot_id, 0) + 1
            self._hooks_seen[slot_id] = count
            if count > 1:
                raise ValueError(
                    f"duplicate component key {key!r} for "
                    f"{getattr(fn, '__name__', fn)}; keys must be unique "
                    "per component within one render")
            slots = self._hooks.setdefault(slot_id, [])
            frame = _hooks.enter(slots, self.invalidate)
            try:
                expanded = fn(*node.get("args", ()), **node.get("kwargs", {}))
            finally:
                arity_ok = (frame.index == len(slots)
                            and frame.index == self._hooks_arity.setdefault(slot_id, frame.index))
                _hooks.exit()
            if not arity_ok:
                raise RuntimeError(
                    "hook count changed between renders: call hooks "
                    "unconditionally in the same order every render")
            self._hooks_visited.add(slot_id)
            return self._expand(expanded, path)
        if isinstance(node, dict):
            out = dict(node)
            children = out.get("children")
            if isinstance(children, list):
                out["children"] = [self._expand(c, f"{path}.{i}")
                                   for i, c in enumerate(children)]
            return out
        if isinstance(node, list):
            return [self._expand(c, f"{path}.{i}") for i, c in enumerate(node)]
        return node

    def _serialize(self, node: Any, path: str) -> None:
        if not isinstance(node, dict):
            return
        key = node.get("key", "")
        for evt in ("on_click", "on_change"):
            fn = node.get(evt)
            if callable(fn):
                # Path keeps ids unique per position; key keeps them stable
                # across list reorders (client reconciles via `key` too).
                hid = f"{path}:{evt}:{key}"
                self.registry[hid] = fn
                node[evt] = {"handlerId": hid,
                             **{**EVENT_DEFAULTS,
                                **node.get("event_options", {}).get(evt, {})}}
        for i, c in enumerate(node.get("children", []) or []):
            self._serialize(c, f"{path}.{i}")

    def _check_allowlist(self, node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t not in ("__Slot__",) and t not in (self.allowed_types or set()):
                raise ValueError(f"node type {t!r} not in allowlist")
            for c in node.get("children", []) or []:
                self._check_allowlist(c)

    def diff_and_commit(self, tree: dict) -> list[dict]:
        h = _hash(tree)
        if h == self._last_hash:
            return []
        old = self._last_tree if self._last_tree is not None else {}
        ops = _diff(old, tree, path="")
        self._last_tree = copy.deepcopy(tree)
        self._last_hash = h
        return ops

    def tick(self, state: Any, version: Any = None) -> list[dict]:
        """Render + diff. Pure renders skip render CPU when the host version
        matches the last rendered version and nothing invalidated since.
        Unmarked renders always re-render (correct by default)."""
        if (version is not None
                and version == self._version
                and not self._dirty
                and getattr(self.render_fn, "_flyrail_pure", False)):
            return []
        tree = self.render(state)
        self._version = version
        return self.diff_and_commit(tree)

    def snapshot(self, state: Any, seq: int) -> dict:
        """Full-tree recovery message answering a client resync-request.

        Re-renders, re-registers handlers, and resets the diff baseline so
        subsequent ticks stay incremental from the snapshot point.
        """
        tree = self.render(state)
        self._last_tree = copy.deepcopy(tree)
        self._last_hash = _hash(tree)
        return {"chan": "ui", "type": "snapshot", "seq": seq, "tree": tree}

    def dispatch(self, handler_id: str, state: Any, event: Any = None) -> None:
        """Run a sync handler; refuse an async one.

        Two different author mistakes used to give the same message. A handler
        that *is* async is caught on the function, before a throwaway coroutine
        is built; a sync handler that *returns* an awaitable can only be caught
        after calling it, and its synchronous part has necessarily already run.
        Saying which happened is the difference between "use adispatch" and
        "you have a half-applied handler".
        """
        fn = self.registry.get(handler_id)
        if fn is None:
            raise KeyError(f"unknown handler {handler_id!r}")
        if _is_async(fn):
            raise RuntimeError(
                f"handler {handler_id!r} is async; use await adispatch() "
                "instead of dispatch()")
        res = fn(state, event)
        if inspect.isawaitable(res):
            # A sync function that returns an awaitable: nothing of the
            # awaitable has run, so closing it really does cancel it.
            if inspect.iscoroutine(res):
                res.close()
            raise RuntimeError(
                f"handler {handler_id!r} returned an awaitable; use await "
                "adispatch() instead of dispatch()")

    async def adispatch(self, handler_id: str, state: Any, event: Any = None) -> Any:
        """Dispatch both sync and async handlers; awaits awaitables.
        Async-host pattern: await layout.adispatch(...) then invalidate()."""
        fn = self.registry.get(handler_id)
        if fn is None:
            raise KeyError(f"unknown handler {handler_id!r}")
        res = fn(state, event)
        if inspect.isawaitable(res):
            res = await res
        return res

    def set_slot(self, name: str, value: Any) -> dict | None:
        """Hot path bypassing the diff: unchanged values return None."""
        h = _hash(value)
        if self._slots.get(name, {}).get("hash") == h:
            return None
        self._slots[name] = {"hash": h, "value": value}
        return {"chan": "ui", "type": "slot", "name": name, "value": value}
