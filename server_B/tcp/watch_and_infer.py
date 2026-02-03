#!/usr/bin/env python3
import argparse
import json
import os
import socket
import sys
import time
import subprocess
import tarfile
from pathlib import Path

MAX_ERROR_BYTES = 2048
def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sanitize_token(val: str) -> str:
    out = []
    for ch in (val or ""):
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
    s = "".join(out)
    return s if s else "unknown"

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)

def unlink_if_exists(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def parse_name(filename: str):
    if filename.endswith("__bundle.tar.gz"):
        return filename[:-len("__bundle.tar.gz")], "bundle"
    if filename.endswith("__action_result.tar.gz"):
        return filename[:-len("__action_result.tar.gz")], "action_result"
    return None, None

def safe_read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""

def write_latest(out_root: Path, device_id: str, run_id: str, actions_data: bytes) -> None:
    dev = out_root / device_id
    atomic_write(dev / "latest_actions_device.txt", actions_data)
    atomic_write(dev / "latest_run_id.txt", (run_id + "\n").encode("utf-8"))

def write_status(out_root: Path, device_id: str, status: str) -> None:
    dev = out_root / device_id
    atomic_write(dev / "latest_infer_status.txt", (status + "\n").encode("utf-8"))

def write_error(out_root: Path, device_id: str, reason: str) -> None:
    dev = out_root / device_id
    payload = (reason or "error").encode("utf-8", errors="ignore")
    atomic_write(dev / "latest_error.txt", payload[:MAX_ERROR_BYTES])

def clear_error(out_root: Path, device_id: str) -> None:
    p = out_root / device_id / "latest_error.txt"
    if p.exists():
        try:
            p.unlink()
        except Exception:
            atomic_write(p, b"")

def parse_run_dir(stdout: str):
    for line in (stdout or "").splitlines():
        if line.startswith("run_dir="):
            return line.split("=", 1)[1].strip()
    return None

def run_closed_loop(repo_root: Path, bundle_path: Path, runs_root: Path):
    script = repo_root / "server_B" / "orchestrator" / "run_closed_loop.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle_path), "--out_root", str(runs_root)]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError("run_closed_loop rc=%s stderr=%s" % (proc.returncode, stderr[:1024]))
    rd = parse_run_dir(proc.stdout or "")
    return Path(rd).expanduser().resolve() if rd else None

def find_actions(run_dir: Path):
    p = run_dir / "_server_out" / "actions_device.txt"
    if p.exists():
        return p
    p = run_dir / "actions_device.txt"
    if p.exists():
        return p
    return None

def mark_infer(item: Path, status: str, device_id: str = "", run_id: str = "", extra: dict = None):
    payload = {
        "ts_utc": now_utc(),
        "status": status,
    }
    if device_id:
        payload["device_id"] = device_id
    if run_id:
        payload["run_id"] = run_id
    if extra:
        for k, v in extra.items():
            payload[k] = v
    Path(str(item) + ".infer_done").write_text(
        json.dumps(payload, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

def mark_server_out_infer_done(run_dir: Path, status: str) -> None:
    """Write _server_out/.infer_done so demo_stage2.ps1 can reliably detect Stage2 completion.

    We keep this marker separate from the inbox-side *.infer_done used for dedup/cleanup.
    """
    try:
        out_dir = Path(run_dir) / "_server_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / ".infer_done").write_text(f"{status}\n", encoding="utf-8")
    except Exception:
        pass


def cleanup_inbox_item(item: Path, delete_infer_done: bool = False) -> None:
    done = Path(str(item) + ".done")
    infer = Path(str(item) + ".infer_done")
    unlink_if_exists(item)
    unlink_if_exists(done)
    if delete_infer_done:
        unlink_if_exists(infer)

def safe_extract_tar(tar: tarfile.TarFile, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    members = []
    for m in tar.getmembers():
        name = (m.name or "").lstrip("/")
        if not name:
            continue
        # basic path traversal guard
        parts = Path(name).parts
        if ".." in parts:
            continue
        # normalize leading "./"
        if name.startswith("./"):
            name = name[2:]
        m.name = name
        members.append(m)
    tar.extractall(dst, members=members)

def unpack_action_result(tar_path: Path, run_dir: Path) -> Path:
    out_dir = run_dir / "_action_result"
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".unpack_done"
    if marker.exists():
        return out_dir
    with tarfile.open(tar_path, "r:*") as tar:
        safe_extract_tar(tar, out_dir)
    atomic_write(marker, (now_utc() + "\n").encode("utf-8"))
    return out_dir

def cleanup_processed_marks(device_dir: Path, keep_max: int, keep_days: int) -> None:
    marks = []
    for p in device_dir.glob("*.infer_done"):
        try:
            marks.append((p.stat().st_mtime, p))
        except Exception:
            continue
    if not marks:
        return
    marks.sort(key=lambda x: x[0], reverse=True)
    cutoff = None
    if keep_days > 0:
        cutoff = time.time() - (keep_days * 86400)
    for idx, (mtime, mark) in enumerate(marks):
        over = (keep_max > 0 and idx >= keep_max)
        old = (cutoff is not None and mtime < cutoff)
        if not (over or old):
            continue
        base = Path(str(mark)[:-len(".infer_done")])  # -> *.tar.gz
        unlink_if_exists(base)
        unlink_if_exists(Path(str(base) + ".done"))
        unlink_if_exists(mark)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs_root", required=True)
    ap.add_argument("--poll_sec", type=int, default=2)
    args = ap.parse_args()

    inbox_root = Path(args.inbox).expanduser().resolve()
    out_root = Path(args.out).expanduser().resolve()
    runs_root = Path(args.runs_root).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]

    inbox_root.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    print("[watcher] inbox=%s out=%s runs=%s" % (inbox_root, out_root, runs_root), flush=True)

    keep_max = int(os.environ.get("WK_TCP_INBOX_KEEP_MAX", "200"))
    keep_days = int(os.environ.get("WK_TCP_INBOX_KEEP_DAYS", "7"))
    cleanup_every = int(os.environ.get("WK_TCP_INBOX_CLEANUP_EVERY", "10"))
    delete_infer_done = (os.environ.get("WK_TCP_INBOX_DELETE_INFER_DONE", "0").strip() == "1")
    loop_n = 0

    while True:
        try:
            for device_dir in sorted(inbox_root.iterdir()):
                if not device_dir.is_dir():
                    continue
                device_id = sanitize_token(device_dir.name)

                # A) 预扫描：done!=ok 的文件，直接 infer_done=skip_bad_bundle，避免反复尝试
                for done_item in sorted(device_dir.glob("*.done")):
                    base = done_item.with_suffix("")  # remove ".done"
                    infer_mark = Path(str(base) + ".infer_done")
                    if infer_mark.exists():
                        continue
                    done_txt = safe_read_text(done_item)
                    if done_txt.lstrip().startswith("ok"):
                        continue
                    run_id, kind = parse_name(base.name)
                    if not run_id or not kind:
                        mark_infer(base, "skip_bad_name", device_id=device_id, run_id="")
                    else:
                        mark_infer(base, "skip_bad_bundle", device_id=device_id, run_id=run_id, extra={"done_text": done_txt[:200]})
                    # done!=ok: avoid inbox growth (delete tar + .done; keep .infer_done unless configured)
                    cleanup_inbox_item(base, delete_infer_done=delete_infer_done)

                # B) 只处理“最新的一个 ok bundle”，其余历史 ok bundle 标记 skip_stale，防止旧包覆盖 latest
                bundles = []
                action_results = []
                for item in device_dir.iterdir():
                    if item.name.endswith(".tmp") or item.name.endswith(".done") or item.name.endswith(".infer_done"):
                        continue
                    run_id, kind = parse_name(item.name)
                    if not run_id or not kind:
                        continue
                    ready = Path(str(item) + ".done")
                    infer = Path(str(item) + ".infer_done")
                    if not ready.exists() or infer.exists():
                        continue
                    if not safe_read_text(ready).lstrip().startswith("ok"):
                        mark_infer(item, "skip_bad_bundle", device_id=device_id, run_id=run_id)
                        cleanup_inbox_item(item, delete_infer_done=delete_infer_done)
                        continue
                    if kind == "bundle":
                        bundles.append(item)
                    elif kind == "action_result":
                        action_results.append(item)

                # 先把 action_result 都标记一下（不影响 latest）
                for ar in sorted(action_results, key=lambda p: p.stat().st_mtime):
                    try:
                        run_id, _ = parse_name(ar.name)
                        print("[watcher] action_result ready device=%s run_id=%s" % (device_id, run_id), flush=True)
                        run_dir = runs_root / run_id
                        if not run_dir.exists():
                            # 通常不会发生（action_result 在推理完成后才会出现），但遇到就先不处理，留待下次
                            print("[watcher] action_result wait_run_dir device=%s run_id=%s" % (device_id, run_id), flush=True)
                            continue
                        out_dir = unpack_action_result(ar, run_dir)
                        mark_infer(ar, "ok_action_result_unpacked", device_id=device_id, run_id=run_id, extra={"out_dir": str(out_dir)})
                        cleanup_inbox_item(ar, delete_infer_done=delete_infer_done)
                    except Exception:
                        pass

                if bundles:
                    newest = max(bundles, key=lambda p: p.stat().st_mtime)
                    for b in bundles:
                        if b != newest:
                            try:
                                rid, _ = parse_name(b.name)
                                mark_infer(b, "skip_stale", device_id=device_id, run_id=rid)
                                cleanup_inbox_item(b, delete_infer_done=delete_infer_done)
                            except Exception:
                                pass

                    run_id, _ = parse_name(newest.name)
                    try:
                        print("[watcher] bundle ready device=%s run_id=%s" % (device_id, run_id), flush=True)
                        run_dir = run_closed_loop(repo_root, newest, runs_root)
                        if not run_dir:
                            run_dir = runs_root / run_id
                        actions_path = find_actions(run_dir)
                        if not actions_path:
                            raise FileNotFoundError("actions_device.txt missing for run_id=%s" % run_id)
                        data = actions_path.read_bytes()
                        if not data.strip():
                            # tolerate empty actions_device.txt: synthesize a minimal default so the pipeline can proceed
                            default_lines = [
                                "dmesg | tail -n 200",
                                "cat /proc/loadavg",
                                "cat /proc/meminfo | head -n 40",
                                "ps -A | head -n 80",
                                "top -n 1 | head -n 80",
                            ]
                            data = ("\n".join(default_lines) + "\n").encode("utf-8")
                            try:
                                actions_path.write_bytes(data)
                            except Exception:
                                pass

                        write_latest(out_root, device_id, run_id, data)
                        write_status(out_root, device_id, "llm_ok")
                        clear_error(out_root, device_id)
                        mark_infer(newest, "ok", device_id=device_id, run_id=run_id, extra={"run_dir": str(run_dir)})
                        mark_server_out_infer_done(run_dir, 'ok')
                        cleanup_inbox_item(newest, delete_infer_done=delete_infer_done)
                        print("[watcher] bundle ok device=%s run_id=%s" % (device_id, run_id), flush=True)
                    except Exception as exc:
                        reason = str(exc)
                        # run_dir exists 这种是历史重复包：不写 fallback，不覆盖 latest，只做 skip
                        if "run_dir exists:" in reason:
                            mark_infer(newest, "skip_exists_run_dir", device_id=device_id, run_id=run_id, extra={"reason": reason[:512]})
                            mark_server_out_infer_done(run_dir, 'skip_exists_run_dir')
                            # 这种重复包也没必要留在 inbox
                            cleanup_inbox_item(newest, delete_infer_done=delete_infer_done)
                            print("[watcher] bundle skip_exists device=%s run_id=%s" % (device_id, run_id), flush=True)
                        else:
                            fallback = ("echo INFER_FAILED device=%s run=%s\n" % (device_id, run_id)).encode("utf-8")
                            write_latest(out_root, device_id, run_id, fallback)
                            write_error(out_root, device_id, reason)
                            write_status(out_root, device_id, "fallback")
                            mark_infer(newest, "error", device_id=device_id, run_id=run_id, extra={"reason": reason[:1024]})
                            mark_server_out_infer_done(run_dir, 'error')
                            print("[watcher] bundle error device=%s run_id=%s reason=%s" % (device_id, run_id, reason), flush=True)

        except Exception as loop_exc:
            print("[watcher] loop_error: %s" % loop_exc, flush=True)
        loop_n += 1
        if cleanup_every > 0 and (loop_n % cleanup_every == 0):
            try:
                for device_dir in sorted(inbox_root.iterdir()):
                    if device_dir.is_dir():
                        cleanup_processed_marks(device_dir, keep_max=keep_max, keep_days=keep_days)
            except Exception:
                pass

        time.sleep(max(1, int(args.poll_sec)))

if __name__ == "__main__":
    main()
