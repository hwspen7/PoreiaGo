"""Multi-provider model router with dynamically configured roles and fallbacks."""
import asyncio
import json
import os
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

class ModelUnavailable(Exception):
    """A model request failed with a safely summarized diagnostic."""

# Independent ceilings for each day. These are not shared across a trip.
DAILY_DETAIL_MAX_OUTPUT_TOKENS = 8192
# A shorter second attempt limits the cost of repeated runaway generations.
DAILY_DETAIL_RETRY_OUTPUT_TOKENS = 4096


def _loopback_compatible(base_url: str) -> bool:
    """Classify local OpenAI-compatible endpoints without contacting them."""
    try:
        url = urlsplit(base_url)
        return url.scheme in ('http', 'https') and url.hostname in ('localhost', '127.0.0.1', '::1')
    except (ValueError, TypeError):
        return False


def _is_loopback_ollama(base_url: str) -> bool:
    """Recognize common loopback URLs without treating lookalike hosts as local."""
    try:
        url = urlsplit(base_url)
        return (url.scheme in ('http', 'https') and
                url.hostname in ('localhost', '127.0.0.1', '::1') and
                url.port == 11434)
    except (ValueError, TypeError):
        return False


class ModelGateway:
    def __init__(self, settings, *, preset=None, presets_path=None):
        self.settings=settings
        self.profiles=json.loads(Path(settings.models_file).read_text(encoding='utf-8'))['profiles']
        self.preset_name = preset or os.getenv('POREIAGO_MODEL_PRESET', '').strip() or None
        self._stage_profiles = None
        if self.preset_name:
            from intelligence.model_presets import load_preset
            preset_file = Path(presets_path or os.getenv('POREIAGO_MODEL_PRESETS_FILE') or
                               Path(settings.models_file).parent / 'model_presets.json')
            self._stage_profiles = load_preset(preset_file, self.preset_name, self.profiles)
        self._clients={}
        self.last_completion = None
        self._task_completion = ContextVar("poreiago_model_completion", default=None)

    def register_profile(self, profile, *, replace=False):
        required = ('name', 'role', 'provider', 'model')
        if not isinstance(profile, dict) or any(not isinstance(profile.get(key), str) or not profile[key].strip() for key in required):
            raise ValueError('A model profile requires name, role, provider, and model.')
        if profile['provider'] not in {'openai-compatible', 'local-hf'}:
            raise ValueError('Unsupported provider type.')
        if profile['provider'] == 'openai-compatible' and (not profile.get('base_url') or not profile.get('api_key_env')):
            raise ValueError('OpenAI-compatible profiles require base_url and api_key_env.')
        if self._stage_profiles is not None:
            raise ValueError('Register profiles before activating a model preset.')
        existing = next((i for i, p in enumerate(self.profiles) if p['name'] == profile['name']), None)
        if existing is not None:
            if not replace:
                raise ValueError('Model profile already exists.')
            self.profiles[existing] = dict(profile)
        else:
            self.profiles.append(dict(profile))
        self._clients.pop(profile['name'], None)
        for key in list(self._clients):
            if isinstance(key, tuple) and key[0] == profile['name']:
                self._clients.pop(key, None)

    def catalog(self):
        return [{'name':p['name'],'role':p['role'],'provider':p['provider'],'model':os.getenv(p.get('model_env', ''), p['model']),'configured':self._configured(p)} for p in self.profiles]

    def selected_profiles(self):
        """Return the effective stage map without exposing credentials."""
        if self._stage_profiles is None:
            return None
        return {
            stage: {'profile': p['name'],
                    'model': os.getenv(p.get('model_env', ''), p['model']),
                    'provider': p['provider'],
                    'endpoint': ('ollama-local' if _is_loopback_ollama(p.get('base_url', ''))
                                 else ('local-compatible' if _loopback_compatible(p.get('base_url', ''))
                                       else 'remote'))}
            for stage, p in self._stage_profiles.items()
        }

    def _candidate_profiles(self, role, stage=None):
        if self._stage_profiles is None:
            return [p for p in self.profiles if p['role'] == role and
                    p['provider'] == 'openai-compatible']
        name = stage or role
        if name not in self._stage_profiles:
            raise ValueError(f'Unknown model stage: {name}')
        return [self._stage_profiles[name]]

    def _configured(self, profile):
        return profile.get('provider')=='local-hf' or bool(os.getenv(profile.get('api_key_env',''),''))

    def configured(self, role):
        return any(self._configured(p) for p in self._candidate_profiles(role))

    async def complete(self, role, system, prompt, json_mode=False, *,
                       max_output_tokens=None, response_schema=None,
                       disable_reasoning=False, stage=None):
        self.last_completion = None
        self._task_completion.set(None)
        failures=[]
        for p in self._candidate_profiles(role, stage):
            if not self._configured(p): continue
            try:
                from openai import AsyncOpenAI
                # Share connection-level clients but apply stage deadlines per request.
                # Keep the legacy name key for external test doubles and callers.
                connection_key = (p['name'], p['base_url'], p['api_key_env'])
                client = self._clients.get(connection_key) or self._clients.get(p['name'])
                if client is None:
                    client = AsyncOpenAI(api_key=os.environ[p['api_key_env']],
                                         base_url=p['base_url'],
                                         timeout=self.settings.request_timeout,
                                         max_retries=0)
                    self._clients[connection_key] = client
                stage_timeout = p.get('request_timeout_seconds', self.settings.request_timeout)
                if isinstance(stage_timeout, bool) or not isinstance(stage_timeout, (int, float)) or stage_timeout <= 0:
                    stage_timeout = self.settings.request_timeout
                protocol_system = system
                if response_schema is not None:
                    protocol_system += '\nReturn JSON matching this JSON Schema exactly:\n' + json.dumps(response_schema)
                kwargs={'model':os.getenv(p.get('model_env', ''), p['model']),'messages':[{'role':'system','content':protocol_system},{'role':'user','content':prompt}], 'temperature':p.get('temperature', 0.2), 'timeout':float(stage_timeout)}
                if json_mode:
                    if (response_schema is not None and
                            (_is_loopback_ollama(p.get('base_url', '')) or
                             p.get('structured_output_mode') == 'json_schema')):
                        kwargs['response_format'] = {
                            'type': 'json_schema',
                            'json_schema': {'name': 'poreiago_structured_travel', 'strict': True,
                                            'schema': response_schema},
                        }
                    else:
                        kwargs['response_format'] = {'type': 'json_object'}
                # Use the standard OpenAI-compatible field for Ollama output budgets.
                # Keep provider-specific max_completion_tokens settings separate.
                if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and max_output_tokens > 0:
                    profile_limit = p.get('max_output_tokens')
                    if isinstance(profile_limit, int) and profile_limit > 0:
                        kwargs['max_tokens'] = min(profile_limit, max_output_tokens)
                    else:
                        # Don't send both provider-specific max_completion_tokens
                        # and standard max_tokens in the same request.
                        if not p.get('max_completion_tokens'):
                            kwargs['max_tokens'] = max_output_tokens
                elif isinstance(p.get('max_output_tokens'), int) and p['max_output_tokens'] > 0:
                    kwargs['max_tokens'] = p['max_output_tokens']
                extra = {}
                if 'max_completion_tokens' in p:
                    limit = p['max_completion_tokens']
                    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
                        limit = min(limit, max_output_tokens)
                    extra['max_completion_tokens'] = limit
                effective_disable = p.get('disable_reasoning', disable_reasoning)
                if effective_disable and _is_loopback_ollama(p.get('base_url', '')):
                    extra['reasoning_effort'] = 'none'
                elif p.get('reasoning_effort'):
                    extra['reasoning_effort'] = p['reasoning_effort']
                if extra: kwargs['extra_body']=extra
                schema_fallback = False
                try:
                    response = await client.chat.completions.create(**kwargs)
                except Exception as exc:
                    # Older local Ollama versions may lack OpenAI-compatible
                    # json_schema support. Retry JSON mode only for an explicit
                    # schema/format rejection, never for a length/timeout fault.
                    message = str(exc).casefold()
                    if (kwargs.get('response_format', {}).get('type') == 'json_schema'
                            and getattr(exc, 'status_code', None) in (400, 422)
                            and any(term in message for term in
                                    ('json_schema', 'response_format', 'schema', 'format'))):
                        kwargs['response_format'] = {'type': 'json_object'}
                        schema_fallback = True
                        response = await client.chat.completions.create(**kwargs)
                    else:
                        raise
                choice=response.choices[0]
                usage = getattr(response, 'usage', None)
                self.last_completion = {
                    'profile': p['name'],
                    'stage': stage or role,
                    'preset': self.preset_name,
                    'model': kwargs['model'],
                    'finish_reason': getattr(choice, 'finish_reason', None),
                    'prompt_tokens': getattr(usage, 'prompt_tokens', None),
                    'completion_tokens': getattr(usage, 'completion_tokens', None),
                    'total_tokens': getattr(usage, 'total_tokens', None),
                    'max_output_tokens': kwargs.get('max_tokens',
                                                     extra.get('max_completion_tokens')),
                    'schema_fallback': schema_fallback,
                }
                self._task_completion.set(dict(self.last_completion))
                if getattr(choice, 'finish_reason', None) == 'length':
                    raise ModelUnavailable('OUTPUT_TRUNCATED')
                content=choice.message.content or ''
                if not content.strip():
                    raise ModelUnavailable('EMPTY_MODEL_RESPONSE')
                if json_mode:
                    try:
                        parsed=json.loads(content)
                    except (json.JSONDecodeError, ValueError) as exc:
                        raise ModelUnavailable('INVALID_MODEL_JSON') from exc
                    if not isinstance(parsed, dict):
                        raise ModelUnavailable('INVALID_MODEL_JSON')
                    return parsed
                return content
            except Exception as exc:
                status = getattr(exc, 'status_code', None)
                if isinstance(status, int):
                    detail = f'HTTP {status}'
                elif isinstance(exc, ModelUnavailable):
                    detail = str(exc)
                elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
                    detail = 'MODEL_TIMEOUT'
                else:
                    detail = type(exc).__name__
                failures.append(f"{p['name']}: {detail}")
        raise ModelUnavailable('; '.join(failures) if failures else f'No configured model for role: {role}')

    def completion_metadata(self):
        """Per-coroutine usage metadata for concurrent local evaluations."""
        return self._task_completion.get()

    def daily_concurrency_limit(self):
        """Opt-in bounded parallelism; one Ollama generation at a time by default.

        Multiple requests can compete for VRAM and increase tail latency. Users
        may set POREIAGO_DAILY_PARALLELISM to 2 or 3 after checking hardware.
        """
        raw = os.getenv('POREIAGO_DAILY_PARALLELISM', '1').strip()
        try:
            value = int(raw)
        except ValueError:
            value = 1
        return max(1, min(3, value))

    def _stage_token_budget(self, stage, default):
        """Honor explicit preset budgets while preserving legacy defaults."""
        if self._stage_profiles is not None:
            profile = self._stage_profiles[stage]
            if profile.get('_explicit_stage_max_tokens'):
                return profile.get('max_output_tokens',
                                   profile.get('max_completion_tokens', default))
        return default

    async def complete_daily_detail(self, system, prompt, *, day_number, compact=False):
        """Use a bounded, schema-guided response for one itinerary day.

        Every day has its own maximum 8192-token response allowance, regardless
        of itinerary length (1-30 days) or earlier days' token usage. An
        OUTPUT_TRUNCATED/invalid-JSON retry uses the compact prompt and a smaller
        4096-token ceiling to avoid repeating a costly runaway completion.
        Provider-specific ceilings remain authoritative when lower.
        """
        from planning.segmented_planning import daily_response_schema
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=(DAILY_DETAIL_RETRY_OUTPUT_TOKENS if compact
                               else self._stage_token_budget('daily',
                                                             DAILY_DETAIL_MAX_OUTPUT_TOKENS)),
            response_schema=daily_response_schema(day_number),
            disable_reasoning=True, stage='daily',
        )

    async def complete_activity_repair(self, system, prompt, *, day_number):
        """Repair a single activity while preserving the rest of the validated day.

        A small, schema-constrained output avoids a full-day regeneration.
        The per-day 8192-token budget remains unchanged for ordinary details.
        """
        from planning.segmented_planning import activity_repair_response_schema
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=self._stage_token_budget('repair', 2048),
            response_schema=activity_repair_response_schema(),
            disable_reasoning=True, stage='repair',
        )

    async def complete_global_blueprint(self, system, prompt, *, day_count, compact=False):
        """Generate only the route allocation in one bounded, schema-guided call.

        Ordinary reasoning and synthesis retain their configured output limits.
        The budget scales with duration but short/flagship trips cannot burn
        8192 tokens on an unbounded first-stage route outline.
        """
        from planning.segmented_planning import blueprint_response_schema
        if isinstance(day_count, bool) or not isinstance(day_count, int) or not 1 <= day_count <= 30:
            raise ValueError('Blueprint requires a duration from 1 to 30 days.')
        if day_count <= 3:
            limit = 1536
        elif day_count <= 7:
            limit = 2560
        elif day_count <= 14:
            limit = 4096
        elif day_count <= 21:
            limit = 6144
        else:
            limit = 8192
        if compact:
            limit = max(1536, (limit * 3) // 4)
        else:
            limit = self._stage_token_budget('blueprint', limit)
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=limit,
            response_schema=blueprint_response_schema(day_count),
            disable_reasoning=True, stage='blueprint',
        )

    async def complete_revision_scope(self, system, prompt, *, response_schema):
        """Choose edited days with an independent, small, structured response."""
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=self._stage_token_budget('repair', 1024),
            response_schema=response_schema, disable_reasoning=True,
            stage='repair' if self._stage_profiles is not None else 'reasoning',
        )

    async def complete_revision_day(self, system, prompt, *, day_number,
                                    response_schema, compact=False):
        """Generate one day, never a full-trip rewrite or reused token allowance."""
        limit = 2048 if compact else 4096
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=self._stage_token_budget('repair', limit),
            response_schema=response_schema, disable_reasoning=True,
            stage='repair' if self._stage_profiles is not None else 'reasoning',
        )

    async def complete_revision_patch(self, system, prompt, *, day_number,
                                      response_schema, compact=False):
        """Generate a bounded activity edit without reproducing the existing day."""
        limit = 1536 if compact else 2048
        return await self.complete(
            'reasoning', system, prompt, json_mode=True,
            max_output_tokens=self._stage_token_budget('repair', limit),
            response_schema=response_schema, disable_reasoning=True,
            stage='repair' if self._stage_profiles is not None else 'reasoning',
        )

    def daily_planning_enabled(self):
        """Advertise support for global-blueprint plus one-day JSON generation."""
        return True

    def router_decision_only(self):
        """Router selects intent only; trip content uses the dedicated planner.

        Older profiles may omit decision_only or set it false. Keeping router
        behavior dependent on those settings enabled a legacy revision bypass.
        """
        return True

    def local_model_profile(self, role):
        """Return the effective local model profile, including an optional .env override."""
        if role not in {'embedding', 'reranking'}:
            raise ValueError(f'Unsupported local model role: {role}')
        profile = next((p for p in self.profiles if p['role'] == role), None)
        environment_name = 'POREIAGO_EMBEDDING_MODEL' if role == 'embedding' else 'POREIAGO_RERANKER_MODEL'
        explicit_override = os.getenv(environment_name)
        if profile is None:
            fallback = self.settings.embedding_id if role == 'embedding' else self.settings.reranker_id
            return {'role': role, 'model': explicit_override or fallback}
        resolved = {**profile, 'model': explicit_override or profile['model']}
        if explicit_override and explicit_override != profile['model'] and role == 'embedding':
            # Profile-specific instructions must not leak into a different model.
            resolved.pop('query_template', None)
            resolved.pop('query_instruction', None)
        return resolved

    def embedding_model_id(self):
        return self.local_model_profile('embedding')['model']

    def reranker_model_id(self):
        return self.local_model_profile('reranking')['model']

    def embedding_query(self, query):
        """Apply the query-side instruction declared by the active embedding model."""
        profile = self.local_model_profile('embedding')
        template = profile.get('query_template')
        instruction = profile.get('query_instruction')
        if template and instruction:
            return template.format(instruction=instruction, query=query)
        # Never silently apply an E5-specific instruction to a different model.
        if profile['model'] == 'intfloat/multilingual-e5-large-instruct':
            return ('Instruct: Given a travel-related question, retrieve relevant passages '
                    'from multilingual travel documents that answer the question.\nQuery: ' + query)
        return query
