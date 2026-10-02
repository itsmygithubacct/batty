# SPDX-License-Identifier: MIT
kilix_on_ready() {
    local handle=$1 first=$2 second=''
    kilix_automatic_add second --target "$first" --direction right -- /bin/cat
    kilix_action close-page "$first"
    printf '%s\n' "$BATTY_CONTROL" >"${BATTY_AUTO_RECOVERY_READY:?}"
}
