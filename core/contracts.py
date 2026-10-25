"""Stable contracts shared by skills, providers, runtime, and API."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

class Evidence(BaseModel):
    provider: str
    url: str | None = None
    observed_at: str = Field(default_factory=utc_now)
    label: str | None = None
    mode: str = 'reference'

class SkillOutput(BaseModel):
    name: str = ''
    status: Literal['ok', 'partial', 'unavailable', 'error'] = 'ok'
    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    sources: list[Evidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    error_code: str | None = None

def _bounded_json(value: dict, *, max_bytes: int, max_depth: int = 12) -> dict:
    import json
    def walk(node, depth):
        if depth > max_depth:
            raise ValueError('Maximum JSON nesting exceeded.')
        if isinstance(node, dict):
            if len(node) > 256:
                raise ValueError('Too many object fields.')
            for child in node.values():
                walk(child, depth + 1)
        elif isinstance(node, list):
            if len(node) > 1024:
                raise ValueError('Too many list items.')
            for child in node:
                walk(child, depth + 1)
    walk(value, 0)
    if len(json.dumps(value, ensure_ascii=False).encode('utf-8')) > max_bytes:
        raise ValueError('Structured input exceeds the size limit.')
    return value


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    session_id: str = Field(default='default', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator('context')
    @classmethod
    def bounded_context(cls, value: dict) -> dict:
        return _bounded_json(value, max_bytes=64 * 1024)

class SaveTripRequest(BaseModel):
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    session_id: str = Field(default='default', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    expected_version: int | None = Field(default=None, ge=0)
    expected_verified_version: int | None = Field(default=None, ge=0)
    expected_draft_version: int = Field(ge=0)


class SkillRequest(BaseModel):
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    session_id: str = Field(default='direct', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    query: str = Field(default='', max_length=8000)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator('arguments')
    @classmethod
    def bounded_arguments(cls, value: dict) -> dict:
        return _bounded_json(value, max_bytes=96 * 1024)

class PreferenceRequest(BaseModel):
    user_id: str = Field(default='local-demo', pattern=r'^[A-Za-z0-9_-]{1,80}$')
    category: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=400)
    scope: Literal['global', 'trip'] = 'global'
    session_id: str = 'default'
    consent: bool = False

class SkillSpec(BaseModel):
    id: str
    description: str
    domain: str
    wave: Literal['P0', 'P1', 'P2', 'P3']
    release_state: Literal['implemented', 'planned']
    entrypoint: str | None = None
    keywords: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    mutates_state: bool = False
    depends_on: list[str] = Field(default_factory=list)
    input_bindings: dict[str, str] = Field(default_factory=dict)
    fallback_skills: list[str] = Field(default_factory=list)
    dependency_policy: Literal['require_usable', 'continue'] = 'require_usable'
    priority: int = 50
    selection_requires_any_field: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=20, ge=1, le=120)
    input_schema: dict[str, Any] = Field(default_factory=dict)
