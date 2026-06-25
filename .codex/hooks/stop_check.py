#!/usr/bin/env python3
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()

def run(cmd):
    try:
        return subprocess.run(
            cmd,
            cwd=ROOT,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except Exception as e:
        return None

def get_changed_files():
    result = run("git diff --name-only")
    if not result or result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]

def file_text(path):
    try:
        return (ROOT / path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""

def main():
    changed = get_changed_files()
    warnings = []

    risky_patterns = [
        re.compile(r"net_fault[0-9]+"),
        re.compile(r"run_wukong_collect_refactor[0-9]+"),
    ]

    board_shell_pattern = re.compile(r"\b(awk|tr)\b")

    for f in changed:
        text = file_text(f)

        for pat in risky_patterns:
            if pat.search(text) or pat.search(f):
                warnings.append(f"Forbidden numbered naming pattern found in {f}: {pat.pattern}")

        if f.endswith((".sh", ".ps1", ".py", ".md")):
            if board_shell_pattern.search(text):
                warnings.append(f"Potential awk/tr usage found in {f}; verify it is not board-side code.")

    high_risk_keywords = [
        "net",
        "fault",
        "export",
        "derive",
        "validate",
        "canonical",
        "evidence",
        "trigger",
        "collector",
    ]

    high_risk_files = [
        f for f in changed
        if any(k in f.lower() for k in high_risk_keywords)
    ]

    if high_risk_files:
        warnings.append(
            "High-risk files changed. Final response must include validation commands and results: "
            + ", ".join(high_risk_files)
        )

    if warnings:
        print("[STOP_CHECK_WARNINGS]")
        for w in warnings:
            print(f"- {w}")
        # Do not hard fail initially; this is a warning gate.
        return 0

    print("[STOP_CHECK_OK]")
    return 0

if __name__ == "__main__":
    sys.exit(main())