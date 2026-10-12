"""Settings kept in Telegram itself — a pinned message that outlives the process.

Why this exists
---------------
A serverless host (Vercel) has no disk that survives the next request, so the few
values that make the bot work need somewhere durable: the settings the setup page
saves, and the Redis/Upstash REST pair an admin types into Telegram with
``/database``.  A KV store is the best answer, but it used to be a *requirement*
before the bot would start at all — which meant the owner could not bring the bot
up first and connect the database from Telegram afterwards.

Telegram is that fallback: the bot pins **one** message in an admin's private chat
and reads it back with ``getChat`` (``ChatFullInfo`` → ``pinned_message``) on the
next start.  Nothing else is needed — no card, no account, no redeploy.

What is inside the pin
----------------------
The message holds a *sealed* copy of the values: JSON encrypted with a keystream
derived from the bot token (HMAC-SHA256 as the PRF, encrypt-then-MAC, stdlib
only — no new dependency on Vercel's build).  The chat therefore never shows a
readable token, and a copy of the message is useless without the deployment's
``BOT_TOKEN``.  This is protection against casual exposure (screenshots, chat
exports) and it is not a vault: deployment environment variables stay the
authoritative place for credentials, and the bot says so when it writes the pin.

Design rules
------------
* Nothing here ever raises.  A failure is logged and reported as "no settings",
  because a deployment that cannot read its settings must still be able to render
  the page (and the Telegram menu) that asks for them.
* The copy is verified after it is written (:meth:`TelegramConfigStore.probe`):
  a Telegram client that hides the bot's own pin would otherwise lose the
  fallback silently — exactly the outage this project keeps fighting.
* This is a *settings* store, not a state database.  The group, the merchants and
  the prices stay in ``storage.py``'s state backend; a real database is still
  what makes them shared and durable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Iterable

log = logging.getLogger("p2p-bot.telegram-store")

#: The line a human sees on the pinned message.  It has to be recognisable in a
#: chat and stable across versions, because it is how the pin is found again.
MARKER = "🗄 P2P bot settings — do not delete"

#: Prefix of the sealed payload.  Bump it when the format changes; a message with
#: an unknown tag is ignored instead of being misread.
TAG = "P2P-CONFIG1"

#: Domain separation, so the same token used elsewhere never derives this key.
KEY_INFO = b"p2p-config-store-v1"

#: Telegram rejects messages longer than 4096 characters; the seal is tiny but a
#: runaway value must not end up as a rejected API call.
MAX_PAYLOAD = 3500

#: How long a read (including "there is no pin") is reused inside one process.
#: Every request would otherwise cost one ``getChat`` per admin chat.
CACHE_SECONDS = 45.0
_cache: dict[str, dict[str, Any]] = {}


class TelegramError(RuntimeError):
    """A Telegram API call failed — reported, never raised to the caller."""


# ── sealing (encrypt-then-MAC, standard primitives only) ───────────────────
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _derive(secret: str, salt: bytes) -> tuple[bytes, bytes]:
    """``(encryption key, MAC key)`` from the bot token and a per-message salt."""
    prk = hmac.new(str(secret).encode("utf-8"), salt, hashlib.sha256).digest()
    enc = hmac.new(prk, KEY_INFO + b"|enc", hashlib.sha256).digest()
    mac = hmac.new(prk, KEY_INFO + b"|mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    """HMAC-SHA256 in counter mode — a stream cipher built from one primitive."""
    blocks = bytearray()
    counter = 0
    while len(blocks) < size:
        blocks += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(blocks[:size])


def seal(values: dict, secret: str) -> str:
    """Seal ``values`` for a chat message.  Returns ``TAG.salt.nonce.body.tag``.

    The bot token is the key material and every message draws a fresh salt and
    nonce, so the same settings never produce the same ciphertext twice.
    """
    raw = json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(16)
    enc_key, mac_key = _derive(secret, salt)
    body = bytes(a ^ b for a, b in zip(raw, _keystream(enc_key, nonce, len(raw))))
    tag = hmac.new(mac_key, nonce + body, hashlib.sha256).digest()
    return ".".join([TAG, _b64(salt), _b64(nonce), _b64(body), _b64(tag)])


def open_sealed(blob: str, secret: str) -> dict | None:
    """The values inside a sealed payload, or ``None`` when it cannot be trusted.

    Every failure mode — wrong token, edited message, truncated text, a payload
    from another version — ends in ``None``; the caller then behaves like a fresh
    deployment instead of guessing.
    """
    try:
        parts = str(blob or "").strip().split(".")
        if len(parts) != 5 or parts[0] != TAG:
            return None
        salt, nonce, body, tag = (_unb64(item) for item in parts[1:])
        enc_key, mac_key = _derive(secret, salt)
        if not hmac.compare_digest(tag, hmac.new(mac_key, nonce + body, hashlib.sha256).digest()):
            return None
        raw = bytes(a ^ b for a, b in zip(body, _keystream(enc_key, nonce, len(body))))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:                              # wrong token, bad JSON, …
        log.debug("Sealed settings could not be opened: %s", exc)
        return None
    return data if isinstance(data, dict) else None


# ── the message the chat shows ─────────────────────────────────────────────
def payload_of(text: str) -> str | None:
    """The sealed line inside a message, or ``None`` when it is not ours."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line.startswith(TAG + "."):
            return line
    return None


def message_text(blob: str, note: str = "") -> str:
    """The pinned message: a human-readable explanation plus the sealed payload.

    The explanation matters — an admin who finds a mysterious pinned message in
    the bot chat should be able to tell what it is, why it exists, and when it is
    safe to delete (answer: they can delete it, the bot will then ask for the
    database again).
    """
    about = (
        "The bot keeps its saved settings in this pinned message, because this "
        "deployment has no storage that survives a request.\n"
        "It is read back with getChat on every start. The contents are sealed with "
        "the bot token, so this chat holds no readable secret — but deleting or "
        "editing this message makes the bot forget them."
    )
    return f"{MARKER}\n\n{about}{(' ' + note) if note else ''}\n\n{blob}"


# ── the store ──────────────────────────────────────────────────────────────
class TelegramConfigStore:
    """``load``/``save``/``probe`` over a pinned message in an admin's chat.

    ``chat_ids`` is ordered by preference: the chat the admin is using right now
    first (``runtime_config.prefer_chat``), then the remaining admins — a bot in
    a group of admins may have several chats to search, and only one of them
    needs the pin.
    """

    backend = "telegram"

    def __init__(self, token: str, chat_ids: Iterable[int], timeout: float = 6.0,
                 api_base: str = "https://api.telegram.org",
                 cache_seconds: float = CACHE_SECONDS):
        seen: list[int] = []
        for value in chat_ids or ():
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                continue
            if chat_id and chat_id not in seen:
                seen.append(chat_id)
        self.token = str(token or "").strip()
        self.chat_ids = seen
        self.timeout = float(timeout)
        self.api_base = str(api_base).rstrip("/")
        self.cache_seconds = float(cache_seconds)
        self._where: tuple[int, int] | None = None         # (chat id, message id)
        # Chats Telegram refused to show (an admin who never started the bot, a
        # deleted account): asking again is one wasted round-trip per write.
        self._unusable: set[int] = set()

    # -- low level ---------------------------------------------------------
    @property
    def signature(self) -> str:
        """Cache key for this token + chat set — hashed, so no secret is stored."""
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:12]
        return digest + "|" + ",".join(str(chat) for chat in self.chat_ids)

    def _call(self, method: str, payload: dict | None = None,
              timeout: float | None = None) -> Any:
        """One Bot API call.  Raises :class:`TelegramError`, never httpx errors."""
        import httpx

        if not self.token or not self.chat_ids:
            raise TelegramError("no bot token or admin chat to keep the settings in")
        url = f"{self.api_base}/bot{self.token}/{method}"
        try:
            reply = httpx.post(url, json=dict(payload or {}),
                               timeout=self.timeout if timeout is None else timeout)
            reply.raise_for_status()
            body = reply.json()
        except Exception as exc:                          # offline, HTTP error, HTML reply
            raise TelegramError(f"{type(exc).__name__}: {exc}") from None
        if not isinstance(body, dict) or not body.get("ok"):
            detail = (body or {}).get("description") if isinstance(body, dict) else ""
            raise TelegramError(f"Telegram rejected {method}"
                                f"{': ' + str(detail) if detail else ''}")
        return body.get("result")

    def pinned(self, chat_id: int, timeout: float | None = None) -> dict | None:
        """The most recent pinned message of a chat, when Telegram returns one."""
        result = self._call("getChat", {"chat_id": chat_id}, timeout=timeout)
        pinned = (result or {}).get("pinned_message") if isinstance(result, dict) else None
        return pinned if isinstance(pinned, dict) else None

    # -- api ---------------------------------------------------------------
    def load(self) -> dict | None:
        """The sealed settings, or ``None`` when there are none (or unreadable)."""
        entry = _cache.get(self.signature)
        if entry and time.monotonic() - entry["at"] < self.cache_seconds:
            self._where = entry.get("where")
            values = entry.get("values")
            return dict(values) if isinstance(values, dict) else None
        values, where, _notes = self.fetch()
        _cache[self.signature] = {"at": time.monotonic(), "values": values, "where": where}
        self._where = where
        return dict(values) if isinstance(values, dict) else None

    def fetch(self, timeout: float | None = None) -> tuple[dict | None, tuple[int, int] | None, list[str]]:
        """``(values, where, notes)`` — the real read, without the process cache.

        ``notes`` carries one line per chat that answered something unexpected, so
        ``/database`` can tell the admin *why* the fallback is not working instead
        of just saying "nothing found".
        """
        notes: list[str] = []
        for chat_id in self.chat_ids:
            try:
                pinned = self.pinned(chat_id, timeout=timeout)
            except TelegramError as exc:
                notes.append(f"chat {chat_id}: {exc}")
                self._unusable.add(chat_id)
                continue
            if not pinned:
                notes.append(f"chat {chat_id}: no pinned message")
                continue
            blob = payload_of(pinned.get("text") or pinned.get("caption") or "")
            if not blob:
                notes.append(f"chat {chat_id}: the pinned message is not the bot's settings copy")
                continue
            values = open_sealed(blob, self.token)
            if values is None:
                notes.append(f"chat {chat_id}: the pinned settings copy cannot be opened "
                             "(it was sealed with a different bot token)")
                continue
            message_id = pinned.get("message_id")
            where = (chat_id, int(message_id)) if isinstance(message_id, int) else None
            return values, where, notes
        return None, None, notes

    def save(self, data: dict) -> None:
        """Pin the settings in the first usable chat, editing the message in place."""
        if not self.token or not self.chat_ids:
            return
        try:
            blob = seal(dict(data or {}), self.token)
        except Exception as exc:                          # pragma: no cover - defensive
            log.warning("Settings could not be sealed for the chat copy: %s", exc)
            return
        if len(blob) > MAX_PAYLOAD:
            log.warning("The settings are too large for a pinned message (%d characters) — "
                        "the chat copy was not written", len(blob))
            return
        text = message_text(blob)
        if self._where is None:
            self.load()                                   # reuse an existing pin when there is one
        if self._where is not None and self._edit(text):
            self._remember(data)
            return
        if self._where is None:
            for chat_id in self.chat_ids:
                if chat_id in self._unusable:
                    continue                      # Telegram already refused this chat
                if self._send_and_pin(chat_id, text):
                    self._remember(data)
                    return
        log.warning("The settings could not be kept in a pinned Telegram message")

    def _remember(self, data: dict) -> None:
        _cache[self.signature] = {"at": time.monotonic(), "values": dict(data or {}),
                                  "where": self._where}

    def _edit(self, text: str) -> bool:
        chat_id, message_id = self._where or (0, 0)
        try:
            self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                           "text": text, "disable_web_page_preview": True})
            return True
        except TelegramError as exc:
            log.debug("The pinned settings message could not be edited (%s) — pinning a new one", exc)
            self._where = None
            return False

    def _send_and_pin(self, chat_id: int, text: str) -> bool:
        try:
            sent = self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                              "disable_web_page_preview": True})
            message_id = (sent or {}).get("message_id") if isinstance(sent, dict) else None
            if not isinstance(message_id, int):
                return False
            self._call("pinChatMessage", {"chat_id": chat_id, "message_id": message_id,
                                          "disable_notification": True})
        except TelegramError as exc:
            log.warning("The settings could not be pinned in chat %s: %s", chat_id, exc)
            return False
        self._where = (chat_id, message_id)
        return True

    def probe(self, timeout: float | None = None) -> tuple[bool, str]:
        """Whether the pinned copy can really be read back — the honest check.

        Called after a write (and by ``/database``): if the chat is one the bot
        cannot re-read — an admin who never started the bot, a client that hides
        its own pins, a token that changed since the copy was made — this says so
        instead of leaving the owner with a fallback that silently fails later.
        """
        if not self.token or not self.chat_ids:
            return False, "no admin chat is available for the pinned settings copy"
        values, where, notes = self.fetch(timeout=timeout)
        if values is not None and where is not None:
            return True, f"the pinned settings copy in chat {where[0]} is readable"
        if values is not None:
            return True, "the pinned settings copy is readable"
        detail = "; ".join(notes[-2:]) if notes else "no pinned message was found"
        return False, detail

    def forget(self) -> None:
        """Drop the in-process cache (a write from another instance invalidates it)."""
        _cache.pop(self.signature, None)
        self._where = None

    def describe(self) -> str:
        chats = ", ".join(str(chat) for chat in self.chat_ids[:2])
        more = f" +{len(self.chat_ids) - 2}" if len(self.chat_ids) > 2 else ""
        return f"telegram (pinned message in chat {chats}{more})"


def reset_cache() -> None:
    """Forget every cached read (tests, and right after a settings change)."""
    _cache.clear()
