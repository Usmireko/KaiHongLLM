# faults/net/AGENTS.md — NET Fault Rules

This file defines NET-family fault semantics and review rules.

Use this file together with:

- global AGENTS.md
- .codex/skills/board-net-collect-and-analyze/SKILL.md
- .codex/skills/board-net-review-run/SKILL.md
- .codex/skills/dataset-l1-l2-validation/SKILL.md

The global AGENTS.md still controls project-wide constraints such as:

- no board-side awk / tr
- no numbered net_fault file names
- anti evidence-gaming
- run window alignment
- GT / OBS separation
- PASS / UNCERTAIN / FAIL policy
- maximum repair depth 3

---

## 1) NET family scope

The NET fault family covers network connectivity, name resolution, route, interface, gateway, packet quality, and authentication-related failures.

Known or planned NET subtypes include:

- net_dns_fail
- net_link_down
- net_link_flap
- net_gateway_unreachable
- net_packet_loss
- net_latency_spike
- net_auth_fail
- other explicitly added NET subtypes

Do not create a new NET subtype without checking:

1. injector behavior
2. collection behavior
3. _net_outcome.json
4. _run_meta.json
5. L1 export
6. evidence candidates
7. L2 derivation
8. validators

---

## 2) Core NET artifacts

Important run artifacts:

- _run_meta.json
- _net_outcome.json
- probe_fault.txt
- probe_post.txt
- probe_post2.txt

Possible additional artifacts:

- net_*.txt
- route*.txt
- ifconfig*.txt
- dns*.txt
- ping*.txt
- logs*.txt

Important derived outputs:

- canonical_case.json
- evidence_candidates.jsonl
- diagnosis.jsonl
- evidence_extraction.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl

---

## 3) Required _net_outcome.json concepts

When available, review these fields:

- net_fault_type
- iface_used
- inject_ok
- fault_observed
- recovery_observed
- fault_observation_reason
- recovery_observation_reason
- recovery_gate_ok
- recovery_gate_reason

Interpretation:

- net_fault_type should align with GT subtype.
- inject_ok means the injector action was attempted and considered successful.
- fault_observed means probes or logs show fault effect.
- recovery_observed means probes or logs show recovery effect.
- recovery_gate_ok means the final recovery gate passed.
- recovery_gate_reason explains why the recovery gate passed or failed.

Do not collapse recovery_observed and recovery_gate_ok.

A run may show:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false

This means the fault/recovery chain may have evidence, but the final gate still blocks acceptance.

Do not treat this as injector failure without checking probe epochs and baseline.

---

## 4) GT / OBS for NET faults

GT is the intended injected NET subtype.

OBS is the observed evidence:

- DNS probe result
- IP ping result
- gateway ping result
- route state
- interface state
- link transition
- packet loss
- latency
- authentication failure symptoms
- recovery gate behavior

OBS must not replace GT.

If GT is net_auth_fail but OBS includes DNS failure, do not relabel the case as net_dns_fail unless an explicit relabeling workflow is requested.

Concurrent symptoms should usually be secondary evidence unless they directly match GT and the probe chain supports them.

---

## 5) Common NET evidence review order

For every NET run review, inspect evidence in this order:

1. _run_meta.json
   - run_id
   - run_window
   - GT family
   - GT subtype
   - declared fault parameters

2. _net_outcome.json
   - net_fault_type
   - inject_ok
   - fault_observed
   - recovery_observed
   - recovery_gate_ok
   - reasons

3. Probe files
   - fault-phase probe
   - post-phase probe
   - post2 or delayed recovery probe if present

4. Timing
   - fault-phase probe timestamps inside run window
   - recovery/post timestamps inside the NET recovery/post-window policy when applicable
   - injection timestamp
   - recovery timestamp

5. Baseline
   - pre-run baseline if available
   - post-run baseline if available

6. L1 / L2 readiness
   - canonical case can preserve GT / OBS
   - evidence candidates can distinguish primary / secondary
   - validators can reproduce the decision

---

## 6) NET run_window / recovery timing policy

This section is the authoritative NET-family timing policy for review and audit.

For non-DNS NET runs, `_run_meta.json.run_window` means the fault/main collection window, not the complete artifact lifecycle.

Current non-DNS NET runs include:

- net_public_ip_unreachable
- net_no_default_route
- net_no_ipv4_on_iface
- net_wrong_default_route
- net_wifi_disconnect
- net_wifi_auth_fail_wrong_psk

Timing interpretation:

- `probe_pre` may occur before `run_window_start`.
- `probe_fault` should occur inside, or causally correspond to, the fault/main collection window.
- `Stop-FaultInject`, `Wait-WifiRecovery`, `recovery_gate`, `probe_post`, and `probe_post2` may occur after `run_window_end`.
- A run is not invalid merely because `probe_post` or `probe_post2` is after `_run_meta.json.run_window`.
- For timing audit, post/post2 after `run_window_end` must be recorded as recovery/post-window evidence, not as a run_window violation.

Accepted eligibility for non-DNS NET runs requires:

- validator `RESULT=PASS`
- `fault_observed=true`
- `recovery_gate_ok=true`
- `recovery_gate_reason=all_conditions_met` or an explicitly documented subtype-equivalent success reason
- post baseline PASS
- residual cleanup PASS
- evidence consistency across `probe_pre`, `probe_fault`, `probe_post`, and `probe_post2` when present
- no fixcheck-only, rejected, dry-run, or quarantine marker

Recovery/post-window evidence must support the same GT subtype and must not silently relabel GT from observed symptoms. If a subtype has different timing semantics, document it in that subtype section before accepting a run under that rule.

`net_dns_fail` has different timing behavior in the current runner because the run window may be deferred through recovery/post snapshots. Review DNS runs against the DNS-specific evidence rules and the actual `_run_meta.json.run_window`.

---

## 7) NET subtype boundaries

### 7.1 net_dns_fail

Primary DNS failure evidence should show:

- DNS host resolution fails
- IP connectivity remains OK or mostly OK
- gateway may remain OK
- failure is specific to name resolution

Good evidence patterns:

- dns_host_probe_failed_while_ip_probe_still_ok
- dns_resolution_failed
- host lookup failed

Do not mark DNS failure as primary if:

- link is down
- gateway is unreachable
- all IP pings fail
- interface is unavailable
- DNS failure appears only as a secondary symptom

Allowed secondary evidence:

- DNS error during link down
- DNS error during gateway unreachable
- DNS error during auth failure

But secondary DNS evidence should not override GT.

---

### 7.2 net_link_down

Primary link-down evidence should show:

- interface/link becomes unavailable
- route path is disrupted because link is down
- IP/gateway/DNS probes fail as a consequence
- recovery shows link or connectivity restored

Do not mark link down as link flap unless a down-to-up transition is observed.

A single static failure is not enough for net_link_flap.

---

### 7.3 net_link_flap

Primary link-flap evidence requires a transition.

Expected pattern:

  up -> down -> up

or:

  connected -> disconnected -> connected

Valid evidence can include:

- interface state transition
- route disappearance and restoration
- ping failure and recovery during repeated flap cycle
- explicit injector flap markers

Do not infer flap from only:

- one failed ping
- one DNS failure
- one static link-down state
- recovery after a single hold period

---

### 6.4 net_gateway_unreachable

Primary gateway-unreachable evidence should show:

- link or interface may remain up
- IP address may remain assigned
- default route or gateway path fails
- gateway ping fails
- DNS and public IP failures may appear as downstream symptoms

Do not classify as DNS failure merely because DNS fails when gateway is unreachable.

Do not classify as link down if interface remains available and the key failure is gateway reachability.

---

### 6.5 net_packet_loss

Primary packet-loss evidence should show:

- partial packet loss
- intermittent ping failures
- link remains generally available
- route remains generally available
- DNS may be noisy but not the root cause

Evidence should include counts or before/after delta where possible.

Do not use arbitrary counters as primary evidence unless they are tied to the fault window.

---

### 6.6 net_latency_spike

Primary latency-spike evidence should show:

- increased RTT
- link remains available
- route remains available
- packet loss may be low or absent
- baseline RTT is meaningfully lower than fault-window RTT

Use deltas near the fault window.

Do not mark as latency spike solely because one ping is slow.

---

### 6.7 net_auth_fail

Primary auth-fail evidence should show:

- authentication or access-control related failure
- connection may appear associated but traffic is blocked or restricted
- recovery may require re-auth, reconnect, or access restoration
- final recovery gate may fail for an unrelated downstream probe

Important rule:

A run can be meaningful even if:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false
  recovery_gate_reason=ping_ip_fail

In this case:

- do not immediately call the injection failed
- inspect probe epochs
- inspect post2 recovery
- inspect final baseline
- decide accepted / not accepted based on the evidence chain

If final baseline passes but recovery gate failed inside the run, the run may remain not accepted for training while still being useful for debugging.

---

## 7) Recovery gate interpretation

recovery_gate_ok=false blocks automatic acceptance unless the task explicitly allows manual override.

Review recovery_gate_reason.

Common reasons may include:

- ping_ip_fail
- dns_fail
- gateway_fail
- link_not_recovered
- probe_missing
- probe_syntax_error

Rules:

- If recovery gate failed because a required probe failed, mark the run not accepted unless a fault-specific rule says otherwise.
- If post-run baseline later passes, report the discrepancy.
- Do not erase the failed in-window recovery gate.
- Do not treat post-run baseline as a substitute for in-window evidence unless explicitly requested.

---

## 8) Keep / drop / uncertain policy for NET

### KEEP / accepted

Use KEEP or accepted only if:

- GT subtype is clear
- injection was successful
- fault effect was observed
- recovery effect was observed when required
- recovery gate passes or manual rule allows exception
- required fault/main evidence is inside `_run_meta.json.run_window`
- recovery gate, `probe_post`, and `probe_post2` may be recovery/post-window evidence when allowed by the NET run_window / recovery timing policy above
- no shell/probe syntax error invalidates evidence
- subtype boundary is not ambiguous

### UNCERTAIN

Use UNCERTAIN if:

- evidence exists but is weak
- recovery is partially observed
- final baseline conflicts with in-window gate
- some probe files are missing but core effect exists
- subtype is plausible but secondary symptoms are strong

### DROP / not accepted

Use DROP or not accepted if:

- required files are missing
- injector failed
- fault effect not observed
- required fault/main evidence outside run window, except recovery/post-window evidence allowed by section 6
- shell/probe execution broken
- subtype boundary cannot be resolved
- recovery gate failure blocks training acceptance
- GT / OBS would be conflated by accepting

---

## 9) Evidence candidate roles

When producing or reviewing evidence_candidates.jsonl, use:

- primary: evidence directly supporting GT subtype
- secondary: relevant symptoms or downstream effects
- context: environment, baseline, or supporting context
- negative: evidence against a subtype or against acceptance

Examples:

- DNS failure with IP OK for net_dns_fail: primary
- DNS failure during link down: secondary
- route table before fault: context
- successful IP ping during alleged full link down: negative or boundary evidence

---

## 10) NET review output format

For NET run review, use:

  RESULT: PASS / UNCERTAIN / FAIL
  Decision: keep / drop / not accepted / needs rerun
  GT:
  OBS summary:
  Fault observed:
  Recovery observed:
  Recovery gate:
  Subtype boundary:
  Run window:
  Failed criteria:
  Warnings:
  Next fix:
  L1/L2 recommendation:

Keep output concise and actionable.
