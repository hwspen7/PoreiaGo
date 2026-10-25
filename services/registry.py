"""Discover plugins from manifests; handlers are loaded only when invoked."""
import importlib
import importlib.util
import hashlib
import sys
import json
import time
from collections import deque
import jsonschema
from pathlib import Path
from core.contracts import SkillSpec

class SkillRegistry:
    def __init__(self, root):
        self.root=Path(root)
        self.specs={}
        self._handlers={}
        self._measurements={}
        self.reload()

    def reload(self):
        self.specs.clear();self._handlers.clear()
        for path in sorted(self.root.glob('*/manifest.json')):
            spec=SkillSpec.model_validate_json(path.read_text(encoding='utf-8'))
            if spec.id != path.parent.name:raise ValueError(f'Plugin directory mismatch: {path}')
            if spec.id in self.specs:raise ValueError(f'Duplicate skill: {spec.id}')
            self.specs[spec.id]=spec

    def metrics(self) -> dict:
        """In-process bounded metrics; never expose venue text or user prompts."""
        output = {}
        for name, rows in sorted(self._measurements.items()):
            sample = list(rows)
            durations = sorted(item[0] for item in sample)
            n = len(sample)
            output[name] = {
                'sample_size': n,
                'success_rate': round(sum(item[1] in ('ok', 'partial') for item in sample) / n, 3),
                'p50_ms': round(durations[(n - 1) // 2], 2),
                'p95_ms': round(durations[min(n - 1, int(n * 0.95))], 2),
                'status_counts': {status: sum(item[1] == status for item in sample)
                                  for status in ('ok', 'partial', 'unavailable', 'error')},
            }
        return output

    def list(self):
        return [spec.model_dump() for spec in sorted(self.specs.values(),key=lambda x:(x.wave,x.domain,x.id))]

    def get(self,name):
        return self.specs.get(name)

    async def run(self,name,context,args):
        spec=self.get(name)
        if spec is None:raise KeyError(name)
        if spec.release_state!='implemented' or not spec.entrypoint:
            from core.contracts import SkillOutput
            return SkillOutput(name=name,status='unavailable',text='This skill is in the roadmap and is not implemented.',error_code='SKILL_NOT_IMPLEMENTED')
        try:
            jsonschema.validate(args, spec.input_schema)
        except jsonschema.ValidationError as error:
            raise ValueError(f'Invalid arguments for {name}: {error.message}') from error
        if name not in self._handlers:
            module_name,func=spec.entrypoint.split(':',1)
            if module_name == 'handler.py':
                path = self.root / spec.id / 'handler.py'
                if not path.is_file():
                    raise ImportError(f'Missing skill implementation for {spec.id}: {path}')
                module_key = ('poreiago_skill_' + spec.id.replace('-', '_') + '_' +
                              hashlib.sha256((str(path.resolve()) + str(path.stat().st_mtime_ns)).encode()).hexdigest()[:12])
                if module_key not in sys.modules:
                    module_spec = importlib.util.spec_from_file_location(module_key, path)
                    if module_spec is None or module_spec.loader is None:
                        raise ImportError(f'Unable to load {path}')
                    module = importlib.util.module_from_spec(module_spec)
                    sys.modules[module_key] = module
                    try:
                        module_spec.loader.exec_module(module)
                    except Exception:
                        sys.modules.pop(module_key, None)
                        raise
                module = sys.modules[module_key]
            else:
                module = importlib.import_module(module_name)
            self._handlers[name]=getattr(module,func)
        start = time.perf_counter()
        status = 'error'
        try:
            result = await self._handlers[name](context, args)
            status = result.status
            return result
        finally:
            entries = self._measurements.setdefault(name, deque(maxlen=256))
            entries.append(((time.perf_counter() - start) * 1000., status))
