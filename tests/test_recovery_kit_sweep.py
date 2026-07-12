"""Recovery Kit orphan-plaintext startup sweep (ADR-0033, ADR-0049).

The sweep disposes only transient *spool* files the (deferred) print
pathway produces — never a deliberate ``--output`` kit, even one saved into
the swept directory (the data-loss footgun this suite guards against).
"""

from fnmatch import fnmatch
from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from healthspan.recovery_kit import (
    KIT_FILENAME_TEMPLATE,
    ORPHAN_SPOOL_GLOB,
    default_kit_filename,
    orphan_spool_filename,
    sweep_orphans,
)

# uuid/pid-shaped tokens: no path separator (the orphan_spool_filename contract).
_SPOOL_TOKENS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=40
)


def test_sweep_disposes_orphan_spool(tmp_path: Path) -> None:
    orphan = tmp_path / orphan_spool_filename("pid1234")
    orphan.write_text("orphaned kit plaintext", encoding="utf-8")
    keep = tmp_path / "healthspan.db"
    keep.write_text("ciphertext", encoding="utf-8")

    disposed = sweep_orphans(tmp_path)

    assert disposed == [orphan]
    assert not orphan.exists()
    assert keep.exists()  # non-spool files are untouched


def test_sweep_never_touches_a_deliberate_kit_in_the_swept_dir(
    tmp_path: Path,
) -> None:
    # The finding this fix closes: a user who saves their Recovery Kit into
    # the data directory (a natural reading of "encrypted storage") must not
    # have their only offline key copy silently disposed on service start.
    deliberate = tmp_path / default_kit_filename()
    deliberate.write_text("the user's only offline key copy", encoding="utf-8")

    disposed = sweep_orphans(tmp_path)

    assert disposed == []
    assert deliberate.exists()
    assert deliberate.read_text(encoding="utf-8") == "the user's only offline key copy"


# The data-loss invariant as standing properties (not just today's date):
# the deliberate-kit and spool namespaces are disjoint over their whole
# input spaces, so no future edit to either template can silently re-collide
# them the way the original broad glob did.


@given(date=st.dates().map(lambda d: d.isoformat()))
def test_no_deliberate_kit_name_is_ever_a_sweep_target(date: str) -> None:
    name = KIT_FILENAME_TEMPLATE.format(date=date)
    assert not fnmatch(name, ORPHAN_SPOOL_GLOB)


@given(token=_SPOOL_TOKENS)
def test_every_spool_name_is_a_sweep_target(token: str) -> None:
    assert fnmatch(orphan_spool_filename(token), ORPHAN_SPOOL_GLOB)


def test_sweep_returns_empty_for_clean_dir(tmp_path: Path) -> None:
    assert sweep_orphans(tmp_path) == []


def test_sweep_tolerates_missing_dir(tmp_path: Path) -> None:
    assert sweep_orphans(tmp_path / "does-not-exist") == []


def test_sweep_disposes_multiple_spool_orphans(tmp_path: Path) -> None:
    first = tmp_path / orphan_spool_filename("a")
    second = tmp_path / orphan_spool_filename("b")
    first.write_text("x", encoding="utf-8")
    second.write_text("y", encoding="utf-8")

    disposed = sweep_orphans(tmp_path)

    assert set(disposed) == {first, second}
    assert not first.exists()
    assert not second.exists()
