---
name: wk_safe_edit_with_backup
description: >
  Make direct file edits with mandatory backups for rollback.
  Before editing each file: cp -a file file.bak_<YYYYMMDD_HHMMSS>.
  Always return: changed file list, backup list, unified diff, and minimal regression steps.
---

# Guardrails
- Do not delete original files.
- Backups must be in-place next to original file.
- Diffs must be unified and copy-pasteable.
- Keep changes minimal and scoped.

# Workflow
1) Locate targets with grep -R patterns.
2) For each target file:
   - create backup: cp -a <f> <f>.bak_<ts>
   - apply minimal edits
3) Run quick lint/smoke if possible.
4) Output:
   - changed files
   - backup files
   - unified diff
   - regression commands
