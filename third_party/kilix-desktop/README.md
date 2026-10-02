# Kilix 95 desktop

Runtime source from [Kilix](https://github.com/itsmygithubacct/kilix), pinned by
`upstream.json`. The manifest lists the SHA-256 of each committed upstream file
and, where adapted, the SHA-256 of the Batty copy. The GPLv3 license is included.

`src/` preserves the source layout expected by the desktop. Its `kilix` and
`third_party/` symlinks point to Batty's launcher and the separately bundled
runtime dependencies. The desktop launcher sets `KILIX_HOME` to this tree and
uses the Batty Kitty-remote adapter for terminal-page and OS-window launches.
In bundled Batty mode, `kilix desktop --app ID` also accepts pinned catalog
application IDs and opens them through the desktop's managed XPane surface.
`kilix desktop --open PATH` sends an existing file or directory through the
desktop's file-association handler at startup.

The Settings app shows only shared choices implemented by Batty, writes the
shared settings file, and requests a live reload from the Batty frontend.
The desktop's original Kitty configuration editor is hidden in this mode.
Checkout-specific installer and update scripts are omitted. The bundled mode
uses Batty's Pane Center for the PTY Sessions action. Mux Terminal opens a
named persistent Batty session, and the source checkout's system update menu
is absent. This is a runtime subset, not a complete Kilix distribution.
Its adapted stream supervisor probes VA-API render nodes before enabling H.264
hardware encoding and falls back to a software encoder when necessary. It
shares the bundled application provider's pinned MediaMTX verification,
token-only WebRTC viewer access, LAN TLS requirement and Opus audio encoding.
