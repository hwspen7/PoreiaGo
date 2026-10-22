"""Generic dependency planner, parallel execution layers, and bounded calls."""
from __future__ import annotations

from dataclasses import dataclass, field


class PlanError(ValueError):
    pass


@dataclass
class ExecutionPlan:
    layers: list[list[dict]] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    @property
    def names(self):
        return [item['name'] for layer in self.layers for item in layer]

    def as_dict(self):
        return {'layers': [[item['name'] for item in layer] for layer in self.layers],
                'skills': self.names, 'rejected': self.rejected}


class PlanCompiler:
    def __init__(self, registry, *, maximum: int = 12):
        self.registry = registry
        self.maximum = maximum

    def compile(self, requests: list[dict]) -> ExecutionPlan:
        plan = ExecutionPlan()
        calls: dict[str, dict] = {}
        visiting: set[str] = set()
        done: set[str] = set()

        def add(name: str, arguments: dict, *, prerequisite: bool = False):
            spec = self.registry.get(name)
            if not spec or spec.release_state != 'implemented' or not spec.entrypoint:
                raise PlanError(f'Unknown or unimplemented capability: {name}')
            if spec.mutates_state:
                raise PlanError(f'Mutating capability requires explicit authorization: {name}')
            if name in visiting:
                raise PlanError(f'Circular skill dependencies involving {name}')
            if name in done:
                return
            visiting.add(name)
            for dependency in spec.depends_on:
                add(dependency, {}, prerequisite=True)
            visiting.remove(name)
            if len(calls) >= self.maximum:
                raise PlanError(f'Plan exceeds execution limit ({self.maximum})')
            calls[name] = {'name': name, 'arguments': arguments if isinstance(arguments, dict) else {},
                           'dependencies': list(spec.depends_on), 'prerequisite': prerequisite}
            done.add(name)

        for step in requests:
            try:
                if not isinstance(step, dict) or not isinstance(step.get('name'), str):
                    raise PlanError('Malformed skill request')
                # Compile each requested skill atomically so a failing branch leaves no orphan prerequisites.
                before = set(calls)
                try:
                    add(step['name'], step.get('arguments', {}))
                except PlanError:
                    for added in set(calls) - before:
                        calls.pop(added)
                        done.discard(added)
                    visiting.clear()
                    raise
            except PlanError as exc:
                plan.rejected.append({'name': step.get('name', '') if isinstance(step, dict) else '', 'reason': str(exc)})
        remaining = dict(calls)
        completed: set[str] = set()
        while remaining:
            ready = [item for item in remaining.values() if all(dep in completed for dep in item['dependencies'])]
            if not ready:
                raise PlanError('Dependency graph could not be resolved')
            ready.sort(key=lambda item: (self.registry.get(item['name']).priority, item['name']))
            plan.layers.append(ready)
            for item in ready:
                completed.add(item['name'])
                remaining.pop(item['name'])
        return plan
