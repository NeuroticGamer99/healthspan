"""The shared CLI REST client (ADR-0006)."""

from collections.abc import Callable

from healthspan.cli_client import default_client
from healthspan.config import Config


def test_default_client_ignores_environment_proxies(
    make_config: Callable[[], Config],
) -> None:
    # trust_env=False so an inherited HTTP_PROXY cannot route the loopback,
    # bearer-token-bearing request through an external proxy (CodeRabbit PR #31).
    client = default_client(make_config())
    try:
        assert client.trust_env is False
    finally:
        client.close()
