# Stage2 TCP Transport Protocol (nc)

This protocol is for the Stage2 demo only. It is plaintext TCP over a trusted LAN.
Do NOT expose these ports to public networks.

## Ports

- Ingest: TCP 18080 (board -> server)
- Actions: TCP 18081 (board -> server request, server -> board response)

## Ingest protocol (port 18080)

Board sends header + payload:

TYPE=<bundle|action_result>
DEVICE=<device_id>
RUN=<run_id>
LEN=<decimal_bytes>

<LEN bytes binary payload>

Server writes to:
- /home/xrh/qwen3_os_fault/storage/tcp_inbox/<device_id>/<run_id>__<type>.tar.gz
- Atomic write: .tmp then rename

## Actions protocol (port 18081)

Board sends request:

DEVICE=<device_id>

Server replies:

RUN=<run_id>
LEN=<decimal_bytes>

<LEN bytes actions_device.txt>

If no actions exist, LEN=0 and body is empty.

## Server dirs

- Inbox: /home/xrh/qwen3_os_fault/storage/tcp_inbox/<device_id>/
- Runs: /home/xrh/qwen3_os_fault/storage/runs/<run_id>/
- Actions out: /home/xrh/qwen3_os_fault/storage/tcp_out/<device_id>/latest_actions_device.txt
- Latest run: /home/xrh/qwen3_os_fault/storage/tcp_out/<device_id>/latest_run_id.txt

## Atomic write rules

- Board uploader writes to .tmp on server and renames.
- Server writes latest_actions_device.txt and latest_run_id.txt via .tmp -> rename.

## Demo safety notes

- Plaintext only; avoid sensitive data in payloads.
- Restrict access to the LAN or a dedicated VLAN.
- Prefer firewall allowlist for ports 18080/18081.
