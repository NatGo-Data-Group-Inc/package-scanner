from __future__ import annotations


APPROVAL_PHASES = {
    "awaiting-approval",
    "approval-pending",
    "package-validation-approval-pending",
    # Compatibility with executions created before the terminology change.
    "preflight-approval-pending",
}
MATERIALIZATION_PHASES = {"materialize", "building", "awaiting-build", "awaiting-materialization", "materialization-pending"}


def dashboard_attention_kind(row: dict) -> str | None:
    """Return the operator queue represented by a run, if any."""

    phase = str(row.get("current_phase") or row.get("phase") or "").strip().lower().replace("_", "-")
    if phase in APPROVAL_PHASES:
        return "approval"
    if phase in MATERIALIZATION_PHASES:
        return "materialization"
    if str(row.get("status") or "").upper() == "SUCCEEDED" and not bool(row.get("approved")):
        return "approval"
    if bool(row.get("approved")) and bool(row.get("awaiting_materialization")):
        return "materialization"
    return None
