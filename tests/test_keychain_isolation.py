"""The test suite's own keychain containment (ADR-0068 §3).

Both barriers between pytest and the machine's real credential store live in
`tests/conftest.py`, on a machine that holds a real encrypted health database.
The autouse `fake_keychain` fixture is exercised implicitly by every test in the
suite. The import-time `PYTHON_KEYRING_BACKEND` assignment is not, and cannot
be: by the time any test body runs, that assignment has already happened, so an
in-process test can only observe its own conftest's handiwork. Proving it takes
a fresh interpreter started with a hostile value already in the environment,
which is what the first two here do — one for the value, one for the effect.

A third asserts the assignment's *position*: `keyring.core.init_backend()` runs
lazily on the first `get_keyring()`, so the belt only wins while nothing
imported above it resolves a backend at import time. The other two assert the
final value and effect and would stay green with the assignment moved anywhere
in the file, which is exactly the property that needs its own probe.

This file is out of bounds for mutation testing (`test-reviewer.md`
§ Mutation testing), so nothing else will notice if this docstring and the
tests below stop agreeing — it says three because there are three.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)

_BACKEND_VAR = "PYTHON_KEYRING_BACKEND"

# The shape of the accident the unconditional assignment exists to defeat —
# another tool's setting, or a line left in an old shell profile, inherited by
# the suite — but naming a backend that **cannot resolve to anything**.
#
# This must never name a real one. These probes are the single place in the
# suite that deliberately overrides the mutation-testing reviewer's own
# out-of-tree `PYTHON_KEYRING_BACKEND` export, because overriding it is the
# behavior under test. That strips the outer belt for the child process, so if
# the inner belt were also broken — a mutation, a bad merge dropping the line
# in conftest — a real backend here would resolve and `get_password` below
# would read this machine's live credential store before any assertion ran.
# On Windows that is the OS Credential Vault, on the very machine ADR-0068 §3
# was written for. An unresolvable module cannot do that on any platform: the
# import fails, the test goes red, and nothing is touched.
#
# GENERATED rather than a literal, and that is the safety property rather than
# a flourish. As `keyring.backends.no_such_module_exists.Keyring` it was one
# token away from `keyring.backends.Windows.Keyring`, and the whole file's
# safety rested on nobody making that edit — a rule stated only in
# `test-reviewer.md`'s mutation carve-out, which is prose enforced by nothing
# and which the natural mutation ("does the belt actually decide the backend?")
# aims at directly. A random component leaves no plausible single-token edit:
# there is no real backend this can be turned into by changing one name.
_HOSTILE = f"keyring.backends.{secrets.token_hex(8)}_absent.Keyring"

# A curated child environment rather than `dict(os.environ)`. These probes
# exist to prove what decides the backend, so inheriting the whole environment
# hands part of that decision to whatever else the host happens to set —
# keyring reads more than this one variable, and its POSIX backends consult
# `XDG_*` and the session bus. Only what a fresh interpreter needs to start
# and import survives; anything absent on a leg is simply not passed.
_ENV_KEEP = (
    "SYSTEMROOT",  # Windows: sockets and the CRT fail to initialize without it
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "PYTHONUTF8",  # set by ci.yml; the probes print ASCII but imports may not
)

_VALUE_PROBE = "\n".join(
    (
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "import conftest",  # the import under test
        "import os",
        "print(os.environ['PYTHON_KEYRING_BACKEND'])",
    )
)

_EFFECT_PROBE = "\n".join(
    (
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "import conftest",  # the import under test
        "import keyring",
        "import keyring.errors",
        "try:",
        "    keyring.get_password('healthspan-probe', 'nobody')",
        "except keyring.errors.NoKeyringError:",
        "    print('NoKeyringError')",
    )
)


def _probe(source: str) -> subprocess.CompletedProcess[str]:
    """Run `source` in a fresh interpreter that already has the hostile value."""
    env = {name: os.environ[name] for name in _ENV_KEEP if name in os.environ}
    env[_BACKEND_VAR] = _HOSTILE
    return subprocess.run(  # noqa: S603 - this interpreter, no shell
        [sys.executable, "-c", source, _TESTS_DIR],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )


def test_the_keyring_belt_overrides_an_inherited_real_backend() -> None:
    """`tests/conftest.py` assigns rather than `setdefault`s, so a value
    inherited from the environment cannot silently win.

    keyring's `load_env()` consults this variable *first* at backend-resolution
    time, so a hostile value that survives conftest's import is a value that
    would decide which backend the suite talks to.
    """
    proc = _probe(_VALUE_PROBE)
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
    assert proc.stdout.strip() == "keyring.backends.fail.Keyring"


def test_the_belt_makes_real_backend_resolution_raise() -> None:
    """The value is not merely set, it does the job end to end: a code path
    that reaches real backend resolution — a test escaping the autouse fixture,
    or a subprocess a test spawns — raises `NoKeyringError` instead of reading
    the OS keychain. Run with the hostile value pre-set, so this covers the
    override and its consequence in one shot.
    """
    proc = _probe(_EFFECT_PROBE)
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
    assert proc.stdout.strip() == "NoKeyringError"


_ORDER_PROBE = "\n".join(
    (
        "import sys",
        "sys.path.insert(0, sys.argv[1])",
        "import conftest",  # the import under test
        "import keyring.core",
        # None means nothing resolved a backend while conftest was importing,
        # so the belt cannot have been assigned too late to matter.
        "print(keyring.core._keyring_backend is None)",
    )
)


def test_the_belt_is_assigned_before_any_backend_is_resolved() -> None:
    """The belt's guarantee is positional, and nothing else pins the position.

    `keyring.core.init_backend()` runs lazily on the first `get_keyring()`, so
    the assignment in `conftest.py` only decides the backend while nothing
    imported *above* it has resolved one already. It is not the first
    statement in the file — `import keyring` and `from healthspan.config
    import ...` both precede it — so today it holds by ordering rather than by
    construction, and the two probes above cannot see that: both assert the
    final value and the final effect, so they stay green with the assignment
    moved anywhere in the file.

    This asserts the property that actually matters, which is stronger than
    the line number: *nothing resolves a backend during conftest's import at
    all*. While that holds the position is irrelevant, and the day someone
    adds an import above the assignment that touches a credential at import
    time, this goes red — on the machine that holds a real encrypted health
    database, which is the whole reason ADR-0068 §3 exists.

    Deliberately no line number here. This docstring carried "above line 41"
    while the assignment sat at `conftest.py:62`, which is what a cited line
    in a file nothing re-derives becomes; the property above needs no
    coordinate to be checkable.
    """
    proc = _probe(_ORDER_PROBE)
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"
    assert proc.stdout.strip() == "True", (
        "something imported by conftest.py resolved a keyring backend during "
        "import; the belt's position is now load-bearing and it is not first"
    )
