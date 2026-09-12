"""Focused tests for Layout.render handler serialization."""
import unittest

from flyrail import Layout, Slot, Stack, Text, Button


class SerializeTest(unittest.TestCase):
    def test_callable_becomes_handler_id(self):
        def stop(state, event):
            state["stopped"] = True

        layout = Layout(lambda s: Stack(Button("Stop", on_click=stop)))
        tree = layout.render({})
        btn = tree["children"][0]
        hid = btn["on_click"]["handlerId"]
        self.assertIs(layout.registry[hid], stop)

    def test_handler_id_embeds_path_and_key(self):
        layout = Layout(lambda s: Stack(Button("A", on_click=lambda s, e: None, key="k1")))
        tree = layout.render({})
        # The path segment is the child's key, not its index, so the id does
        # not move when siblings do.
        self.assertEqual(tree["children"][0]["on_click"]["handlerId"], "0.k1:on_click:k1")

    def test_a_keyless_child_still_falls_back_to_its_index(self):
        layout = Layout(lambda s: Stack(Button("A", on_click=lambda s, e: None)))
        tree = layout.render({})
        self.assertEqual(tree["children"][0]["on_click"]["handlerId"], "0.0:on_click:")

    def test_registry_cleared_per_render(self):
        layout = Layout(lambda s: Stack(Button("A", on_click=lambda s, e: None)))
        first = layout.render({})
        hid = first["children"][0]["on_click"]["handlerId"]
        layout.render({})
        # Same position/key re-registers (fresh closure); stale entries never linger.
        self.assertIn(hid, layout.registry)
        self.assertEqual(len(layout.registry), 1)

    def test_allowlist_rejects_unknown_type(self):
        layout = Layout(
            lambda s: Stack(Button("A", on_click=lambda s, e: None)),
            allowed_types={"Stack", "Text"},
        )
        with self.assertRaises(ValueError):
            layout.render({})

    def test_slot_exempt_from_allowlist(self):
        layout = Layout(lambda s: Stack(Slot("rows")), allowed_types={"Stack"})
        tree = layout.render({})
        self.assertEqual(tree["children"][0]["type"], "__Slot__")

    def test_wire_tree_contains_no_callables(self):
        import json

        layout = Layout(lambda s: Stack(Button("A", on_click=lambda s, e: None)))
        tree = layout.render({})
        json.dumps(tree)  # raises TypeError if a callable leaked onto the wire


if __name__ == "__main__":
    unittest.main()


class NonMutatingSerializeTest(unittest.TestCase):
    """Serialization rebuilds only the nodes that carry handlers."""

    def test_the_nodes_the_component_returned_are_not_written_through(self):
        """The old path deep-copied the tree precisely so it could mutate it.
        Without a copy, mutating would corrupt whatever the component held."""
        held = Button("go", on_click=lambda state, event: None, key="go")
        layout = Layout(lambda state: Stack(held))

        layout.render(None)

        self.assertTrue(callable(held["on_click"]),
                        "the component's own node was rewritten in place")

    def test_a_node_without_handlers_survives_serialization_as_the_same_object(self):
        text = Text("hello", key="t")
        layout = Layout(lambda state: Stack(text, Button(
            "go", on_click=lambda state, event: None, key="go")))

        tree = layout.render(None)

        # Stack and the Button are rebuilt; the Text has nothing to rewrite.
        self.assertEqual(tree["children"][0]["props"]["value"], "hello")
        self.assertIsInstance(tree["children"][1]["on_click"], dict)

    def test_handlers_still_reach_the_registry_and_dispatch(self):
        calls = []
        layout = Layout(lambda state: Stack(
            Button("go", on_click=lambda state, event: calls.append("hit"), key="go")))

        tree = layout.render(None)
        layout.dispatch(tree["children"][0]["on_click"]["handlerId"], None, None)

        self.assertEqual(calls, ["hit"])


class StableHandlerIdTest(unittest.TestCase):
    """A widget keeps its handler id when its siblings move around it."""

    def _rows(self, order):
        return lambda state: Stack(*[
            Button(name, on_click=lambda s, e, n=name: None, key=name) for name in order])

    def _ids(self, tree):
        return {child["key"]: child["on_click"]["handlerId"]
                for child in tree["children"]}

    def test_reordering_siblings_does_not_move_a_handler_id(self):
        layout = Layout(self._rows(["a", "b", "c"]))
        before = self._ids(layout.render(None))

        layout.render_fn = self._rows(["c", "a", "b"])
        after = self._ids(layout.render(None))

        self.assertEqual(before, after)

    def test_removing_an_earlier_sibling_does_not_move_a_handler_id(self):
        layout = Layout(self._rows(["a", "b", "c"]))
        before = self._ids(layout.render(None))

        layout.render_fn = self._rows(["b", "c"])
        after = self._ids(layout.render(None))

        self.assertEqual(after["b"], before["b"])
        self.assertEqual(after["c"], before["c"])

    def test_an_id_still_reaches_the_handler_that_widget_owns(self):
        hits = []
        rows = lambda state: Stack(*[
            Button(name, on_click=lambda s, e, n=name: hits.append(n), key=name)
            for name in state])

        layout = Layout(rows)
        tree = layout.render(["a", "b", "c"])
        target = self._ids(tree)["c"]

        # 'c' moves to the front; the id a client already holds must still be
        # c's handler, not whatever now sits where c used to be.
        layout.render(["c", "a", "b"])
        layout.dispatch(target, None, None)

        self.assertEqual(hits, ["c"])
