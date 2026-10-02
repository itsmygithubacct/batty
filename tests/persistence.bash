#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
enable -f "$PWD/build/batty.so" batty
: "${BATTY_BASH:?tests/run.py must select the verified bash-os runtime}"
private=${BATTY_TEST_SESSION_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/bt-cli-XXXXXX")}
export BATTY_SESSION_DIR=$private
export BATTY_CONFIG=$private/empty.bash
: > "$BATTY_CONFIG"
a='' b='' observer='' creator=''
cleanup() {
    local status=$? handle name
    for handle in "$a" "$b" "$observer"; do
        [[ -z $handle ]] || batty close "$handle" >/dev/null 2>&1 || :
    done
    if [[ -n $creator ]]; then
        kill -KILL "$creator" 2>/dev/null || :
        wait "$creator" 2>/dev/null || :
    fi
    for name in shell hard cli; do
        batty terminate "$name" >/dev/null 2>&1 || :
    done
    for name in "$private"/*; do
        if [[ -S $name || ( -d $name && ! -L $name ) ]]; then
            printf 'FAIL persistence cleanup retained session: %s\n' "$name" >&2
            exit 1
        fi
    done
    rm -rf -- "$private"
    return "$status"
}
trap cleanup EXIT
fail() { printf 'FAIL persistence Bash: %s\n' "$*" >&2; exit 1; }
pump() { if batty pump "$1" -t 5; then :; else [[ $? == 1 ]] || fail 'pump failed'; fi; }
dump() { batty dump "$1" > "$private/screen"; }
until_text() {
    local handle=$1 expected=$2 deadline=$((SECONDS+5))
    while ((SECONDS<deadline)); do
        pump "$handle"
        dump "$handle"
        [[ $(< "$private/screen") != *"$expected"* ]] || return 0
    done
    fail "missing screen text: $expected"
}
until_done() {
    local handle=$1 expected=$2 state=running deadline=$((SECONDS+5))
    while [[ $state == running ]] && ((SECONDS<deadline)); do
        pump "$handle"
        batty status "$handle" -V state
    done
    [[ $state == "$expected" ]] || fail "expected exit $expected, got $state"
}
status_is() {
    local expected=$1 actual=0
    shift
    "$@" || actual=$?
    [[ $actual == "$expected" ]] || fail "expected command exit $expected, got $actual"
}

# This creator process exits entirely. The service must retain the PTY/parser.
# shellcheck disable=SC2016
"$BATTY_BASH" --noprofile --norc -c '
    enable -f "$PWD/build/batty.so" batty
    batty new -h h --headless --session shell -- "$BATTY_BASH" --noprofile --norc -c '\''printf CREATOR_EXIT_SURVIVED; IFS= read -r gate; printf "\nGATE:%s" "$gate"; exit 11'\''
'
batty attach -h a --headless -- shell
until_text "$a" CREATOR_EXIT_SURVIVED
batty attach -h observer --headless --observe -- shell
if batty send "$observer" forbidden 2>/dev/null; then fail 'observer accepted input'; fi
if batty resize "$observer" 70 20 2>/dev/null; then fail 'observer resized terminal'; fi
batty close "$observer"; observer=''
batty info "$a" > "$private/info"
[[ $(< "$private/info") == *persistent=1* ]] || fail 'handle does not report persistent ownership'
if ignored=$(batty send "$a" forbidden 2>/dev/null); then fail 'subshell accepted parent handle'; fi
[[ -z $ignored ]] || fail 'rejected subshell emitted data'
batty close "$a"; a=''
batty attach -h b --headless --session-dir "$private" -- shell
until_text "$b" CREATOR_EXIT_SURVIVED
batty send "$b" $'reattached\n'
until_done "$b" 11
until_text "$b" GATE:reattached
batty close "$b"; b=''
batty attach -h a --headless -- shell
until_done "$a" 11
until_text "$a" GATE:reattached
batty close "$a"; a=''
batty terminate --session-dir "$private" shell
[[ ! -e $private/shell.sock ]] || fail 'terminated shell endpoint remains'
printf '%s\n' 'PASS persistence Bash: creator exit, read-only observer, handle ownership, detach and retained exit status'

# A stopped creator cannot run an EXIT trap. Killing our own recorded child
# therefore exercises loss of the entire frontend without graceful detachment.
# shellcheck disable=SC2016
"$BATTY_BASH" --noprofile --norc -c '
    enable -f "$PWD/build/batty.so" batty
    batty new -h h --headless --session hard -- "$BATTY_BASH" --noprofile --norc -c '\''printf HARD_LOSS_SURVIVED; IFS= read -r gate; exit 12'\'' || exit
    printf ready > "$BATTY_SESSION_DIR/creator-ready"
    kill -STOP "$BASHPID"
' &
creator=$!
deadline=$((SECONDS+5))
while [[ ! -f $private/creator-ready ]] && ((SECONDS<deadline)); do sleep 0.01; done
[[ -f $private/creator-ready ]] || fail 'hard-loss creator startup deadline'
kill -KILL "$creator"
wait "$creator" 2>/dev/null || :
creator=''
batty attach -h a --headless -- hard
until_text "$a" HARD_LOSS_SURVIVED
batty send "$a" $'finish\n'
until_done "$a" 12
batty close "$a"; a=''
batty terminate hard
printf '%s\n' 'PASS persistence Bash: SIGKILL of the creator leaves its application attachable'

# Exercise the public launcher, including attach-only behavior and final status.
status_is 17 ./batty --headless --session cli -- "$BATTY_BASH" --noprofile --norc -c 'printf CLI_RETAINED; exit 17'
./batty --list > "$private/list"
[[ $(< "$private/list") == *cli* ]] || fail 'launcher list omitted completed named session'
status_is 17 ./batty --headless --attach cli
status_is 17 ./batty --headless --observe cli
status_is 17 ./batty --headless --session cli -- "$BATTY_BASH" --noprofile --norc -c 'exit 99'
./batty --terminate cli
[[ ! -e $private/cli.sock ]] || fail 'launcher terminate left endpoint behind'
if ./batty --headless --attach absent 2>/dev/null; then fail 'attach-only created a missing session'; fi
[[ ! -e $private/absent.sock ]] || fail 'failed attach left endpoint behind'
if batty new -h a --headless --session badexec -- /nonexistent-batty-persistence-command 2>/dev/null; then
    fail 'named startup accepted nonexistent command'
fi
[[ ! -e $private/badexec.sock ]] || fail 'failed named startup left an endpoint'
printf '%s\n' 'PASS persistence Bash: public create/attach/observe/list/terminate and startup failure'
