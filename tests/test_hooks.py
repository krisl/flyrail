"""Focused tests for hooks: local state, memo, keys, pruning."""
import unittest

from flyrail import Layout, Stack, Text, Button
from flyrail.core import component
from flyrail.hooks import use_state, use_memo


@component
def Counter(s, key=None, step=1):
    n, set_n = use_state(0)
    return Stack(
        Text(f"n={n}", key="label"),
        Button("+", on_click=lambda st, ev: set_n(lambda c: c + step), key="inc"),
    )


def two_counters(s):
    return Stack(Counter(s, key="c1"), Counter(s, key="c2"))


def first_inc(layout):
    hid = sorted(h for h in layout.registry if h.endswith(":on_click:inc"))[0]
    return hid


class StateTest(unittest.TestCase):
    def test_instances_hold_independent_state(self):
        layout = Layout(two_counters)
        layout.tick({})
        layout.dispatch(first_inc(layout), {})
        tree = layout.render({})
        labels = [c["children"][0]["props"]["value"] for c in tree["children"]]
        self.assertEqual(labels, ["n=1", "n=0"])

    def test_dispatch_schedules_rerender(self):
        layout = Layout(two_counters)
        layout.tick({})
        layout.dispatch(first_inc(layout), {})
        self.assertTrue(layout._dirty)

    def test_duplicate_key_raises(self):
        layout = Layout(lambda s: Stack(Counter(s, key="dup"), Counter(s, key="dup")))
        with self.assertRaises(ValueError):
            layout.tick({})

    def test_hooks_outside_component_raise(self):
        with self.assertRaises(RuntimeError):
            use_state(0)
        with self.assertRaises(RuntimeError):
            use_memo(lambda: 1, [])

    def test_initializer_runs_once(self):
        runs = []

        @component
        def Init(s, key=None):
            v, _set_v = use_state(lambda: runs.append(1) or "ready")
            return Text(v, key="t")

        layout = Layout(lambda s: Stack(Init(s, key="i")))
        layout.tick({})
        layout.tick({})
        self.assertEqual(runs, [1])

    def test_order_change_raises(self):
        @component
        def Flippy(s, key=None):
            a, _set_a = use_state(0)
            if s["extra"]:
                use_memo(lambda: 1, [1])
            return Text(f"a={a}", key="t")

        layout = Layout(lambda s: Stack(Flippy(s, key="f")))
        layout.tick({"extra": False})
        with self.assertRaises(RuntimeError):  # grew a hook
            layout.tick({"extra": True})


class MemoTest(unittest.TestCase):
    def test_recomputes_only_on_dep_change(self):
        recomputes = []

        @component
        def BigList(s, key=None):
            q, set_q = use_state("")
            def compute():
                recomputes.append(1)
                return [r for r in s["rows"] if q in r]
            visible = use_memo(compute, [q, len(s["rows"])])
            return Stack(
                Text(f"{len(visible)}", key="count"),
                Button("q", on_click=lambda st, ev: set_q("a"), key="setq"),
            )

        state = {"rows": ["aa", "ab", "bb"]}
        layout = Layout(lambda s: Stack(BigList(s, key="b")))
        layout.tick(state)
        layout.tick(state)
        self.assertEqual(recomputes, [1])
        hid = next(h for h in layout.registry if h.endswith(":on_click:setq"))
        layout.dispatch(hid, state)
        layout.tick(state)
        self.assertEqual(recomputes, [1, 1])


class PruneTest(unittest.TestCase):
    def test_unmounted_state_is_dropped(self):
        @component
        def Maybe(s, key=None):
            n, set_n = use_state(0)
            return Stack(
                Text(f"m={n}", key="t"),
                Button("+", on_click=lambda st, ev: set_n(5), key="inc"),
            )

        state = {"show": True}
        layout = Layout(lambda s: Stack([Maybe(s, key="m")] if s["show"] else []))
        layout.tick(state)
        hid = next(h for h in layout.registry if h.endswith(":on_click:inc"))
        layout.dispatch(hid, state)
        layout.tick(state)
        self.assertEqual(len(layout._hooks), 1)

        state["show"] = False
        layout.tick(state)
        self.assertEqual(len(layout._hooks), 0)

        state["show"] = True
        tree = layout.render(state)
        label = tree["children"][0]["children"][0]["props"]["value"]
        self.assertEqual(label, "m=0")  # fresh state, no leak from before


if __name__ == "__main__":
    unittest.main()


class ThreadIsolationTest(unittest.TestCase):
    def test_hook_frames_are_not_visible_from_another_thread(self):
        import threading

        escaped = {}

        @component
        def Outer(_state):
            use_state(1)

            def peek():
                try:
                    use_state(2)
                    escaped['leaked'] = True
                except RuntimeError as error:
                    escaped['error'] = str(error)

            thread = threading.Thread(target=peek)
            thread.start()
            thread.join(timeout=5)
            return Text('x')

        Layout(Outer).render(None)

        self.assertNotIn('leaked', escaped)
        self.assertIn('inside a @component body', escaped['error'])


class ComponentMemoTest(unittest.TestCase):
    def test_a_pure_component_with_unchanged_arguments_is_not_re_run(self):
        from flyrail import Layout, component, pure, use_state

        runs = []

        @component
        @pure
        def Row(label):
            use_state(0)
            runs.append(label)
            return Text(label)

        @component
        def Board(_state):
            return Stack(*[Row(f'row {i}', key=i) for i in range(4)])

        layout = Layout(Board)
        layout.render(None)
        mounted = list(runs)
        runs.clear()

        layout.render(None)
        layout.render(None)

        self.assertEqual(len(mounted), 4)
        self.assertEqual(runs, [], 'unchanged @pure rows should not have re-run')

    def test_a_changed_argument_re_runs_only_that_component(self):
        from flyrail import Layout, component, pure

        runs = []
        picked = {'index': -1}

        @component
        @pure
        def Row(label, selected):
            runs.append(label)
            return Text(f'{label}{"*" if selected else ""}')

        @component
        def Board(_state):
            return Stack(*[Row(f'row {i}', picked['index'] == i, key=i) for i in range(4)])

        layout = Layout(Board)
        layout.render(None)
        runs.clear()

        picked['index'] = 2
        layout.render(None)

        self.assertEqual(runs, ['row 2'])

    def test_set_state_re_runs_only_the_component_that_owns_it(self):
        from flyrail import Layout, component, pure, use_state

        runs = []
        setters = {}

        @component
        @pure
        def Row(label):
            value, set_value = use_state(0)
            setters[label] = set_value
            runs.append(label)
            return Text(f'{label}={value}')

        @component
        def Board(_state):
            return Stack(*[Row(f'row {i}', key=i) for i in range(4)])

        layout = Layout(Board)
        layout.render(None)
        runs.clear()

        setters['row 1'](5)
        layout.render(None)

        self.assertEqual(runs, ['row 1'])

    def test_an_unmarked_component_always_re_runs(self):
        """Correct by default: only @pure opts into being skipped."""
        from flyrail import Layout, component

        runs = []

        @component
        def Row(label):
            runs.append(label)
            return Text(label)

        layout = Layout(lambda state: Stack(Row('a', key='a')))
        layout.render(None)
        layout.render(None)

        self.assertEqual(runs, ['a', 'a'])

    def test_memo_can_be_turned_off(self):
        from flyrail import Layout, component, pure

        runs = []

        @component
        @pure
        def Row(label):
            runs.append(label)
            return Text(label)

        layout = Layout(lambda state: Stack(Row('a', key='a')), memo=False)
        layout.render(None)
        layout.render(None)

        self.assertEqual(runs, ['a', 'a'])

    def test_pure_works_whichever_way_round_the_decorators_go(self):
        from flyrail import Layout, component, pure

        runs = []

        @pure
        @component
        def Row(label):
            runs.append(label)
            return Text(label)

        layout = Layout(lambda state: Stack(Row('a', key='a')))
        layout.render(None)
        layout.render(None)

        self.assertEqual(runs, ['a'])


class MemoLifecycleTest(unittest.TestCase):
    def test_a_reused_subtree_keeps_its_hook_state(self):
        """The prune deletes slots it did not see. A skipped subtree is never
        walked, so it has to report itself as still mounted or it silently
        restarts from its initial state."""
        from flyrail import Layout, component, pure, use_state

        seen = []
        setters = {}

        @component
        @pure
        def Inner(_label):
            value, set_value = use_state('initial')
            setters['inner'] = set_value
            seen.append(value)
            return Text(value)

        @component
        @pure
        def Outer(label):
            return Stack(Inner(label, key='inner'))

        layout = Layout(lambda state: Stack(Outer('x', key='outer')))
        layout.render(None)
        setters['inner']('changed')
        layout.render(None)
        seen.clear()

        # Outer is reusable now; Inner must not have been collected under it.
        layout.render(None)
        setters['inner']('changed again')
        layout.render(None)

        self.assertEqual(seen, ['changed again'], f'hook state was lost: {seen}')

    def test_invalidate_per_tick_does_not_defeat_the_memo(self):
        """Driver documents invalidate() per tick for tick hosts. If that dropped
        the memo, memoisation would never survive a tick."""
        from flyrail import Layout, component, pure

        runs = []

        @component
        @pure
        def Row(label):
            runs.append(label)
            return Text(label)

        layout = Layout(lambda state: Stack(Row('a', key='a')))
        layout.render(None)
        runs.clear()

        for _ in range(5):
            layout.invalidate()
            layout.tick(None)

        self.assertEqual(runs, [])

    def test_reset_forces_every_component_to_run_again(self):
        from flyrail import Layout, component, pure

        runs = []

        @component
        @pure
        def Row(label):
            runs.append(label)
            return Text(label)

        layout = Layout(lambda state: Stack(Row('a', key='a')))
        layout.render(None)
        runs.clear()

        layout.reset()
        layout.render(None)

        self.assertEqual(runs, ['a'])
