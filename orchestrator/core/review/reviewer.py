from __future__ import annotations

from collections.abc import Callable
from collections import Counter
from dataclasses import dataclass
import logging
import re
from urllib.parse import urlparse

from orchestrator.core.policy_pack import find_banned_pattern_violations, select_policy_pack_for_files
from orchestrator.core.review.pr_ready import PrReadinessResult, evaluate_pr_readiness
from orchestrator.core.signal_templates import format_discord_pr_ready_message
from orchestrator.tools.github_app import GitHubAppClient

logger = logging.getLogger(__name__)
_DEMO_EVIDENCE_SECTION_PATTERN = re.compile(r"^## Demo Evidence\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_DEMO_EVIDENCE_MARKER = "<!-- master-builder:qa-demo-evidence v1 -->"
_DEMO_EVIDENCE_REQUIRED_TARGETS_PATTERN = re.compile(
    r"<!--\s*master-builder:qa-demo-required-targets\s+([^>]*)-->",
    re.MULTILINE,
)
_DEMO_EVIDENCE_REQUIRED_COUNTS_PATTERN = re.compile(
    r"<!--\s*master-builder:qa-demo-required-counts\s+([^>]*)-->",
    re.MULTILINE,
)
_STRUCTURED_DEMO_EVIDENCE_LINE_PATTERN = re.compile(
    r"^- (?P<name>.+?) \[target=(?P<target>browser|ios|android|desktop); "
    r"reference=(?P<reference>[^;\]]+); "
    r"object_key=(?P<object_key>[^;\]]+); sha256=(?P<sha256>[0-9a-f]{64}); "
    r"release_commit_sha=(?P<release_commit_sha>[0-9a-f]{7,64}); "
    r"release_context_sha256=(?P<release_context_sha256>[0-9a-f]{64})\]: "
    r"(?P<artifact_url>[^ \t\r\n]+)\s*$",
    re.MULTILINE,
)
_SUPPORTED_DEMO_CAPTURE_TARGETS = frozenset({"browser", "ios", "android", "desktop"})


@dataclass(frozen=True)
class ReviewerSignal:
    ready: bool
    state: str
    message: str
    readiness: PrReadinessResult
    must_fix_findings: tuple[str, ...] = ()
    policy_pack: str | None = None


class ReviewAgentGate:
    def __init__(
        self,
        github_client: GitHubAppClient,
        *,
        required_workflows: tuple[str, ...] = ("CI", "Security"),
        require_demo_evidence: bool = False,
        required_demo_capture_targets: tuple[str, ...] | list[str] | None = None,
        tenant_id: str | None = None,
        project_id: str | None = None,
        demo_artifact_public_base_url: str | None = None,
        demo_evidence_run_id_resolver: Callable[[str], str | None] | None = None,
        demo_evidence_recordings_resolver: Callable[[str], tuple[dict[str, str], ...] | None] | None = None,
        demo_proof_status_resolver: Callable[[str], dict[str, object] | None] | None = None,
    ):
        self._github_client = github_client
        self._required_workflows = required_workflows
        self._require_demo_evidence = require_demo_evidence
        self._required_demo_capture_targets = _normalize_required_demo_targets(required_demo_capture_targets)
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._demo_artifact_public_base_url = str(demo_artifact_public_base_url or "").strip().rstrip("/")
        self._demo_evidence_run_id_resolver = demo_evidence_run_id_resolver
        self._demo_evidence_recordings_resolver = demo_evidence_recordings_resolver
        self._demo_proof_status_resolver = demo_proof_status_resolver

    def evaluate_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> ReviewerSignal:
        pr = self._github_client.get_pull_request_details(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        checks = self._github_client.list_check_suites(
            repo_full_name=repo_full_name,
            ref=pr.head_sha,
        )
        changed_files = self._github_client.list_pull_request_files(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        selected_policy_pack = select_policy_pack_for_files(files=changed_files)
        must_fix_findings: list[str] = []
        selected_policy_pack_key: str | None = None
        if selected_policy_pack is not None:
            selected_policy_pack_key = selected_policy_pack.language_key
            must_fix_findings = find_banned_pattern_violations(
                policy_pack=selected_policy_pack,
                files=changed_files,
            )
        logger.info(
            "reviewer_policy_pack_selected repo=%s pr=%s policy_pack=%s changed_files=%s findings=%s",
            repo_full_name,
            pr_number,
            selected_policy_pack_key,
            len(changed_files),
            len(must_fix_findings),
        )
        readiness = evaluate_pr_readiness(
            review_summary_markdown=pr.body,
            required_workflows=self._required_workflows,
            workflow_checks=checks,
            tenant_id=self._tenant_id,
            project_id=self._project_id,
        )

        if must_fix_findings:
            summary = "; ".join(must_fix_findings[:2])
            return ReviewerSignal(
                ready=False,
                state="policy_violations",
                message=f"PR blocked by policy violations: {summary}",
                readiness=readiness,
                must_fix_findings=tuple(must_fix_findings),
                policy_pack=selected_policy_pack_key,
            )

        mixed_housekeeping = _mixed_housekeeping_and_source_changes(changed_files)
        if mixed_housekeeping:
            mixed_summary = ", ".join(mixed_housekeeping)
            return ReviewerSignal(
                ready=False,
                state="out_of_scope_changes",
                message=f"PR blocked: housekeeping file changes mixed into product diff: {mixed_summary}",
                readiness=readiness,
                must_fix_findings=(
                    "Remove repo housekeeping/self-improvement files from the feature PR before review.",
                ),
                policy_pack=selected_policy_pack_key,
            )

        if _source_changes_present(changed_files) and not _test_changes_present(changed_files):
            return ReviewerSignal(
                ready=False,
                state="missing_test_coverage",
                message="PR blocked: source changes detected without test file updates",
                readiness=readiness,
                must_fix_findings=("Add or update tests that cover the changed behavior.",),
                policy_pack=selected_policy_pack_key,
            )

        if self._require_demo_evidence:
            pr_url = str(pr.html_url or "").strip()
            if self._demo_proof_status_resolver is not None:
                demo_proof_status = self._demo_proof_status_resolver(pr_url)
                if not _demo_proof_status_ready(demo_proof_status):
                    return ReviewerSignal(
                        ready=False,
                        state="missing_demo_evidence",
                        message="PR blocked: completed QA demo proof workflow is required before ready-for-review signaling",
                        readiness=readiness,
                        must_fix_findings=("Complete the QA demo proof workflow and attach checked evidence links.",),
                        policy_pack=selected_policy_pack_key,
                    )
            expected_run_id = (
                self._demo_evidence_run_id_resolver(pr_url)
                if self._demo_evidence_run_id_resolver is not None
                else None
            )
            expected_recordings = (
                self._demo_evidence_recordings_resolver(expected_run_id) or ()
                if self._demo_evidence_recordings_resolver is not None and expected_run_id is not None
                else ()
            )
            if not _demo_evidence_present(
                pr.body,
                required_demo_capture_targets=self._required_demo_capture_targets,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                run_id=expected_run_id,
                artifact_public_base_url=self._demo_artifact_public_base_url,
                expected_recordings=expected_recordings,
            ):
                return ReviewerSignal(
                    ready=False,
                    state="missing_demo_evidence",
                    message="PR blocked: QA demo evidence is required before ready-for-review signaling",
                    readiness=readiness,
                    must_fix_findings=("Attach QA demo evidence links in the PR body.",),
                    policy_pack=selected_policy_pack_key,
            )

        if readiness.ready:
            ready_signal = format_discord_pr_ready_message(
                pr_url=pr.html_url,
                jira_url=None,
                run_id=None,
                what_changed=(f"Required checks passed: {', '.join(self._required_workflows)}",),
                risk_impact=("No failing required checks detected.",),
                how_to_test=("Open the PR checks tab and verify CI/Security are green.",),
                questions=(),
                next_action="Please review + merge",
            )
            return ReviewerSignal(
                ready=True,
                state="ready",
                message=ready_signal,
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "missing_review_sections":
            missing_sections = ", ".join(readiness.missing_review_sections)
            return ReviewerSignal(
                ready=False,
                state="missing_review_sections",
                message=f"PR review summary missing required sections: {missing_sections}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "pending_checks":
            details = ", ".join(readiness.pending_workflows)
            return ReviewerSignal(
                ready=False,
                state="pending_checks",
                message=f"PR opened, checks running: {details}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "failing_checks":
            failures = ", ".join(readiness.failing_workflows[:2])
            return ReviewerSignal(
                ready=False,
                state="failing_checks",
                message=f"PR checks failing: {failures}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "missing_checks":
            missing = ", ".join(readiness.missing_workflows)
            return ReviewerSignal(
                ready=False,
                state="missing_checks",
                message=f"PR checks missing: {missing}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        return ReviewerSignal(
            ready=False,
            state=readiness.state,
            message="PR review summary is required before PR Ready signal",
            readiness=readiness,
            policy_pack=selected_policy_pack_key,
        )


def _source_changes_present(changed_files) -> bool:  # noqa: ANN001
    source_suffixes = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".kt",
        ".swift",
        ".go",
        ".rb",
        ".rs",
        ".cs",
    }
    for change in changed_files:
        filename = str(getattr(change, "filename", "")).strip().lower()
        if not filename:
            continue
        if _is_test_path(filename):
            continue
        if filename.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
            continue
        if any(filename.endswith(ext) for ext in source_suffixes):
            return True
    return False


def _mixed_housekeeping_and_source_changes(changed_files) -> tuple[str, ...]:  # noqa: ANN001
    housekeeping_paths: list[str] = []
    source_present = False
    for change in changed_files:
        filename = str(getattr(change, "filename", "")).strip()
        normalized = filename.lower()
        if not normalized:
            continue
        if normalized == "tasks/lessons.md":
            housekeeping_paths.append(filename)
        if _is_source_path(normalized):
            source_present = True
    if not source_present or not housekeeping_paths:
        return ()
    return tuple(sorted(dict.fromkeys(housekeeping_paths)))


def _is_source_path(filename: str) -> bool:
    source_suffixes = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".kt",
        ".swift",
        ".go",
        ".rb",
        ".rs",
        ".cs",
    }
    if not filename or _is_test_path(filename):
        return False
    if filename.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
        return False
    return any(filename.endswith(ext) for ext in source_suffixes)


def _test_changes_present(changed_files) -> bool:  # noqa: ANN001
    for change in changed_files:
        filename = str(getattr(change, "filename", "")).strip().lower()
        if not filename:
            continue
        if _is_test_path(filename):
            return True
    return False


def _is_test_path(filename: str) -> bool:
    if (
        filename.startswith("tests/")
        or "/tests/" in filename
        or "__tests__/" in filename
        or filename.startswith("src/test/")
        or "/src/test/" in filename
        or filename.startswith("src/androidtest/")
        or "/src/androidtest/" in filename
        or filename.startswith("src/integrationtest/")
        or "/src/integrationtest/" in filename
    ):
        return True
    if filename.startswith("test_") or "/test_" in filename:
        return True
    if filename.endswith(
        (
            "_test.py",
            ".test.js",
            ".test.ts",
            ".test.tsx",
            ".spec.ts",
            ".spec.tsx",
            ".spec.js",
            "test.java",
            "tests.java",
            "spec.java",
            "test.kt",
            "tests.kt",
            "spec.kt",
        )
    ):
        return True
    return False


def _normalize_required_demo_targets(required_demo_capture_targets: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    ordered: list[str] = []
    for target in required_demo_capture_targets or ():
        normalized = str(target or "").strip()
        if not normalized:
            continue
        if normalized not in _SUPPORTED_DEMO_CAPTURE_TARGETS:
            raise ValueError(f"Unsupported QA demo capture target requirement: {normalized}")
        if normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def _required_demo_targets_from_section(section: str) -> tuple[str, ...] | None:
    match = _DEMO_EVIDENCE_REQUIRED_TARGETS_PATTERN.search(section)
    if match is None:
        return None
    try:
        return _normalize_required_demo_targets(re.split(r"[,\s]+", match.group(1).strip()))
    except ValueError:
        return ()


def _normalize_required_demo_counts(required_demo_capture_counts: dict[str, int]) -> dict[str, int]:
    normalized_counts: dict[str, int] = {}
    for target, count in required_demo_capture_counts.items():
        normalized_target = str(target or "").strip()
        if normalized_target not in _SUPPORTED_DEMO_CAPTURE_TARGETS:
            raise ValueError(f"Unsupported QA demo capture target count requirement: {normalized_target}")
        normalized_count = int(count)
        if normalized_count <= 0:
            raise ValueError(f"QA demo capture target count must be positive: {normalized_target}")
        normalized_counts[normalized_target] = normalized_count
    return normalized_counts


def _required_demo_counts_from_section(section: str) -> dict[str, int] | None:
    match = _DEMO_EVIDENCE_REQUIRED_COUNTS_PATTERN.search(section)
    if match is None:
        return None
    raw_entries = [entry.strip() for entry in re.split(r"[,\s]+", match.group(1).strip()) if entry.strip()]
    parsed_counts: dict[str, int] = {}
    try:
        for entry in raw_entries:
            target, raw_count = entry.split("=", 1)
            parsed_counts[target] = int(raw_count)
        return _normalize_required_demo_counts(parsed_counts)
    except (TypeError, ValueError):
        return {}


def _demo_evidence_present(
    body: str | None,
    *,
    required_demo_capture_targets: tuple[str, ...] | list[str] | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    artifact_public_base_url: str | None = None,
    expected_recordings: tuple[dict[str, str], ...] | None = None,
) -> bool:
    normalized_body = str(body or "").strip()
    if not normalized_body:
        return False
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_artifact_public_base_url = str(artifact_public_base_url or "").strip().rstrip("/")
    if (
        not normalized_tenant_id
        or not normalized_project_id
        or not normalized_run_id
        or not normalized_artifact_public_base_url
    ):
        return False
    match = _DEMO_EVIDENCE_SECTION_PATTERN.search(normalized_body)
    if match is None:
        return False
    section = match.group(0)
    if _DEMO_EVIDENCE_MARKER not in section:
        return False
    structured_matches = list(_STRUCTURED_DEMO_EVIDENCE_LINE_PATTERN.finditer(section))
    if not structured_matches:
        return False
    if not all(
        _structured_demo_evidence_line_is_run_artifact(
            match,
            tenant_id=normalized_tenant_id,
            project_id=normalized_project_id,
            run_id=normalized_run_id,
            artifact_public_base_url=normalized_artifact_public_base_url,
        )
        for match in structured_matches
    ):
        return False
    if expected_recordings is not None and not _structured_demo_evidence_matches_expected_recordings(
        structured_matches=structured_matches,
        expected_recordings=expected_recordings,
    ):
        return False
    content_sha256_counts = Counter(match.group("sha256") for match in structured_matches)
    if any(count > 1 for count in content_sha256_counts.values()):
        return False
    release_context_sha256_counts = Counter(match.group("release_context_sha256") for match in structured_matches)
    if len(release_context_sha256_counts) != 1:
        return False
    release_commit_sha_counts = Counter(match.group("release_commit_sha") for match in structured_matches)
    if len(release_commit_sha_counts) != 1:
        return False
    required_targets = _normalize_required_demo_targets(required_demo_capture_targets)
    embedded_required_targets = _required_demo_targets_from_section(section)
    if not embedded_required_targets:
        return False
    required_targets = tuple(dict.fromkeys((*required_targets, *embedded_required_targets)))
    recorded_targets = {structured_match.group("target") for structured_match in structured_matches}
    if not all(target in recorded_targets for target in required_targets):
        return False
    embedded_required_counts = _required_demo_counts_from_section(section)
    if not embedded_required_counts:
        return False
    recorded_counts = Counter(structured_match.group("target") for structured_match in structured_matches)
    return all(recorded_counts.get(target, 0) >= count for target, count in embedded_required_counts.items())


def _demo_proof_status_ready(status: dict[str, object] | None) -> bool:
    if not isinstance(status, dict):
        return False
    if str(status.get("status") or "").strip() != "completed":
        return False
    if str(status.get("demo_proof_state") or "").strip() != "complete":
        return False
    if str(status.get("terminal_event") or "").strip() != "PreviewCleanupCompleted":
        return False
    if str(status.get("artifact_url_check_status") or "").strip() != "passed":
        return False
    checked_urls = status.get("checked_artifact_urls")
    return isinstance(checked_urls, tuple | list) and bool(checked_urls)


def _structured_demo_evidence_matches_expected_recordings(
    *,
    structured_matches: list[re.Match[str]],
    expected_recordings: tuple[dict[str, str], ...],
) -> bool:
    if not expected_recordings:
        return False
    actual = {
        (
            str(match.group("name") or "").strip(),
            match.group("target"),
            str(match.group("reference") or "").strip(),
            match.group("object_key"),
            match.group("sha256"),
            match.group("release_commit_sha"),
            match.group("release_context_sha256"),
            str(match.group("artifact_url") or "").strip(),
        )
        for match in structured_matches
    }
    expected: set[tuple[str, str, str, str, str, str, str, str]] = set()
    for recording in expected_recordings:
        fingerprint = (
            str(recording.get("name") or "").strip(),
            str(recording.get("capture_target") or "").strip(),
            str(recording.get("capture_reference") or "").strip(),
            str(recording.get("object_key") or "").strip(),
            str(recording.get("content_sha256") or "").strip().lower(),
            str(recording.get("release_commit_sha") or "").strip().lower(),
            str(recording.get("release_context_sha256") or "").strip().lower(),
            str(recording.get("artifact_url") or "").strip(),
        )
        if not all(fingerprint):
            return False
        expected.add(fingerprint)
    return actual == expected


def _structured_demo_evidence_line_is_run_artifact(
    match: re.Match[str],
    *,
    tenant_id: str,
    project_id: str,
    run_id: str,
    artifact_public_base_url: str,
) -> bool:
    artifact_url = str(match.group("artifact_url") or "").strip()
    expected_base = artifact_public_base_url.rstrip("/")
    if not artifact_url.startswith(f"{expected_base}/"):
        return False
    parsed_url = urlparse(artifact_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return False
    object_key = str(match.group("object_key") or "").strip()
    if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
        return False
    key_parts = object_key.split("/")
    if len(key_parts) < 4 or any(not part for part in key_parts):
        return False
    if key_parts[0] != tenant_id or key_parts[1] != project_id or key_parts[2] != run_id:
        return False
    return parsed_url.path.endswith(f"/{object_key}")
