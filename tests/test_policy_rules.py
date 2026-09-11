"""Company policy rules are checked before the AI and only ever send a request to a person."""
import json
from datetime import datetime, timedelta

import pytest

from conftest import REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_user, execute, login, query, use_model_score

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
RULES = '/api/auth/policy_rules'
CREATE = '/api/auth/create_policy_rule'
UPDATE = '/api/auth/update_policy_rule'
JUNIOR_HOTEL_CAP = {'role': 'Junior Developer', 'request_type': 'Hotel Booking', 'max_amount_inr': 4000}


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


@pytest.fixture
def admin(app_db):
    return session(app_db, SUPER_ADMIN['email'])


@pytest.fixture
def staff(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.99)
    create_user(app_db, 'staff@example.com')
    return session(app_db, 'staff@example.com')


def add_rule(admin, rule_type, config, name='Test rule'):
    response = admin.post(CREATE, json={'rule_type': rule_type, 'name': name, 'config': config})
    assert response.status_code == 201, response.get_json()
    return response.get_json()['rule_id']


def submit(client, **changes):
    response = client.post('/api/predict', json={**REQUEST, **changes})
    assert response.status_code == 200, response.get_json()
    return response.get_json()['status']


def latest(app_db):
    [row] = query(app_db, 'SELECT final_decision, ai_decision, policy_violations FROM Requests ORDER BY id DESC LIMIT 1')
    row['policy_violations'] = json.loads(row['policy_violations']) if row['policy_violations'] else []
    return row


def test_amount_limit_sends_a_matching_request_to_a_person(app_db, admin, staff):
    rule_id = add_rule(admin, 'amount_limit', JUNIOR_HOTEL_CAP, name='Junior hotel cap')
    assert submit(staff) == 'PENDING_REVIEW'

    saved = latest(app_db)
    assert (saved['final_decision'], saved['ai_decision']) == ('ESCALATED_RULE', 'APPROVED')
    assert saved['policy_violations'] == [
        {'rule_id': rule_id, 'name': 'Junior hotel cap', 'reason': 'Amount ₹5,000 is above the ₹4,000 limit.'}
    ]
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'request.submitted'")
    assert json.loads(event['details'])['policy_violations'] == saved['policy_violations']

    # Employees only learn that the request is waiting for review.
    [own] = staff.get('/api/auth/my_requests').get_json()
    assert own['final_decision'] == 'ESCALATED' and 'policy_violations' not in own


@pytest.mark.parametrize('changes', [{'Amount': 3000}, {'Role': 'Senior Engineer'}, {'Request_Type': 'Flight Ticket'}])
def test_amount_limit_ignores_requests_it_does_not_cover(app_db, admin, staff, changes):
    add_rule(admin, 'amount_limit', JUNIOR_HOTEL_CAP)
    assert submit(staff, **changes) == 'APPROVED'
    assert latest(app_db)['policy_violations'] == []


def test_amount_limit_without_role_or_type_covers_every_request(app_db, admin, staff):
    add_rule(admin, 'amount_limit', {'max_amount_inr': 4000})
    assert submit(staff, Role='Senior Engineer', Request_Type='Flight Ticket') == 'PENDING_REVIEW'


def test_duplicate_request_rule_flags_repeats_within_the_window(app_db, admin, staff):
    add_rule(admin, 'duplicate_request', {'window_days': 7})
    create_user(app_db, 'colleague@example.com')
    colleague = session(app_db, 'colleague@example.com')

    assert submit(staff) == 'APPROVED'
    assert submit(colleague) == 'APPROVED'  # A different employee.
    assert submit(staff, Amount=5200) == 'APPROVED'  # A different amount.
    assert submit(staff) == 'PENDING_REVIEW'

    earlier = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
    execute(app_db, 'UPDATE Requests SET created_at = ?', (earlier,))
    assert submit(staff) == 'APPROVED'  # Matching requests older than the window no longer count.


def test_always_review_rule_ignores_letter_case(app_db, admin, staff):
    add_rule(admin, 'always_review', {'field': 'destination', 'values': ['mumbai']})
    assert submit(staff) == 'PENDING_REVIEW'
    assert latest(app_db)['policy_violations'][0]['reason'] == 'Destination "Mumbai" always needs review.'
    assert submit(staff, Destination='Delhi') == 'APPROVED'


def test_rules_take_priority_over_shadow_mode(app_db, admin, staff):
    execute(app_db, "UPDATE Organizations SET approval_mode = 'shadow' WHERE id = ?", (app_db.DEFAULT_ORGANIZATION_ID,))
    add_rule(admin, 'always_review', {'field': 'destination', 'values': ['Mumbai']})
    assert submit(staff) == 'PENDING_REVIEW'
    saved = latest(app_db)
    assert (saved['final_decision'], saved['ai_decision']) == ('ESCALATED_RULE', 'APPROVED')


def test_turned_off_rules_are_not_applied(app_db, admin, staff):
    rule_id = add_rule(admin, 'always_review', {'field': 'destination', 'values': ['Mumbai']})
    assert admin.post(UPDATE, json={'id': rule_id, 'is_active': False}).status_code == 200
    assert submit(staff) == 'APPROVED'
    assert admin.post(UPDATE, json={'id': rule_id, 'is_active': True}).status_code == 200
    assert submit(staff) == 'PENDING_REVIEW'


def test_rule_changes_are_audited(app_db, admin):
    rule_id = add_rule(admin, 'amount_limit', {'max_amount_inr': 4000}, name='Hotel cap')
    assert admin.post(UPDATE, json={'id': rule_id, 'name': 'Raised hotel cap', 'config': {'max_amount_inr': 6000}}).status_code == 200

    original = {'role': None, 'request_type': None, 'max_amount_inr': 4000}
    raised = {'role': None, 'request_type': None, 'max_amount_inr': 6000}
    events = query(app_db, "SELECT action, actor_email, details FROM AuditEvents WHERE action IN ('policy_rule.created', 'policy_rule.updated') ORDER BY id")
    assert [(e['action'], e['actor_email'], json.loads(e['details'])) for e in events] == [
        ('policy_rule.created', SUPER_ADMIN['email'], {'rule_id': rule_id, 'rule_type': 'amount_limit', 'name': 'Hotel cap', 'config': original}),
        ('policy_rule.updated', SUPER_ADMIN['email'], {
            'rule_id': rule_id, 'from': {'name': 'Hotel cap', 'config': original}, 'to': {'name': 'Raised hotel cap', 'config': raised},
        }),
    ]
    [listed] = admin.get(RULES).get_json()['rules']
    assert (listed['name'], listed['config'], listed['is_active']) == ('Raised hotel cap', raised, True)


@pytest.mark.parametrize('body', [
    {'rule_type': 'block_everything', 'name': 'Unknown type', 'config': {}},
    {'rule_type': 'amount_limit', 'name': 'X', 'config': {'max_amount_inr': 4000}},
    {'rule_type': 'amount_limit', 'name': 'No limit', 'config': {}},
    {'rule_type': 'amount_limit', 'name': 'Negative limit', 'config': {'max_amount_inr': -5}},
    {'rule_type': 'amount_limit', 'name': 'Text limit', 'config': {'max_amount_inr': '4000'}},
    {'rule_type': 'amount_limit', 'name': 'List config', 'config': [4000]},
    {'rule_type': 'duplicate_request', 'name': 'Window too long', 'config': {'window_days': 365}},
    {'rule_type': 'duplicate_request', 'name': 'Fractional window', 'config': {'window_days': 2.5}},
    {'rule_type': 'always_review', 'name': 'Unknown field', 'config': {'field': 'amount', 'values': ['1']}},
    {'rule_type': 'always_review', 'name': 'No values', 'config': {'field': 'destination', 'values': []}},
    {'rule_type': 'always_review', 'name': 'Blank value', 'config': {'field': 'destination', 'values': ['   ']}},
])
def test_invalid_rules_are_refused(app_db, admin, body):
    assert admin.post(CREATE, json=body).status_code == 400
    assert query(app_db, 'SELECT id FROM PolicyRules') == []


@pytest.mark.parametrize('changes', [{}, {'is_active': 'no'}, {'name': ''}, {'config': {'window_days': 0}}])
def test_invalid_rule_updates_are_refused(app_db, admin, changes):
    rule_id = add_rule(admin, 'duplicate_request', {'window_days': 7}, name='Repeat claims')
    assert admin.post(UPDATE, json={'id': rule_id, **changes}).status_code == 400
    assert query(app_db, 'SELECT name, config, is_active FROM PolicyRules') == [
        {'name': 'Repeat claims', 'config': json.dumps({'window_days': 7}), 'is_active': 1}
    ]


def test_only_super_admins_manage_rules_and_employees_cannot_see_them(app_db, admin):
    rule_id = add_rule(admin, 'duplicate_request', {'window_days': 7})
    create_user(app_db, 'manager@example.com', role='Admin')
    create_user(app_db, 'staff@example.com')
    manager = session(app_db, 'manager@example.com')

    assert manager.get(RULES).status_code == 200
    assert manager.post(CREATE, json={'rule_type': 'duplicate_request', 'name': 'Another', 'config': {'window_days': 3}}).status_code == 403
    assert manager.post(UPDATE, json={'id': rule_id, 'is_active': False}).status_code == 403
    assert session(app_db, 'staff@example.com').get(RULES).status_code == 403


def test_rules_belong_to_one_organization(app_db, admin, staff):
    add_rule(admin, 'always_review', {'field': 'destination', 'values': ['Mumbai']})
    other_org = create_organization(app_db, 'Other Co')
    create_user(app_db, 'super@other.test', role='SuperAdmin', organization_id=other_org)
    create_user(app_db, 'staff@other.test', organization_id=other_org)
    other_admin = session(app_db, 'super@other.test')

    assert other_admin.get(RULES).get_json()['rules'] == []
    [rule] = admin.get(RULES).get_json()['rules']
    assert other_admin.post(UPDATE, json={'id': rule['id'], 'is_active': False}).status_code == 404
    assert submit(session(app_db, 'staff@other.test')) == 'APPROVED'
