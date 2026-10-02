# Kilix application provider

Runtime source subset from [Kilix](https://github.com/itsmygithubacct/kilix),
pinned by `upstream.json`. The manifest records original source hashes,
explicit integration patches, and hashes of patched files. Tests reverse
the patches and verify the original source hashes. The GPLv3 license is included.

Batty's `tools/kilix_apps.py` supplies its launcher, private storage paths and
bundled presenter import path. This package supplies the X11 application
provider, terminal parser, capture/input helpers, private-display supervisor,
browser profile isolation and optional streaming bridge. `browse.py` is
included because the application provider shares its terminal parser. The XDG
application scanner supplies installed application discovery. Only the SDK
modules needed by these integrations are included; this is not a complete
Kilix SDK distribution.

Updates must copy committed source and retain its license and provenance.
Integration changes belong in Batty's adapter unless an upstream change is
explicitly recorded and reviewed.

The resize patch paints a fresh or retained application frame after updating
layout and clearing old placements, so idle applications remain visible.
The WebRTC authentication patch grants anonymous loopback clients publish-only
access; viewer reads require the per-run token, including over SSH forwarding.
The MediaMTX pin patch verifies both the [v1.9.3 release archive](https://github.com/bluenviron/mediamtx/releases/tag/v1.9.3)
and the cached executable for each supported Linux architecture before running it.
The LAN WebRTC patch reuses the provider's private TLS certificate for the
MediaMTX viewer and prints HTTPS connection links.
The VA-API patch probes render nodes for working H.264 encoding before choosing
one, so dual-GPU hosts do not select an unusable first node. It falls back to
software H.264 when no usable hardware encoder is found.
The WebRTC audio patch publishes Opus over RTSP for MediaMTX to forward to
WebRTC viewers. HLS and MSE continue using AAC.
