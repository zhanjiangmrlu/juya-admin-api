from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentPackage:
    id: str
    name: str
    status: str
    scene_ids: tuple[str, ...]
    sort_order: int = 0
    internal_notes: str | None = None
