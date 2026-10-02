"""Terminal input and raw-mode helpers for Kilix-hosted applications."""

import browse as _browse

CSI_RE = _browse.CSI_RE
SPECIAL_CSI = _browse.SPECIAL_CSI
SPECIAL_TILDE = _browse.SPECIAL_TILDE
SPECIAL_U = _browse.SPECIAL_U
Term = _browse.Term
cdp_mods = _browse.cdp_mods

__all__ = [
    "CSI_RE",
    "SPECIAL_CSI",
    "SPECIAL_TILDE",
    "SPECIAL_U",
    "Term",
    "cdp_mods",
]
