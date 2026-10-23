"""Regressions for durable traveler constraints and atomic itinerary revisions.

No model calls or network access: each invariant is checked using real SQLite
transactions and the same pre-commit gate used in production.
"""
from copy import deepcopy
import sqlite3

import pytest

from aligo.memory import MemoryStore
from aligo.validation_gate import ItineraryGateError, explicit_revision_intent

USER = 'traveler'
SESSION = 'journey'


def itinerary():
    return {
        'destination': 'Example City', 'title': 'A structured journey',
        'days': [{'day': 1, 'title': 'First day', 'activities': [
            {'description': 'Visit Harbor Square', 'place_name': 'Harbor Square',
             'start_time': '08:00', 'duration_minutes': 60},
            {'description': 'Visit Harbor Gardens', 'place_name': 'Harbor Gardens',
             'venue_category': 'park', 'start_time': '10:00', 'duration_minutes': 60},
            {'description': 'Explore Local Museum', 'place_name': 'Local Museum',
             'venue_category': 'museum', 'start_time': '14:00', 'duration_minutes': 60},
        ]}, {'day': 2, 'title': 'Second day', 'activities': [
            {'description': 'Visit a waterfront street', 'start_time': '11:00',
             'duration_minutes': 60},
        ]}],
    }


def add_late(document, place='Night Market', clock='21:00'):
    result = deepcopy(document)
    result['days'][1]['activities'].append({
        'description': 'Visit ' + place, 'place_name': place,
        'start_time': clock, 'duration_minutes': 60})
    return result


def move_square(document, clock):
    result = deepcopy(document)
    result['days'][0]['activities'][0]['start_time'] = clock
    return result


def codes(exc):
    return {issue.code for issue in exc.value.issues}


def test_commit_records_anchor_atomically_and_keeps_it_over_two_edits(tmp_path):
    filename = tmp_path / 'durable.sqlite3'
    with MemoryStore(filename) as store:
        store.save_itinerary(USER, SESSION, itinerary())
        anchored = move_square(itinerary(), '07:00')
        first = store.save_itinerary(USER, SESSION, anchored, expected_version=1,
                                     intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert first['version'] == 2
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 420
        second = add_late(anchored)
        store.save_itinerary(USER, SESSION, second, expected_version=2)
        third = deepcopy(second)
        third['days'][1]['title'] = 'Updated second day'
        store.save_itinerary(USER, SESSION, third, expected_version=3)
        assert store.active_itinerary(USER, SESSION)['version'] == 4
        assert len(store.journey_constraints(USER, SESSION)) == 1
    with MemoryStore(filename) as reopened:
        assert len(reopened.journey_constraints(USER, SESSION)) == 1
        erased = deepcopy(third)
        erased['days'][0]['activities'].pop(0)
        with pytest.raises(ItineraryGateError) as error:
            reopened.save_itinerary(USER, SESSION, erased, expected_version=4)
        assert 'PRIOR_TRAVELER_CONSTRAINT_LOST' in codes(error)
        assert reopened.active_itinerary(USER, SESSION)['version'] == 4
        assert len(reopened.itinerary_versions(USER, SESSION)) == 4


def test_explicit_reschedule_replaces_anchor_in_same_commit(tmp_path):
    with MemoryStore(tmp_path / 'reschedule.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        moved = move_square(itinerary(), '07:00')
        store.save_itinerary(USER, SESSION, moved, expected_version=1,
            intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        constraints = store.journey_constraints(USER, SESSION)
        assert len(constraints) == 1
        assert constraints[0]['minute'] == 420
        with pytest.raises(ItineraryGateError):
            store.save_itinerary(USER, SESSION, itinerary(), expected_version=2)


def test_unfulfilled_explicit_reschedule_does_not_bypass_prior_anchor(tmp_path):
    with MemoryStore(tmp_path / 'unfulfilled.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        removed = deepcopy(itinerary())
        removed['days'][0]['activities'].pop(0)
        with pytest.raises(ItineraryGateError) as error:
            store.save_itinerary(USER, SESSION, removed, expected_version=1,
                intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert 'PRIOR_TRAVELER_CONSTRAINT_LOST' in codes(error)
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 480
        assert store.active_itinerary(USER, SESSION)['version'] == 1


def test_explicit_remove_only_releases_targeted_anchor(tmp_path):
    with MemoryStore(tmp_path / 'remove.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        removed = deepcopy(itinerary())
        removed['days'][0]['activities'].pop(0)
        with pytest.raises(ItineraryGateError):
            store.save_itinerary(USER, SESSION, removed, expected_version=1,
                intent=explicit_revision_intent('Remove a different place from my itinerary.'))
        assert len(store.journey_constraints(USER, SESSION)) == 1
        store.save_itinerary(USER, SESSION, removed, expected_version=1,
            intent=explicit_revision_intent('Remove Harbor Square from my itinerary.'))
        assert store.journey_constraints(USER, SESSION) == []


def test_early_topic_persists_and_can_be_explicitly_rescheduled(tmp_path):
    early = deepcopy(itinerary())
    early['days'][0]['activities'][0]['start_time'] = '07:00'
    early['days'][0]['activities'][1]['start_time'] = '09:00'
    with MemoryStore(tmp_path / 'early.sqlite3') as store:
        store.save_itinerary(USER, SESSION, early,
            intent=explicit_revision_intent('Visit some parks earlier, around 9am.'))
        moved = deepcopy(early)
        moved['days'][0]['activities'][1]['start_time'] = '11:00'
        with pytest.raises(ItineraryGateError):
            store.save_itinerary(USER, SESSION, moved, expected_version=1)
        store.save_itinerary(USER, SESSION, moved, expected_version=1,
            intent=explicit_revision_intent('Visit some parks earlier, around 11am.'))
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 660


def test_late_activity_is_identity_bound_and_not_replaceable_by_unrelated_stop(tmp_path):
    base = itinerary()
    late = add_late(base)
    with MemoryStore(tmp_path / 'late.sqlite3') as store:
        store.save_itinerary(USER, SESSION, base)
        store.save_itinerary(USER, SESSION, late, expected_version=1,
            intent=explicit_revision_intent('Add an activity after 8pm.'))
        late_record = store.journey_constraints(USER, SESSION)[0]
        assert late_record['identity'] == ['place', 'night market']
        other = add_late(base, 'Different Cafe')
        with pytest.raises(ItineraryGateError) as error:
            store.save_itinerary(USER, SESSION, other, expected_version=2)
        assert 'PRIOR_TRAVELER_CONSTRAINT_LOST' in codes(error)


def test_failed_write_rolls_back_version_and_constraint_set(tmp_path):
    with MemoryStore(tmp_path / 'atomic.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary())
        # A database failure during constraint insertion must undo the entire version write.
        store._connection.execute('''CREATE TRIGGER fail_constraints BEFORE INSERT
                ON journey_constraints BEGIN SELECT RAISE(ABORT, 'simulated failure'); END''')
        with pytest.raises(sqlite3.DatabaseError):
            store.save_itinerary(USER, SESSION, move_square(itinerary(), '07:00'),
                expected_version=1,
                intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert store.active_itinerary(USER, SESSION)['version'] == 1
        assert len(store.itinerary_versions(USER, SESSION)) == 1
        assert store.journey_constraints(USER, SESSION) == []


def test_draft_does_not_publish_pending_constraints_and_recovery_retains_them(tmp_path):
    filename = tmp_path / 'drafts.sqlite3'
    plan = itinerary()
    late = add_late(plan)
    intent = explicit_revision_intent('Add an activity after 8pm.')
    with MemoryStore(filename) as store:
        store.save_itinerary(USER, SESSION, plan)
        draft = store.save_working_itinerary(USER, SESSION, late,
            base_version=1, expected_draft_version=0, previous=plan, intent=intent)
        assert draft['draft_version'] == 1
        assert not draft['issues']
        assert store.journey_constraints(USER, SESSION) == []
        assert len(store._pending_constraint_intents(USER, SESSION, 1)) == 1
    with MemoryStore(filename) as reopened:
        draft = reopened.working_itinerary(USER, SESSION)
        assert draft['draft_version'] == 1
        following = deepcopy(late)
        following['days'][1]['title'] = 'Adjusted itinerary'
        next_draft = reopened.save_working_itinerary(USER, SESSION, following,
            base_version=1, expected_draft_version=1, previous=late)
        assert next_draft['draft_version'] == 2
        assert len(reopened._pending_constraint_intents(USER, SESSION, 2)) == 1
        saved = reopened.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=2)
        assert saved['saved_status'] == 'validated'
        assert len(reopened.journey_constraints(USER, SESSION)) == 1
        assert reopened.active_itinerary(USER, SESSION)['version'] == 2


def test_bad_draft_keeps_original_and_records_constraint_issue(tmp_path):
    with MemoryStore(tmp_path / 'review.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        erased = deepcopy(itinerary())
        erased['days'][0]['activities'].pop(0)
        draft = store.save_working_itinerary(USER, SESSION, erased,
            base_version=1, expected_draft_version=0, previous=itinerary())
        assert 'PRIOR_TRAVELER_CONSTRAINT_LOST' in {i['code'] for i in draft['issues']}
        saved = store.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=1)
        assert saved['saved_status'] == 'draft'
        assert store.active_itinerary(USER, SESSION)['version'] == 1


def test_restore_creates_new_version_but_cannot_discard_later_constraint(tmp_path):
    with MemoryStore(tmp_path / 'restore.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary())  # v1, original 08:00
        moved = move_square(itinerary(), '07:00')
        store.save_itinerary(USER, SESSION, moved, expected_version=1,
            intent=explicit_revision_intent('Go to Harbor Square at 7am.'))  # v2
        with pytest.raises(ItineraryGateError):
            store.restore_itinerary_version(USER, SESSION, 1, expected_version=2)
        assert store.active_itinerary(USER, SESSION)['version'] == 2
        new_v3 = store.restore_itinerary_version(USER, SESSION, 2, expected_version=2)
        assert new_v3['version'] == 3
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 420
        assert len(store.itinerary_versions(USER, SESSION)) == 3


def test_scope_and_delete_cover_constraints_and_draft_intents(tmp_path):
    with MemoryStore(tmp_path / 'scope.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        assert store.journey_constraints(USER, 'other-session') == []
        assert store.journey_constraints('other-user', SESSION) == []
        store.save_itinerary('other-user', SESSION, itinerary())
        store.save_working_itinerary(USER, SESSION, add_late(itinerary()),
            base_version=1, intent=explicit_revision_intent('Add an activity after 8pm.'))
        assert len(store._pending_constraint_intents(USER, SESSION, 1)) == 1
        store.delete_user(USER)
        assert store.journey_constraints(USER, SESSION) == []
        assert store._pending_constraint_intents(USER, SESSION, 1) == []
        assert store.active_itinerary('other-user', SESSION)['version'] == 1


def test_legacy_after_constraint_remains_readable_and_valid(tmp_path):
    with MemoryStore(tmp_path / 'legacy.sqlite3') as store:
        base = add_late(itinerary())
        store.save_itinerary(USER, SESSION, base)
        with store._connection:
            store._connection.execute('INSERT INTO journey_constraints VALUES(?,?,?,?,?)',
                (USER, SESSION, 'after:1200', '{"kind":"after","key":"after:1200","minute":1200}', '2026-01-01'))
        updated = deepcopy(base)
        updated['days'][1]['title'] = 'Legacy revision'
        store.save_itinerary(USER, SESSION, updated, expected_version=1)
        assert store.active_itinerary(USER, SESSION)['version'] == 2


def test_later_draft_cannot_silently_drop_staged_time_request(tmp_path):
    plan = itinerary()
    with MemoryStore(tmp_path / 'pending.sqlite3') as store:
        store.save_itinerary(USER, SESSION, plan)
        first = store.save_working_itinerary(
            USER, SESSION, add_late(plan), base_version=1,
            previous=plan, intent=explicit_revision_intent('Add an activity after 8pm.'))
        assert not first['issues']
        # The next edit removes the staged stop, without withdrawing the request.
        second = store.save_working_itinerary(
            USER, SESSION, plan, base_version=1,
            expected_draft_version=1, previous=add_late(plan))
        assert 'REVISION_TIME_REQUIREMENT_NOT_MET' in {issue['code'] for issue in second['issues']}
        saved = store.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=2)
        assert saved['saved_status'] == 'draft'
        assert store.active_itinerary(USER, SESSION)['version'] == 1
        assert store.journey_constraints(USER, SESSION) == []


def test_newer_staged_anchor_supersedes_previous_staged_clock(tmp_path):
    plan = itinerary()
    with MemoryStore(tmp_path / 'staged_reschedule.sqlite3') as store:
        store.save_itinerary(USER, SESSION, plan)
        first_plan = move_square(plan, '07:00')
        first = store.save_working_itinerary(
            USER, SESSION, first_plan, base_version=1, previous=plan,
            intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert not first['issues']
        second_plan = move_square(plan, '06:00')
        second = store.save_working_itinerary(
            USER, SESSION, second_plan, base_version=1,
            previous=first_plan, expected_draft_version=1,
            intent=explicit_revision_intent('Go to Harbor Square at 6am.'))
        assert not second['issues']
        assert len(store._pending_constraint_intents(USER, SESSION, 2)) == 1
        result = store.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=2)
        assert result['saved_status'] == 'validated'
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 360


def test_explicit_new_trip_does_not_inherit_different_trip_constraints(tmp_path):
    with MemoryStore(tmp_path / 'new_destination.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        unrelated = itinerary()
        unrelated['destination'] = 'Different City'
        unrelated['days'][0]['activities'].pop(0)
        created = store.save_itinerary(USER, SESSION, unrelated,
            expected_version=1, operation='create')
        assert created['version'] == 2
        assert store.journey_constraints(USER, SESSION) == []


def test_invalid_stored_timing_is_never_silently_ignored(tmp_path):
    with MemoryStore(tmp_path / 'tamper.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary())
        with store._connection:
            store._connection.execute('INSERT INTO journey_constraints VALUES(?,?,?,?,?)',
                (USER, SESSION, 'anchor:bad',
                 '{"kind":"anchor","key":"anchor:bad","minute":"bad","place":"Harbor Square"}',
                 '2026-01-01'))
        with pytest.raises(ItineraryGateError) as error:
            store.save_itinerary(USER, SESSION, add_late(itinerary()), expected_version=1)
        assert 'INVALID_STORED_JOURNEY_CONSTRAINT' in codes(error)
        assert store.active_itinerary(USER, SESSION)['version'] == 1


def test_draft_staged_explicit_removal_only_commits_on_approval(tmp_path):
    with MemoryStore(tmp_path / 'pending_removal.sqlite3') as store:
        base = itinerary()
        store.save_itinerary(USER, SESSION, base,
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        dropped = deepcopy(base)
        dropped['days'][0]['activities'].pop(0)
        draft = store.save_working_itinerary(USER, SESSION, dropped,
            base_version=1, previous=base,
            intent=explicit_revision_intent('Remove Harbor Square from my itinerary.'))
        assert not draft['issues']
        assert len(store.journey_constraints(USER, SESSION)) == 1
        saved = store.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=1)
        assert saved['saved_status'] == 'validated'
        assert store.journey_constraints(USER, SESSION) == []


def test_draft_staged_reschedule_checks_new_clock_before_commit(tmp_path):
    with MemoryStore(tmp_path / 'pending_anchor.sqlite3') as store:
        base = itinerary()
        store.save_itinerary(USER, SESSION, base,
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        new = move_square(base, '07:00')
        draft = store.save_working_itinerary(USER, SESSION, new,
            base_version=1, previous=base,
            intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert not draft['issues']
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 480
        saved = store.save_current_itinerary(USER, SESSION,
            expected_version=1, expected_draft_version=1)
        assert saved['saved_status'] == 'validated'
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 420


def test_failed_constraint_replacement_restores_original_requirement(tmp_path):
    with MemoryStore(tmp_path / 'replace_atomic.sqlite3') as store:
        store.save_itinerary(USER, SESSION, itinerary(),
            intent=explicit_revision_intent('Go to Harbor Square at 8am.'))
        store._connection.execute('''CREATE TRIGGER block_insert BEFORE INSERT
            ON journey_constraints BEGIN SELECT RAISE(ABORT, 'constraint failed'); END''')
        with pytest.raises(sqlite3.DatabaseError):
            store.save_itinerary(USER, SESSION, move_square(itinerary(), '07:00'),
                expected_version=1,
                intent=explicit_revision_intent('Go to Harbor Square at 7am.'))
        assert store.active_itinerary(USER, SESSION)['version'] == 1
        assert len(store.itinerary_versions(USER, SESSION)) == 1
        assert store.journey_constraints(USER, SESSION)[0]['minute'] == 480
