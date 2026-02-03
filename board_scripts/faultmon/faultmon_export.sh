#!/bin/sh
# faultmon_export.sh — faultmon 数据导出工具（toybox sh 版本）
# 安装示例：mkdir -p /data/faultmon/tools && cp faultmon_export.sh /data/faultmon/tools/
# 运行示例：sh /data/faultmon/tools/faultmon_export.sh --date "$(date +%Y%m%d)"

set -eu

ROOT=${ROOT:-/data/faultmon}
EVENT_DIR=${EVENT_DIR:-$ROOT/events}
METRICS_DIR=${METRICS_DIR:-$ROOT/metrics}
LOG_DIR=${LOG_DIR:-$ROOT/logs}
STATE_DIR=${STATE_DIR:-$ROOT/state}
ARCHIVE_DIR=${ARCHIVE_DIR:-$ROOT/archive}
SAMPLES_DIR=${SAMPLES_DIR:-$ROOT/samples}

WINDOW_SEC=60
DAYS=1
DATE_OVERRIDE=""
TAG_REGEX=""
LEVEL_REGEX=""
SOURCE_REGEX=""
SELFTEST=0
SELFTEST_LIMIT=0

warn() {
  printf '%s\n' "$*" >&2
}

usage() {
  printf '%s\n' "Usage: faultmon_export.sh [--days N | --date YYYYMMDD] [--window-sec N]"
  printf '%s\n' "                          [--tag REGEX] [--level REGEX] [--source REGEX]"
  printf '%s\n' "                          [--selftest]"
  printf '%s\n' "Defaults: --days 1, window 60s, no filters."
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r/\\r/g; s/\n/\\n/g'
}

extract_json_number() {
  key="$1"; line="$2"; def="$3"
  val="$(printf '%s\n' "$line" | sed -n "s/.*\"$key\":\([0-9][0-9]*\).*/\1/p" | head -n 1)"
  [ -n "$val" ] || val="$def"
  printf '%s\n' "$val"
}

extract_json_string() {
  key="$1"; line="$2"; def="$3"
  val="$(printf '%s\n' "$line" | sed -n "s/.*\"$key\":\"\([^\"]*\)\".*/\1/p" | head -n 1)"
  [ -n "$val" ] || val="$def"
  printf '%s\n' "$val"
}

matches_regex() {
  pat="$1"; val="$2"
  [ -z "$pat" ] && return 0
  printf '%s\n' "$val" | grep -E "$pat" >/dev/null 2>&1
}

sanitize_tag() {
  raw="$1"
  [ -n "$raw" ] || raw="-"
  printf '%s\n' "$(printf '%s' "$raw" | sed 's/[^A-Za-z0-9._-]/_/g')"
}

day_from_offset() {
  off="$1"
  if day="$(date -d "-${off} day" +%Y%m%d 2>/dev/null)"; then
    printf '%s\n' "$day"
    return
  fi
  base="$(date +%s)"
  secs=$((base - off*86400))
  if day="$(date -r "$secs" +%Y%m%d 2>/dev/null)"; then
    printf '%s\n' "$day"
    return
  fi
  printf '%s\n' "$(date +%Y%m%d)"
}

latest_event_day() {
  latest=""
  found=0
  for f in "$EVENT_DIR"/events_*.jsonl; do
    [ -f "$f" ] || continue
    found=1
    name="${f##*/}"
    day="${name#events_}"
    day="${day%.jsonl}"
    if [ -z "$latest" ] || [ "$day" -gt "$latest" ]; then
      latest="$day"
    fi
  done
  [ "$found" -eq 0 ] && return 1
  printf '%s\n' "$latest"
}

day_list=""

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --days)
        [ $# -ge 2 ] || { warn "--days requires value"; exit 1; }
        DAYS="$2"; shift
        ;;
      --date)
        [ $# -ge 2 ] || { warn "--date requires value"; exit 1; }
        DATE_OVERRIDE="$2"; shift
        ;;
      --tag)
        [ $# -ge 2 ] || { warn "--tag requires regex"; exit 1; }
        TAG_REGEX="$2"; shift
        ;;
      --level)
        [ $# -ge 2 ] || { warn "--level requires regex"; exit 1; }
        LEVEL_REGEX="$2"; shift
        ;;
      --source)
        [ $# -ge 2 ] || { warn "--source requires regex"; exit 1; }
        SOURCE_REGEX="$2"; shift
        ;;
      --window-sec)
        [ $# -ge 2 ] || { warn "--window-sec requires value"; exit 1; }
        WINDOW_SEC="$2"; shift
        ;;
      --selftest)
        SELFTEST=1
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        warn "unknown arg: $1"
        usage
        exit 1
        ;;
    esac
    shift || true
  done
}

ensure_positive_int() {
  val="$1"; def="$2"
  case "$val" in
    ''|*[!0-9]*)
      printf '%s\n' "$def"
      ;;
    *)
      printf '%s\n' "$val"
      ;;
  esac
}

build_day_list() {
  if [ "$SELFTEST" -eq 1 ]; then
    if latest="$(latest_event_day)"; then
      day_list="$latest"
      SELFTEST_LIMIT=3
      return
    else
      warn "selftest: no events available"
      day_list=""
      return
    fi
  fi
  if [ -n "$DATE_OVERRIDE" ]; then
    day_list="$DATE_OVERRIDE"
    return
  fi
  count=0
  list=""
  while [ "$count" -lt "$DAYS" ]; do
    d="$(day_from_offset "$count")"
    list="$list $d"
    count=$((count + 1))
  done
  day_list="$(printf '%s\n' "$list" | sed 's/^ *//')"
}

abs_diff() {
  a="$1"; b="$2"
  diff=$((a - b))
  if [ "$diff" -lt 0 ]; then
    diff=$((0 - diff))
  fi
  printf '%s\n' "$diff"
}

find_archive_for_ts() {
  target_ts="$1"
  match_path=""
  match_diff=0
  index_file="$STATE_DIR/index.csv"
  [ -f "$index_file" ] || { printf '\n'; return; }
  line_no=0
  while IFS= read -r line || [ -n "$line" ]; do
    line_no=$((line_no + 1))
    [ "$line_no" -eq 1 ] && continue
    [ -n "$line" ] || continue
    ts_field="${line%%,*}"
    case "$ts_field" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    rest="${line#*,}"
    rest="${rest#*,}"
    rest="${rest#*,}"
    path_field="${rest%%,*}"
    path_field="$(printf '%s\n' "$path_field" | sed 's/^"//; s/"$//')"
    if [ -n "$path_field" ] && [ "${path_field#/}" = "$path_field" ]; then
      path_field="$ARCHIVE_DIR/$path_field"
    fi
    [ -n "$path_field" ] || continue
    diff="$(abs_diff "$target_ts" "$ts_field")"
    if [ "$diff" -le 120000 ]; then
      if [ -z "$match_path" ] || [ "$diff" -lt "$match_diff" ]; then
        match_path="$path_field"
        match_diff="$diff"
      fi
    fi
  done < "$index_file"
  printf '%s\n' "$match_path"
}

write_metrics_window() {
  csv="$1"; out="$2"; start="$3"; end="$4"
  if [ ! -f "$csv" ]; then
    : >"$out"
    warn "metrics missing: $csv"
    return
  fi
  : >"$out"
  header_done=0
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$header_done" -eq 0 ]; then
      printf '%s\n' "$line" >>"$out"
      header_done=1
      continue
    fi
    ts_field="${line%%,*}"
    ts_field="$(printf '%s\n' "$ts_field" | sed 's/[^0-9]//g')"
    case "$ts_field" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    if [ "$ts_field" -ge "$start" ] && [ "$ts_field" -le "$end" ]; then
      printf '%s\n' "$line" >>"$out"
    fi
  done < "$csv"
}

write_events_window() {
  src="$1"; out="$2"; start="$3"; end="$4"
  if [ ! -f "$src" ]; then
    : >"$out"
    warn "events missing for window: $src"
    return
  fi
  : >"$out"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    ts="$(extract_json_number "ts" "$line" "")"
    case "$ts" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    if [ "$ts" -ge "$start" ] && [ "$ts" -le "$end" ]; then
      printf '%s\n' "$line" >>"$out"
    fi
  done < "$src"
}

write_log_window() {
  log_file="$1"; out="$2"; start="$3"; end="$4"
  if [ ! -f "$log_file" ]; then
    : >"$out"
    warn "log missing: $log_file"
    return
  fi
  : >"$out"
  matched=0
  while IFS= read -r line || [ -n "$line" ]; do
    digits="${line%%[!0-9]*}"
    if [ -n "$digits" ]; then
      first="$(printf '%s\n' "$digits" | cut -c1-13)"
      if [ "${#first}" -ge 13 ]; then
        case "$first" in
          ''|*[!0-9]*)
            ;;
          *)
            if [ "$first" -ge "$start" ] && [ "$first" -le "$end" ]; then
              printf '%s\n' "$line" >>"$out"
              matched=1
            fi
            ;;
        esac
      fi
    fi
  done < "$log_file"
  if [ "$matched" -eq 0 ]; then
    tail -n 500 "$log_file" >"$out" 2>/dev/null || : >"$out"
  fi
}

append_samples_index() {
  day="$1"; sample_id="$2"; ts="$3"; tag="$4"; level="$5"; source="$6"; component="$7"
  window="$8"; metrics="$9"; events_file="${10}"; logs_out="${11}"; logs_err="${12}"; archive_path="${13}"; dir="${14}"
  index="$SAMPLES_DIR/$day/samples_${day}.jsonl"
  [ -f "$index" ] || : >"$index"
  if grep -F "\"id\":\"$sample_id\"" "$index" >/dev/null 2>&1; then
    return
  fi
  archive_json=""
  if [ -n "$archive_path" ]; then
    archive_json=",\"archive_path\":\"$(json_escape "$archive_path")\""
  fi
  printf '{"id":"%s","ts":%s,"tag":"%s","level":"%s","source":"%s","component":"%s","window_sec":%s,"metrics_csv":"%s","events_window":"%s","logs_out":"%s","logs_err":"%s","sample_dir":"%s"%s}\n' \
    "$(json_escape "$sample_id")" "$ts" "$(json_escape "$tag")" "$(json_escape "$level")" \
    "$(json_escape "$source")" "$(json_escape "$component")" "$window" \
    "$(json_escape "$metrics")" "$(json_escape "$events_file")" "$(json_escape "$logs_out")" \
    "$(json_escape "$logs_err")" "$(json_escape "$dir")" "$archive_json" >>"$index"
}

write_meta() {
  file="$1"; ts="$2"; tag="$3"; level="$4"; source="$5"; component="$6"; pid="$7"; rule="$8"; day="$9"; dir="${10}"; window="${11}"; archive="${12}"
  archive_json=""
  if [ -n "$archive" ]; then
    archive_json=",\"archive_path\":\"$(json_escape "$archive")\""
  fi
  printf '{"ts":%s,"tag":"%s","level":"%s","source":"%s","component":"%s","pid":"%s","rule":"%s","date":"%s","sample_dir":"%s","window_sec":%s%s}\n' \
    "$ts" "$(json_escape "$tag")" "$(json_escape "$level")" "$(json_escape "$source")" \
    "$(json_escape "$component")" "$(json_escape "$pid")" "$(json_escape "$rule")" \
    "$day" "$(json_escape "$dir")" "$window" "$archive_json" >"$file"
}

process_event_line() {
  day="$1"; lineno="$2"; line="$3"; events_file="$4"
  ts="$(extract_json_number "ts" "$line" "0")"
  case "$ts" in
    ''|*[!0-9]*)
      ts=0
      ;;
  esac
  tag="$(extract_json_string "tag" "$line" "-")"
  level="$(extract_json_string "level" "$line" "-")"
  source="$(extract_json_string "source" "$line" "-")"
  component="$(extract_json_string "component" "$line" "-")"
  pid="$(extract_json_string "pid" "$line" "-")"
  rule="$(extract_json_string "rule" "$line" "-")"

  matches_regex "$TAG_REGEX" "$tag" || return 0
  matches_regex "$LEVEL_REGEX" "$level" || return 0
  matches_regex "$SOURCE_REGEX" "$source" || return 0

  preferred="$tag"
  if [ -z "$preferred" ] || [ "$preferred" = "-" ]; then
    preferred="$component"
  fi
  if [ -z "$preferred" ] || [ "$preferred" = "-" ]; then
    preferred="no_tag"
  fi
  safe_tag="$(sanitize_tag "$preferred")"
  sample_dir="$SAMPLES_DIR/$day/${ts}_${safe_tag}_${lineno}"
  mkdir -p "$sample_dir"

  event_file="$sample_dir/event.json"
  printf '%s\n' "$line" >"$event_file"

  window_ms=$((WINDOW_SEC * 1000))
  ts_start=$((ts - window_ms))
  if [ "$ts_start" -lt 0 ]; then
    ts_start=0
  fi
  ts_end=$((ts + window_ms))

  metrics_src="$METRICS_DIR/sys_${day}.csv"
  metrics_out="$sample_dir/metrics_window.csv"
  write_metrics_window "$metrics_src" "$metrics_out" "$ts_start" "$ts_end"

  events_out="$sample_dir/events_window.jsonl"
  write_events_window "$events_file" "$events_out" "$ts_start" "$ts_end"

  log_out="$sample_dir/logs_ctx.out"
  log_err="$sample_dir/logs_ctx.err"
  write_log_window "$LOG_DIR/faultmon.out" "$log_out" "$ts_start" "$ts_end"
  write_log_window "$LOG_DIR/faultmon.err" "$log_err" "$ts_start" "$ts_end"

  archive_path="$(find_archive_for_ts "$ts")"
  if [ -n "$archive_path" ]; then
    link_target="$sample_dir/archive_link"
    if [ -e "$archive_path" ] && [ ! -e "$link_target" ]; then
      if ln -s "$archive_path" "$link_target" 2>/dev/null; then
        :
      else
        base="${archive_path##*/}"
        cp "$archive_path" "$sample_dir/$base" 2>/dev/null || true
      fi
    fi
  fi

  write_meta "$sample_dir/meta.json" "$ts" "$tag" "$level" "$source" "$component" "$pid" "$rule" "$day" "$sample_dir" "$WINDOW_SEC" "$archive_path"

  sample_id="${day}_${ts}_${safe_tag}_${lineno}"
  append_samples_index "$day" "$sample_id" "$ts" "$tag" "$level" "$source" "$component" "$WINDOW_SEC" "$metrics_out" "$events_out" "$log_out" "$log_err" "$archive_path" "$sample_dir"

  printf '%s\n' "$sample_id"
}

process_day() {
  day="$1"; limit="$2"
  events_file="$EVENT_DIR/events_${day}.jsonl"
  if [ ! -f "$events_file" ]; then
    warn "events file missing: $events_file"
    return
  fi
  mkdir -p "$SAMPLES_DIR/$day"
  total_lines="$(wc -l <"$events_file" 2>/dev/null || printf '0')"
  start_line=1
  if [ "$limit" -gt 0 ] && [ "$total_lines" -gt "$limit" ]; then
    start_line=$((total_lines - limit + 1))
  fi
  lineno=0
  created=0
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    [ "$lineno" -ge "$start_line" ] || continue
    [ -n "$line" ] || continue
    id="$(process_event_line "$day" "$lineno" "$line" "$events_file")" || true
    [ -n "$id" ] || continue
    created=$((created + 1))
  done <"$events_file"
  printf '%s\n' "$created"
}

selftest_report() {
  day="$1"
  index="$SAMPLES_DIR/$day/samples_${day}.jsonl"
  count="$(grep -c '"id":"' "$index" 2>/dev/null || printf '0')"
  printf 'selftest_samples=%s\n' "$count"
  printf 'selftest_output=%s\n' "$SAMPLES_DIR/$day"
  printf 'selftest_preview:\n'
  if [ -f "$index" ]; then
    tail -n 5 "$index"
  fi
}

main() {
  parse_args "$@"
  DAYS="$(ensure_positive_int "$DAYS" 1)"
  WINDOW_SEC="$(ensure_positive_int "$WINDOW_SEC" 60)"
  if [ -n "$DATE_OVERRIDE" ] && [ "$DAYS" != "1" ] && [ "$SELFTEST" -eq 0 ]; then
    warn "--days conflicts with --date; using --date only"
    DAYS=1
  fi
  mkdir -p "$SAMPLES_DIR"
  build_day_list
  [ -n "$day_list" ] || exit 0
  total_created=0
  last_day=""
  for day in $day_list; do
    last_day="$day"
    limit="$SELFTEST_LIMIT"
    created="$(process_day "$day" "${limit:-0}")"
    case "$created" in
      ''|*[!0-9]*)
        created=0
        ;;
    esac
    total_created=$((total_created + created))
  done
  if [ "$SELFTEST" -eq 1 ] && [ -n "$last_day" ]; then
    selftest_report "$last_day"
  fi
  exit 0
}

main "$@"
