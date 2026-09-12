"""Focused tests for Layout diffing and tick suppression."""
import copy
import unittest

from flyrail import Layout, Stack, Text
from flyrail.layout import _diff


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


def _apply(doc, ops):
    """Reference RFC6902 applier for the add/replace/remove subset, so the tests
    assert what a client would actually end up with rather than op shapes."""
    out = copy.deepcopy(doc)
    for op in ops:
        path = op["path"]
        if path in ("", "/"):
            out = copy.deepcopy(op["value"])
            continue
        segments = [seg.replace("~1", "/").replace("~0", "~")
                    for seg in path.lstrip("/").split("/")]
        parent = out
        for seg in segments[:-1]:
            parent = parent[int(seg)] if isinstance(parent, list) else parent[seg]
        last = segments[-1]
        if isinstance(parent, list):
            index = int(last)
            if op["op"] == "remove":
                parent.pop(index)
            elif op["op"] == "add":
                parent.insert(index, copy.deepcopy(op["value"]))
            else:
                parent[index] = copy.deepcopy(op["value"])
        else:
            if op["op"] == "remove":
                del parent[last]
            else:
                parent[last] = copy.deepcopy(op["value"])
    return out


class KeyedListDiffTest(unittest.TestCase):
    """Lists reconcile by key; whatever the ops are, they must rebuild `new`."""

    def _rows(self, labels):
        return {"type": "Stack",
                "children": [{"type": "Text", "key": k, "props": {"value": v}}
                             for k, v in labels]}

    def _roundtrip(self, old, new, keyed=True):
        ops = _diff(old, new, "", keyed)
        self.assertEqual(_apply(old, ops), new, f"ops did not rebuild new: {ops}")
        return ops

    def test_one_changed_value_patches_only_that_element(self):
        old = self._rows([("a", "1"), ("b", "2"), ("c", "3")])
        new = self._rows([("a", "1"), ("b", "CHANGED"), ("c", "3")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children/1/props/value"])

    def test_an_appended_row_is_one_add(self):
        old = self._rows([("a", "1"), ("b", "2")])
        new = self._rows([("a", "1"), ("b", "2"), ("c", "3")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["op"] for op in ops], ["add"])
        self.assertEqual(ops[0]["path"], "/children/2")

    def test_a_removed_row_is_one_remove(self):
        old = self._rows([("a", "1"), ("b", "2"), ("c", "3")])
        new = self._rows([("a", "1"), ("c", "3")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["op"] for op in ops], ["remove"])
        self.assertEqual(ops[0]["path"], "/children/1")

    def test_removals_are_emitted_back_to_front_so_indices_stay_valid(self):
        old = self._rows([("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")])
        new = self._rows([("a", "1"), ("d", "4")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children/2", "/children/1"])

    def test_an_insertion_in_the_middle_lands_in_the_right_place(self):
        old = self._rows([("a", "1"), ("c", "3")])
        new = self._rows([("a", "1"), ("b", "2"), ("c", "3")])

        self._roundtrip(old, new)

    def test_adds_and_removes_together_still_rebuild_the_list(self):
        old = self._rows([("a", "1"), ("b", "2"), ("c", "3")])
        new = self._rows([("a", "1"), ("c", "CHANGED"), ("d", "4")])

        self._roundtrip(old, new)

    def test_a_reorder_falls_back_to_replacing_the_list(self):
        """`move` is not in the op set this emits, so a pure reorder is not
        expressible element by element; replacing is correct, if blunt."""
        old = self._rows([("a", "1"), ("b", "2")])
        new = self._rows([("b", "2"), ("a", "1")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children"])

    def test_an_unkeyed_list_falls_back_to_replacing(self):
        old = {"type": "Stack", "children": [{"type": "Text", "props": {"value": "1"}}]}
        new = {"type": "Stack", "children": [{"type": "Text", "props": {"value": "2"}}]}

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children"])

    def test_duplicate_keys_fall_back_to_replacing(self):
        old = self._rows([("a", "1"), ("a", "2")])
        new = self._rows([("a", "1"), ("a", "CHANGED")])

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children"])

    def test_unhashable_keys_fall_back_to_replacing(self):
        old = {"type": "Stack",
               "children": [{"type": "Text", "key": ["a"], "props": {"value": "1"}},
                            {"type": "Text", "key": ["b"], "props": {"value": "2"}}]}
        new = {"type": "Stack",
               "children": [{"type": "Text", "key": ["a"], "props": {"value": "1"}},
                            {"type": "Text", "key": ["b"], "props": {"value": "CHANGED"}}]}

        ops = self._roundtrip(old, new)

        self.assertEqual([op["path"] for op in ops], ["/children"])

    def test_keyed_lists_can_be_turned_off_for_a_limited_client(self):
        old = self._rows([("a", "1"), ("b", "2")])
        new = self._rows([("a", "1"), ("b", "CHANGED")])

        ops = self._roundtrip(old, new, keyed=False)

        self.assertEqual([op["path"] for op in ops], ["/children"])

    def test_the_layout_option_reaches_the_diff(self):
        def panel(state):
            return Stack(Text(state, key="a"), Text("fixed", key="b"))

        keyed, whole = Layout(panel), Layout(panel, keyed_lists=False)
        keyed.tick("1")
        whole.tick("1")

        self.assertEqual([op["path"] for op in keyed.tick("2")],
                         ["/children/0/props/value"])
        self.assertEqual([op["path"] for op in whole.tick("2")], ["/children"])
