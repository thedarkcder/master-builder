from __future__ import annotations

from orchestrator.core.prompt_model_schema import prompt_model_from_dataclass
from orchestrator.core.runtime.payload_models import (
    ArchitectStageOutput,
    AskIntent,
    DecisionGate,
    DecisionPlanner,
    EngineeringClarification,
    EngineeringSeedPlan,
    GoodToDo,
    InteractionResponse,
    JiraIssueIntakeRoute,
    PMDecisionResolutionSet,
    PMInterviewPlan,
    PMMessageBrief,
    ParentFeatureBriefNormalization,
    PmParentSeedPlan,
    PRReady,
    PRReviewFindings,
    PrecheckMessage,
    PrecheckPolicy,
    RepoSetup,
    RetroVoiceBrief,
    SecurityStageOutput,
    StandupVoiceBrief,
    TestingStageOutput,
    VoiceEntryRoute,
    WorkerCapabilityRequirement,
)
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
)
from orchestrator.core.workflow.runner import QaResult


PromptDomainContract = type[
    ArchitectStageOutput
    | AskIntent
    | DecisionGate
    | DecisionPlanner
    | DevResult
    | EngineeringClarification
    | EngineeringSeedPlan
    | GoodToDo
    | InteractionResponse
    | JiraIssueIntakeRoute
    | PMDecisionResolutionSet
    | PMInterviewPlan
    | PMMessageBrief
    | ParentFeatureBriefNormalization
    | PmParentSeedPlan
    | PmPlan
    | QaResult
    | PRReady
    | PRReviewFindings
    | PrecheckMessage
    | PrecheckPolicy
    | RepoSetup
    | RetroVoiceBrief
    | ReviewResult
    | SecurityStageOutput
    | StandupVoiceBrief
    | TestingStageOutput
    | TestResult
    | VoiceEntryRoute
    | WorkerCapabilityRequirement
]


_DOMAIN_MODELS_BY_TEMPLATE: dict[str, PromptDomainContract] = {
    "policy/decision_gate_system.j2": DecisionGate,
    "policy/decision_planner_system.j2": DecisionPlanner,
    "policy/decision_reply_system.j2": InteractionResponse,
    "policy/gtd_system.j2": GoodToDo,
    "policy/precheck_message_system.j2": PrecheckMessage,
    "policy/precheck_system.j2": PrecheckPolicy,
    "policy/pr_ready_system.j2": PRReady,
    "policy/pr_review_findings_system.j2": PRReviewFindings,
    "policy/worker_capability_system.j2": WorkerCapabilityRequirement,
    "workflow/pm_decision_resolution_system.j2": PMDecisionResolutionSet,
    "workflow/pm_parent_brief_normalization_system.j2": ParentFeatureBriefNormalization,
    "workflow/pm_parent_brief_normalization_user.j2": ParentFeatureBriefNormalization,
    "workflow/pm_planning_architect_system.j2": ArchitectStageOutput,
    "workflow/pm_planning_architect_user.j2": ArchitectStageOutput,
    "workflow/pm_planning_security_system.j2": SecurityStageOutput,
    "workflow/pm_planning_security_user.j2": SecurityStageOutput,
    "workflow/pm_planning_tester_system.j2": TestingStageOutput,
    "workflow/pm_planning_tester_user.j2": TestingStageOutput,
    "workflow/pm_system.j2": PmPlan,
    "workflow/pm_user.j2": PmPlan,
    "workflow/dev_system.j2": DevResult,
    "workflow/dev_user.j2": DevResult,
    "workflow/test_system.j2": TestResult,
    "workflow/test_user.j2": TestResult,
    "workflow/review_system.j2": ReviewResult,
    "workflow/review_user.j2": ReviewResult,
    "workflow/qa_system.j2": QaResult,
    "workflow/qa_user.j2": QaResult,
    "workflow/standup_voice_brief_system.j2": StandupVoiceBrief,
    "workflow/retro_voice_brief_system.j2": RetroVoiceBrief,
    "repo_setup/prepare_user.j2": RepoSetup,
    "discord/ask_intent_system.j2": AskIntent,
    "discord/pm_answer_system.j2": PMMessageBrief,
    "discord/pm_answer_user.j2": PMMessageBrief,
    "discord/pm_interview_system.j2": PMInterviewPlan,
    "discord/pm_interview_user.j2": PMInterviewPlan,
    "discord/issues_seed_system.j2": EngineeringSeedPlan,
    "discord/issues_seed_user.j2": EngineeringSeedPlan,
    "discord/pm_seed_batch_system.j2": PmParentSeedPlan,
    "discord/pm_seed_batch_user.j2": PmParentSeedPlan,
    "discord/voice_entry_router_system.j2": VoiceEntryRoute,
    "discord/voice_room_pm_system.j2": PMMessageBrief,
    "discord/voice_room_reviewer_system.j2": PMMessageBrief,
    "jira/engineering_clarification_system.j2": EngineeringClarification,
    "jira/engineering_clarification_user.j2": EngineeringClarification,
    "jira/issue_intake_routing_system.j2": JiraIssueIntakeRoute,
    "jira/issue_intake_routing_user.j2": JiraIssueIntakeRoute,
}


def prompt_domain_model_for_template(template_name: str) -> dict[str, object] | None:
    domain_model = _DOMAIN_MODELS_BY_TEMPLATE.get(template_name)
    if domain_model is None:
        return None
    return prompt_model_from_dataclass(domain_model)


def registered_prompt_domain_model_templates() -> tuple[str, ...]:
    return tuple(sorted(_DOMAIN_MODELS_BY_TEMPLATE))
