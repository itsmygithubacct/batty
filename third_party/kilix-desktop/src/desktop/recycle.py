"""kilix desktop — Recycle Bin backing store (no UI).

Deleted files/dirs are moved under Kilix's private data directory into a
per-item opaque token, beside a JSON sidecar recording the
original path, deletion time, name, size and is_dir. The sidecars ARE the
index: items() rescans them each call, so a missing or corrupt sidecar
degrades to a best-effort entry instead of desyncing the bin. No network.

Location: $KILIX_RECYCLE_DIR wins; else the bin sits beside the desktop
folder (a sibling of $KILIX_DESKTOP_DIR when set — so redirecting the desktop
to a temp dir isolates the bin too); else the canonical Kilix data root.
"""
import json
import math
import os
import shutil
import time
import uuid

import storage


def _base():
    over = os.environ.get("KILIX_RECYCLE_DIR")
    if over:
        return os.path.expanduser(over)
    desk = os.environ.get("KILIX_DESKTOP_DIR")
    if desk:
        return os.path.join(os.path.dirname(
            os.path.abspath(os.path.expanduser(desk))), "recycled")
    return storage.data_dir("recycled")


def _store():
    fdir = os.path.join(_base(), "files")
    os.makedirs(fdir, exist_ok=True)
    return fdir


def _tree_size(path):
    if os.path.islink(path) or not os.path.isdir(path):
        try:
            return os.lstat(path).st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for n in files:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total


def _unique(path):
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.lexists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def _token_path(fdir, token):
    if (not isinstance(token, str) or not token or token in (".", "..")
            or "\0" in token or os.path.isabs(token)
            or os.path.basename(token) != token
            or (os.path.altsep and os.path.altsep in token)):
        raise KeyError(token)
    return os.path.join(fdir, token)


def _load_info(path):
    try:
        with open(path) as f:
            info = json.load(f)
        return info if isinstance(info, dict) else {}
    except (OSError, ValueError):
        return {}


def _finite_number(value, default):
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value))):
        return value
    return default


def _item_from_info(token, path, st):
    info = _load_info(path + ".info")
    name = info.get("name")
    if not isinstance(name, str) or not name:
        name = token
    orig = info.get("orig")
    if not isinstance(orig, str):
        orig = ""
    is_dir = info.get("is_dir")
    if not isinstance(is_dir, bool):
        is_dir = os.path.isdir(path) and not os.path.islink(path)
    when = float(_finite_number(info.get("when"), st.st_mtime))
    size = int(max(0, _finite_number(info.get("size"), st.st_size)))
    return {"token": token, "orig": orig, "name": name, "when": when,
            "size": size, "is_dir": is_dir}


def send(path):
    """Move path into the bin; return its token."""
    path = os.path.abspath(os.path.expanduser(path))
    fdir = _store()
    token = uuid.uuid4().hex
    is_dir = os.path.isdir(path) and not os.path.islink(path)
    info = {"orig": path,
            "name": os.path.basename(path.rstrip("/")) or path,
            "when": time.time(),
            "size": _tree_size(path),
            "is_dir": is_dir}
    dest = os.path.join(fdir, token)
    info_path = dest + ".info"
    tmp_info = dest + ".info.tmp"
    try:
        with open(tmp_info, "w") as f:
            json.dump(info, f)
    except Exception:
        try:
            os.unlink(tmp_info)
        except OSError:
            pass
        raise
    try:
        shutil.move(path, dest)     # may raise OSError
    except Exception:
        try:
            os.unlink(tmp_info)
        except OSError:
            pass
        raise
    try:
        os.replace(tmp_info, info_path)
    except Exception:
        try:
            shutil.move(dest, path)
        except Exception:
            pass
        try:
            os.unlink(tmp_info)
        except OSError:
            pass
        raise
    return token


def items():
    """List of {token, orig, name, when, size, is_dir}, newest first."""
    fdir = _store()
    out = []
    for n in sorted(os.listdir(fdir)):
        if n.endswith(".info") or n.endswith(".info.tmp"):
            continue
        p = os.path.join(fdir, n)
        try:
            st = os.lstat(p)
        except OSError:
            continue
        out.append(_item_from_info(n, p, st))
    out.sort(key=lambda i: i["when"], reverse=True)
    return out


def has_items():
    """True if the bin holds anything. Cheap: no sidecar reads (for tick hooks)."""
    return any(not (n.endswith(".info") or n.endswith(".info.tmp"))
               for n in os.listdir(_store()))


def restore(token):
    """Move a token back to its original path (disambiguated if occupied);
    return the path it landed at."""
    fdir = _store()
    src = _token_path(fdir, token)
    if not os.path.lexists(src):
        raise KeyError(token)
    info = _load_info(src + ".info")
    orig = info.get("orig")
    name = info.get("name")
    if not isinstance(name, str) or not name:
        name = token
    dest = orig if isinstance(orig, str) and orig else os.path.join(
        os.path.expanduser("~"), name)
    try:
        os.makedirs(os.path.dirname(dest) or "/", exist_ok=True)
    except OSError:
        pass
    dest = _unique(dest)
    shutil.move(src, dest)
    try:
        os.unlink(src + ".info")
    except OSError:
        pass
    return dest


def purge(token):
    """Permanently remove one token from the bin."""
    fdir = _store()
    try:
        p = _token_path(fdir, token)
    except KeyError:
        return
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        try:
            os.unlink(p)
        except OSError:
            pass
    try:
        os.unlink(p + ".info")
    except OSError:
        pass


def empty():
    """Purge every item."""
    for it in items():
        purge(it["token"])
