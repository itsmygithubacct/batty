#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config=${BATTY_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/batty/config.bash}
if [[ -r $config ]]; then
    # User-owned configuration intentionally supplies Bash variables/functions.
    # shellcheck source=/dev/null
    source "$config"
fi
cols=${BATTY_COLS:-100}
rows=${BATTY_ROWS:-30}
font=${BATTY_FONT:-monospace}
font_size=${BATTY_FONT_SIZE:-16}
gpu_info=false
headless=false
session_mode=ephemeral
session_name=
session_dir=${BATTY_SESSION_DIR:-}
while (($#)); do
    case $1 in
        --help|-h)
            printf '%s\n' 'Usage: batty [--session NAME | --attach NAME | --observe NAME] [--session-dir ROOT] [--gpu-info] [--cols N] [--rows N] [--font FAMILY] [--font-size PX] [--headless] [-- COMMAND ARG ...]' \
                'batty --list [--session-dir ROOT] lists named sessions.' \
                'batty --terminate NAME [--session-dir ROOT] stops a named session.' \
                'Closing a named window detaches; --session creates or reconnects, --attach only reconnects.' \
                'batty --runtime-info prints the managed bash-os revision and executable.' \
                'Default command: bash-os. Copy: Ctrl+Shift+C. Paste: Ctrl+Shift+V.' \
                'Scroll: mouse wheel or Shift+PageUp/PageDown. Hold Shift to select in mouse-aware programs.'
            exit 0 ;;
        --) shift; break ;;
        --gpu-info) gpu_info=true; shift ;;
        --headless) headless=true; shift ;;
        --session|--attach|--observe|--terminate)
            (($#>=2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            [[ $session_mode == ephemeral ]] || { printf '%s\n' 'Choose one session operation.' >&2; exit 2; }
            session_mode=${1#--}; session_name=$2
            shift 2 ;;
        --list)
            [[ $session_mode == ephemeral ]] || { printf '%s\n' 'Choose one session operation.' >&2; exit 2; }
            session_mode=list; shift ;;
        --cols|--rows|--font|--font-size|--session-dir)
            (($#>=2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            case $1 in --cols) cols=$2;; --rows) rows=$2;; --font) font=$2;; --font-size) font_size=$2;; --session-dir) session_dir=$2;; esac
            shift 2 ;;
        *) break ;;
    esac
done
[[ -f $root/build/batty.so ]] || { printf '%s\n' 'Build Batty first with ./build.sh.' >&2; exit 2; }
enable -f "$root/build/batty.so" batty
session_options=()
[[ -z $session_dir ]] || session_options+=(--session-dir "$session_dir")
case $session_mode in
    list|terminate)
        (($#==0)) || { printf '%s\n' 'Administrative commands do not take a child command.' >&2; exit 2; }
        if [[ $session_mode == list ]]; then batty list "${session_options[@]}"
        else batty terminate "${session_options[@]}" -- "$session_name"; fi
        exit 0 ;;
    attach|observe)
        (($#==0)) || { printf '%s\n' 'Attach uses the existing session command.' >&2; exit 2; } ;;
esac
terminal=
cleanup() {
    local status=$?
    trap - EXIT
    [[ -z $terminal ]] || batty close "$terminal" || :
    if [[ -n $terminal && $session_mode != ephemeral ]]; then
        printf 'Reconnect: %q' "$root/batty" >&2
        if [[ -n $session_dir ]]; then printf ' --session-dir %q' "$session_dir" >&2; fi
        if [[ $session_mode == observe ]]; then printf ' --observe %q\n' "$session_name" >&2
        else printf ' --attach %q\n' "$session_name" >&2; fi
    fi
    return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
window_options=(--font "$font" --font-size "$font_size")
if "$headless"; then window_options+=(--headless); fi
if [[ $session_mode == attach || $session_mode == observe ]]; then
    if [[ $session_mode == observe ]]; then window_options+=(--observe); fi
    batty attach -h terminal "${session_options[@]}" "${window_options[@]}" -- "$session_name"
else
    if (($#==0)); then set -- "${BATTY_SHELL:-$BASH}"; fi
    if [[ $session_mode == session ]]; then window_options+=(--session "$session_name" "${session_options[@]}"); fi
    batty new -h terminal -W "$cols" -H "$rows" "${window_options[@]}" -- "$@"
fi
if "$gpu_info"; then batty graphics "$terminal"; fi
event=tick
while :; do
    if batty pump "$terminal" -t 16 -V event; then
        if [[ $event != tick ]] && declare -F batty_on_event >/dev/null; then
            batty_on_event "$event" "$terminal"
        fi
    else
        result=$?
        ((result==1)) || exit "$result"
        batty status "$terminal" -V result
        exit "$result"
    fi
done
