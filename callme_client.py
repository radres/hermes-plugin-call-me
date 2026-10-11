import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import httpx

API = "https://callmemcp.com/aiphone"
PLUGIN_NAME = "call-me"
MAX_TEXT = 2000
MAX_SPOKEN = 2000
MIN_CALL_WAIT = 30
MAX_CALL_WAIT = 300
DEFAULT_LABEL = "Hermes"
USER_AGENT = "hermes-plugin-call-me/0.1.0"


class CallMeError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SessionGone(CallMeError):
    pass


def digits(value: Any) -> str:
    raw = re.sub(r"\D", "", str(value or ""))
    if len(raw) == 11 and raw.startswith("1"):
        raw = raw[1:]
    return raw


def display(number: str) -> str:
    n = digits(number)
    return f"{n[:3]}-{n[3:6]}-{n[6:]}" if len(n) == 10 else n


def parse_numbers(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,;\n]+", str(raw or ""))
    seen: list[str] = []
    for item in items:
        n = digits(item)
        if len(n) == 10 and n not in seen:
            seen.append(n)
    return seen


def parse_names(raw: Any) -> dict[str, str]:
    names: dict[str, str] = {}
    for item in re.split(r"[,;]+", str(raw or "")):
        number, sep, name = item.partition("=")
        if not sep:
            number, sep, name = item.partition(":")
        n, name = digits(number), name.strip()
        if sep and len(n) == 10 and name:
            names[n] = name[:40]
    return names


def resolve_person(who: Any, numbers: list[str], names: dict[str, str]) -> Optional[str]:
    text = str(who or "").strip()
    if not text:
        return numbers[0] if len(numbers) == 1 else None
    n = digits(text)
    if n in numbers:
        return n
    wanted = text.casefold()
    for number in numbers:
        if names.get(number, "").casefold() == wanted:
            return number
    return None


def chunks(text: str, limit: int = MAX_TEXT) -> list[str]:
    text = (text or "").strip()
    out: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


def state_path() -> Path:
    from plugins.plugin_storage import plugin_data_dir

    return plugin_data_dir(PLUGIN_NAME) / "session.json"


def load_state(path: Optional[Path] = None) -> dict:
    path = path or state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, path: Optional[Path] = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def new_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, headers={"user-agent": USER_AGENT})


async def _request(client: httpx.AsyncClient, method: str, path: str, *, timeout: Optional[float] = None,
                   **kwargs: Any) -> dict:
    if timeout is not None:
        kwargs["timeout"] = timeout
    resp = await client.request(method, f"{API}{path}", **kwargs)
    if resp.status_code >= 300:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = None
        detail = detail if isinstance(detail, str) else resp.text[:300]
        if resp.status_code == 401 and "session token" in detail:
            raise SessionGone(resp.status_code, detail)
        raise CallMeError(resp.status_code, detail)
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def create_session(client: httpx.AsyncClient, label: str) -> dict:
    return await _request(client, "POST", "/sessions", json={"label": label[:80]})


async def set_label(client: httpx.AsyncClient, token: str, label: str) -> dict:
    return await _request(client, "POST", "/sessions/label", json={"session_token": token, "label": label[:80]})


async def send_text(client: httpx.AsyncClient, token: str, to: str, body: str) -> dict:
    return await _request(client, "POST", "/messages",
                          json={"session_token": token, "to": digits(to), "body": body[:MAX_TEXT]})


async def place_call(client: httpx.AsyncClient, token: str, to: str, text: str, wait_s: int) -> dict:
    wait_s = max(MIN_CALL_WAIT, min(MAX_CALL_WAIT, int(wait_s)))
    return await _request(client, "POST", "/calls", timeout=wait_s + 30,
                          json={"session_token": token, "to": digits(to), "text": text[:MAX_SPOKEN],
                                "timeout_s": wait_s})


async def poll_events(client: httpx.AsyncClient, token: str, cursor: int, wait: int = 25) -> dict:
    return await _request(client, "GET", "/sessions/events", timeout=wait + 20,
                          params={"session_token": token, "cursor": cursor, "wait": wait})


async def ensure_session(client: httpx.AsyncClient, label: str, path: Optional[Path] = None) -> dict:
    state = load_state(path)
    if state.get("token") and state.get("number"):
        if state.get("label") != label:
            try:
                await set_label(client, state["token"], label)
                state["label"] = label
                save_state(state, path)
            except SessionGone:
                return await mint_session(client, label, path)
            except (CallMeError, httpx.HTTPError):
                pass
        return state
    return await mint_session(client, label, path)


async def mint_session(client: httpx.AsyncClient, label: str, path: Optional[Path] = None) -> dict:
    created = await create_session(client, label)
    state = {
        "token": created["session_token"],
        "number": created["session_number"],
        "label": label,
        "cursor": 0,
        "greeted": [],
    }
    save_state(state, path)
    return state
