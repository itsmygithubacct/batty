# Batty

A working Linux desktop terminal prototype for bash-os. Bash runs the
controller and configuration; a C loadable owns PTYs, input and the window.
The pinned libghostty-vt supplies terminal state and protocols. OpenGL ES 3
draws text with FreeType/HarfBuzz and displays native Kitty and Sixel images.
Named sessions keep the PTY, terminal parser and image state in a local
service so windows can detach and reconnect.

## Build and run

```sh
./build.sh
./batty
./batty -- vim
./batty --cols 120 --rows 36 --font-size 18
./batty --runtime-info
```

The controller and default interactive shell use **bash-os**, including its
full collection of compiled-in builtins. Each build and launch checks the
`origin` remote's default branch from `~/projects/bash-os`, fetches its latest
commit into Batty's private cache, and builds that exact revision when needed.
It also builds the native loadable against the matching Bash 5.3 headers.
The source checkout, its branches and existing build outputs are preserved.
The first launch can take several minutes while dependencies compile.

`./batty --runtime-info` reports the selected revision, executable, checksum
and builtin count. The same information is saved in `build/bash-os.json`.
The launcher never falls back to a system Bash. If the remote check fails,
launch fails; `BATTY_OFFLINE=1 ./batty` explicitly reuses the last successfully
paired build and verifies its checksum and builtins. Offline mode requires
an earlier successful online build.

Set `BATTY_BASH_OS_SOURCE` to use another local bash-os checkout,
`BATTY_BASH_OS_REMOTE` to choose its remote, or `BATTY_BASH_OS_REF` to choose
a remote branch/ref (default `HEAD`). Built runtime copies stay under
`.cache/bash-os/COMMIT/`; older copies remain usable by running shells.
`BATTY_BASH` and `SHELL` are set to the selected executable for the controller
and its children; an inherited `BATTY_BASH` does not override that selection.
Set `BATTY_SHELL` to explicitly choose a different child shell, or pass a
command after `--`. The controller skips Bash startup files; the interactive
child uses its normal startup files.

Build requirements: a C compiler, a host Bash for bootstrapping, Git, Python
3.12 or newer, pkg-config, SDL2, FreeType, HarfBuzz, Fontconfig, libpng and GLES
development files. The native supervisor requires Linux pidfd support and
glibc 2.34 or newer.
The desktop needs a working GLES 3 context and installed fonts.

For Debian/Ubuntu systems, the native dependencies are typically:

```sh
sudo apt install build-essential git python3 pkg-config libsdl2-dev \
  libfreetype6-dev libharfbuzz-dev libfontconfig1-dev libpng-dev libgles-dev \
  curl libpcre2-dev zlib1g-dev liblzma-dev libzstd-dev libbz2-dev
```

The bash-os build downloads and verifies its pinned GNU Bash source and
patches, reusing verified download files from the local checkout when present.
`BASH_SOURCE_DIR` is supplied internally from that build; it cannot select
unrelated headers through the public build script.
`GHOSTTY_SOURCE` can point to an existing Ghostty checkout;
the build archives the pinned revision without changing that checkout.
Otherwise it fetches the source into `.cache/`.

[deps.lock.json](deps.lock.json) pins Ghostty and Zig 0.16.0. Set `ZIG` to
that compiler or let the build download a checksum-verified Linux x86_64 or
aarch64 compiler into `.cache/`. Build products stay under `build/`; nothing is
installed globally. `BUILD_JOBS` defaults to four.

Ghostty's C API is unstable; updating the pin can require adapter changes.
The build applies the project patches to its private source copy and rebuilds
when their contents change.

## Named sessions

```sh
./batty --session work
./batty --attach work
./batty --observe work
./batty --list
./batty --terminate work
```

`--session NAME` creates a session or reconnects to an existing one.
`--attach NAME` requires an existing session. Close the named window to
detach; the controller prints a reconnect command. The shell and its programs
continue running, and another window can attach to the same terminal state.
Use `--terminate NAME` to stop the session explicitly.

One window controls a session at a time. Up to eight observer windows can
watch its current contents; they send no input and do not resize the session.
Reconnection presents the current text, cursor, title, selection and native
image placements. The service continues parsing terminal replies and partial
graphics transfers while no window is attached. History bytes are not replayed
through a second terminal parser.

New sessions use the currently managed bash-os binary by default. An existing
session retains its original process and runtime; passing a command to
`--session NAME` only starts that command when the name is new. For example:

```sh
./batty --session editor -- vim
BATTY_SESSION_DIR=/tmp/my-batty ./batty --session work
./batty --session-dir /tmp/my-batty --attach work
```

`BATTY_SESSION_DIR` or `--session-dir ROOT` selects an absolute, private
session directory. The default is `$XDG_RUNTIME_DIR/batty`, or
`/tmp/batty-UID` when `XDG_RUNTIME_DIR` is unset. Names contain 1–48 ASCII
letters, digits, underscores, dashes or dots, and cannot begin with a dot.
`--headless` connects without creating a desktop window.

The service parses PTY output from child startup and publishes versioned frames
through sealed memory files over a local Unix socket. Attach always supplies a
complete snapshot. Capable clients then receive complete text/placement metadata
with image references or changed rectangles against one exact earlier revision.
Clients that miss that base, and older clients, receive complete snapshots.
Older services continue working with full snapshots. Unchanged polls carry no
frame payload.

The owner retains one current immutable presentation, one delta from its previous
revision, and a lazily generated full snapshot. It does not retain frame histories
per client. Unchanged image generations share pixel storage; changed images are
copied during capture. Full snapshots decode with read-only mapped image pixels.
Incremental reconstruction owns individual images: changed rectangles copy and
patch the previous image, while references share immutable storage after the
first copy from a mapped full snapshot. This avoids retaining chains of old
frames or historical full mappings. The codec validates exact base revisions,
image dimensions, bounds and reconstructed resource limits before publication.
Text and placement metadata still travel in full; changed images still require
CPU comparison and reconstruction. This is incremental image transport, not a
zero-copy GPU path.

Image renderers reuse textures for changed images with the same ID, dimensions
and format. They compare straight-alpha pixel shadows and upload only the
bounding rectangle of changed pixels; identical content requires no upload.
The conservative 64 MiB texture budget also bounds each renderer's pixel shadows
to 64 MiB. Native workspaces additionally trim all pane renderers to a 128 MiB
aggregate GPU texture budget after each composed frame, evicting hidden panes
first. Across multiple workspaces in one process, a 256 MiB budget counts both
textures and their CPU pixel shadows, evicting hidden panes and unfocused
windows first. An evicted image is recreated from its retained presentation
when next shown. Native hosts can adjust the process cap with
`bt_workspace_process_image_budget`; it does not include presentations,
decoder images or driver allocations. Dimensions, formats and session epochs
invalidate reuse.
`bt_renderer_image_stats` reports lifetime full/region upload counts,
identical-content skips and uploaded bytes, plus current texture-budget and
shadow bytes. The presentation library exposes the incremental codec through
`bt_presentation_pack_delta_fd` and `bt_presentation_unpack_delta_mapping`.

`bt_remote_frame_stats` and control pane metadata's `frame_transport` report
accepted full frames, delta frames, unchanged polls and separate full/delta
payload byte totals. These counters include the initial attach snapshot, remain
available after a connection failure, and reset on a new attachment. Byte totals
measure sealed frame contents, not Unix-socket traffic or GPU uploads. Local
panes report zero transport counters.

Workspace I/O pumps resume in round-robin
order and yield after 16 panes or an 8 ms soft budget, checked between panes.
`batty workspace pump-stats H` reports passes, budget yields, panes visited in
the last pass, and last/maximum I/O-pass duration in milliseconds. These
counters exclude event/control dispatch, background detaches and drawing. The budget cannot
interrupt one expensive parse or synchronous remote request. Zero-timeout
persistent frame/clipboard polling keeps at most one request outstanding and
queues the next request after consuming a reply, without waiting for it.
Persistent graphical controllers also queue keyboard, paste and pointer input
without waiting for that poll. The queue holds at most 128 unacknowledged
operations and 8 MiB of payload; overflow reports an error without accepting
the new operation. Requests retain a three-second failure deadline. Input
success means accepted into the frontend queue; pumping reports later service
errors. Closing the whole frontend does not wait for owner acknowledgments;
queued operations that have not reached an owner are discarded at shutdown.
Clipboard policy changes share this ordered queue, so graphical focus
changes do not wait for a clipboard reply. Revocation disables local delivery
immediately; replies fetched under an earlier permission are discarded even if
focus has already returned. A policy that cannot be queued disconnects the view.
Graphical resize also queues without waiting. One resize is sent at a time;
adjacent unsent resizes collapse to the latest dimensions without crossing
keyboard, paste or other input. The last published frame and cell geometry
remain in use until a new frame arrives; control metadata reports
`resize_pending` during this interval. External pane dumps use asynchronous text
requests, so an unresponsive owner does not block other control clients or panes.
Each connection permits 16 pending or unclaimed text results with caller-selected
limits up to 1 MiB; the control endpoint caps replies at 131,064 bytes.
Cancelled replies are drained without delivery. Oversized results fail
only that read. Persistent selection-copy also uses an asynchronous read, capped
at 1 MiB with one outstanding copy per view. Repeated copy presses reuse the
pending request; focus loss or view closure cancels delivery. Empty selections
and failed reads leave the clipboard unchanged. The direct synchronous text API
and positive-timeout pumps still drain earlier requests and wait for their replies.
Direct session clients retain synchronous input unless they opt into
`bt_remote_input_queue`; `bt_remote_input_flush` waits for service acknowledgment,
not child consumption. Closing a persistent pane in a running workspace removes
its graphics immediately and drains accepted operations in the background.
At most 64 detaches are retained, with four serviced per pump; their original
request deadlines still apply. Control `ping` exposes `pending_detaches` and
`failed_detaches`, and `workspace pump-stats` also counts completed detaches.
Wait for pending detaches to finish before reattaching a controller to the same
session. Standalone view closure and final workspace destruction still attempt
a synchronous flush. Closing a raw session discards pending input. Revoking clipboard permission
discards any fetched data drained while that policy change is being applied.
A failed persistent connection leaves its workspace pane and last frame in place;
other panes continue running. Pane chrome shows “Disconnected” and the failure
reason. Input to that pane is disabled, while focus, layout and close remain
available. Control metadata exposes `disconnected` and `connection_error`, and
the event stream reports a status change. The retained child status is its last
known status, not an inferred exit. Close the failed view and attach a new view
to reconnect to a service that is still running.
Windows request the latest frame; an idle observer does not accumulate an
output queue. Frames have a 128 MiB limit, including at most 64 MiB of image
pixels and 16,384 placements. User input can occupy up to 7 MiB of the native
8 MiB queue; the remaining capacity is reserved for terminal replies and
control events. The service keeps the original command's exit status while
continuing PTY I/O for descendants until the PTY closes. Explicit termination
waits for descendant cleanup and removes the session endpoint before returning.

State remains in the running service's memory. Reconnection survives a
window or controller closing, including an abrupt controller exit. It does
not preserve a process after service failure or a machine restart. Kilix's
durable workspace recovery below restores saved output into fresh shells.
Sessions without a name retain the original
behavior: closing their window stops their command.

## Transcript recording

Kilix records new panes by default under
`${XDG_STATE_HOME:-$HOME/.local/state}/batty/transcripts`. Set
`BATTY_TRANSCRIPT_DIR` to choose another absolute directory. The disk worker
creates missing directory components with mode 0700; an existing final directory
must be user-owned with mode 0700, and path components must not be symlinks or
`.`/`..`. Existing directory permissions are preserved. A storage failure reports
failed recording while the terminal continues running. Each owner creates a
private, randomly named `.log` file;
attaching a frontend does not start a second recording. Named owners continue
recording while detached. Changing the environment does not change an existing
owner's recording policy.

The Kilix frontend reads shared recording settings once at startup:

```sh
./kilix settings --set transcript=off
./kilix settings --set transcript=on --set transcript_size=32M
./kilix settings --set transcript_graphics=keep
./kilix settings --set transcript_total=5G --set transcript_archive_total=1G
```

These use the same `KILIX_TRANSCRIPT*` keys and presets as the shared Kilix SDK.
Per-pane size presets are 2M, 8M, 32M and 128M. Recent-tier presets are 1G, 5G,
10G, 20G, 50G and 100G; the archive also accepts `off`. The shared toggle defaults
to on.
`transcript=off` disables new owners in newly opened Kilix windows while retaining
the directory for transcript inspection. Existing windows keep their launch
policy; existing owners keep their recorder even when attached from a window
whose policy is off. Ctrl+Shift+F5 reloads presentation settings only.
Explicit `BATTY_TRANSCRIPT_LIMIT` and `BATTY_TRANSCRIPT_GRAPHICS` values override
the shared size and graphics presets. The standalone `batty` launcher does not
read shared recording settings; native hosts can disable new recorders with
`BATTY_TRANSCRIPT_ENABLED=0` while preserving the directory setting.
Standalone `batty` recording remains opt-in through `BATTY_TRANSCRIPT_DIR`, which
must already exist unless `BATTY_TRANSCRIPT_CREATE_ROOT=1` is also set. Kilix sets
that creation flag for its default or explicitly selected directory. Directory
creation and disk writes happen in the isolated worker after PTY startup.

`BATTY_TRANSCRIPT_LIMIT` sets the per-file byte limit (4096–134217728, default
8388608). Rotation retains a recent suffix; temporary rotation storage can
approach twice that limit. `BATTY_TRANSCRIPT_GRAPHICS=elide` (default) replaces
seven- and eight-bit Kitty and Sixel graphics sequences with markers; `keep`
preserves raw PTY output. These are output streams, including terminal controls,
rather than screen dumps.

Disk writes run in a separate process behind a bounded, nonblocking channel.
A full channel or writer failure stops recording permanently for that session;
terminal I/O continues. The writer marks interrupted streams when storage permits.
Pane control metadata reports `recording` as `disabled`, `active`, `finishing`,
`complete`, or `failed`; older services report `unknown`. Recording changes emit
status events. `transcript_id` and `transcript_dir` identify the owner's log
and remain stable when a persistent pane is reattached from a frontend with a
different recording directory. Each is `null` when unavailable. A failed writer
can report an ID without leaving a readable log.
A disconnected view retains the last reported state.
`complete` means the worker acknowledged its final flush. Session closure does
not wait for disk completion.

Use the same `BATTY_TRANSCRIPT_DIR` when inspecting recordings:

```sh
./kilix transcript                  # newest-first index
./kilix transcript --json           # index with initial command and directory
./kilix transcript show ID          # raw recorded output to stdout
./kilix transcript path [ID]        # directory or exact log path
./kilix transcript show --pane PANE  # resolve a live pane through its owner
./kilix transcript path --pane PANE  # resolve the owner's original directory
./kilix transcript prune            # compress dead logs and apply tier budgets
./kilix transcript archive          # move dead logs to the older tier
```

`--directory PATH` overrides the environment for these commands. Without either,
the commands look under `${XDG_STATE_HOME:-$HOME/.local/state}/batty/transcripts`;
they do not create that directory or enable recording. IDs are the exact names
from the index, without `.log`. Viewing a live transcript reads the file and
length observed when the request starts, so continuing output cannot extend the
request indefinitely. `--pane` accepts a current control pane ID for `show` or
`path`; `--socket PATH` selects an explicit control endpoint. Pane lookup uses
the owner-reported directory, even when the calling frontend has a different
directory setting. An owner predating directory reporting has no pane-resolved
path; its transcript remains available by ID with `--directory`.

Each new recording has a private `.meta` journal containing its initial child PID,
working directory, start/end timestamps and up to 32 command arguments, capped at
256 bytes each. The index reports truncation. A worker holds a lock on this
metadata inode throughout recording, including log rotation. The index reports
`recording`, `complete`, `interrupted`, or `failed`; older logs without metadata
report `unknown`. These describe the writer, not whether the pane's command is
still running. A killed writer with no completion record is interrupted. Metadata
retains the initial directory and command, not subsequent shell activity.

`prune` compresses inactive logs with `zstd -3` into `recent/`. Its default 5 GiB
budget counts compressed files; excess oldest recordings move to `archive/` with
`zstd -9`. The archive defaults to 1 GiB, evicting its oldest recordings when full.
Use `--recent-budget 5G --archive-budget 1G` to select budgets in bytes or K/M/G.
Without these flags, maintenance reads the shared tier presets on each run.
An archive budget of `off` disables that tier and removes excess oldest recent
recordings instead. `archive` moves all eligible recordings into the archive and
then applies its budget; it requires a nonzero archive budget. Both commands
print counts and resulting tier sizes. Metadata is removed when the last copy of
an evicted transcript is removed.

Active writers and legacy logs with no metadata are protected. Their raw files
are outside the tier budgets; per-writer rotation still applies. Metadata and
temporary compression storage are additional. Budget enforcement is explicit,
not a hard disk quota. If protected compressed files prevent meeting a budget,
maintenance reports that condition and exits unsuccessfully.

Compression requires `zstd` on PATH. Each codec operation has a 30-second limit;
decoding limits the decoder window and output to prevent oversized expansion. Before
source removal, maintenance verifies the compressed contents against the source
and durably publishes without replacing an existing file. On filesystems supporting
unnamed temporary files, killing maintenance during compression leaves no partial
file in the transcript directory. Filesystems without that support use private
`.compress-*` files, which may remain after a forced kill and need manual
inspection. An interrupted transfer after publication can leave two copies; a
later run reconciles identical copies and refuses conflicting ones. A directory
lock excludes overlapping maintenance
and readers; commands report busy promptly rather than waiting for maintenance.
Viewing and path lookup work across both compressed tiers, and the JSON index
reports the selected tier, copies and compressed sizes.

Kilix windows start a separate background maintenance
process. One frontend per directory holds the maintenance leadership lock; another
open frontend takes over when it exits. Maintenance runs at startup and every
60 seconds, rereading shared budgets each pass. Backlogs resume after one second.
Each pass attempts at most 16 file operations with a two-second soft time budget
checked between operations. One operation, filesystem access, or codec can exceed
that budget; compression still has its separate 30-second per-codec limit. Existing
tier overages take priority over compressing more raw files.

The worker observes its frontend through a pidfd. Frontend exit cancels an active
codec and releases leadership; the terminal does not wait for maintenance during
input, rendering or shutdown. Blocking filesystem calls can delay worker exit.
Maintenance failures are reported to the frontend's stderr, with repeated identical
errors suppressed. `BATTY_TRANSCRIPT_MAINTENANCE=0` disables the worker for that
window. Recording may be disabled while maintenance continues managing older logs.
When all frontends close, maintenance stops; persistent owners keep recording and
the next frontend resumes housekeeping. Standalone `batty` does not start it.

The native settings overlay edits page-strip and pane-button choices and the
five shared transcript presets. Use Up/Down or the wheel and Enter to cycle a
choice. Recording and per-pane size changes apply to newly opened Kilix windows;
existing windows and session owners retain their launch policy. Recent/archive
retention choices are read by maintenance on its next pass. The settings CLI
and shared SDK interfaces expose the same choices.

## Using the window

| Action | Input |
| --- | --- |
| Copy selection | Ctrl+Shift+C |
| Paste clipboard | Ctrl+Shift+V |
| Select text | Left-button drag |
| Select when an application captures the mouse | Shift+drag |
| Scroll history | Mouse wheel or Shift+PageUp/PageDown |
| Interrupt or suspend a foreground job | Ctrl+C or Ctrl+Z |

Paste uses bracketed mode when the application requests it. Resize updates
both the terminal grid and the PTY and sends SIGWINCH. Application mouse and
focus reports are encoded according to terminal modes. Applications can select
blinking or steady block, underline and bar cursors with DECSCUSR and set the
cursor color with OSC 12. Focused block cursors fill the character's cell and
redraw its shaped text in the terminal background color, using the foreground
color when the cursor matches that background. Wide characters use both cells,
including when the cursor is on their trailing cell. Unfocused
cursors use a steady outline. Text decorations support single, double, curly,
dotted and dashed underlines, including explicit RGB and palette colors;
patterns continue across adjacent cells and wide characters. Terminal clipboard
reads are disabled. `BATTY_CLIPBOARD=write` allows the focused pane to copy
UTF-8 plain text to the system clipboard through OSC 52. Kilix enables this
write policy by default; `BATTY_CLIPBOARD=off ./kilix` disables it. The standalone
Batty launcher defaults to off. Local copy/paste shortcuts work in either mode.

Writes are limited to 1 MiB, reject embedded NUL and invalid UTF-8, and support
only the standard clipboard's plain-text representation. The latest pending
write replaces earlier pending writes. Background panes, observers and
detached sessions cannot write. Persistent writes travel as one-time requests
to the controlling frontend, separately from retained frames; reconnecting
does not replay them. Older running state services retain their disabled
clipboard policy until recreated with the updated build.

Applications requesting keyboard event types receive releases for held keys
when focus leaves the window or pane, and before a persistent view detaches.
Changing synchronized-input membership also releases held keys in its panes.
Late physical releases are ignored by views that did not receive the press.
Captured mouse buttons are also released on focus loss and detach. Pressing
Shift during an application drag does not divert its release into selection.
The SDL-to-Ghostty physical key map includes Print Screen, Pause, Scroll Lock,
context menu, international and extended keypad keys, browser/media keys and
the side of right-hand modifiers. Keyboard-mode applications receive the
corresponding encoded press and release events where Ghostty defines them.
IME composition keys stay with the input method; committed text reaches the
application as UTF-8 even when it arrives without a preedit event under Kitty
report-all keyboard mode.

Configuration is Bash code read from
`${XDG_CONFIG_HOME:-$HOME/.config}/batty/config.bash`, or from `BATTY_CONFIG`.
See [examples/config.bash](examples/config.bash). Configuration can set the
font, size and initial geometry, and define `batty_on_event EVENT HANDLE` for
resize and title events. Hooks run in the controller and must return promptly.

## Bash builtin

Load it in the managed bash-os executable reported by `--runtime-info` with
`enable -f "$PWD/build/batty.so" batty`.
Handles belong to the process that loaded the builtin. Use `-V` to bind a
result directly; command substitution and other subshells cannot use handles.

```text
batty new -h VARIABLE [--session NAME] [--session-dir ROOT] [-W COLS] [-H ROWS] [--font FAMILY] [--font-size PX] [--headless] -- COMMAND ARG...
batty attach -h VARIABLE [--session-dir ROOT] [--headless] [--observe] [--font FAMILY] [--font-size PX] NAME
batty list [--session-dir ROOT]
batty terminate [--session-dir ROOT] NAME
batty pump HANDLE [-t MILLISECONDS] [-V EVENT]
batty send HANDLE TEXT
batty paste HANDLE TEXT
batty resize HANDLE -W COLS -H ROWS
batty dump HANDLE
batty capture HANDLE PATH.ppm
batty status|info|title|graphics HANDLE [-V VARIABLE]
batty close HANDLE
```

`pump` returns 0 while running, 1 for close/exit, and 2 for an error. Its
event is `tick`, `resize`, `title`, `exit` or `close`; the timeout is 0–100 ms.
`status` yields `running` or the command's numeric exit status (zero for a
user-closed window while the command is running). `info` reports PID, grid
dimensions, queued input, byte counts and persistence/observer flags. `dump` writes plain text from
terminal state; `capture` writes the rendered window as a binary PPM image.
`send` queues text verbatim; `paste` applies terminal paste encoding. Bash
strings cannot carry NUL, while the native PTY path preserves binary bytes.

A controller can own up to sixteen handles, each with its own window or a
headless session. Events are routed to their originating OS window.
Headless sessions use the same PTY engine and require no display. The supplied
launcher manages one window and returns its command's exit status.
`close` disconnects a named handle. `list` and `terminate` use session names
and require no handle. Observer handles reject `send`, `paste` and `resize`.

## Workspaces

[src/workspace.h](src/workspace.h) exposes the native tab and pane interface
for embedding Batty in a desktop frontend. A workspace owns one OS window;
up to sixteen tabs and sixty-four panes share its graphics context. Panes can
run independent commands or attach to named persistent sessions. IDs remain
stable through splitting, movement, resizing and tab changes.

The API supports four-way splits, directional focus and movement, adjustable
split ratios, maximize/restore, and selected-pane synchronized input. Hidden
tabs continue pumping their sessions. Closing a pane releases its view;
named sessions remain running until explicitly terminated. Process exit is
reported independently of pane closure, so the host can keep completed output
visible. Closing the last pane marks its workspace closed.

Native hosts can capture an owned `BtWorkspaceLayout` value with
`bt_workspace_layout_capture` and apply it with `bt_workspace_layout_apply`.
The value retains page order and names, each page's split/tall/grid trees and
ratios, selected and previous panes, zoom, layout mode, per-pane font sizes,
title overrides and synchronized-input choices. Applying it requires an explicit one-to-one mapping
from saved pane IDs to every pane in the destination workspace. A host can
therefore reattach persistent sessions into fresh views and restore their layout
without replacing the processes. Invalid topology or mappings are rejected before
changing the workspace. This C API does not serialize files or launch sessions;
the frontend uses them when restoring saved workspaces.
`bt_workspace_layout_pack` and `bt_workspace_layout_unpack` convert the value to
and from the versioned `BWL2` byte format, bounded to 32 KiB. Existing `BWL1`
checkpoints remain readable. Both formats use
explicit little-endian fields and contain no pointers, executable commands or
C struct padding. Decoding validates the whole topology, pane membership, title
text and preferences before returning an owned value; unknown versions,
truncated records and trailing bytes are rejected. Session descriptors and
frontend presentation settings are stored alongside the layout in workspace files.


Persistent pane metadata includes `session_dir`, `session` and `session_epoch`
(a 16-digit hexadecimal owner identity). A host restoring saved references can
pass that identity as `BtPaneLaunch.expected_epoch`, or use
`batty workspace add H -V PANE --session NAME --session-dir ROOT --attach --epoch HEX`.
The epoch option also works with `--observe`. A different owner under the same
name is rejected before publishing a pane or sending resize/input requests.
An omitted epoch keeps the usual attachment-by-name behavior. Local panes report
an empty session directory and a zero epoch.


Hosts supply keyboard and chrome policy through an event filter and the
workspace operations. Each pane translates input into its own grid and draws
within its assigned pixel rectangle. All visible panes are composed before
the window swaps buffers. The single-session Bash launcher remains available
and does not create a workspace automatically.

The loadable exposes the same operations to a Bash controller through
`batty workspace`. For example, after loading `build/batty.so` in the managed
Bash runtime:

```bash
batty workspace new -h workspace --title 'Two panes' --width 1000 --height 700
batty workspace add "$workspace" -V left -- "$BASH"
batty workspace add "$workspace" -V right --target "$left" --direction right -- "$BASH"
while batty workspace pump "$workspace" -t 16; do :; done
batty workspace close "$workspace"
```

An `add` without `--target` creates a tab. Use `--session NAME` to create or
reconnect a persistent pane; add `--attach` or `--observe` to require an
existing session without supplying a command. `active` and `panes` accept
`-V VARIABLE`, so controllers can retrieve IDs without creating a subshell.
`panes` returns one numeric row per pane with columns
`pane tab window active visible sync x y width height` in drawable pixels.
`layout-check H HEX [-V VARIABLE]` validates BWL1 or BWL2 bytes and returns saved pane IDs;
`layout-apply H HEX SAVED CURRENT ...` applies a complete explicit mapping.
`window-size H WIDTH HEIGHT` sets logical window dimensions.
`status H PANE` reports `running` or the child's exit status. `remove H PANE`
closes a pane, while `close H` disposes the whole workspace. See `help batty`
for focus, movement, resizing, synchronization, input and capture operations.
Workspace handles belong to the Bash process that created them.

Drag a pane title onto another visible pane to place it on the nearest left,
right, top, or bottom edge. Hover over another page in the page strip while
dragging to reveal its panes, then drop on one to move the running pane there.
Clicking a title without dragging focuses the pane and opens its action menu:
rename, copy or reset its title; clear its terminal; split right or down; or
close it. Clearing resets the authoritative terminal parser while its PTY keeps
running, including for persistent panes. Controllers can also
move running panes between pages with
`workspace relocate H PANE TARGET left|right|up|down`.
`workspace pane-rename H PANE TITLE`, `pane-rename-prompt`, `pane-reset-title`,
`pane-copy-title`, and `pane-clear` expose the title actions to native hosts.
Custom pane titles survive `kilix save` and `--restore` through the layout
checkpoint, without changing the running pane process.

## Kilix frontend

`./kilix` starts the Batty-based Kilix frontend using the same managed bash-os
runtime. It provides pages and tiled panes with clickable headers and keyboard
controls. Compatibility with the Kitty-based Kilix application stack is still
under development.

```sh
./kilix
./kilix --session work
./kilix --attach work
./kilix --font-size 18 --width 1200 --height 800 -- vim
./kilix screen-size show
./kilix screen-size larger
./kilix screen-size set 20
```

`kilix screen-size` saves an integer font size from 6 to 96 pixels in Batty's
private Kilix storage and updates the panes and default size of running Batty
Kilix windows. A new frontend uses that default unless `BATTY_FONT_SIZE` or
`--font-size` overrides it. `--socket PATH` also targets an explicit workspace
endpoint. This uses Batty pixels; Kitty's fractional point-size range is not
supported.

| Shortcut | Action |
| --- | --- |
| Ctrl+Shift+T | New page |
| Ctrl+Shift+Arrow | Split in that direction |
| Alt+Arrow | Focus neighboring pane |
| Ctrl+Shift+Enter | Maximize or restore pane |
| Ctrl+Shift+W | Close pane |
| Ctrl+Tab / Ctrl+Shift+Tab | Next / previous page |
| Ctrl+Shift+S | Toggle this pane's synchronized input selection |
| Ctrl+Shift+R | Resize pane interactively |
| Ctrl+Shift+Home | Reset the current layout’s pane sizes |
| Ctrl+Shift+F5 | Reload supported shared presentation settings |
| F12 | Open the all-page Pane Center |
| Ctrl+Alt+S | Open presentation settings |
| Ctrl+Alt+M | Toggle Start menu |

Press Ctrl+Shift+B, release the modifiers, then a key below for a leader
command. The active page shows `Leader` while waiting. Escape, an unknown
key, a mouse click, or a focus change cancels the sequence. Ctrl+B continues
to reach the application.

| Leader key | Action |
| --- | --- |
| Arrow | Focus neighboring pane |
| o / ; | Next / last focused pane |
| z / x | Maximize or restore / close pane |
| r | Resize pane interactively |
| q / w | Choose a pane in this page / choose a page |
| , | Rename the current page |
| Space | Cycle splits, stack, tall, and grid |
| c | New page |
| n / p / l | Next / previous / last focused page |
| 1–9 | Select page by its displayed number |
| Shift+[ / Shift+] | Swap with previous / next pane |
| Shift+5 / Shift+' | Split right / down |
| Shift+7 | Close the current page and all its pane views |

Symbol shortcuts above use SDL key names (the US keyboard symbols are
`{`, `}`, `%`, `"`, and `&`).

Layout cycling preserves the custom split tree. Stack shows only the active
pane; tall gives the first pane a master column and stacks the remaining panes
on the right; grid divides panes into columns. Tall and grid each retain their
own resize adjustments until panes are added, removed, or reordered, which
regenerates their geometry. Returning to splits restores the custom proportions,
including pane additions and removals made while another layout was active.
Reset sizes restores the selected layout's default proportions. Controllers
can use `workspace layout H PANE next|splits|stack|tall|grid`; control metadata
reports the page's `layout`, with layout-change events on affected panes.

Choosers appear over the workspace while applications continue running. Use
Up/Down, Home/End, or the mouse wheel to select a row, Enter to focus it, or
1–9 to choose that numbered row directly before entering a query. Type to
filter by title or pane ID; `/` starts search explicitly so digits can be part
of the query. Matching ignores ASCII letter case and matches other UTF-8
characters exactly. Backspace removes one codepoint, and Ctrl+U clears the
query. Queries are limited to 127 UTF-8 bytes. Enter with no matches keeps the
chooser open. Click a row to select it; Escape or
a click outside cancels. Long lists scroll to keep the selection visible.
Focus changes and pane closure dismiss the chooser. The native API is
`workspace choose H panes|pages|all`; it requires workspace chrome. F12 opens
the combined all-page view. Its rows identify each page and pane; the selected
pane shows its PID, current directory when available, connection state and
recording state. While Pane Center is open, the Kilix frontend samples only
its displayed rows and adds foreground process and coding-agent activity from
the same live-owner checks used by `kilix panes --json`. Labels expire after
3.5 seconds if the sampler stops. On sufficiently wide windows, the highlighted
pane also has a scaled live preview of its rendered text and inline graphics. The preview
uses its retained presentation, so a stopped persistent owner cannot freeze
the chooser or change the pane's PTY dimensions. Tab cycles all panes, this page
and other pages. The combined view also matches custom page names in its
filter. The selected pane's task comes from the latest operator message in a
live Codex rollout owned by that pane's foreground process, or from a live
Claude session name when available. A partial rollout record leaves the task
unknown until the next complete sample. `a` opens the highlighted pane's action
menu without changing focus; right-clicking a row opens the same menu. It offers
title rename/copy/reset, terminal clear, split and close. F2 renames the selected pane's page
without changing focus; `s` sends
up to 1022 UTF-8 bytes plus Return to that pane; `x` asks for `y` confirmation
before closing it. The reference Pane Center's broker journal and some richer
actions remain to be integrated.

The rename prompt edits the page's custom name: type text, use Backspace to
remove the last Unicode codepoint or Ctrl+U to clear, then Enter to save or
Escape to cancel. Clicking or changing focus cancels. Names accept printable
UTF-8 up to 255 bytes. An empty name restores the active terminal's title.
Custom names appear in both the page strip and page chooser, and survive
switching or closing individual panes. Workspace layout checkpoints preserve
them across frontend replacement. Controllers can use
`workspace rename H PANE TITLE`, `workspace page-title H PANE [-V VAR]`, and
`workspace rename-prompt H PANE`. Control listings expose `page_title` and
emit title-change events for affected panes.

In resize mode, Right/Down grow the focused pane along that axis and Left/Up
shrink it. Arrows repeat while held. Enter keeps the changes; Escape restores
the original proportions. Clicking, changing focus, or editing the layout
cancels resizing. A page needs at least two panes and must not be maximized.
Custom controllers can enter this mode with `workspace resize-mode H PANE`.
`workspace reset-sizes H PANE` restores default proportions in the selected
pane's page. In splits mode it restores half shares at every split, preserving
split directions, pane identities and sessions. It cancels an active resize transaction before resetting. Nested
splits retain their structure, so their leaf panes can have different areas.

The page strip selects pages and its `+` button creates one. Its position
comes from `KILIX_CHROME_TAB_BAR_EDGE=top|bottom` in the shared GPU Terminal
settings file: `GPU_TERMINAL_SETTINGS_FILE`, or
`${GPU_TERMINAL_HOME:-$HOME/.local/gpu_terminal}/settings.conf`.
`BATTY_TAB_BAR_EDGE=top|bottom` overrides that choice, including when set in
Batty's Bash configuration. The default is top. Settings are read at
startup and on Ctrl+Shift+F5 as bounded `KEY=value` data, never sourced as shell code. Missing
files and invalid shared edge values use the default; unreadable, oversized,
non-regular, or symlinked files report an error. Page-strip placement, Start-badge visibility, and pane-button visibility are
integrated from this shared settings contract so far. Native controllers can
change placement with `workspace chrome-edge H top|bottom`.

The shared `KILIX_CHROME_BUTTON_` settings support `SYNCHRONIZE_INPUT`,
`FONT_DECREASE`, `FONT_INCREASE`, `SPLIT_LEFT`, `SPLIT_UP`, `SPLIT_DOWN`,
`SPLIT_RIGHT`, `MAXIMIZE`, and `CLOSE`. All default on; empty values, `0`,
`no`, `false`, `off`, and `disabled` hide a button (case-insensitive). Hidden
buttons leave no clickable control behind; the title uses the freed space.
Keyboard actions remain available. `workspace chrome-buttons H MASK` provides
the same control to native hosts: bits 0–8 follow the order above, 511 shows
all buttons and 0 leaves only the title.

The native Start menu provides new pages, page and pane navigation, splits,
layout controls, and settings. Ctrl+Alt+M opens it. Enable its page-strip badge
with `kilix settings --set start_menu=on` and reload with Ctrl+Shift+F5; the
shared key is `KILIX_CHROME_START_MENU` and defaults off. Arrows navigate,
Right/Left enter or leave submenus, Enter activates, and Escape closes. The
letters printed beside items are mnemonics. Mouse selection and wheel scrolling
also work. Applications (G) opens the installed application list; arrows,
Home/End and the wheel select entries and Enter launches a new page. The first
256 installed applications are shown in name order. Ctrl+Shift+F5 refreshes
the list after applications are installed or removed. Downloadable content,
desktop providers, and system/power entries are not integrated yet. Native hosts use `workspace menu H` and
`workspace start-badge H 0|1`.

`kilix apps list` discovers installed applications from the XDG data directories
using Kilix's bundled scanner. Add `--json` for desktop IDs, labels, categories,
and launch metadata. `kilix apps open DESKTOP_ID` launches the selected entry:
graphical applications use the private X11 provider, and `Terminal=true` entries
use a terminal page. When called inside a pane, either kind opens a new page;
outside the frontend it opens a new window. Desktop `Path` selects the working
directory. Desktop Entry quoting and field codes are decoded into literal
arguments without shell evaluation. User entries override system
entries, including hidden entries. This installed-application catalog is
separate from the reference downloadable content catalog. `open --in-place`
executes in the current process instead of creating another page; the Start
menu uses this inside its newly created page. A Start-menu application that exits
with a nonzero status leaves its pane and diagnostic output open, with a one-time
status message. Dismiss the message to inspect the output: completed panes
support mouse selection with edge-drag autoscroll, Ctrl+Shift+C copy, wheel scrolling and Shift+PageUp /
Shift+PageDown. Typing and paste remain disabled after the process exits. Close
the pane normally. Successful applications still close automatically. This retention policy
currently applies to Start-menu launches; explicit CLI commands retain their
existing exit behavior. Custom controllers can call `kilix_collect_exits` to apply
the same policy in their event loop.

Native hosts clear application
entries with `workspace app-menu H` and append a printable label (up to 255
UTF-8 bytes) with `workspace app-menu H LABEL`. Selection emits
`application-N` with the zero-based entry index; the host owns launch policy.

Installed applications can receive documents or URLs through their declared
Desktop Entry field codes:

```sh
./kilix apps open editor.desktop -- "/path/document with spaces.txt"
./kilix apps open browser.desktop -- https://example.org/
```

`%f` and `%u` accept one file or URL per launch; `%F` and `%U` accept multiple.
Local filenames become absolute before the application's working directory is
applied, and URL arguments preserve their encoding. File-only applications reject
remote URLs. `%i`, `%c` and `%k` expand the icon, application name and desktop-file
location; `%%` is a literal percent. Expansion creates arguments without shell
execution or recursive field expansion. Unsupported codes, quoted field codes,
multiple file-code declarations and supplied inputs without a matching field
code produce an error. Deprecated field codes are removed.

Rejected frontend actions show a native message identifying the failed action;
Enter, Escape or a click dismisses it. The workspace keeps pumping its PTYs while
the message consumes keyboard and pointer input. Detailed diagnostics still go
to the launching terminal. Settings failures use their existing save/reload
feedback. Native hosts can set `workspace message H TEXT`, clear it with an empty
string, or read it with `workspace message H -V VARIABLE`. Messages accept up to
255 bytes of printable UTF-8 and do not execute terminal escape sequences.

Ctrl+Alt+S opens a native settings overlay. Up/Down, Home/End, or the wheel
select a row; Enter, Space, or a mouse click changes it. Escape or an outside
click closes it. Changes save through the shared settings writer. Page placement
and pane-button changes apply to the current window without closing applications;
transcript choices affect new Kilix windows and retention maintenance reads the
new budgets on its next pass. The overlay confirms saves
and distinguishes a failed save from a saved change whose reload failed.
After fixing a settings-file problem, toggle the choice again to retry. Other windows can reload
with Ctrl+Shift+F5. A page edge fixed by `BATTY_TAB_BAR_EDGE` is marked as a
configuration override and cannot be toggled here. The overlay covers page
placement, pane-button visibility, recording, transcript size and graphics,
and recent/archive retention budgets. Custom controllers can use
`workspace settings H [--edge-locked]`; its `setting-edge-*` and
`setting-button-N-on|off` and `setting-transcript*` actions must be dispatched
by the host to persist and apply a choice. `workspace settings-recording H
ENABLED SIZE GRAPHICS RECENT ARCHIVE` supplies validated displayed values.
`workspace settings-result H clear|saved|save-failed|reload-failed`
updates the visible feedback.

Use the settings command to inspect or update these supported shared values:

```sh
./kilix settings --list
./kilix settings --get tab_bar_edge
./kilix settings --set tab_bar_edge=bottom --set button_close=off
./kilix settings --set button_close=on
./kilix reload-settings --window front-ABCDEFGH
```

Aliases are `tab_bar_edge`, `start_menu`, and `button_` followed by a lowercase button name
from the list above. Full `KILIX_CHROME_...` keys also work. Reads do not create
files. Writes validate the entire batch, preserve comments and unrelated
entries, and update the last matching assignment under the shared SDK's lock.
Replacement is atomic, with private file permissions and file/directory sync.
Press Ctrl+Shift+F5 in an existing window or use `kilix reload-settings`
to apply changes without restarting its applications. The command waits for
the frontend to finish and reports a failed reload. Use `--window ID` from
outside a pane to select a frontend. Failed reads keep the previous live
settings and report an error. This reload covers the supported shared
presentation settings; it does not re-source executable Bash configuration.
The settings command reports stored choices,
while `BATTY_TAB_BAR_EDGE` can still override the running frontend's edge.
Unsupported settings are rejected rather than silently added.

Pane headers provide synchronized-input selection (`S`), smaller/larger text, four-way
splits, maximize/restore, and close. Click a pane title to focus it.

Selected panes in the same page receive synchronized keyboard input. Named
panes detach when closed; new pages and splits start independent, unnamed
shells. Completed commands close their panes. The frontend returns the last
observed child exit status when its final pane closes.

Configuration is `${XDG_CONFIG_HOME:-$HOME/.config}/batty/kilix.bash`, overridden
by `BATTY_KILIX_CONFIG`. It is executable, user-owned Bash configuration and
does not read the Kitty-based Kilix installation's settings. A
`kilix_on_ready WORKSPACE INITIAL_PANE` function can customize bindings and
launch additional panes. For example:

```bash
kilix_on_ready() {
    batty workspace bind "$1" Ctrl+Shift+N new-page
}
```

`workspace bind H CHORD ACTION` registers an action name; an empty action
removes the binding. `workspace bind H PREFIX CHORD ACTION` registers a
two-key sequence. A prefix cannot also be a direct binding. Chords accept SDL key names and `Ctrl+`, `Alt+`, `Shift+`,
or `Super+` prefixes. `workspace action H -V VARIABLE` drains one `NAME PANE`
record, returning 1 with an empty variable when no action is pending. Shortcut
repeats, releases and associated text are consumed before terminal input;
records retain the pane selected at the original key press. The queue holds
64 actions and reports overflow as an error. Up to 256 subsequent SDL events
wait for queued host actions to be dispatched, preserving input order across
the controller boundary; event overflow is also an error. `workspace chrome H 0|1` toggles
clickable controls for custom hosts; `workspace font H PANE DELTA` changes a
pane's font size. Chrome clicks use the same action queue as keyboard bindings.

## Graphical applications in Kilix

```sh
./kilix run -- xmessage 'Hello from Batty'
./kilix run --size 1280x720 --fps 30 -- APPLICATION ARG ...
./kilix run --serve --no-pane --size 800x600 -- APPLICATION ARG ...
./kilix run --mse --audio --no-pane --size 800x600 -- APPLICATION ARG ...
./kilix run --lan --hls --no-pane --size 800x600 -- APPLICATION ARG ...
```

Inside a Batty Kilix pane, `kilix run` uses that pane. From outside, it opens
a Kilix window and runs the provider there. Arguments remain literal. The
application runs on an authenticated private X display; XDamage/MIT-SHM
capture and the bundled Kitty frame presenter deliver pixels into the pane.
Keyboard and mouse events travel back to that private display through XTest.
The provider owns its application and display processes and cleans them up
when it exits. Ctrl+Q quits the provider.
`--serve` exposes the app on a loopback VNC listener with separate control and
view-only passwords; `--no-pane` runs it without opening a local Batty window.
The provider prints connection details only to the local terminal.
The live provider suite also launches installed `xterm`, checks its VNC
framebuffer, types into its child through authenticated VNC, and verifies
normal exit and cleanup.

Install the additional dependencies `xvfb`, `xauth`, `ffmpeg`, `python3-xlib`
and `python3-pil`, plus the application you want to run. The application test
suite also requires `Xvnc`, `xwininfo` from `x11-utils`, and Python's
`Cryptodome` package for its VNC
network check, plus PulseAudio, `pactl`, `pacat`, and Python `websockets` for
the MSE/audio check. No separate Kilix checkout or Python
package installation is needed for the bundled provider or presenter.

Without `--size`, the application display follows the pane dimensions. A fixed
size scales the captured image to fit. `--fill` stretches it; `--desktop-session`
lets a launched window manager own its private display's window placement.
`kilix run --help` lists the upstream provider options. The bundled
`--serve --no-pane` VNC path has an end-to-end loopback check for authenticated
pixels, controller input, view-only isolation and cleanup. The
`--mse --audio --no-pane` path also has a loopback WebSocket check for token
rejection, H.264 video, non-silent AAC audio and private sink cleanup. The
`--lan --hls --no-pane` path has a loopback HTTPS check for token and cookie
authentication, playable H.264 fMP4 segments and cleanup. A Chromium check
also played WebRTC video over loopback and over HTTPS through this host's LAN
address. An ARM64 second host decoded H.264 video and non-silent Opus audio
from WHEP; its HTTPS signaling used an SSH reverse tunnel while ICE media
used the provider's LAN UDP candidate. The WebRTC publisher uses a one-second
keyframe interval; four ARM64 joins after that change decoded progressing
video and audio, with first video arriving 0.017–0.345 seconds after first
audio. Direct LAN HTTPS access from that host
and physical audio/video presentation timing remain unverified.
The WebRTC service requires its printed per-run token for viewer reads even
through a loopback SSH tunnel; anonymous loopback access can only publish.
`--lan --webrtc` serves its viewer over HTTPS using the same private
self-signed certificate as Batty's other LAN browser tiers.
An isolated nested Weston session also rendered a real X11 xterm through the
provider in a Wayland Batty frontend and delivered keyboard input to that app.
Nested Weston checks also exercised scale-2 keyboard input and UTF-8 clipboard
paste and OSC 52 writes in both directions. A separate nested labwc/Fcitx5
check delivered real Wayland text-input preedit and committed a composed UTF-8
character into a Batty pane. A full user Wayland session remains unverified.
The optional `KILIX_HW=1` H.264 path probes VA-API render nodes and selects one
that can encode; capture still crosses CPU memory. GPU-host/DMA-BUF capture and
a general-purpose Kilix SDK host are not integrated yet. The bundled desktop
contains its pinned SDK; the application provider bundles only the modules it
uses.

Provider data lives beneath `$XDG_DATA_HOME/batty/kilix` (default
`~/.local/share/batty/kilix`), overridable with `BATTY_KILIX_STORAGE_HOME`.
Session files use `$XDG_RUNTIME_DIR/batty-apps`, or the storage directory's
`session/` when no runtime directory is set. Existing storage/session roots
must be private and user-owned. The adapter overrides the original Kilix
storage variables and discards foreign terminal session identities.

The [Kilix provider source](third_party/kilix-apps/README.md) is GPLv3;
the [frame presenter](third_party/kitty-frame-presenter/README.md) is MIT.
Their committed revisions, file hashes and licenses accompany the source.

## External workspace control

The Kilix launcher also provides commands for an already-running workspace:

```sh
./kilix ls --panes
./kilix ls --json
./kilix ls --all --json
./kilix focus pane:2
./kilix focus tab:3
./kilix focus pane:2 --window front-ABCDEFGH
./kilix rename tab:3 'New page title'
./kilix close pane:2
./kilix close tab:3
./kilix watch --once --plain 2
./kilix panes                         # Open the native Pane Center
./kilix panes list                    # Pane Center's script-facing list
./kilix panes --json                  # Versioned joined pane snapshot
./kilix panes dump 2 --lines 40
./kilix panes wait 2 --for idle --timeout 300
./kilix panes send 2 --enter 'continue'
./kilix panes focus 2
./kilix new-pane left -- vim
./kilix new-page --cwd /tmp -- bash
```

Run these inside a Batty Kilix pane, with its inherited control environment.
`panes` accepts an exact pane ID, a unique title, or a named persistent-session
prefix, and rejects ambiguous names. `send` accepts at most 1024 UTF-8 bytes,
including the optional carriage return; accepted input is queued, so use
`dump` to check its effect. The `kilix.panes/v1` JSON reports Batty's named
session, recording and transcript identity under `batty_session`. On Linux,
its `process` scans up to 64 descendants of the initial PTY child and reports
members of the terminal's foreground process group; the representative process
supplies the activity label (`shell`, `running`, `remote`, or `agent`) and
working directory. The `doing` field uses a live coding session's current task
when known, then the pane title. For Codex, `coding_session` and `wait --for idle` require a
live foreground process that owns its rollout JSONL file. A completed explicit
turn boundary reports `idle`; a started turn reports `working`. For Claude,
the same commands require a session descriptor whose PID and process start time
match the live foreground process; its published `idle`, `working`, or
`waiting` status is reported. Missing, partial, or stale evidence stays
`agent`, and a changed owner aborts a wait. Other agents' turn boundaries and
broker telemetry remain unavailable. Output silence is never treated as an
agent turn ending.
`new-pane` resolves the calling pane through process ancestry, even if another
pane has focus, and inherits the calling command's working directory. From
outside a pane, pass `--socket PATH` and `--target PANE_ID`; the default
directory then comes from that pane's initial process. `--cwd DIRECTORY`
overrides it. Pass child commands after `--`; arguments are preserved without
shell evaluation. Ordinary Kilix panes, including panes opened through these
commands, use independent persistent PTY owners.
`new-pane` and `new-page` accept `--tab-title`, `--pane-title`, and repeated
`--env NAME=VALUE` options for desktop/application launchers. Environment
overrides apply only to the new child.
`kilix launch --type=tab` accepts the same options and `--self`, providing a
tab-launch surface for graphical desktop hosts. `--type=os-window` starts an
independent Kilix frontend with its own control endpoint and persistent pane.
It skips taking over unrelated detached sessions at startup; its window and
pane titles, child environment and working directory follow the launch request.
`kilix rename` changes a page title. `kilix close` closes one pane or every
pane on an explicitly addressed page; generated persistent owners are
terminated with their panes on a Kilix frontend endpoint.
Each Kilix frontend has its own pane and page ID space. `kilix ls --all`
lists the live frontends in the private control registry with their `front-…`
window IDs; `--window ID` directs focus, watch, launch, rename, and close to
that frontend. This keeps targets unambiguous when separate OS windows use
the same pane number.
`./kilix desktop` opens the [bundled Kilix 95 desktop](third_party/kilix-desktop/README.md).
`./kilix desktop xp` (or `./kilix xp`) opens its bundled XP flavor. Each flavor
has a separate desktop data directory inside Batty's private Kilix storage.
`./kilix desktop tui` (or `./kilix tui`) installs and opens the pinned text
desktop from the same private catalog package as Kilix File and Launcher.
Its Batty adapter uses Batty host commands and status, and omits menu actions
whose providers are not ported yet. `./kilix tui --install-only` prepares the
pin without opening a window.
`./kilix voice install` installs the pinned Voice runtime, Vosk library, and
small English model into Batty's private Kilix data tree. Use
`./kilix voice install --without-dictation` for read-aloud only, or
`./kilix voice install --model lgraph-en-us` for the larger model. The pinned
installer verifies the source, wheel, and model hashes. `./kilix tts` and
`./kilix stt` open their configuration screens; `./kilix voice doctor` reports
the current audio and model state. Voice Studio is available in the pinned TUI
catalog. In a Batty pane, `kilix speak` reads its visible text, and
`kilix dictate --pane` pastes recognized words into that pane without Enter.
The pane menu also offers Read aloud and Dictate; Ctrl+Alt+R and Ctrl+Alt+D
invoke them for the active pane, and Ctrl+Alt+X stops speech or dictation.
`kilix speak TEXT` speaks literal text, while plain `kilix dictate` prints one
recognized utterance for scripts.
`./kilix desktop cap` (or `./kilix cap`) and `./kilix desktop land` (or
`./kilix land`) install their exact pinned C desktop revisions with their
submodules in Batty's private data directory. Their Kitty graphics render
inside Batty pages, and the Batty control adapter handles tab launches.
`--install-only` prepares either provider without opening a window. The
providers' art remains in their managed installs; it is not bundled with
Batty's MIT source tree.
`./kilix desktop icewm` (or `./kilix icewm`) installs the pinned IceWM
provider and builds its pinned upstream IceWM source on first use inside
Batty's private data directory. Its desktop session runs through Batty's
X11 application host. `--install-only` prepares it without opening a window.
From a Batty pane it opens a new page; outside Batty it opens a new frontend
window. `--app settings` opens the desktop Settings panel with the shared
choices Batty implements. Apply writes the shared settings file and reloads
the attached frontend. The in-project `batty-kitten` adapter handles the
desktop's `kitten @ launch` tab and OS-window calls, so its Terminal icon and
desktop application launchers open Batty pages or windows. An external source
checkout can still be exercised with `--source /path/to/kilix`; its own
Settings panel and unsupported host services retain their original behavior.
The bundled desktop's Kilix Applications menu uses `kilix app run|window ID`
to install and launch pinned catalog applications. `kilix app install ID`
prepares one without launching it, and `kilix app ref ID` prints its catalog
revision. Installed applications live under Batty's Kilix data directory
(`$XDG_DATA_HOME/batty/kilix/data/desktop-apps` by default). Set
`BATTY_KILIX_STORAGE_HOME` to select another private Batty root; inherited
`KILIX_DATA_HOME` and `KILIX_STORAGE_HOME` from a separate Kilix installation
do not redirect Batty's catalog installs. Set
`KILIX_APP_AUTO_INSTALL=0` to require an explicit install before launching.
`./kilix desktop --app kilix-file` opens a catalog app on desktop startup.
Bare `./kilix settings` opens the bundled desktop's interactive Settings
panel; `--get`, `--set`, and `--list` keep the settings CLI. The pinned System,
Settings, Software, and Session Center catalog apps use Batty's filtered TUI
adapter, so their menus use Batty host commands and private state.
`./kilix install --json` reports Batty's pinned app and game install status for
the text desktop's software menu. `./kilix install ID` prepares an app or game
through the same Batty-private installer as its normal launcher.
`./kilix default-desktop set xp` makes a later bare `./kilix` launch open the
bundled XP desktop. `show` and `list` inspect the private choice; `none`
restores a plain terminal, `tui` selects the pinned text desktop, and `auto`,
`builtin`, and `external` select the bundled 95 desktop. `cap`, `land`, and
`icewm` select the pinned graphical providers.
`./kilix status` reports the verified Bash runtime, selected desktop, and
current frontend pane counts; `--json` exposes the same Batty-owned facts.
`./kilix desktop --open /path/document.pdf` opens a file through the desktop's
associations, including the pinned PDF viewer.
Catalog terminal windows use a disposable Batty PTY, so closing their desktop
window ends the application without leaving a detached session.
Native multiwindow catalog apps such as Kilix Amp keep their own window sizes
inside the desktop pane.
`./kilix app install dosbox` prepares the catalog's DOSBox release in the
private `desktop-games` directory; its desktop launcher opens a DOS prompt
through Batty's X11 application provider.
`./kilix games list` shows the bundled desktop's shared game IDs and their
availability. `./kilix games enable|disable ID...` updates the shared settings,
and `./kilix games play ID` installs and starts a game. DOSBox and Doom use
Batty's graphical application provider; Minesweeper and Solitaire open in the
bundled desktop. Add `--setup-only` to prepare a game without launching it.
The Camera Wall entry uses the catalog's pinned `kilix-nvr` build and shares
Batty-private NVR and RTSP directories with the corresponding catalog apps.
It needs cameras configured before `view` can display a feed.
`./kilix nvr cameras` opens the same managed recorder; `record` and `recorder`
are aliases. `./kilix nvr --install-only` prepares its pinned binary without
starting it. `--force-install` atomically rebuilds the pinned source when its
managed checkout is clean. Camera configuration and recordings stay in
Batty-private storage.
Batty's NVR build initializes the pinned RTSP decoder's seek offset, then
restores the pinned source tree; a binary-bound marker triggers a rebuild when
an older NVR installation lacks that fix.
The Tmux Manager entry installs the pinned `tmux-tui` source and its `tmux-cli`
submodule under Batty storage, and launches with that nested CLI selected.
The desktop's Temps and Memory entries and the `kilix temps`, `kilix memory`,
and `kilix tmux` commands use Batty-owned launchers instead of ambient PATH
commands. Temps and Memory share the pinned `kilix-tui-utils` install. Their
`--graphics` mode builds the pinned `soft-raster` library on first use and uses
Batty's bundled frame presenter; text mode needs no native raster build.
`./kilix launcher` opens the pinned TUI launcher from that same package.
`./kilix launcher --install-only` prepares it without opening a pane.
`./kilix screensaver [matrix]` builds the pinned Kilix matrix source into
Batty's private cache and runs it in the current terminal; press `q` to leave.
`--install-only` builds the executable without starting it. The pinned TUI's
Screensavers submenu discovers this bundled source. It retains Kilix's GPLv3
license beside the bundled desktop source.
Its curated stack programs route through Batty's app catalog; graphical XDG
applications open through `kilix run`, and desktop launchers come from Batty's
private Kilix data directory. `./kilix launcher --list` prints its rows.
`./kilix laptop list|status|open PROFILE|close PROFILE` reads Kilix-style
`KEY=value` laptop profiles from Batty's private Kilix storage (or an absolute
`KILIX_LAPTOP_PROFILES` override). Pane profiles open their own Batty window,
using pages or splits as requested; the launcher shows the same live run state.
Desktop profiles for the bundled Kilix 95 and XP flavors, the pinned TUI,
Cap, Land, and IceWM also open through Batty's provider adapters.
Music Control resolves its pinned Kilix Amp backend and control socket under
Batty storage. Its first-run install uses `kilix amp --install-only`; Camera
Manager's viewer actions use Batty's `kilix-rtsp` adapter and a private camera
configuration path. `kilix rtsp list` reaches that same pinned viewer;
`camera` and `cameras` are aliases. `--install-only` prepares it, and
`--force-install` atomically rebuilds a clean managed checkout. Neither menu
selects an ambient Kilix backend.
`./kilix look classes` runs the catalog's pinned image analyzer; `analyze` and
`analyse` are aliases. Use `./kilix look --install-only` to build it without
running it, or `--force-install` to atomically rebuild its clean managed
checkout. Image analysis needs a separately installed detection model; the
analyzer's command help describes image, recording, and camera inputs. Batty
looks for a detector command at `data/runtimes/yolo/bin/kilix-look-detect`
under its private Kilix storage root. Set `KILIX_OBJECT_DETECTOR` to an
explicit command to use another runtime; both the analyzer and recorder
inherit that setting.
`./kilix pdf-view FILE.pdf` opens the pinned PDF viewer; `pdf-viewer` is an
alias. `--install-only` builds its Poppler/Cairo core and `--print-ref` shows
the catalog revision. The viewer uses Kitty graphics for pages and retained
scrolling when Kilix rendering is advertised, with a standard graphics
fallback for other compatible terminals.
The Region Painter entry installs the pinned `kilix-mask` source. Opening it
without an input creates a 640×400 canvas; its Open action accepts a PNG image
or an existing Kilix mask PNG and preserves painted regions when editing a mask.
JPEG and WebP inputs are decoded to private, reusable PPM plates before opening;
the original images stay untouched. This conversion needs Pillow (`python3-pil`).
The Model Store entry installs the pinned `kilix-bonsai` TUI without downloading
model weights. Its model and component data paths stay under Batty's private
Kilix storage root.
The Chawan Text Browser entry builds pinned Chawan with a checksum-pinned Nim
toolchain under Batty storage. Its first run seeds an editable private browser
config with images and true color enabled; later runs preserve edits. Browse
opens Chawan's visual home page, and Open accepts a URL. The desktop's
`kilix open-url URL` fallback now opens that URL in Chawan when no graphical
browser is available.
`kilix open-url` without a URL opens Chawan's home page. The `kilix chawan`,
`kilix bonsai`, and `kilix mask` verbs use the same pinned application adapters
as the desktop catalog.
Browser streams prepare noVNC and HLS/MPEG-TS player assets in private Batty
storage from immutable source and SHA-256 pins. First use needs network access;
later launches reuse verified local copies.
Catalog entries backed by reference-only Kilix services still need those
services ported before they can run in Batty.
The bundled Pane Center icon and Start-menu item open Batty's native Pane Center.
The bundled Mux Terminal icon opens a named Batty-owned terminal page through
`kilix serve` (default name `main`). `kilix attach NAME` requires that owner;
`kilix view NAME` opens a read-only observer. Repeating `serve` or `attach`
focuses the existing controlling pane, including in another registered
frontend that uses the same session root. A named owner
survives closing its view until explicitly terminated with
`batty --terminate kilix-mux-NAME`; Batty permits one controlling view at a time.
`kilix mux [NAME]` is the reference command alias for `kilix serve [NAME]`.
`kilix pty` opens a native persistent-session manager showing attached and
detached Batty owners. Select a session with the arrows, press `a` to attach,
`v` to observe, or `x` and then `y` to terminate it. For scripts,
`kilix pty list` and `kilix pty --json` report the same owners;
`kilix pty attach|view NAME` and `kilix pty terminate NAME --yes` expose the
actions. The pinned TUI's PTY sessions and Mux terminal entries use these
Batty commands.
The bundled desktop still has commands and host services that require porting.
A frontend crash or ordinary window exit leaves their processes running.
While the frontend runs, it writes private, atomic layout and output snapshots
under `${XDG_STATE_HOME:-$HOME/.local/state}/batty/recovery` (override with
`BATTY_KILIX_RECOVERY_DIR`). The next
no-command `./kilix` startup restores a matching detached workspace's splits,
page titles, appearance and owner views, preserving child PIDs and output when
the owners survive. After owner loss or reboot, it restores styled text,
scrollback and a static graphics frame, and starts fresh shells in the saved
working directories. Other programs remain stopped. Layout changes are saved
as they arrive; output snapshots refresh every five seconds. A sudden failure
can lose changes since the last successful snapshot. Deliberately terminated
owners are excluded from automatic recovery.
If no complete matching snapshot exists, detached generated owners open as
recovered pages. A crash immediately after a layout edit may precede its
snapshot; the owners still recover. Explicit `--restore FILE` takes precedence.
Set `BATTY_KILIX_AUTO_RECOVER=0` to leave detached owners for manual attachment.
The pane and page close actions ask for Y before terminating running generated
owners; completed generated owners close directly. Explicitly named
sessions retain their attach/detach behavior. `split` and `new-tab` are aliases for `new-pane` and
`new-page`. Tab focus restores that tab's selected pane. `watch` currently
provides plain visible-screen text, with optional polling, and refuses to watch
its own pane.

`./kilix` creates a private local control socket and exports its path as
`BATTY_CONTROL` to child programs. `./battyctl` locates the child's controlling
workspace, or accepts `--socket PATH` to select an endpoint explicitly:

```sh
./battyctl list
./battyctl info 2
./battyctl send 2 'hello'
printf 'hello\n' | ./battyctl paste 2 -
./battyctl dump 2
./battyctl launch --target 2 --direction right -- bash
./battyctl launch --session work -- bash
./battyctl launch --session work --attach
./battyctl launch --session work --observe
./battyctl focus 2
./battyctl resize 2 horizontal 500
./battyctl rename 2 'Build workspace'
./battyctl rename 2 ''  # Restore the terminal-derived page title
```

Use IDs returned by `list`; the numbers above are examples. `launch` without
a target creates a new page. Arguments are passed directly to the new process,
without shell evaluation. `ping` reports the protocol version and operations.
Named sessions use the host's session directory selected when the endpoint
starts (`BATTY_SESSION_DIR` or its usual default). `--session` creates or
reconnects; `--attach` requires an existing session and takes no command;
`--observe` creates a view that cannot send input. Closing a view and attaching
again preserves the original process and terminal state. Listings include
`persistent`, `observe` and `session` fields. Pane IDs identify views, so a
reattached view receives a new pane ID even though its child PID is unchanged.
Other operations include `move`, `zoom`, `sync` and `close`. Responses are JSON,
except `dump`, which prints visible terminal text. Closing an explicitly named
pane detaches it. Closing an automatically generated Kilix pane terminates its
owner, matching the frontend close action.

`battyctl checkpoint` exports a version-1 JSON checkpoint in one host request:
`layout_format` / `layout_hex` contain the portable layout, `panes` contains
matching session identities and metadata, and `appearance` contains logical
window size, font, font size and chrome settings. Both writable and read-only
endpoints permit this operation. An oversized checkpoint is rejected as a whole.

`kilix save FILE` saves that checkpoint for a workspace whose panes all use
named persistent sessions. Use `--socket PATH` for an explicit host; otherwise
normal endpoint discovery applies. The parent directory must exist. Writes use
a private temporary file and atomic replacement; the resulting file is mode
0600. Existing symlinks and nonregular destinations are rejected. Version-2 saves
include private, checksum-verified output files in a sibling `.batty-output-*`
directory; keep that directory with the JSON file. Each output archive is at
most 128 MiB, and a workspace's archives total at most 256 MiB. Saving records
owner references, output, working directories and original command arguments. It does
not detach panes or stop processes. Restore it with `kilix --restore FILE` after closing the original controlling
frontend. Restore uses the saved window/font/chrome settings, checks the entire
file and native layout before attachment, and requires each saved owner epoch.
Replaced or already-controlled owners make restore fail; partial views
are detached without terminating their owners. Restored panes retain output when
their processes exit, including processes that had already exited at save time;
close those panes explicitly. Missing owners are replaced with fresh shells;
saved output is loaded directly into the terminal parser before PTY startup.
It is never sent as shell input. Use `kilix --restore FILE --restart-programs`
to explicitly restart the original commands for lost owners. This reruns their
argument vectors; it cannot resume process memory, editor buffers or animations.
Legacy version-1 saves can reattach surviving owners but have no durable output.
Local unnamed panes
cannot currently be saved or restored. User configuration and its ready hook
still run; saved appearance overrides startup appearance defaults.

Custom Bash hosts can use `batty workspace listen H PATH [--read-only]`.
The parent directory must already exist, be owned by the current user, and
deny access to other users. Existing paths are never replaced. The endpoint
accepts only same-user connections, uses mode 0600, and is removed when its
workspace closes. A read-only endpoint permits discovery, metadata and text
retrieval but rejects launches, input, renaming and layout changes.
The generic control endpoint detaches persistent views on `close`. A host may
opt into termination for its own generated session namespace with
`--terminate-prefix PREFIX`: names must have that prefix followed by 24
lowercase hexadecimal digits, and the owner must be in the endpoint's session
root. Kilix supplies `kilix-auto-` through this policy; Batty does not assign
special behavior to Kilix names on endpoints that omit it.
An unrestricted writable host can opt into `--host-actions`, which permits
`reload-settings` requests. Each request queues one host action and returns a
ticket; `reload-status` reports the host's completion code. Concurrent requests
for the same pending reload share its ticket. Hosts must finish it with
`batty workspace reload-complete H TICKET STATUS`; the Kilix frontend applies
supported shared settings and reports zero only after success. Generic control
endpoints do not advertise or accept host actions.
For a single pane, use `batty workspace listen H PATH --pane ID [--read-only]`.
Up to eight additional pane-scoped endpoints may be attached to a workspace.
They list, inspect, retrieve text from, and subscribe to changes for only that
pane. Writable scoped endpoints also permit `send` and `paste` to it. They do
not expose workspace-wide operations or other pane IDs. The endpoint remains
bound to the original ID if that pane closes; it cannot acquire another pane.
`--pane` and `--terminate-prefix` cannot be combined. Keep the workspace-wide
endpoint private when distributing a pane-scoped endpoint to another same-user
process; Unix same-user access is not an isolation boundary against a process
that can already open the workspace-wide socket.

The version 1 transport is Unix `SOCK_SEQPACKET`. Requests contain `BTC1`, a
one-byte opcode, three zero bytes, a little-endian 64-bit pane ID, then an
operation payload. Responses contain `BTC1`, a little-endian 32-bit status
(zero means success), then the body. One connection carries one request.
Packets are limited to 131,072 bytes; oversized text retrieval returns an error.
Sixteen clients may be pending, idle clients expire after one second, and each
pump services at most four requests in round-robin order. The Python client
in [tools/control.py](tools/control.py) defines operation codes and payloads.

`rename PANE TITLE` changes the page containing that pane. Opcode 16 carries
up to 255 bytes of printable UTF-8 without a NUL terminator; an empty payload
clears the custom name. Malformed input leaves the existing name unchanged.
Writable endpoints advertise `rename` in their capabilities. `kilix ls`
uses custom page names when present.
`pane-rename PANE TITLE` uses opcode 22 with the same title rules to set or
clear one pane's title override. The override is included in BWL2 checkpoints.
Writable endpoints also advertise `pane-center`. Opcode 18 takes no pane ID or
payload and opens the native all-page center. Read-only endpoints reject it.
`pane-center-state` (opcode 20) is read-only and reports whether the center is
open and the IDs of its displayed rows. The Kilix frontend uses that bounded
list to sample process and agent activity while the overlay is open, then sends
`telemetry` (opcode 19) on its writable endpoint. Each telemetry payload is an
activity name, process label and agent label separated by two NUL bytes;
malformed values are rejected and unrefreshed labels expire after 3.5 seconds.

`battyctl events --follow` emits a JSON snapshot followed by change batches.
Pane listings and info include one-based `page_index` from the native page
order; it updates when a page closes or a freed slot is reused.
Each response carries an endpoint `epoch` and event `cursor`; use
`events --epoch HEX --after CURSOR` to resume. Opcode 15 uses the request's
64-bit ID field for that cursor and an eight-byte little-endian epoch payload.
An initial request uses epoch zero. A matching cursor waits for changes or a
one-second heartbeat without blocking the workspace. Follow mode suppresses
empty heartbeats and exits with an error if the endpoint disappears.

The endpoint retains 256 events. A different epoch, a future cursor, or a
cursor older than the retained history returns `reset: true`, an empty event
list, and the current `panes` listing. Replace cached pane state on reset.
Otherwise events identify a pane and an `added`, `removed`, or `changed` kind.
Changed events carry a bitmask: 1 for layout/focus/geometry, 2 for title,
4 for terminal output, and 8 for process status. Clients fetch `info` or
`dump` as needed. Changes are coalesced between workspace pumps; notifications
are state invalidations, not an output byte stream or an audit log. Read-only
endpoints permit events. Subscriptions share the pending-client limit.

Frontends register beneath `BATTY_CONTROL_DIR`, defaulting to
`$XDG_RUNTIME_DIR/batty-control` or `/tmp/batty-control-UID`. This directory and
each frontend's subdirectory must be private and user-owned. Persistent
children inherit the registry path. If their original endpoint disappears or
no longer contains their pane, `kilix` and `battyctl` find the current
controlling view by matching process ancestry to live pane metadata. Observer
views and dead sockets are excluded; ambiguous or missing matches fail rather
than selecting another pane. Discovery checks at most 128 registry entries,
using eight concurrent probes with 300 ms timeouts. It does not delete stale
registry entries. Explicit `--socket` bypasses discovery for external tools.

Custom hosts that export only `BATTY_CONTROL` retain direct endpoint behavior.
Discovery requires a surviving process ancestry link to the PTY's initial
process; daemonized processes can use an explicit endpoint. Each endpoint
currently controls one workspace. Pane-scoped endpoints provide limited access
to an individual pane.

`./kilix share [--size 1280x800] [--fps 15] [--lan] [--audio]` starts a fresh
Batty Kilix desktop on a private X display and serves its full pixels through
the bundled provider. The command prints a per-run browser URL and VNC
credentials. Without `--lan`, access is loopback-only for local viewing or an
SSH tunnel; `--lan` enables the provider's TLS/token browser bridge. Direct
access from another machine also requires the host firewall and network to
permit inbound connections to the printed HTTPS port. The HLS
browser picture includes the whole desktop, and authenticated browser/VNC
control forwards input. The shared desktop has separate private PTY and
control roots, so it does not attach to the operator's existing panes.
If the configured runtime path would make its Unix sockets too long, the shared
desktop uses a short private temporary directory for those roots.
Stopping the share stops its automatic PTY owners and removes its runtime.

To view one named persistent Batty pane on another machine, start
`./kilix remote serve PANE` in its workspace. The command prints a frame port,
an input port, a controlling token, and a separate view token. A persistent
owner without a workspace can use
`./kilix remote serve --session NAME --session-dir PRIVATE_ROOT`. Forward
the frame port with an SSH local tunnel, then run
`./kilix remote view --port LOCAL_FRAME_PORT --token VIEW_TOKEN` on the viewing
machine for a display-only native Batty window. Forward the input port and add
`--input-port LOCAL_INPUT_PORT --observe` to allow pane-text queries while
remaining read-only. To type, paste, resize and send pointer events, use the
controlling token with `--input-port` and omit `--observe`. `--headless`
receives without opening a window. Interactive viewers can copy selected
terminal text and receive terminal-initiated OSC 52 clipboard writes while their clipboard
policy is active (`BATTY_CLIPBOARD=write ./kilix remote view ...`). Read-only
viewers cannot fetch clipboard contents. The server and viewer bind/connect
only to loopback addresses, and each viewer uses a separate local mirror. The view token
authorizes frames, audio and pane-text queries; the controlling token also
authorizes input and clipboard operations. Handle both as secrets. The
transport sends a complete semantic frame at startup and after reconnect. For
later same-size updates, it can send bounded changed-byte ranges against the
last revision; the mirror reconstructs and seals a complete local frame before
presenting it. Other updates use complete frames, compressed when the result
saves more than 10% and stays below 16 MiB. A dropped frame socket
reconnects to the same listening server and owner epoch without replacing the
local view. If an input acknowledgment is lost, that operation reports an
error and is never replayed; later input reconnects. After a server restart
with new ports or new tokens, a viewer started with `--name NAME` can run
`./kilix remote retarget --name NAME --port NEW_FRAME_PORT --token NEW_TOKEN`
on the viewing machine. Use the new view token for an observer or the new
control token for an interactive viewer. Include `--input-port NEW_INPUT_PORT`
for a viewer with pane-text queries or input, and the same `--session-dir`
supplied to `remote view`, if any. The native window remains open, and
retarget accepts only the same owner epoch. An active clipboard policy is
restored on the next input connection. If a retarget attempt fails
authentication or owner validation, the mirror keeps the last accepted frame
available to existing and new local viewers, marked disconnected until a valid
route resumes. For a pane-associated
audio source, add
`--audio-source 'COMMAND'` to `remote serve`; the command must write raw
signed-16-bit little-endian PCM (48 kHz stereo by default). The server prints
an audio port. Forward that port and add `--audio-port LOCAL_AUDIO_PORT` to
`remote view`. Playback uses `pacat` or `aplay`, or an explicit
`--audio-output 'COMMAND'` that reads PCM from standard input. The source
rate and channels can be set with `--audio-rate` and `--audio-channels`;
`--audio-budget` bounds each viewer's encoded audio bytes per second. Audio
uses a separate authenticated channel with bounded 20 ms blocks and a short
drop-oldest queue, so stalled playback cannot block terminal frames. Include
the new `--audio-port` when retargeting an audio viewer. Audio requires an
explicit source command; Batty does not automatically capture sound from a
PTY. Version 2 frame and audio records share the source monotonic clock; the
viewer maps them to one local timeline with an 80 ms playout buffer. Older
records remain readable without timed playout. Backend device latency is not
measured, so precise audio/video synchronization remains under development.

To serve several named persistent panes from one source workspace, run
`./kilix remote serve-group --all` or name 2–64 pane targets instead of `--all`.
It prints one versioned JSON record with each pane's source ID, owner epoch,
separate view and control tokens, frame/input ports and a read-only `observe`
route. When the selected owning panes cover the entire source workspace, a
version 3 record also carries its page names, split topology, ratios, focus
and source window size. `view-group` applies that layout to its local mirrors
after they connect. A selected subset gets a version 2 route-only record and
the viewer tiles four panes per page. The selection is fixed when the command
starts and excludes observer panes. Add `--follow` to `serve-group --all` to
emit newline-delimited layout updates after the version 3 manifest. Pass the
whole stream to `view-group --stream` on the viewing machine; it applies later
page names, split ratios, topology and focus changes. When source panes are
added or removed, a route refresh creates or closes the corresponding mirrors
in the same viewer window. A replaced pane owner receives a new route. The
initial frame/input ports and any later ports in route refreshes must be
reachable from the viewer. `ssh-group` starts the source stream over SSH and
creates local bridges for every frame/input route, including later additions:

```sh
./kilix remote ssh-group user@source-host \
  --remote-kilix /path/to/batty/kilix \
  --source-socket /path/to/source/control.sock
```

Use an SSH host alias for custom ports or keys. The source executable defaults
to `kilix` in the remote `PATH`; omit `--source-socket` when source control
discovery is unambiguous. Each active frame and input connection uses an SSH
stdio channel. `--control PANE_ID` permits input to a pane present at startup;
the default is read-only viewing. `--view-only` also removes control tokens
from the source stream. `ssh-group` closes its source process, local bridges
and viewer together. For a separately managed stream, forward its routes
through SSH and use `--port-map` for changed local port numbers. The stream
contains access tokens:
carry it over SSH and keep any saved copy private. If a source pane cannot be
served, the source reports the condition and stops layout updates while the
existing viewer stays open.
An opt-in integration run against a private localhost OpenSSH server exercised
the command channel, `ssh -W` frame/input bridges, controller input, live pane
membership updates and cleanup. A separate-machine route remains to test.
Add `--view-only` to emit
`null` control tokens, so a shared manifest grants only viewing and text
queries. A normal manifest includes control tokens even if its default routes
are observers. Use `umask 077` before redirecting either token-bearing output
to a file. After forwarding the frame and input ports through SSH, copy the JSON
record to an owned 0600 regular file on the viewer and run:

```sh
./kilix remote view-group --manifest routes.json \
  --port-map SOURCE_FRAME=LOCAL_FRAME --port-map SOURCE_INPUT=LOCAL_INPUT \
  --control PANE_ID
```

Use `--port-map` only for ports whose local forwarded numbers differ from the
source numbers; repeat it for each changed frame or input port. By default all
panes use their view tokens. Repeat `--control` for source pane IDs that should
accept input using their control tokens; `--control` is rejected for a
view-only manifest. The viewer rejects a symlink, a
permissive or foreign-owned manifest, duplicate pane IDs, conflicting ports
and unknown mappings before opening any mirrors. To enter routes manually,
repeat `--route` in source-pane order:

```sh
./kilix remote view-group \
  --route FRAME1,VIEW_TOKEN1,INPUT1,observe \
  --route FRAME2,CONTROL_TOKEN2,INPUT2,attach
```

`view-group` also accepts routes from separate `remote serve` commands. It
accepts 2–64 routes; manual routes tile four panes per page. `observe`
rejects local input while retaining pane-text queries for the Pane Center;
`attach` forwards input to that pane's owner. The short form
`FRAME,TOKEN` creates a display-only observer without pane-text queries, and
`FRAME,TOKEN,INPUT` creates a controlling pane. An optional fifth field after
the role specifies that pane's forwarded audio port; `--audio-output` applies
to audio routes. The viewer creates separate local mirrors and stops all of
them when its workspace closes. A route that fails at startup prevents the
workspace from opening and cleans up the other mirrors. Each source has its
own server, token pair and owner epoch. The version 3 layout is a startup
snapshot; later source edits and pane additions are not synchronized. A view
token can query text on a forwarded input port, but
the server rejects send, pointer, resize and clipboard requests made with it.

Pane listings and `info` report `cols`, `rows`, `cell_width` and `cell_height`.
The cell dimensions are the pixels reported to the PTY; multiplying them by
the column/row counts gives its terminal pixel size. Pane bounds also include
frontend chrome and padding, so graphical clients should use the PTY metrics
when calculating their content size. Changes to cell dimensions produce a
layout-change notification.

## Native images

Open a new Batty window showing Kitty and Sixel side by side:

```sh
./graphics-test
./graphics-test --image /path/to/image.png
./graphics-test --protocol sixel --image /path/to/image.jpg
./graphics-test --seconds 10
```

Press **1** for Kitty, **2** for Sixel, **3** for both, **R** to redraw, and
**Q** to close the demo. The generated fixture shows single-pixel checks,
gradients, rings and transparency. The status line reports terminal replies.
Resize the window to compare the images at different sizes. Inside a Batty
shell, use `./tools/graphics-demo.py` with the same options; **Q** returns to
the shell. `--once` writes a static frame on the current screen.

Image files need Pillow (`python3-pil` on Debian/Ubuntu); the generated demo
needs only Python's standard library. Kitty receives full RGBA pixels.
The demo's Sixel sender quantizes to 256 colors and composites fractional
alpha onto its dark background; fully transparent pixels stay transparent.

Batty supports direct Kitty RGB, RGBA and PNG uploads, chunking, zlib
compression, normal and relative placements, cropping, pixel offsets,
alpha blending, image layering and deletion. Images follow their screen
and scrollback positions. Sixel supports repeat runs, raster dimensions,
RGB/HLS palettes and transparent backgrounds. Mode 80 set places Sixel at
the page origin without moving the cursor; reset (the default) places it
at the cursor and permits scrolling. Modes 8452 and 1070 select rightward
cursor advancement and private palettes. These modes support queries.

Local Kitty shared-memory transfers (`t=s`) are supported alongside inline
image bytes. Batty consumes and unlinks each POSIX shared-memory object, which
allows bounded producer rings to reuse their slots. Root-frame edits (`a=f`)
update existing image pixels through either transport. Shared-memory names
refer to the terminal owner's host. Kilix also enables local file (`t=f`) and
one-shot temporary-file (`t=t`) transfers for its frame SDK. `t=t` accepts
marked files in a temporary directory and removes each after reading; `t=f`
leaves its source in place. Standalone Batty keeps file media disabled unless
`BATTY_KITTY_LOCAL_FILES=1`; set it to `0` in Kilix's config to disable them
there. File paths and shared-memory names resolve on the owner's host, so
remote producers should send inline data.

Kitty animations advance on a monotonic clock even when the child is idle;
finite loops and frame gaps follow the pinned Ghostty decoder. Unicode
placeholder cells render virtual image fragments and follow text overwrites.
Image placements rooted at those virtual placements follow the cells through
edits and scrolling. Kilix's
overlapping same-frame overwrite extension (`a=c,C=1,N=2`) is supported.
Unflagged overlap and overlapping alpha blending remain errors. `./kilix`
exports `KITTY_KILIX_RENDERING=1` so compatible frame presenters can use
scroll copies followed by damage updates.
Sixel uses image placements: clearing the screen
with `CSI 2 J` removes them, while partial text erases and text overwrites
do not erase individual image pixels. Opaque Sixel uses the terminal's
default background color.

Each terminal has a 64 MiB resident image budget. Each pane renderer caches at most
128 textures using a 64 MiB GPU budget; one workspace caps their aggregate at
128 MiB. A pane draws at most 16,384 visible placements. Decode/upload working
memory and image data retained by terminal owners are additional. Sixel dimensions
are limited to 4096×4096 and its encoded stream to 16 MiB. Device texture
limits also apply. These limits may reject or evict large images.

## Animated text graphics

Inside the existing Batty shell, run:

```sh
./tools/visual-demo.py
./tools/visual-demo.py --image /path/to/image.png
```

The demo shows animated true-color plasma, gradients, a checkerboard, and
text styles. Press **1**, **2** or **3** to choose a pattern, **Space** to
pause animation, and **Q** to return to the shell. Resize the window while
it runs. When an image is loaded, **4** returns to the image view. `--once`
prints a static view without entering the alternate screen.

To open a separate Batty window and print that window's graphics driver:

```sh
./visual-test
./visual-test --image /path/to/image.png
./visual-test --seconds 10 --fps 30
```

`--fps` requests a rate from the demo producer. Its displayed updates per
second measure produced terminal frames, not GPU presentation rate or input
latency. This exercises the PTY, VT parser and renderer together and is not
a standalone GPU benchmark.

Image viewing requires Pillow (`python3-pil` on Debian/Ubuntu). It converts
images into true-color **half-block characters**, with two sampled image
pixels per terminal cell. Resolution and aspect depend on the cell grid;
transparency is composited onto the dark background. Use `graphics-test`
above for images at native pixel resolution.

`./batty --gpu-info` prints GL_VENDOR, GL_RENDERER, GL_VERSION and GLSL from
the actual window context. Renderer names containing `llvmpipe` or `softpipe`
indicate software rendering. The controller API is
`batty graphics HANDLE [-V VARIABLE]`; the child shell cannot access its
parent controller's handles.

## Desktop launcher

```sh
./kilix --install-desktop
./kilix --uninstall-desktop
```

Installation adds **Kilix — Batty** to the user application menu, with its own
`batty-kilix` icon. The frontend sets its X11 window class and Wayland application
ID to `batty-kilix` during video initialization, matching the desktop entry.
These temporary SDL settings do not replace pane applications' inherited identity
settings. The automated window-class check runs on X11. Nested Weston tests
exercise Wayland GLES rendering and keyboard input; application-ID behavior in
a user Wayland session remains to verify.

Installation uses `XDG_DATA_HOME` (default `~/.local/share`) for the
desktop entry and icon, and `XDG_STATE_HOME` (default `~/.local/state`) for a
private ownership record under `batty/`. It launches this checkout; rerun the
installer after moving or replacing the checkout. Installation does not build
the runtime: the normal launcher performs its usual runtime checks on first use.

The installer preserves unrelated launchers, including the Kitty-based Kilix
installation. It refuses to overwrite or uninstall files whose contents differ
from its ownership record. Restore a modified file to its installed contents
before retrying, or manage that customized entry yourself. Interrupted installs
can be retried. Uninstall removes only the recorded entry and icon; shared XDG
directories and the installer lock remain. If `XDG_DATA_HOME` changes, uninstall
the previous entry before installing into the new directory.

## Validation and limits

```sh
./test.sh
```

Tests require Xvfb and exercise PTY backpressure with 2 MiB of binary input,
terminal replies, initial geometry, resize, Bash job control, independent
statuses, failed exec, bounded shutdown, output flooding and descriptor
cleanup. Window checks exercise keyboard input, clipboard paste, selection,
mouse reporting, text rendering and resize. Native graphics tests check
decoded colors and framebuffer pixels, transparency, cropping, image-only
updates, all image layers, relative placements, scrolling, resizing and
malformed input recovery. A separate test runs the real graphics demo
through the PTY. Vim and less smoke tests run when those programs are
installed. Results and rendered PPMs are saved under `build/`; PNGs are
also saved when Pillow is available.

Workspace checks run real child processes across multiple tabs, shared
surfaces and independent OS windows. They verify input targeting, synchronized
typing, image clipping, pane geometry, independent exit statuses and persistent
view reattachment. Persistent graphics checks also animate a 1920×1080 RGBA
image with bounded rectangle edits, an active observer and a lagging observer.
They verify incremental payload/upload volumes, stable texture caches, idle
polls and full-snapshot recovery. Fixture timings are printed as observations;
the tests impose no hardware frame-rate promise.
Layout checks exercise directional splits and thousands of
resize, move and close operations for complete coverage without overlap.

The automated graphics checks use Xvfb and Mesa software rendering. Native
image framebuffer checks also passed on an Intel UHD Graphics 630 under
Openbox. An isolated nested Weston session exercised Batty's Wayland GLES
renderer, a graphical Kilix dashboard, the bundled desktop, and keyboard input.
The short `visual-test` demo also completed on the active X11 session with Mesa's
Intel UHD Graphics 630 hardware renderer.
Hardware GPU performance and full user-session Wayland behavior remain untested.
The single-session launcher has no tab/split UI or search, and sessions are not
restored after service restart. It displays IME preedit text at the cursor and
accepts committed text; paragraph bidirectional layout and complete extended keyboard
protocol support are unfinished.
Unchanged frames are skipped; changed frames redraw the visible grid using
a glyph atlas. Hooks and rendering share the controller thread.

The original [probe.sh](probe.sh) remains a separate headless experiment
using bash-os's `pty`, `vt` and `bashpoll` builtins. It is not used by the
desktop implementation.

Original project code is MIT licensed. Dependencies retain their own
licenses; the Ghostty license is copied beside the built library. The cached
bash-os executable includes GNU Bash and is GPLv3 licensed; its source and
license notices are retained with that private build.
The bundled Kilix 95 desktop is GPLv3; its content, state and telemetry
libraries are MIT. Their licenses and upstream file hashes accompany the
runtime source under `third_party/`.
