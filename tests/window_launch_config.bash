# SPDX-License-Identifier: MIT
kilix_on_ready() {
    printf '%s %s\n' "$BATTY_CONTROL" "$2" > "${BATTY_WINDOW_LAUNCH_READY:?}"
}
