"""Core Service runtime state, shared between the app and its endpoints.

Lives in its own module so :mod:`healthspan.api_health`,
:mod:`healthspan.api_security`, and :mod:`healthspan.service` can all reach
:class:`ServiceRuntime` without an import cycle. It holds what the process
owns for its lifetime: the derived database key (INV-1 sole key-holder,
ADR-0028), the single-instance lock (ADR-0042), the thread-affine
connection pool built on that key (ADR-0037), the schema version verified
at startup (ADR-0039), and the cached readiness flag the liveness endpoint
reads without touching the database (ADR-0037/0040).
"""

import time
from dataclasses import dataclass, field

from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock
from healthspan.pool import ConnectionPool


@dataclass
class ServiceRuntime:
    cfg: Config
    key: DbKey
    lock: InstanceLock
    pool: ConnectionPool
    schema_version: int
    ready: bool = False
    # Reset when the lifespan flips `ready` on, so uptime measures serving
    # time (observability.md `uptime_seconds`).
    started_monotonic: float = field(default_factory=time.monotonic)

    def uptime_seconds(self) -> int:
        return max(0, int(time.monotonic() - self.started_monotonic))
