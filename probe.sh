#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Headless PTY -> native VT proof. Run with a bash-os executable.
set -euo pipefail
PATH=

for command in pty vt bashpoll; do
    [[ $(type -t "$command") == builtin ]] || {
        printf 'batty probe needs the bash-os %s builtin\n' "$command" >&2
        exit 2
    }
done

master= child= terminal=
cleanup() {
    local result=$?
    trap - EXIT
    if [[ -n $child ]]; then
        kill -KILL "$child" 2>/dev/null || :
        pty waitpid "$child" >/dev/null 2>&1 || :
    fi
    [[ -z $master ]] || pty close "$master" || :
    [[ -z $terminal ]] || vt free "$terminal" || :
    return "$result"
}
trap cleanup EXIT

vt new -h terminal -W 20 -H 3
pty spawn master child "$BASH" --noprofile --norc -c '
    PATH=
    IFS= read -r request
    printf "\033[2J\033[Hhello %s\n\033[31mred\033[0m\000" "$request"
    exit 7
'
pty echo "$master" off
pty resize "$master" -W 20 -H 3
bashpoll set-nonblock "$master" on
printf 'batty\n' >&"$master"

# Fixed fixture length lets this small proof stop before PTY EOF/EIO.
# A general terminal needs explicit would-block, EOF and error events.
expected=$'\e[2J\e[Hhello batty\r\n\e[31mred\e[0m'
target=$((${#expected} + 1)) # Includes the NUL, which never enters a Bash string.
received=0
deadline=$((SECONDS + 5))
while ((received < target)); do
    ((SECONDS < deadline)) || { printf 'PTY read deadline exceeded\n' >&2; exit 1; }
    bashpoll wait -t 1000 "$master" >/dev/null
    vt feed-fd "$terminal" "$master" -N 65536
    ((BVT_LAST_BYTES > 0)) || { printf 'Premature PTY EOF\n' >&2; exit 1; }
    received=$((received + BVT_LAST_BYTES))
done
((received == target))
if pty waitpid "$child" child_status; then
    wait_status=0
else
    wait_status=$?
fi
child=
[[ $wait_status == 7 && $child_status == 7 ]]
pty close "$master"
master=
vt flush "$terminal"
vt feed "$terminal" $'\e[6n'
vt response "$terminal" -V reply
[[ $reply == $'\e[2;4R' ]]
printf 'PTY bytes=%s child_status=%s cursor_reply=2;4R\n' "$received" "$child_status"
vt render "$terminal"
