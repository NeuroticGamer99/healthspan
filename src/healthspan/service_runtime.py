"""Core Service runtime state, shared between the app and its endpoints.

Lives in its own module so :mod:`healthspan.api_health` and
:mod:`healthspan.service` can both reach :class:`ServiceRuntime` without an
import cycle. It holds what the process owns for its lifetime: the derived
database key (INV-1 sole key-holder, ADR-0028), the single-instance lock
(ADR-0042), and the cached readiness flag the liveness endpoint reads
without touching the database (ADR-0037/0040).
"""

from dataclasses import dataclass

from healthspan.config import Config
from healthspan.kdf import DbKey
from healthspan.locking import InstanceLock


@dataclass
class ServiceRuntime:
    cfg: Config
    key: DbKey
    lock: InstanceLock
    ready: bool = False
