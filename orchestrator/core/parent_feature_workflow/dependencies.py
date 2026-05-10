from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParentFeatureWorkflowHandlerDeps:
    integration_router: Any
    extract_changed_fields_fn: Any
    extract_status_transition_fn: Any
    build_runtime_for_selector_fn: Any
    seed_issues_with_runtime_fn: Any
    post_jira_comment_fn: Any
    create_jira_comment_fn: Any
