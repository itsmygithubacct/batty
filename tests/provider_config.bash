# SPDX-License-Identifier: MIT
export BATTY_WIDTH=640 BATTY_HEIGHT=480
kilix_on_ready() {
    # The provider fixture may spend tens of seconds in software GL under
    # load; keep its host alive past the fixture's 55-second action deadline.
    local h=$1 deadline=$((SECONDS+60)) event action pane action_status
    printf '%s\n' "$BATTY_CONTROL" > "$BATTY_PROVIDER_TEST/endpoint"
    while [[ ! -f $BATTY_PROVIDER_TEST/stop ]]; do
        ((SECONDS<deadline)) || return 1
        batty workspace pump "$h" -t 5
        while batty workspace action "$h" -V event; do
            read -r action pane <<< "$event"
            if kilix_action "$action" "$pane"; then action_status=0; else action_status=$?; fi
            printf '%s %s\n' "$action" "$action_status" >> "$BATTY_PROVIDER_TEST/actions"
        done
        if [[ -f $BATTY_PROVIDER_TEST/capture ]]; then
            rm "$BATTY_PROVIDER_TEST/capture"
            batty workspace capture "$h" "$BATTY_PROVIDER_TEST/frame.ppm"
            : > "$BATTY_PROVIDER_TEST/captured"
        fi
    done
}
