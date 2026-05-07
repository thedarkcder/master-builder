from __future__ import annotations

from dataclasses import dataclass

PM_PARENT_LABEL = "pm-parent"
ENGINEERING_CHILD_LABEL = "engineering-child"
PARENT_LABEL_PREFIX = "parent-"
ISSUE_CLASS_PARENT = "parent"
ISSUE_CLASS_ENGINEERING_CHILD = "engineering_child"
MB_WORK_STATE_PLANNING_CANDIDATE = "planning_candidate"
MB_WORK_STATE_NOT_PLANNING = "not_planning"


@dataclass(frozen=True)
class JiraReconciliationIssue:
    issue_id: str
    key: str
    summary: str
    description: str
    status: str
    mb_work_state: str
    issue_type: str | None
    labels: tuple[str, ...]
    parent_key: str | None
    parent_issue_id: str | None
    issue_type_hierarchy_level: int | None = None
    issue_type_is_subtask: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "key": self.key,
            "summary": self.summary,
            "description": self.description,
            "status": self.status,
            "mb_work_state": self.mb_work_state,
            "issue_type": self.issue_type,
            "labels": list(self.labels),
            "parent_key": self.parent_key,
            "parent_issue_id": self.parent_issue_id,
            "issue_type_hierarchy_level": self.issue_type_hierarchy_level,
            "issue_type_is_subtask": self.issue_type_is_subtask,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "JiraReconciliationIssue":
        return cls(
            issue_id=str(payload.get("issue_id") or "").strip(),
            key=str(payload.get("key") or "").strip().upper(),
            summary=str(payload.get("summary") or "").strip(),
            description=str(payload.get("description") or ""),
            status=str(payload.get("status") or "").strip(),
            mb_work_state=str(payload.get("mb_work_state") or "").strip(),
            issue_type=str(payload.get("issue_type") or "").strip() or None,
            labels=tuple(str(label).strip() for label in list(payload.get("labels") or []) if str(label).strip()),
            parent_key=str(payload.get("parent_key") or "").strip().upper() or None,
            parent_issue_id=str(payload.get("parent_issue_id") or "").strip() or None,
            issue_type_hierarchy_level=(
                int(payload.get("issue_type_hierarchy_level"))
                if payload.get("issue_type_hierarchy_level") is not None
                else None
            ),
            issue_type_is_subtask=(
                bool(payload.get("issue_type_is_subtask"))
                if payload.get("issue_type_is_subtask") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ClassifiedJiraIssue:
    issue: JiraReconciliationIssue
    classification: str
    desired_labels: tuple[str, ...]
    labels_changed: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "issue": self.issue.to_payload(),
            "classification": self.classification,
            "desired_labels": list(self.desired_labels),
            "labels_changed": self.labels_changed,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "ClassifiedJiraIssue":
        issue_payload = payload.get("issue")
        if not isinstance(issue_payload, dict):
            raise ValueError("Classified Jira issue payload is missing issue data")
        return cls(
            issue=JiraReconciliationIssue.from_payload(issue_payload),
            classification=str(payload.get("classification") or "").strip(),
            desired_labels=tuple(
                str(label).strip() for label in list(payload.get("desired_labels") or []) if str(label).strip()
            ),
            labels_changed=bool(payload.get("labels_changed")),
        )


@dataclass(frozen=True)
class ParentWorkflowReconciliationResult:
    issue_key: str
    workflow_id: str
    created: bool
    mb_work_state: str
    deactivated: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "issue_key": self.issue_key,
            "workflow_id": self.workflow_id,
            "created": self.created,
            "mb_work_state": self.mb_work_state,
            "deactivated": self.deactivated,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "ParentWorkflowReconciliationResult":
        return cls(
            issue_key=str(payload.get("issue_key") or "").strip().upper(),
            workflow_id=str(payload.get("workflow_id") or "").strip(),
            created=bool(payload.get("created")),
            mb_work_state=str(payload.get("mb_work_state") or "").strip(),
            deactivated=bool(payload.get("deactivated")),
        )


@dataclass(frozen=True)
class JiraProjectReconciliationSummary:
    scanned: int
    parent_workflows_created: int
    parent_workflows_existing: int
    labels_updated: int
    parent_issue_keys: tuple[str, ...]
    engineering_issue_keys: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "parent_workflows_created": self.parent_workflows_created,
            "parent_workflows_existing": self.parent_workflows_existing,
            "labels_updated": self.labels_updated,
            "parent_issue_keys": list(self.parent_issue_keys),
            "engineering_issue_keys": list(self.engineering_issue_keys),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "JiraProjectReconciliationSummary":
        return cls(
            scanned=int(payload.get("scanned") or 0),
            parent_workflows_created=int(payload.get("parent_workflows_created") or 0),
            parent_workflows_existing=int(payload.get("parent_workflows_existing") or 0),
            labels_updated=int(payload.get("labels_updated") or 0),
            parent_issue_keys=tuple(
                str(value).strip().upper() for value in list(payload.get("parent_issue_keys") or []) if str(value).strip()
            ),
            engineering_issue_keys=tuple(
                str(value).strip().upper()
                for value in list(payload.get("engineering_issue_keys") or [])
                if str(value).strip()
            ),
        )
