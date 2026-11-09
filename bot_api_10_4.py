"""Small Bot API 10.4 bridge for features not yet exposed by python-telegram-bot.

The bridge deliberately uses only the standard library.  It is used for
ephemeral messages and private widgets while the Telegram library catches up.
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.request import Request, urlopen


def _call_sync(method: str, payload: dict) -> dict:
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", f"Telegram API error in {method}"))
    return result.get("result")


async def call(method: str, **payload):
    """Call a Bot API method without blocking the bot's event loop."""
    return await asyncio.to_thread(_call_sync, method, payload)


async def send_ephemeral_message(*, callback_query_id: str, text: str,
                                 reply_markup: dict | None = None,
                                 parse_mode: str | None = None):
    payload = {
        "text": text,
        "ephemeral_message_parameters": {
            "callback_query_id": callback_query_id,
        },
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return await call("sendMessage", **payload)


async def send_widget(*, chat_id: int | str, html: str, caption: str = "",
                      chat_data: str = "", public_data: str = "",
                      allow_queries: bool = False, is_shared: bool = False,
                      width: int | None = None, height: int | None = None):
    """Send a Bot API 10.4 widget.

    Telegram currently accepts widgets in production only in private chats,
    while the test server also accepts groups and inline messages.  The
    adapter intentionally leaves that decision to Telegram instead of adding
    a local chat-type restriction.
    """
    payload = {
        "chat_id": chat_id,
        "html": html,
        "caption": caption,
        "chat_data": chat_data,
        "public_data": public_data,
        "allow_queries": allow_queries,
        "is_shared": is_shared,
    }
    if width is not None:
        payload["width"] = width
    if height is not None:
        payload["height"] = height
    return await call("sendWidget", **payload)


async def edit_message_widget_data(*, chat_id: int | str, message_id: int,
                                   chat_data: str = "", public_data: str = ""):
    return await call("editMessageWidgetData", chat_id=chat_id,
                      message_id=message_id, chat_data=chat_data,
                      public_data=public_data)


async def answer_widget_query(*, widget_query_id: str, data: str = ""):
    return await call("answerWidgetQuery", widget_query_id=widget_query_id,
                      data=data)

