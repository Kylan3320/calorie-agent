"""Factual tool feedback shared by the native loop and completion checks."""


def unmet_conditions(result):
    data = result.get("data", {})
    rows = [*data.get("unmet_conditions", [])]
    for item in data.get("unfinished", []):
        for gap in item.get("unmet_conditions", []):
            row = {**gap, **({"ref": item["pending_id"]} if item.get("pending_id") and not gap.get("ref") else {})}
            if row not in rows:
                rows.append(row)
    for item in data.get("queries", []):
        rows.extend(row for row in unmet_conditions(item) if row not in rows)
    return rows


def describe(result, remaining):
    rows = unmet_conditions(result)
    status, data = result.get("status"), result.get("data", {})
    web_status = data.get("portion_web", {}).get("status")
    if status in {"error", "limit"} or web_status == "service_error":
        outcome = "failed"
    elif rows:
        outcome = "needs_user" if all(row.get("requires_user") is True for row in rows) else "unresolved"
    elif status == "proposal":
        outcome = "needs_user"
    elif status == "pending" or data.get("unfinished"):
        outcome = "needs_user" if data.get("question") else "unresolved"
    elif data.get("selection", {}).get("awaiting_choice"):
        outcome = "unresolved"
    elif web_status in {"no_results", "unreadable", "not_applicable", "budget_exhausted", "repeated_request"}:
        outcome = "unresolved"
    else:
        outcome = "satisfied"
    return {"outcome": outcome, "unmet_conditions": rows, "remaining": remaining}


def new_unmet_conditions(result, prior):
    """A finish declaration made before discovery cannot decide a new semantic gap."""
    def key(row):
        return row.get("code"), row.get("ref"), str(row.get("detail", ""))
    seen = {key(row) for old in prior for row in unmet_conditions(old)}
    return [row for row in unmet_conditions(result) if not row.get("requires_user")
            and key(row) not in seen]
