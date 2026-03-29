from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.storage.models import PlatformSetting

SETTING_KEY_AGENT_RUNTIME_ROUTING = "agent_runtime_routing"
SETTING_KEY_AGENT_RUNTIME_PROFILES = "agent_runtime_profiles"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PlatformSettingsService:
    def get_json(self, *, session: Session, setting_key: str) -> dict:
        row = session.get(PlatformSetting, str(setting_key or "").strip())
        if row is None or not isinstance(row.value_json, dict):
            return {}
        return dict(row.value_json)

    def upsert_json(self, *, session: Session, setting_key: str, value_json: dict) -> PlatformSetting:
        normalized_key = str(setting_key or "").strip()
        if not normalized_key:
            raise ValueError("setting_key is required")
        if not isinstance(value_json, dict):
            raise ValueError("value_json must be an object")
        now = _utc_now()
        row = session.get(PlatformSetting, normalized_key)
        if row is None:
            row = PlatformSetting(
                setting_key=normalized_key,
                value_json=dict(value_json),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.value_json = dict(value_json)
            row.updated_at = now
        session.commit()
        session.refresh(row)
        return row

    def delete(self, *, session: Session, setting_key: str) -> None:
        normalized_key = str(setting_key or "").strip()
        if not normalized_key:
            raise ValueError("setting_key is required")
        row = session.get(PlatformSetting, normalized_key)
        if row is None:
            return
        session.delete(row)
        session.commit()


platform_settings_service = PlatformSettingsService()
