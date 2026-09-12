"""Focused tests for use_effect: when it runs, and what it undoes."""
import unittest

from flyrail import Layout, Stack, Text, component, pure, use_effect, use_state


class UseEffectTest(unittest.TestCase):
    def test_an_effect_runs_after_the_first_render(self):
        log = []

        @component
        def Panel(_state):
            use_effect(lambda: log.append("ran"), [])
            log.append("rendered")
            return Text("x")

        Layout(Panel).render(None)

        self.assertEqual(log, ["rendered", "ran"], "effects run on the commit")

    def test_an_effect_with_empty_deps_runs_once_however_many_renders(self):
        log = []

        @component
        def Panel(_state):
            use_effect(lambda: log.append("ran"), [])
            return Text("x")

        layout = Layout(Panel)
        layout.render(None)
        layout.render(None)
        layout.render(None)

        self.assertEqual(log, ["ran"])

    def test_changed_deps_clean_up_before_running_again(self):
        log = []

        @component
        def Panel(state):
            def effect():
                log.append(f"on {state}")
                return lambda: log.append(f"off {state}")

            use_effect(effect, [state])
            return Text(str(state))

        layout = Layout(Panel)
        layout.render(1)
        layout.render(2)

        self.assertEqual(log, ["on 1", "off 1", "on 2"])

    def test_leaving_the_tree_runs_the_cleanup(self):
        log = []

        @component
        def Highlighted(_state):
            use_effect(lambda: (log.append("on"), lambda: log.append("off"))[1], [])
            return Text("lit")

        @component
        def Panel(shown):
            return Stack(Highlighted(shown, key="h")) if shown else Stack()

        layout = Layout(Panel)
        layout.render(True)
        layout.render(False)

        self.assertEqual(log, ["on", "off"])

    def test_a_cleanup_runs_once_even_after_several_unchanged_renders(self):
        """reactpy re-runs a stale cleanup at unmount once deps have moved;
        pinning that this does not."""
        log = []

        @component
        def Highlighted(colour):
            def effect():
                log.append(f"on {colour}")
                return lambda: log.append(f"off {colour}")

            use_effect(effect, [colour])
            return Text(colour)

        @component
        def Panel(state):
            return Stack(Highlighted(state, key="h")) if state else Stack()

        layout = Layout(Panel)
        layout.render("red")
        layout.render("blue")
        layout.render(None)

        self.assertEqual(log, ["on red", "off red", "on blue", "off blue"])

    def test_effects_run_children_before_parents(self):
        log = []

        @component
        def Inner(_state):
            use_effect(lambda: log.append("inner"), [])
            return Text("i")

        @component
        def Outer(_state):
            use_effect(lambda: log.append("outer"), [])
            return Stack(Inner(None, key="i"))

        Layout(Outer).render(None)

        self.assertEqual(log, ["inner", "outer"])

    def test_a_memoised_component_does_not_re_run_its_effect(self):
        log = []

        @component
        @pure
        def Row(label):
            use_effect(lambda: log.append(f"on {label}"), [label])
            return Text(label)

        layout = Layout(lambda state: Stack(Row("a", key="a")))
        layout.render(None)
        layout.render(None)

        self.assertEqual(log, ["on a"])

    def test_an_effect_may_set_state_and_the_next_render_sees_it(self):
        seen = []

        @component
        def Panel(_state):
            value, set_value = use_state("initial")
            seen.append(value)
            use_effect(lambda: set_value("from effect"), [])
            return Text(value)

        layout = Layout(Panel)
        layout.render(None)
        layout.render(None)

        self.assertEqual(seen, ["initial", "from effect"])

    def test_hook_order_is_still_enforced_across_effect_and_state(self):
        toggle = {"on": True}

        @component
        def Panel(_state):
            if toggle["on"]:
                use_state(0)
            use_effect(lambda: None, [])
            return Text("x")

        layout = Layout(Panel)
        layout.render(None)
        toggle["on"] = False

        with self.assertRaises(RuntimeError):
            layout.render(None)
