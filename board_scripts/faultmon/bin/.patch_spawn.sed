/^spawn() {/,/^}/c\
spawn() {\
 func="$1"; out_file="$2"; err_file="$3";\
 /bin/sh -c ". \\"$BIN_DIR/faultmon.sh\\"; $func" >>"$out_file" 2>>"$err_file" &\
  printf "%s\n" "$!" >"$STATE_DIR/pid.$func"\
}
