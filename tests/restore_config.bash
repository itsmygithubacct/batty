# SPDX-License-Identifier: MIT
kilix_on_ready() {
    local handle=$1 first=$2 second='' third=''
    if [[ ${BATTY_RESTORE_SOURCE:-0} == 1 ]]; then
        batty workspace add "$handle" -V second --target "$first" --direction down --session restore-second -- /bin/cat
        batty workspace rename "$handle" "$first" 'Build λ'
        batty workspace resize "$handle" "$first" vertical 900
        batty workspace sync "$handle" "$second" 1
        batty workspace add "$handle" -V third --session restore-third -- /bin/sh -c 'printf "COMPLETED_RESTORE\n"; exit 7'
        # The frontend consumes this associative policy table.
        # shellcheck disable=SC2034,SC2004
        application_panes[$third]=held
        batty workspace rename "$handle" "$third" Logs
        batty workspace focus "$handle" "$second"
    fi
    printf '%s\n' "$BATTY_CONTROL" >"${BATTY_RESTORE_READY:?}"
}
