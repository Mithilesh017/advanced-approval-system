"""An employee's manager decides their requests first; admins can still step in, and every decision is recorded."""
import io
import json

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_user, login, query, use_model_score

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
PDF = b'%PDF-1.4\n%%EOF\n'
SET_MANAGER = '/api/auth/set_manager'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def submit(app_db, client):
    response = client.post('/api/predict', json=REQUEST)
    assert response.status_code == 200, response.get_json()
    return latest_request(app_db)['id']


def latest_request(app_db):
    [row] = query(app_db, 'SELECT id, final_decision, approver_email, reviewed_by FROM Requests ORDER BY id DESC LIMIT 1')
    return row


def request_row(app_db, request_id):
    [row] = query(app_db, 'SELECT final_decision, approver_email, reviewed_by FROM Requests WHERE id = ?', (request_id,))
    return row


def decision_events(app_db, action):
    rows = query(app_db, 'SELECT actor_email, details FROM AuditEvents WHERE action = ? ORDER BY id', (action,))
    return [(row['actor_email'], json.loads(row['details'])) for row in rows]


@pytest.fixture
def team(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.5)  # Every request needs a person.
    for email in ('lead@example.com', 'staff@example.com', 'colleague@example.com'):
        create_user(app_db, email)
    create_user(app_db, 'manager@example.com', role='Admin')
    admin = session(app_db, SUPER_ADMIN['email'])
    assert admin.post(SET_MANAGER, json={'email': 'staff@example.com', 'manager_email': 'lead@example.com'}).status_code == 200
    return {
        'admin': admin,
        'lead': session(app_db, 'lead@example.com'),
        'staff': session(app_db, 'staff@example.com'),
        'colleague': session(app_db, 'colleague@example.com'),
    }


def test_assigning_a_manager_is_saved_shown_and_audited(app_db, team):
    users = {user['email']: user for user in team['admin'].get('/api/auth/users').get_json()}
    assert users['staff@example.com']['manager_email'] == 'lead@example.com'
    assert team['staff'].get('/api/auth/get_profile').get_json()['manager_email'] == 'lead@example.com'
    assert team['lead'].get('/api/auth/get_profile').get_json()['report_count'] == 1
    assert decision_events(app_db, 'user.manager_changed') == [
        (SUPER_ADMIN['email'], {'email': 'staff@example.com', 'from': None, 'to': 'lead@example.com'})
    ]


@pytest.mark.parametrize('body, status', [
    ({'email': 'staff@example.com', 'manager_email': 'staff@example.com'}, 400),
    ({'email': 'staff@example.com', 'manager_email': 7}, 400),
    ({'email': 'staff@example.com', 'manager_email': 'nobody@example.com'}, 404),
    ({'email': 'staff@example.com', 'manager_email': 'outsider@other.test'}, 404),
    ({'email': 'nobody@example.com', 'manager_email': 'lead@example.com'}, 404),
    ({'email': 'lead@example.com', 'manager_email': 'staff@example.com'}, 409),
])
def test_invalid_manager_assignments_are_refused(app_db, team, body, status):
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'outsider@other.test', organization_id=other_org)

    assert team['admin'].post(SET_MANAGER, json=body).status_code == status
    assert query(app_db, 'SELECT email, manager_email FROM Users WHERE manager_email IS NOT NULL') == [
        {'email': 'staff@example.com', 'manager_email': 'lead@example.com'}
    ]


def test_only_admins_assign_managers_and_only_super_admins_for_administrators(app_db, team):
    body = {'email': 'colleague@example.com', 'manager_email': 'lead@example.com'}
    assert team['lead'].post(SET_MANAGER, json=body).status_code == 403

    admin = session(app_db, 'manager@example.com')
    assert admin.post(SET_MANAGER, json=body).status_code == 200
    assert admin.post(SET_MANAGER, json={'email': 'manager@example.com', 'manager_email': 'lead@example.com'}).status_code == 403
    assert team['admin'].post(SET_MANAGER, json={'email': 'manager@example.com', 'manager_email': 'lead@example.com'}).status_code == 200


def test_requests_that_need_a_person_wait_for_the_manager(app_db, team, monkeypatch):
    submit(app_db, team['staff'])
    assert latest_request(app_db)['approver_email'] == 'lead@example.com'

    submit(app_db, team['colleague'])
    assert latest_request(app_db)['approver_email'] is None  # No manager, so the admins decide.

    use_model_score(app_db, monkeypatch, 0.99)
    submit(app_db, team['staff'])
    saved = latest_request(app_db)
    assert (saved['final_decision'], saved['approver_email']) == ('APPROVED', None)


def test_a_manager_decides_only_the_requests_waiting_for_them(app_db, team):
    staff_request = submit(app_db, team['staff'])
    colleague_request = submit(app_db, team['colleague'])

    assert team['lead'].post('/api/auth/approve_request', json={'id': colleague_request}).status_code == 404
    assert team['colleague'].post('/api/auth/approve_request', json={'id': staff_request}).status_code == 404
    assert team['lead'].post('/api/auth/reject_request', json={'id': staff_request}).status_code == 400

    assert team['lead'].post('/api/auth/approve_request', json={'id': staff_request, 'comment': 'Planned client trip'}).status_code == 200
    assert request_row(app_db, staff_request) == {'final_decision': 'APPROVED', 'approver_email': None, 'reviewed_by': 'lead@example.com'}
    assert decision_events(app_db, 'request.approved') == [('lead@example.com', {'decided_as': 'manager'})]

    # Once decided, the request no longer waits for the manager, and reopening stays with the admins.
    assert team['lead'].post('/api/auth/reject_request', json={'id': staff_request, 'comment': 'Changed my mind'}).status_code == 404
    assert team['lead'].post('/api/auth/reopen_request', json={'id': staff_request, 'comment': 'Changed my mind'}).status_code == 403


def test_admins_can_still_decide_a_request_waiting_for_a_manager(app_db, team):
    request_id = submit(app_db, team['staff'])
    assert team['admin'].post('/api/auth/reject_request', json={'id': request_id, 'comment': 'Manager on leave; over budget'}).status_code == 200
    assert decision_events(app_db, 'request.rejected') == [(SUPER_ADMIN['email'], {'decided_as': 'admin'})]


def test_a_reopened_request_goes_back_to_the_current_manager(app_db, team):
    request_id = submit(app_db, team['staff'])
    assert team['admin'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert team['admin'].post('/api/auth/reopen_request', json={'id': request_id, 'comment': 'Receipt missing'}).status_code == 200
    assert request_row(app_db, request_id)['approver_email'] == 'lead@example.com'


def test_managers_see_their_teams_requests_receipts_and_history(app_db, team):
    staff_request = submit(app_db, team['staff'])
    colleague_request = submit(app_db, team['colleague'])
    added = team['staff'].post(
        '/api/auth/add_receipts', data={'request_id': str(staff_request), 'receipts': [(io.BytesIO(PDF), 'hotel.pdf')]},
        content_type='multipart/form-data'
    )
    assert added.status_code == 201

    listed = team['lead'].get('/api/auth/team_requests').get_json()['requests']
    assert [(row['id'], row['approver_email'], row['receipt_count']) for row in listed] == [(staff_request, 'lead@example.com', 1)]
    assert team['colleague'].get('/api/auth/team_requests').get_json()['requests'] == []

    assert team['lead'].get('/api/auth/request_history', query_string={'id': staff_request}).status_code == 200
    assert team['lead'].get('/api/auth/request_history', query_string={'id': colleague_request}).status_code == 403

    assert team['lead'].get('/api/auth/request_receipts', query_string={'id': staff_request}).status_code == 200
    assert team['lead'].get('/api/auth/request_receipts', query_string={'id': colleague_request}).status_code == 404
    [receipt] = query(app_db, 'SELECT id FROM Receipts')
    assert team['lead'].get('/api/auth/receipt', query_string={'id': receipt['id']}).data == PDF


def test_changing_or_removing_a_manager_moves_waiting_requests(app_db, team):
    create_user(app_db, 'new.lead@example.com')
    request_id = submit(app_db, team['staff'])

    assert team['admin'].post(SET_MANAGER, json={'email': 'staff@example.com', 'manager_email': 'new.lead@example.com'}).status_code == 200
    assert request_row(app_db, request_id)['approver_email'] == 'new.lead@example.com'

    assert team['admin'].post(SET_MANAGER, json={'email': 'staff@example.com', 'manager_email': None}).status_code == 200
    assert request_row(app_db, request_id)['approver_email'] is None


def test_deleting_a_managers_account_sends_their_team_back_to_the_admins(app_db, team):
    request_id = submit(app_db, team['staff'])
    assert team['admin'].post('/api/auth/delete_user', json={'email': 'lead@example.com'}).status_code == 200

    assert query(app_db, 'SELECT manager_email FROM Users WHERE email = ?', ('staff@example.com',)) == [{'manager_email': None}]
    assert request_row(app_db, request_id)['approver_email'] is None
