from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrecheckSlotDefinition:
    slot_id: str
    aliases: tuple[str, ...] = ()


_SLOT_DEFINITIONS: tuple[PrecheckSlotDefinition, ...] = (
    PrecheckSlotDefinition("objective"),
    PrecheckSlotDefinition("scope"),
    PrecheckSlotDefinition("acceptance_criteria"),
    PrecheckSlotDefinition("how_to_test"),
    PrecheckSlotDefinition("nfr_intent"),
    PrecheckSlotDefinition("reliability_security_constraints"),
    PrecheckSlotDefinition("out_of_scope"),
    PrecheckSlotDefinition("rollout_constraints"),
    PrecheckSlotDefinition("decision_owner"),
    PrecheckSlotDefinition("dependencies_and_risks"),
)

_CANONICAL_IDS = {definition.slot_id for definition in _SLOT_DEFINITIONS}
_ALIAS_TO_ID = {
    alias: definition.slot_id
    for definition in _SLOT_DEFINITIONS
    for alias in definition.aliases
}


def canonical_slot_id(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        return ""
    normalized = raw_value.strip()
    if not normalized:
        return ""
    if normalized in _CANONICAL_IDS:
        return normalized
    return _ALIAS_TO_ID.get(normalized, "")


def all_precheck_slot_ids() -> tuple[str, ...]:
    return tuple(definition.slot_id for definition in _SLOT_DEFINITIONS)
