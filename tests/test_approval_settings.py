"""Neuzem sets each organization's approval mode; organizations may only make auto-approval stricter."""
import json

import pytest

from conftest import (
    REQUEST_DETAILS, SUPER_ADMIN, create_organization, create_platform_owner, create_user, execute, login, query, use_model_score,
)

REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
}
UPDATE_SETTINGS = '/api/auth/update_approval_settings'


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def submit(client):
    response = client.post('/api/predict', json=REQUEST)
    assert response.status_code == 200, response.get_json()
    return response.get_json()['status']


def stored_threshold(app_db):
    [row] = query(app_db, 'SELECT auto_approve_above FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,))
    return row['auto_approve_above']


@pytest.fixture
def owner(app_db):
    return session(app_db, create_platform_owner(app_db))


def test_existing_organizations_stay_automatic_and_new_ones_start_in_shadow(app_db, owner, monkeypatch):
    execute(app_db, 'INSERT INTO Organizations (name, join_code) VALUES (?, ?)', ('Created Before Settings', 'existing-join-code'))
    app_db.setup_database()
    monkeypatch.setattr(app_db.email_service, 'sendOrganizationCreatedEmail', lambda *args: None)
    assert owner.post('/api/platform/create_organization', json={'name': 'New Co', 'super_admin_email': 'boss@new.test'}).status_code == 201

    assert query(app_db, 'SELECT name, approval_mode FROM Organizations ORDER BY id') == [
        {'name': 'Default Organization', 'approval_mode': 'automatic'},
        {'name': 'Created Before Settings', 'approval_mode': 'automatic'},
        {'name': 'New Co', 'approval_mode': 'shadow'},
    ]


def test_neuzem_switches_an_organizations_approval_mode(app_db, owner):
    org_id = create_organization(app_db, 'Acme Pvt Ltd', approval_mode='shadow')
    # This organization has decided nothing yet, so the quality gate is overruled on purpose (see test_quality_gate.py).
    assert owner.post('/api/platform/update_organization', json={'id': org_id, 'approval_mode': 'automatic', 'force': True}).status_code == 200
    assert owner.post('/api/platform/update_organization', json={'id': org_id, 'approval_mode': 'manual'}).status_code == 400

    acme = next(o for o in owner.get('/api/platform/organizations').get_json()['organizations'] if o['id'] == org_id)
    assert (acme['approval_mode'], acme['auto_approve_above']) == ('automatic', 0.8)
    [event] = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'organization.updated'")
    details = json.loads(event['details'])
    assert (details['changes'], details['forced']) == ({'approval_mode': 'automatic'}, True)


def test_admins_see_approval_settings_but_employees_do_not(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    create_user(app_db, 'staff@example.com')

    admin_view = session(app_db, 'manager@example.com').get('/api/auth/organization').get_json()
    assert admin_view['approval_settings'] == {
        'approval_mode': 'automatic', 'auto_approve_above': 0.8,
        'minimum_auto_approve_above': 0.8, 'maximum_auto_approve_above': 0.99,
        'second_approval_above': None, 'maximum_second_approval_above': app_db.SECOND_APPROVAL_MAX_INR,
        'spot_check_percent': app_db.SPOT_CHECK_MIN_PERCENT,
        'minimum_spot_check_percent': app_db.SPOT_CHECK_MIN_PERCENT,
        'maximum_spot_check_percent': app_db.SPOT_CHECK_MAX_PERCENT,
    }
    assert 'approval_settings' not in session(app_db, 'staff@example.com').get('/api/auth/organization').get_json()


def test_super_admin_can_raise_the_threshold_and_the_change_is_audited(app_db):
    admin = session(app_db, SUPER_ADMIN['email'])
    assert admin.post(UPDATE_SETTINGS, json={'auto_approve_above': 0.9}).status_code == 200
    assert stored_threshold(app_db) == 0.9
    assert admin.post(UPDATE_SETTINGS, json={'auto_approve_above': 0.8}).status_code == 200

    events = query(app_db, "SELECT actor_email, organization_id, details FROM AuditEvents WHERE action = 'settings.updated' ORDER BY id")
    assert [(e['actor_email'], e['organization_id'], json.loads(e['details'])) for e in events] == [
        (SUPER_ADMIN['email'], app_db.DEFAULT_ORGANIZATION_ID, {'auto_approve_above': {'from': 0.8, 'to': 0.9}}),
        (SUPER_ADMIN['email'], app_db.DEFAULT_ORGANIZATION_ID, {'auto_approve_above': {'from': 0.9, 'to': 0.8}}),
    ]


@pytest.mark.parametrize('value', [0.79, 0.5, 1.0, True, '0.9', None])
def test_threshold_cannot_go_below_the_default_or_above_the_maximum(app_db, value):
    assert session(app_db, SUPER_ADMIN['email']).post(UPDATE_SETTINGS, json={'auto_approve_above': value}).status_code == 400
    assert stored_threshold(app_db) is None
    assert query(app_db, "SELECT id FROM AuditEvents WHERE action = 'settings.updated'") == []


def test_only_super_admins_change_thresholds_and_never_the_mode(app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    assert session(app_db, 'manager@example.com').post(UPDATE_SETTINGS, json={'auto_approve_above': 0.9}).status_code == 403

    super_admin = session(app_db, SUPER_ADMIN['email'])
    assert super_admin.post(UPDATE_SETTINGS, json={'approval_mode': 'shadow'}).status_code == 403
    assert super_admin.post(UPDATE_SETTINGS, json={'approval_mode': 'automatic', 'auto_approve_above': 0.9}).status_code == 403

    assert query(app_db, 'SELECT approval_mode, auto_approve_above FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,)) == [
        {'approval_mode': 'automatic', 'auto_approve_above': None}
    ]


def test_submissions_use_the_organizations_threshold(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.85)
    create_user(app_db, 'staff@example.com')
    staff = session(app_db, 'staff@example.com')

    assert submit(staff) == 'APPROVED'
    execute(app_db, 'UPDATE Organizations SET auto_approve_above = 0.9 WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,))
    assert submit(staff) == 'PENDING_REVIEW'

    assert [r['final_decision'] for r in query(app_db, 'SELECT final_decision FROM Requests ORDER BY id')] == ['APPROVED', 'ESCALATED_MANUAL_REVIEW']
    submitted = query(app_db, "SELECT details FROM AuditEvents WHERE action = 'request.submitted' ORDER BY id")
    assert [json.loads(e['details'])['thresholds']['auto_approve_above'] for e in submitted] == [0.8, 0.9]


def test_a_stored_threshold_below_the_default_is_never_used(app_db, monkeypatch):
    use_model_score(app_db, monkeypatch, 0.7)
    execute(app_db, 'UPDATE Organizations SET auto_approve_above = 0.5 WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,))
    create_user(app_db, 'staff@example.com')

    assert submit(session(app_db, 'staff@example.com')) == 'PENDING_REVIEW'
