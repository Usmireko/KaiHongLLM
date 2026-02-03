#!/bin/sh
EV_DIR="/data/faultmon/events"
[ -d "$EV_DIR" ] || { echo "no events dir: $EV_DIR"; exit 0; }

for F in "$EV_DIR"/events_*.jsonl; do
  [ -f "$F" ] || continue
  BK="$F.bak.$(date +%s)"
  cp "$F" "$BK"

  # 1) 把恰好 10 位的 "ts":1234567890 统一补成毫秒（*1000）
  #    仅修改 "ts":<10位><非数字> 的位置，避免误伤已有 13 位的条目。
  sed -i 's/"ts":\([0-9]\{10\}\)\([^0-9]\)/"ts":\1000\2/g' "$F"

  # 2) 可选：丢弃“显著未来”的测试行（以 2030 年前后的秒级为主）
  #    若你想保留全部原始数据，把下面三行注释掉即可。
  TMP="${F}.tmp.$$"
  now_s=$(date +%s)
  fut_s=$(( now_s + 86400 ))   # 允许最多 +1 天
  # 过滤条件：仍然残留的 10 位 ts（极少数）且 >= 1900000000（约 2030-…）
  # 注意：仅基于前缀启发式删除测试噪声行，不影响 13 位毫秒数据。
  grep -v '"ts":19[0-9]\{9\}\([^0-9]\)' "$F" > "$TMP" || true
  mv "$TMP" "$F"

  echo "[fixed] $(basename "$F")  (backup=$(basename "$BK"))"
  echo "  10-digit left : $(grep -Eo '"ts":[0-9]{10}\b' "$F" | wc -l)"
  echo "  13-digit total: $(grep -Eo '"ts":[0-9]{13}\b' "$F" | wc -l)"
done
