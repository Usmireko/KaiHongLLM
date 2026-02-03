#!/usr/bin/env python3

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple


def sanitize_token(val: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", val.strip()) or "unknown"


def parse_name(name: str) -> Tuple[str, str]:
    base = name
    if base.endswith(".tar.gz"):
        base = base[:-7]
    elif "." in base:
        base = base.rsplit(".", 1)[0]

    if "__" not in base:
        return "", ""

    run_id, kind = base.split("__", 1)
    return sanitize_token(run_id), sanitize_token(kind)


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def run_closed_loop(bundle_path: Path, repo_root: Path) -> None:
    script = repo_root / "server_B" / "orchestrator" / "run_closed_loop.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle_path)]
    subprocess.check_call(cmd)


def find_actions(run_dir: Path) -> Optional[Path]:
    actions = run_dir / "_server_out" / "actions_device.txt"
    return actions if actions.exists() else None


def process_bundle(path: Path, device_id: str, run_id: str, inbox_root: Path, out_root: Path, runs_root: Path, repo_root: Path) -> None:
    run_closed_loop(path, repo_root)

    run_dir = runs_root / run_id
    actions_path = find_actions(run_dir)
    if not actions_path:
        raise FileNotFoundError(f"actions_device.txt missing for run_id={run_id}")

    device_dir = out_root / device_id
    device_dir.mkdir(parents=True, exist_ok=True)

    atomic_write(device_dir / "latest_actions_device.txt", actions_path.read_bytes())
    atomic_write(device_dir / "latest_run_id.txt", (run_id + "\n").encode("utf-8"))


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="/home/xrh/qwen3_os_fault/storage/tcp_inbox")
    ap.add_argument("--out", default="/home/xrh/qwen3_os_fault/storage/tcp_out")
    ap.add_argument("--runs_root", default="/home/xrh/qwen3_os_fault/storage/runs")
    ap.add_argument("--poll_sec", type=int, default=5)
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    inbox_root = Path(args.inbox)
    out_root = Path(args.out)
    runs_root = Path(args.runs_root)
    repo_root = Path(__file__).resolve().parents[2]

    inbox_root.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    while True:
        for device_dir in sorted(inbox_root.glob("*")):
            if not device_dir.is_dir():
                continue
            device_id = sanitize_token(device_dir.name)

            for item in sorted(device_dir.iterdir()):
                if item.suffix == ".tmp" or item.name.endswith(".done"):
                    continue
                if not item.is_file():
                    continue

                done_mark = Path(str(item) + ".done")
                if done_mark.exists():
                    continue

                run_id, kind = parse_name(item.name)
                if not run_id or not kind:
                    done_mark.write_text("skip_bad_name\n", encoding="utf-8")
                    continue

                try:
                    if kind == "bundle":
                        process_bundle(item, device_id, run_id, inbox_root, out_root, runs_root, repo_root)
                    elif kind == "action_result":
                        # demo: only record
                        pass
                    else:
                        pass

                    done_mark.write_text("ok\n", encoding="utf-8")
                except Exception as exc:
                    done_mark.write_text(f"error:{exc}\n", encoding="utf-8")
        time.sleep(max(1, int(args.poll_sec)))


if __name__ == "__main__":
    main()
