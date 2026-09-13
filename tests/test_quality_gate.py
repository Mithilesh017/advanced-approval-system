"""An organization only leaves shadow mode once its own decisions show the AI can be trusted."""
import json
from datetime import datetime, timedelta

import pytest

from conftest import SUPER_ADMIN, create_organization, create_platform_owner, create_user, execute, login, query

PLATFORM_QUALITY = '/api/platform/decision_quality'
UPDATE_ORGANIZATION = '/api/platform/update_organization'
QUALITY = '/api/auth/decision_quality'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def add(app_db, organization_id, ai_decision, final_decision, reviewed_by='boss@example.com', days_ago=1):
    created = datetime.utcnow() - timedelta(days=days_ago)
    rows = execute(
        app_db,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, ai_decision, final_decision, reviewed_by, submitted_by, organization_id, created_at) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.5, ?, ?, ?, "
        "'staff@example.com', ?, ?) RETURNING id",
        (ai_decision, final_decision, reviewed_by, organization_id, created.strftime('%Y-%m-%d %H:%M:%S'))
    )
    return rows[0][0]


def good_history(app_db, organization_id, decisions=20):
    """Decisions the AI got right, enough of them to pass every check."""
    for _ in range(decisions):
        add(app_db, organization_id, 'APPROVED', 'APPROVED')


def checks_of(readiness):
    return {check['name']: check['passed'] for check in readiness['checks']}


@pytest.fixture
def owner(app_db):
    return session(app_db, create_platform_owner(app_db))


@pytest.fixture
def acme(app_db):
    return create_organization(app_db, 'Acme Pvt Ltd', approval_mode='shadow')


def test_a_new_organization_is_not_ready_and_the_page_says_why(app_db, owner, acme):
    report = owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).get_json()
    readiness = report['readiness']

    assert readiness['ready'] is False
    assert checks_of(readiness) == {'decisions': False, 'agreement': False, 'missed_problems': False}
    assert len(readiness['blocked_by']) == 3
    assert report['days'] == 90


def test_switching_to_automatic_is_refused_until_the_numbers_are_good(app_db, owner, acme):
    add(app_db, acme, 'APPROVED', 'APPROVED')
    refused = owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic'})
    assert refused.status_code == 409
    assert refused.get_json()['readiness']['ready'] is False
    assert query(app_db, 'SELECT approval_mode FROM Organizations WHERE id = ?', (acme,)) == [{'approval_mode': 'shadow'}]

    good_history(app_db, acme)
    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic'}).status_code == 200
    assert query(app_db, 'SELECT approval_mode FROM Organizations WHERE id = ?', (acme,)) == [{'approval_mode': 'automatic'}]


def test_too_many_missed_problems_keep_an_organization_in_shadow_mode(app_db, owner, acme):
    good_history(app_db, acme, decisions=19)
    for _ in range(2):
        add(app_db, acme, 'APPROVED', 'REJECTED')  # 2 of 21 the AI would have let through: above the limit.

    readiness = owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).get_json()['readiness']
    # The people still mostly agreed with the AI; what stops it is what it would have let through.
    assert checks_of(readiness) == {'decisions': True, 'agreement': True, 'missed_problems': False}
    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic'}).status_code == 409


def test_wrong_spot_checks_count_against_an_organization(app_db, owner, acme):
    good_history(app_db, acme, decisions=40)
    for verdict in ('CORRECT', 'CORRECT', 'WRONG'):
        request_id = add(app_db, acme, 'APPROVED', 'APPROVED', reviewed_by=None)
        execute(app_db, 'INSERT INTO SpotChecks (organization_id, request_id, verdict) VALUES (?, ?, ?)', (acme, request_id, verdict))

    readiness = owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).get_json()['readiness']
    assert checks_of(readiness) == {'decisions': True, 'agreement': True, 'missed_problems': True, 'spot_checks': False}
    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic'}).status_code == 409


def test_neuzem_can_overrule_the_gate_and_the_audit_log_records_it(app_db, owner, acme):
    forced = owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic', 'force': True})
    assert forced.status_code == 200
    assert query(app_db, 'SELECT approval_mode FROM Organizations WHERE id = ?', (acme,)) == [{'approval_mode': 'automatic'}]

    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'organization.updated'")
    details = json.loads(event['details'])
    assert details['changes'] == {'approval_mode': 'automatic'}
    assert details['forced'] is True
    assert details['readiness']['ready'] is False and details['readiness']['blocked_by']

    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'shadow', 'force': 'yes'}).status_code == 400


def test_going_back_to_shadow_mode_is_never_blocked(app_db, owner, acme):
    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'shadow'}).status_code == 200
    assert owner.post(UPDATE_ORGANIZATION, json={'id': acme, 'allow_training_data': True}).status_code == 200
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'organization.updated' ORDER BY id DESC LIMIT 1")
    assert 'readiness' not in json.loads(event['details'])


def test_a_company_sees_the_same_checklist_as_neuzem(app_db, acme):
    create_user(app_db, 'boss@acme.test', role='SuperAdmin', organization_id=acme)
    good_history(app_db, acme)

    readiness = session(app_db, 'boss@acme.test').get(QUALITY, query_string={'days': 90}).get_json()['readiness']
    assert readiness['ready'] is True
    assert [check['name'] for check in readiness['checks']] == ['decisions', 'agreement', 'missed_problems']


@pytest.mark.parametrize('days', [30, 365])
def test_the_company_checklist_uses_the_gate_window_whatever_period_is_shown(app_db, owner, acme, days):
    create_user(app_db, 'boss@acme.test', role='SuperAdmin', organization_id=acme)
    for _ in range(20):
        add(app_db, acme, 'APPROVED', 'APPROVED', days_ago=45)  # Inside Neuzem's 90 days, outside the last 30.
    for _ in range(5):
        add(app_db, acme, 'APPROVED', 'REJECTED', days_ago=200)  # Outside 90 days, inside the last year.

    report = session(app_db, 'boss@acme.test').get(QUALITY, query_string={'days': days}).get_json()
    gate = owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).get_json()['readiness']

    assert report['days'] == days
    assert report['readiness'] == gate
    assert report['readiness']['days'] == 90 and report['readiness']['ready'] is True


def test_the_numbers_belong_to_one_organization_only(app_db, owner, acme):
    other = create_organization(app_db, 'Other Co', approval_mode='shadow')
    good_history(app_db, acme)

    assert owner.get(PLATFORM_QUALITY, query_string={'organization_id': other}).get_json()['readiness']['ready'] is False
    assert owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).get_json()['readiness']['ready'] is True
    assert owner.get(PLATFORM_QUALITY, query_string={'organization_id': 9999}).status_code == 404
    assert owner.get(PLATFORM_QUALITY, query_string={'organization_id': acme, 'days': 7}).status_code == 400


def test_only_neuzem_sees_another_organizations_numbers(app_db, acme):
    create_user(app_db, 'manager@example.com', role='Admin')
    admin = session(app_db, 'manager@example.com')
    assert admin.get(PLATFORM_QUALITY, query_string={'organization_id': acme}).status_code == 403
    assert admin.post(UPDATE_ORGANIZATION, json={'id': acme, 'approval_mode': 'automatic', 'force': True}).status_code == 403
    assert session(app_db, SUPER_ADMIN['email']).get(PLATFORM_QUALITY, query_string={'organization_id': acme}).status_code == 403
