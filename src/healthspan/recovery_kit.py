"""Recovery Kit rendering (ADR-0013, ADR-0028, ADR-0033).

Renders in memory to text: the secret key in grouped Base32, a QR code
encoding the same string (Unicode half-block cells — scannable from a
screen or a monospace printout), and the custody instructions. OS print
pathways (``lp``/``lpr``, Windows shell print) are a later work item;
until then the kit is displayed and, only by explicit ``--output``
choice, written to a warned-about file.
"""

import contextlib
import io
import os
from pathlib import Path

import qrcode

from healthspan.fsperm import set_owner_only
from healthspan.kdf import encode_secret_key
from healthspan.keyparams import utc_now_iso

# ADR-0033: recognizable naming, matched by the repo .gitignore pattern.
KIT_FILENAME_TEMPLATE = "healthspan-recovery-kit-{date}.txt"
# The same pattern the orphan sweep and .gitignore recognize.
KIT_FILENAME_GLOB = "*recovery-kit*"

OUTPUT_WARNING = (
    "This file contains the secret key. Store it only on encrypted storage "
    "(a password manager attachment or an encrypted volume). A digital kit "
    "lingering on unencrypted or synced storage collapses the two-factor "
    "model toward passphrase-only strength (ADR-0033)."
)


def render_kit(secret_key: bytes, *, include_qr: bool = True) -> str:
    """Render the full Recovery Kit as text (in memory, ADR-0033).

    ``include_qr=False`` substitutes a note for the QR block — used when
    the output stream's encoding cannot carry the Unicode half-block
    cells (e.g. stdout redirected under a legacy Windows code page).
    """
    b32 = encode_secret_key(secret_key)
    if include_qr:
        qr_lines = [
            "QR code (encodes the Base32 secret key above):",
            "",
            _qr_text(b32),
        ]
    else:
        qr_lines = [
            "[QR code omitted: this output stream cannot render it. Run",
            "'healthspan keys recovery-kit' in a terminal to scan it.]",
            "",
        ]
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
        *qr_lines,
        "Keep this kit in a safe or safety-deposit box. Anyone holding it",
        "has your secret key; with your passphrase it opens your entire",
        "health history. To set up a new machine: healthspan init --restore",
        "with this kit at hand, then healthspan db restore.",
        "=" * 68,
    ]
    return "\n".join(lines)


def default_kit_filename() -> str:
    return KIT_FILENAME_TEMPLATE.format(date=utc_now_iso()[:10])


def write_kit(secret_key: bytes, output: Path) -> Path:
    """Write a deliberate digital copy (ADR-0033 ``--output`` pathway)."""
    if output.is_dir():
        output = output / default_kit_filename()
    # Create owner-only from the first byte: this is the one file that holds
    # the secret key in plaintext, so it must never exist even briefly under
    # the umask's default (world/group-readable) mode. POSIX honors the 0o600
    # at open(); Windows ignores it, so set_owner_only applies its ACLs
    # immediately after the content is written.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(render_kit(secret_key))
    set_owner_only(output)
    return output


def sweep_orphans(private_dir: Path) -> list[Path]:
    """Dispose orphaned Recovery Kit plaintext at startup (ADR-0033).

    A crash between a kit render and its disposal — the Windows shell-print
    pathway is the only producer, still deferred (ADR-0047) — could leave a
    plaintext kit in the platform's private data directory. The Core Service
    sweeps it on startup so an interrupted render is caught at the next
    start. Disposal is best-effort (overwrite-then-unlink): honest about
    what modern storage can and cannot erase (ADR-0033's disposal policy).

    Scans only ``private_dir`` (non-recursive) for the recognizable kit
    naming; a deliberate ``--output`` copy the user placed elsewhere is
    their custody and is untouched. Returns the paths disposed of.
    """
    disposed: list[Path] = []
    try:
        candidates = sorted(private_dir.glob(KIT_FILENAME_GLOB))
    except OSError:
        return disposed
    for path in candidates:
        if not path.is_file():
            continue
        _best_effort_dispose(path)
        disposed.append(path)
    return disposed


def _best_effort_dispose(path: Path) -> None:
    """Overwrite with zeroes, then unlink (ADR-0033 best-effort disposal).

    Best-effort by the honest standard: SSD wear leveling, copy-on-write
    and journaling filesystems, snapshots, and sync history all defeat it.
    Retained because it still raises the bar where it works, and it is
    nearly free. Never raises — a sweep failure must not block startup.
    """
    try:
        size = path.stat().st_size
        with path.open("r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _qr_text(data: str) -> str:
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue()
