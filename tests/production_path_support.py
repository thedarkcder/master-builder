from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.core.config import get_settings
from orchestrator.core.platform_secret_service import platform_secret_service
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from tests.test_support.db_harness import SqliteTemplateDbTestCase


_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class DeferredTaskHarness:
    def __init__(self) -> None:
        self._events: list[threading.Event] = []
        self._errors: list[BaseException] = []

    def create_task(self, coro):  # noqa: ANN001
        event = threading.Event()
        self._events.append(event)

        def _runner() -> None:
            try:
                asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001
                self._errors.append(exc)
            finally:
                event.set()

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        return SimpleNamespace(add_done_callback=lambda callback: None, thread=thread)

    def wait(self, *, timeout: float = 5.0) -> None:
        for event in self._events:
            if not event.wait(timeout):
                raise AssertionError("Deferred task did not complete in time")
        if self._errors:
            raise self._errors[0]


class FakeDiscordApiClient:
    def __init__(self, *, bot_token: str | None = None) -> None:
        self.bot_token = str(bot_token or "")
        self.posted_messages: list[dict[str, object]] = []
        self.posted_attachments: list[dict[str, object]] = []
        self.created_threads: list[dict[str, str]] = []
        self._messages: dict[tuple[str, str], dict[str, object]] = {}
        self._next_message_id = 1000

    def _new_message_id(self) -> str:
        self._next_message_id += 1
        return str(self._next_message_id)

    def post_message(self, *, channel_id: str, content: str, components=None):  # noqa: ANN001
        message_id = self._new_message_id()
        payload = {
            "id": message_id,
            "channel_id": channel_id,
            "content": content,
            "components": components,
        }
        self.posted_messages.append(payload)
        self._messages[(channel_id, message_id)] = payload
        return {"id": message_id}

    def post_message_with_attachment(
        self,
        *,
        channel_id: str,
        content: str,
        filename: str,
        file_bytes: bytes,
        content_type: str = "application/octet-stream",
        components=None,  # noqa: ANN001
    ) -> dict:
        message_id = self._new_message_id()
        payload = {
            "id": message_id,
            "channel_id": channel_id,
            "content": content,
            "filename": filename,
            "file_bytes": bytes(file_bytes),
            "content_type": content_type,
            "components": components,
        }
        self.posted_attachments.append(payload)
        self._messages[(channel_id, message_id)] = payload
        return {"id": message_id}

    def create_thread_from_message(self, *, channel_id: str, message_id: str, name: str, auto_archive_duration: int = 1440) -> str:  # noqa: ARG002
        thread_id = f"thread-{message_id}"
        self.created_threads.append({"channel_id": channel_id, "message_id": message_id, "name": name})
        self._messages[(channel_id, message_id)] = {
            **self._messages.get((channel_id, message_id), {"id": message_id, "channel_id": channel_id}),
            "thread": {"id": thread_id},
        }
        return thread_id

    def ensure_thread_for_message(self, *, channel_id: str, message_id: str, thread_name: str) -> str:
        message = self._messages.get((channel_id, message_id))
        thread = message.get("thread") if isinstance(message, dict) else None
        if isinstance(thread, dict) and str(thread.get("id") or "").strip():
            return str(thread["id"])
        return self.create_thread_from_message(channel_id=channel_id, message_id=message_id, name=thread_name)

    def get_message(self, *, channel_id: str, message_id: str) -> dict:
        return dict(self._messages.get((channel_id, message_id), {"id": message_id, "channel_id": channel_id}))

    def get_channel(self, *, channel_id: str) -> dict[str, str]:
        return {"id": channel_id, "name": f"channel-{channel_id}"}


def configure_runtime_environment(
    *,
    temp_dir: TemporaryDirectory,
    database_name: str,
    include_admin: bool = False,
    include_checkout_dir: bool = True,
) -> tuple[str, str]:
    database_url = f"sqlite:///{temp_dir.name}/{database_name}"
    checkout_dir = os.path.join(temp_dir.name, "checkouts")
    os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
    os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
    if include_checkout_dir:
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = checkout_dir
    if include_admin:
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
    get_settings.cache_clear()
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    return database_url, checkout_dir


def clear_runtime_environment() -> None:
    for key in (
        "ORCHESTRATOR_DATABASE_URL",
        "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY",
        "ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR",
        "ORCHESTRATOR_ADMIN_USERNAME",
        "ORCHESTRATOR_ADMIN_PASSWORD",
    ):
        os.environ.pop(key, None)
    get_settings.cache_clear()
    reset_db_engine_cache()


def session_factory_for(database_url: str):
    return create_session_factory(database_url=database_url)


class ProductionPathApiTestCase(SqliteTemplateDbTestCase):
    """Production-path API tests with one seeded runtime template per class."""

    _class_client: TestClient
    _runtime_workspace: TemporaryDirectory[str]
    _runtime_checkout_dir: str
    _class_env_originals: dict[str, str | None]
    _secrets_encryption_key: str

    @classmethod
    def include_admin_env(cls) -> bool:
        return False

    @classmethod
    def include_checkout_dir(cls) -> bool:
        return True

    @classmethod
    def bootstrap_template_state(cls) -> None:
        """Optional hook for one-time runtime/bootstrap work on the template DB."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._runtime_workspace = TemporaryDirectory()
        cls._runtime_checkout_dir = os.path.join(cls._runtime_workspace.name, "checkouts")
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

        cls._class_env_originals = {}
        env_updates = {
            "ORCHESTRATOR_DATABASE_URL": cls._template_database_url,
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
        }
        if cls.include_checkout_dir():
            env_updates["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = cls._runtime_checkout_dir
        if cls.include_admin_env():
            env_updates["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
            env_updates["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"

        for key, value in env_updates.items():
            cls._class_env_originals[key] = os.environ.get(key)
            os.environ[key] = value

        get_settings.cache_clear()
        reset_db_engine_cache()
        from orchestrator.api.main import create_app

        cls._class_client = TestClient(create_app())
        cls.bootstrap_template_state()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._class_client.close()
        finally:
            for key, original_value in cls._class_env_originals.items():
                if original_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original_value
            get_settings.cache_clear()
            reset_db_engine_cache()
            cls._runtime_workspace.cleanup()
            super().tearDownClass()

    def _start_test_runtime(self, *, name_prefix: str) -> str:
        database_url = self._prepare_test_database(name_prefix=name_prefix)
        os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        self.database_url = database_url
        self.session_factory = session_factory_for(database_url)
        self.client = self.__class__._class_client
        return database_url

    def _stop_test_runtime(self) -> None:
        self._cleanup_test_database()
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self._template_database_url
        get_settings.cache_clear()
        reset_db_engine_cache()


def seed_core_runtime_state(
    session_factory,
    *,
    tenant_id: str = "route25",
    project_id: str = "route25-default",
    jira_project_key: str = "TP",
    github_repository: str = "org/repo",
    discord_channel_id: str = "discord-channel-1",
    tenant_discord_config: dict | None = None,
    project_discord_config: dict | None = None,
    tenant_is_enabled: bool = True,
) -> tuple[Tenant, Project, JiraOAuthConnection]:
    now = datetime.now(timezone.utc)
    tenant_cfg = tenant_discord_config or {
        "channel_id": discord_channel_id,
        "notify_events": [],
        "allowed_user_ids": ["u-1"],
    }
    project_cfg = project_discord_config or {
        "channel_id": discord_channel_id,
        "notify_events": [],
        "allowed_user_ids": ["u-1"],
    }
    with session_factory() as session:
        tenant = Tenant(
            tenant_id=tenant_id,
            name=tenant_id,
            is_enabled=tenant_is_enabled,
            jira_config={
                "connection_id": "conn-1",
                "project_keys": [jira_project_key],
                "ready_statuses": ["To Do", "Ready for Agent"],
                "ready_trigger_mode": "status_recheck",
                "ready_jql": f'project = {jira_project_key} AND status = "To Do"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": None,
                "webhook_secret_ref": None,
            },
            github_config={
                "mode": "github_app",
                "installation_id": "12345",
                "webhook_secret_ref": None,
            },
            repos_config={"github_repository": f"https://github.com/{github_repository}"},
            policy_config={
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            discord_config=tenant_cfg,
            created_at=now,
            updated_at=now,
        )
        project = Project(
            project_id=project_id,
            tenant_id=tenant_id,
            name=project_id,
            github_repository=f"https://github.com/{github_repository}",
            jira_project_key=jira_project_key,
            policy_overrides={},
            environment={},
            secret_refs={},
            discord_config=project_cfg,
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        connection = JiraOAuthConnection(
            connection_id="conn-1",
            account_id="account-1",
            account_email="test@example.com",
            cloud_id="cloud-1",
            site_url="https://example.atlassian.net",
            scopes=["read:jira-work", "write:jira-work"],
            access_token_encrypted=encrypt_value(
                plaintext="access-token",
                encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
            ),
            refresh_token_encrypted=encrypt_value(
                plaintext="refresh-token",
                encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
            ),
            access_token_expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant, project, connection])
        session.commit()
        return tenant, project, connection


def upsert_platform_secret(*, session_factory, secret_ref: str, plaintext_value: str) -> None:
    with session_factory() as session:
        platform_secret_service.upsert_secret(
            session=session,
            secret_ref=secret_ref,
            plaintext_value=plaintext_value,
            encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
        )
        session.commit()


def github_signature(*, payload_bytes: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def load_json_fixture(*relative_parts: str) -> dict:
    fixture_path = _FIXTURES_DIR.joinpath(*relative_parts)
    with fixture_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
