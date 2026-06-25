#!/bin/sh
# Unified server endpoint configuration for sh/bash scripts.
# Priority: environment > tools/config.local.env > defaults.

_qwen3_defaults_host="183.56.183.131"
_qwen3_defaults_ingest_port="18080"
_qwen3_defaults_actions_port="28081"

_qwen3_env_host_set="${QWEN3_SERVER_HOST+x}"
_qwen3_env_host_val="${QWEN3_SERVER_HOST:-}"
_qwen3_env_ingest_set="${QWEN3_INGEST_PORT+x}"
_qwen3_env_ingest_val="${QWEN3_INGEST_PORT:-}"
_qwen3_env_actions_set="${QWEN3_ACTIONS_PORT+x}"
_qwen3_env_actions_val="${QWEN3_ACTIONS_PORT:-}"
_qwen3_env_ssh_set="${QWEN3_SSH_HOST+x}"
_qwen3_env_ssh_val="${QWEN3_SSH_HOST:-}"

qwen3_server_host="$_qwen3_defaults_host"
qwen3_ingest_port="$_qwen3_defaults_ingest_port"
qwen3_actions_port="$_qwen3_defaults_actions_port"
qwen3_ssh_host=""

_qwen3_script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)"
if [ -f "./tools/config.local.env" ]; then
  # shellcheck disable=SC1091
  . "./tools/config.local.env"
elif [ -n "$_qwen3_script_dir" ] && [ -f "$_qwen3_script_dir/config.local.env" ]; then
  # shellcheck disable=SC1090
  . "$_qwen3_script_dir/config.local.env"
fi

if [ -n "${QWEN3_SERVER_HOST:-}" ]; then qwen3_server_host="$QWEN3_SERVER_HOST"; fi
if [ -n "${QWEN3_INGEST_PORT:-}" ]; then qwen3_ingest_port="$QWEN3_INGEST_PORT"; fi
if [ -n "${QWEN3_ACTIONS_PORT:-}" ]; then qwen3_actions_port="$QWEN3_ACTIONS_PORT"; fi
if [ -n "${QWEN3_SSH_HOST:-}" ]; then qwen3_ssh_host="$QWEN3_SSH_HOST"; fi

if [ "$_qwen3_env_host_set" = "x" ] && [ -n "$_qwen3_env_host_val" ]; then qwen3_server_host="$_qwen3_env_host_val"; fi
if [ "$_qwen3_env_ingest_set" = "x" ] && [ -n "$_qwen3_env_ingest_val" ]; then qwen3_ingest_port="$_qwen3_env_ingest_val"; fi
if [ "$_qwen3_env_actions_set" = "x" ] && [ -n "$_qwen3_env_actions_val" ]; then qwen3_actions_port="$_qwen3_env_actions_val"; fi
if [ "$_qwen3_env_ssh_set" = "x" ] && [ -n "$_qwen3_env_ssh_val" ]; then qwen3_ssh_host="$_qwen3_env_ssh_val"; fi

if [ -z "$qwen3_server_host" ]; then qwen3_server_host="$_qwen3_defaults_host"; fi
if [ -z "$qwen3_ingest_port" ]; then qwen3_ingest_port="$_qwen3_defaults_ingest_port"; fi
if [ -z "$qwen3_actions_port" ]; then qwen3_actions_port="$_qwen3_defaults_actions_port"; fi
if [ -z "$qwen3_ssh_host" ]; then qwen3_ssh_host="$qwen3_server_host"; fi

export qwen3_server_host qwen3_ingest_port qwen3_actions_port qwen3_ssh_host
