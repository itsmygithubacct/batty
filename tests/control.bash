#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
enable -f "$PWD/build/batty.so" batty
: "${BATTY_CONTROL_TEST_DIR:?}"
export BATTY_SESSION_DIR=${BATTY_TEST_SESSION_DIR:?isolated session root required}
a='' b='' saved='' temporary='' first='' second=''
cleanup() {
    local h
    for h in "$a" "$b" "$saved" "$temporary"; do [[ -z $h ]] || batty workspace close "$h" || :; done
    batty terminate control >/dev/null 2>&1 || :
    batty terminate saved-workspace >/dev/null 2>&1 || :
}
trap cleanup EXIT
export BATTY_CONTROL=$BATTY_CONTROL_TEST_DIR/rw.sock
batty workspace new -h a --width 800 --height 500
batty workspace chrome "$a" 1
batty workspace add "$a" -V first -- /bin/cat
batty workspace listen "$a" "$BATTY_CONTROL"
batty workspace listen "$a" "$BATTY_CONTROL_TEST_DIR/scope-view.sock" --pane "$first" --read-only
batty workspace listen "$a" "$BATTY_CONTROL_TEST_DIR/scope-input.sock" --pane "$first"
batty workspace new -h b --width 400 --height 300
batty workspace add "$b" -V second -- /bin/cat
batty workspace listen "$b" "$BATTY_CONTROL_TEST_DIR/ro.sock" --read-only
batty workspace new -h temporary --width 200 --height 100
if batty workspace listen "$temporary" "$BATTY_CONTROL" 2>/dev/null; then exit 2; fi
if batty workspace listen "$temporary" "$BATTY_CONTROL_TEST_DIR/unsafe/socket" 2>/dev/null; then exit 2; fi
batty workspace close "$temporary"; temporary=''
batty workspace new -h saved --width 500 --height 300
batty workspace add "$saved" -V saved_pane --session saved-workspace -- /bin/cat
batty workspace listen "$saved" "$BATTY_CONTROL_TEST_DIR/saved.sock"
printf '%s %s\n' "$first" "$second"
while batty workspace pump "$a" -t 2; do batty workspace pump "$b" -t 0; batty workspace pump "$saved" -t 0; done
