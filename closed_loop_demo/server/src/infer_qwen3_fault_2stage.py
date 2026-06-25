#!/usr/bin/env python3
# 3CM-R1 compatibility wrapper, candidate/shadow only.
# Provides the import surface expected by closed_loop_infer_run.py:
# build_model(), stage1_reason(), and stage2_summarize().
# It does not train, mutate adapters, call HDC/board commands, execute Action R1, or emit action_command.

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


BASE_MODEL = os.environ.get("QWEN3_BASE_MODEL", "/home/xrh/models/Qwen/Qwen3-8B")
LEGACY_ADAPTER = "/home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora"
PRIMARY_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_expanded_300_rca_aware_3cg_r1_20260611/train_3cg_r1_20260611_104816/adapter"
ROLLBACK_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_mixed_11class_rca_aware_3bz_r1_20260609/train_3bz_r1_20260609_100500/adapter"
LABEL_FALLBACK_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_mixed_11class_3bs_r4_20260608/train_r4_20260608_165610/adapter"
MAX_NEW_TOKENS = min(512, int(os.environ.get("QWEN3_MAX_NEW_TOKENS", "256")))
COMMAND_KEYS = {"cmd", "command", "action_command"}


def write_runtime_proof(**updates: Any) -> None:
    proof_path = os.environ.get("WK_3CM_RUNTIME_PROOF_PATH", "").strip()
    if not proof_path:
        return
    path = Path(proof_path)
    try:
        current: dict[str, Any] = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
        current.update({
            "schema_version": "3cm_adapter_runtime_proof_v1",
            "mode": "candidate_shadow_only" if os.environ.get("WK_3CM_CANDIDATE_MODE", "").strip() == "1" else "legacy_compat",
        })
        current.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return


def adapter_selection() -> dict[str, Any]:
    candidate_mode = os.environ.get("WK_3CM_CANDIDATE_MODE", "").strip() == "1"
    default_adapter = PRIMARY_3CM_ADAPTER if candidate_mode else LEGACY_ADAPTER
    primary = os.environ.get("WK_QWEN3_ADAPTER_PRIMARY") or os.environ.get("QWEN3_ADAPTER_DIR") or default_adapter
    selection = {
        "schema_version": "3cm_r1_adapter_selection_v1",
        "mode": "candidate_shadow_only" if candidate_mode else "legacy_compat",
        "base_model": BASE_MODEL,
        "primary_adapter": primary,
        "rollback_adapter": os.environ.get("WK_QWEN3_ADAPTER_ROLLBACK", ROLLBACK_3CM_ADAPTER),
        "label_fallback_adapter": os.environ.get("WK_QWEN3_ADAPTER_LABEL_FALLBACK", LABEL_FALLBACK_3CM_ADAPTER),
        "qwen3_adapter_dir": os.environ.get("QWEN3_ADAPTER_DIR", primary),
        "stage2_enabled": os.environ.get("WK_QWEN3_ENABLE_STAGE2", ""),
    }
    checks = {
        "adapter_path_set": bool(selection["primary_adapter"]),
        "qwen3_adapter_dir_matches_primary": selection["qwen3_adapter_dir"] == selection["primary_adapter"],
    }
    if candidate_mode:
        checks.update({
            "primary_matches_3cg_r1": selection["primary_adapter"] == PRIMARY_3CM_ADAPTER,
            "qwen3_adapter_dir_matches_primary": selection["qwen3_adapter_dir"] == PRIMARY_3CM_ADAPTER,
            "rollback_matches_3bz_r1": selection["rollback_adapter"] == ROLLBACK_3CM_ADAPTER,
            "label_fallback_matches_3bs_r4": selection["label_fallback_adapter"] == LABEL_FALLBACK_3CM_ADAPTER,
            "stage2_enabled": selection["stage2_enabled"] == "1",
        })
    selection["checks"] = checks
    selection["status"] = "PASS" if all(checks.values()) else "FAIL"
    return selection


def fail_if_bad_adapter_selection() -> dict[str, Any]:
    selection = adapter_selection()
    if selection["status"] != "PASS":
        raise RuntimeError("3CM-R1 adapter selection failed: " + json.dumps(selection, ensure_ascii=False))
    return selection


def contains_command_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in COMMAND_KEYS:
                return True
            if contains_command_field(child):
                return True
    elif isinstance(value, list):
        return any(contains_command_field(child) for child in value)
    return False


def schema_smoke_payload() -> dict[str, Any]:
    payload = {
        "family": "cpu",
        "main_label": "cpu_single_point_high_load",
        "display_name_zh": "CPU single point high load",
        "diagnosis_summary": "Host-only schema smoke for 3CM-R1 candidate runtime wiring.",
        "evidence": [
            {"source_signal": "schema_smoke", "value": "host_only", "why": "validates RCA output shape without board access"}
        ],
        "root_object": {
            "schema_version": "root_object.v1",
            "object_type": "process",
            "object_name": "candidate_process",
            "object_id": "schema_smoke",
            "scope": "host_only",
            "evidence_refs": ["schema_smoke"],
            "attributes": {"source": "3CM-R1"},
        },
        "cause": {"summary": "Synthetic host-only smoke cause.", "evidence_refs": ["schema_smoke"]},
        "symptom": {"summary": "Synthetic host-only smoke symptom.", "evidence_refs": ["schema_smoke"]},
        "candidate_caveat": ["candidate/shadow only", "not production baseline"],
        "safety_caveat": ["suggestion-only", "execution_enabled=false", "manual_approval_required=true"],
        "action_r1_suggestion_id": "r1_schema_smoke_collect_only",
        "action_r1_summary": "Review evidence manually; no command is generated or executed.",
        "execution_enabled": False,
        "manual_approval_required": True,
    }
    if contains_command_field(payload):
        raise RuntimeError("schema smoke unexpectedly contains command-shaped fields")
    return payload


def validate_schema_payload(payload: dict[str, Any]) -> None:
    required = [
        "family",
        "main_label",
        "diagnosis_summary",
        "evidence",
        "root_object",
        "cause",
        "symptom",
        "action_r1_suggestion_id",
        "action_r1_summary",
        "execution_enabled",
        "manual_approval_required",
    ]
    missing = [k for k in required if k not in payload]
    if missing:
        raise RuntimeError("schema smoke missing fields: " + ",".join(missing))
    root_object = payload.get("root_object")
    if not isinstance(root_object, dict) or root_object.get("schema_version") != "root_object.v1":
        raise RuntimeError("root_object.v1 invalid")
    if payload.get("execution_enabled") is not False:
        raise RuntimeError("execution_enabled must be false")
    if payload.get("manual_approval_required") is not True:
        raise RuntimeError("manual_approval_required must be true")
    if contains_command_field(payload):
        raise RuntimeError("command-shaped field detected")


def load_test_samples(path: str) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def build_model(device_map: Any = None):
    selection = fail_if_bad_adapter_selection()
    write_runtime_proof(
        adapter=selection["primary_adapter"],
        base_model=BASE_MODEL,
        model_loaded=False,
        adapter_loaded=False,
        generation_ok=False,
    )
    try:
        import torch
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    except Exception as exc:
        write_runtime_proof(load_error=str(exc)[:500])
        raise RuntimeError("Qwen3 runtime dependencies unavailable; use --schema-smoke for host-only validation") from exc

    try:
        if device_map is None:
            device_map = os.environ.get("QWEN3_DEVICE_MAP", "auto")
        tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, use_fast=False)
        if tokenizer.pad_token is None:
            tokenizer.pad_token = tokenizer.eos_token
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        base_model = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL,
            quantization_config=bnb_config,
            device_map=device_map,
            torch_dtype=torch.bfloat16,
        )
        write_runtime_proof(model_loaded=True)
        model = PeftModel.from_pretrained(base_model, selection["primary_adapter"])
        model.eval()
        write_runtime_proof(adapter_loaded=True)
        return tokenizer, model
    except Exception as exc:
        write_runtime_proof(load_error=str(exc)[:500])
        raise


def _generate(tokenizer: Any, model: Any, messages: list[dict[str, str]]) -> str:
    import torch

    input_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(model.device)
    attention_mask = torch.ones_like(input_ids)
    with torch.no_grad():
        gen_ids = model.generate(
            input_ids=input_ids,
            attention_mask=attention_mask,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )
    gen_ids = gen_ids[0][input_ids.shape[-1]:]
    out = tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
    write_runtime_proof(generation_ok=bool(out), last_generation_chars=len(out))
    return out


def stage1_reason(tokenizer: Any, model: Any, messages_for_model: list[dict[str, str]]) -> str:
    return _generate(tokenizer, model, messages_for_model)


def stage2_summarize(tokenizer: Any, model: Any, analysis_text: str) -> str:
    system = {
        "role": "system",
        "content": (
            "You are a KaiHongOS/OpenHarmony OS fault diagnosis assistant. "
            "Return only a concise four-section diagnosis. Do not output command fields. "
            "Action R1 is suggestion-only: execution_enabled=false and manual_approval_required=true."
        ),
    }
    user = {
        "role": "user",
        "content": (
            "Summarize the analysis using this exact parser-friendly format:\n"
            "1. Fault judgement\n"
            "- fault_state: fault|normal|unknown\n"
            "- family: net|cpu|mem|background|other\n"
            "2. Root cause\n"
            "- concise evidence-aware RCA text\n"
            "3. Suggested checks\n"
            "- suggestion-only manual check, no command\n"
            "4. Confidence: 0.xx\n\n"
            "Analysis:\n"
            f"{analysis_text}"
        ),
    }
    out = _generate(tokenizer, model, [system, user])
    if re.search(r"\b(action_command|cmd|command)\b\s*[:=]", out, flags=re.IGNORECASE):
        write_runtime_proof(generation_ok=False, generation_error="command-shaped field in model output")
        raise RuntimeError("model output contains command-shaped field")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema-smoke", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--print-adapter-selection", action="store_true")
    args = ap.parse_args()

    if args.print_adapter_selection:
        print(json.dumps(adapter_selection(), ensure_ascii=False, indent=2))
        return
    if args.schema_smoke or args.self_test:
        selection = adapter_selection()
        payload = schema_smoke_payload()
        validate_schema_payload(payload)
        print(json.dumps({"adapter_selection": selection, "schema_smoke": payload}, ensure_ascii=False, indent=2))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
