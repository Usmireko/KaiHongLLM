# Closed Loop Demo Runbook

## Dry Run / Preflight

```sh
scripts/start_demo.sh --dry-run
scripts/demo_preflight.sh
```

This checks that the local host, server, and board source entrypoints exist. It
does not contact hardware.

## Server Services

On the server:

```sh
cd /home/xrh/qwen3_os_fault
export QWEN3_OS_FAULT_ROOT=/home/xrh/qwen3_os_fault
bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh restart
bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh status
```

Services:

- ingest: `QWEN3_INGEST_PORT`, default `18080`
- actions: `QWEN3_ACTIONS_PORT`, default `28081`
- watcher: consumes ready uploads and writes latest actions

## Host Start

```sh
scripts/start_demo.sh -Server qwen3-server -RepoDir /home/xrh/qwen3_os_fault -DeviceId dev1
```

The host script deploys the board script bundle from
`closed_loop_demo/board/scripts`.

## Status and Stop

```sh
scripts/demo_status.sh
scripts/stop_demo.sh
```

Without a live server shell context, these scripts print the exact server-side
commands to run.
