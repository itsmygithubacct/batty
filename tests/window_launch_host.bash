#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
enable -f "$PWD/build/batty.so" batty
export BATTY_SESSION_DIR=${BATTY_TEST_SESSION_DIR:?}
: "${BATTY_WINDOW_LAUNCH_DIR:?}"
workspace='' first=''
cleanup() { [[ -z $workspace ]] || batty workspace close "$workspace" || :; }
trap cleanup EXIT
batty workspace new -h workspace --width 500 --height 320
batty workspace add "$workspace" -V first -- /bin/cat
batty workspace listen "$workspace" "$BATTY_WINDOW_LAUNCH_DIR/rw.sock"
printf '%s\n' "$first"
while batty workspace pump "$workspace" -t 2; do :; done
