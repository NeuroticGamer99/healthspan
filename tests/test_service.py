"""Core Service startup, passphrase channels, and liveness (ADR-0037/0039/0040)."""

import dataclasses
import io
import os
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
from healthspan import keychain, token_bootstrap
from healthspan.api_health import LIVENESS_PATH
from healthspan.api_security import LivenessRateLimiter, assert_all_routes_declared
from healthspan.cli import app as cli_app
from healthspan.config import Config, load_config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool, PoolClosedError
from healthspan.service import (
    ServiceStartupError,
    bootstrap_tokens,
    build_runtime,
    create_app,
    resolve_passphrase,
    start_service,
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


def _passphrase_file(tmp_path: Path) -> Path:
    path = tmp_path / "pp.secret"
    path.write_text(PASSPHRASE, encoding="utf-8")
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
    pp = tmp_path / "flag.secret"
    pp.write_text("from-the-flag\n", encoding="utf-8")
    got = resolve_passphrase(make_config(), pp, stdin=_Tty(), prompt=lambda: "typed")
    assert got == "typed"


def test_passphrase_file_flag_channel(
    make_config: Callable[[], Config], tmp_path: Path
) -> None:
    # The systemd/Docker path: no TTY, empty stdin -> the --passphrase-file
    # flag is read (and overrides the config key within the file tier).
    pp = tmp_path / "flag.secret"
    pp.write_text(f"{PASSPHRASE}\n", encoding="utf-8")
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


def test_no_channel_available_is_an_error(make_config: Callable[[], Config]) -> None:
    with pytest.raises(ServiceStartupError, match="no passphrase channel"):
        resolve_passphrase(make_config(), None, stdin=io.StringIO(""))


def test_no_channel_message_forbids_env_var(
    make_config: Callable[[], Config],
) -> None:
    with pytest.raises(ServiceStartupError, match="never read from an environment"):
        resolve_passphrase(make_config(), None, stdin=io.StringIO(""))


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
    bad = tmp_path / "bad.secret"
    bad.write_text("not the right passphrase at all", encoding="utf-8")
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
