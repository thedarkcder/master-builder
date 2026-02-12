from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone


class DiscordAskHistoryService:
    def __init__(
        self,
        *,
        max_pending_actions: int = 50,
        max_history_entries: int = 80,
        max_history_context: int = 6,
    ) -> None:
        self._max_pending_actions = max_pending_actions
        self._max_history_entries = max_history_entries
        self._max_history_context = max_history_context

    def tenant_ask_history(self, *, tenant) -> list[dict]:
        discord_config = tenant.discord_config or {}
        raw_history = discord_config.get("ask_history")
        if not isinstance(raw_history, list):
            return []
        normalized: list[dict] = []
        for entry in raw_history:
            if not isinstance(entry, dict):
                continue
            user_id = str(entry.get("user_id") or "").strip()
            channel_id = str(entry.get("channel_id") or "").strip()
            question = str(entry.get("question") or "").strip()
            answer = str(entry.get("answer") or "").strip()
            if not user_id or not channel_id or not question or not answer:
                continue
            normalized.append(
                {
                    "user_id": user_id,
                    "channel_id": channel_id,
                    "question": question,
                    "answer": answer,
                    "issue_key": str(entry.get("issue_key") or "").strip().upper() or None,
                    "status": str(entry.get("status") or "").strip() or None,
                    "created_at": str(entry.get("created_at") or "").strip()
                    or datetime.now(timezone.utc).isoformat(),
                }
            )
        return normalized

    def recent_ask_history(
        self,
        *,
        tenant,
        user_id: str,
        channel_id: str,
        limit: int | None = None,
    ) -> list[dict]:
        entries = self.tenant_ask_history(tenant=tenant)
        scoped = [
            entry
            for entry in entries
            if entry.get("user_id") == user_id and entry.get("channel_id") == channel_id
        ]
        if not scoped:
            return []
        history_limit = max(1, limit if isinstance(limit, int) and limit > 0 else self._max_history_context)
        return scoped[-history_limit:]

    def store_ask_history_entry(
        self,
        *,
        session,
        tenant,
        user_id: str,
        channel_id: str,
        question: str,
        answer: str,
        issue_key: str | None,
        status_name: str | None,
    ) -> None:
        discord_config = dict(tenant.discord_config or {})
        entries = self.tenant_ask_history(tenant=tenant)
        entries.append(
            {
                "user_id": user_id,
                "channel_id": channel_id,
                "question": question.strip(),
                "answer": answer.strip(),
                "issue_key": issue_key.strip().upper() if isinstance(issue_key, str) and issue_key.strip() else None,
                "status": status_name.strip() if isinstance(status_name, str) and status_name.strip() else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        discord_config["ask_history"] = entries[-self._max_history_entries :]
        tenant.discord_config = discord_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()

    def store_pending_ask_action(
        self,
        *,
        session,
        tenant,
        request_id: str,
        user_id: str,
        channel_id: str | None,
        question: str,
        summary: str,
        proposed_command: str,
    ) -> dict:
        discord_config = dict(tenant.discord_config or {})
        raw_pending = discord_config.get("pending_ask_actions")
        pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
        created_at = datetime.now(timezone.utc).isoformat()
        pending.append(
            {
                "request_id": request_id,
                "user_id": user_id.strip(),
                "channel_id": channel_id.strip() if isinstance(channel_id, str) and channel_id.strip() else None,
                "question": question.strip(),
                "summary": summary.strip(),
                "proposed_command": proposed_command.strip(),
                "created_at": created_at,
            }
        )
        discord_config["pending_ask_actions"] = pending[-self._max_pending_actions :]
        tenant.discord_config = discord_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return {"request_id": request_id, "created_at": created_at}

    def consume_pending_ask_action(
        self,
        *,
        session,
        tenant,
        request_id: str,
    ) -> dict | None:
        normalized_request_id = request_id.strip()
        if not normalized_request_id:
            return None
        discord_config = dict(tenant.discord_config or {})
        raw_pending = discord_config.get("pending_ask_actions")
        pending = [entry for entry in raw_pending if isinstance(entry, dict)] if isinstance(raw_pending, list) else []
        matched: dict | None = None
        kept: list[dict] = []
        for entry in pending:
            entry_id = str(entry.get("request_id") or "").strip()
            if matched is None and entry_id == normalized_request_id:
                matched = entry
                continue
            kept.append(entry)
        if matched is None:
            return None
        discord_config["pending_ask_actions"] = kept
        tenant.discord_config = discord_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return matched

    def remove_issue_key_from_ask_history(
        self,
        *,
        session,
        tenant,
        issue_key: str,
        user_id: str | None = None,
        channel_id: str | None = None,
    ) -> int:
        target_issue_key = issue_key.strip().upper()
        if not target_issue_key:
            return 0

        entries = self.tenant_ask_history(tenant=tenant)
        kept_entries: list[dict] = []
        removed_count = 0
        for entry in entries:
            entry_issue_key = str(entry.get("issue_key") or "").strip().upper()
            matches_scope = True
            if user_id is not None:
                matches_scope = matches_scope and entry.get("user_id") == user_id
            if channel_id is not None:
                matches_scope = matches_scope and entry.get("channel_id") == channel_id
            if matches_scope and entry_issue_key == target_issue_key:
                removed_count += 1
                continue
            kept_entries.append(entry)

        if removed_count == 0:
            return 0

        discord_config = dict(tenant.discord_config or {})
        discord_config["ask_history"] = kept_entries[-self._max_history_entries :]
        tenant.discord_config = discord_config
        tenant.updated_at = datetime.now(timezone.utc)
        session.commit()
        return removed_count

    def prune_missing_issue_keys_from_ask_history(
        self,
        *,
        session,
        tenant,
        user_id: str,
        channel_id: str,
        existing_issue_keys_fn: Callable[..., set[str]],
    ) -> int:
        connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
        if not connection_id:
            return 0

        entries = self.tenant_ask_history(tenant=tenant)
        scoped_issue_keys = {
            str(entry.get("issue_key") or "").strip().upper()
            for entry in entries
            if entry.get("user_id") == user_id and entry.get("channel_id") == channel_id
        }
        scoped_issue_keys.discard("")
        if not scoped_issue_keys:
            return 0

        existing_issue_keys = existing_issue_keys_fn(
            session=session,
            tenant=tenant,
            channel_id=channel_id,
            issue_keys=scoped_issue_keys,
        )
        missing_issue_keys = scoped_issue_keys - existing_issue_keys
        if not missing_issue_keys:
            return 0

        removed_count = 0
        for issue_key in sorted(missing_issue_keys):
            removed_count += self.remove_issue_key_from_ask_history(
                session=session,
                tenant=tenant,
                issue_key=issue_key,
                user_id=user_id,
                channel_id=channel_id,
            )
        return removed_count

    def collect_ask_context_with_history_context(
        self,
        *,
        session,
        tenant,
        user_id: str,
        channel_id: str,
        question: str,
        scoped_issue_key: str | None,
        collect_ask_context_fn: Callable[..., tuple[str | None, str | None, list[dict], dict[str, int]]],
        existing_issue_keys_fn: Callable[..., set[str]],
    ) -> tuple[str | None, str | None, list[dict], dict[str, int], list[dict]]:
        history_context = self.recent_ask_history(
            tenant=tenant,
            user_id=user_id,
            channel_id=channel_id,
            limit=self._max_history_context,
        )
        if self.prune_missing_issue_keys_from_ask_history(
            session=session,
            tenant=tenant,
            user_id=user_id,
            channel_id=channel_id,
            existing_issue_keys_fn=existing_issue_keys_fn,
        ):
            history_context = self.recent_ask_history(
                tenant=tenant,
                user_id=user_id,
                channel_id=channel_id,
                limit=self._max_history_context,
            )

        resolved_scoped_issue_key = scoped_issue_key
        if resolved_scoped_issue_key is None:
            for entry in reversed(history_context):
                issue_key = str(entry.get("issue_key") or "").strip().upper()
                if issue_key:
                    resolved_scoped_issue_key = issue_key
                    break

        normalized_issue_key, requested_status, issues, status_counts = collect_ask_context_fn(
            session=session,
            tenant=tenant,
            channel_id=channel_id,
            question=question,
            scoped_issue_key=resolved_scoped_issue_key,
        )
        return normalized_issue_key, requested_status, issues, status_counts, history_context
