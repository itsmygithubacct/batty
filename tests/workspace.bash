#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
enable -f "$PWD/build/batty.so" batty
workspace= other=
: "${BATTY_TEST_SESSION_DIR:?run through tests/run.py for isolated session cleanup}"
export BATTY_SESSION_DIR=$BATTY_TEST_SESSION_DIR
cleanup() {
    [[ -z $workspace ]] || batty workspace close "$workspace" || :
    [[ -z $other ]] || batty workspace close "$other" || :
    batty terminate workspace >/dev/null 2>&1 || :
}
trap cleanup EXIT
reject() { if "$@" 2>/dev/null; then printf 'Unexpected success: %s\n' "$*" >&2; exit 1; fi; }
readonly locked=unchanged
reject batty workspace new -h locked
batty workspace new -h workspace --title 'Workspace test' --width 720 --height 480
batty workspace pump-stats "$workspace" -V pump_stats
[[ $pump_stats == 'passes=0 yields=0 panes=0 io_ms=0 max_io_ms=0 pending_detaches=0 completed_detaches=0 failed_detaches=0' ]]
reject batty workspace pump-stats "$workspace" unexpected
# Application menus reject unsafe labels and bound their storage.
reject batty workspace app-menu "$workspace" ''
reject batty workspace app-menu "$workspace" $'bad\nlabel'
for ((app=0; app<256; ++app)); do batty workspace app-menu "$workspace" "Application $app"; done
reject batty workspace app-menu "$workspace" 'Overflow'
batty workspace app-menu "$workspace"
batty workspace app-menu "$workspace" 'After reset'
batty workspace app-menu "$workspace"
export BATTY_TEST_EXPORT=workspace
# Two real PTYs must keep separate input, exit status and environment.
batty workspace add "$workspace" -V first -- "$BASH" --noprofile --norc -c \
    'read -r line; printf "FIRST:%s:%s\n" "$line" "$BATTY_TEST_EXPORT"; exit 3'
batty workspace add "$workspace" -V second --target "$first" --direction right -- \
    "$BASH" --noprofile --norc -c 'read -r line; printf "SECOND:%s\n" "$line"; exit 4'
batty workspace active "$workspace" -V active
[[ $active == "$second" ]]
batty workspace neighbor "$workspace" "$first" right -V neighbor
[[ $neighbor == "$second" ]]
batty workspace panes "$workspace" -V before
batty workspace pane-rename "$workspace" "$first" 'Named pane'
batty workspace title "$workspace" "$first" -V pane_title
[[ $pane_title == 'Named pane' ]]
reject batty workspace pane-rename "$workspace" "$first" $'bad\ntitle'
batty workspace pane-copy-title "$workspace" "$first"
batty workspace pane-reset-title "$workspace" "$first"
batty workspace title "$workspace" "$first" -V pane_title
[[ $pane_title != 'Named pane' ]]
batty workspace pane-clear "$workspace" "$first"
reject batty workspace add "$workspace" -V locked -- /bin/true
reject batty workspace add "$workspace" -V bad -- /nonexistent-batty-test-command
batty workspace panes "$workspace" -V after
[[ $before == "$after" ]]
reject batty workspace focus "$workspace" -1
reject batty workspace focus "$workspace" 18446744073709551616
reject batty workspace focus "$workspace" "$first" extra
reject batty workspace send "$workspace" 999999 anything
if ignored=$(batty workspace active "$workspace" 2>/dev/null); then exit 1; fi
[[ -z $ignored ]]
batty workspace resize "$workspace" "$first" horizontal 500
batty workspace move "$workspace" "$first" right
batty workspace neighbor "$workspace" "$first" left -V neighbor
[[ $neighbor == "$second" ]]
batty workspace sync "$workspace" "$first" 1
batty workspace zoom "$workspace" "$first"
batty workspace panes "$workspace" -V listing
visible=0
while read -r id tab window active shown sync x y width height; do
    visible=$((visible+shown))
done <<<"$listing"
[[ $visible == 1 ]]
batty workspace zoom "$workspace" "$first"
batty workspace add "$workspace" -V third -- /bin/cat
batty workspace tab "$workspace" -1
batty workspace active "$workspace" -V active
[[ $active == "$first" ]]
batty workspace send "$workspace" "$first" $'one\n'
batty workspace paste "$workspace" "$second" $'two\n'
deadline=$((SECONDS+10))
a=running b=running
while [[ $a == running || $b == running ]]; do
    ((SECONDS<deadline))
    batty workspace pump "$workspace" -t 5
    batty workspace status "$workspace" "$first" -V a
    batty workspace status "$workspace" "$second" -V b
done
[[ $a == 3 && $b == 4 ]]
batty workspace dump "$workspace" "$first" >build/workspace-first.txt
batty workspace dump "$workspace" "$second" >build/workspace-second.txt
grep -q 'FIRST:one:workspace' build/workspace-first.txt
grep -q 'SECOND:two' build/workspace-second.txt
batty workspace capture "$workspace" build/workspace-builtin.ppm
[[ -s build/workspace-builtin.ppm ]]
# A separate window survives disposal of the first, with disjoint pane IDs.
batty workspace new -h other --width 320 --height 240
batty workspace add "$other" -V fourth -- /bin/cat
reject batty workspace focus "$other" "$first"
for id in "$first" "$second" "$third"; do batty workspace remove "$workspace" "$id"; done
if batty workspace pump "$workspace" -t 0; then exit 1; else [[ $? == 1 ]]; fi
batty workspace close "$workspace"; workspace=
batty workspace pump "$other" -t 0
old=$other
batty workspace close "$other"; other=
reject batty workspace pump "$old"
# Persistent views detach and reconnect without replaying or restarting the child.
batty workspace new -h workspace --width 500 --height 300
batty workspace add "$workspace" -V first --session workspace -- /bin/cat
batty workspace send "$workspace" "$first" $'PERSISTENT_WORKSPACE\n'
deadline=$((SECONDS+5))
while :; do
    ((SECONDS<deadline))
    batty workspace pump "$workspace" -t 5
    batty workspace dump "$workspace" "$first" >build/workspace-persistent.txt
    if grep -q PERSISTENT_WORKSPACE build/workspace-persistent.txt; then break; fi
done
batty workspace new -h other --width 500 --height 300
batty workspace add "$other" -V observer --session workspace --observe
reject batty workspace send "$other" "$observer" forbidden
reject batty workspace paste "$other" "$observer" forbidden
batty workspace close "$workspace"; workspace=
# Epoch syntax and attachment-only restriction are checked before opening a view.
for epoch in 0 -1 +1 0x1 10000000000000000 invalid; do
    reject batty workspace add "$other" -V rejected --session workspace --attach --epoch "$epoch"
done
reject batty workspace add "$other" -V rejected --session workspace --epoch 1 -- /bin/true
batty workspace add "$other" -V attached --session workspace --attach
batty workspace pump "$other" -t 5
batty workspace dump "$other" "$attached" >build/workspace-reattached.txt
grep -q PERSISTENT_WORKSPACE build/workspace-reattached.txt
batty workspace close "$other"; other=
batty terminate workspace
printf '%s\n' 'PASS workspace Bash API: layout, PTYs, windows, handle ownership, persistent reattachment and observers'
