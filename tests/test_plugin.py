import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin():
    name = "callme_plugin_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


def _register_platform():
    from gateway.platform_registry import PlatformEntry, platform_registry

    if not platform_registry.is_registered("callme"):
        platform_registry.register(PlatformEntry(name="callme", label="/call-me", adapter_factory=lambda c: None,
                                                 check_fn=lambda: True))


_register_platform()
adapter_mod = sys.modules["callme_plugin_under_test.adapter"]
api = sys.modules["callme_plugin_under_test.callme_client"]

SAM = "5550001111"
ALEX = "5551230000"
STRANGER = "5559990000"


class FakeCallMe:
    def __init__(self):
        self.sessions = 0
        self.messages: list[dict] = []
        self.calls: list[dict] = []
        self.labels: list[str] = []
        self.events: list[dict] = []
        self.valid_tokens: set[str] = set()
        self.polls = 0

    def add_event(self, type_, payload):
        self.events.append({"id": len(self.events) + 1, "type": type_, "payload": payload,
                            "created_at": "2026-10-11T10:00:00"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/aiphone")
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/sessions":
            self.sessions += 1
            token = f"tok{self.sessions}"
            self.valid_tokens.add(token)
            return httpx.Response(200, json={"session_number": f"700000000{self.sessions}",
                                             "display": "x", "session_token": token})
        token = body.get("session_token") or request.url.params.get("session_token")
        if token not in self.valid_tokens:
            return httpx.Response(401, json={"detail": "invalid session token"})
        if path == "/sessions/label":
            self.labels.append(body["label"])
            return httpx.Response(200, json={"ok": True, "label": body["label"]})
        if path == "/messages":
            self.messages.append(body)
            return httpx.Response(200, json={"ok": True, "message_id": len(self.messages), "delivered": True})
        if path == "/calls":
            self.calls.append(body)
            return httpx.Response(200, json={"status": "completed", "transcript": "Yes, go ahead"})
        if path == "/sessions/events":
            self.polls += 1
            cursor = int(request.url.params.get("cursor"))
            pending = [e for e in self.events if e["id"] > cursor]
            return httpx.Response(200, json={"events": pending, "cursor": pending[-1]["id"] if pending else cursor})
        return httpx.Response(404, json={"detail": "not found"})


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tmp.name) / "session.json"
        self.fake = FakeCallMe()
        transport = httpx.MockTransport(self.fake.handler)
        patches = [
            mock.patch.object(api, "state_path", lambda: self.state_file),
            mock.patch.object(api, "new_client", lambda timeout=30.0: httpx.AsyncClient(transport=transport)),
            mock.patch.object(adapter_mod, "MIN_CYCLE_S", 0.0),
            mock.patch.object(adapter_mod, "POLL_WAIT_S", 0),
            mock.patch.dict(os.environ, {"CALLME_ALLOWED_USERS": f"{SAM}, 555-123-0000",
                                         "CALLME_NAMES": f"{SAM}=Sam,{ALEX}=Alex"}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def adapter(self):
        from gateway.config import PlatformConfig

        a = adapter_mod.CallMeAdapter(PlatformConfig(enabled=True, extra={}))
        a.received = []

        async def capture(event):
            a.received.append(event)

        a.handle_message = capture
        return a

    async def run_adapter(self, a, until, timeout=3.0):
        await a.connect()
        try:
            async with asyncio.timeout(timeout):
                while not until():
                    await asyncio.sleep(0.01)
        finally:
            await a.disconnect()


class Parsing(unittest.TestCase):
    def test_numbers_are_normalized_and_deduplicated(self):
        self.assertEqual(api.parse_numbers("555-000-1111, +1 (555) 000-1111;555 123 0000, 12"),
                         [SAM, ALEX])

    def test_names_accept_both_separators(self):
        self.assertEqual(api.parse_names(f"{SAM}=Sam, 555-123-0000:Alex, junk"),
                         {SAM: "Sam", ALEX: "Alex"})

    def test_resolve_person(self):
        names = {SAM: "Sam", ALEX: "Alex"}
        self.assertEqual(api.resolve_person("alex", [SAM, ALEX], names), ALEX)
        self.assertEqual(api.resolve_person("555-000-1111", [SAM, ALEX], names), SAM)
        self.assertIsNone(api.resolve_person(STRANGER, [SAM, ALEX], names))
        self.assertIsNone(api.resolve_person("", [SAM, ALEX], names))
        self.assertEqual(api.resolve_person("", [SAM], names), SAM)

    def test_chunks_respect_the_limit(self):
        text = ("word " * 900).strip()
        parts = api.chunks(text)
        self.assertTrue(all(len(p) <= api.MAX_TEXT for p in parts))
        self.assertEqual(" ".join(parts), text)


class Adapter(Base):
    async def test_greets_each_number_once_and_persists_the_session(self):
        a = self.adapter()
        await self.run_adapter(a, lambda: len(self.fake.messages) >= 2 and self.fake.polls >= 1)
        self.assertEqual(sorted(m["to"] for m in self.fake.messages), sorted([SAM, ALEX]))
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["token"], "tok1")
        self.assertEqual(sorted(state["greeted"]), sorted([SAM, ALEX]))
        self.assertEqual(oct(self.state_file.stat().st_mode & 0o777), "0o600")

        b = self.adapter()
        await self.run_adapter(b, lambda: self.fake.polls >= 3)
        self.assertEqual(len(self.fake.messages), 2)
        self.assertEqual(self.fake.sessions, 1)

    async def test_verified_message_from_allowed_number_reaches_the_agent(self):
        self.fake.add_event("message", {"from": ALEX, "body": "buy milk", "message_id": 9, "verified": True})
        a = self.adapter()
        await self.run_adapter(a, lambda: a.received)
        event = a.received[0]
        self.assertEqual(event.text, "buy milk")
        self.assertEqual(event.source.user_id, ALEX)
        self.assertEqual(event.source.chat_id, ALEX)
        self.assertEqual(event.source.user_name, "Alex")
        self.assertEqual(json.loads(self.state_file.read_text())["cursor"], 1)

    async def test_unverified_strangers_and_duplicates_are_dropped(self):
        self.fake.add_event("message", {"from": SAM, "body": "spoofed", "message_id": 1, "verified": False})
        self.fake.add_event("message", {"from": SAM, "body": "old server", "message_id": 2})
        self.fake.add_event("message", {"from": STRANGER, "body": "hi", "message_id": 3, "verified": True})
        self.fake.add_event("ring", {"from": SAM})
        self.fake.add_event("message", {"from": SAM, "body": "real", "message_id": 4, "verified": True})
        self.fake.add_event("message", {"from": SAM, "body": "real", "message_id": 4, "verified": True})
        a = self.adapter()
        await self.run_adapter(a, lambda: self.fake.polls >= 3)
        self.assertEqual([e.text for e in a.received], ["real"])

    async def test_unverified_can_be_allowed_explicitly(self):
        self.fake.add_event("message", {"from": SAM, "body": "old app", "message_id": 1, "verified": False})
        with mock.patch.dict(os.environ, {"CALLME_ALLOW_UNVERIFIED": "true"}):
            a = self.adapter()
        await self.run_adapter(a, lambda: a.received)
        self.assertEqual(a.received[0].text, "old app")

    async def test_voicemail_is_marked(self):
        self.fake.add_event("voicemail", {"from": SAM, "transcript": "call the plumber", "call_id": 5,
                                          "verified": True})
        a = self.adapter()
        await self.run_adapter(a, lambda: a.received)
        self.assertEqual(a.received[0].text, "[Voicemail] call the plumber")

    async def test_send_refuses_numbers_outside_the_allowlist_and_chunks(self):
        a = self.adapter()
        await a.connect()
        try:
            refused = await a.send(STRANGER, "hello")
            self.assertFalse(refused.success)
            sent = await a.send(ALEX, ("long " * 700).strip())
            self.assertTrue(sent.success)
        finally:
            await a.disconnect()
        to_alex = [m for m in self.fake.messages if m["to"] == ALEX and not m["body"].startswith("Hi, this is")]
        self.assertEqual(len(to_alex), 2)
        self.assertFalse(any(m["to"] == STRANGER for m in self.fake.messages))

    async def test_rejected_token_mints_a_new_session(self):
        api.save_state({"token": "dead", "number": "7000000000", "label": "Hermes", "cursor": 0,
                        "greeted": [SAM, ALEX]})
        a = self.adapter()
        await self.run_adapter(a, lambda: self.fake.polls >= 1 and len(self.fake.messages) >= 2)
        self.assertEqual(json.loads(self.state_file.read_text())["token"], "tok1")
        self.assertEqual(sorted(m["to"] for m in self.fake.messages), sorted([SAM, ALEX]))

    async def test_label_change_renames_the_thread(self):
        api.save_state({"token": "tok0", "number": "7000000000", "label": "Old", "cursor": 0,
                        "greeted": [SAM, ALEX]})
        self.fake.valid_tokens.add("tok0")
        with mock.patch.dict(os.environ, {"CALLME_LABEL": "Home bot"}):
            a = self.adapter()
        await self.run_adapter(a, lambda: self.fake.polls >= 1)
        self.assertEqual(self.fake.labels, ["Home bot"])
        self.assertEqual(self.fake.sessions, 0)


class Tool(Base):
    async def test_call_resolves_a_name_and_clamps_the_wait(self):
        out = json.loads(await adapter_mod._call_tool({"to": "Sam", "text": "Ship it?", "wait_seconds": 999}))
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["transcript"], "Yes, go ahead")
        self.assertEqual(self.fake.calls[0]["to"], SAM)
        self.assertEqual(self.fake.calls[0]["timeout_s"], api.MAX_CALL_WAIT)

    async def test_call_refuses_unknown_recipients(self):
        out = json.loads(await adapter_mod._call_tool({"to": STRANGER, "text": "hi"}))
        self.assertIn("unknown recipient", out["error"])
        out = json.loads(await adapter_mod._call_tool({"text": "hi"}))
        self.assertIn("unknown recipient", out["error"])
        self.assertEqual(self.fake.calls, [])

    async def test_standalone_send_defaults_to_the_first_number(self):
        result = await adapter_mod._standalone_send(None, "", "cron says hi")
        self.assertTrue(result["success"])
        self.assertEqual(self.fake.messages[-1]["to"], SAM)
        refused = await adapter_mod._standalone_send(None, STRANGER, "nope")
        self.assertFalse(refused.get("success", False))


class Registration(unittest.TestCase):
    def test_register_declares_platform_and_tool(self):
        ctx = mock.Mock()
        plugin.register(ctx)
        kwargs = ctx.register_platform.call_args.kwargs
        self.assertEqual(kwargs["name"], "callme")
        self.assertEqual(kwargs["allowed_users_env"], "CALLME_ALLOWED_USERS")
        self.assertEqual(ctx.register_tool.call_args.kwargs["name"], "callme_call")


if __name__ == "__main__":
    unittest.main()
