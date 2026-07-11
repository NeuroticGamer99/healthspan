"""Recovery Kit rendering (ADR-0013, ADR-0028, ADR-0033).

Renders in memory to text: the secret key in grouped Base32, a QR code
encoding the same string (Unicode half-block cells — scannable from a
screen or a monospace printout), and the custody instructions. OS print
pathways (``lp``/``lpr``, Windows shell print) are a later work item;
until then the kit is displayed and, only by explicit ``--output``
choice, written to a warned-about file.
"""

import io
from pathlib import Path

import qrcode

from healthspan.fsperm import set_owner_only
from healthspan.kdf import encode_secret_key
from healthspan.keyparams import utc_now_iso

# ADR-0033: recognizable naming, matched by the repo .gitignore pattern.
KIT_FILENAME_TEMPLATE = "healthspan-recovery-kit-{date}.txt"

OUTPUT_WARNING = (
    "This file contains the secret key. Store it only on encrypted storage "
    "(a password manager attachment or an encrypted volume). A digital kit "
    "lingering on unencrypted or synced storage collapses the two-factor "
    "model toward passphrase-only strength (ADR-0033)."
)


def render_kit(secret_key: bytes) -> str:
    """Render the full Recovery Kit as text (in memory, ADR-0033)."""
    b32 = encode_secret_key(secret_key)
    lines = [
        "=" * 68,
        "HEALTHSPAN RECOVERY KIT",
        "=" * 68,
        "",
        f"Generated (UTC): {utc_now_iso()}",
        "",
        "Secret key (Base32):",
        "",
        f"    {b32}",
        "",
        "Master passphrase (write it here by hand; it is never stored):",
        "",
        "    " + "_" * 48,
        "",
        "QR code (encodes the Base32 secret key above):",
        "",
        _qr_text(b32),
        "Keep this kit in a safe or safety-deposit box. Anyone holding it",
        "has your secret key; with your passphrase it opens your entire",
        "health history. To set up a new machine: healthspan init --restore",
        "(arrives with backup/restore) with this kit at hand.",
        "=" * 68,
    ]
    return "\n".join(lines)


def default_kit_filename() -> str:
    return KIT_FILENAME_TEMPLATE.format(date=utc_now_iso()[:10])


def write_kit(secret_key: bytes, output: Path) -> Path:
    """Write a deliberate digital copy (ADR-0033 ``--output`` pathway)."""
    if output.is_dir():
        output = output / default_kit_filename()
    output.write_text(render_kit(secret_key), encoding="utf-8")
    set_owner_only(output)
    return output


def _qr_text(data: str) -> str:
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()
