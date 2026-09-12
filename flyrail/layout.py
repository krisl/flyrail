"""Transport-agnostic layout: handler registry, wire serialization, diffing."""
from __future__ import annotations
import copy
import functools
import inspect
from typing import Any, Callable

from . import hooks as _hooks


def _unchanged(old: Any, new: Any) -> bool:
    """Whether new says the same as old, erring towards "changed".

    Exotic values (ndarray and friends) return something that is not a bool
    from ==; those are reported as changed rather than guessed at, the same way
    _deps_changed treats them.
    """
    try:
        return bool(old == new)
    except Exception:
        return False


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


def _segment(child: Any, index: int) -> Any:
    """What names a child within its parent: its key, or its position.

    Position alone made a handler id move when its siblings did, so a click on
    a row that had shifted up reached whatever now sat where it used to. A key
    is exactly the promise that this element is the same element, so use it.
    """
    if isinstance(child, dict):
        key = child.get("key")
        if key is not None:
            return key
    return index


def _escape(path: str) -> str:
    return path.replace("~", "~0").replace("/", "~1")


#: Wire defaults for event descriptors. preventDefault is True because a
#: socket-driven control must never trigger browser navigation (reload =
#: dead session); opt out explicitly with prevent_default=False. Shape
#: mirrors reactpy's eventHandlers entries for cross-compat.
EVENT_DEFAULTS = {"preventDefault": True, "stopPropagation": False}


def _keys_of(items: list) -> list | None:
    """The list's element keys, or None if it is not keyed throughout.

    Reconciling by key needs every element to be a keyed dict with keys unique
    within the list; anything else and position is all there is to go on.
    Unhashable keys (a list as a key) cannot be reconciled either -- they
    fall back to replacing, rather than raising mid-diff.
    """
    keys = []
    for item in items:
        if not isinstance(item, dict):
            return None
        key = item.get("key")
        if key is None:
            return None
        keys.append(key)
    try:
        unique = len(set(keys)) == len(keys)
    except TypeError:
        return None
    return keys if unique else None


def _diff_keyed_list(old: list, new: list, path: str, keyed_lists: bool) -> list[dict] | None:
    """Per-element ops for two keyed lists, or None to fall back to a replace.

    Emitted in apply order against a running copy, because RFC6902 array
    pointers are indices: a remove shifts everything after it, so the ops only
    mean anything applied in sequence from the same baseline -- which is what
    the committed tree guarantees.
    """
    old_keys, new_keys = _keys_of(old), _keys_of(new)
    if old_keys is None or new_keys is None:
        return None

    ops: list[dict] = []
    working = list(old)
    keys = list(old_keys)

    wanted = set(new_keys)
    for index in range(len(working) - 1, -1, -1):
        if keys[index] not in wanted:
            ops.append({"op": "remove", "path": f"{path}/{index}"})
            del working[index]
            del keys[index]

    present = set(keys)
    for index, key in enumerate(new_keys):
        if key not in present:
            ops.append({"op": "add", "path": f"{path}/{index}", "value": new[index]})
            working.insert(index, new[index])
            keys.insert(index, key)
            present.add(key)

    if keys != new_keys:
        # Same elements, different order. RFC6902 has `move`, but the ops this
        # emits are add/replace/remove, so a reorder is not expressible here;
        # let the caller replace the list outright rather than emit something
        # a client cannot apply.
        return None

    for index, (before, after) in enumerate(zip(working, new)):
        ops.extend(_diff(before, after, f"{path}/{index}", keyed_lists))
    return ops


def _diff(old: Any, new: Any, path: str = "", keyed_lists: bool = True) -> list[dict]:
    """Minimal RFC6902 diff. Dicts recurse; keyed lists reconcile by key.

    Lists used to replace wholesale, on the reasoning that a client reconciles
    arrays by `key` anyway. That is true of the DOM and irrelevant to the wire:
    the whole list has already crossed the socket by the time the client
    reconciles it, so one changed label re-sent every sibling it had.
    """
    ops: list[dict] = []
    if _unchanged(old, new):
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
                ops.extend(_diff(old[k], v, p, keyed_lists))
        return ops
    if keyed_lists and isinstance(old, list) and isinstance(new, list):
        keyed = _diff_keyed_list(old, new, path, keyed_lists)
        if keyed is not None:
            return keyed
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
                 strict: bool = False, keyed_lists: bool = True,
                 memo: bool = True):
        #: Reconcile keyed lists element by element instead of replacing them
        #: whole. The ops stay within add/replace/remove, and the bundled
        #: client applies array pointers already, so this is on by default.
        #: Turn it off for a client that only understands object pointers.
        self.keyed_lists = keyed_lists
        #: Skip re-running a @pure component whose arguments are unchanged and
        #: nothing under which went stale. Only @pure components are eligible,
        #: so an unmarked component still re-renders every time. Off turns the
        #: whole thing back into an unconditional render, which is what you
        #: want when a stale panel has you suspecting a lying @pure.
        self.memo = memo
        self.render_fn = render_fn
        self.allowed_types = allowed_types
        self.strict = strict
        self.registry: dict[str, Callable] = {}
        self._last_tree: Any = None
        #: The committed tree as rendered, not the defensive copy, so an
        #: unchanged render can be recognised by identity before anything is
        #: compared element by element.
        self._last_source: Any = None
        self._slots: dict[str, Any] = {}
        self._hooks: dict = {}
        self._hooks_seen: dict = {}
        self._hooks_visited: set = set()
        self._hooks_arity: dict = {}
        #: Per-slot memo: slot_id -> (args, kwargs, expanded subtree).
        self._memo: dict = {}
        #: Slots expanded beneath each slot last render, so a component can ask
        #: whether anything under it went stale, not just itself.
        self._subtree: dict = {}
        #: Per-slot serialized form: slot_id -> (expanded, path, tree, handlers).
        #: Keyed on the expanded object and the path it was serialized at, both
        #: of which have to still hold for the cached tree to be the right
        #: answer -- handler ids are built from the path.
        self._serial: dict = {}
        #: id(expanded subtree) -> slot_id, for the slots currently memoised.
        #: Rebuilt each render; the objects are held alive by _memo, so their
        #: ids cannot be recycled underneath it.
        self._memo_root_by_id: dict = {}
        self._expanding: list = []
        self._dirty_slots: set = set()
        #: Slots in the order they finished expanding, which is children before
        #: parents, so an effect sees its children already committed.
        self._effect_order: list = []
        self._version: Any = None
        self._dirty = True

    def invalidate(self) -> None:
        """Mark dirty: the next tick() re-renders regardless of version.
        Scheduling seam for hook dispatch and the Driver.

        Deliberately does not drop memoised subtrees. Driver documents
        invalidate() per tick for tick hosts, so clearing here would mean the
        memo never survives a tick and buys nothing for the main use case.
        Host state reaches a component through its arguments, which the memo
        compares, so anything the host actually changed re-renders on that
        basis. A @pure component that reads host state it was not passed is
        lying, and this is the stale UI the decorator warns about; reach for
        reset() or memo=False when chasing one.
        """
        self._dirty = True

    def reset(self) -> None:
        """Drop every memoised subtree, forcing the next render to run all of
        them. For a host that mutates state components read without being
        handed it, and for bisecting a suspected @pure that is not."""
        self._memo.clear()
        self._serial.clear()

    def _invalidate_slot(self, slot_id: Any) -> None:
        """A hook in this component set new state: only it needs re-running."""
        self._dirty = True
        self._dirty_slots.add(slot_id)

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
        self._run_effects()
        self._dirty = False
        return tree

    def _render_once(self, state: Any) -> dict:
        self._hooks_seen = {}
        self._hooks_visited = set()
        self._effect_order = []
        self._memo_root_by_id = {id(entry[2]): slot_id
                                 for slot_id, entry in self._memo.items()}
        expanded = self._expand(self.render_fn(state))
        for k in list(self._hooks):
            if k not in self._hooks_visited:
                # Leaving the tree: unwind its effects before its state goes.
                _hooks.run_cleanups(self._hooks[k])
                del self._hooks[k]
                self._hooks_arity.pop(k, None)
                # An unmounted component must not leave a memo behind: the same
                # slot_id can be handed to a later instance, which would then
                # start from the old instance's subtree.
                stale = self._memo.pop(k, None)
                if stale is not None:
                    # Dropping the last reference to that subtree frees it, and
                    # a later allocation can be handed the same id. Forget the
                    # mapping rather than let an unrelated node match it.
                    self._memo_root_by_id.pop(id(stale[2]), None)
                self._serial.pop(k, None)
                self._subtree.pop(k, None)
                self._dirty_slots.discard(k)
        collected: dict[str, Callable] = {}
        tree = self._serialize(expanded, path="0", collected=collected)
        self.registry.update(collected)
        if self.allowed_types:
            self._check_allowlist(tree)
        return tree

    def _run_effects(self) -> None:
        """Commit point: effects run once the tree they describe is built.

        A reused subtree scheduled nothing, so nothing of it runs -- which is
        the behaviour you want and falls out for free.
        """
        for slot_id in self._effect_order:
            slots = self._hooks.get(slot_id)
            if slots is not None:
                _hooks.run_effects(slots)

    def _reusable(self, slot_id: Any, fn: Any, node: dict) -> Any:
        """The cached subtree for this component, if reusing it is safe.

        Safe means: it was marked @pure, its arguments still compare equal, its
        own hooks have not been written to, and nothing expanded beneath it has
        either. Strict mode never reuses -- its whole job is to render twice and
        compare, which a cache would quietly turn into one render.
        """
        if not self.memo or self.strict or not getattr(fn, "_flyrail_pure", False):
            return None
        remembered = self._memo.get(slot_id)
        if remembered is None:
            return None
        if slot_id in self._dirty_slots:
            return None
        if self._dirty_slots & self._subtree.get(slot_id, frozenset()):
            return None
        args, kwargs, result = remembered
        if not _unchanged(args, node.get("args", ())):
            return None
        if not _unchanged(kwargs, node.get("kwargs", {})):
            return None
        return result

    def _mark_reused(self, slot_id: Any) -> None:
        """Report a skipped subtree as still mounted.

        The prune at the end of a render deletes hook state for any slot it did
        not see. A reused subtree is never walked, so without this its hooks --
        and every hook under it -- would be collected and the component would
        silently restart from its initial state on the next real render.
        """
        self._hooks_visited.add(slot_id)
        beneath = self._subtree.get(slot_id, frozenset())
        self._hooks_visited.update(beneath)
        for ancestor in self._expanding:
            self._subtree.setdefault(ancestor, set()).update(beneath)

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
            for ancestor in self._expanding:
                self._subtree.setdefault(ancestor, set()).add(slot_id)

            cached = self._reusable(slot_id, fn, node)
            if cached is not None:
                self._mark_reused(slot_id)
                return cached

            slots = self._hooks.setdefault(slot_id, [])
            self._subtree[slot_id] = set()
            self._expanding.append(slot_id)
            frame = _hooks.enter(slots, lambda sid=slot_id: self._invalidate_slot(sid))
            try:
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
                self._dirty_slots.discard(slot_id)
                # Stays on the expanding stack across this call: children are
                # expanded here, and each has to be recorded against every
                # component above it or an ancestor cannot tell that something
                # beneath it went stale.
                result = self._expand(expanded, path)
            finally:
                self._expanding.pop()
            self._effect_order.append(slot_id)
            if self.memo and getattr(fn, "_flyrail_pure", False):
                self._memo[slot_id] = (node.get("args", ()),
                                       node.get("kwargs", {}), result)
                self._memo_root_by_id[id(result)] = slot_id
            return result
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

    def _serialize(self, node: Any, path: str, collected: dict) -> Any:
        """Serialize a subtree, reusing the last result at a memoised root.

        Serialization rebuilds every node carrying a handler, so a subtree that
        _expand handed back untouched still came out as fresh objects and the
        tree was never identical twice. At a memoised root the answer is
        already known: same expanded input, same path, same output. The path
        has to match because handler ids are built from it.

        The registry is still rebuilt from scratch every render -- the ids have
        to stay resolvable -- so a cache hit replays the subtree's
        registrations rather than skipping them.
        """
        slot_id = self._memo_root_by_id.get(id(node)) if isinstance(node, dict) else None
        if slot_id is None:
            return self._serialize_node(node, path, collected)

        cached = self._serial.get(slot_id)
        if cached is not None and cached[0] is node and cached[1] == path:
            collected.update(cached[3])
            return cached[2]

        own: dict[str, Callable] = {}
        out = self._serialize_node(node, path, own)
        self._serial[slot_id] = (node, path, out, own)
        collected.update(own)
        return out

    def _serialize_node(self, node: Any, path: str, collected: dict) -> Any:
        """Swap callables for {"handlerId": ...}, returning a new node only
        where something actually changed.

        This used to mutate a deep copy of the whole tree. Copying every node
        to rewrite the few that carry handlers cost more than the diff did, and
        a subtree that is reused across renders must not be written through at
        all. Rebuilding only the nodes that change keeps untouched subtrees
        identical, which is what lets a caller skip work on them.
        """
        if not isinstance(node, dict):
            return node

        key = node.get("key", "")
        replaced: dict[str, Any] = {}
        for evt in ("on_click", "on_change"):
            fn = node.get(evt)
            if callable(fn):
                # The path is built from keys where children have them, so a
                # widget keeps its id when its siblings move around it.
                hid = f"{path}:{evt}:{key}"
                collected[hid] = fn
                replaced[evt] = {"handlerId": hid,
                                 **{**EVENT_DEFAULTS,
                                    **node.get("event_options", {}).get(evt, {})}}

        children = node.get("children")
        serialized_children = None
        if isinstance(children, list):
            walked = [self._serialize(child, f"{path}.{_segment(child, i)}", collected)
                      for i, child in enumerate(children)]
            if any(a is not b for a, b in zip(walked, children)):
                serialized_children = walked

        if not replaced and serialized_children is None:
            return node

        out = dict(node)
        out.update(replaced)
        if serialized_children is not None:
            out["children"] = serialized_children
        return out

    def _check_allowlist(self, node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("type")
            if t not in ("__Slot__",) and t not in (self.allowed_types or set()):
                raise ValueError(f"node type {t!r} not in allowlist")
            for c in node.get("children", []) or []:
                self._check_allowlist(c)

    def diff_and_commit(self, tree: dict) -> list[dict]:
        """Ops for what changed, or [] when nothing did.

        Three gates, cheapest first. A memoised render hands back the very
        same tree object, so identity settles it outright. Otherwise the
        previous tree has to be kept anyway to diff against, so comparing it is
        cheaper than serialising and hashing it -- and cannot collide, which a
        digest over json.dumps(default=str) can: two values that stringify
        alike hashed alike and the update was silently dropped.
        """
        if tree is self._last_source:
            return []
        if self._last_tree is not None and _unchanged(self._last_tree, tree):
            self._last_source = tree
            return []
        old = self._last_tree if self._last_tree is not None else {}
        ops = _diff(old, tree, path="", keyed_lists=self.keyed_lists)
        self._last_tree = copy.deepcopy(tree)
        self._last_source = tree
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
        self._last_source = tree
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
        """Hot path bypassing the diff: unchanged values return None.

        Compared, not hashed, for the same reason as the tree: the last value
        is kept regardless, so the digest bought nothing and could collide.
        """
        if name in self._slots and _unchanged(self._slots[name], value):
            return None
        self._slots[name] = value
        return {"chan": "ui", "type": "slot", "name": name, "value": value}
