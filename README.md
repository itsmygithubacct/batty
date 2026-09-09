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
that compiler or let the build download a checksum-verified Linux x86_64
compiler into `.cache/`. Build products stay under `build/`; nothing is
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

The service parses PTY output from child startup and supplies complete,
versioned frames through sealed memory files over a local Unix socket.
Windows request the latest frame; an idle observer does not accumulate an
output queue. Frames have a 128 MiB limit, including at most 64 MiB of image
pixels and 16,384 placements. User input can occupy up to 7 MiB of the native
8 MiB queue; the remaining capacity is reserved for terminal replies and
control events. The service keeps the original command's exit status while
continuing PTY I/O for descendants until the PTY closes. Explicit termination
waits for descendant cleanup and removes the session endpoint before returning.

State remains in the running service's memory. Reconnection survives a
window or controller closing, including an abrupt controller exit. It does
not restore sessions after service failure, logout policies that stop the
service, or a machine restart. Sessions without a name retain the original
behavior: closing their window stops their command.

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
focus reports are encoded according to terminal modes. OSC clipboard access
is disabled; clipboard operations require the local shortcuts.

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

There can be one window and up to sixteen total handles in a controller.
Headless sessions use the same PTY engine and require no display. The supplied
launcher manages one window and returns its command's exit status.
`close` disconnects a named handle. `list` and `terminate` use session names
and require no handle. Observer handles reject `send`, `paste` and `resize`.

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

Automatic Kitty animation playback and Unicode placeholder placements are
unfinished. File, temporary-file and shared-memory transfers are disabled;
send image bytes directly. Sixel uses image placements: clearing the screen
with `CSI 2 J` removes them, while partial text erases and text overwrites
do not erase individual image pixels. Opaque Sixel uses the terminal's
default background color.

Each terminal has a 64 MiB resident image budget. Each window caches at most
128 textures using a 64 MiB GPU budget and draws at most 16,384 visible
placements. Decode/upload working memory is additional. Sixel dimensions
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

The automated graphics checks use Xvfb and Mesa software rendering. Native
image framebuffer checks also passed on an Intel UHD Graphics 630 under
Openbox. GPU performance and native Wayland behavior remain untested.
This is a prototype, with one window per controller and no tabs, splits,
search or restoration after service restart. It accepts committed IME text but does not display
preedit text; paragraph bidirectional layout and complete extended keyboard
protocol support are unfinished. A block cursor is drawn as an outline,
some underline styles use a straight line, and selection does not autoscroll.
Unchanged frames are skipped; changed frames redraw the visible grid using
a glyph atlas. Hooks and rendering share the controller thread.

The original [probe.sh](probe.sh) remains a separate headless experiment
using bash-os's `pty`, `vt` and `bashpoll` builtins. It is not used by the
desktop implementation.

Original project code is MIT licensed. Dependencies retain their own
licenses; the Ghostty license is copied beside the built library. The cached
bash-os executable includes GNU Bash and is GPLv3 licensed; its source and
license notices are retained with that private build.
