"""The local benchmark must distinguish model-only and Skill pipeline runs."""
from __future__ import annotations

import pytest

from evaluation import evaluate_travel_quality as quality


class _FakeGateway:
    profiles = []


@pytest.mark.asyncio
@pytest.mark.parametrize('with_skills', [False, True])
async def test_live_quality_mode_explicitly_controls_skill_registry(monkeypatch,
                                                                     with_skills):
    recorded = []
    monkeypatch.setattr(quality, 'installed_models', lambda: {'mock:latest'})
    monkeypatch.setattr(quality, 'ModelGateway', lambda settings: _FakeGateway())
    monkeypatch.setattr(quality, 'require_local_gateway', lambda gateway: gateway)

    async def fake_evaluate(case, model, registry, saved, *, providers=None,
                            skill_mode='model_only'):
        recorded.append((registry.get('schedule-conflict-validation') is not None,
                         providers is not None, skill_mode))
        return ({'status': 'PASS', 'score': 100, 'case_id': case['id'],
                 'elapsed_seconds': 0.0,
                 'warnings': [], 'passed_structure': True,
                 'skill_calls_trace': [], 'skill_mode': skill_mode}, None)

    monkeypatch.setattr(quality, 'evaluate_case', fake_evaluate)
    report = await quality.run_models(
        [{'id': 'test', 'request': 'Trip', 'kind': 'create'}],
        ['mock:latest'], with_skills=with_skills)
    expected = 'skill_orchestrated' if with_skills else 'model_only'
    assert report['evaluation_mode'] == expected
    assert recorded == [(with_skills, with_skills, expected)]


def test_human_report_displays_skill_mode_and_call_trace(tmp_path):
    report = {'results': [{
        'model': 'mock', 'trial': 1, 'case_id': 'city3',
        'status': 'PASS', 'score': 100, 'passed_structure': True,
        'warnings': [], 'elapsed_seconds': 1.0, 'role_calls': [],
        'skill_mode': 'skill_orchestrated',
        'skill_calls_trace': [{'skill': 'schedule-conflict-validation',
                               'status': 'ok', 'conflicts': 0}],
        'answer': 'Trip planned.',
    }]}
    dest = tmp_path / 'report.json'
    quality.save_report(report, dest)
    content = dest.with_suffix('.md').read_text(encoding='utf-8')
    assert 'Skill evaluation mode: skill_orchestrated' in content
    assert 'Specialist Skill calls recorded: 1' in content
    assert 'schedule-conflict-validation' in content
