"""Persistent local journal of inbound Telegram requests."""
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3


def _connect(path):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS requests "
        "(id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    return db


def record(path, admin_chat_id, update):
    message = update.get("message")
    callback = update.get("callback_query")
    if isinstance(message, dict):
        chat = message.get("chat", {})
        payload = {"kind": "message", "message": message}
    elif isinstance(callback, dict):
        chat = (callback.get("message") or {}).get("chat", {})
        payload = {"kind": "callback", "from": callback.get("from"), "data": callback.get("data")}
    else:
        return
    chat_id = chat.get("id")
    update_id = update.get("update_id")
    if type(chat_id) is not int or chat_id == admin_chat_id or type(update_id) is not int:
        return
    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    payload["chat_id"] = chat_id
    with closing(_connect(path)) as db, db:
        db.execute("INSERT OR IGNORE INTO requests VALUES (?, ?, ?)",
                   (update_id, chat_id, json.dumps(payload, ensure_ascii=False)))


def read(path, limit, before_id, chat_id):
    with closing(_connect(path)) as db:
        rows = db.execute(
            "SELECT id, payload FROM requests WHERE (? IS NULL OR id < ?) "
            "AND (? IS NULL OR chat_id = ?) ORDER BY id DESC LIMIT ?",
            (before_id, before_id, chat_id, chat_id, limit + 1),
        ).fetchall()
    return {
        "requests": [{"update_id": row_id, **json.loads(payload)} for row_id, payload in rows[:limit]],
        "next_before_id": rows[limit - 1][0] if len(rows) > limit else None,
    }
