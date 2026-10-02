"""kilix — WebSocket bridge + static server for the browser streaming tiers.

Spawned by `kilix run --serve`/`kilix share` when a browser tier is requested.
It:
  - bridges  /websockify  (binary WebSocket) <-> the loopback RFB port, so a
    browser running vendored noVNC can view+control the Xvnc session;
  - fans out /ts  — a live MPEG-TS feed (H.264+AAC) pushed by ffmpeg to a
    loopback TCP port — to any number of WebSocket viewers; /watch plays it
    with vendored mpegts.js over MSE at ~0.3-1 s glass latency (E1);
  - serves the vendored noVNC app (view+control) and an HLS <video> page
    (view-only, many devices) plus the fMP4 HLS segments, with cache headers;
  - token-gates the stream/control endpoints (a ?t=<token> query sets a cookie;
    later requests present the cookie). noVNC's/player static assets are public
    library code and are not gated — the RFB session is still password-protected.

Binds loopback by default; with --host 0.0.0.0 + --tls-cert/--tls-key it serves
the LAN over HTTPS. The token comes from the env (KILIX_BRIDGE_TOKEN), never argv,
so it is not visible in `ps`. Uses only stdlib + `websockets` (10.x legacy API).
"""
import argparse
import asyncio
import hmac
import http
import mimetypes
import os
import ssl
import urllib.parse

import warnings
import websockets

# websockets compat: use the legacy asyncio server API explicitly. It exists on
# both 10.x (where it is the default `websockets.serve`) and 15.x (where the
# default switched to a new, incompatible API but legacy is still shipped). This
# keeps the ws_handler(ws, path) / process_request(path, headers) shape working
# on either version. Silence the 14+ deprecation notice.
warnings.filterwarnings("ignore", category=DeprecationWarning)
try:
    from websockets.legacy.server import serve as ws_serve
except Exception:                          # very old websockets
    from websockets.server import serve as ws_serve

mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/iso.segment", ".m4s")
mimetypes.add_type("text/javascript", ".js")

TS_PKT = 188                               # MPEG-TS packet size


def _ct_eq(a, b):
    """Constant-time compare that never raises on non-ASCII input
    (hmac.compare_digest(str,str) TypeErrors on non-ASCII)."""
    try:
        return hmac.compare_digest(a.encode("utf-8", "ignore"), b.encode())
    except Exception:
        return False

LANDING = """<!doctype html><meta charset=utf-8><title>kilix</title>
<style>body{{background:#1d1f21;color:#d3d7cf;font:16px system-ui;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
a{{display:block;margin:1em;padding:1em 2em;background:#3465a4;color:#fff;
text-decoration:none;border-radius:6px}}a.v{{background:#2e3436}}</style>
<div><h2>kilix — {what}</h2>{links}</div>"""

# noVNC quality/compression: Tight with stronger zlib — the text-efficiency
# tier (E6); H.264 stays the video tier.
LINK_VNC = ('<a href="vnc.html?path=websockify&resize=scale&autoconnect=1'
            '&quality=6&compression=9">Control (noVNC) →</a>')
LINK_TS = '<a href="watch">Watch — low latency (~0.5 s) →</a>'
LINK_HLS = '<a class=v href="view">Watch — HLS (view-only, scales out) →</a>'

VIEWER = """<!doctype html><meta charset=utf-8><title>kilix — view</title>
<style>html,body{margin:0;background:#000;height:100%}
video{width:100%;height:100%;object-fit:contain}</style>
<video id=v controls autoplay muted playsinline></video>
<script src="/hlsjs/hls.min.js"></script><script>
var v=document.getElementById('v'),src='/hls/live.m3u8';
if(window.Hls&&Hls.isSupported()){
  var h=new Hls({liveSyncDurationCount:2,liveMaxLatencyDurationCount:6,
                 maxLiveSyncPlaybackRate:1.5});
  h.loadSource(src);h.attachMedia(v);}else{v.src=src;}
</script>"""

WATCH = """<!doctype html><meta charset=utf-8><title>kilix — live</title>
<style>html,body{margin:0;background:#000;height:100%}
video{width:100%;height:100%;object-fit:contain}
#e{color:#d3d7cf;font:15px system-ui;padding:2em}</style>
<video id=v controls autoplay muted playsinline></video>
<script src="/mpegtsjs/mpegts.js"></script><script>
if(window.mpegts&&mpegts.getFeatureList().mseLivePlayback){
  var url=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/ts';
  var p=mpegts.createPlayer({type:'mse',isLive:true,url:url},
    {enableStashBuffer:false,liveBufferLatencyChasing:true,
     liveBufferLatencyMaxLatency:1.5,liveBufferLatencyMinRemain:0.2});
  p.attachMediaElement(document.getElementById('v'));p.load();p.play();
}else{
  document.body.innerHTML='<div id=e>This browser lacks MSE live playback — '
    +'use the <a href="view" style="color:#729fcf">HLS viewer</a>.</div>';
}
</script>"""


class TSFan:
    """Accepts the ffmpeg MPEG-TS feed on a loopback TCP port and fans the
    packet stream out to WebSocket viewer queues. Chunks are aligned to whole
    188-byte TS packets; a viewer that can't keep up is kicked (bounded queue),
    never buffered unboundedly."""

    def __init__(self):
        self.clients = set()               # asyncio.Queue per viewer
        self.buf = b""
        self._server = None                # kept: GC would close the listener

    async def listen(self, host, port):
        self._server = await asyncio.start_server(self._feed, host, port)

    async def _feed(self, reader, writer):
        # newest encoder connection wins (a restarted ffmpeg just reconnects)
        try:
            while True:
                data = await reader.read(1 << 16)
                if not data:
                    break
                self.buf += data
                cut = len(self.buf) - (len(self.buf) % TS_PKT)
                if cut:
                    chunk, self.buf = self.buf[:cut], self.buf[cut:]
                    self._broadcast(chunk)
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _broadcast(self, chunk):
        for q in list(self.clients):
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:      # slow viewer: kick, don't buffer
                self.clients.discard(q)
                try:
                    q.get_nowait()
                    q.put_nowait(None)     # sentinel unblocks its sender
                except asyncio.QueueEmpty:
                    pass

    async def client(self, ws):
        q = asyncio.Queue(maxsize=256)
        self.clients.add(q)
        try:
            while True:
                chunk = await q.get()
                if chunk is None:
                    break
                await ws.send(chunk)
        except Exception:
            pass
        finally:
            self.clients.discard(q)


class Bridge:
    def __init__(self, a):
        self.rfb_port = a.rfb_port
        self.ts_port = a.ts_port
        self.token = os.environ.get("KILIX_BRIDGE_TOKEN", "")
        self.novnc = os.path.realpath(a.novnc) if a.novnc else None
        self.hlsjs = os.path.realpath(a.hlsjs) if a.hlsjs else None
        self.mpegtsjs = os.path.realpath(a.mpegtsjs) if a.mpegtsjs else None
        self.hls = os.path.realpath(a.hls) if a.hls else None
        self.what = a.what
        self.tsfan = TSFan() if a.ts_port else None

    # ---- auth ---------------------------------------------------------------
    def _authed(self, path, headers):
        if not self.token:
            return True                      # no token configured => open (loopback)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        tok = (q.get("t") or [None])[0]
        if tok and _ct_eq(tok, self.token):
            return True
        for part in (headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "kilix_t" and _ct_eq(v, self.token):
                    return True
        return False

    def _has_query_token(self, path):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        tok = (q.get("t") or [None])[0]
        return bool(tok and _ct_eq(tok, self.token))

    # ---- HTTP responses -----------------------------------------------------
    @staticmethod
    def _resp(status, body, ctype="text/plain", cookie=None, cache=None):
        if isinstance(body, str):
            body = body.encode()
        hdrs = [("Content-Type", ctype), ("Content-Length", str(len(body)))]
        if cache:
            hdrs.append(("Cache-Control", cache))
        if cookie:
            hdrs.append(("Set-Cookie",
                         f"kilix_t={cookie}; Path=/; SameSite=Strict"))
        return http.HTTPStatus(status), hdrs, body

    def _file(self, root, rel, cookie=None):
        if not root:
            return self._resp(404, b"not found")
        rel = urllib.parse.unquote(rel)
        full = os.path.normpath(os.path.join(root, rel))
        if full != root and not full.startswith(root + os.sep):
            return self._resp(403, b"forbidden")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._resp(404, b"not found")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        # cache policy: the live playlist must revalidate every fetch; segments
        # are content-unique (sequence-numbered) and short-lived; library js is
        # a pinned vendored version
        if full.endswith(".m3u8"):
            cache = "no-cache"
        elif full.endswith((".ts", ".m4s", ".mp4")):
            cache = "max-age=60, immutable"
        elif full.endswith((".js", ".css", ".woff2")):
            cache = "max-age=86400"
        else:
            cache = None
        with open(full, "rb") as f:
            body = f.read()
        return self._resp(200, body, ctype, cookie, cache)

    # process_request: return None only to let a WS route proceed;
    # every other route returns a full HTTP response.
    async def process_request(self, path, headers):
        route = urllib.parse.urlsplit(path).path
        cookie = self.token if self._has_query_token(path) else None
        if route == "/websockify" and self.rfb_port:
            return None if self._authed(path, headers) else self._resp(401, b"unauthorized")
        if route == "/ts" and self.tsfan:
            return None if self._authed(path, headers) else self._resp(401, b"unauthorized")
        if route in ("/", "/index.html"):
            if not self._authed(path, headers):
                return self._resp(401, b"unauthorized - append ?t=<token>")
            links = "".join([LINK_VNC if self.rfb_port else "",
                             LINK_TS if self.tsfan else "",
                             LINK_HLS if self.hls else ""])
            return self._resp(200, LANDING.format(what=self.what, links=links),
                              "text/html", cookie)
        if route == "/view" and self.hls:
            if not self._authed(path, headers):
                return self._resp(401, b"unauthorized")
            return self._resp(200, VIEWER, "text/html", cookie)
        if route == "/watch" and self.tsfan:
            if not self._authed(path, headers):
                return self._resp(401, b"unauthorized")
            return self._resp(200, WATCH, "text/html", cookie)
        if route.startswith("/hls/"):
            if not self._authed(path, headers):
                return self._resp(401, b"unauthorized")
            return self._file(self.hls, route[len("/hls/"):], cookie)
        if route.startswith("/hlsjs/"):
            return self._file(self.hlsjs, route[len("/hlsjs/"):])
        if route.startswith("/mpegtsjs/"):
            return self._file(self.mpegtsjs, route[len("/mpegtsjs/"):])
        if not self.rfb_port:
            return self._resp(404, b"not found")
        # everything else: vendored noVNC static assets (public library code)
        return self._file(self.novnc, route.lstrip("/") or "vnc.html")

    # ---- WebSocket handlers -------------------------------------------------
    async def ws_handler(self, ws, path):
        if not self._authed(path, ws.request_headers):
            await ws.close(1008, "unauthorized")
            return
        route = urllib.parse.urlsplit(path).path
        if route == "/ts" and self.tsfan:
            await self.tsfan.client(ws)
            return
        await self._rfb_bridge(ws)

    async def _rfb_bridge(self, ws):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", self.rfb_port)
        except OSError:
            await ws.close(1011, "backend unavailable")
            return

        async def ws_to_tcp():
            try:
                async for msg in ws:
                    writer.write(msg if isinstance(msg, bytes) else msg.encode())
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        async def tcp_to_ws():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send(data)
            except Exception:
                pass
            finally:
                try:
                    await ws.close()
                except Exception:
                    pass

        await asyncio.gather(ws_to_tcp(), tcp_to_ws())


async def _main(a):
    b = Bridge(a)
    ssl_ctx = None
    if a.tls_cert and a.tls_key:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(a.tls_cert, a.tls_key)
    if b.tsfan:
        # the encoder's TS feed arrives on loopback only, whatever --host is
        await b.tsfan.listen("127.0.0.1", a.ts_port)
    async with ws_serve(b.ws_handler, a.host, a.http_port,
                        process_request=b.process_request,
                        subprotocols=["binary"], ssl=ssl_ctx,
                        max_size=None, ping_interval=None):
        await asyncio.Future()      # run forever


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rfb-port", type=int, default=0)
    ap.add_argument("--http-port", type=int, required=True)
    ap.add_argument("--ts-port", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--novnc", default=None)
    ap.add_argument("--hlsjs", default=None)
    ap.add_argument("--mpegtsjs", default=None)
    ap.add_argument("--hls", default=None)
    ap.add_argument("--tls-cert", default=None)
    ap.add_argument("--tls-key", default=None)
    ap.add_argument("--what", default="session")
    a = ap.parse_args()
    try:
        asyncio.run(_main(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
