from __future__ import annotations

PARENT_OP_BRIEF_NORMALIZATION = "brief_normalization"
PARENT_OP_JIRA_PARENT_UPDATE = "jira_parent_update"
PARENT_OP_JIRA_COMMENT_PROJECTION = "jira_comment_projection"
PARENT_OP_DISCORD_FOLLOWUP_PROJECTION = "discord_followup_projection"
PARENT_OP_BACKLOG_PLANNING = "backlog_planning"
PARENT_OP_JIRA_CHILD_FANOUT = "jira_child_fanout"
PARENT_OP_JIRA_CHILD_PROMOTION = "jira_child_promotion"
PARENT_OP_NOTIFICATION_EMIT = "notification_emit"

PARENT_RETRYABLE_OPERATION_TYPES = frozenset(
    {
        PARENT_OP_JIRA_PARENT_UPDATE,
        PARENT_OP_JIRA_CHILD_FANOUT,
    }
)

PARENT_PROJECTION_OPERATION_TYPES = frozenset(
    {
        PARENT_OP_JIRA_COMMENT_PROJECTION,
        PARENT_OP_DISCORD_FOLLOWUP_PROJECTION,
        PARENT_OP_NOTIFICATION_EMIT,
    }
)
