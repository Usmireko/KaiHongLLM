---
name: stage2_ingest_integrity_guard
description: >
  Prevent ingest server from writing <file>.done=ok for truncated/corrupt uploads.
  Either verify received bytes == expected LEN (if protocol has LEN), or validate tarfile opens before marking ok.
---

# Goal
Avoid tarfile.ReadError in watcher caused by ingest marking incomplete bundle as done.

# Target file
- server_B/tcp/tcp_ingest_server.py

# Requirements (choose safest available)
Option A (preferred if protocol provides expected length):
- Track expected_len from request header.
- Only finalize dest + write dest.done("ok") if bytes_received == expected_len.
- Otherwise write dest.done("error:short_read ...") and keep file as dest.partial or delete dest.

Option B (no expected length available):
- After os.replace(tmp, dest), attempt:
  - import tarfile
  - tarfile.open(dest, "r:*").getmembers() (or a cheap open/close)
- If tarfile validation fails:
  - delete dest (or rename to dest.bad)
  - write dest.done("error:bad_tar ...")
- Only on success write dest.done("ok")

# Watcher compatibility
Ensure watcher treats .done as READY, but it should only proceed for done content starting with "ok".
If watcher currently does not parse done content, also patch watcher accordingly (minimal).

# Acceptance
Simulate truncated upload:
- Create a fake bundle file and upload only first N bytes (or interrupt upload).
- Server must NOT produce done="ok".
- Watcher must not attempt inference for that run_id.
