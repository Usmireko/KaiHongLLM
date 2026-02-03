#!/usr/bin/env python3

import argparse
import os
import re
import socket
import threading
from pathlib import Path
from typing import Dict, Tuple


HEADER_LIMIT = 4096


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


def read_exact(conn: socket.socket, need: int, first: bytes) -> bytes:
    if need <= 0:
        return b""
    buf = bytearray(first[:need])
    while len(buf) < need:
        chunk = conn.recv(min(65536, need - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def handle_conn(conn: socket.socket, addr, inbox_root: Path) -> None:
    try:
        headers, rest = read_headers(conn)
        kind = sanitize_token(headers.get("TYPE", ""))
        device_id = sanitize_token(headers.get("DEVICE", ""))
        run_id = sanitize_token(headers.get("RUN", ""))
        length_raw = headers.get("LEN", "0")
        try:
            length = int(length_raw)
        except ValueError:
            length = -1

        if not kind or not device_id or not run_id or length < 0:
            conn.sendall(b"ERR\n")
            return

        if length > 1024 * 1024 * 1024:
            conn.sendall(b"ERR\n")
            return

        payload = read_exact(conn, length, rest)
        if len(payload) != length:
            conn.sendall(b"ERR\n")
            return

        device_dir = inbox_root / device_id
        device_dir.mkdir(parents=True, exist_ok=True)

        ext = ".bin"
        if kind in ("bundle", "action_result"):
            ext = ".tar.gz"

        name = f"{run_id}__{kind}{ext}"
        dest = device_dir / name
        tmp = Path(str(dest) + ".tmp")

        with tmp.open("wb") as f:
            f.write(payload)
        os.replace(tmp, dest)

        conn.sendall(b"OK\n")
    except Exception:
        try:
            conn.sendall(b"ERR\n")
        except Exception:
            pass
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--inbox", default="/home/xrh/qwen3_os_fault/storage/tcp_inbox")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    inbox_root = Path(args.inbox)
    inbox_root.mkdir(parents=True, exist_ok=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(16)

    print(f"[tcp_ingest] listen {args.host}:{args.port} inbox={inbox_root}")

    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=handle_conn, args=(conn, addr, inbox_root), daemon=True)
        t.start()


if __name__ == "__main__":
    main()
