"""Verify the V3.6.1 specialist Skill manifests in the project and runtime roots."""
from __future__ import annotations

import sys
from pathlib import Path

from aligo.config import settings
from aligo.registry import SkillRegistry

SKILLS = (
    "restaurant-discovery",
    "place-detail-verification",
    "source-verification",
    "airport-transfer",
    "public-transit-router",
    "travel-cost-estimation",
)
ENTRYPOINT_PREFIX = "aligo.travel_skill_handlers:"


def validate(root: Path) -> list[str]:
    registry = SkillRegistry(root)
    errors: list[str] = []
    print(f"Skill registry: {root}")
    installed = sum(spec.release_state == "implemented" for spec in registry.specs.values())
    print(f"  total={len(registry.specs)} implemented={installed} planned={len(registry.specs)-installed}")
    for name in SKILLS:
        spec = registry.get(name)
        if spec is None:
            errors.append(f"{name}: manifest missing")
            continue
        valid = spec.release_state == "implemented" and bool(
            spec.entrypoint and spec.entrypoint.startswith(ENTRYPOINT_PREFIX)
        )
        print(f"  {name}: {'OK' if valid else 'INVALID'} ({spec.release_state}, {spec.entrypoint})")
        if not valid:
            errors.append(f"{name}: requires V3.6.1 implemented manifest and handler entrypoint")
    if len(registry.specs) != 64 or installed != 29:
        errors.append(f"expected total=64 and implemented=29, got total={len(registry.specs)} and implemented={installed}")
    return errors


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    default_root = (project_root / "plugins" / "skills").resolve()
    runtime_root = Path(settings.extensions_dir).resolve()
    paths = list(dict.fromkeys((default_root, runtime_root)))
    failures: list[str] = []
    for path in paths:
        if not path.is_dir():
            failures.append(f"Skill registry directory not found: {path}")
            continue
        failures.extend(validate(path))
    if default_root != runtime_root:
        print(f"WARNING: runtime Skill directory differs from the project directory: {runtime_root}")
    if failures:
        print("\nVerification failed:")
        for failure in failures:
            print(f"  - {failure}")
        print("Re-extract the V3.6.1 Skill repair ZIP at the project root with overwrite enabled.")
        return 1
    print("\nV3.6.1 Skill install verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
