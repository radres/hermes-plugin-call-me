import asyncio
import json
import logging
import time
from datetime import datetime, UTC
from typing import Any, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms._shared import (
    extra_or_secret as _extra_or_secret,
    get_scoped_secret as _get_scoped_secret,
    seed_extra_from_env as _seed_extra_from_env,
    send_error,
)

from . import callme_client as api

logger = logging.getLogger(__name__)

PLATFORM = "callme"
MIN_CYCLE_S = 3.0
POLL_WAIT_S = 25
BACKOFF_S = [3, 5, 10, 30, 60]
RATE_LIMIT_PAUSE_S = 60
_TRUTHY = ("1", "true", "yes", "on")

GREETING = (
    "Hi, this is {label}, your Hermes agent. Text me here any time and I'll answer. "
    "This thread is how I'll reach you too."
)

PLATFORM_HINT = (
    "You are texting with the user through /call-me, an iPhone app that gives your agent a phone "
    "number. Write like a text message: plain text, no markdown, short paragraphs. Each message is "
    "capped at 2000 characters. When a text is not enough (something urgent, or you need a spoken "
    "answer right now), use the callme_call tool to ring their phone; it speaks your text and returns "
    "what they said. Messages that start with [Voicemail] were spoken by the user and transcribed."
)


def _people() -> tuple[list[str], dict[str, str]]:
    numbers = api.parse_numbers(_get_scoped_secret("CALLME_ALLOWED_USERS", ""))
    names = api.parse_names(_get_scoped_secret("CALLME_NAMES", ""))
    return numbers, {n: names[n] for n in numbers if n in names}


def _label(extra: Optional[dict] = None) -> str:
    return str(_extra_or_secret(extra or {}, "label", "CALLME_LABEL", api.DEFAULT_LABEL)).strip()[:80] or api.DEFAULT_LABEL


def _allow_unverified(extra: Optional[dict] = None) -> bool:
    return str(_extra_or_secret(extra or {}, "allow_unverified", "CALLME_ALLOW_UNVERIFIED", "")).strip().lower() in _TRUTHY


def check_requirements() -> bool:
    return bool(_people()[0])


def validate_config(config) -> bool:
    return check_requirements()


def is_connected(config) -> bool:
    return check_requirements()


class CallMeAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = api.MAX_TEXT

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM))
        extra = config.extra or {}
        self._label = _label(extra)
        self._numbers, self._names = _people()
        self._allow_unverified = _allow_unverified(extra)
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None
        self._state: dict = {}
        self._session_lock = asyncio.Lock()
        self._dedup = MessageDeduplicator(max_size=1000, ttl_seconds=600)

    def normalize_user_id(self, user_id: Any) -> str:
        return api.digits(user_id)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._numbers:
            logger.warning("[%s] CALLME_ALLOWED_USERS has no 10-digit /call-me number", self.name)
            return False
        self._client = api.new_client()
        self._task = asyncio.create_task(self._run())
        self._mark_connected()
        self._wire_plugin_handlers(None)
        logger.info("[%s] Connected; allowed numbers: %s", self.name,
                    ", ".join(api.display(n) for n in self._numbers))
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None
        self._dedup.clear()

    async def _session(self, *, fresh: bool = False) -> dict:
        async with self._session_lock:
            if fresh:
                self._state = await api.mint_session(self._client, self._label)
                logger.info("[%s] Minted a new /call-me session %s", self.name, api.display(self._state["number"]))
            elif not self._state.get("token"):
                self._state = await api.ensure_session(self._client, self._label)
            return self._state

    async def _greet(self) -> None:
        state = await self._session()
        greeted = list(state.get("greeted") or [])
        for number in self._numbers:
            if number in greeted:
                continue
            try:
                result = await api.send_text(self._client, state["token"], number, GREETING.format(label=self._label))
            except api.CallMeError as e:
                logger.warning("[%s] Could not greet %s: %s", self.name, api.display(number), e.detail)
                continue
            greeted.append(number)
            if not result.get("delivered", True):
                logger.warning("[%s] Greeting to %s saved but not shown: %s", self.name, api.display(number),
                               result.get("notice", ""))
        state["greeted"] = greeted
        api.save_state(state)

    async def _run(self) -> None:
        failures = 0
        greeted = False
        while self._running:
            started = time.monotonic()
            try:
                if not greeted:
                    await self._greet()
                    greeted = True
                await self._poll_once()
                failures = 0
            except asyncio.CancelledError:
                return
            except api.SessionGone:
                logger.warning("[%s] Session token was rejected; minting a new session", self.name)
                try:
                    await self._session(fresh=True)
                except Exception as e:
                    logger.warning("[%s] Could not mint a session: %s", self.name, e)
                greeted = False
                continue
            except api.CallMeError as e:
                if e.status == 429:
                    logger.warning("[%s] Rate limited by /call-me: %s", self.name, e.detail)
                    await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                    continue
                failures += 1
                logger.warning("[%s] /call-me error: %s", self.name, e)
            except Exception as e:
                failures += 1
                logger.warning("[%s] Poll failed: %s", self.name, e)
            if failures:
                await asyncio.sleep(BACKOFF_S[min(failures, len(BACKOFF_S)) - 1])
            await asyncio.sleep(max(0.0, MIN_CYCLE_S - (time.monotonic() - started)))

    async def _poll_once(self) -> None:
        state = await self._session()
        cursor = int(state.get("cursor") or 0)
        data = await api.poll_events(self._client, state["token"], cursor, POLL_WAIT_S)
        for event in data.get("events") or []:
            try:
                await self._on_event(event)
            except Exception:
                logger.exception("[%s] Failed to handle event %s", self.name, event.get("id"))
        new_cursor = int(data.get("cursor") or cursor)
        if new_cursor != cursor:
            state["cursor"] = new_cursor
            api.save_state(state)

    async def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind == "message":
            text = str(payload.get("body") or "").strip()
            ref = payload.get("message_id")
        elif kind == "voicemail":
            text = str(payload.get("transcript") or "").strip()
            ref = payload.get("call_id")
            text = f"[Voicemail] {text}" if text else ""
        else:
            return
        sender = api.digits(payload.get("from"))
        if not text or not sender:
            return
        if sender not in self._numbers:
            logger.info("[%s] Ignoring %s from %s: not in CALLME_ALLOWED_USERS", self.name, kind,
                        api.display(sender))
            return
        if payload.get("verified") is not True and not self._allow_unverified:
            logger.warning("[%s] Ignoring %s claiming to be %s: the sender's device is not verified "
                           "(update the /call-me app, or set CALLME_ALLOW_UNVERIFIED=true)",
                           self.name, kind, api.display(sender))
            return
        msg_id = f"{kind}-{ref or event.get('id')}"
        if self._dedup.is_duplicate(msg_id):
            return
        name = self._names.get(sender) or api.display(sender)
        source = self.build_source(chat_id=sender, chat_name=name, chat_type="dm", user_id=sender,
                                   user_name=name, message_id=msg_id)
        timestamp = datetime.now(tz=UTC)
        try:
            timestamp = datetime.fromisoformat(str(event.get("created_at"))).replace(tzinfo=UTC)
        except (TypeError, ValueError):
            pass
        await self.handle_message(MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source, message_id=msg_id,
            raw_message=event, timestamp=timestamp))

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[dict[str, Any]] = None) -> SendResult:
        to = api.digits(chat_id)
        if to not in self._numbers:
            return SendResult(success=False, error=f"{api.display(to) or chat_id} is not in CALLME_ALLOWED_USERS")
        if not self._client:
            return SendResult(success=False, error="/call-me adapter is not connected")
        last_id = None
        try:
            for part in api.chunks(content):
                state = await self._session()
                try:
                    result = await api.send_text(self._client, state["token"], to, part)
                except api.SessionGone:
                    state = await self._session(fresh=True)
                    result = await api.send_text(self._client, state["token"], to, part)
                if not result.get("delivered", True):
                    logger.warning("[%s] Text to %s saved but not shown: %s", self.name, api.display(to),
                                   result.get("notice", ""))
                last_id = str(result.get("message_id") or "")
        except api.CallMeError as e:
            return SendResult(success=False, error=e.detail)
        except httpx.HTTPError as e:
            return SendResult(success=False, error=f"/call-me unreachable: {type(e).__name__}")
        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        n = api.digits(chat_id)
        return {"name": self._names.get(n) or api.display(n), "type": "dm"}


def _env_enablement() -> dict | None:
    numbers, _ = _people()
    if not numbers:
        return None
    seed = _seed_extra_from_env((("CALLME_LABEL", "label", None),), home_env="CALLME_HOME_CHANNEL",
                                home_default=numbers[0])
    home = seed.get("home_channel")
    if home:
        home["chat_id"] = api.digits(home["chat_id"]) or numbers[0]
    return seed


async def _with_session(fn):
    async with api.new_client() as client:
        state = await api.ensure_session(client, _label())
        try:
            return await fn(client, state)
        except api.SessionGone:
            state = await api.mint_session(client, _label())
            return await fn(client, state)


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list[str]] = None, force_document: bool = False) -> dict[str, Any]:
    numbers, _ = _people()
    to = api.digits(chat_id) or (numbers[0] if numbers else "")
    if to not in numbers:
        return send_error(f"/call-me: {api.display(to) or chat_id} is not in CALLME_ALLOWED_USERS")

    async def go(client, state):
        last = {}
        for part in api.chunks(message):
            last = await api.send_text(client, state["token"], to, part)
        return last

    try:
        result = await _with_session(go)
    except api.CallMeError as e:
        return send_error(f"/call-me: {e.detail}")
    except Exception as e:
        return send_error(f"/call-me send failed: {type(e).__name__}")
    return {"success": True, "platform": PLATFORM, "chat_id": to, "message_id": str(result.get("message_id") or "")}


CALL_TOOL_SCHEMA = {
    "name": "callme_call",
    "description": (
        "Ring the user's iPhone through /call-me, speak your text aloud, and wait for their spoken "
        "answer, which comes back as a transcript. Only numbers in CALLME_ALLOWED_USERS can be called. "
        "Prefer a text message; call when it is urgent or a spoken answer is needed now. Keep the text "
        "short and end with a question that can be answered in one sentence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": (
                "Who to call: a 10-digit /call-me number or a name from CALLME_NAMES. "
                "Optional when only one number is allowed.")},
            "text": {"type": "string", "description": "What to say. Spoken verbatim, at most 2000 characters."},
            "wait_seconds": {"type": "integer", "description": (
                "How long to wait for the call to finish, 30 to 300 seconds. Default 300.")},
        },
        "required": ["text"],
    },
}


async def _call_tool(args: dict, **_: Any) -> str:
    numbers, names = _people()
    text = str(args.get("text") or "").strip()
    if not text:
        return json.dumps({"error": "text is required"})
    to = api.resolve_person(args.get("to"), numbers, names)
    if not to:
        allowed = ", ".join(names.get(n) or api.display(n) for n in numbers) or "nobody"
        return json.dumps({"error": f"unknown recipient {args.get('to')!r}; allowed: {allowed}"})
    try:
        wait_s = int(args.get("wait_seconds") or api.MAX_CALL_WAIT)
    except (TypeError, ValueError):
        wait_s = api.MAX_CALL_WAIT
    try:
        result = await _with_session(lambda client, state: api.place_call(client, state["token"], to, text, wait_s))
    except api.CallMeError as e:
        return json.dumps({"error": e.detail, "status_code": e.status})
    except httpx.HTTPError as e:
        return json.dumps({"error": f"/call-me unreachable: {type(e).__name__}"})
    status = result.get("status")
    out = {"status": status, "transcript": result.get("transcript") or "", "to": names.get(to) or api.display(to)}
    if status != "completed":
        out["hint"] = "Nobody answered. Send a text instead of calling again right away."
    return json.dumps(out, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM, label="/call-me", adapter_factory=lambda cfg: CallMeAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config, is_connected=is_connected,
        required_env=["CALLME_ALLOWED_USERS"], install_hint="no extra packages; httpx ships with Hermes",
        env_enablement_fn=_env_enablement, cron_deliver_env_var="CALLME_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send, allowed_users_env="CALLME_ALLOWED_USERS",
        max_message_length=api.MAX_TEXT, emoji="📞", pii_safe=False, platform_hint=PLATFORM_HINT)
    ctx.register_tool(
        name="callme_call", toolset="callme", schema=CALL_TOOL_SCHEMA, handler=_call_tool,
        check_fn=check_requirements, is_async=True, description=CALL_TOOL_SCHEMA["description"], emoji="📞")
