from forge_lib import state, util
from forge_lib.errors import UsageError

TYPES = ("unit", "integration", "e2e", "manual", "ci")
STATUSES = ("pending", "red", "green", "skipped")


def add(key: str, name: str, type_: str, command: str = "", expected: str = "") -> dict:
    if type_ not in TYPES:
        raise UsageError(f"test type must be one of {', '.join(TYPES)}")
    state.load_task(key)
    items = state.load_json_items(key, "tests")
    item = {"id": util.next_id(items, "TST-"), "name": name, "type": type_, "command": command or "",
            "expected": expected or "", "status": "pending", "evidence": "", "skip_reason": "",
            "updated": util.now_iso()}
    items.append(item)
    state.save_json_items(key, "tests", items)
    return item


def set_status(key: str, test_id: str, status: str, evidence: str | None = None,
               skip_reason: str | None = None) -> dict:
    if status not in STATUSES:
        raise UsageError(f"test status must be one of {', '.join(STATUSES)}")
    items = state.load_json_items(key, "tests")
    item = state.find_item(items, test_id, "test")
    item["status"] = status
    if evidence is not None:
        item["evidence"] = evidence
    if skip_reason is not None:
        item["skip_reason"] = skip_reason
    if status == "skipped" and not item.get("skip_reason"):
        raise UsageError("--skip-reason is required when status is skipped")
    item["updated"] = util.now_iso()
    state.save_json_items(key, "tests", items)
    return item
