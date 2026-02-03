#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="/home/xrh/qwen3_os_fault/storage/inbox_bundles")
    ap.add_argument("--out", default="/home/xrh/qwen3_os_fault/storage/out")
    ap.add_argument("--runs_root", default="/home/xrh/qwen3_os_fault/storage/runs")
    ap.add_argument("--poll_sec", type=int, default=5)
    return ap.parse_args()


def read_device_id(run_dir: Path) -> str:
    meta_path = run_dir / "_run_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return meta.get("device_sn") or meta.get("device_id") or "unknown_device"
        except Exception:
            return "unknown_device"
    return "unknown_device"


def ingest_bundle(bundle: Path, runs_root: Path) -> Path:
    script = Path(__file__).resolve().parents[1] / "ingest" / "ingest_bundle.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle), "--out_root", str(runs_root)]
    out = subprocess.check_output(cmd, text=True).strip().splitlines()
    if not out:
        raise RuntimeError("ingest_bundle.py returned empty output")
    return Path(out[-1]).resolve()

def infer_run_id(bundle: Path) -> Optional[str]:
    name = bundle.name
    if name.startswith("bundle_") and name.endswith(".tar.gz"):
        return name[len("bundle_") : -len(".tar.gz")]
    return None


def run_closed_loop(run_dir: Path) -> Path:
    script = Path(__file__).resolve().parent / "run_closed_loop.py"
    cmd = [sys.executable, str(script), "--run_dir", str(run_dir)]
    subprocess.check_call(cmd)
    return run_dir / "_server_out"


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def maybe_process_bundle(bundle: Path, inbox: Path, out_root: Path, runs_root: Path) -> None:
    if bundle.name.endswith(".tmp"):
        return
    if not bundle.name.endswith(".tar.gz"):
        return
    done_mark = Path(str(bundle) + ".done")
    if done_mark.exists():
        return

    run_id = infer_run_id(bundle)
    if run_id and (runs_root / run_id).exists():
        run_dir = runs_root / run_id
    else:
        run_dir = ingest_bundle(bundle, runs_root)
    out_dir = run_closed_loop(run_dir)
    actions_device = out_dir / "actions_device.txt"
    if not actions_device.exists():
        raise FileNotFoundError(f"actions_device.txt missing: {actions_device}")

    device_id = read_device_id(run_dir)
    run_id = run_dir.name
    dest_dir = out_root / device_id / run_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / "actions_device.txt"
    if dest_path.exists():
        done_mark.write_text("ok\n", encoding="utf-8")
        return

    atomic_write(dest_path, actions_device.read_text(encoding="utf-8"))
    done_mark.write_text("ok\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    inbox = Path(args.inbox)
    out_root = Path(args.out)
    runs_root = Path(args.runs_root)

    inbox.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    while True:
        for bundle in sorted(inbox.glob("*.tar.gz")):
            try:
                maybe_process_bundle(bundle, inbox, out_root, runs_root)
            except Exception as exc:
                print(f"[watch_inbox] bundle={bundle} error={exc}", file=sys.stderr)
        time.sleep(max(1, int(args.poll_sec)))


if __name__ == "__main__":
    main()
