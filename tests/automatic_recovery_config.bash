# SPDX-License-Identifier: MIT
kilix_on_ready() {
    printf '%s\n' "$BATTY_CONTROL" >"${BATTY_AUTO_RECOVERY_READY:?}"
    if [[ ${BATTY_AUTO_RECOVERY_CLOSE:-0} == 1 ]]; then
        local listing pane rest first=1
        batty workspace panes "$1" -V listing
        while read -r pane rest; do
            [[ -n $pane ]] || continue
            if ((first)); then
                kilix_automatic_close_page "$pane"
                first=0
            else
                kilix_automatic_close "$pane"
            fi
        done <<<"$listing"
    fi
}
