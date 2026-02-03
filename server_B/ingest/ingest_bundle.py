#!/usr/bin/env python3

import argparse
import json
import tarfile
import tempfile
import shutil
from pathlib import Path
from typing import Any, Optional


def safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/"):
            raise ValueError(f"unsafe path in tar: {name}")
        parts = Path(name).parts
        if any(part == ".." for part in parts):
            raise ValueError(f"unsafe path in tar: {name}")
    tar.extractall(dest)


def find_root_dir(extract_dir: Path) -> Path:
    entries = [p for p in extract_dir.iterdir() if p.name not in ("__MACOSX",)]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extract_dir


def load_manifest(root_dir: Path) -> dict:
    for name in ("bundle_manifest.json", "manifest.json"):
        path = root_dir / name
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return {}
    return {}


def ensure_required(run_dir: Path) -> None:
    missing = []
    metrics_dir = run_dir / "metrics"
    if not metrics_dir.exists() or not list(metrics_dir.glob("sys_*.csv")):
        missing.append("metrics/sys_*.csv")

    events_dir = run_dir / "events"
    if not events_dir.exists() or not list(events_dir.glob("events_*.jsonl")):
        missing.append("events/events_*.jsonl")

    procs_dir = run_dir / "procs"
    if not procs_dir.exists() or not list(procs_dir.glob("procs_*.txt")):
        missing.append("procs/procs_*.txt")

    if missing:
        raise FileNotFoundError("missing required files: " + ", ".join(missing))


def patch_run_meta(meta_path: Path, run_dir: Path, run_id: str, manifest: dict) -> None:
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    start_ms = safe_int(meta.get("run_window_host_epoch_ms_start"))
    end_ms = safe_int(meta.get("run_window_host_epoch_ms_end"))
    if not start_ms:
        start_ms = safe_int(meta.get("run_window_board_ms_start"))
    if not end_ms:
        end_ms = safe_int(meta.get("run_window_board_ms_end"))

    if not start_ms:
        start_ms = safe_int(manifest.get("window_start_ms"))
    if not end_ms:
        end_ms = safe_int(manifest.get("window_end_ms"))

    if not start_ms:
        start_ms = safe_int(meta.get("run_start"))
    if not end_ms:
        end_ms = safe_int(meta.get("run_end"))

    meta.setdefault("run_id", run_id)
    meta.setdefault("scenario_tag", manifest.get("scenario_tag", "demo_manual"))
    meta.setdefault("fault_type", manifest.get("fault_type", "manual"))
    if start_ms is not None:
        meta.setdefault("run_start", start_ms)
    if end_ms is not None:
        meta.setdefault("run_end", end_ms)
    meta.setdefault("run_window_host_epoch_ms_start", start_ms or 0)
    meta.setdefault("run_window_host_epoch_ms_end", end_ms or 0)
    meta.setdefault("run_window_board_ms_start", start_ms or 0)
    meta.setdefault("run_window_board_ms_end", end_ms or 0)
    meta.setdefault("run_window_source", manifest.get("run_window_source", "manual"))
    meta["run_dir"] = str(run_dir)

    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=True), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="path to bundle_*.tar.gz")
    ap.add_argument("--out_root", default="server_B/storage/runs", help="output root for runs")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    bundle_path = Path(args.bundle).expanduser().resolve()
    out_root = Path(args.out_root).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if not bundle_path.exists():
        raise FileNotFoundError(f"bundle not found: {bundle_path}")

    with tempfile.TemporaryDirectory() as td:
        extract_dir = Path(td)
        with tarfile.open(bundle_path, "r:*") as tar:
            safe_extract(tar, extract_dir)

        root_dir = find_root_dir(extract_dir)
        manifest = load_manifest(root_dir)

        run_id = manifest.get("run_id")
        if not run_id:
            if root_dir != extract_dir:
                run_id = root_dir.name
            else:
                name = bundle_path.name
                run_id = name.replace("bundle_", "").replace(".tar.gz", "").replace(".tgz", "")

        if not run_id:
            raise ValueError("unable to determine run_id")

        run_dir = out_root / run_id
        if run_dir.exists():
            raise FileExistsError(f"run_dir exists: {run_dir}")

        shutil.copytree(root_dir, run_dir)
        ensure_required(run_dir)
        patch_run_meta(run_dir / "_run_meta.json", run_dir, run_id, manifest)

    print(str(run_dir))


if __name__ == "__main__":
    main()
