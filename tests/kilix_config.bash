# SPDX-License-Identifier: MIT
kilix_on_ready() {
    local handle=$1 first=$2 listing pane rest count=0 message
    [[ $BASH == "$BATTY_BASH" ]]
    [[ ${KITTY_KILIX_RENDERING:-} == 1 ]]
    if kilix_dispatch close 999999999; then return 1; fi
    batty workspace message "$handle" -V message
    [[ $message == 'Action failed: close' ]]
    batty workspace message "$handle" ''
    # Exercise asynchronous catalog failures through the real Start action path.
    local failed_pane deadline diagnostic
    local -x XDG_DATA_HOME=${BATTY_CONTROL%/*}/test-xdg XDG_DATA_DIRS=${BATTY_CONTROL%/*}/test-system
    local -a saved_ids=("${application_ids[@]}")
    application_ids=(batty-intentionally-missing.desktop)
    kilix_action application-0 "$first"
    batty workspace active "$handle" -V failed_pane
    deadline=$((SECONDS+5))
    while [[ ${application_panes[$failed_pane]-} != held ]]; do
        ((SECONDS<deadline)) || return 1
        batty workspace pump "$handle" -t 5
        kilix_collect_exits
    done
    batty workspace message "$handle" -V message
    [[ $message == *"pane $failed_pane exited with status 1"* ]]
    batty workspace dump "$handle" "$failed_pane" > "$PWD/build/kilix-failed-app.txt"
    diagnostic=$(<"$PWD/build/kilix-failed-app.txt")
    [[ $diagnostic == *'No installed application matches'* ]]
    batty workspace message "$handle" ''
    kilix_collect_exits
    batty workspace message "$handle" -V message
    [[ -z $message && ${application_panes[$failed_pane]} == held ]]
    kilix_action close "$failed_pane"
    kilix_collect_exits
    [[ -z ${application_panes[$failed_pane]-} ]]
    mkdir -p "$XDG_DATA_HOME/applications"
    printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Successful fixture' 'Terminal=true' 'Exec=/bin/true' > "$XDG_DATA_HOME/applications/success.desktop"
    application_ids=(success.desktop)
    kilix_action application-0 "$first"
    batty workspace active "$handle" -V failed_pane
    deadline=$((SECONDS+5))
    while [[ -n ${application_panes[$failed_pane]-} ]]; do
        ((SECONDS<deadline)) || return 1
        batty workspace pump "$handle" -t 5
        kilix_collect_exits
    done
    batty workspace message "$handle" -V message
    [[ -z $message ]]
    rm "$XDG_DATA_HOME/applications/success.desktop"
    rmdir "$XDG_DATA_HOME/applications" "$XDG_DATA_HOME"
    application_ids=("${saved_ids[@]}")
    batty workspace focus "$handle" "$first"
    kilix_action split-right "$first"
    kilix_action split-down "$first"
    kilix_action new-page "$first"
    batty workspace panes "$handle" -V listing
    while read -r pane rest; do count=$((count+1)); done <<<"$listing"
    [[ $count == 4 ]]
    kilix_action previous-page "$first"
    kilix_action zoom "$first"
    kilix_action zoom "$first"
    kilix_action sync "$first"
    batty workspace capture "$handle" "$PWD/build/kilix-controller.ppm"
    while read -r pane rest; do
        [[ $pane == "$first" ]] || kilix_automatic_close "$pane"
    done <<<"$listing"
    batty workspace send "$handle" "$first" $'ready\n'
    printf '%s\n' 'PASS managed Kilix controller: split, page, zoom, sync, close, capture and input'
}
