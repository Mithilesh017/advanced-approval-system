"""A random few of the approvals the AI makes on its own are checked afterwards by a person."""
import json

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_user, execute, login, query, use_model_score

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
SPOT_CHECKS = '/api/auth/spot_checks'
REVIEW = '/api/auth/review_spot_check'
UPDATE_SETTINGS = '/api/auth/update_approval_settings'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def submit(client, **changes):
    response = client.post('/api/predict', json={**REQUEST, **changes})
    assert response.status_code == 200, response.get_json()
    return response.get_json()['status']


def stored_checks(app_db):
    return query(app_db, 'SELECT request_id, verdict, reviewed_by, comment FROM SpotChecks ORDER BY id')


def sample_always(app_db, monkeypatch, selected=True):
    monkeypatch.setattr(app_db, 'spot_check_selected', lambda percent: selected)


@pytest.fixture
def staff(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.99)  # The AI approves on its own.
    create_user(app_db, 'staff@example.com')
    return session(app_db, 'staff@example.com')


def test_a_sampled_automatic_approval_waits_for_a_person_to_check_it(app_db, staff, monkeypatch):
    sample_always(app_db, monkeypatch)
    assert submit(staff) == 'APPROVED'

    [saved] = query(app_db, 'SELECT id, final_decision FROM Requests')
    assert saved['final_decision'] == 'APPROVED'  # The employee is not held up by the check.
    assert stored_checks(app_db) == [{'request_id': saved['id'], 'verdict': None, 'reviewed_by': None, 'comment': None}]
    [event] = query(app_db, "SELECT request_id, details FROM AuditEvents WHERE action = 'spotcheck.sampled'")
    assert (event['request_id'], json.loads(event['details'])) == (saved['id'], {'percent': 5})


def test_only_approvals_nobody_saw_are_checked(app_db, staff, monkeypatch):
    sample_always(app_db, monkeypatch)
    execute(app_db, "UPDATE Organizations SET approval_mode = 'shadow' WHERE id = ?", (app_db.DEFAULT_ORGANIZATION_ID,))
    assert submit(staff) == 'PENDING_REVIEW'  # Shadow mode: a person sees every request already.

    execute(app_db, "UPDATE Organizations SET approval_mode = 'automatic' WHERE id = ?", (app_db.DEFAULT_ORGANIZATION_ID,))
    use_model_score(app_db, monkeypatch, 0.01)
    assert submit(staff) == 'PENDING_REVIEW'  # Escalated, so a person decides it anyway.
    assert stored_checks(app_db) == []


def test_the_share_that_is_checked_can_only_be_raised(app_db, staff):
    admin = session(app_db, SUPER_ADMIN['email'])
    settings = admin.get('/api/auth/organization').get_json()['approval_settings']
    assert (settings['spot_check_percent'], settings['minimum_spot_check_percent']) == (5, 5)

    assert admin.post(UPDATE_SETTINGS, json={'spot_check_percent': 40}).status_code == 200
    assert admin.get(SPOT_CHECKS).get_json()['spot_check_percent'] == 40

    for refused in (4, -1, 101, 12.5, True, '40'):
        assert admin.post(UPDATE_SETTINGS, json={'spot_check_percent': refused}).status_code == 400
    assert query(app_db, 'SELECT spot_check_percent FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,)) == [
        {'spot_check_percent': 40}
    ]
    assert [json.loads(event['details']) for event in query(app_db, "SELECT details FROM AuditEvents WHERE action = 'settings.updated'")] == [
        {'spot_check_percent': {'from': 5, 'to': 40}}
    ]


def test_the_sampled_share_follows_the_setting(app_db, staff, monkeypatch):
    seen = []
    monkeypatch.setattr(app_db, 'spot_check_selected', lambda percent: seen.append(percent) or False)
    assert session(app_db, SUPER_ADMIN['email']).post(UPDATE_SETTINGS, json={'spot_check_percent': 25}).status_code == 200

    assert submit(staff) == 'APPROVED'
    assert seen == [25]
    assert stored_checks(app_db) == []


def test_an_admin_says_whether_the_approval_was_right(app_db, staff, monkeypatch):
    sample_always(app_db, monkeypatch)
    submit(staff)
    admin = session(app_db, SUPER_ADMIN['email'])

    listed = admin.get(SPOT_CHECKS).get_json()
    [check] = listed['checks']
    assert listed['waiting'] == 1
    assert (check['request_type'], check['purpose'], check['verdict']) == ('Hotel Booking', REQUEST_DETAILS['Purpose'], None)

    assert admin.post(REVIEW, json={'id': check['id'], 'verdict': 'CORRECT'}).status_code == 200
    assert stored_checks(app_db) == [
        {'request_id': check['request_id'], 'verdict': 'CORRECT', 'reviewed_by': SUPER_ADMIN['email'], 'comment': None}
    ]
    assert admin.get(SPOT_CHECKS).get_json()['waiting'] == 0
    [event] = query(app_db, "SELECT actor_email, request_id, details FROM AuditEvents WHERE action = 'spotcheck.reviewed'")
    assert (event['actor_email'], event['request_id'], json.loads(event['details'])) == (
        SUPER_ADMIN['email'], check['request_id'], {'verdict': 'CORRECT'}
    )


def test_calling_an_approval_wrong_needs_a_reason(app_db, staff, monkeypatch):
    sample_always(app_db, monkeypatch)
    submit(staff)
    admin = session(app_db, SUPER_ADMIN['email'])
    [check] = admin.get(SPOT_CHECKS).get_json()['checks']

    assert admin.post(REVIEW, json={'id': check['id'], 'verdict': 'WRONG'}).status_code == 400
    assert admin.post(REVIEW, json={'id': check['id'], 'verdict': 'MAYBE', 'comment': 'Not sure'}).status_code == 400
    assert stored_checks(app_db)[0]['verdict'] is None

    assert admin.post(REVIEW, json={'id': check['id'], 'verdict': 'WRONG', 'comment': 'No receipt for a hotel stay'}).status_code == 200
    assert stored_checks(app_db) == [
        {'request_id': check['request_id'], 'verdict': 'WRONG', 'reviewed_by': SUPER_ADMIN['email'],
         'comment': 'No receipt for a hotel stay'}
    ]
    # The same check cannot be done twice.
    assert admin.post(REVIEW, json={'id': check['id'], 'verdict': 'CORRECT'}).status_code == 409


def test_spot_checks_stay_inside_one_organization(app_db, staff, monkeypatch):
    sample_always(app_db, monkeypatch)
    submit(staff)
    [check] = session(app_db, SUPER_ADMIN['email']).get(SPOT_CHECKS).get_json()['checks']

    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'admin@other.test', role='Admin', organization_id=other_org)
    outsider = session(app_db, 'admin@other.test')
    assert outsider.get(SPOT_CHECKS).get_json()['checks'] == []
    assert outsider.post(REVIEW, json={'id': check['id'], 'verdict': 'CORRECT'}).status_code == 404

    # Employees never see the checks on their own requests.
    assert staff.get(SPOT_CHECKS).status_code == 403
    assert staff.post(REVIEW, json={'id': check['id'], 'verdict': 'CORRECT'}).status_code == 403
    assert stored_checks(app_db)[0]['verdict'] is None
