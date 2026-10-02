from __future__ import annotations

import json


def qa_demo_launch_context(payload: dict[str, object]) -> dict[str, str]:
    release_service_urls = payload.get("release_service_urls")
    serialized_service_urls = (
        json.dumps(release_service_urls, sort_keys=True, separators=(",", ":"))
        if isinstance(release_service_urls, list)
        else ""
    )
    api_recording_url = str(payload.get("release_api_recording_url") or "").strip()
    browser_recording_url = str(
        payload.get("release_browser_recording_url") or ""
    ).strip()
    api_base_url = str(payload.get("release_api_base_url") or "").strip()
    browser_url = str(payload.get("release_browser_url") or "").strip()
    candidates = {
        "MB_QA_DEMO_RELEASE_COMMIT_SHA": payload.get("release_commit_sha"),
        "MB_QA_DEMO_RELEASE_SERVICE_URLS_JSON": serialized_service_urls,
        "MB_QA_DEMO_RELEASE_API_BASE_URL": api_base_url,
        "MB_QA_DEMO_RELEASE_BROWSER_URL": browser_url,
        "MB_QA_DEMO_RELEASE_API_RECORDING_URL": api_recording_url,
        "MB_QA_DEMO_RELEASE_BROWSER_RECORDING_URL": browser_recording_url,
        "QA_DEMO_API_BASE_URL": api_recording_url or api_base_url,
        "QA_DEMO_BROWSER_URL": browser_recording_url or browser_url,
    }
    return {
        key: str(value).strip()
        for key, value in candidates.items()
        if str(value or "").strip()
    }
