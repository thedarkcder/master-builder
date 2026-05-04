#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from datetime import date
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from orchestrator.core.projects.release_train import (
    apply_release_label,
    next_release_version_from_tags,
    normalize_release_version,
    release_label_for_version,
)


def _json_dumps(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode("utf-8")


@dataclass(frozen=True)
class JiraIssue:
    key: str
    labels: list[str]
    status_name: str


class JiraClient:
    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        cloud_id: str = "",
    ) -> None:
        resolved_base_url = base_url.rstrip("/")
        if cloud_id.strip():
            resolved_base_url = f"https://api.atlassian.com/ex/jira/{cloud_id.strip()}"
        self._base_url = resolved_base_url
        auth_raw = f"{email}:{api_token}".encode("utf-8")
        self._auth_header = "Basic " + base64.b64encode(auth_raw).decode("ascii")

    @property
    def base_url(self) -> str:
        return self._base_url

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any] | list[Any]:
        query_string = ""
        if query:
            query_string = "?" + urllib.parse.urlencode(query)
        url = f"{self._base_url}{path}{query_string}"
        data = _json_dumps(payload) if payload is not None else None
        request = urllib.request.Request(url, method=method, data=data)
        request.add_header("Authorization", self._auth_header)
        request.add_header("Accept", "application/json")
        if payload is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Jira API request failed ({exc.code}) {path}: {body}") from exc

    def search_issues(self, *, jql: str) -> list[JiraIssue]:
        next_page_token = ""
        seen_page_tokens: set[str] = set()
        collected: list[JiraIssue] = []
        while True:
            payload = {
                "jql": jql,
                "maxResults": 100,
                "fields": ["labels", "status"],
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token

            # Jira Cloud removed /rest/api/3/search and requires /rest/api/3/search/jql.
            data = self._request_json(method="POST", path="/rest/api/3/search/jql", payload=payload)
            issues = data.get("issues", [])
            for issue in issues:
                fields = issue.get("fields", {})
                status = fields.get("status", {}) or {}
                collected.append(
                    JiraIssue(
                        key=str(issue.get("key", "")).strip(),
                        labels=[str(label).strip() for label in fields.get("labels", []) if str(label).strip()],
                        status_name=str(status.get("name", "")).strip(),
                    )
                )

            # New search API uses nextPageToken pagination.
            # Guard against repeated tokens to avoid infinite loops on API anomalies.
            raw_next_page_token = str(data.get("nextPageToken", "")).strip()
            if not issues or not raw_next_page_token:
                break
            if raw_next_page_token in seen_page_tokens:
                raise RuntimeError("Jira search pagination loop detected (repeated nextPageToken)")
            seen_page_tokens.add(raw_next_page_token)
            next_page_token = raw_next_page_token
        return collected

    def update_issue_labels(self, *, issue_key: str, labels: list[str]) -> None:
        payload = {"fields": {"labels": labels}}
        self._request_json(method="PUT", path=f"/rest/api/3/issue/{issue_key}", payload=payload)

    def add_comment(self, *, issue_key: str, comment: str) -> None:
        payload = {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}}
        self._request_json(method="POST", path=f"/rest/api/3/issue/{issue_key}/comment", payload=payload)

    def get_transitions(self, *, issue_key: str) -> list[dict[str, Any]]:
        data = self._request_json(method="GET", path=f"/rest/api/3/issue/{issue_key}/transitions")
        return [transition for transition in data.get("transitions", []) if isinstance(transition, dict)]

    def transition_issue(self, *, issue_key: str, transition_id: str) -> None:
        payload = {"transition": {"id": transition_id}}
        self._request_json(method="POST", path=f"/rest/api/3/issue/{issue_key}/transitions", payload=payload)

    def project_id(self, *, project_key: str) -> str:
        data = self._request_json(method="GET", path=f"/rest/api/3/project/{project_key}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Unexpected Jira project response for {project_key}")
        project_id = str(data.get("id", "")).strip()
        if not project_id:
            raise RuntimeError(f"Unable to resolve Jira project id for {project_key}")
        return project_id

    def find_version(self, *, project_id: str, version_name: str) -> dict[str, Any] | None:
        start_at = 0
        while True:
            data = self._request_json(
                method="GET",
                path=f"/rest/api/3/project/{project_id}/version",
                query={"startAt": str(start_at), "maxResults": "50"},
            )
            if isinstance(data, list):
                versions = [item for item in data if isinstance(item, dict)]
                for version in versions:
                    if str(version.get("name", "")).strip() == version_name:
                        return version
                return None

            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected Jira version response for project id {project_id}")

            versions = data.get("values", [])
            if not isinstance(versions, list):
                raise RuntimeError(f"Unexpected Jira version list payload for project id {project_id}")

            for raw_version in versions:
                if not isinstance(raw_version, dict):
                    continue
                if str(raw_version.get("name", "")).strip() == version_name:
                    return raw_version

            is_last = bool(data.get("isLast", True))
            if is_last:
                break
            page_size = int(data.get("maxResults", len(versions)) or len(versions) or 50)
            start_at += page_size
        return None

    def create_version(self, *, project_id: str, version_name: str, released: bool) -> None:
        payload = {
            "name": version_name,
            "projectId": int(project_id),
            "released": released,
        }
        if released:
            payload["releaseDate"] = date.today().isoformat()
        self._request_json(method="POST", path="/rest/api/3/version", payload=payload)

    def update_version_release(self, *, version_id: str, released: bool) -> None:
        payload = {"released": released}
        if released:
            payload["releaseDate"] = date.today().isoformat()
        self._request_json(method="PUT", path=f"/rest/api/3/version/{version_id}", payload=payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assign and close Jira release-train issues.")
    parser.add_argument(
        "mode",
        choices=["assign", "close", "keys", "release", "count-ready"],
        help="assign: label Ready to Release issues; close: transition release issues to Done; keys: print issue keys for release label; release: ensure Jira release version exists and is released; count-ready: print READY TO RELEASE issue count.",
    )
    parser.add_argument("--jira-base-url", required=True)
    parser.add_argument("--jira-cloud-id", default="")
    parser.add_argument("--jira-email", required=True)
    parser.add_argument("--jira-api-token", required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--ready-status", default="READY TO RELEASE")
    parser.add_argument("--done-status", default="Done")
    parser.add_argument("--release-version", default="")
    parser.add_argument("--comment", action="store_true", help="Add Jira comments when assigning/closing.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def git_tags() -> list[str]:
    result = subprocess.run(
        ["git", "tag", "--list", "v*.*.*", "--sort=-v:refname"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def resolve_release_version(explicit_version: str) -> str:
    if explicit_version.strip():
        return normalize_release_version(explicit_version)
    return next_release_version_from_tags(git_tags())


def assign_release_label(
    *,
    client: JiraClient,
    project_key: str,
    ready_status: str,
    release_version: str,
    dry_run: bool,
    comment: bool,
) -> int:
    release_label = release_label_for_version(release_version)
    jql = f'project = "{project_key}" AND status = "{ready_status}"'
    print(
        f"[assign] base_url={client.base_url} project={project_key} ready_status={ready_status} release={release_version}"
    )
    print(f"[assign] jql={jql}")
    issues = client.search_issues(jql=jql)
    updated_count = 0
    for issue in issues:
        if not issue.key:
            continue
        target_labels = apply_release_label(issue.labels, target_label=release_label)
        if target_labels == issue.labels:
            continue
        updated_count += 1
        print(f"[assign] {issue.key} -> {release_label}")
        if dry_run:
            continue
        client.update_issue_labels(issue_key=issue.key, labels=target_labels)
        if comment:
            client.add_comment(
                issue_key=issue.key,
                comment=f"Automatically assigned to release train {release_version}.",
            )
    print(f"assign summary: scanned={len(issues)} updated={updated_count} release={release_version}")
    return 0


def count_ready_issues(
    *,
    client: JiraClient,
    project_key: str,
    ready_status: str,
) -> int:
    jql = f'project = "{project_key}" AND status = "{ready_status}"'
    issues = client.search_issues(jql=jql)
    ready_count = sum(1 for issue in issues if issue.key)
    print(
        f"[count-ready] base_url={client.base_url} project={project_key} ready_status={ready_status} count={ready_count}",
        file=sys.stderr,
    )
    print(str(ready_count))
    return 0


def close_released_issues(
    *,
    client: JiraClient,
    project_key: str,
    done_status: str,
    release_version: str,
    dry_run: bool,
    comment: bool,
) -> int:
    release_label = release_label_for_version(release_version)
    jql = f'project = "{project_key}" AND labels = "{release_label}"'
    print(
        f"[close] base_url={client.base_url} project={project_key} done_status={done_status} release={release_version}"
    )
    print(f"[close] jql={jql}")
    issues = client.search_issues(jql=jql)
    transitioned = 0
    skipped = 0
    for issue in issues:
        if not issue.key:
            continue
        if issue.status_name == done_status:
            skipped += 1
            continue
        transitions = client.get_transitions(issue_key=issue.key)
        transition_id = ""
        for transition in transitions:
            to_status = transition.get("to", {}) or {}
            if str(to_status.get("name", "")).strip() == done_status:
                transition_id = str(transition.get("id", "")).strip()
                break

        if not transition_id:
            print(f"[close] {issue.key} missing transition to '{done_status}'")
            skipped += 1
            continue

        transitioned += 1
        print(f"[close] {issue.key} -> {done_status}")
        if dry_run:
            continue
        client.transition_issue(issue_key=issue.key, transition_id=transition_id)
        if comment:
            client.add_comment(
                issue_key=issue.key,
                comment=f"Release {release_version} published. Transitioned to {done_status}.",
            )
    print(
        "close summary: scanned={} transitioned={} skipped={} release={}".format(
            len(issues), transitioned, skipped, release_version
        )
    )
    return 0


def ensure_jira_release_version(
    *,
    client: JiraClient,
    project_key: str,
    release_version: str,
    dry_run: bool,
) -> int:
    project_id = client.project_id(project_key=project_key)
    existing = client.find_version(project_id=project_id, version_name=release_version)
    if existing is None:
        print(f"[release] create project={project_key} version={release_version} released=true")
        if not dry_run:
            client.create_version(project_id=project_id, version_name=release_version, released=True)
        return 0

    version_id = str(existing.get("id", "")).strip()
    is_released = bool(existing.get("released"))
    if is_released:
        print(f"[release] exists project={project_key} version={release_version} released=true")
        return 0

    if not version_id:
        raise RuntimeError(f"Jira version '{release_version}' exists but id is missing")

    print(f"[release] update project={project_key} version={release_version} set released=true")
    if not dry_run:
        client.update_version_release(version_id=version_id, released=True)
    return 0


def print_release_issue_keys(
    *,
    client: JiraClient,
    project_key: str,
    release_version: str,
) -> int:
    release_label = release_label_for_version(release_version)
    jql = f'project = "{project_key}" AND labels = "{release_label}"'
    issues = client.search_issues(jql=jql)
    print(
        f"[keys] base_url={client.base_url} project={project_key} release={release_version} scanned={len(issues)}",
        file=sys.stderr,
    )
    for issue in issues:
        if issue.key:
            print(issue.key)
    return 0


def main() -> int:
    args = parse_args()
    release_version = resolve_release_version(args.release_version)
    client = JiraClient(
        base_url=args.jira_base_url,
        cloud_id=args.jira_cloud_id,
        email=args.jira_email,
        api_token=args.jira_api_token,
    )
    if args.mode == "assign":
        return assign_release_label(
            client=client,
            project_key=args.project_key,
            ready_status=args.ready_status,
            release_version=release_version,
            dry_run=args.dry_run,
            comment=args.comment,
        )
    if args.mode == "count-ready":
        return count_ready_issues(
            client=client,
            project_key=args.project_key,
            ready_status=args.ready_status,
        )
    if args.mode == "keys":
        return print_release_issue_keys(
            client=client,
            project_key=args.project_key,
            release_version=release_version,
        )
    if args.mode == "release":
        return ensure_jira_release_version(
            client=client,
            project_key=args.project_key,
            release_version=release_version,
            dry_run=args.dry_run,
        )
    return close_released_issues(
        client=client,
        project_key=args.project_key,
        done_status=args.done_status,
        release_version=release_version,
        dry_run=args.dry_run,
        comment=args.comment,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
