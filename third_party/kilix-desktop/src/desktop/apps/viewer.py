"""kilix desktop — image viewer. Fit-to-window, resize rescales."""
import os

from PIL import Image

import theme as T
import wm


class Viewer(wm.Window):
    def __init__(self, desk, path):
        path = os.path.expanduser(path or "")
        try:
            self.pic = Image.open(path).convert("RGB")
        except (OSError, ValueError) as e:
            self.pic = None
            err = str(e)
        sw, sh = desk.size()
        if self.pic:
            iw, ih = self.pic.size
            w = min(iw + 2 * T.BORDER + 8, sw - 60, 900)
            h = min(ih + 2 * T.BORDER + T.TITLE_H + 8, sh - T.TASKBAR_H - 40)
            title = f"{os.path.basename(path)} ({iw}×{ih})"
        else:
            w, h, title = 320, 140, "Image Viewer"
        super().__init__(desk, title, w, h, icon="doc_image")
        self.min_w, self.min_h = 160, 120
        self._scaled = None
        if self.pic:
            desk.shell.add_recent(path)
        else:
            wm.msgbox(desk, "Image Viewer", err, icon="error")

    def on_resize(self):
        self._scaled = None

    def draw_client(self, d, img):
        cw, ch = self.client_size()
        d.rectangle([0, 0, cw - 1, ch - 1], fill=T.SHADOW)
        if not self.pic:
            return
        if self._scaled is None or getattr(self, "_scaled_for", None) != (cw, ch):
            iw, ih = self.pic.size
            s = min((cw - 4) / iw, (ch - 4) / ih, 1.0)
            self._scaled = self.pic.resize((max(1, int(iw * s)),
                                            max(1, int(ih * s))))
            self._scaled_for = (cw, ch)
        pw, ph = self._scaled.size
        img.paste(self._scaled, ((cw - pw) // 2, (ch - ph) // 2))
