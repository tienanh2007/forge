"""Comment thread between user/coordinator and the worker."""
from forge_lib import state, util
from forge_lib.errors import UsageError

AUTHORS = ("user", "worker", "coordinator")


def add(key: str, body: str, issue_id: str | None = None, author: str = "user",
        reply_to: str | None = None) -> dict:
    if author not in AUTHORS:
        raise UsageError(f"author must be one of {', '.join(AUTHORS)}")
    if not body or not body.strip():
        raise UsageError("message body is empty")
    state.load_task(key)
    items = state.load_json_items(key, "inbox")
    if issue_id:
        state.find_item(state.load_json_items(key, "issues"), issue_id, "issue")
    if reply_to:
        state.find_item(items, reply_to, "message")
    msg = {"id": util.next_id(items, "C-"), "issue_id": issue_id, "author": author, "body": body,
           "at": util.now_iso(), "reply_to": reply_to,
           "status": "open", "delivered": False}
    items.append(msg)
    state.save_json_items(key, "inbox", items)
    return msg


def list_items(key: str, open_only: bool = False) -> list[dict]:
    return state.open_inbox(key) if open_only else state.load_json_items(key, "inbox")


def _update(key: str, msg_id: str, **fields) -> dict:
    items = state.load_json_items(key, "inbox")
    msg = state.find_item(items, msg_id, "message")
    msg.update(fields)
    state.save_json_items(key, "inbox", items)
    return msg


def ack(key: str, msg_id: str) -> dict:
    return _update(key, msg_id, status="acked", delivered=True)


def resolve(key: str, msg_id: str, reply: str | None = None) -> dict:
    msg = _update(key, msg_id, status="resolved", delivered=True)
    if reply:
        add(key, reply, issue_id=msg.get("issue_id"), author="worker", reply_to=msg_id)
    return msg


def mark_delivered(key: str, msg_id: str) -> dict:
    return _update(key, msg_id, delivered=True)


def pending() -> list[dict]:
    """Undelivered open user messages across all tasks, each with its task_key."""
    return [{"task_key": key, **m} for key in state.all_task_keys()
            for m in state.load_json_items(key, "inbox")
            if m.get("author") == "user" and m.get("status") == "open" and not m.get("delivered")]
