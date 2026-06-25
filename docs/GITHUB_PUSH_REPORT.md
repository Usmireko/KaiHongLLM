# GitHub Push Report

## Source And Target

- Source workspace: `C:/Users/kaihong-xuerx/Desktop/work`
- Temporary publish directory: `C:/Users/kaihong-xuerx/Desktop/work_github_publish_20260625_090546`
- Target repository: `https://github.com/Usmireko/KaiHongLLM`
- Publishing mode: direct single-agent Git operations, no native subagents, no harness aggregation gate

## Branch Design

- `main`: complete cleaned engineering workspace.
- `board`: board-side focused snapshot for RK3588 / KaiHongOS / OpenHarmony scripts and docs.
- `server`: server-side focused snapshot for data processing, evaluation, model service, and inference server code.
- `host`: Windows host orchestration and bridge snapshot, with minimal deployment resources referenced by host scripts.

## Branch Snapshots Created

- `snapshots/main`
- `snapshots/board`
- `snapshots/server`
- `snapshots/host`

## Excluded From Publishing

- `.git/`
- `work_cleanup_backup_*`
- `archive/`
- `artifacts/`
- `ver1/`
- `logs/`
- `tools/out/`
- `docs/legacy/`
- `dataset_batches/`
- `inbox/`
- `_tmp*/`
- `__pycache__/`
- `.pytest_cache/`
- `.mypy_cache/`
- `.ruff_cache/`
- `.venv/`
- `venv/`
- `node_modules/`
- datasets, checkpoints, model weights, large generated outputs, real `.env` files, secrets, and local runtime files
- `*.bak_*`
- `*.zip`
- generated harness reports, task run history, and task specs
- `.codex-harness/tasks/` after secret-pattern scan flagged example task fixtures
- `data_train_test/server/scripts/tooling/evidence_diag20_inference_runner.py` after secret-pattern scan flagged password-like fixture text

## Conditional Directories

- Included in `main`: `.agents/`, `.codex/`, `.codex-harness/`
- Excluded from conditional directories: generated reports, generated task examples/specs, zip archives, caches, and runtime outputs
- Focus branches omit agent and harness internals to keep branch snapshots small and runtime-focused.

## Authentication And Remote

- `gh auth status`: not available in this shell because `gh` is not installed.
- `git ls-remote https://github.com/Usmireko/KaiHongLLM`: passed.
- Git write operations are run with `GIT_TERMINAL_PROMPT=0`.

## Remote Backup Result

- Created: `backup/remote-before-work-upload-20260625-090546`
- Source: previous `origin/main` at `876aee122b0b2200656645c13c55d9de97baa10a`

## Scan And Validation Summary

- `main`: shell syntax PASS, PowerShell parse PASS, Python parse PASS, demo preflight PASS, demo dry-run PASS, secret scan PASS, large file scan PASS, forbidden path scan PASS.
- `board`: shell syntax PASS, PowerShell parse PASS, Python parse PASS, board entrypoint existence PASS, secret scan PASS, large file scan PASS, forbidden path scan PASS.
- `server`: shell syntax PASS, PowerShell parse PASS, Python parse PASS, server entrypoint existence PASS, safe Python CLI `--help` checks PASS, secret scan PASS, large file scan PASS, forbidden path scan PASS.
- `host`: shell syntax PASS, PowerShell parse PASS, Python parse PASS, host entrypoint existence PASS, safe demo dry-run PASS, secret scan PASS, large file scan PASS, forbidden path scan PASS.

## Push Commands

Planned:

```sh
git push --force-with-lease origin main:main
git push --force-with-lease origin board:board
git push --force-with-lease origin server:server
git push --force-with-lease origin host:host
```

## Pushed Commit Hashes

- `board`: `f5e459d1bd0c409e61e203714d377ce90f6a1093`
- `server`: `29e9891821acd35fe8f9d7a98c2488443099d2dd`
- `host`: `3f4c53a7a1c60a8ff1dacf82efc59bdcb4118f9d`
- `main`: recorded in final verification after push. The exact current `main` commit cannot be self-referenced inside the commit that contains this report without changing that commit hash.

## Limitations

Not run during publishing validation:

- real HDC / board commands
- real SSH deployment
- live server restart
- real model loading
- model training
- data collection
- fault injection
- live closed-loop demo
