"""An unchanged subtree comes back as the same object, handlers and all."""
import unittest

from flyrail import (Button, Layout, Stack, Text, component, pure, use_effect,
                     use_state)


@component
@pure
def Row(index, label, selected):
    use_state(0)
    return Stack(Text(label, key="t"),
                 Button("pick", on_click=lambda s, e: None, key="b",
                        variant="contained" if selected else "outlined"),
                 key=f"r{index}")


@component
@pure
def Board(picked):
    return Stack(*[Row(i, f"Row {i}", picked == i, key=i) for i in range(6)])


class IdentityTest(unittest.TestCase):
    def test_two_renders_of_the_same_state_give_the_same_tree_object(self):
        layout = Layout(Board)

        first = layout.render(-1)
        second = layout.render(-1)

        self.assertIs(second, first)

    def test_a_subtree_carrying_a_handler_is_still_the_same_object(self):
        """Serialization rebuilds handler nodes, so this is the case that was
        broken: the tree compared equal but was never identical."""
        layout = Layout(Board)

        first = layout.render(-1)
        second = layout.render(-1)

        row = first["children"][0]
        self.assertIs(second["children"][0], row)
        self.assertIsInstance(row["children"][1]["on_click"], dict)

    def test_a_changed_subtree_is_new_and_its_siblings_are_not(self):
        layout = Layout(Board)
        before = layout.render(-1)

        after = layout.render(2)

        self.assertIsNot(after["children"][2], before["children"][2])
        for index in (0, 1, 3, 4, 5):
            self.assertIs(after["children"][index], before["children"][index],
                          f"row {index} was rebuilt for a change elsewhere")

    def test_handler_ids_still_dispatch_after_a_cache_hit(self):
        hits = []

        @component
        @pure
        def Panel(_label):
            return Stack(Button("go", on_click=lambda s, e: hits.append("hit"),
                                key="go"), key="p")

        layout = Layout(lambda state: Stack(Panel("x", key="p")))
        layout.render(None)
        tree = layout.render(None)

        handler_id = tree["children"][0]["children"][0]["on_click"]["handlerId"]
        layout.dispatch(handler_id, None, None)

        self.assertEqual(hits, ["hit"])

    def test_the_registry_is_rebuilt_even_when_the_tree_is_reused(self):
        """render() clears the registry, so a reused subtree has to replay its
        registrations or its ids stop resolving."""
        layout = Layout(Board)
        first = layout.render(-1)
        ids = [row["children"][1]["on_click"]["handlerId"]
               for row in first["children"]]

        layout.render(-1)

        for handler_id in ids:
            self.assertIn(handler_id, layout.registry)

    def test_a_reused_subtree_keeps_its_hook_state(self):
        seen = []
        setters = {}

        @component
        @pure
        def Inner(_label):
            value, set_value = use_state("initial")
            setters["inner"] = set_value
            seen.append(value)
            return Button("b", on_click=lambda s, e: None, key="b")

        @component
        @pure
        def Outer(label):
            return Stack(Inner(label, key="inner"), key="o")

        layout = Layout(lambda state: Stack(Outer("x", key="outer")))
        layout.render(None)
        layout.render(None)
        seen.clear()

        setters["inner"]("changed")
        layout.render(None)

        self.assertEqual(seen, ["changed"])

    def test_effects_still_run_once_across_reused_renders(self):
        log = []

        @component
        @pure
        def Panel(label):
            use_effect(lambda: log.append(f"on {label}"), [label])
            return Button("b", on_click=lambda s, e: None, key="b")

        layout = Layout(lambda state: Stack(Panel("a", key="p")))
        layout.render(None)
        layout.render(None)
        layout.render(None)

        self.assertEqual(log, ["on a"])

    def test_a_keyed_subtree_survives_a_reorder_intact(self):
        """Paths are built from keys, so moving a keyed item does not change
        its handler ids -- and so it keeps its identity across the move too."""
        @component
        @pure
        def Item(label):
            return Button(label, on_click=lambda s, e: None, key="b")

        def rows(order):
            return lambda state: Stack(*[Item(n, key=n) for n in order])

        layout = Layout(rows(["a", "b"]))
        first = layout.render(None)
        layout.render_fn = rows(["b", "a"])
        second = layout.render(None)

        moved = second["children"][0]
        self.assertEqual(moved["key"], "b")
        self.assertIs(moved, first["children"][1])
        self.assertEqual(moved["on_click"]["handlerId"],
                         first["children"][1]["on_click"]["handlerId"])

    def test_a_keyless_subtree_that_moves_is_re_serialized(self):
        """Without a key the path segment is the index, so the same component
        at a new position really does get new handler ids."""
        @component
        @pure
        def Item(label):
            return Button(label, on_click=lambda s, e: None)

        layout = Layout(lambda order: Stack(*[Item(n, key=n) for n in order]))
        first = layout.render(["a", "b"])
        second = layout.render(["b", "a"])

        moved = second["children"][0]
        self.assertIsNot(moved, first["children"][1])
        self.assertEqual(moved["on_click"]["handlerId"], "0.0:on_click:")

    def test_diff_and_commit_short_circuits_on_identity(self):
        layout = Layout(Board)
        layout.tick(-1)

        self.assertEqual(layout.tick(-1), [])
        self.assertEqual(layout.tick(-1), [])

    def test_a_real_change_still_diffs_after_an_identical_render(self):
        layout = Layout(Board)
        layout.tick(-1)
        layout.tick(-1)

        ops = layout.tick(3)

        self.assertEqual([op["path"] for op in ops],
                         ["/children/3/children/1/props/variant"])

    def test_memo_off_gives_up_identity_but_stays_correct(self):
        layout = Layout(Board, memo=False)

        first = layout.render(-1)
        second = layout.render(-1)

        self.assertIsNot(second, first)
        self.assertEqual(second, first)
        layout.tick(-1)
        self.assertEqual(layout.tick(-1), [],
                         "the compare gate still catches an equal tree")

    def test_reset_drops_the_serialized_form_too(self):
        layout = Layout(Board)
        first = layout.render(-1)

        layout.reset()
        second = layout.render(-1)

        self.assertIsNot(second, first)
        self.assertEqual(second, first)

    def test_an_unmounted_component_does_not_leave_a_serialized_form_behind(self):
        @component
        @pure
        def Item(label):
            return Button(label, on_click=lambda s, e: None, key="b")

        def panel(shown):
            return Stack(*([Item("a", key="a")] if shown else []))

        layout = Layout(panel)
        layout.render(True)
        layout.render(False)

        self.assertEqual(layout._serial, {})
