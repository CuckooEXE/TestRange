"""Strip terminal control sequences from guest console output (CORE-6).

A build VM's serial console is raw guest terminal output: ANSI/CSI colour and
cursor-movement escapes, OSC title-setting, carriage returns, and stray C0
control bytes. Echoed verbatim to the operator's terminal (the live console
mirror) or folded into a captured failure log, those bytes hijack the display —
clear-screens, cursor jumps, and overwrites were observed in live PVE runs — and
garble the saved log. This scrubs them to printable text, keeping only the line
structure (newlines and tabs).
"""

from __future__ import annotations

import re

# OSC (Operating System Command): ESC ] ... terminated by BEL or ST (ESC \).
# Matched first because ']' (0x5d) falls inside the generic Fe range below,
# which would otherwise strip the introducer and leave the title body behind.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

# CSI (Control Sequence Introducer): ESC [ <params 0x30-0x3f> <intermediates
# 0x20-0x2f> <final 0x40-0x7e>. Covers colour (m), cursor movement (H/A/B/…),
# erase (J/K), and the cursor-position-report response (ESC [ <row> ; <col> R)
# a guest emits when something queries it with ESC [ 6 n.
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Remaining escapes: a single Fe byte (ESC c reset, 0x40-0x5f sans the CSI/OSC
# introducers handled above) or an nF/charset sequence (ESC ( B and friends).
_ESC = re.compile(r"\x1b(?:[@-Z\\-_]|[ -/]*[0-~])")

# C0 control bytes and DEL, minus the two kept for structure: TAB (0x09) and
# LF (0x0a). This is what drops embedded carriage returns (0x0d) — the overwrite
# culprit — plus BEL, backspace, etc.
_C0 = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def scrub_terminal_control(text: str) -> str:
    """Remove ANSI/CSI/OSC escape sequences and C0 control bytes from ``text``.

    Preserves newlines and tabs so line structure survives; everything else in
    the control range — including embedded carriage returns and DEL — is dropped.
    """
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    return _C0.sub("", text)


__all__ = ["scrub_terminal_control"]
