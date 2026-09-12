"""Focused tests for Layout diffing and tick suppression."""
import unittest

from flyrail import Layout, Stack, Text


class DiffTest(unittest.TestCase):
    def test_first_tick_emits(self):
        layout = Layout(lambda s: Stack(Text(f"speed: {s['speed']}")))
        self.assertTrue(layout.tick({"speed": 1}))

    def test_unchanged_tick_sends_nothing(self):
        layout = Layout(lambda s: Stack(Text(f"speed: {s['speed']}")))
        layout.tick({"speed": 1})
        self.assertEqual(layout.tick({"speed": 1}), [])

    def test_changed_leaf_replaces_children_wholesale(self):
        # Arrays patch by replace, never by index: React reconciles via `key`
        # client-side, so index ops would be pure overhead. Pin that here.
        layout = Layout(lambda s: Stack(Text(s["label"])))
        layout.tick({"label": "a"})
        ops = layout.tick({"label": "b"})
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "replace")
        self.assertEqual(ops[0]["path"], "/children")

    def test_reorder_is_single_replace(self):
        layout = Layout(lambda s: Stack(*[Text(n, key=n) for n in s["items"]]))
        layout.tick({"items": ["a", "b"]})
        ops = layout.tick({"items": ["b", "a"]})
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "replace")

    def test_ops_are_json_serializable(self):
        import json

        layout = Layout(lambda s: Stack(Text(f"speed: {s['speed']}")))
        ops = layout.tick({"speed": 1})
        json.dumps(ops)


if __name__ == "__main__":
    unittest.main()


class UnchangedGateTest(unittest.TestCase):
    """The gate in front of the diff: a compare, not a digest."""

    class _Reading:
        """Two of these stringify alike but are not the same value."""

        def __init__(self, value):
            self.value = value

        def __str__(self):
            return "Reading"

    def _layout(self, holder):
        return Layout(lambda state: Text(holder["value"]))

    def test_two_values_that_stringify_alike_are_not_taken_for_equal(self):
        holder = {"value": self._Reading(1)}
        layout = self._layout(holder)
        layout.tick(holder)

        holder["value"] = self._Reading(2)
        ops = layout.tick(holder)

        self.assertTrue(ops, "a digest over str() collided here and dropped this")

    def test_an_unchanged_tree_still_sends_nothing(self):
        layout = Layout(lambda state: Text(state))
        layout.tick("same")

        self.assertEqual(layout.tick("same"), [])

    def test_a_slot_repeating_a_stringwise_alike_value_still_sends(self):
        layout = Layout(lambda state: Text("x"))

        first = layout.set_slot("reading", self._Reading(1))
        second = layout.set_slot("reading", self._Reading(2))

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_a_slot_repeating_the_same_value_sends_nothing(self):
        layout = Layout(lambda state: Text("x"))

        layout.set_slot("pose", {"x": 1.0})

        self.assertIsNone(layout.set_slot("pose", {"x": 1.0}))

    def test_a_value_whose_equality_is_not_a_bool_counts_as_changed(self):
        """ndarray-ish: == gives an array, and bool() of it raises. Report
        changed rather than guess, the way _deps_changed already does."""
        class Elementwise:
            def __bool__(self):
                raise ValueError("truth value of an array is ambiguous")

        class Weird:
            def __eq__(self, other):
                return Elementwise()

        layout = Layout(lambda state: Text("x"))
        layout.set_slot("w", Weird())

        self.assertIsNotNone(layout.set_slot("w", Weird()))
