#!/usr/bin/env python3

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", help="path to bundle_*.tar.gz")
    ap.add_argument("--run_dir", help="path to run directory")
    ap.add_argument("--out_root", default="server_B/storage/runs")
    return ap.parse_args()


def run_ingest(bundle: Path, out_root: Path) -> Path:
    script = Path(__file__).resolve().parents[1] / "ingest" / "ingest_bundle.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle), "--out_root", str(out_root)]
    result = subprocess.check_output(cmd, text=True).strip().splitlines()
    if not result:
        raise RuntimeError("ingest_bundle.py returned empty output")
    return Path(result[-1]).resolve()


def run_infer(run_dir: Path) -> Path:
    out_dir = run_dir / "_server_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not env.get("WK_QWEN3_ENABLE_STAGE2"):
        env["WK_QWEN3_ENABLE_STAGE2"] = "0"

    script = Path(__file__).resolve().parents[2] / "closed_loop_infer_run.py"
    cmd = [sys.executable, str(script), "--run_dir", str(run_dir), "--out_dir", str(out_dir)]
    rc = subprocess.call(cmd, env=env)
    if rc != 0:
        raise RuntimeError(f"closed_loop_infer_run.py failed rc={rc}")
    return out_dir


def write_actions_device(actions_json: Path, out_path: Path) -> int:
    if not actions_json.exists():
        out_path.write_text("", encoding="utf-8")
        return 0

    data = json.loads(actions_json.read_text(encoding="utf-8"))
    actions: List[dict] = data.get("actions") or []
    lines: List[str] = []
    for action in actions:
        cmd = action.get("cmd")
        if cmd:
            lines.append(str(cmd))

    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def main() -> None:
    args = parse_args()
    if not args.bundle and not args.run_dir:
        raise SystemExit("--bundle or --run_dir required")

    out_root = Path(args.out_root).expanduser().resolve()
    if args.bundle:
        run_dir = run_ingest(Path(args.bundle).expanduser().resolve(), out_root)
    else:
        run_dir = Path(args.run_dir).expanduser().resolve()

    out_dir = run_infer(run_dir)
    actions_path = out_dir / "actions.json"
    actions_device_path = out_dir / "actions_device.txt"
    count = write_actions_device(actions_path, actions_device_path)

    print(f"run_dir={run_dir}")
    print(f"out_dir={out_dir}")
    print(f"actions_device={actions_device_path} count={count}")


if __name__ == "__main__":
    main()
