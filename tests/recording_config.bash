# SPDX-License-Identifier: MIT
kilix_on_ready() {
    printf '%s\n' "$BATTY_CONTROL" > "$BATTY_RECORDING_READY"
}
