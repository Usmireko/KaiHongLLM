# PROJECT-HDC-RULES.md — Project-Specific HDC Rules

This reference defines project-specific rules for using HDC from Windows PowerShell to operate KaiHongOS / OpenHarmony boards in the OS fault diagnosis project.

Use this together with:
- repo root AGENTS.md
- .agents/skills/hdc-kaihongos-windows/SKILL.md
- .agents/skills/hdc-kaihongos-windows/references/HDC-COMMANDS.md
- faults/net/AGENTS.md when the task is NET-related
- board-net-collect-and-analyze skill for new NET collection
- board-net-review-run skill for existing NET run review

This file is project-specific. It should override generic HDC examples when the two conflict.

---

## 1) Project context

This project uses HDC to interact with KaiHongOS / OpenHarmony boards for:

- board-side fault injection
- telemetry collection
- network probing
- log collection
- run bundle generation
- recovery observation
- validation of collected samples

The common host-side environment is:

- Windows PowerShell
- hdc
- scripts that call hdc shell / hdc file send / hdc file recv
- optional server-side SSH in other project workflows

Do not assume a full desktop Linux environment on the target board.

---

## 2) Board command restrictions

The target board environment does not have:

- awk
- tr

Do not generate board-side commands that depend on these tools.

Avoid relying on the following unless already verified on the current target image:

- netcat
- nc
- ss
- ip
- jq
- python
- perl
- find -exec
- GNU-specific grep/sed options

Prefer:

- sh
- cat
- grep
- sed
- head
- tail
- cut only when known safe
- ifconfig
- route
- ping
- simple shell loops
- multiple short hdc shell calls

When in doubt, use simpler commands and explicit output markers.

---

## 3) PowerShell to hdc quoting rules

Remote command construction is fragile.

Default style for this project:

  hdc shell "ifconfig"
  hdc shell "route"
  hdc shell "ping -c 3 8.8.8.8"

Prefer double quotes around the remote command on the PowerShell side.

Use single quotes only inside the remote command when needed.

Examples:

  hdc shell "echo '### PROBE_IP begin'"
  hdc shell "ping -c 3 8.8.8.8"
  hdc shell "echo '### PROBE_IP end'"

Avoid complex interpolation such as:

  hdc shell "ping -c 3 $UserInput"

If variables must be used:
1. validate them first
2. restrict allowed characters
3. avoid shell metacharacters
4. prefer known-good constants for probe targets

Dangerous characters in remote command variables include:

- ;
- &
- |
- `
- $
- <
- >
- newline
- carriage return

Do not inject raw hostnames, IPs, or paths into hdc shell commands without validation.

---

## 4) Prefer short hdc commands

Prefer many short invocations over one clever remote script.

Good style:

  hdc shell "echo '### NET_ROUTE begin'"
  hdc shell "route"
  hdc shell "echo '### NET_ROUTE end'"

Avoid:

  hdc shell "echo start; route | awk ...; if ...; then ...; fi; echo end"

Reasons:

- easier to debug
- less quoting risk
- fewer Toybox / BusyBox compatibility problems
- clearer evidence for validators
- easier run window alignment

---

## 5) Probe marker rules

Every diagnostic probe should be visible in logs.

Use marker lines before and after important probes.

Recommended marker format:

  ### PROBE_<NAME> begin
  ### PROBE_<NAME> end

Examples:

  ### PROBE_IFCONFIG begin
  ### PROBE_IFCONFIG end

  ### PROBE_ROUTE begin
  ### PROBE_ROUTE end

  ### PROBE_GATEWAY begin
  ### PROBE_GATEWAY end

  ### PROBE_IP begin
  ### PROBE_IP end

  ### PROBE_DNS begin
  ### PROBE_DNS end

Self-tests or synthetic traffic must use explicit markers:

  ### SELFTEST_<TYPE> begin
  ### SELFTEST_<TYPE> end

Do not hide probes with silent redirects unless a structured result is logged elsewhere.

Avoid:

  hdc shell "ping -c 3 8.8.8.8 >/dev/null 2>&1"

Prefer:

  hdc shell "echo '### PROBE_IP begin'"
  hdc shell "ping -c 3 8.8.8.8"
  hdc shell "echo '### PROBE_IP end'"

---

## 6) Network probe layers

For NET-related work, prefer checking multiple layers:

1. interface state
2. IP address
3. route table
4. gateway ping
5. public IP ping
6. DNS host probe

Example sequence:

  hdc shell "echo '### NET_IFCONFIG begin'"
  hdc shell "ifconfig"
  hdc shell "echo '### NET_IFCONFIG end'"

  hdc shell "echo '### NET_ROUTE begin'"
  hdc shell "route"
  hdc shell "echo '### NET_ROUTE end'"

  hdc shell "echo '### NET_GATEWAY begin'"
  hdc shell "ping -c 3 <gateway_ip>"
  hdc shell "echo '### NET_GATEWAY end'"

  hdc shell "echo '### NET_IP begin'"
  hdc shell "ping -c 3 <public_ip>"
  hdc shell "echo '### NET_IP end'"

  hdc shell "echo '### NET_DNS begin'"
  hdc shell "ping -c 3 <dns_host>"
  hdc shell "echo '### NET_DNS end'"

Use actual probe targets from the run configuration.

Do not invent new probe targets without explaining why.

---

## 7) Probe execution failure indicators

The following output indicates probe execution may be broken:

- /bin/sh:
- syntax error
- unmatched
- inaccessible
- Unknown command
- not found
- Permission denied
- toybox: Unknown command

If any required probe contains these errors:

1. Do not treat the probe as valid fault evidence.
2. Mark the run as FAIL or UNCERTAIN according to the validator policy.
3. Recommend fixing quoting or command compatibility.
4. Do not continue to accepted training data unless the broken probe is irrelevant and this is explicitly justified.

---

## 8) Destructive command policy

Be extremely careful with destructive board commands.

Examples of destructive commands:

- rm
- rm -r
- rm -rf
- mv over existing path
- chmod/chown over broad path
- kill/killall
- reboot
- param set
- service stop
- network interface down commands

Do not run destructive commands unless:

1. the task explicitly requires it
2. the target path or process is narrowly scoped
3. the command is guarded
4. the final output reports what was changed

For cleanup commands, never allow unsafe paths such as:

- empty string
- /
- /data
- /system
- /vendor
- /etc
- /bin
- /usr
- /tmp

For deploy scripts, prefer project-specific directories under /data, for example:

  /data/local/tmp/<project-specific-name>

Before running a cleanup command, log the target path.

Example safe style:

  Refusing to clean unsafe DeviceDir: <path>

or:

  Cleaning safe device directory: <path>

Do not use broad cleanup patterns unless the path has passed a safety guard.

---

## 9) File transfer rules

For receiving files:

  hdc file recv "<remote_path>" "<local_path>"

For sending files:

  hdc file send "<local_path>" "<remote_path>"

Before receiving:

- ensure local destination directory exists
- avoid overwriting important local files unless intended

Before sending:

- ensure the remote directory is safe
- avoid sending to /system or other protected paths unless explicitly requested

For project run bundles:

- preserve original artifacts
- do not rewrite raw evidence
- avoid deleting board-side logs until they have been copied and verified

---

## 10) Run window and evidence rules

For fault data collection, evidence must align with _run_meta.json run_window.

Probe marker timestamps and artifact timestamps should be inside the run window, with only small tolerance when allowed by project validators.

Do not treat post-run baseline outside the run window as a replacement for in-window recovery evidence.

A post-run baseline can be diagnostic context, but not automatic training evidence.

---

## 11) NET-specific reminders

For NET tasks, always distinguish:

- GT subtype
- observed symptoms
- primary evidence
- secondary evidence
- recovery gate result

Do not relabel a run only because a downstream symptom appears.

Examples:

- DNS failure during link_down is usually secondary.
- DNS failure during gateway failure is usually secondary.
- link_flap requires down-to-up transition.
- recovery_observed and recovery_gate_ok are not the same field.

If the task is NET-related, read:

  faults/net/AGENTS.md

---

## 12) Deployment script cautions

The hdc-kaihongos-windows skill may include scripts such as:

  scripts/deploy.ps1

These scripts are for explicit deployment workflows.

Do not use deployment scripts as the default mechanism for fault collection or run review.

When modifying deploy scripts:

1. preserve existing parameters
2. add safety guards instead of broad rewrites
3. protect destructive cleanup paths
4. keep hdc shell commands short
5. avoid adding unsupported board commands
6. run PowerShell syntax checks when possible
7. report remaining risks

---

## 13) Review checklist

When reviewing an HDC-related patch, check:

1. Does it use awk/tr on the board?
2. Does it rely on unverified board tools?
3. Does it use fragile PowerShell quoting?
4. Does it inject raw variables into hdc shell commands?
5. Are probe markers visible?
6. Are destructive commands guarded?
7. Are file transfer paths explicit?
8. Are run-window evidence rules preserved?
9. Are NET subtype semantics preserved if NET-related?
10. Is validation sufficient?

---

## 14) Output format for HDC reviews

Use this format:

  RESULT: PASS / UNCERTAIN / FAIL
  Scope:
  HDC commands reviewed:
  Board command compatibility:
  PowerShell quoting:
  Destructive command safety:
  Probe marker quality:
  Run window impact:
  NET semantics impact:
  Validation:
  Failed criteria:
  Warnings:
  Next fix: