# SPDX-License-Identifier: MIT
# These variables are consumed by the controller that sources this file.
# shellcheck disable=SC2034
BATTY_FONT=monospace
BATTY_FONT_SIZE=16
BATTY_COLS=100
BATTY_ROWS=30

# Hooks run in the window's controller and must return promptly.
batty_on_event() {
    case $1 in
        resize) batty info "$2" -V BATTY_LAST_GEOMETRY ;;
    esac
}
