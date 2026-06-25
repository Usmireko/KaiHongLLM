#!/usr/bin/env python3
"""
Executable dataset validators for L1 and L2 artifacts.

This module avoids external dependencies and implements the core constraints
defined in dataset_schema_spec.md.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


ALLOWED_SUPPORT_ROLES = {"primary", "symptom", "secondary", "noise"}
L2_TASK_VERSION = "l2_tasks_v1"
PATH_LEAK_MARKERS = ("C:\\Users\\", "Desktop\\work")
POLLUTION_KINDS = {"Count", "Keys", "Values", "IsReadOnly", "IsFixedSize", "SyncRoot", "IsSynchronized"}
NET_SUBTYPE_TARGET_RE = re.compile(r"^net_[a-z0-9_]+$", re.IGNORECASE)
ACTIONABLE_NET_TARGET_KINDS = {
    "dns_resolver",
    "network_component",
    "network_interface",
    "network_path",
    "network_probe",
    "network_service",
    "route",
    "routing_table",
    "wifi_connection",
    "wifi_profile",
}


@dataclass
class ValidationIssue:
    path: str
    message: str


class ValidationError(Exception):
    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues = list(issues)
        summary = ["Validation failed:"]
        for issue in self.issues:
            summary.append(f"- {issue.path}: {issue.message}")
        super().__init__("\n".join(summary))


def _type_name(value: Any) -> str:
    return type(value).__name__


def _issue(issues: List[ValidationIssue], path: str, message: str) -> None:
    issues.append(ValidationIssue(path=path, message=message))


def _expect_mapping(value: Any, path: str, issues: List[ValidationIssue]) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        _issue(issues, path, f"expected object, got {_type_name(value)}")
        return None
    return value


def _expect_list(value: Any, path: str, issues: List[ValidationIssue]) -> Optional[List[Any]]:
    if not isinstance(value, list):
        _issue(issues, path, f"expected list, got {_type_name(value)}")
        return None
    return value


def _require_keys(obj: Dict[str, Any], keys: Iterable[str], path: str, issues: List[ValidationIssue]) -> None:
    for key in keys:
        if key not in obj:
            _issue(issues, f"{path}.{key}", "missing required field")


def _expect_string(value: Any, path: str, issues: List[ValidationIssue], allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        _issue(issues, path, f"expected string, got {_type_name(value)}")
        return
    if not allow_empty and value == "":
        _issue(issues, path, "must not be empty")


def _expect_bool(value: Any, path: str, issues: List[ValidationIssue]) -> None:
    if not isinstance(value, bool):
        _issue(issues, path, f"expected bool, got {_type_name(value)}")


def _scan_for_leaks(payload: Any, path: str, issues: List[ValidationIssue]) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for marker in PATH_LEAK_MARKERS:
        if marker in blob:
            _issue(issues, path, f"contains host path marker {marker}")
    if '"device_sn"' in blob:
        _issue(issues, path, "contains raw device_sn field")


def validate_evidence_candidate(candidate: Dict[str, Any], path: str = "evidence_candidate") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(candidate, path, issues)
    if obj is None:
        return issues

    _require_keys(obj, ("eid", "source", "kind", "text"), path, issues)
    if "eid" in obj:
        _expect_string(obj["eid"], f"{path}.eid", issues)
    if "source" in obj:
        _expect_string(obj["source"], f"{path}.source", issues)
    if "kind" in obj:
        _expect_string(obj["kind"], f"{path}.kind", issues)
        if obj["kind"] in POLLUTION_KINDS:
            _issue(issues, f"{path}.kind", f"contains blocked pollution kind {obj['kind']}")
    if "text" in obj:
        _expect_string(obj["text"], f"{path}.text", issues)
    if "support_role" in obj and obj["support_role"] not in ALLOWED_SUPPORT_ROLES:
        _issue(issues, f"{path}.support_role", f"must be one of {sorted(ALLOWED_SUPPORT_ROLES)}")
    _scan_for_leaks(obj, path, issues)
    return issues


def validate_canonical_case(canonical_case: Dict[str, Any], path: str = "canonical_case") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(canonical_case, path, issues)
    if obj is None:
        return issues

    _require_keys(
        obj,
        ("schema_version", "source_type", "source_case_id", "case_id", "source", "gt", "obs", "quality_flags"),
        path,
        issues,
    )
    for field_name in ("schema_version", "source_type", "source_case_id", "case_id"):
        if field_name in obj:
            _expect_string(obj[field_name], f"{path}.{field_name}", issues)

    gt = _expect_mapping(obj.get("gt"), f"{path}.gt", issues)
    if gt is not None:
        _require_keys(gt, ("family", "subtype"), f"{path}.gt", issues)
        if "family" in gt:
            _expect_string(gt["family"], f"{path}.gt.family", issues)
        if "subtype" in gt:
            _expect_string(gt["subtype"], f"{path}.gt.subtype", issues)

    obs = _expect_mapping(obj.get("obs"), f"{path}.obs", issues)
    if obs is not None:
        _require_keys(obs, ("primary_family", "secondary_families"), f"{path}.obs", issues)
        if "primary_family" in obs:
            _expect_string(obs["primary_family"], f"{path}.obs.primary_family", issues)
        if "secondary_families" in obs:
            secondary = _expect_list(obs["secondary_families"], f"{path}.obs.secondary_families", issues)
            if secondary is not None:
                for index, item in enumerate(secondary):
                    _expect_string(item, f"{path}.obs.secondary_families[{index}]", issues)

    quality_flags = _expect_list(obj.get("quality_flags"), f"{path}.quality_flags", issues)
    if quality_flags is not None:
        for index, flag in enumerate(quality_flags):
            _expect_string(flag, f"{path}.quality_flags[{index}]", issues)

    _scan_for_leaks(obj, path, issues)
    return issues


def _validate_l2_common(record: Dict[str, Any], expected_sample_type: str, path: str, issues: List[ValidationIssue]) -> None:
    _require_keys(
        record,
        ("task_version", "validation_status", "schema_version", "sample_type", "sample_id", "case_id", "source_case_id", "source_type", "input", "target"),
        path,
        issues,
    )
    for field_name in ("task_version", "validation_status", "schema_version", "sample_type", "sample_id", "case_id", "source_case_id", "source_type"):
        if field_name in record:
            _expect_string(record[field_name], f"{path}.{field_name}", issues)
    if record.get("task_version") != L2_TASK_VERSION:
        _issue(issues, f"{path}.task_version", f"must equal {L2_TASK_VERSION}")
    if record.get("sample_type") != expected_sample_type:
        _issue(issues, f"{path}.sample_type", f"must equal {expected_sample_type}")
    if record.get("source_case_id") in (None, ""):
        _issue(issues, f"{path}.source_case_id", "must exist")

    input_block = _expect_mapping(record.get("input"), f"{path}.input", issues)
    target_block = _expect_mapping(record.get("target"), f"{path}.target", issues)
    _scan_for_leaks(record, path, issues)

    if input_block is not None and "gt" in input_block and expected_sample_type in {"diagnosis", "cause_vs_symptom"}:
        _issue(issues, f"{path}.input.gt", "GT must not be placed in input for this task")
    if target_block is not None and expected_sample_type == "diagnosis":
        if "gt" not in target_block:
            _issue(issues, f"{path}.target.gt", "diagnosis target must contain gt")


def validate_l2_diagnosis(record: Dict[str, Any], path: str = "diagnosis_record") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(record, path, issues)
    if obj is None:
        return issues
    _validate_l2_common(obj, "diagnosis", path, issues)
    return issues


def validate_l2_evidence_extraction(record: Dict[str, Any], path: str = "evidence_extraction_record") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(record, path, issues)
    if obj is None:
        return issues
    _validate_l2_common(obj, "evidence_extraction", path, issues)

    input_block = obj.get("input") or {}
    target_block = obj.get("target") or {}
    candidate_rows = input_block.get("candidate_evidence") or []
    if not isinstance(candidate_rows, list):
        _issue(issues, f"{path}.input.candidate_evidence", "must be a list")
    else:
        for index, row in enumerate(candidate_rows):
            issues.extend(validate_evidence_candidate(row, f"{path}.input.candidate_evidence[{index}]"))

    for bucket in ("primary_evidence", "symptom_evidence", "secondary_evidence", "noise_evidence"):
        rows = target_block.get(bucket) or []
        if not isinstance(rows, list):
            _issue(issues, f"{path}.target.{bucket}", "must be a list")
            continue
        for index, row in enumerate(rows):
            issues.extend(validate_evidence_candidate(row, f"{path}.target.{bucket}[{index}]"))
    return issues


def validate_l2_cause_vs_symptom(record: Dict[str, Any], path: str = "cause_vs_symptom_record") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(record, path, issues)
    if obj is None:
        return issues
    _validate_l2_common(obj, "cause_vs_symptom", path, issues)

    target_block = obj.get("target") or {}
    if target_block.get("gt_obs_separated") is not True:
        _issue(issues, f"{path}.target.gt_obs_separated", "must be true")
    for field_name in ("primary_family", "primary_subtype"):
        if field_name in target_block:
            _expect_string(target_block[field_name], f"{path}.target.{field_name}", issues)
        else:
            _issue(issues, f"{path}.target.{field_name}", "missing required field")
    return issues


def validate_l2_action_after_diagnosis(record: Dict[str, Any], path: str = "action_after_diagnosis_record") -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    obj = _expect_mapping(record, path, issues)
    if obj is None:
        return issues
    _validate_l2_common(obj, "action_after_diagnosis", path, issues)

    input_block = obj.get("input") or {}
    target_block = obj.get("target") or {}
    if input_block.get("task_condition") != "diagnosis_conditioned":
        _issue(issues, f"{path}.input.task_condition", "must equal diagnosis_conditioned")
    diagnosis_result = input_block.get("diagnosis_result")
    diagnosis_result_obj = _expect_mapping(diagnosis_result, f"{path}.input.diagnosis_result", issues)
    if diagnosis_result_obj is not None:
        for field_name in ("family", "subtype"):
            if field_name in diagnosis_result_obj:
                _expect_string(diagnosis_result_obj[field_name], f"{path}.input.diagnosis_result.{field_name}", issues)
            else:
                _issue(issues, f"{path}.input.diagnosis_result.{field_name}", "missing required field")

    policy_obj = _expect_mapping(target_block.get("policy"), f"{path}.target.policy", issues)
    if policy_obj is not None:
        if policy_obj.get("task_condition") != "diagnosis_conditioned":
            _issue(issues, f"{path}.target.policy.task_condition", "must equal diagnosis_conditioned")
        if policy_obj.get("direct_commands_allowed") is not False:
            _issue(issues, f"{path}.target.policy.direct_commands_allowed", "must be false")
        if policy_obj.get("destructive_actions_allowed") is not False:
            _issue(issues, f"{path}.target.policy.destructive_actions_allowed", "must be false")

    actions_obj = _expect_list(target_block.get("recommended_actions"), f"{path}.target.recommended_actions", issues)
    if actions_obj is not None:
        for index, action in enumerate(actions_obj):
            action_obj = _expect_mapping(action, f"{path}.target.recommended_actions[{index}]", issues)
            if action_obj is None:
                continue
            target_kind = action_obj.get("target_kind")
            target = action_obj.get("target")
            if "target_kind" in action_obj:
                _expect_string(target_kind, f"{path}.target.recommended_actions[{index}].target_kind", issues)
            if "target" in action_obj:
                _expect_string(target, f"{path}.target.recommended_actions[{index}].target", issues)
            if isinstance(target_kind, str) and isinstance(target, str):
                if target_kind in ACTIONABLE_NET_TARGET_KINDS and NET_SUBTYPE_TARGET_RE.fullmatch(target.strip()):
                    _issue(
                        issues,
                        f"{path}.target.recommended_actions[{index}].target",
                        f"must be an actionable object, not NET subtype label {target}",
                    )
    return issues


def raise_if_invalid(issues: Sequence[ValidationIssue]) -> None:
    if issues:
        raise ValidationError(issues)
