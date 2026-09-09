#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
enable -f "$PWD/build/batty.so" batty
a='' b=''
cleanup() {
    [[ -z $a ]] || batty close "$a" || :
    [[ -z $b ]] || batty close "$b" || :
}
trap cleanup EXIT
export BATTY_TEST_EXPORT=exported_after_shell_start
# The child expands the exported variable.
# shellcheck disable=SC2016
batty new --headless -h a -- "$BASH" --noprofile --norc -c 'read -r gate; printf "ONE:%s" "$BATTY_TEST_EXPORT"; exit 3'
batty new --headless -h b -- "$BASH" --noprofile --norc -c 'read -r gate; printf TWO; exit 4'
if ignored=$(batty send "$a" lost 2>/dev/null); then
    printf '%s\n' 'FAIL: subshell handle was accepted' >&2; exit 1
fi
[[ -z $ignored ]]
batty send "$a" $'go\n'
batty send "$b" $'go\n'
deadline=$((SECONDS+5))
first=running second=running
while [[ $first == running || $second == running ]]; do
    ((SECONDS<deadline))
    for handle in "$a" "$b"; do
        if batty pump "$handle" -t 5; then :; else [[ $? == 1 ]]; fi
    done
    batty status "$a" -V first
    batty status "$b" -V second
done
[[ $first == 3 && $second == 4 ]]
batty dump "$a" >build/builtin-screen.txt
# PTY echo may precede the fixture text, so inspect the whole saved screen externally below.
batty close "$a"; a=
batty close "$b"; b=
if batty status batty-1 >/dev/null 2>&1; then exit 1; fi
if batty new --headless -h a -- /nonexistent-batty-test-command 2>/dev/null; then exit 1; fi
printf '%s\n' 'PASS Bash-owned handles, exported environment, concurrent exit statuses, subshell rejection and failed exec'
