"""Large requests need two approvals, and whoever must decide is told by email."""
import json

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_user, login, query, use_model_score

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 60000, 'Currency': 'INR', **REQUEST_DETAILS,
}
UPDATE_SETTINGS = '/api/auth/update_approval_settings'
SET_MANAGER = '/api/auth/set_manager'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def submit(app_db, client, **changes):
    response = client.post('/api/predict', json={**REQUEST, **changes})
    assert response.status_code == 200, response.get_json()
    [row] = query(app_db, 'SELECT id FROM Requests ORDER BY id DESC LIMIT 1')
    return row['id']


def request_row(app_db, request_id):
    [row] = query(
        app_db, 'SELECT final_decision, approver_email, first_approved_by, reviewed_by FROM Requests WHERE id = ?', (request_id,)
    )
    return row


def events(app_db, action):
    rows = query(app_db, 'SELECT actor_email, details FROM AuditEvents WHERE action = ? ORDER BY id', (action,))
    return [(row['actor_email'], json.loads(row['details'] or '{}')) for row in rows]


@pytest.fixture
def notices(app_db, monkeypatch):
    """Records every approval notice instead of sending it."""
    sent = []
    monkeypatch.setattr(
        app_db.email_service, 'sendApprovalWaitingEmail',
        lambda approver, summary, link, organization, note=None: sent.append((approver, summary, link, note))
    )
    return sent


@pytest.fixture
def team(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.99)  # The AI would approve every request on its own.
    create_user(app_db, 'lead@example.com')
    create_user(app_db, 'staff@example.com')
    create_user(app_db, 'finance@example.com', role='Admin')
    admin = session(app_db, SUPER_ADMIN['email'])
    assert admin.post(SET_MANAGER, json={'email': 'staff@example.com', 'manager_email': 'lead@example.com'}).status_code == 200
    assert admin.post(UPDATE_SETTINGS, json={'second_approval_above': 50000}).status_code == 200
    return {
        'admin': admin,
        'finance': session(app_db, 'finance@example.com'),
        'lead': session(app_db, 'lead@example.com'),
        'staff': session(app_db, 'staff@example.com'),
    }


def test_a_super_admin_sets_and_clears_the_second_approval_amount(app_db, team):
    def stored():
        [row] = query(app_db, 'SELECT second_approval_above FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,))
        return row['second_approval_above']

    assert stored() == 50000
    assert team['admin'].get('/api/auth/organization').get_json()['approval_settings']['second_approval_above'] == 50000
    assert team['admin'].post(UPDATE_SETTINGS, json={'second_approval_above': None}).status_code == 200
    assert stored() is None

    assert [details for _, details in events(app_db, 'settings.updated')] == [
        {'second_approval_above': {'from': None, 'to': 50000}},
        {'second_approval_above': {'from': 50000.0, 'to': None}},
    ]


@pytest.mark.parametrize('value', [0, -5, True, '50000', float('inf')])
def test_invalid_second_approval_amounts_are_refused(app_db, team, value):
    assert team['admin'].post(UPDATE_SETTINGS, json={'second_approval_above': value}).status_code == 400
    assert team['admin'].post(UPDATE_SETTINGS, json={}).status_code == 400
    assert query(app_db, 'SELECT second_approval_above FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,)) == [
        {'second_approval_above': 50000}
    ]


def test_only_a_super_admin_sets_the_amount(app_db, team):
    assert team['finance'].post(UPDATE_SETTINGS, json={'second_approval_above': 10}).status_code == 403
    assert team['lead'].post(UPDATE_SETTINGS, json={'second_approval_above': 10}).status_code == 403


def test_a_large_request_is_never_approved_by_the_ai_alone(app_db, team):
    large = submit(app_db, team['staff'])
    assert request_row(app_db, large) == {
        'final_decision': 'ESCALATED_HIGH_VALUE', 'approver_email': 'lead@example.com', 'first_approved_by': None, 'reviewed_by': None
    }

    small = submit(app_db, team['staff'], Amount=4000)
    assert request_row(app_db, small)['final_decision'] == 'APPROVED'


def test_the_manager_gives_the_first_approval_and_an_admin_the_second(app_db, team):
    request_id = submit(app_db, team['staff'])

    first = team['lead'].post('/api/auth/approve_request', json={'id': request_id, 'comment': 'Planned client trip'})
    assert first.status_code == 200
    assert first.get_json()['final_decision'] == 'ESCALATED_SECOND_APPROVAL'
    assert request_row(app_db, request_id) == {
        'final_decision': 'ESCALATED_SECOND_APPROVAL', 'approver_email': None,
        'first_approved_by': 'lead@example.com', 'reviewed_by': None
    }
    assert events(app_db, 'request.first_approved') == [
        ('lead@example.com', {'decided_as': 'manager', 'second_approval_above': 50000})
    ]

    second = team['finance'].post('/api/auth/approve_request', json={'id': request_id})
    assert second.get_json()['final_decision'] == 'APPROVED'
    assert request_row(app_db, request_id) == {
        'final_decision': 'APPROVED', 'approver_email': None,
        'first_approved_by': 'lead@example.com', 'reviewed_by': 'finance@example.com'
    }
    assert events(app_db, 'request.approved') == [
        ('finance@example.com', {'decided_as': 'admin', 'first_approved_by': 'lead@example.com'})
    ]


def test_the_second_approval_must_come_from_another_administrator(app_db, team):
    request_id = submit(app_db, team['staff'])
    assert team['finance'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200

    # The same person cannot approve twice, and the manager is no longer the assigned approver.
    assert team['finance'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 403
    assert team['lead'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 404
    assert request_row(app_db, request_id)['final_decision'] == 'ESCALATED_SECOND_APPROVAL'

    assert team['admin'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert request_row(app_db, request_id)['reviewed_by'] == SUPER_ADMIN['email']


def test_changing_the_manager_leaves_a_half_approved_request_with_the_admins(app_db, team):
    request_id = submit(app_db, team['staff'])
    assert team['lead'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200

    assert team['admin'].post(SET_MANAGER, json={'email': 'staff@example.com', 'manager_email': 'finance@example.com'}).status_code == 200
    assert request_row(app_db, request_id)['approver_email'] is None


def test_a_half_approved_request_can_still_be_rejected_or_reopened(app_db, team):
    rejected = submit(app_db, team['staff'])
    assert team['lead'].post('/api/auth/approve_request', json={'id': rejected}).status_code == 200
    assert team['finance'].post('/api/auth/reject_request', json={'id': rejected, 'comment': 'Over the travel budget'}).status_code == 200
    assert request_row(app_db, rejected)['final_decision'] == 'REJECTED'

    reopened = submit(app_db, team['staff'])
    assert team['lead'].post('/api/auth/approve_request', json={'id': reopened}).status_code == 200
    assert team['finance'].post('/api/auth/approve_request', json={'id': reopened}).status_code == 200
    assert team['finance'].post('/api/auth/reopen_request', json={'id': reopened, 'comment': 'Wrong dates'}).status_code == 200
    # Reopening starts the approvals again with the manager.
    assert request_row(app_db, reopened) == {
        'final_decision': 'ESCALATED_MANUAL_REVIEW', 'approver_email': 'lead@example.com',
        'first_approved_by': None, 'reviewed_by': None
    }


def test_lowering_the_amount_below_a_request_leaves_one_approval_enough(app_db, team):
    assert team['admin'].post(UPDATE_SETTINGS, json={'second_approval_above': None}).status_code == 200
    request_id = submit(app_db, team['staff'])
    assert request_row(app_db, request_id)['final_decision'] == 'APPROVED'


def test_approvers_are_told_by_email_when_a_request_waits_for_them(app_db, team, notices):
    request_id = submit(app_db, team['staff'])
    [(approver, summary, link, note)] = notices
    assert (approver, note) == ('lead@example.com', None)
    assert summary == {
        'reference': f'REQ_{request_id:04d}', 'employee': 'staff@example.com', 'amount': '60000.0 INR',
        'details': 'Hotel Booking - Mumbai', 'purpose': REQUEST_DETAILS['Purpose'],
    }
    assert link.endswith('/user.html')  # Managers decide from the employee portal.

    notices.clear()
    assert team['lead'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert sorted(approver for approver, _, _, _ in notices) == ['finance@example.com', SUPER_ADMIN['email']]
    assert all(link.endswith('/admin.html') for _, _, link, _ in notices)
    assert all('second one from an administrator' in note for _, _, _, note in notices)

    notices.clear()
    assert team['finance'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert notices == []  # A decided request waits for nobody.


def test_an_employee_without_a_manager_notifies_the_administrators(app_db, team, notices):
    create_user(app_db, 'solo@example.com')
    submit(app_db, session(app_db, 'solo@example.com'))
    assert sorted(approver for approver, _, _, _ in notices) == ['finance@example.com', SUPER_ADMIN['email']]


def test_a_reopened_request_tells_the_manager_again(app_db, team, notices):
    request_id = submit(app_db, team['staff'])
    assert team['lead'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200
    assert team['finance'].post('/api/auth/approve_request', json={'id': request_id}).status_code == 200

    notices.clear()
    assert team['finance'].post('/api/auth/reopen_request', json={'id': request_id, 'comment': 'Wrong dates'}).status_code == 200
    assert [approver for approver, _, _, _ in notices] == ['lead@example.com']
