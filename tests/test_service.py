"""Core Service startup, passphrase channels, and liveness (ADR-0037/0039/0040)."""

import dataclasses
import io
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlcipher3
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import healthspan.rotation as rotation
import healthspan.service as service_mod
from healthspan import db, keychain, migrate, token_bootstrap
from healthspan.api_health import LIVENESS_PATH
from healthspan.api_security import LivenessRateLimiter, assert_all_routes_declared
from healthspan.cli import app as cli_app
from healthspan.config import Config, load_config
from healthspan.fsperm import owner_only_exposure, set_owner_only
from healthspan.kdf import DbKey
from healthspan.keyparams import sidecar_path
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool, PoolClosedError
from healthspan.service import (
    ServiceStartupError,
    bootstrap_tokens,
    build_runtime,
    create_app,
    resolve_passphrase,
    start_service,
    verify_schema,
)
from healthspan.service_runtime import ServiceRuntime

runner = CliRunner()
PASSPHRASE = "a perfectly reasonable passphrase"


def _init(tmp_path: Path, *, migrate: bool) -> Path:
    """Init (and optionally migrate) a database; return its config path."""
    config = tmp_path / "config.toml"
    config.write_text(
        'config_version = 1\n\n[database]\npath = "hs.db"\n', encoding="utf-8"
    )
    assert (
        runner.invoke(
            cli_app,
            ["--config", str(config), "init"],
            input=f"{PASSPHRASE}\n{PASSPHRASE}\n",
        ).exit_code
        == 0
    )
    if migrate:
        assert (
            runner.invoke(
                cli_app,
                ["--config", str(config), "db", "migrate"],
                input=f"{PASSPHRASE}\n",
            ).exit_code
            == 0
        )
    return config


def _passphrase_file(
    tmp_path: Path, name: str = "pp.secret", content: str = PASSPHRASE
) -> Path:
    """A passphrase file as a real deployment must present one: owner-only.

    ADR-0066 refuses to read this channel when it is readable beyond its
    owner, so a fixture that skipped the protection would be testing an
    unstartable deployment. Every passphrase file in this module goes through
    here — including the deliberately *wrong* ones, which still have to be
    readable to fail for the reason the test is about. A file written at the
    default umask is 0644 on POSIX and refused before the test's own
    assertion is ever reached; on Windows the same file carries only benign
    principals and passes, so the mistake is invisible until CI's POSIX legs.
    """
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    set_owner_only(path)
    return path


@pytest.fixture
def empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate the systemd/Docker path: no TTY, empty stdin (EOF).

    ``build_runtime`` reads ``sys.stdin`` internally per ADR-0039's channel
    order; pytest's captured stdin raises on ``readline``, so replace it with
    an empty stream that falls through to the ``passphrase_file`` channel.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _get(client: Any, path: str) -> httpx.Response:
    # Starlette's TestClient inherits httpx.Client.get, whose signature pyright
    # reads as partially unknown; taking the client as Any keeps the call clean
    # while the returned response stays fully typed for every assertion.
    response: httpx.Response = client.get(path)
    return response


# --------------------------------------------------------------------------
# Passphrase channels (ADR-0039)
# --------------------------------------------------------------------------


def test_tty_prompt_takes_precedence_over_flag(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    # ADR-0039 order: an interactive TTY prompts even if --passphrase-file is
    # set; the file tier is reached only when stdin is neither TTY nor piped.
    pp = _passphrase_file(tmp_path, "flag.secret", "from-the-flag\n")
    got = resolve_passphrase(make_config(), pp, stdin=_Tty(), prompt=lambda: "typed")
    assert got == "typed"


def test_passphrase_file_flag_channel(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    # The systemd/Docker path: no TTY, empty stdin -> the --passphrase-file
    # flag is read (and overrides the config key within the file tier).
    pp = _passphrase_file(tmp_path, "flag.secret")
    cfg = make_config()
    cfg = dataclasses.replace(
        cfg,
        service=dataclasses.replace(cfg.service, passphrase_file=tmp_path / "other"),
    )
    assert resolve_passphrase(cfg, pp, stdin=io.StringIO("")) == PASSPHRASE


def test_tty_prompt_channel(make_config: Callable[[], Config]) -> None:
    got = resolve_passphrase(make_config(), None, stdin=_Tty(), prompt=lambda: "typed")
    assert got == "typed"


def test_stdin_pipe_channel(make_config: Callable[[], Config]) -> None:
    assert (
        resolve_passphrase(make_config(), None, stdin=io.StringIO("piped\n")) == "piped"
    )


def test_config_passphrase_file_channel(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    pp = _passphrase_file(tmp_path)
    cfg = make_config()
    cfg = dataclasses.replace(
        cfg, service=dataclasses.replace(cfg.service, passphrase_file=pp)
    )
    # stdin is empty (EOF), so the configured file is the fallback channel.
    assert resolve_passphrase(cfg, None, stdin=io.StringIO("")) == PASSPHRASE


@pytest.mark.parametrize(
    "suffix",
    ["", "\n", "\r\n", "\nunrelated second line\n"],
    ids=["bare", "echo-lf", "windows-crlf", "extra-lines"],
)
def test_passphrase_file_reads_exactly_the_first_line(
    make_config: Callable[[], Config], tmp_path: Path, suffix: str
) -> None:
    """A terminator must never reach key derivation.

    A file written by ``echo`` carries a trailing ``\\n`` and one saved by a
    Windows editor carries ``\\r\\n``; feeding either into the KDF derives a
    different key, so the database silently fails to open with no hint that
    an invisible byte is the cause.
    """
    path = tmp_path / "pp.secret"
    path.write_text(f"{PASSPHRASE}{suffix}", encoding="utf-8")
    set_owner_only(path)
    assert resolve_passphrase(make_config(), path, stdin=io.StringIO("")) == PASSPHRASE


def test_no_channel_available_is_an_error(make_config: Callable[[], Config]) -> None:
    with pytest.raises(ServiceStartupError, match="no passphrase channel"):
        resolve_passphrase(make_config(), None, stdin=io.StringIO(""))


def test_no_channel_message_forbids_env_var(
    make_config: Callable[[], Config],
) -> None:
    with pytest.raises(ServiceStartupError, match="never read from an environment"):
        resolve_passphrase(make_config(), None, stdin=io.StringIO(""))


# --------------------------------------------------------------------------
# Passphrase-file permission refusal (ADR-0066 decision 2)
# --------------------------------------------------------------------------


def _expose(path: Path) -> Path:
    """Make a file readable beyond its owner, on either platform."""
    if os.name == "posix":
        path.chmod(0o644)
    else:
        subprocess.run(  # noqa: S603 - fixed executable, no shell
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],  # noqa: S607
            capture_output=True,
            encoding="oem",
            errors="replace",
            check=True,
        )
    return path


def test_exposed_config_passphrase_file_refuses_startup(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    pp = _expose(_passphrase_file(tmp_path))
    cfg = make_config()
    cfg = dataclasses.replace(
        cfg, service=dataclasses.replace(cfg.service, passphrase_file=pp)
    )
    with pytest.raises(ServiceStartupError) as excinfo:
        resolve_passphrase(cfg, None, stdin=io.StringIO(""))
    message = str(excinfo.value)
    assert "accessible beyond its owner" in message
    # A disclosed secret is not fixed by a mode bit; the message must say so.
    assert "rotate" in message


def test_exposed_passphrase_file_flag_refuses_startup(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    # The check lives at the read, so it covers the flag tier identically —
    # the flag is not a way around the config key's protection.
    pp = _expose(_passphrase_file(tmp_path, "flag.secret"))
    with pytest.raises(ServiceStartupError, match="accessible beyond its owner"):
        resolve_passphrase(make_config(), pp, stdin=io.StringIO(""))


def test_refused_passphrase_file_is_never_repaired(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    pp = _expose(_passphrase_file(tmp_path))
    with pytest.raises(ServiceStartupError):
        resolve_passphrase(make_config(), pp, stdin=io.StringIO(""))
    # Repairing would leave the deployment startable while the passphrase
    # stayed disclosed — the exposure must survive to force the decision.
    assert owner_only_exposure(pp) is not None


def test_unread_passphrase_file_is_not_checked(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    # ADR-0039's channel order still wins: a TTY prompt never reaches the
    # file tier, so an exposed file that is never read cannot block startup.
    pp = _expose(_passphrase_file(tmp_path, "flag.secret"))
    got = resolve_passphrase(make_config(), pp, stdin=_Tty(), prompt=lambda: "typed")
    assert got == "typed"


def test_build_runtime_repairs_every_swept_surface(
    tmp_path: Path, empty_stdin: None
) -> None:
    """The tar-arrival case, across all four surfaces the sweep covers.

    Each is exposed through the real ``build_runtime`` wiring rather than a
    direct ``verify_startup_files`` call, so a dropped or swapped argument —
    the config path or the backup directory going unchecked — fails here.
    """
    config = _init(tmp_path, migrate=True)
    cfg = load_config(flag=config)
    cfg.backup.directory.mkdir(parents=True, exist_ok=True)
    surfaces = [
        config,
        cfg.database.path,
        sidecar_path(cfg.database.path),
        cfg.backup.directory,
    ]
    for surface in surfaces:
        _expose(surface)
        assert owner_only_exposure(surface) is not None  # the setup took hold

    runtime = build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    try:
        for surface in surfaces:
            assert owner_only_exposure(surface) is None, surface
    finally:
        runtime.pool.close_all()
        runtime.lock.release()
        runtime.key.zeroize()


def test_startup_survives_a_repair_it_cannot_apply(
    tmp_path: Path, empty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair the platform cannot apply must not become a lockout.

    The whole point of the repair posture is that it never stands between
    the owner and their own database, so the service still starts and the
    finding is reported instead.
    """
    from healthspan import permcheck
    from healthspan.fsperm import PermissionSetError

    cfg = load_config(flag=_init(tmp_path, migrate=True))
    pp = _passphrase_file(tmp_path)
    _expose(cfg.database.path)

    def _deny(_path: Path) -> None:
        raise PermissionSetError("simulated read-only mount")

    monkeypatch.setattr(permcheck, "set_owner_only", _deny)
    runtime = build_runtime(cfg, passphrase_file_flag=pp)
    try:
        assert runtime.lock.held  # started anyway
        assert owner_only_exposure(cfg.database.path) is not None  # still broad
    finally:
        runtime.pool.close_all()
        runtime.lock.release()
        runtime.key.zeroize()


# --------------------------------------------------------------------------
# build_runtime: lock, unlock, schema check (ADR-0039/0042)
# --------------------------------------------------------------------------


def test_build_runtime_succeeds_and_retains_key(
    tmp_path: Path, empty_stdin: None
) -> None:
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    runtime = build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    try:
        assert runtime.lock.held
        assert len(runtime.key.hex()) == 64  # key retained, not zeroized
    finally:
        runtime.lock.release()
        runtime.key.zeroize()


def test_build_runtime_refuses_pending_migration(
    tmp_path: Path, empty_stdin: None
) -> None:
    cfg = load_config(flag=_init(tmp_path, migrate=False))
    with pytest.raises(ServiceStartupError, match="db migrate"):
        build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    # The lock was released on failure — a fresh acquire succeeds.
    reclaim = InstanceLock(cfg.database.path)
    reclaim.acquire()
    reclaim.release()


def test_build_runtime_refuses_when_database_already_held(tmp_path: Path) -> None:
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    holder = InstanceLock(cfg.database.path)
    holder.acquire()
    try:
        with pytest.raises(ServiceStartupError, match="holds the database lock"):
            build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    finally:
        holder.release()


def test_build_runtime_wrong_passphrase_releases_lock(
    tmp_path: Path, empty_stdin: None
) -> None:
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    bad = _passphrase_file(tmp_path, "bad.secret", "not the right passphrase at all")
    with pytest.raises(rotation.RotationError):
        build_runtime(cfg, passphrase_file_flag=bad)
    reclaim = InstanceLock(cfg.database.path)
    reclaim.acquire()  # lock was released despite the failure
    reclaim.release()


def test_bootstrap_tokens_mints_on_first_start_and_prints_mcp_secret_to_stderr(
    tmp_path: Path, empty_stdin: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # The ADR-0050 §1 hook: first `service start` finds an empty tokens
    # table and mints; the second start finds it populated and does not.
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    runtime = build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    try:
        assert bootstrap_tokens(runtime) is True
        err = capsys.readouterr().err
        assert "hsp_mcpclient_" in err  # the console channel, not stdout logs
        assert bootstrap_tokens(runtime) is False
    finally:
        runtime.pool.close_all()
        runtime.lock.release()
        runtime.key.zeroize()


def test_start_service_bootstrap_failure_releases_everything(
    tmp_path: Path, empty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0051 §7: a bootstrap failure aborts startup with nothing held —
    # lock released (the next start can retry), key zeroized, pool closed,
    # and uvicorn never reached.
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    captured: list[ServiceRuntime] = []
    real_build = service_mod.build_runtime

    def capturing_build(
        cfg: Config, passphrase_file_flag: Path | None = None
    ) -> ServiceRuntime:
        runtime = real_build(cfg, passphrase_file_flag)
        captured.append(runtime)
        return runtime

    def broken_store(name: str, token: str) -> None:
        raise keychain.KeychainError("keyring backend unavailable")

    def refuse_to_serve(app: FastAPI, cfg: Config) -> None:
        pytest.fail("must not serve after a bootstrap failure")

    monkeypatch.setattr(service_mod, "build_runtime", capturing_build)
    monkeypatch.setattr(keychain, "store_token_plaintext", broken_store)
    monkeypatch.setattr(service_mod, "_run_uvicorn", refuse_to_serve)

    with pytest.raises(ServiceStartupError, match="default token set"):
        start_service(cfg, _passphrase_file(tmp_path))

    (runtime,) = captured
    with pytest.raises(RuntimeError, match="zeroized"):
        runtime.key.hex()
    with pytest.raises(PoolClosedError):
        runtime.pool.connection()
    reclaim = InstanceLock(cfg.database.path)
    reclaim.acquire()  # the lock was released despite the failure
    reclaim.release()


def test_bootstrap_database_failure_becomes_a_startup_error(
    tmp_path: Path, empty_stdin: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Driver-level failures (locked past busy_timeout, disk I/O error) must
    # reach the operator through the same ServiceStartupError channel as
    # every other startup refusal, never as a raw sqlcipher3 traceback.
    cfg = load_config(flag=_init(tmp_path, migrate=True))

    def broken_bootstrap(conn: object, console: object) -> bool:
        raise sqlcipher3.OperationalError("disk I/O error")

    monkeypatch.setattr(token_bootstrap, "bootstrap_default_tokens", broken_bootstrap)
    runtime = build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    try:
        with pytest.raises(ServiceStartupError, match="default token set"):
            bootstrap_tokens(runtime)
    finally:
        runtime.pool.close_all()
        runtime.lock.release()
        runtime.key.zeroize()


def test_build_runtime_leaves_no_passphrase_in_environment(
    tmp_path: Path, empty_stdin: None
) -> None:
    # ADR-0039: the passphrase never reaches the environment. (The full
    # spawned-process argv/environ inspection per testing-strategy.md line 92
    # is E2E-tier — deferred with the process-spawning harness; there is no
    # --passphrase value flag, so argv carries no passphrase material.)
    cfg = load_config(flag=_init(tmp_path, migrate=True))
    runtime = build_runtime(cfg, passphrase_file_flag=_passphrase_file(tmp_path))
    try:
        assert not any(PASSPHRASE in value for value in os.environ.values())
    finally:
        runtime.lock.release()
        runtime.key.zeroize()


# --------------------------------------------------------------------------
# Reserved category row assertion (ADR-0055 §2)
# --------------------------------------------------------------------------


def test_verify_schema_refuses_a_database_missing_the_reserved_category(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    key = DbKey(bytearray(range(1, 33)))
    db.provision(cfg.database.path, key)
    migrate.migrate_database(cfg.database.path, key)
    # Simulate a corrupted database where the reserved not_assigned row (id 0)
    # is gone: the write-path delete-guard trigger (migration 0004) forbids
    # this through any normal SQL path, so the trigger is dropped first to
    # reach the state verify_schema must still catch defensively.
    conn = db.connect(cfg.database.path, key)
    try:
        conn.execute("DROP TRIGGER categories_reserved_no_delete")
        conn.execute("DELETE FROM categories WHERE id = 0")
    finally:
        db.close(conn)
    with pytest.raises(ServiceStartupError, match="not_assigned"):
        verify_schema(cfg, key)


def test_verify_schema_succeeds_when_the_reserved_category_is_present(
    make_config: Callable[[], Config],
) -> None:
    cfg = make_config()
    key = DbKey(bytearray(range(1, 33)))
    db.provision(cfg.database.path, key)
    migrate.migrate_database(cfg.database.path, key)
    assert verify_schema(cfg, key) == migrate.target_version()


# --------------------------------------------------------------------------
# Liveness endpoint (ADR-0037/0040)
# --------------------------------------------------------------------------


@pytest.fixture
def live(
    make_config: Callable[[], Config],
) -> Iterator[tuple[TestClient, ServiceRuntime, FastAPI]]:
    cfg = make_config()
    lock = InstanceLock(cfg.database.path)
    lock.acquire()
    key = DbKey(bytearray(os.urandom(32)))
    # The pool is lazy and the key random: no route under test may touch the
    # database (liveness reads only the cached flag, ADR-0037/0040).
    runtime = ServiceRuntime(
        cfg=cfg,
        key=key,
        lock=lock,
        pool=ConnectionPool(cfg.database.path, key),
        schema_version=0,
    )
    application = create_app(runtime)
    with TestClient(application) as client:
        yield client, runtime, application


def test_liveness_ready_is_status_word_only(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    client, _runtime, _app = live
    response = _get(client, LIVENESS_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert list(response.json().keys()) == ["status"]  # no version/schema/uptime
    assert response.headers.get("x-request-id")


def test_liveness_reports_unavailable_when_not_ready(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    client, runtime, _app = live
    runtime.ready = False
    response = _get(client, LIVENESS_PATH)
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_liveness_answers_without_touching_the_database(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    # The runtime key is random bytes; any real database query would fail.
    # A 200 proves liveness reads only the cached flag (ADR-0037).
    client, _runtime, _app = live
    assert _get(client, LIVENESS_PATH).status_code == 200


def test_exactly_one_public_route_is_liveness(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    _client, _runtime, application = live
    assert assert_all_routes_declared(application) == [LIVENESS_PATH]


def test_liveness_rate_limited_returns_429(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    client, _runtime, application = live
    application.state.liveness_limiter = LivenessRateLimiter(max_requests=1)
    assert _get(client, LIVENESS_PATH).status_code == 200
    limited = _get(client, LIVENESS_PATH)
    assert limited.status_code == 429
    assert limited.json() == {"status": "unavailable"}


def test_docs_and_openapi_are_disabled(
    live: tuple[TestClient, ServiceRuntime, FastAPI],
) -> None:
    # No unauthenticated API-surface disclosure in Phase 2 (ADR-0049 §7); the
    # docs/OpenAPI routes must stay off so liveness is the only reachable path.
    client, _runtime, _app = live
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert _get(client, path).status_code == 404
