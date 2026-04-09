from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HumanInputAnswerInput:
    request_id: str
    answer_source_ref: str | None


@dataclass(frozen=True)
class HumanInputResumeResult:
    resumed_run_id: str | None
