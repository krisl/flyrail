# What each piece buys you

One section per capability: what it is, the problem it removes, and the
smallest example that shows it. Every example is self-contained and runs
against the library as it stands — no host, no socket, no frontend.

Numbers are measured on the panel each section builds, not estimated.

---

## 1. Components and hooks

Function components with per-instance state, so a panel is declared once and
re-declared from its own state rather than mutated from the outside.

A component is a function returning elements. `use_state` gives it state that
survives between renders, keyed by position or by an explicit `key=`. Handlers
take `(state, event)` — host state is threaded in, never read off `self`.

```python
from flyrail import Layout, Stack, Text, Button, component, use_state

@component
def Counter(label):
    count, set_count = use_state(0)
    return Stack(
        Text(f"{label}: {count}", key="t"),
        Button("+1", on_click=lambda state, event: set_count(count + 1), key="b"),
    )

layout = Layout(lambda state: Counter("clicks", key="c"))
tree = layout.render(None)
print(tree["children"][0]["props"]["value"])          # clicks: 0

layout.dispatch(tree["children"][1]["on_click"]["handlerId"], None, None)
print(layout.render(None)["children"][0]["props"]["value"])   # clicks: 1
```

The component was never told to update. It set its own state, and the next
render read it back.

---

## 2. `use_effect` — setting something up, and undoing it

Anything a component turns on and has to turn off again — a highlight, a
subscription, a lock — belongs to the component's lifetime, not to whichever
handler happened to switch it on.

The effect runs after the render commits. What it returns runs before the
effect runs again, and once more when the component leaves the tree.

```python
from flyrail import Layout, Stack, Text, component, use_effect

log = []

@component
def Highlighted(uri):
    def effect():
        log.append(f"highlight {uri}")
        return lambda: log.append(f"clear {uri}")
    use_effect(effect, [uri])
    return Text(uri)

@component
def Panel(selected):
    return Stack(Highlighted(selected, key="h")) if selected else Stack()

layout = Layout(Panel)
layout.render("vise1")
layout.render("vise2")
layout.render(None)
print(log)
# ['highlight vise1', 'clear vise1', 'highlight vise2', 'clear vise2']
```

Without this, the clear lives in whichever handler deselects — and gets
forgotten on the path that deletes instead. The cleanup cannot be forgotten
because it is attached to the thing it undoes.

Effects are synchronous: a tick host has nowhere to await, and the ordering
(clean up, then run) stays obvious.

---

## 3. `@pure` and per-component memoization

Mark a component as a function of its arguments and its hooks, and an unchanged
one stops re-running.

```python
from flyrail import Layout, Stack, Text, Button, component, pure, use_state

runs = []

@component
@pure
def Row(index, label, selected):
    use_state(0)
    runs.append(index)
    return Stack(
        Text(label, key="t"),
        Button("pick", on_click=lambda s, e: None, key="b",
               variant="contained" if selected else "outlined"),
        key=f"r{index}")

@component
def Board(picked):
    return Stack(*[Row(i, f"Row {i}", picked == i, key=i) for i in range(12)])

layout = Layout(Board)
layout.render(-1)
runs.clear()

layout.render(3)
print(runs)     # [3]
```

Twelve rows on the panel, one body ran. `Board` is unmarked so it re-runs every
time and rebuilds the twelve `Row(...)` calls; each `Row` compares its own
arguments and only the one whose `selected` moved does any work.

Unchanged render, twelve rows: **161.68 µs → 5.41 µs**.

`@pure` is a declaration, not a proof. A component that reads state it was not
handed will go stale — that is the bug the decorator warns about, and
`memo=False` or `reset()` is how you go looking for it.

---

## 4. The four gates: version, memo, identity, compare

Four cheap checks in front of an expensive render-and-diff, tried in order of
what they cost. They are complementary, not alternatives.

| gate | cost | skips | needs |
|---|---|---|---|
| version | ~0.2 µs | the whole render | a host version token, `@pure` root |
| memo | per component | that component's body | `@pure`, unchanged arguments |
| identity | one `is` | the diff | a memoised root |
| compare | one `==` | the diff | nothing |

```python
from flyrail import Layout, Text, component, pure

renders = []

@pure
@component
def Panel(state):
    renders.append(state)
    return Text(str(state))

layout = Layout(Panel)
layout.tick("a", version=1)
for _ in range(20):
    layout.tick("a", version=1)          # version gate: never rendered
print(len(renders))                       # 1

print(layout.tick("a", version=2))        # []
print(len(renders))                       # 1
```

Read the last two lines carefully, because all three gates are visible in them.
The version token moved, so the first gate could not fire and `render()` ran.
The memo then found `Panel`'s arguments unchanged and skipped its body anyway —
`renders` stays at 1. And the tree that came back said what the last one said,
so the compare gate returned `[]` without diffing.

That is the point of the layering: the version gate is cheapest and bluntest,
believing a host that says nothing changed. The memo is the middle tier — when
the token *has* moved, it still skips the components whose arguments did not.
The compare gate is the backstop that needs no cooperation at all.

Between the last two sits identity. When a render reuses every subtree it had,
the tree that comes back is *the same object*, so the gate is an `is` rather
than a walk of two trees:

```python
from flyrail import Layout, Stack, Text, Button, component, pure, use_state

@component
@pure
def Row(index, label, selected):
    use_state(0)
    return Stack(
        Text(label, key="t"),
        Button("pick", on_click=lambda s, e: None, key="b",
               variant="contained" if selected else "outlined"),
        key=f"r{index}")

@component
@pure
def Board(picked):
    return Stack(*[Row(i, f"Row {i}", picked == i, key=i) for i in range(12)])

layout = Layout(Board)
first = layout.render(-1)
print(layout.render(-1) is first)            # True

changed = layout.render(3)
print(changed["children"][3] is first["children"][3])   # False — the one that moved
print(changed["children"][0] is first["children"][0])   # True  — every other row
```

Note `Board` is `@pure` here and unmarked in §3 and §5. The root has to be
memoised for the *whole* tree to come back identical; with an unmarked root the
rows are still reused individually, but the `Stack` holding them is rebuilt, so
the gate falls through to the compare. Unchanged render, twelve rows:
**161.68 µs → 5.41 µs**.

Handler-bearing nodes were the last thing standing in the way of this: the
registry is rebuilt from nothing every render, so a subtree carrying a callable
had to be rewritten to re-register its ids. A memoised subtree now replays its
registrations instead of rebuilding its nodes — cached against both the expanded
object and the path it was serialized at, since ids are built from the path and
the same subtree at a new path is a different answer.

It gates on a compare, not a digest. The previous tree is retained anyway to
diff against, so comparing it outright costs less than serialising and hashing
it — **27.03 µs → 0.08 µs** on a 1134-byte tree — and it cannot collide, which
a digest over `json.dumps(default=str)` can: two values that stringify alike
hashed alike, and the update was dropped with no error.

---

## 5. Keyed lists — patches that name what changed

Lists reconcile by `key` instead of being replaced whole.

```python
import json
from flyrail import Layout, Stack, Text, Button, component, pure, use_state

@component
@pure
def Row(index, label, selected):
    use_state(0)
    return Stack(
        Text(label, key="t"),
        Button("pick", on_click=lambda s, e: None, key="b",
               variant="contained" if selected else "outlined"),
        key=f"r{index}")

@component
def Board(picked):
    return Stack(*[Row(i, f"Row {i}", picked == i, key=i) for i in range(12)])

keyed = Layout(Board)
keyed.tick(-1)
ops = keyed.tick(3)
print([op["path"] for op in ops])
# ['/children/3/children/1/props/variant']
print(len(json.dumps(ops, separators=(",", ":"))))     # 84

whole = Layout(Board, keyed_lists=False, memo=False)
whole.tick(-1)
print(len(json.dumps(whole.tick(3), separators=(",", ":"))))   # 4206
```

**4206 bytes → 84.** Every element of a panel hangs off one `children` list, so
replacing lists wholesale meant one changed label re-sent every sibling it had
— and with the op wrapper on top, a patch could come out *larger* than the
snapshot it replaced.

Reconciliation needs every element on both sides to be a dict with a unique
non-`None` key; anything else falls back to replacing. A pure reorder is not
expressible in `add`/`replace`/`remove`, so that case replaces too, rather than
emitting something a client cannot apply. Removals are emitted back to front,
because RFC 6902 array pointers are indices and a remove shifts what follows.

---

## 6. Slots — a value that moves every tick

A live scalar pushed straight to the client, never entering the tree.

```python
import json
from flyrail import Layout, Slot, Stack, Text

layout = Layout(lambda state: Stack(
    Text("Spindle", key="t"),
    Slot("spindle_load", default=0.0),
))
layout.tick(None)

total = 0
for step in range(50):
    message = layout.set_slot("spindle_load", round(step * 0.7, 3))
    total += len(json.dumps(message, separators=(",", ":")))
    assert layout.tick(None) == []        # the tree never moved

print(total)                              # 3085
print(layout.set_slot("spindle_load", round(49 * 0.7, 3)))   # None
```

Fifty updates, **no diff computed once**, and the tree stays byte-identical so
the gates in §4 keep firing. Repeating a value sends nothing.

Worth being precise about what this wins: it is mostly a **CPU** optimisation.
A slot pushes the whole value; a patch names the fields that moved. Over 49
updates of a six-float pose:

| | in the tree | in a slot |
|---|---|---|
| one axis moving | 5574 bytes | 5476 bytes |
| every axis moving | 22527 bytes | 5818 bytes |

When one field moves, the two are a wash on bytes and the slot wins only on the
diff it did not compute. When most of the value moves, the per-op paths overtake
it and the slot wins on both. Reach for a slot because of the diff cost, and
treat the byte count as a bonus that depends on how the value moves.

---

## 7. Handler ids that survive a reorder

A widget keeps its handler id when its siblings move around it.

```python
from flyrail import Layout, Stack, Button

hits = []

def rows(order):
    return lambda state: Stack(*[
        Button(name, on_click=lambda s, e, n=name: hits.append(n), key=name)
        for name in order])

layout = Layout(rows(["a", "b", "c"]))
tree = layout.render(None)
held = tree["children"][2]["on_click"]["handlerId"]     # the client holds c's id

layout.render_fn = rows(["c", "a", "b"])                # c moves to the front
layout.render(None)
layout.dispatch(held, None, None)
print(hits)                                             # ['c']
```

The id is built from the child's `key` where it has one, falling back to its
index. Before, the path segment was the index, so a click already in flight
reached whatever now sat in that position — here, `b`. The registry is rebuilt
every render, so there was no error to notice: just the wrong button firing.

---

## 8. Async handlers

```python
import asyncio
from flyrail import Layout, Stack, Button

async def save(state, event):
    await asyncio.sleep(0)
    return "saved"

layout = Layout(lambda state: Stack(Button("Save", on_click=save, key="s")))
layout.render(None)
handler_id = next(iter(layout.registry))

try:
    layout.dispatch(handler_id, None, None)
except RuntimeError as error:
    print(error)        # handler '0.s:on_click:s' is async; use await adispatch() ...

print(asyncio.run(layout.adispatch(handler_id, None, None)))   # saved
```

`dispatch` checks the function rather than its result, so an async handler is
refused before a coroutine is built — and a `functools.partial` around one no
longer reads as sync. A *sync* handler that returns an awaitable is a different
mistake: it can only be caught after calling it, and its synchronous half has
already run, so it gets its own message saying so.

---

## 9. Threads

One Layout renders serially. Two Layouts on two threads keep their hooks to
themselves — the frame stack is thread-local, so neither can reach the other's
slots.

```python
import threading
from flyrail import Layout, Text, component, use_state

escaped = {}

@component
def Panel(_state):
    use_state(1)

    def peek():
        try:
            use_state(2)
            escaped["leaked"] = True
        except RuntimeError as error:
            escaped["error"] = str(error)

    thread = threading.Thread(target=peek)
    thread.start()
    thread.join()
    return Text("x")

Layout(Panel).render(None)
print(escaped)
# {'error': 'hooks may only be called inside a @component body'}
```

Before, that second thread found the rendering thread's frame and took a slot
from it. It did not raise; it surfaced later as state that could not have
happened.

---

## 10. Escape hatches

| reach for | when |
|---|---|
| `Layout(memo=False)` | a panel looks stale and you suspect a `@pure` that is not |
| `layout.reset()` | the host mutates state components read without being handed it |
| `Layout(keyed_lists=False)` | your client only understands object pointers, not array indices |
| `Layout(strict=True)` | double-render a `@pure` root and compare, to catch nondeterminism |

`invalidate()` deliberately does **not** drop memoised subtrees. `Driver`
documents `invalidate()` per tick for tick hosts, so clearing there would mean
the memo never survives a tick. Host state reaches a component through its
arguments, which the memo compares, so anything the host actually changed
re-renders on that basis. `reset()` is the one that forgets everything.
