#!/usr/bin/env python3

import argparse
import os
import re
import socket
import threading
from pathlib import Path
from typing import Dict, Tuple


HEADER_LIMIT = 2048


def sanitize_token(val: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", val.strip()) or "unknown"


def read_headers(conn: socket.socket) -> Tuple[Dict[str, str], bytes]:
    data = b""
    headers: Dict[str, str] = {}
    conn.settimeout(30)
    while b"\n\n" not in data and b"\r\n\r\n" not in data:
        chunk = conn.recv(512)
        if not chunk:
            break
        data += chunk
        if len(data) > HEADER_LIMIT:
            raise ValueError("header too large")

    if b"\r\n\r\n" in data:
        header_bytes, rest = data.split(b"\r\n\r\n", 1)
    else:
        header_bytes, rest = data.split(b"\n\n", 1) if b"\n\n" in data else (data, b"")

    for raw in header_bytes.splitlines():
        line = raw.decode("utf-8", errors="ignore").strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        headers[key.strip().upper()] = val.strip()

    return headers, rest


def handle_conn(conn: socket.socket, addr, out_root: Path) -> None:
    try:
        headers, _rest = read_headers(conn)
        device_id = sanitize_token(headers.get("DEVICE", ""))
        if not device_id:
            conn.sendall(b"RUN=\nLEN=0\n\n")
            return

        device_dir = out_root / device_id
        actions_path = device_dir / "latest_actions_device.txt"
        run_id_path = device_dir / "latest_run_id.txt"
        run_id = ""
        if run_id_path.exists():
            run_id = run_id_path.read_text(encoding="utf-8", errors="ignore").strip()

        if not actions_path.exists():
            conn.sendall(f"RUN={run_id}\nLEN=0\n\n".encode("utf-8"))
            return

        body = actions_path.read_bytes()
        header = f"RUN={run_id}\nLEN={len(body)}\n\n".encode("utf-8")
        conn.sendall(header)
        if body:
            conn.sendall(body)
    except Exception:
        try:
            conn.sendall(b"RUN=\nLEN=0\n\n")
        except Exception:
            pass
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18081)
    ap.add_argument("--out", default="/home/xrh/qwen3_os_fault/storage/tcp_out")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(16)

    print(f"[tcp_actions] listen {args.host}:{args.port} out={out_root}")

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_conn, args=(conn, addr, out_root), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
