from __future__ import annotations


def admin_run_url(*, admin_ui_base_url: str | None, run_id: str) -> str | None:
    base = str(admin_ui_base_url or "").strip().rstrip("/")
    normalized_run_id = str(run_id or "").strip()
    if not base or not normalized_run_id:
        return None
    return f"{base}/runs/{normalized_run_id}"
