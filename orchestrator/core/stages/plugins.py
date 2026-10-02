from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any, Callable, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

STAGE_STATUS_PLANNING = "stage_planning"
STAGE_STATUS_REVIEW_PENDING = "stage_review_pending"
STAGE_STATUS_FEEDBACK_PENDING = "stage_feedback_pending"
STAGE_STATUS_REVISING = "stage_revising"
STAGE_STATUS_READY = "stage_ready_for_implementation"
STAGE_STATUS_BLOCKED = "stage_blocked"

STAGE_EVENT_REQUESTED = "stage_requested"
STAGE_EVENT_PLANNED = "stage_planned"
STAGE_EVENT_REVIEW_STARTED = "stage_review_started"
STAGE_EVENT_FEEDBACK_RECEIVED = "stage_feedback_received"
STAGE_EVENT_REVISION_REQUESTED = "stage_revision_requested"
STAGE_EVENT_BLOCKED = "stage_blocked"
STAGE_EVENT_APPROVED = "stage_approved"
STAGE_EVENT_READY = "stage_ready_for_implementation"
STAGE_EVENT_REJECTED = "stage_rejected"

_STITCH_URL_RE = re.compile(r"https://stitch\.withgoogle\.com/[^\s)]+", re.IGNORECASE)
_APPROVAL_MARKERS = ("approve", "approved", "looks good", "ship it", "good to go")
_REJECTION_MARKERS = (
    "reject",
    "rejected",
    "no go",
    "not approved",
    "do not approve",
    "start over",
    "revise completely",
)

_STAGE_PLUGIN_FACTORIES: dict[str, Callable[[], "StagePlugin"]] = {}


def register_stage_plugin(plugin_id: str, factory: Callable[[], "StagePlugin"]) -> None:
    normalized = str(plugin_id or "").strip().lower()
    if not normalized:
        raise ValueError("plugin_id is required")
    _STAGE_PLUGIN_FACTORIES[normalized] = factory


def resolve_stage_plugin_factory(plugin_id: str) -> Callable[[], "StagePlugin"] | None:
    normalized = str(plugin_id or "").strip().lower()
    return _STAGE_PLUGIN_FACTORIES.get(normalized)


class StagePlugin(Protocol):
    def plugin_id(self) -> str: ...
    def supported_states(self) -> tuple[str, ...]: ...
    def plan(
        self, *, state: dict[str, Any], stakeholder_text: str, assistant_summary: str
    ) -> dict[str, Any]: ...
    def apply_feedback(
        self,
        *,
        state: dict[str, Any],
        stakeholder_text: str,
        decision_state: str | None = None,
    ) -> dict[str, Any]: ...
    def is_ready_for_implementation(self, *, state: dict[str, Any]) -> bool: ...
    def render_stakeholder_message(self, *, state: dict[str, Any]) -> str: ...
    def serialize_outputs(self, *, state: dict[str, Any]) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class StagePluginResult:
    stage_plugin: str
    stage_status: str
    stage_artifacts: dict[str, Any]
    stage_open_questions: tuple[str, ...]
    stage_feedback_log: tuple[dict[str, Any], ...]
    stage_tool_outputs: tuple[dict[str, Any], ...]
    stage_ready_for_implementation: bool
    message: str
    block_reason: str | None = None


def log_stage_spi_event(
    *,
    spi_event_type: str,
    stage_plugin: str,
    from_state: str | None,
    to_state: str | None,
    tenant_id: str,
    project_id: str | None,
    request_id: str | None,
    block_reason: str | None = None,
) -> None:
    logger.info(
        "stage_spi_event",
        extra={
            "event_type": "orchestrator.core.stage_spi",
            "tenant_id": str(tenant_id or "").strip() or "",
            "project_id": str(project_id or "").strip() or "",
            "metadata": {
                "spi_event_type": str(spi_event_type or "").strip(),
                "stage_plugin": str(stage_plugin or "").strip(),
                "from_state": str(from_state or "").strip(),
                "to_state": str(to_state or "").strip(),
                "request_id": str(request_id or "").strip(),
                "block_reason": str(block_reason or "").strip(),
            },
        },
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_state(state: dict[str, Any] | None) -> dict[str, Any]:
    source = state if isinstance(state, dict) else {}
    return {
        "stage_plugin": str(source.get("stage_plugin") or "design").strip().lower()
        or "design",
        "stage_status": str(source.get("stage_status") or STAGE_STATUS_PLANNING)
        .strip()
        .lower()
        or STAGE_STATUS_PLANNING,
        "stage_artifacts": dict(source.get("stage_artifacts") or {}),
        "stage_open_questions": [
            str(item).strip()
            for item in (source.get("stage_open_questions") or [])
            if str(item).strip()
        ],
        "stage_feedback_log": [
            dict(item)
            for item in (source.get("stage_feedback_log") or [])
            if isinstance(item, dict)
        ],
        "stage_tool_outputs": [
            dict(item)
            for item in (source.get("stage_tool_outputs") or [])
            if isinstance(item, dict)
        ],
        "stage_ready_for_implementation": bool(
            source.get("stage_ready_for_implementation")
        ),
    }


def _merge_llm_plan_payload(
    normalized: dict[str, Any], llm: dict[str, Any] | None
) -> None:
    if not isinstance(llm, dict):
        return
    arts = llm.get("stage_artifacts")
    if isinstance(arts, dict):
        normalized["stage_artifacts"].update(arts)
    loq = llm.get("stage_open_questions")
    if isinstance(loq, list):
        cleaned = [str(x).strip() for x in loq if str(x).strip()]
        if cleaned:
            normalized["stage_open_questions"] = cleaned


def _should_replan_stage(state: dict[str, Any]) -> bool:
    status = str(state.get("stage_status") or "").strip().lower()
    if status == STAGE_STATUS_REVISING:
        return True
    return not bool(state.get("stage_artifacts"))


def extract_stitch_tool_outputs(
    *, text: str, attachments: list[dict[str, str]] | None
) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    urls = set(_STITCH_URL_RE.findall(str(text or "")))
    for attachment in attachments or []:
        raw_url = str(attachment.get("url") or "").strip()
        if _STITCH_URL_RE.match(raw_url):
            urls.add(raw_url)
    for url in sorted(urls):
        outputs.append(
            {
                "provider": "stitch",
                "kind": "design_asset",
                "url": url,
                "captured_at": _now_iso(),
            }
        )
    return outputs


class DesignStagePlugin:
    def plugin_id(self) -> str:
        return "design"

    def supported_states(self) -> tuple[str, ...]:
        return (
            STAGE_STATUS_PLANNING,
            STAGE_STATUS_REVIEW_PENDING,
            STAGE_STATUS_FEEDBACK_PENDING,
            STAGE_STATUS_REVISING,
            STAGE_STATUS_READY,
            STAGE_STATUS_BLOCKED,
        )

    def plan(
        self, *, state: dict[str, Any], stakeholder_text: str, assistant_summary: str
    ) -> dict[str, Any]:
        normalized = _normalize_state(state)
        artifacts = dict(normalized["stage_artifacts"])
        artifacts.setdefault("design_brief", assistant_summary)
        artifacts.setdefault("design_direction", stakeholder_text)
        artifacts.setdefault("artifact_version", uuid4().hex)
        normalized["stage_artifacts"] = artifacts
        normalized["stage_status"] = STAGE_STATUS_REVIEW_PENDING
        normalized["stage_open_questions"] = [
            "Please confirm the design direction or request revisions before implementation seeding."
        ]
        normalized["stage_ready_for_implementation"] = False
        return normalized

    def apply_feedback(
        self,
        *,
        state: dict[str, Any],
        stakeholder_text: str,
        decision_state: str | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_state(state)
        decision = str(decision_state or "").strip().lower()
        if decision == "revisions_required":
            normalized["stage_status"] = STAGE_STATUS_REVISING
            normalized["stage_ready_for_implementation"] = False
            normalized["stage_open_questions"] = [
                "Design direction needs revisions. Please describe the changes you want."
            ]
            return normalized
        if decision == "approved":
            normalized["stage_status"] = STAGE_STATUS_READY
            normalized["stage_open_questions"] = []
            normalized["stage_ready_for_implementation"] = True
            return normalized
        if decision == "pending":
            normalized["stage_status"] = STAGE_STATUS_FEEDBACK_PENDING
            normalized["stage_ready_for_implementation"] = False
            normalized["stage_open_questions"] = [
                "Please share design revisions or explicitly approve to continue to implementation planning."
            ]
            return normalized

        lowered = str(stakeholder_text or "").strip().lower()
        if any(marker in lowered for marker in _REJECTION_MARKERS):
            normalized["stage_status"] = STAGE_STATUS_REVISING
            normalized["stage_ready_for_implementation"] = False
            normalized["stage_open_questions"] = [
                "Design direction was rejected or needs a reset. Please describe the changes you want."
            ]
            return normalized
        negated = (
            "not approved" in lowered
            or "do not approve" in lowered
            or "don't approve" in lowered
        )
        approved = not negated and any(
            marker in lowered for marker in _APPROVAL_MARKERS
        )
        if approved:
            normalized["stage_status"] = STAGE_STATUS_READY
            normalized["stage_open_questions"] = []
            normalized["stage_ready_for_implementation"] = True
            return normalized
        normalized["stage_status"] = STAGE_STATUS_FEEDBACK_PENDING
        normalized["stage_ready_for_implementation"] = False
        normalized["stage_open_questions"] = [
            "Please share design revisions or explicitly approve to continue to implementation planning."
        ]
        return normalized

    def is_ready_for_implementation(self, *, state: dict[str, Any]) -> bool:
        normalized = _normalize_state(state)
        if not normalized["stage_artifacts"].get("artifact_version"):
            return False
        return (
            bool(normalized["stage_ready_for_implementation"])
            and not normalized["stage_open_questions"]
        )

    def render_stakeholder_message(self, *, state: dict[str, Any]) -> str:
        normalized = _normalize_state(state)
        if self.is_ready_for_implementation(state=normalized):
            return "Design stage approved. Proceeding to implementation planning."
        questions = normalized["stage_open_questions"]
        if questions:
            return questions[0]
        return "Design stage is pending review."

    def serialize_outputs(self, *, state: dict[str, Any]) -> list[dict[str, Any]]:
        normalized = _normalize_state(state)
        return list(normalized["stage_tool_outputs"])


def _default_design_factory() -> StagePlugin:
    return DesignStagePlugin()


register_stage_plugin("design", _default_design_factory)


def _blocked_plugin_result(*, plugin_id: str, reason: str) -> StagePluginResult:
    return StagePluginResult(
        stage_plugin=plugin_id,
        stage_status=STAGE_STATUS_BLOCKED,
        stage_artifacts={},
        stage_open_questions=(reason,),
        stage_feedback_log=(),
        stage_tool_outputs=(),
        stage_ready_for_implementation=False,
        message=reason,
        block_reason=reason,
    )


def evaluate_stage_plugin(
    *,
    state: dict[str, Any] | None,
    stakeholder_text: str,
    assistant_summary: str,
    attachments: list[dict[str, str]] | None,
    plugin_id: str | None = None,
    default_plugin_id: str = "design",
    tenant_id: str = "",
    project_id: str | None = None,
    request_id: str | None = None,
    llm_plan_payload: dict[str, Any] | None = None,
    explicit_tool_outputs: list[dict[str, Any]] | None = None,
    decision_state: str | None = None,
) -> StagePluginResult:
    from orchestrator.core.config import get_settings

    settings = get_settings()
    resolved_plugin_id = (
        str(
            plugin_id
            or getattr(settings, "stage_spi_default_plugin", None)
            or default_plugin_id
        )
        .strip()
        .lower()
    )
    factory = resolve_stage_plugin_factory(resolved_plugin_id)
    prior = _normalize_state(state)
    from_status = str(prior.get("stage_status") or "")

    if factory is None:
        reason = f"Unknown stage plugin '{resolved_plugin_id}'"
        log_stage_spi_event(
            spi_event_type=STAGE_EVENT_BLOCKED,
            stage_plugin=resolved_plugin_id,
            from_state=from_status,
            to_state=STAGE_STATUS_BLOCKED,
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
            block_reason=reason,
        )
        return _blocked_plugin_result(plugin_id=resolved_plugin_id, reason=reason)

    plugin = factory()
    normalized = _normalize_state(state)
    if str(normalized.get("stage_plugin") or "").strip().lower() != resolved_plugin_id:
        normalized["stage_plugin"] = resolved_plugin_id

    feedback_log = list(normalized["stage_feedback_log"])
    feedback_log.append(
        {
            "event_type": STAGE_EVENT_FEEDBACK_RECEIVED,
            "recorded_at": _now_iso(),
            "stakeholder_text": str(stakeholder_text or "").strip(),
            "assistant_summary": str(assistant_summary or "").strip(),
        }
    )
    normalized["stage_feedback_log"] = feedback_log
    normalized["stage_tool_outputs"] = list(
        normalized["stage_tool_outputs"]
    ) + extract_stitch_tool_outputs(
        text=stakeholder_text,
        attachments=attachments,
    )
    if isinstance(explicit_tool_outputs, list) and explicit_tool_outputs:
        normalized["stage_tool_outputs"] = list(normalized["stage_tool_outputs"]) + [
            dict(item) for item in explicit_tool_outputs if isinstance(item, dict)
        ]

    planned_transition = _should_replan_stage(normalized)
    planned_llm_payload = llm_plan_payload if planned_transition else None
    if planned_transition:
        if from_status == STAGE_STATUS_REVISING:
            normalized["stage_artifacts"] = {}
        normalized = plugin.plan(
            state=normalized,
            stakeholder_text=stakeholder_text,
            assistant_summary=assistant_summary,
        )
        _merge_llm_plan_payload(normalized, planned_llm_payload)
        if not normalized["stage_artifacts"].get("artifact_version"):
            normalized["stage_artifacts"]["artifact_version"] = uuid4().hex
        log_stage_spi_event(
            spi_event_type=STAGE_EVENT_PLANNED,
            stage_plugin=resolved_plugin_id,
            from_state=from_status,
            to_state=str(normalized.get("stage_status") or ""),
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
            block_reason=None,
        )
    else:
        normalized = plugin.apply_feedback(
            state=normalized,
            stakeholder_text=stakeholder_text,
            decision_state=decision_state,
        )
        to_state = str(normalized.get("stage_status") or "")
        if to_state == STAGE_STATUS_READY:
            log_stage_spi_event(
                spi_event_type=STAGE_EVENT_READY,
                stage_plugin=resolved_plugin_id,
                from_state=from_status,
                to_state=to_state,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                block_reason=None,
            )
        elif to_state == STAGE_STATUS_REVISING:
            log_stage_spi_event(
                spi_event_type=STAGE_EVENT_REJECTED,
                stage_plugin=resolved_plugin_id,
                from_state=from_status,
                to_state=to_state,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                block_reason=None,
            )
        else:
            log_stage_spi_event(
                spi_event_type=STAGE_EVENT_REVISION_REQUESTED,
                stage_plugin=resolved_plugin_id,
                from_state=from_status,
                to_state=to_state,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                block_reason=None,
            )

    message = plugin.render_stakeholder_message(state=normalized)
    if planned_transition and isinstance(planned_llm_payload, dict):
        lm = planned_llm_payload.get("message")
        if isinstance(lm, str) and lm.strip():
            message = lm.strip()
    ready = plugin.is_ready_for_implementation(state=normalized)
    return StagePluginResult(
        stage_plugin=resolved_plugin_id,
        stage_status=str(normalized["stage_status"]),
        stage_artifacts=dict(normalized["stage_artifacts"]),
        stage_open_questions=tuple(
            str(item) for item in normalized["stage_open_questions"]
        ),
        stage_feedback_log=tuple(
            dict(item) for item in normalized["stage_feedback_log"]
        ),
        stage_tool_outputs=tuple(
            dict(item) for item in normalized["stage_tool_outputs"]
        ),
        stage_ready_for_implementation=ready,
        message=message,
        block_reason=None,
    )
