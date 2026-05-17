"""Simple run resource lease tracking for experiment execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional


@dataclass
class ResourceLease:
    run_id: str
    resource: str


class ResourceManager:
    def __init__(self) -> None:
        self._leases: Dict[str, ResourceLease] = {}

    def acquire_for_run(self, run_id: str, resources: Iterable[str]) -> None:
        blocked = [resource for resource in resources if resource in self._leases and self._leases[resource].run_id != run_id]
        if blocked:
            raise RuntimeError(f"Resources already leased: {', '.join(blocked)}")
        for resource in resources:
            self._leases[resource] = ResourceLease(run_id=run_id, resource=resource)

    def release(self, run_id: str) -> None:
        for resource, lease in list(self._leases.items()):
            if lease.run_id == run_id:
                self._leases.pop(resource, None)

    def is_leased(self, resource: str) -> bool:
        return resource in self._leases

    def owner(self, resource: str) -> Optional[str]:
        lease = self._leases.get(resource)
        return lease.run_id if lease else None
