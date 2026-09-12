"""Focused tests for async event handlers via adispatch."""
import asyncio
import unittest

from flyrail import Layout, Stack, Button


class AsyncDispatchTest(unittest.TestCase):
    def setUp(self):
        async def save(state, event):
            await asyncio.sleep(0)
            state["saved"].append(event["value"])

        def ping(state, event):
            state["pings"] += 1
            return "pong"

        layout = Layout(lambda s: Stack(
            Button("Save", on_click=save, key="save"),
            Button("Ping", on_click=ping, key="ping"),
        ))
        layout.tick({})
        self.hids = {h.split(":")[-1]: h for h in layout.registry}
        self.layout = layout

    def test_sync_dispatch_rejects_async_handler_loudly(self):
        with self.assertRaises(RuntimeError):
            self.layout.dispatch(self.hids["save"], {"saved": []})

    def test_sync_dispatch_still_works(self):
        state = {"pings": 0}
        self.layout.dispatch(self.hids["ping"], state)
        self.assertEqual(state["pings"], 1)


class AsyncDispatchAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_adispatch_awaits_and_returns(self):
        async def save(state, event):
            await asyncio.sleep(0)
            state["saved"].append(event["value"])
            return "saved!"

        def ping(state, event):
            return "pong"

        layout = Layout(lambda s: Stack(
            Button("Save", on_click=save, key="save"),
            Button("Ping", on_click=ping, key="ping"),
        ))
        layout.tick({})
        hids = {h.split(":")[-1]: h for h in layout.registry}

        state = {"saved": []}
        res = await layout.adispatch(hids["save"], state, {"value": "a"})
        self.assertEqual(res, "saved!")
        self.assertEqual(state["saved"], ["a"])
        # Sync handlers work through the same path.
        self.assertEqual(await layout.adispatch(hids["ping"], state), "pong")

    async def test_adispatch_unknown_raises(self):
        layout = Layout(lambda s: Stack(Button("A", on_click=lambda s, e: None)))
        layout.tick({})
        with self.assertRaises(KeyError):
            await layout.adispatch("9:999:on_click:", {})


if __name__ == "__main__":
    unittest.main()


class AsyncRefusalTest(unittest.TestCase):
    def test_refusing_an_async_handler_does_not_run_any_of_it(self):
        """Calling an async def only builds a coroutine -- its body does not
        run -- so refusing after the call was already safe. Pinning it anyway:
        the check moved onto the function, and this is the property that must
        not regress."""
        ran = []

        async def on_click(state, event):
            ran.append('before await')
            await asyncio.sleep(0)
            ran.append('after await')

        layout = Layout(lambda state: Stack(Button('go', on_click=on_click, key='go')))
        layout.render(None)
        handler_id = next(iter(layout.registry))

        with self.assertRaisesRegex(RuntimeError, 'is async'):
            layout.dispatch(handler_id, None, None)

        self.assertEqual(ran, [], 'nothing of the handler should have run')

    def test_an_async_handler_behind_a_partial_is_still_refused(self):
        from functools import partial

        ran = []

        async def on_click(prefix, state, event):
            ran.append(prefix)

        layout = Layout(lambda state: Stack(
            Button('go', on_click=partial(on_click, 'x'), key='go')))
        layout.render(None)
        handler_id = next(iter(layout.registry))

        with self.assertRaisesRegex(RuntimeError, 'is async'):
            layout.dispatch(handler_id, None, None)
        self.assertEqual(ran, [])

    def test_a_sync_handler_returning_an_awaitable_is_refused_without_running_it(self):
        ran = []

        async def work():
            ran.append('work ran')

        def on_click(state, event):
            ran.append('sync part ran')
            return work()

        layout = Layout(lambda state: Stack(Button('go', on_click=on_click, key='go')))
        layout.render(None)
        handler_id = next(iter(layout.registry))

        with self.assertRaisesRegex(RuntimeError, 'returned an awaitable'):
            layout.dispatch(handler_id, None, None)

        # Unavoidable: you cannot know a sync function returns an awaitable
        # without calling it. The message is what changed -- it now names this
        # case rather than blaming an async handler.
        self.assertEqual(ran, ['sync part ran'])
