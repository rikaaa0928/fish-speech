from __future__ import annotations

from enum import Enum


class Priority(Enum):
    V1 = "v1"
    V2 = "v2"
    V3 = "v3"
    V4 = "v4"

    @property
    def index(self) -> int:
        return PRIORITIES.index(self)

    @classmethod
    def parse(cls, value: object, default: "Priority | None" = None) -> "Priority":
        if default is None:
            default = cls.V3
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            return default
        normalized = value.strip().lower()
        for priority in PRIORITIES:
            if priority.value == normalized:
                return priority
        return default


PRIORITIES = (Priority.V1, Priority.V2, Priority.V3, Priority.V4)


def priority_map_to_wire(values: dict[Priority, int]) -> dict[str, int]:
    return {priority.value: int(values.get(priority, 0)) for priority in PRIORITIES}


def effective_queue_length(priority: Priority, queued_by_priority: dict[Priority, int]) -> int:
    return sum(queued_by_priority.get(candidate, 0) for candidate in PRIORITIES[: priority.index + 1])
