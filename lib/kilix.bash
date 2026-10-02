#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
# The bundled decoder supports Kilix overwrite composition with N=2.
export KITTY_KILIX_RENDERING=1
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
config=${BATTY_KILIX_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/batty/kilix.bash}
if [[ -r $config ]]; then
    # User-owned Bash configuration, separate from the Kitty-based Kilix install.
    # shellcheck source=/dev/null
    source "$config"
fi
export BATTY_CLIPBOARD=${BATTY_CLIPBOARD:-write}
# Kilix's local frame SDK still publishes one-shot t=t files. The owner
# decodes those on its own host; standalone Batty leaves file reads disabled.
export BATTY_KITTY_LOCAL_FILES=${BATTY_KITTY_LOCAL_FILES:-1}
width=${BATTY_WIDTH:-1000} height=${BATTY_HEIGHT:-700}
font=${BATTY_FONT:-monospace} size=${BATTY_FONT_SIZE:-$(python3 -B "$root/tools/kilix_screen_size.py" --default)}
window_title=${BATTY_KILIX_WINDOW_TITLE:-'Kilix — Batty'}
initial_title='' initial_pane_title=''
skip_initial_recovery=${BATTY_KILIX_SKIP_INITIAL_RECOVERY:-0}
unset BATTY_KILIX_SKIP_INITIAL_RECOVERY
session_options=()
session_names=() session_roles=()
session_mode=run
restore_file=''
auto_restore=0
restart_programs=0
ephemeral=0
while (($#)); do
    case $1 in
        --help|-h)
            printf '%s\n' 'Usage: kilix [--width PX] [--height PX] [--font FAMILY] [--font-size PX] [--session NAME | (--attach NAME | --observe NAME)...] [--session-dir ROOT] [-- COMMAND ARG ...]' \
                'Batty Kilix frontend: Ctrl+Shift+T new page; Ctrl+Shift+Arrow split; Alt+Arrow focus.' \
                'Ctrl+Shift+Enter maximize/restore; Ctrl+Shift+W close pane; Ctrl+Tab next page.' \
                'Ctrl+Shift+S selects a pane for synchronized typing with other selected panes.' \
                'Inside a pane: kilix ls --panes; kilix focus pane:ID; kilix watch --once ID; kilix new-pane left -- COMMAND; kilix new-page -- COMMAND.' \
                'Pane Center: F12 or kilix panes; kilix panes --json; kilix panes dump ID --lines 40; kilix panes send ID --enter TEXT.' \
                'Graphical applications: kilix run [--size WIDTHxHEIGHT] [--fps N] -- COMMAND ARG ...' \
                'Disposable app window: kilix --ephemeral -- COMMAND ARG ...' \
                'Installed applications: kilix apps list [--json]; kilix apps open DESKTOP_ID [-- FILE_OR_URL ...]' \
                'Save layout and output: kilix save [--socket PATH] FILE; restore: kilix --restore FILE' \
                'Lost owners start fresh shells; --restore FILE --restart-programs explicitly reruns saved commands.' \
                'Desktop launcher: kilix --install-desktop; kilix --uninstall-desktop' \
                'Ordinary panes and recent layouts recover after frontend loss; pane close ends their owners.' \
                'Closing explicitly named panes detaches; use batty --terminate NAME to stop their sessions.'
            exit 0 ;;
        --) shift; break ;;
        --restore)
            if (($#<2)) || [[ -z ${2:-} || -n $restore_file ]]; then printf '%s\n' 'Supply one restore file.' >&2; exit 2; fi
            restore_file=$2; shift 2 ;;
        --restart-programs) restart_programs=1; shift ;;
        --ephemeral) ephemeral=1; shift ;;
        --width|--height|--font|--font-size|--window-title|--initial-title|--initial-pane-title|--title|--session|--attach|--observe|--session-dir)
            (($#>=2)) || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
            case $1 in
                --width) width=$2;; --height) height=$2;; --font) font=$2;; --font-size) size=$2;;
                --window-title) window_title=$2;; --initial-title|--title) initial_title=$2;; --initial-pane-title) initial_pane_title=$2;;
                --session)
                    [[ $session_mode == run ]] || { printf '%s\n' 'Choose one session operation.' >&2; exit 2; }
                    session_mode=session; session_options+=(--session "$2");;
                --attach|--observe)
                    [[ $session_mode == run || $session_mode == attach || $session_mode == observe ]] || {
                        printf '%s\n' 'Cannot combine a new session with attached sessions.' >&2; exit 2; }
                    ((${#session_names[@]}<64)) || { printf '%s\n' 'At most 64 attached panes are allowed.' >&2; exit 2; }
                    session_mode=${1#--}
                    session_roles+=("$session_mode") session_names+=("$2");;
                --session-dir) export BATTY_SESSION_DIR=$2;;
            esac
            shift 2 ;;
        -*) printf 'Unknown option: %s\n' "$1" >&2; exit 2 ;;
        *) break ;;
    esac
done
if ((restart_programs)) && [[ -z $restore_file ]]; then
    printf '%s\n' '--restart-programs requires an explicit --restore file.' >&2
    exit 2
fi
if ((ephemeral)); then
    if [[ $session_mode != run || -n $restore_file || $# == 0 ]]; then
        printf '%s\n' 'An ephemeral frontend needs one new command.' >&2
        exit 2
    fi
    skip_initial_recovery=1
fi
# The native loadable sees shell variables; the snapshot helper needs the same
# root through its environment even when user configuration did not export it.
[[ -z ${BATTY_SESSION_DIR:-} ]] || export BATTY_SESSION_DIR
[[ -z ${XDG_RUNTIME_DIR:-} ]] || export XDG_RUNTIME_DIR
recovery_directory=${BATTY_KILIX_RECOVERY_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/batty/recovery}
if (( !ephemeral )) && [[ ${BATTY_KILIX_AUTO_RECOVER:-1} != 0 || -n ${BATTY_KILIX_RECOVERY_DIR:-} || -e $recovery_directory || -L $recovery_directory ]]; then
    export BATTY_KILIX_RECOVERY_DIR=$recovery_directory
    # Prepare configured or existing recovery storage before an owner starts.
    # Disabling recovery on fresh state does not create durable state directories.
    python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); from kilix_recovery import recovery_directory; recovery_directory(create=True)' "$root/tools"
else
    unset BATTY_KILIX_RECOVERY_DIR
fi
enable -f "$root/build/batty.so" batty
if [[ -z $restore_file && $session_mode == run && $# == 0 && $skip_initial_recovery != 1 && ${BATTY_KILIX_AUTO_RECOVER:-1} != 0 ]]; then
    orphan_listing=$(batty list) || orphan_listing=''
    restore_file=$(printf '%s\n' "$orphan_listing" | python3 "$root/tools/kilix_auto_workspace.py" _select) || restore_file=''
    [[ -z $restore_file ]] || auto_restore=1
fi
restore_records=()
if [[ -n $restore_file ]]; then
    [[ $session_mode == run && $# == 0 ]] || { printf '%s\n' 'Restore cannot be combined with a session operation or command.' >&2; exit 2; }
    restore_operation=_restore
    (( !auto_restore )) || restore_operation=_restore-auto
    mapfile -d '' -t restore_records < <(python3 "$root/tools/kilix_workspace.py" "$restore_operation" "$restore_file")
    if ((${#restore_records[@]} >= 16)) && [[ ${restore_records[-1]} == DONE ]]; then
        width=${restore_records[0]} height=${restore_records[1]} font=${restore_records[2]} size=${restore_records[3]}
    elif ((auto_restore)); then
        # All remaining panes may have closed since snapshot selection.
        restore_file='' auto_restore=0 restore_records=()
    else
        exit 2
    fi
fi
recording=$(python3 "$root/tools/kilix_settings.py" --recording)
read -r BATTY_TRANSCRIPT_ENABLED recording_limit recording_graphics <<< "$recording"
# Preserve explicit override bytes. Invalid values fail only the disk worker;
# recording configuration must not prevent the terminal from starting.
BATTY_TRANSCRIPT_LIMIT=${BATTY_TRANSCRIPT_LIMIT:-$recording_limit}
BATTY_TRANSCRIPT_GRAPHICS=${BATTY_TRANSCRIPT_GRAPHICS:-$recording_graphics}
BATTY_TRANSCRIPT_DIR=${BATTY_TRANSCRIPT_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/batty/transcripts}
# The isolated disk worker creates private directories after PTY startup.
export BATTY_TRANSCRIPT_DIR BATTY_TRANSCRIPT_CREATE_ROOT=1
export BATTY_TRANSCRIPT_ENABLED BATTY_TRANSCRIPT_LIMIT BATTY_TRANSCRIPT_GRAPHICS
workspace='' initial='' event='' listing='' status='' control_dir='' overlay_pid='' autosave_pid=''
cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n $overlay_pid ]]; then
        kill "$overlay_pid" 2>/dev/null || :
        wait "$overlay_pid" 2>/dev/null || :
    fi
    [[ -z $workspace ]] || batty workspace close "$workspace" || :
    if [[ -n $autosave_pid ]]; then
        kill "$autosave_pid" 2>/dev/null || :
        wait "$autosave_pid" 2>/dev/null || :
    fi
    [[ -z $control_dir ]] || rmdir "$control_dir" || :
    return "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
application_ids=()
declare -A application_panes=()
declare -A automatic_panes=()
declare -A automatic_roots=()
declare -A automatic_closed=()
kilix_automatic_name() {
    python3 -c 'import secrets; print("kilix-auto-" + secrets.token_hex(12))'
}
kilix_automatic_add() {
    local result=$1; shift
    local name
    name=$(kilix_automatic_name) || return
    batty workspace add "$workspace" -V "$result" --session "$name" "$@" || return
    local -n created=$result
    kilix_automatic_track "$created"
}
kilix_automatic_terminate() {
    local pane=$1 name=${automatic_panes[$1]-}
    [[ -n $name ]] || return 0
    [[ -z ${automatic_closed[$pane]-} ]] || return 0
    batty terminate --session-dir "${automatic_roots[$pane]}" "$name" || return
    # Page closure can fail on a later owner. Keep this completion until the
    # pane is removed so a retry does not try to stop a vanished owner again.
    automatic_closed[$pane]=1
}
kilix_automatic_track() {
    local pane=$1 name='' dir=''
    [[ -n ${automatic_panes[$pane]-} ]] && return 0
    batty workspace session-name "$workspace" "$pane" -V name || return
    [[ $name =~ ^kilix-auto-[0-9a-f]{24}$ ]] || return 0
    batty workspace session-dir "$workspace" "$pane" -V dir || return
    [[ $dir == /* ]] || return 1
    automatic_panes[$pane]=$name
    automatic_roots[$pane]=$dir
}
kilix_automatic_close() {
    local pane=$1
    kilix_automatic_track "$pane" || return
    kilix_automatic_terminate "$pane" || return
    batty workspace remove "$workspace" "$pane" || return
    unset 'automatic_panes[$pane]' 'automatic_roots[$pane]' 'automatic_closed[$pane]'
}
kilix_request_close() {
    local pane=$1 status
    kilix_automatic_track "$pane" || return
    if [[ -n ${automatic_panes[$pane]-} ]]; then
        batty workspace status "$workspace" "$pane" -V status || return
        if [[ $status == running ]]; then
            batty workspace confirm "$workspace" "$pane" close-confirmed \
                "Terminate running pane $pane? This ends its process."
            return
        fi
    fi
    kilix_automatic_close "$pane"
}
kilix_request_close_page() {
    local pane=$1 listing row tab target_tab window active visible sync rest status
    batty workspace panes "$workspace" -V listing || return
    while read -r row tab window active visible sync rest; do
        if [[ $row == "$pane" ]]; then target_tab=$tab; break; fi
    done <<<"$listing"
    [[ -n ${target_tab:-} ]] || return 1
    while read -r row tab window active visible sync rest; do
        [[ $tab == "$target_tab" ]] || continue
        kilix_automatic_track "$row" || return
        [[ -n ${automatic_panes[$row]-} ]] || continue
        batty workspace status "$workspace" "$row" -V status || return
        if [[ $status == running ]]; then
            batty workspace confirm "$workspace" "$pane" close-page-confirmed \
                "Terminate running processes on this page?"
            return
        fi
    done <<<"$listing"
    kilix_automatic_close_page "$pane"
}
kilix_automatic_close_page() {
    local pane=$1 listing row tab target_tab window active visible sync rest
    local -a closing=()
    batty workspace panes "$workspace" -V listing || return
    while read -r row tab window active visible sync rest; do
        if [[ $row == "$pane" ]]; then target_tab=$tab; break; fi
    done <<<"$listing"
    [[ -n ${target_tab:-} ]] || return 1
    while read -r row tab window active visible sync rest; do
        if [[ $tab == "$target_tab" ]]; then kilix_automatic_track "$row" || return; fi
        if [[ $tab == "$target_tab" && -n ${automatic_panes[$row]-} ]]; then closing+=("$row"); fi
    done <<<"$listing"
    for row in "${closing[@]}"; do kilix_automatic_terminate "$row" || return; done
    batty workspace close-page "$workspace" "$pane" || return
    for row in "${closing[@]}"; do
        unset 'automatic_panes[$row]' 'automatic_roots[$row]' 'automatic_closed[$row]'
    done
}
kilix_recover_automatic() {
    [[ ${BATTY_KILIX_AUTO_RECOVER:-1} != 0 ]] || return 0
    local listing line name rest restored
    # Automatic snapshots use this session root. Do not attach a controller
    # to an owner already represented in the restored layout by observers.
    local -A represented=()
    for name in "$@"; do represented[$name]=1; done
    listing=$(batty list) || return
    while IFS= read -r line; do
        name=${line%% *}
        [[ $name =~ ^kilix-auto-[0-9a-f]{24}$ && $line == *' controllers=0 '* ]] || continue
        [[ -z ${represented[$name]-} ]] || continue
        if batty workspace add "$workspace" -V restored --attach --session "$name"; then
            kilix_automatic_track "$restored" || return
            application_panes[$restored]=held
            [[ -n $initial ]] || initial=$restored
        fi
    done <<<"$listing"
}
kilix_reload_catalog() {
    local -a records=()
    local i
    mapfile -d '' -t records < <(python3 "$root/tools/kilix_catalog.py" _menu)
    ((${#records[@]} % 2 == 1)) && [[ ${records[-1]} == DONE ]] || return 1
    batty workspace app-menu "$workspace" || return
    application_ids=()
    for ((i=0; i+1<${#records[@]}; i+=2)); do
        batty workspace app-menu "$workspace" "${records[i]}" || return
        application_ids+=("${records[i+1]}")
    done
}
kilix_reload_settings() {
    local presentation recording_settings bar_edge button_mask start_menu
    local recording_enabled recording_size recording_graphics recording_recent recording_archive
    presentation=$(python3 "$root/tools/kilix_settings.py" --chrome) || return
    read -r bar_edge button_mask start_menu <<< "$presentation"
    batty workspace chrome-edge "$workspace" "$bar_edge" || return
    batty workspace chrome-buttons "$workspace" "$button_mask" || return
    batty workspace start-badge "$workspace" "$start_menu" || return
    recording_settings=$(python3 "$root/tools/kilix_settings.py" --recording-settings) || return
    read -r recording_enabled recording_size recording_graphics recording_recent recording_archive <<< "$recording_settings"
    batty workspace settings-recording "$workspace" "$recording_enabled" "$recording_size" \
        "$recording_graphics" "$recording_recent" "$recording_archive" || return
    kilix_reload_catalog
}
kilix_change_setting() {
    local diagnostic
    if diagnostic=$(python3 "$root/tools/settings_cli.py" --set "$1=$2" 2>&1); then
        if kilix_reload_settings; then
            batty workspace settings-result "$workspace" saved
        else
            batty workspace settings-result "$workspace" reload-failed
            return 1
        fi
    else
        printf '%s\n' "$diagnostic" >&2
        batty workspace settings-result "$workspace" save-failed
        return 1
    fi
}
kilix_action() {
    local action=$1 pane=$2 _new neighbor listing id _tab _window _active _visible sync _rest
    case $action in
        reload-settings) kilix_reload_settings;;
        remote-reload-settings)
            local reload_status=0
            kilix_reload_settings || reload_status=$?
            batty workspace reload-complete "$workspace" "$pane" "$reload_status" || return
            return "$reload_status";;
        start-menu) batty workspace menu "$workspace";;
        settings)
            local -a settings_options=()
            [[ -z $BATTY_TAB_BAR_EDGE ]] || settings_options+=(--edge-locked)
            kilix_reload_settings || return
            batty workspace settings "$workspace" "${settings_options[@]}";;
        setting-edge-top|setting-edge-bottom)
            kilix_change_setting tab_bar_edge "${action#setting-edge-}";;
        setting-button-[0-8]-on|setting-button-[0-8]-off)
            local setting=${action#setting-button-} index value
            index=${setting%%-*} value=${setting#*-}
            local -a buttons=(synchronize_input font_decrease font_increase split_left split_up split_down split_right maximize close)
            kilix_change_setting "button_${buttons[index]}" "$value";;
        setting-transcript*)
            local setting=${action#setting-}
            kilix_change_setting "${setting%-*}" "${setting##*-}";;
        application-*)
            local app_index=${action#application-}
            [[ $app_index =~ ^[0-9]+$ ]] && ((app_index<${#application_ids[@]})) || return 1
            kilix_automatic_add _new -- python3 "$root/tools/kilix_catalog.py" open --in-place -- "${application_ids[app_index]}" || return
            application_panes[$_new]=running;;
        new-page) kilix_automatic_add _new -- "${BATTY_SHELL:-$BASH}";;
        split-*) kilix_automatic_add _new --target "$pane" --direction "${action#split-}" -- "${BATTY_SHELL:-$BASH}";;
        focus-*)
            batty workspace neighbor "$workspace" "$pane" "${action#focus-}" -V neighbor || return
            batty workspace focus "$workspace" "$neighbor";;
        close) kilix_request_close "$pane";;
        close-confirmed) kilix_automatic_close "$pane";;
        focus) batty workspace focus "$workspace" "$pane";;
        font-smaller) batty workspace font "$workspace" "$pane" -1;;
        font-larger) batty workspace font "$workspace" "$pane" 1;;
        read-aloud) "$root/kilix" speak --socket "$BATTY_CONTROL" --pane "$pane" & ;;
        dictate-pane) "$root/kilix" dictate --socket "$BATTY_CONTROL" --pane "$pane" & ;;
        stop-voice) "$root/kilix" voice stop & ;;
        next-layout) batty workspace layout "$workspace" "$pane" next;;
        rename-page) batty workspace rename-prompt "$workspace" "$pane";;
        choose-panes) batty workspace focus "$workspace" "$pane" || return; batty workspace choose "$workspace" panes;;
        choose-pages) batty workspace choose "$workspace" pages;;
        choose-all) batty workspace choose "$workspace" all;;
        reset-sizes) batty workspace reset-sizes "$workspace" "$pane";;
        resize-mode) batty workspace resize-mode "$workspace" "$pane";;
        zoom) batty workspace zoom "$workspace" "$pane";;
        next-page) batty workspace tab "$workspace" 1;;
        previous-page) batty workspace tab "$workspace" -1;;
        next-pane|previous-pane|last-pane|swap-next|swap-previous)
            batty workspace focus "$workspace" "$pane" || return
            batty workspace navigate "$workspace" "$action";;
        last-page) batty workspace navigate "$workspace" "$action";;
        page-[1-9]) batty workspace navigate "$workspace" page "${action#page-}";;
        close-page) kilix_request_close_page "$pane";;
        close-page-confirmed) kilix_automatic_close_page "$pane";;
        sync)
            batty workspace panes "$workspace" -V listing || return
            while read -r id _tab _window _active _visible sync _rest; do
                if [[ $id == "$pane" ]]; then batty workspace sync "$workspace" "$pane" "$((1-sync))"; return; fi
            done <<<"$listing"
            return 1;;
        *) printf 'Unknown Kilix action: %s\n' "$action" >&2; return 2;;
    esac
}
# Keep failures visible in desktop launches while preserving the live workspace.
kilix_dispatch() {
    local action=$1 pane=$2 status
    if kilix_action "$action" "$pane"; then return 0; else status=$?; fi
    printf 'Kilix action failed: %s (status %s)\n' "$action" "$status" >&2
    case $action in
        setting-*) ;; # The settings overlay already shows save/reload feedback.
        *) batty workspace message "$workspace" "Action failed: $action" || : ;;
    esac
    return "$status"
}
# Application startup diagnostics must survive an asynchronous child failure.
# Tracking belongs to frontend launch policy, not to the terminal parser.
kilix_collect_exits() {
    local listing pane rest status id
    local -A live=()
    batty workspace panes "$workspace" -V listing || return
    while read -r pane rest; do
        [[ -n $pane ]] || continue
        live[$pane]=1
        batty workspace status "$workspace" "$pane" -V status || return
        [[ $status != running ]] || continue
        if [[ ${application_panes[$pane]-} == held ]]; then continue; fi
        kilix_automatic_track "$pane" || return
        last_status=$status
        if [[ -n ${application_panes[$pane]-} && $status != 0 ]]; then
            application_panes[$pane]=held
            batty workspace message "$workspace" "Application in pane $pane exited with status $status. Pane kept open." || return
        else
            kilix_automatic_close "$pane" || return
            unset 'application_panes[$pane]'
        fi
    done <<<"$listing"
    for id in "${!application_panes[@]}"; do
        [[ -n ${live[$id]-} ]] || unset 'application_panes[$id]'
    done
    for id in "${!automatic_panes[@]}"; do
        if [[ -z ${live[$id]-} ]]; then
            unset 'automatic_panes[$id]'
            unset 'automatic_roots[$id]'
        fi
    done
    return 0
}
# Prepare the private control path before video initialization. A hardware X11
# driver crashed when the later mktemp substitution exited with a live GLES context.
BATTY_CONTROL_DIR=$(python3 "$root/tools/control_paths.py")
export BATTY_CONTROL_DIR
control_dir=$(mktemp -d "$BATTY_CONTROL_DIR/front-XXXXXXXX")
export BATTY_CONTROL=$control_dir/control.sock
# Scope SDL's process identity to video initialization; do not export the
# terminal host's class into independently launched pane applications.
SDL_VIDEO_X11_WMCLASS=batty-kilix SDL_VIDEO_WAYLAND_WMCLASS=batty-kilix \
batty workspace new -h workspace --title "$window_title" --width "$width" --height "$height" --font "$font" --font-size "$size"
batty workspace chrome "$workspace" 1
export BATTY_TAB_BAR_EDGE=${BATTY_TAB_BAR_EDGE:-}
kilix_reload_settings
if [[ -n $restore_file ]]; then
    batty workspace layout-check "$workspace" "${restore_records[8]}" -V restore_ids
    batty workspace chrome-buttons "$workspace" "${restore_records[7]}"
    batty workspace chrome-edge "$workspace" "$([[ ${restore_records[5]} == 1 ]] && printf bottom || printf top)"
    batty workspace start-badge "$workspace" "${restore_records[6]}"
    batty workspace chrome "$workspace" "${restore_records[4]}"
fi
batty workspace listen "$workspace" "$BATTY_CONTROL" --terminate-prefix kilix-auto- --host-actions
python3 -B "$root/tools/kilix_overlay.py" >/dev/null 2>&1 &
overlay_pid=$!
for direction in left right up down; do
    batty workspace bind "$workspace" "Ctrl+Shift+${direction^}" "split-$direction"
    batty workspace bind "$workspace" "Alt+${direction^}" "focus-$direction"
done
batty workspace bind "$workspace" Ctrl+Shift+T new-page
batty workspace bind "$workspace" Ctrl+Shift+W close
batty workspace bind "$workspace" Ctrl+Shift+Return zoom
batty workspace bind "$workspace" Ctrl+Shift+S sync
batty workspace bind "$workspace" Ctrl+Shift+R resize-mode
batty workspace bind "$workspace" Ctrl+Shift+F5 reload-settings
batty workspace bind "$workspace" F12 choose-all
batty workspace bind "$workspace" Ctrl+Alt+S settings
batty workspace bind "$workspace" Ctrl+Alt+M start-menu
batty workspace bind "$workspace" Ctrl+Alt+R read-aloud
batty workspace bind "$workspace" Ctrl+Alt+D dictate-pane
batty workspace bind "$workspace" Ctrl+Alt+X stop-voice
batty workspace bind "$workspace" Ctrl+Shift+Home reset-sizes
for direction in left right up down; do
    batty workspace bind "$workspace" Ctrl+Shift+B "${direction^}" "focus-$direction"
done
for binding in 'O next-pane' '; last-pane' 'Z zoom' 'X close' 'C new-page' \
               'N next-page' 'P previous-page' 'L last-page' 'R resize-mode' 'Q choose-panes' 'W choose-pages' ', rename-page' 'Space next-layout' \
               'Shift+[ swap-previous' 'Shift+] swap-next' 'Shift+5 split-right' 'Shift+7 close-page'; do
    read -r chord action <<< "$binding"
    batty workspace bind "$workspace" Ctrl+Shift+B "$chord" "$action"
done
batty workspace bind "$workspace" Ctrl+Shift+B "Shift+'" split-down
for page in {1..9}; do batty workspace bind "$workspace" Ctrl+Shift+B "$page" "page-$page"; done
batty workspace bind "$workspace" Ctrl+Tab next-page
batty workspace bind "$workspace" Ctrl+Shift+Tab previous-page
if [[ -n $restore_file ]]; then
    restore_map=() restore_sessions=() restore_group=0 restored=''
    declare -A recovered_names=() recovered_epochs=()
    for ((restore_i=0; restore_i<restore_records[9]; ++restore_i)); do
        restore_at=$((10+restore_i*5))
        restore_options=(--attach)
        [[ ${restore_records[restore_at+4]} == 0 ]] || restore_options=(--observe)
        if ((restore_i % 4)); then restore_options+=(--target "$restore_group" --direction right); fi
        restore_owner="${restore_records[restore_at+1]}/${restore_records[restore_at+2]}:${restore_records[restore_at+3]}"
        if [[ -n ${recovered_names[$restore_owner]-} ]]; then
            batty workspace add "$workspace" -V restored "${restore_options[@]}" \
                --session-dir "${restore_records[restore_at+1]}" --session "${recovered_names[$restore_owner]}" \
                --epoch "${recovered_epochs[$restore_owner]}"
        elif batty workspace add "$workspace" -V restored "${restore_options[@]}" \
            --session-dir "${restore_records[restore_at+1]}" --session "${restore_records[restore_at+2]}" --epoch "${restore_records[restore_at+3]}"; then
            :
        else
            recovery_records=() recovery_request=()
            (( !restart_programs )) || recovery_request+=(--restart-programs)
            mapfile -d '' -t recovery_records < <(python3 "$root/tools/kilix_workspace.py" _recover \
                "$restore_file" "${restore_records[restore_at]}" "${recovery_request[@]}")
            ((${#recovery_records[@]} >= 5)) && [[ ${recovery_records[-1]} == DONE ]] || exit 2
            recovery_options=(--session-dir "${restore_records[restore_at+1]}" --session "${recovery_records[2]}")
            if ((restore_i % 4)); then recovery_options+=(--target "$restore_group" --direction right); fi
            recovery_command=("${BATTY_SHELL:-$BASH}")
            if ((restart_programs)); then recovery_command=("${recovery_records[@]:4:${recovery_records[3]}}"); fi
            recovery_cwd=$PWD
            if [[ -n ${recovery_records[1]} && -d ${recovery_records[1]} ]]; then cd -- "${recovery_records[1]}"; fi
            if BATTY_RECOVERY_FILE=${recovery_records[0]} batty workspace add "$workspace" -V restored \
                "${recovery_options[@]}" -- "${recovery_command[@]}"; then
                cd -- "$recovery_cwd"
            else
                cd -- "$recovery_cwd"
                exit 2
            fi
            recovered_names[$restore_owner]=${recovery_records[2]}
            batty workspace session-epoch "$workspace" "$restored" -V recovery_epoch
            recovered_epochs[$restore_owner]=$recovery_epoch
            if [[ ${restore_records[restore_at+4]} == 1 ]]; then
                # Creation temporarily claims the controller role. Release it
                # before rebuilding a saved observer, including observer-only
                # workspaces, without starting another process.
                batty workspace remove "$workspace" "$restored"
                batty workspace add "$workspace" -V restored "${restore_options[@]}" \
                    --session-dir "${restore_records[restore_at+1]}" --session "${recovery_records[2]}" --epoch "$recovery_epoch"
            fi
            kilix_automatic_track "$restored"
        fi
        if ((restore_i % 4 == 0)); then restore_group=$restored; fi
        restore_map+=("${restore_records[restore_at]}" "$restored")
        restore_sessions+=("${recovered_names[$restore_owner]-${restore_records[restore_at+2]}}")
        # Restored owners retain their diagnostic output even if already exited.
        application_panes[$restored]=held
    done
    restore_layout_options=()
    (( !auto_restore )) || restore_layout_options+=(--prune)
    batty workspace layout-apply "$workspace" "${restore_records[8]}" "${restore_layout_options[@]}" "${restore_map[@]}"
    batty workspace window-size "$workspace" "$width" "$height"
    batty workspace active "$workspace" -V initial
    if ((auto_restore)); then kilix_recover_automatic "${restore_sessions[@]}"; fi
elif [[ $session_mode == attach || $session_mode == observe ]]; then
    (($#==0)) || { printf '%s\n' 'Attach uses the existing session command.' >&2; exit 2; }
    attach_group='' attached=''
    for ((attach_i=0; attach_i<${#session_names[@]}; ++attach_i)); do
        attach_options=(--"${session_roles[attach_i]}" --session "${session_names[attach_i]}")
        if ((attach_i % 4)); then attach_options+=(--target "$attach_group" --direction right); fi
        batty workspace add "$workspace" -V attached "${attach_options[@]}"
        if ((attach_i % 4 == 0)); then attach_group=$attached; fi
        if ((attach_i == 0)); then initial=$attached; fi
        application_panes[$attached]=held
    done
else
    if [[ $session_mode == run && $skip_initial_recovery != 1 ]]; then kilix_recover_automatic; fi
    if (($#==0)); then
        if [[ -z $initial ]]; then
            default_desktop=$(python3 -B "$root/tools/kilix_default_desktop.py" _startup)
            if [[ $default_desktop == none ]]; then
                set -- "${BATTY_SHELL:-$BASH}"
            else
                set -- python3 -B "$root/tools/kilix_default_boot.py" "$default_desktop" "${BATTY_SHELL:-$BASH}"
            fi
        else
            set --
        fi
    fi
    if (($#)); then
        if [[ $session_mode == session ]]; then
            batty workspace add "$workspace" -V initial "${session_options[@]}" -- "$@"
        elif ((ephemeral)); then
            batty workspace add "$workspace" -V initial -- "$@"
        else
            kilix_automatic_add initial -- "$@"
        fi
    fi
fi
if [[ -z $restore_file && -n $initial ]]; then
    [[ -z $initial_title ]] || batty workspace rename "$workspace" "$initial" "$initial_title"
    [[ -z $initial_pane_title ]] || batty workspace pane-rename "$workspace" "$initial" "$initial_pane_title"
fi
# The helper observes the external control endpoint while the Bash host pumps
# it; its private snapshot preserves layouts across a sudden frontend loss.
if (( !ephemeral )) && [[ ${BATTY_KILIX_AUTO_RECOVER:-1} != 0 ]]; then
    autosave_parent=$BASHPID
    recovered_snapshot=''
    (( !auto_restore )) || recovered_snapshot=$restore_file
    python3 -B "$root/tools/kilix_auto_workspace.py" _run "$BATTY_CONTROL" "$autosave_parent" "$recovered_snapshot" >/dev/null 2>&1 &
    autosave_pid=$!
fi
# Hooks can customize bindings and launch more panes through the same builtin.
if [[ -n ${BATTY_TRANSCRIPT_DIR:-} && ${BATTY_TRANSCRIPT_MAINTENANCE:-1} != 0 ]]; then
    maintenance_parent=$BASHPID
    python3 "$root/tools/transcript_maintenance.py" "$maintenance_parent" >/dev/null &
fi
last_status=0
if declare -F kilix_on_ready >/dev/null; then kilix_on_ready "$workspace" "$initial"; fi
while :; do
    if batty workspace pump "$workspace" -t 16; then :
    else rc=$?; ((rc==1)) && exit "$last_status"; exit "$rc"; fi
    while batty workspace action "$workspace" -V event; do
        read -r action pane <<<"$event"
        if kilix_dispatch "$action" "$pane"; then :; else :; fi
    done
    kilix_collect_exits
done
