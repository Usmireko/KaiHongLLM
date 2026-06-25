# PathLease

PathLease is a manual coordination record for path ownership during a task. It
helps avoid overlapping edits and clarifies which files are read-only,
write-allowed, or blocked.

P0 status: documentation and template only. It does not lock files, call Git,
monitor processes, or prevent edits.

## Lease Types

Suggested lease modes:
- `read_only`: inspect paths but do not edit.
- `write_allowed`: edits are allowed inside the named path set.
- `blocked`: do not touch these paths in the current task.
- `future_integration`: mention only; no P0 edits.

## Project Defaults

For harness scaffold work:
- `.codex-harness/**` is `write_allowed`.
- Existing business scripts are `blocked` unless the user explicitly asks for
  code changes.
- `server_B/tcp/**` is `future_integration` for this P0.
- Board paths and board deployment scripts are `blocked` for this P0.
- Dataset artifacts under `inbox`, `dataset_batches`, and generated run folders
  are `blocked` unless a validation task explicitly scopes them.

## Lease Notes

The lease should name:
- Owner or operator.
- Reason for the lease.
- Start time and intended expiry.
- Allowed operations.
- Cooldown requirement after changes.
- Review status, if any.

In P0, cooldown is a note such as "wait for manual validation before next
change." It is not enforced automatically.

## Template

Use `templates/path_lease.yaml`.
