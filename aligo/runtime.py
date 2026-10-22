"""Unified execution runtime for CLI, FastAPI, and streaming clients."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from .contracts import SkillOutput
from .providers import ProviderUnavailable
from .models import ModelUnavailable
from .intelligence import understand_intent, CatalogMatcher, IntentFrame
from .planning import PlanCompiler
from .orchestration import ExecutionCoordinator
from .model_agent import (model_first_plan, SYNTHESIS_SYSTEM, synthesis_payload,
                          ITINERARY_REPAIR_SYSTEM, ITINERARY_RESPONSE_SYSTEM)
from .trip_state import (validate_itinerary, trip_delta, render_itinerary,
                         TripValidationError)
from .content_audit import audit_itinerary
from .segmented_planning import planning_tier, generate_daily_trip
from .revision_planning import generate_revision
from .working_drafts import draft_preface
from .validation_gate import validate_for_commit, explicit_revision_intent, ItineraryGateError, unresolved_schedule_fields
from .create_gate_repair import repair_created_itinerary
from .request_normalization import trip_window, budget_currency
from .revision_evidence import audit_revision_place_evidence, cross_day_transfer_uncertainties
from .revision_consistency import uncertain_area_transfers, clear_inherited_stale_references

logger = logging.getLogger(__name__)

def safe_model_issue(exc):
    """Map provider exceptions to bounded diagnostics without exposing secrets."""
    message = str(exc)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return 'MODEL_TIMEOUT'
    if 'APIConnectionError' in message or 'ConnectError' in message:
        return 'MODEL_CONNECTION_FAILED'
    if 'APITimeoutError' in message:
        return 'MODEL_TIMEOUT'
    if 'HTTP 404' in message:
        return 'MODEL_OR_ENDPOINT_NOT_FOUND_HTTP_404'
    if 'HTTP 400' in message or 'HTTP 422' in message:
        return 'MODEL_REQUEST_REJECTED'
    if 'HTTP 429' in message:
        return 'RATE_LIMITED_HTTP_429'
    if 'HTTP 401' in message or 'HTTP 403' in message:
        return 'AUTHENTICATION_FAILED'
    if any(f'HTTP {status}' in message for status in range(500, 600)):
        return 'MODEL_PROVIDER_SERVER_ERROR'
    if 'OUTPUT_TRUNCATED' in message:
        return 'OUTPUT_TRUNCATED'
    if 'INVALID_MODEL_JSON' in message:
        return 'INVALID_MODEL_JSON'
    if 'EMPTY_MODEL_RESPONSE' in message:
        return 'EMPTY_MODEL_RESPONSE'
    if 'Timeout' in message or 'TIMEOUT' in message:
        return 'MODEL_TIMEOUT'
    return 'MODEL_UNAVAILABLE'

@dataclass
class SkillContext:
    user_id: str
    session_id: str
    query: str
    fields: dict
    store: Any
    models: Any
    providers: Any
    shared: dict = field(default_factory=dict)
    retrieval: Any = None

def extract_fields(text: str) -> dict:
    """Best-effort English fallback, never represented as full multilingual NLU."""
    value={}
    route=re.search(r'\bfrom\s+([A-Za-z][A-Za-z .-]{1,55}?)\s+to\s+([A-Za-z][A-Za-z .-]{1,55}?)(?=\s+(?:on|for|departing|with|next|in|this|starting|and)|[,!?]|$)',text,re.I)
    if route:
        value['origin']=route.group(1).strip();value['destination']=route.group(2).strip()
    # Reject action phrases such as "to go on a scenic..." as destinations.
    # Prefer a location after "in" or "around" for activity-focused trips.
    stop = r'(?:for|on|departing|with|next|this|start|starts|starting|begin|begins|beginning|by|coastal|today|tomorrow|tonight|and|after|before|during|while|where|when|from|including|at|in|to)'
    location_pattern=re.compile(
        r'\b(?P<prep>in|around|near|to|visit)\s+'
        r'(?P<place>[A-Za-z][A-Za-z .-]{1,58}?)'
        rf'(?=\s+{stop}\b|[,!?;.]|$)',
        re.I,
    )
    rejected_starts={'go','get','do','run','have','find','book','make','take','see','plan','travel','fly','visit','explore','the','a','an','my','our','your'}
    location_candidates=[]
    for match in location_pattern.finditer(text):
        candidate=match.group('place').strip(' .-')
        words=candidate.split()
        if not words or len(words)>5 or words[0].lower() in rejected_starts:continue
        # A phrase spanning another location preposition is not a place name.
        if re.search(r'\b(?:in|to|from|around|near)\b',candidate,re.I):continue
        location_candidates.append((2 if match.group('prep').lower() in {'in','around','near'} else 1,candidate))
    if location_candidates and 'destination' not in value:
        location_candidates.sort(key=lambda item:item[0],reverse=True)
        value['destination']=location_candidates[0][1]
    duration=re.search(r'\b(\d{1,2})[ -]?(?:day|days)\b',text,re.I)
    if duration:
        value['days']=int(duration.group(1))
    else:
        written_days = {
            'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
            'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
            'fifteen': 15, 'sixteen': 16, 'seventeen': 17,
            'eighteen': 18, 'nineteen': 19, 'twenty': 20,
        }
        duration_word = re.search(
            r'\b(' + '|'.join(written_days) + r')[ -]days?\b', text, re.I)
        if duration_word:
            value['days'] = written_days[duration_word.group(1).lower()]
    money=re.search(r'(?:budget(?:\s+of)?|under|up to|with)\s*(?:(?:of|to|is)\s*)?(?:(USD|EUR|CNY|GBP|CAD|AUD|JPY)\s*)?[$€£¥￥]?\s*([\d,]+(?:\.\d{1,2})?)\s*(USD|EUR|CNY|GBP|CAD|AUD|JPY)?',text,re.I)
    if money:
        value['budget']=float(money.group(2).replace(',',''))
        value.update(budget_currency(money.group(0), money.group(1), money.group(3)))
    travelers=re.search(r'\b(?:for\s+)?(\d+)\s+(?:travelers?|people|persons?|adults?)\b',text,re.I)
    if travelers:value['travelers']=int(travelers.group(1))
    date_iso=re.search(r'\b(20\d{2}-\d{2}-\d{2})\b',text)
    if date_iso:value['departure']=date_iso.group(1)
    else:
        date_words=re.search(r'\b(?:departing|departure|on|starting)\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+20\d{2})',text,re.I)
        if date_words:
            raw=date_words.group(1).replace(',','')
            for form in ('%B %d %Y','%b %d %Y'):
                try:value['departure']=datetime.strptime(raw,form).strftime('%Y-%m-%d');break
                except ValueError:pass
    airports=re.search(r'\b([A-Z]{3})\s*(?:to|[-–])\s*([A-Z]{3})\b',text)
    if airports:value['origin_iata']=airports.group(1);value['destination_iata']=airports.group(2)
    if re.search(r'\b(run|running|jog|jogging)\b',text,re.I):value['activity']='running'
    distance=re.search(r'\b(\d{1,3}(?:\.\d+)?)\s*(km|kilomet(?:er|re)s?|mi|miles?)\b',text,re.I)
    if distance:
        distance_value=float(distance.group(1))
        if distance.group(2).lower() in {'mi','mile','miles'}:distance_value*=1.609344
        value['target_distance_km']=round(distance_value,2)
    daypart=re.search(r'\b(tomorrow|today|tonight)(?:\s+(morning|afternoon|evening))?\b',text,re.I)
    if daypart:value['date_hint']=daypart.group(0).lower()
    value.update(trip_window(text))
    return value


class Runtime:
    def __init__(self, store, models, providers, registry, settings, retrieval=None):
        self.store = store
        # Runtime instances built for model-switches share the same guard.
        # A per-session lock prevents overlapping reads, generations and writes.
        if store is None:
            self._session_gates = {}
        else:
            try:
                if not hasattr(store, '_aligo_session_gates'):
                    store._aligo_session_gates = {}
                self._session_gates = store._aligo_session_gates
            except (AttributeError, TypeError):
                # Synthetic evaluation stores can be immutable or slot-only.
                self._session_gates = {}
        self.models = models
        self.providers = providers
        self.registry = registry
        self.settings = settings
        self.last_travel_skill_trace: list[dict] = []
        if retrieval is None:
            from .retrieval import Retrieval
            retrieval = Retrieval(store, models, settings)
        self.retrieval = retrieval

    def _context(self, text, history, explicit):
        fields = {}
        for item in history[-self.settings.memory_window:]:
            if item['role'] == 'user':
                fields.update(extract_fields(item['content']))
        current = extract_fields(text)
        if current.get('origin') and current.get('destination'):
            fields = {}
        if current.get('currency_ambiguous'):
            fields.pop('currency', None)
        fields.update(current)
        fields.update({k: v for k, v in explicit.items() if v is not None})
        return fields

    @staticmethod
    def _synthesize(results, query, recovery=None):
        if not results:
            return 'I can help with your trip. Please describe the activity, destination, timeframe, or constraints you have in mind.'
        recovered = {item['failed_skill'] for item in (recovery or [])
                     if any(row['name'] == item['replacement_skill'] and row['status'] in ('ok', 'partial') for row in results)}
        successful = [r for r in results if r['status'] in ('ok', 'partial')]
        if not successful:
            return '\n\n'.join(r['text'] for r in results)
        paragraphs = [r['text'] for r in successful]
        unavailable = [r['name'] for r in results if r['status'] == 'unavailable' and r['name'] not in recovered]
        if recovered:
            paragraphs.append('A declared alternative provided results for: ' + ', '.join(sorted(recovered)) + '.')
        if unavailable:
            paragraphs.append('Not available with the current providers: ' + ', '.join(unavailable) + '. No live values have been fabricated.')
        return '\n\n'.join(paragraphs)

    async def _run_skill(self, step, context, on_event):
        name = step['name']
        spec = self.registry.get(name)
        args = {**context.fields, **step.get('arguments', {})}
        if (name == 'trip-budget-allocation' and
                context.fields.get('currency_ambiguous') and
                not context.fields.get('currency')):
            # Router-proposed arguments cannot silently override ambiguous
            # monetary units extracted from the actual user request.
            args.pop('currency', None)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self.registry.run(name, context, args), timeout=spec.timeout_seconds)
        except ProviderUnavailable as exc:
            result = SkillOutput(name=name, status='unavailable', text=str(exc), error_code='PROVIDER_UNAVAILABLE')
        except (ValueError, KeyError) as exc:
            result = SkillOutput(name=name, status='partial', text=f'Invalid or incomplete input for {name}: {exc}', error_code='INVALID_INPUT')
        except asyncio.TimeoutError:
            result = SkillOutput(name=name, status='unavailable', text=f'{name} timed out.', error_code='TIMEOUT')
        except Exception as exc:
            result = SkillOutput(name=name, status='error', text=f'{name} could not complete ({type(exc).__name__}).', error_code='SKILL_ERROR')
        result.name = name
        row = result.model_dump()
        if on_event:
            await on_event('skill', row)
        return row, round((time.perf_counter() - started) * 1000)

    @staticmethod
    def _unwrap_itinerary(payload):
        """Accept an explicit itinerary wrapper without weakening trip validation."""
        if (isinstance(payload, dict) and 'days' not in payload
                and 'changes' not in payload
                and isinstance(payload.get('itinerary'), dict)):
            return payload['itinerary']
        return payload

    @staticmethod
    def _normalize_model_preferences(itinerary):
        """Canonicalize simple model preference variants without discarding constraints.

        The persistent itinerary validator remains authoritative. Unsupported,
        nested, oversized, or conflicting structures are left untouched so the
        validator rejects them rather than silently dropping user requirements.
        """
        if not isinstance(itinerary, dict) or 'preferences' not in itinerary:
            return itinerary
        raw = itinerary['preferences']
        normalized = None
        if isinstance(raw, str):
            if raw.strip() and len(raw.strip()) <= 350:
                normalized = {'travel_preferences': raw.strip()}
        elif isinstance(raw, list) and 1 <= len(raw) <= 25:
            if all(isinstance(item, str) and item.strip() for item in raw):
                combined = ', '.join(item.strip() for item in raw)
                if len(combined) <= 350:
                    normalized = {'travel_preferences': combined}
            elif all(isinstance(item, dict) for item in raw):
                collected = {}
                for item in raw:
                    if set(item) in ({'key', 'value'}, {'name', 'value'}):
                        key = item.get('key', item.get('name'))
                        value = item['value']
                    elif len(item) == 1:
                        key, value = next(iter(item.items()))
                    else:
                        collected = None
                        break
                    if not isinstance(key, str) or not key.strip() or key in collected:
                        collected = None
                        break
                    collected[key] = value
                if collected:
                    normalized = collected
        if isinstance(raw, dict) and len(raw) <= 25:
            # Local models commonly emit a string list for interests or needs.
            # Preserve the entire list as a bounded scalar, without flattening
            # complex objects or weakening the persisted validator.
            collected = dict(raw)
            for key, value in collected.items():
                if isinstance(value, list) and 1 <= len(value) <= 12:
                    if all(isinstance(part, str) and part.strip() for part in value):
                        joined = ', '.join(part.strip() for part in value)
                        if len(joined) <= 350:
                            collected[key] = joined
            normalized = collected
        if normalized is None:
            return itinerary
        return {**itinerary, 'preferences': normalized}

    @staticmethod
    def _enforce_requested_days(itinerary, fields, operation):
        """Prevent a partial plan from silently satisfying a longer request."""
        expected = fields.get('days')
        if (operation == 'create' and isinstance(expected, int)
                and not isinstance(expected, bool) and 1 <= expected <= 30
                and len(itinerary['days']) != expected):
            raise TripValidationError(
                f'Expected {expected} days, but the model supplied {len(itinerary["days"])} days.')

    @staticmethod
    def _complete_itinerary_answer(answer, itinerary):
        """Reject bare status summaries even if they are fluent and long."""
        if not isinstance(answer, str) or len(answer.strip()) < 120:
            return False
        numbers = [str(day['day']) for day in itinerary['days']]
        mentioned = sum(bool(re.search(r'(?<!\d)' + re.escape(n) + r'(?!\d)', answer))
                        for n in numbers)
        return mentioned >= max(1, (len(numbers) * 4 + 4) // 5)

    @staticmethod
    def _needs_active_trip_answer(answer):
        """Detect an absent, dismissive or context-denying trip answer."""
        if not isinstance(answer, str) or len(answer.strip()) < 24:
            return True
        if answer.strip() == '[ACTIVE_TRIP_DISCUSSION_REQUIRED]':
            return True
        return bool(re.search(
            r"(?:no tengo acceso|no dispongo de|no puedo acceder|"
            r"no (?:previous|prior|saved|existing) (?:itinerary|trip|plan)|"
            r"(?:do not|don't|cannot|can't) have access|"
            r"(?:please|por favor) (?:provide|share|proporciona|comparte))",
            answer, re.I))

    @staticmethod
    def _asks_trip_changes(query):
        if not isinstance(query, str):
            return False
        return bool(re.search(
            r"(?:what (?:has |was |did )?(?:changed|change)|"
            r"what did you change|what was updated|"
            r"qu[eé] cambi[oó]|qu[eé] cambios|cambios (?:del|en el))",
            query, re.I))

    async def _complete_active_trip_discussion(self, query, itinerary, delta=None,
                                              output_language='auto', prior_itinerary=None):
        """Answer active-trip questions from actual state, never a vague router denial."""
        context = {'request': query, 'validated_itinerary': itinerary,
                   'changes': delta or {}, 'previous_version': prior_itinerary,
                   'output_language': output_language,
                   'task': 'Answer the request directly from the saved trip. '
                           'When asked about a revision, explain only factual changes. '
                           'Do not invent missing trip history or travel facts. '
                           'If earlier state is supplied, report exact differences; '
                           'otherwise say that historical changes cannot be verified.'}
        if len(json.dumps(context, ensure_ascii=False, default=str)) > 11000:
            # Keep all activity identities; include full details for edited days.
            # This avoids feeding two redundant rich itinerary copies into a
            # short multilingual discussion that should take only one response.
            focus = set((delta or {}).get('changed_days', []))
            for key in ('validated_itinerary', 'previous_version'):
                source = context.get(key)
                if not isinstance(source, dict):
                    continue
                slim = self._compact_revision_context(source, next(iter(focus))
                                                      if len(focus) == 1 else None)
                if slim is source:
                    slim = {
                        'destination': source.get('destination'),
                        'title': source.get('title'),
                        'preferences': source.get('preferences', {}),
                        'days': [{'day': day['day'], 'title': day['title'],
                                  'activities': [a['description'] for a in day['activities']]}
                                 for day in source.get('days', [])],
                    }
                context[key] = slim
        payload = json.dumps(context, ensure_ascii=False, default=str)
        system = (ITINERARY_RESPONSE_SYSTEM + '\nFor a discussion, answer the specific '
                  'question rather than repeating the entire itinerary unless requested. '
                  'Never deny access to the provided validated itinerary.')
        try:
            answer = await asyncio.wait_for(
                self.models.complete('synthesis', system, payload),
                timeout=max(self.settings.synthesis_timeout, 20.))
            if self._needs_active_trip_answer(answer):
                raise ValueError('Active-trip answer did not use the supplied state.')
            return answer.strip()
        except (ModelUnavailable, TypeError, ValueError, asyncio.TimeoutError):
            # This fallback never fabricates a summary of changes.
            return render_itinerary(itinerary)

    @staticmethod
    def _preserve_unrequested_days(updated, baseline, query):
        """Never let an unrequested date be overwritten by a one-day edit."""
        if not baseline or not isinstance(query, str):
            return updated
        if not re.search(r'\b(?:make|change|modify|revise|edit|update|improve|adjust|'
                         r'cambia|modifica|actualiza)\b', query, re.I):
            return updated
        target_index = Runtime._requested_day(query, baseline)
        if target_index is None:
            return updated
        normalized = dict(updated)
        normalized['days'] = [
            day if day['day'] == target_index else baseline['days'][day['day'] - 1]
            for day in updated['days']
        ]
        return normalized

    @staticmethod
    def _requested_day(query, baseline):
        """Recognize one explicitly named edit day, including number words."""
        if not baseline or not isinstance(query, str):
            return None
        words = {
            'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
            'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
            'first': 1, 'second': 2, 'third': 3, 'fourth': 4, 'fifth': 5,
            'uno': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5,
        }
        matches = []
        for match in re.finditer(r'\b(?:day|d[ií]a)\s*'
                                 r'(\d{1,2}|[a-z]+)\b', query, re.I):
            raw = match.group(1).lower()
            value = int(raw) if raw.isdecimal() else words.get(raw)
            if value is not None:
                matches.append(value)
        for match in re.finditer(r'\b(first|second|third|fourth|fifth)\s+day\b',
                                 query, re.I):
            matches.append(words[match.group(1).lower()])
        unique = set(matches)
        if len(unique) != 1:
            return None
        target = unique.pop()
        return target if 1 <= target <= len(baseline['days']) else None

    @staticmethod
    def _compact_revision_context(baseline, target):
        """Keep the editable day exact, but only an index of unrelated days."""
        if target is None:
            return baseline
        return {
            'destination': baseline['destination'],
            'title': baseline.get('title'),
            'preferences': baseline.get('preferences', {}),
            'planning_assumptions': baseline.get('planning_assumptions', []),
            'key_tradeoffs': baseline.get('key_tradeoffs', []),
            'days': [day if day['day'] == target else {
                'day': day['day'], 'title': day['title'],
                'activities': [{'description': activity['description']}
                               for activity in day['activities']]}
                     for day in baseline['days']],
        }

    @staticmethod
    def _needs_translation(query, fields):
        language = str(fields.get('output_language', 'auto')).lower()
        if language in ('en', 'english'):
            return False
        if language != 'auto':
            return True
        if re.search(r'\b(?:in spanish|in french|en espa[nñ]ol|en fran[cç]ais|'
                     r'cambia|modifica|actualiza|por favor)\b', query, re.I):
            return True
        return any(ord(character) > 127 for character in query)

    async def _optional_content_refinement(self, itinerary, query, fields):
        """One bounded, opt-in content improvement, never a blind retry loop.

        A failed or low-quality rewrite is discarded. This only applies to
        creation so targeted edit invariants cannot be weakened by rewriting.
        """
        if os.getenv('ALIGO_CONTENT_REVIEW_MODE', '').strip().lower() != 'adaptive':
            return itinerary
        original = audit_itinerary(itinerary, request=query,
                                   expected_days=fields.get('days'))
        if original['score'] >= 72 or not original['findings']:
            return itinerary
        critique = {
            'task': 'create',
            'request': query,
            'existing_validated_itinerary': itinerary,
            'required_day_count': len(itinerary['days']),
            'known_destination': itinerary['destination'],
            'constraints': fields,
            'quality_gaps': original['findings'][:8],
            'instruction': (
                'Make a full, coherent replacement itinerary addressing the '
                'listed gaps. Preserve destination, trip duration, explicit '
                'travel constraints and named locations when reliable. '
                'Never add fictitious real-time claims. The old plan remains '
                'in use unless this candidate passes all validators.'
            ),
        }
        try:
            proposal = await asyncio.wait_for(
                self.models.complete('reasoning', ITINERARY_REPAIR_SYSTEM,
                                     json.dumps(critique, ensure_ascii=False),
                                     json_mode=True),
                timeout=max(self.settings.intent_timeout, 65.))
            candidate = self._normalize_model_preferences(
                self._unwrap_itinerary(proposal))
            refined = validate_itinerary(candidate, operation='create',
                                         destination_hint=itinerary['destination'])
            if (len(refined['days']) != len(itinerary['days'])
                    or refined['destination'].casefold() != itinerary['destination'].casefold()):
                return itinerary
            # A quality rewrite may add constraints; it may never erase saved ones.
            if any(refined['preferences'].get(k) != v
                   for k, v in itinerary.get('preferences', {}).items()):
                return itinerary
            candidate_review = audit_itinerary(refined, request=query,
                                               expected_days=len(itinerary['days']))
            if (candidate_review['score'] >= original['score'] + 8
                    and not any('Unverified absolute claim' in issue
                                for issue in candidate_review['findings'])):
                return refined
        except (ModelUnavailable, ValueError, TypeError, KeyError,
                asyncio.TimeoutError) as exc:
            logger.info('Optional content refinement skipped: %s', safe_model_issue(exc))
        return itinerary

    async def _prepare_trip_update(self, query, history, previous, legacy_text,
                                   frame, direct_answer, results, preferences, fields):
        """Validate an itinerary before returning or persisting any edit claim."""
        operation = frame.task.get('operation')
        self.last_travel_skill_trace = []
        fields = dict(fields)
        if isinstance(fields.get('days'), str) and fields['days'].strip().isdecimal():
            fields['days'] = int(fields['days'].strip())
        candidate = self._normalize_model_preferences(
            self._unwrap_itinerary(frame.task.get('itinerary')))
        baseline = previous['itinerary'] if previous else None
        # Scoped edits inherit their destination and length from saved state.
        # An English fallback parser may mistake infinitive action phrases
        # (for example, 'to add activities') for a destination.
        if (operation == 'revise' and baseline and
                callable(getattr(self.models, 'complete_revision_patch', None)) and
                callable(getattr(self.models, 'complete_revision_scope', None))):
            # Legacy routers must not bypass the authoritative delta editor.
            candidate = None
        if operation == 'revise' and baseline:
            fields['destination'] = baseline['destination']
            fields['days'] = len(baseline['days'])
        evidence = [dict(row) for row in results[:8]]
        for row in evidence:
            if 'data' in row:
                data = json.dumps(row['data'], ensure_ascii=False, default=str)
                row['data'] = data[:1500]
        # Legacy conversations predate structured trips. Recover the earlier
        # detailed answer so that first-time revisions still have a baseline.
        if operation == 'revise' and baseline is None and legacy_text:
            try:
                reconstruction = await asyncio.wait_for(self.models.complete(
                    'reasoning', ITINERARY_REPAIR_SYSTEM,
                    json.dumps({'task': 'Reconstruct the previous itinerary, without applying new edits.',
                                'previous_answer': legacy_text}, ensure_ascii=False), json_mode=True),
                    timeout=max(self.settings.intent_timeout, 65.))
                baseline = validate_itinerary(reconstruction.get('itinerary'), operation='create')
            except (ModelUnavailable, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
                baseline = None
        if operation == 'revise' and baseline is None:
            raise TripValidationError('No recoverable existing trip for editing.')
        used_daily_generation = False
        used_incremental_revision = False
        used_create_gate_repair = False
        try:
            updated = validate_itinerary(candidate, previous=baseline, operation=operation,
                                         destination_hint=fields.get('destination'))
            self._enforce_requested_days(updated, fields, operation)
            if operation == 'revise':
                updated = self._preserve_unrequested_days(updated, baseline, query)
        except TripValidationError as initial_error:
            # A missing/incomplete itinerary from the router is not a valid edit.
            # Ask the reasoning model for structured data; never save a tool summary.
            try:
                if (operation == 'revise' and baseline is not None
                        and (callable(getattr(self.models, 'complete_revision_patch', None))
                             or callable(getattr(self.models, 'complete_revision_day', None)))
                        and callable(getattr(self.models, 'complete_revision_scope', None))):
                    updated = await generate_revision(
                        self.models, baseline, query,
                        explicit_day=self._requested_day(query, baseline),
                        fields=fields, preferences=preferences, evidence=evidence,
                        timeout=max(self.settings.intent_timeout, 65.))
                    used_incremental_revision = True
                else:
                    duration = fields.get('days') if operation == 'create' else None
                    if (isinstance(duration, int) and not isinstance(duration, bool)
                            and 1 <= duration <= 30
                            and (duration >= 8 or bool(getattr(
                                self.models, 'daily_planning_enabled', lambda: False)()))):
                        destination = fields.get('destination')
                        if not isinstance(destination, str) or not destination.strip():
                            raise TripValidationError('Day-by-day planning requires a known destination.')
                        generated = {'itinerary': await generate_daily_trip(
                            self.models, request=query, destination=destination.strip(),
                            days=duration, fields=fields, preferences=preferences,
                            evidence=evidence, registry=self.registry, providers=self.providers,
                            skill_trace=self.last_travel_skill_trace,
                            timeout=max(self.settings.intent_timeout,
                                        getattr(self.settings, 'daily_generation_timeout', 180.)))}
                        used_daily_generation = True
                    else:
                        generated = await asyncio.wait_for(self.models.complete(
                            'reasoning', ITINERARY_REPAIR_SYSTEM,
                        json.dumps({'task': operation, 'request': query,
                                    'existing_itinerary': self._compact_revision_context(
                                        baseline, self._requested_day(query, baseline)
                                    ) if operation == 'revise' else baseline,
                                    'target_day': (self._requested_day(query, baseline)
                                                   if operation == 'revise' else None),
                                    'previous_assistant_answer': legacy_text[:1400] if baseline is None else '',
                                    'candidate_answer': (direct_answer or '')[:900],
                                    'candidate_itinerary': candidate if candidate else None,
                                    'initial_validation_error': str(initial_error),
                                    'required_day_count': fields.get('days'),
                                    'known_destination': fields.get('destination'),
                                    'tool_evidence': evidence,
                                    'saved_preferences': preferences,
                                    'content_quality': {
                                        'require_specific_local_experiences': True,
                                        'require_coherent_daily_geography': True,
                                        'require_useful_daily_notes': True,
                                        'preserve_user_constraints': True,
                                        'revision_changes_only': operation == 'revise',
                                    },
                                    'output_language': fields.get('output_language', 'auto')},
                                       ensure_ascii=False, default=str), json_mode=True),
                            timeout=max(self.settings.intent_timeout, 65.))
                    # Some local models return the itinerary object directly rather
                    # than wrapping it in {"itinerary": ...}. Both still undergo
                    # the complete, unchanged structural validator.
                    repaired = self._normalize_model_preferences(self._unwrap_itinerary(generated))
                    updated = validate_itinerary(repaired, previous=baseline,
                                                 operation=operation,
                                                 destination_hint=fields.get('destination'))
                    self._enforce_requested_days(updated, fields, operation)
                    if operation == 'revise':
                        updated = self._preserve_unrequested_days(updated, baseline, query)
            except TripValidationError as exc:
                if str(exc).startswith(('MODEL_GENERATION_FAILED:',
                                        'REVISION_PATCH_INVALID:')):
                    # Preserve specific generation and incremental-edit diagnostics
                    # instead of hiding them behind a generic itinerary error.
                    raise
                logger.warning('Structured itinerary validation failed: router=%s; repair=%s',
                               initial_error, exc)
                failure = TripValidationError(
                    f'INVALID_STRUCTURED_ITINERARY: router={initial_error}; repair={exc}')
                # Preserve review-only diagnostic progress without committing
                # a partial itinerary or reporting task success.
                if getattr(exc, 'partial_days', None):
                    failure.partial_days = exc.partial_days
                if getattr(exc, 'failed_day', None) is not None:
                    failure.failed_day = exc.failed_day
                if getattr(exc, 'rejected_day', None):
                    failure.rejected_day = exc.rejected_day
                raise failure from exc
            except (ModelUnavailable, ValueError, TypeError, AttributeError, asyncio.TimeoutError) as exc:
                issue = safe_model_issue(exc)
                # The model gateway's exception is already reduced to profile names
                # and HTTP/status categories. Do not discard its root cause in logs.
                if isinstance(exc, ModelUnavailable):
                    logger.warning('Structured itinerary model stage failed (%s): %s', issue, exc)
                    raise TripValidationError(f'MODEL_GENERATION_FAILED:{issue}') from exc
                logger.warning('Structured itinerary stage raised %s (%s)', type(exc).__name__, issue)
                raise TripValidationError('INVALID_STRUCTURED_ITINERARY') from exc
        if (operation == 'create' and len(updated['days']) <= 7
                and not used_daily_generation):
            updated = await self._optional_content_refinement(updated, query, fields)
        # One hard gate applies to router plans, segmented plans, full-day
        # replacements, activity patches and optional refinements alike.
        if operation == 'revise' and baseline:
            updated = clear_inherited_stale_references(updated, baseline)
        try:
            updated = validate_for_commit(
                updated, previous=baseline if operation == 'revise' else None,
                intent=explicit_revision_intent(
                    query, target_days=([self._requested_day(query, baseline)]
                                        if self._requested_day(query, baseline) is not None else None)
                ) if operation == 'revise' else None)
        except ItineraryGateError as rejection:
            rejection.draft_candidate = updated
            if operation != 'create':
                raise
            try:
                updated = await repair_created_itinerary(
                    self.models, updated, rejection, request=query,
                    fields=fields, preferences=preferences,
                    timeout=max(self.settings.intent_timeout, 65.))
            except TripValidationError as exc:
                exc.draft_candidate = getattr(exc, 'draft_candidate', updated)
                exc.issues = getattr(exc, 'issues', rejection.issues)
                raise
            used_create_gate_repair = True
            self._enforce_requested_days(updated, fields, operation)
            updated = validate_for_commit(updated)
        delta = trip_delta(baseline if operation == 'revise' else None, updated)
        if (operation == 'revise' and not delta['changed_days']
                and not delta['changed_preferences'] and not delta.get('changed_trip_fields')):
            raise TripValidationError('The proposed revision makes no changes.')
        # The router's answer may be about a Skill instead of the revised trip.
        # Always generate the final revision from the validated state.
        if operation == 'revise' and used_incremental_revision:
            final_answer = render_itinerary(updated)
        elif operation == 'revise' and self._requested_day(query, baseline) is not None \
                and not self._needs_translation(query, fields):
            # All days are already validated; rendering them is deterministic.
            # Do not spend another model call regenerating the same full plan.
            final_answer = render_itinerary(updated)
        elif operation == 'revise':
            target = self._requested_day(query, baseline)
            localized_edit = target is not None and self._needs_translation(query, fields)
            if localized_edit:
                response_payload = {
                    'request': query,
                    'destination': updated['destination'],
                    'changed_days': [day for day in updated['days'] if day['day'] == target],
                    'changed_preferences': {key: updated['preferences'].get(key)
                                            for key in delta.get('changed_preferences', [])},
                    'output_language': fields.get('output_language', 'auto'),
                }
                response_system = (
                    'Answer in the language of the user request. Describe the modified '
                    'day and meaningful changes using only this validated data. '
                    'Other days remain unchanged in stored state. '
                    'Do not regenerate or invent the full itinerary. Be concise.'
                )
            else:
                response_payload = {'request': query, 'existing_itinerary': baseline,
                                    'validated_itinerary': updated, 'changes': delta,
                                    'output_language': fields.get('output_language', 'auto')}
                response_system = ITINERARY_RESPONSE_SYSTEM
            try:
                generated = await asyncio.wait_for(self.models.complete(
                    'synthesis', response_system,
                    json.dumps(response_payload, ensure_ascii=False, default=str)),
                    timeout=max(self.settings.synthesis_timeout, 20.))
                valid_response = (isinstance(generated, str) and len(generated.strip()) >= 45
                                  if localized_edit else
                                  self._complete_itinerary_answer(generated, updated))
                if not valid_response:
                    raise ValueError('Short or invalid itinerary response.')
                final_answer = generated.strip()
            except (ModelUnavailable, ValueError, TypeError, asyncio.TimeoutError):
                final_answer = render_itinerary(updated)
        elif not used_create_gate_repair and self._complete_itinerary_answer(direct_answer, updated):
            final_answer = direct_answer.strip()
        else:
            # Avoid a third cloud request for a valid newly created itinerary.
            # A deterministic rendering is grounded in the validated plan.
            final_answer = render_itinerary(updated)
        return updated, delta, final_answer

    @staticmethod
    def _candidate_adoption(trace: list[dict], itinerary: dict | None) -> dict | None:
        """Best-effort, name-based adoption proxy; not a factual quality score."""
        if not itinerary or not isinstance(itinerary.get('days'), list):
            return None
        per_day = {day.get('day'): day for day in itinerary['days']
                   if isinstance(day, dict)}
        offered, used = 0, 0
        for event in trace:
            if event.get('kind') != 'candidate_evidence':
                continue
            day = per_day.get(event.get('day'))
            if not day:
                continue
            described = ' '.join(' '.join(str(activity.get(k) or '')
                                          for k in ('place_name', 'venue_name', 'description'))
                                 for activity in day.get('activities') or []
                                 if isinstance(activity, dict)).casefold()
            names = {str(candidate.get('name') or '').strip().casefold()
                     for candidate in event.get('candidate_places') or []
                     if isinstance(candidate, dict) and candidate.get('name')}
            offered += len(names)
            used += sum(bool(name and name in described) for name in names)
        return {'candidate_count': offered, 'name_matches': used,
                'adoption_proxy': round(used / offered, 3) if offered else None,
                'method': 'per-day candidate name match, not geographic or factual verification'}

    async def execute(self, user_id, session_id, query, context=None, on_event=None):
        key = (user_id, session_id)
        # The counter covers current and queued requests so cleanup cannot race
        # with a waiter acquiring the session lock.
        if key not in self._session_gates:
            self._session_gates[key] = [asyncio.Lock(), 0]
        gate = self._session_gates[key]
        gate[1] += 1
        try:
            async with gate[0]:
                return await self._execute_unlocked(user_id, session_id, query,
                                                    context=context, on_event=on_event)
        finally:
            gate[1] -= 1
            if gate[1] == 0 and self._session_gates.get(key) is gate:
                self._session_gates.pop(key, None)

    async def _execute_unlocked(self, user_id, session_id, query, context=None, on_event=None):
        start = time.perf_counter()
        history = self.store.messages(user_id, session_id, self.settings.memory_window)
        explicit = context or {}
        fields = self._context(query, history, explicit)
        preferences = self.store.preferences(user_id, session_id)
        verified_previous = self.store.active_itinerary(user_id, session_id)
        working_previous = self.store.working_itinerary(user_id, session_id)
        if working_previous and (verified_previous is None or
                                 working_previous['base_version'] == verified_previous['version']):
            previous = {'version': working_previous['base_version'],
                        'itinerary': working_previous['itinerary'],
                        'is_draft': True,
                        'draft_version': working_previous['draft_version']}
        else:
            previous = verified_previous
            working_previous = None
        if previous:
            fields.setdefault('destination', previous['itinerary']['destination'])
            fields.setdefault('days', len(previous['itinerary']['days']))
        # For existing V3.4 sessions without structured state, provide the most
        # substantive earlier assistant reply as optional recovery context.
        legacy_text = max((row['content'] for row in history if row['role'] == 'assistant'),
                          key=len, default='')
        if len(legacy_text) < 200:
            legacy_text = ''
        direct_answer = None
        frame = None
        model_failure = False
        model_issue = None
        model_diagnostic = None
        explicit_save = bool(re.fullmatch(
            r"\s*(?:please\s+|i want to\s+|can you\s+)?save(?:\s+(?:my|the|this|current))?"
            r"\s+(?:itinerary|trip|travel plan|draft)(?:\s+please)?[.!?]*\s*", query, re.I))
        if explicit_save:
            frame = IntentFrame(fields=dict(fields), skill_calls=[], task={'operation': 'save'},
                                mode='model_direct', limitations=[])
        if frame is None and getattr(self.settings, 'agent_mode', 'model-first') == 'model-first' and self.models.configured('router'):
            if on_event:
                await on_event('progress', {'stage': 'router'})
            try:
                frame, direct_answer = await asyncio.wait_for(
                    model_first_plan(query, history, fields, preferences, self.models,
                                     self.registry, self.settings.max_skill_calls,
                                     active_trip=(previous['itinerary'] if previous else None),
                                     previous_itinerary=legacy_text),
                    timeout=max(self.settings.intent_timeout, 65.))
            except (ModelUnavailable, ValueError, asyncio.TimeoutError, TypeError, KeyError) as exc:
                model_failure = True
                model_issue = safe_model_issue(exc)
                logger.warning('Travel planning router failed (%s): %s', model_issue, exc)
                model_diagnostic = {'stage': 'router', 'code': model_issue}
        if frame is None:
            if model_failure:
                frame = IntentFrame(fields=dict(fields), skill_calls=CatalogMatcher(self.registry).rank(
                    query, fields, max_results=self.settings.max_skill_calls),
                    limitations=['Cloud model planning failed; catalog matching was used. Check the model connection.'])
            else:
                try:
                    frame = await asyncio.wait_for(understand_intent(
                        query, history, fields, self.models, self.registry, self.settings.max_skill_calls),
                        timeout=self.settings.intent_timeout)
                except asyncio.TimeoutError:
                    frame = IntentFrame(fields=dict(fields), skill_calls=CatalogMatcher(self.registry).rank(
                        query, fields, max_results=self.settings.max_skill_calls),
                        limitations=['Intent-model deadline exceeded; catalog matching was used.'])
        frame.fields.update({k: v for k, v in explicit.items() if v is not None})
        plan = PlanCompiler(self.registry, maximum=self.settings.max_skill_calls).compile(frame.skill_calls)
        ctx = SkillContext(user_id, session_id, query, frame.fields, self.store, self.models, self.providers, retrieval=self.retrieval)
        self.store.add_message(user_id, session_id, 'user', query)
        if frame.task.get('operation') == 'save':
            from .working_drafts import saved_response
            try:
                snapshot = self.store.save_visible_itinerary(
                    user_id, session_id,
                    expected_verified_version=verified_previous['version'] if verified_previous else 0,
                    expected_draft_version=working_previous['draft_version'] if working_previous else 0)
                saved = {
                    'saved_snapshot': snapshot,
                    'saved_status': 'draft' if snapshot['source_status'] == 'draft' else 'validated',
                    'active_itinerary': verified_previous,
                    'working_draft': working_previous,
                    'draft_history': self.store.working_itinerary_history(user_id, session_id),
                    'saved_snapshots': self.store.saved_trip_snapshots(user_id, session_id),
                }
                output = saved_response(saved, session_id)
            except ValueError as exc:
                output = {'answer': str(exc), 'results': [], 'planner_mode': 'saved_state',
                          'session_id': session_id, 'active_itinerary': verified_previous,
                          'working_draft': working_previous, 'trip_change': None,
                          'model_issue': None, 'model_diagnostic': None}
            output['elapsed_ms'] = round((time.perf_counter() - start) * 1000)
            self.store.add_message(user_id, session_id, 'assistant', output['answer'])
            if on_event:
                await on_event('done', output)
            return output
        if on_event:
            await on_event('plan', {**plan.as_dict(), 'planner_mode': frame.mode,
                                    'fields': frame.fields, 'goals': frame.goals})
        coordinator = ExecutionCoordinator(self.registry, self.settings, self._run_skill)
        results, trace, recovery = await coordinator.run(plan, ctx, on_event)
        answer = direct_answer if direct_answer is not None else self._synthesize(results, query, recovery)
        # Compose a grounded natural-language response even when there is only one Skill.
        # Direct LLM answers need no second model request.
        if direct_answer is None and not model_failure and self.models.configured('synthesis') and frame.task.get('operation') not in ('create', 'revise'):
            try:
                candidate = await asyncio.wait_for(self.models.complete(
                    'synthesis', SYNTHESIS_SYSTEM,
                    synthesis_payload(query, history, preferences, results, frame.goals, recovery)),
                    timeout=self.settings.synthesis_timeout)
                if isinstance(candidate, str) and candidate.strip():
                    answer = candidate.strip()
            except (ModelUnavailable, asyncio.TimeoutError, TypeError, ValueError):
                frame.limitations.append('Natural-language synthesis was unavailable; showing Skill results.')
        # Certified state and provisional edits are intentionally separate.
        active_state = verified_previous
        change = None
        quality_signals = None
        revision_evidence_audit = None
        if getattr(self.settings, 'agent_mode', 'model-first') == 'model-first':
            operation = frame.task.get('operation')
            if operation in ('create', 'revise'):
                try:
                    if on_event:
                        await on_event('progress', {'stage': 'itinerary_generation'})
                    itinerary, change, answer = await self._prepare_trip_update(
                        query, history, previous, legacy_text, frame, direct_answer,
                        results, preferences, frame.fields)
                    # Complete the diagnostic quality audit before committing.
                    # Its soft scores are not hard constraints; the shared
                    # validation gate independently enforces hard invariants.
                    try:
                        quality_signals = audit_itinerary(
                            itinerary, request=query, expected_days=frame.fields.get('days'))
                    except (ValueError, TypeError, KeyError) as exc:
                        logger.warning('Optional itinerary audit unavailable: %s', type(exc).__name__)
                    # Provider-backed candidate review is advisory and bounded.
                    # It never certifies tickets/hours or changes itinerary fields.
                    # Failure of a live provider cannot destroy a valid offline edit.
                    if operation == 'revise' and previous:
                        try:
                            revision_evidence_audit = await asyncio.wait_for(
                                audit_revision_place_evidence(
                                    previous['itinerary'], itinerary, self.providers),
                                timeout=12)
                        except (asyncio.TimeoutError, ProviderUnavailable,
                                ValueError, KeyError, TypeError) as exc:
                            revision_evidence_audit = {
                                'status': 'review_unavailable',
                                'reason': type(exc).__name__,
                                'limitations': ['Evidence refresh was not completed.']}
                    try:
                        active_state = self.store.save_itinerary(
                            user_id, session_id, itinerary,
                            expected_version=verified_previous['version'] if verified_previous else 0,
                            operation=(frame.task.get('operation') if verified_previous else 'create'),
                            expected_draft_version=working_previous['draft_version'] if working_previous else 0,
                            intent=(explicit_revision_intent(query)
                                    if previous and frame.task.get('operation') == 'revise' else None))
                    except ItineraryGateError as gate_exc:
                        gate_exc.draft_candidate = itinerary
                        raise
                except (TripValidationError, ValueError) as exc:
                    change = None
                    quality_signals = None
                    draft_candidate = getattr(exc, 'draft_candidate', None)
                    draft_saved = None
                    if isinstance(draft_candidate, dict):
                        try:
                            # A proposed draft must actually differ from the working baseline.
                            meaningful = not previous or bool(
                                trip_delta(previous['itinerary'], draft_candidate)['changed_days']
                                or trip_delta(previous['itinerary'], draft_candidate).get('changed_trip_fields')
                                or trip_delta(previous['itinerary'], draft_candidate)['changed_preferences'])
                            if meaningful:
                                draft_issues = [entry.as_dict() for entry in getattr(exc, 'issues', [])
                                                if hasattr(entry, 'as_dict')]
                                if not draft_issues:
                                    draft_issues = [{'code': 'REVIEW_NEEDED',
                                                     'detail': str(exc)[:230]}]
                                draft_saved = self.store.save_working_itinerary(
                                    user_id, session_id, draft_candidate,
                                    base_version=verified_previous['version'] if verified_previous else 0,
                                    expected_draft_version=(working_previous['draft_version']
                                                            if working_previous else 0),
                                    issues=draft_issues)
                        except (TripValidationError, ValueError, TypeError, KeyError):
                            draft_saved = None
                    if draft_saved:
                        working_previous = draft_saved
                        answer = render_itinerary(
                            draft_saved['itinerary'],
                            preface=draft_preface(draft_saved))
                        model_issue = None
                        model_diagnostic = None
                        frame.limitations.append('A provisional itinerary is saved for continued editing.')
                    else:
                        gate_issues = [issue.as_dict() for issue in exc.issues] if isinstance(exc, ItineraryGateError) else []
                        if frame.task.get('operation') == 'revise':
                            try:
                                self.store.record_revision_diagnostic(
                                    user_id, session_id,
                                    baseline_version=previous['version'] if previous else 0,
                                    stage='revision_validation',
                                    details={'code': type(exc).__name__,
                                             'message': str(exc)[:1000],
                                             'issues': gate_issues,
                                             'attempts': getattr(exc, 'revision_attempts', [])[:2]})
                            except (ValueError, TypeError, OSError):
                                logger.warning('Revision diagnostic write unavailable')

                        if str(exc).startswith('MODEL_GENERATION_FAILED:'):
                            model_issue = str(exc).partition(':')[2]
                            model_diagnostic = {'stage': 'itinerary_generation', 'code': model_issue}
                            if model_issue == 'RATE_LIMITED_HTTP_429':
                                answer = ('The cloud model reached its request or token rate limit (HTTP 429) '
                                          'while generating the itinerary. No changes were saved. '
                                          'Wait for the rate-limit window to reset before retrying.')
                            else:
                                answer = ('The planning model could not finish generating a structured trip. '
                                          'No itinerary changes were saved. Check the model status and retry.')
                        elif gate_issues:
                            model_issue = 'ITINERARY_GATE_REJECTED'
                            model_diagnostic = {'stage': 'precommit_validation',
                                                'code': model_issue, 'issues': gate_issues[:12]}
                            primary = gate_issues[0]
                            location = (f"Day {primary['day']}, activity {primary['activity_index']}" if
                                        primary.get('day') is not None and primary.get('activity_index') is not None
                                        else f"Day {primary['day']}" if primary.get('day') is not None
                                        else 'Trip')
                            reason = ' '.join(str(primary.get('detail') or '').split())[:200]
                            diagnosis = f"{primary['code']} ({location})"
                            answer = (f'The itinerary could not pass the final validation gate: {diagnosis}. '
                                      + (f'{reason} ' if reason else '')
                                      + 'No changes were saved. The previous trip remains unchanged.')
                        elif str(exc).startswith('REVISION_PATCH_INVALID:'):
                            model_issue = 'REVISION_PATCH_INVALID'
                            model_diagnostic = {
                                'stage': 'revision_validation', 'code': model_issue,
                                'detail': str(exc).partition(':')[2][:160],
                            }
                            # Surface a stable diagnostic token in the transcript; error
                            # banners can be missed when the chat log is exported.
                            diagnostic_detail = str(exc).partition(':')[2]
                            known_codes = (
                                'REVISION_ACTIVITY_TIME_CONFLICT',
                                'REVISION_DUPLICATE_NAMED_VENUE',
                                'REVISION_TIME_REQUIREMENT_NOT_MET',
                                'REVISION_TOPIC_REQUIREMENT_NOT_MET',
                                'REVISION_NEW_ACTIVITY_MISSING_DURATION',
                                'REVISION_NEW_ACTIVITY_MISSING_START_TIME',
                                'REVISION_PATCH_NO_EFFECT',
                            )
                            diagnostic_code = next(
                                (code for code in known_codes if code in diagnostic_detail),
                                'REVISION_PATCH_INVALID')
                            bounded_reason = ' '.join(diagnostic_detail.split())[:220]
                            model_diagnostic['detail'] = bounded_reason
                            answer = ('The itinerary edit could not pass validation '
                                      f'({diagnostic_code}). '
                                      + (f'Detail: {bounded_reason}. ' if bounded_reason else '')
                                      + 'No changes were saved; your previous itinerary is intact.')
                        else:
                            model_issue = 'ITINERARY_VALIDATION_FAILED'
                            model_diagnostic = {'stage': 'itinerary_validation', 'code': model_issue}
                            answer = ('The model returned an incomplete or invalid itinerary, so it was not saved. '
                                      'Your previous itinerary remains unchanged. Please try again.')
                        frame.limitations.append(f'Itinerary update not committed: {model_issue}')
                        logger.warning('Travel itinerary update rejected: %s', model_issue)
                        # A failed edit must not collapse an existing Journey into an
                        # error-only response. Re-display the most recent editable state
                        # without claiming that the requested change was applied.
                        if frame.task.get('operation') == 'revise' and previous:
                            visible = working_previous or previous
                            status = ('working draft' if working_previous else 'saved itinerary')
                            notice = (f'I could not apply the latest change to your {status}. '
                                      'The plan below is unchanged and remains editable. '
                                      'Try asking me to replace or move a lower-priority stop. '
                                      'No new version was saved.')
                            answer = render_itinerary(visible['itinerary'], preface=notice)
                            model_diagnostic = {**(model_diagnostic or {}),
                                                'revision_applied': False,
                                                'recoverable': True,
                                                'displayed_version_unchanged': True}
                            model_issue = None
            elif model_failure:
                if model_issue == 'RATE_LIMITED_HTTP_429':
                    answer = ('The cloud model has reached its request or token rate limit (HTTP 429). '
                              'No itinerary changes were saved. Wait for the provider window to reset, then retry.')
                elif model_issue == 'AUTHENTICATION_FAILED':
                    answer = ('The cloud model rejected its API credentials. '
                              'No itinerary changes were saved. Check the server-side model key.')
                else:
                    answer = ('The planning model is temporarily unavailable. '
                              'No itinerary changes were saved. Please retry when the model is available.')
        if (getattr(self.settings, 'agent_mode', 'model-first') == 'model-first'
                and previous and frame.task.get('operation') == 'discuss'
                and not model_failure and
                (self._needs_active_trip_answer(answer) or self._asks_trip_changes(query))):
            prior_trip = (self.store.itinerary_version_document(
                user_id, session_id, previous['version'] - 1)
                if previous['version'] > 1 else None)
            historical_delta = trip_delta(prior_trip, previous['itinerary']) if prior_trip else None
            answer = await self._complete_active_trip_discussion(
                query, previous['itinerary'], historical_delta,
                output_language=frame.fields.get('output_language', 'auto'),
                prior_itinerary=prior_trip)
        self.store.add_message(user_id, session_id, 'assistant', answer)
        current_working = self.store.working_itinerary(user_id, session_id)
        displayed_state = current_working or active_state
        schedule_uncertainties = (
            [issue.as_dict() for issue in unresolved_schedule_fields(displayed_state['itinerary'])]
            if displayed_state and displayed_state.get('itinerary') else [])
        output = {'answer': answer, 'results': results, 'planner_mode': frame.mode,
                  'working_draft': current_working,
                  'draft_history': self.store.working_itinerary_history(user_id, session_id),
                  'saved_snapshots': self.store.saved_trip_snapshots(user_id, session_id),
                  'elapsed_ms': round((time.perf_counter()-start)*1000),
                  'session_id': session_id, 'fields': frame.fields,
                  'plan': plan.as_dict(), 'execution_trace': trace, 'recovery': recovery,
                  'planner_limitations': frame.limitations,
                  'model_issue': model_issue, 'model_diagnostic': model_diagnostic,
                  'active_itinerary': active_state, 'trip_change': change,
                  'content_quality_signals': quality_signals,
                  'schedule_uncertainties': schedule_uncertainties,
                  'local_transfer_uncertainties': (
                      uncertain_area_transfers(displayed_state['itinerary'])
                      if displayed_state and displayed_state.get('itinerary') else []),
                  'cross_day_transfer_uncertainties': (
                      cross_day_transfer_uncertainties(displayed_state['itinerary'])
                      if displayed_state and displayed_state.get('itinerary') else []),
                  'revision_evidence_audit': revision_evidence_audit,
                  'skill_candidate_adoption': self._candidate_adoption(
                      self.last_travel_skill_trace, active_state.get('itinerary') if active_state else None),
                  'trip_planning_tier': (planning_tier(len(displayed_state['itinerary']['days'])).name
                                         if displayed_state and displayed_state.get('itinerary') else None)}
        if on_event:
            await on_event('done', output)
        return output
