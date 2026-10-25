"""Typed cross-skill bindings and bounded, manifest-declared recovery.

Every dependency is resolved from an already completed skill. This module never
executes expressions or interprets tool output as executable instructions.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Any


class DependencyUnavailable(ValueError):
    """A declared upstream dependency has not produced usable evidence."""


class BindingError(ValueError):
    """A plugin's declared input binding is invalid or cannot be resolved."""


_PATH_PART = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,100}$")


def dereference(path: str, shared: dict[str, dict]) -> Any:
    """Resolve skill-id.data.field paths; forbid traversal and arbitrary attributes."""
    if not isinstance(path, str):
        raise BindingError('Binding reference must be a string.')
    parts = path.split('.')
    if len(parts) < 2 or len(parts) > 10 or not all(_PATH_PART.fullmatch(p) for p in parts):
        raise BindingError('Binding must be a simple skill-id.data path.')
    skill_name, *keys = parts
    if keys[0] != 'data':
        raise BindingError('Only structured skill data may be bound to downstream inputs.')
    result = shared.get(skill_name)
    if result is None:
        raise DependencyUnavailable(f'Upstream skill {skill_name} has not completed.')
    if result.get('status') not in {'ok', 'partial'}:
        raise DependencyUnavailable(f'Upstream skill {skill_name} did not produce usable data.')
    obj: Any = result
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            raise BindingError(f'Skill output does not contain the declared field: {path}')
        obj = obj[key]
    # Prevent downstream handlers from altering the upstream evidence object.
    return deepcopy(obj)


@dataclass
class BindingResult:
    arguments: dict[str, Any]
    provenance: dict[str, str] = field(default_factory=dict)


def bind_arguments(spec, arguments: dict, fields: dict, shared: dict) -> BindingResult:
    """Apply optional manifest bindings and explicit references with stable precedence.

    Explicit values take precedence over default input_bindings. A model may
    specify a reference only to a declared upstream dependency, never another
    user's context or arbitrary path in the process.
    """
    resolved = {**fields, **arguments}
    provenance: dict[str, str] = {}
    dependencies = set(spec.depends_on)
    for target, path in getattr(spec, 'input_bindings', {}).items():
        if target in arguments or target in fields:
            continue
        if path.split('.')[0] not in dependencies:
            raise BindingError(f'Input binding for {target} references an undeclared dependency.')
        resolved[target] = dereference(path, shared)
        provenance[target] = path
    for target, value in list(resolved.items()):
        if not isinstance(value, dict) or set(value) != {'$from'}:
            continue
        path = value['$from']
        if not isinstance(path, str) or path.split('.')[0] not in dependencies:
            raise BindingError('An explicit binding may reference only declared dependencies.')
        resolved[target] = dereference(path, shared)
        provenance[target] = path
    return BindingResult(arguments=resolved, provenance=provenance)


def failed_dependencies(step: dict, shared: dict) -> list[str]:
    """Only hard failures block dependents; partial data can still be inspected."""
    return [name for name in step['dependencies']
            if name not in shared or shared[name].get('status') in {'error', 'unavailable'}]


def recovery_candidates(spec, registry, completed: set[str]) -> list[str]:
    """Return safe, previously unexecuted fallback Skills for this capability."""
    choices = []
    for name in getattr(spec, 'fallback_skills', []):
        other = registry.get(name)
        if (name in completed or other is None or other.release_state != 'implemented'
                or not other.entrypoint or other.mutates_state or name == spec.id):
            continue
        if not set(other.depends_on).issubset(completed):
            continue
        choices.append(name)
    return choices


class ExecutionCoordinator:
    """Execute a compiled DAG with evidence binding and strictly bounded recovery.

    Recovery is opt-in per plugin manifest. There is no LLM-generated retry loop,
    hidden tool execution, or automatic substitution of unlike capabilities.
    """

    def __init__(self, registry, settings, dispatch):
        self.registry = registry
        self.settings = settings
        self.dispatch = dispatch

    async def run(self, plan, context, on_event=None):
        import asyncio
        import time
        from core.contracts import SkillOutput

        results: list[dict] = []
        trace: list[dict] = []
        recovery: list[dict] = []
        seen: set[str] = set()
        remaining_recoveries = min(
            self.settings.max_recovery_steps,
            max(0, self.settings.max_skill_calls - len(plan.names)),
        )
        semaphore = asyncio.Semaphore(self.settings.max_parallel_skills)

        async def emit(name, data):
            if on_event:
                await on_event(name, data)

        async def run_one(step):
            spec = self.registry.get(step['name'])
            start = time.perf_counter()
            broken = failed_dependencies(step, context.shared)
            if broken and spec.dependency_policy == 'require_usable':
                row = SkillOutput(
                    name=step['name'], status='unavailable',
                    text='Cannot execute without usable dependencies: ' + ', '.join(broken) + '.',
                    error_code='DEPENDENCY_UNAVAILABLE',
                    limitations=['No substitute data was inferred for failed dependencies.'],
                ).model_dump()
                await emit('skill', row)
                return row, round((time.perf_counter() - start) * 1000), {}
            try:
                bound = bind_arguments(spec, step.get('arguments', {}), context.fields, context.shared)
            except (BindingError, DependencyUnavailable) as exc:
                row = SkillOutput(name=step['name'], status='unavailable', text=str(exc),
                                  error_code='INPUT_BINDING_UNAVAILABLE').model_dump()
                await emit('skill', row)
                return row, round((time.perf_counter() - start) * 1000), {}
            async with semaphore:
                row, elapsed = await self.dispatch(
                    {**step, 'arguments': bound.arguments}, context, on_event)
            return row, elapsed, bound.provenance

        async def run_layers():
            nonlocal remaining_recoveries
            for layer in plan.layers:
                # No same-layer sibling may consume another sibling's in-flight result.
                executions = await asyncio.gather(*(run_one(step) for step in layer))
                for step, (row, elapsed, provenance) in zip(layer, executions):
                    name = step['name']
                    seen.add(name)
                    results.append(row)
                    context.shared[name] = row
                    trace.append({'name': name, 'status': row['status'], 'elapsed_ms': elapsed,
                                  'dependencies': step['dependencies'],
                                  'input_bindings': provenance})
                # Attempt safe recovery after the entire layer, before dependents run.
                for step, (row, _, _) in zip(layer, executions):
                    if row['status'] not in ('unavailable', 'error') or remaining_recoveries <= 0:
                        continue
                    spec = self.registry.get(step['name'])
                    if not spec.fallback_skills:
                        continue
                    for alternative in recovery_candidates(spec, self.registry, seen):
                        if remaining_recoveries <= 0:
                            break
                        alternative_spec = self.registry.get(alternative)
                        if any(context.shared.get(dep, {}).get('status') not in {'ok', 'partial'}
                               for dep in alternative_spec.depends_on):
                            continue
                        remaining_recoveries -= 1
                        replanned = {'failed_skill': step['name'], 'replacement_skill': alternative,
                                     'reason': row.get('error_code') or row['status']}
                        recovery.append(replanned)
                        await emit('replan', replanned)
                        alternate_step = {'name': alternative, 'arguments': step['arguments'],
                                          'dependencies': list(alternative_spec.depends_on)}
                        new_row, elapsed, binding_trace = await run_one(alternate_step)
                        results.append(new_row)
                        seen.add(alternative)
                        context.shared[alternative] = new_row
                        trace.append({'name': alternative, 'status': new_row['status'],
                                      'elapsed_ms': elapsed, 'dependencies': alternative_spec.depends_on,
                                      'input_bindings': binding_trace, 'recovery_for': step['name']})
                        if new_row['status'] in {'ok', 'partial'}:
                            # The originating skill's declared fallback explicitly
                            # authorizes its output to satisfy downstream dependencies.
                            context.shared[step['name']] = {**new_row,
                                                             'recovered_by': alternative}
                            for record in trace:
                                if record.get('name') == step['name'] and 'recovery_for' not in record:
                                    record['recovered_by'] = alternative
                                    break
                            break

        try:
            async with asyncio.timeout(self.settings.execution_timeout):
                await run_layers()
        except TimeoutError:
            trace.append({'status': 'timeout', 'reason': 'Overall execution deadline exceeded.'})
            results.append(SkillOutput(name='runtime', status='unavailable',
                                       text='The overall execution deadline was exceeded. Some tasks may be incomplete.',
                                       error_code='EXECUTION_DEADLINE').model_dump())
        return results, trace, recovery
