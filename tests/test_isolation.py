"""Organization A must never see or change Organization B's people or requests."""
import pytest

from conftest import SUPER_ADMIN, add_request, create_organization, create_user, join_code_of, login, query

VALID_REQUEST = {
    'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
    'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR',
}


@pytest.fixture
def orgs(app_db):
    ids = {}
    for key in ('a', 'b'):
        org_id = create_organization(app_db, f'Org {key.upper()}')
        create_user(app_db, f'super@{key}.test', role='SuperAdmin', organization_id=org_id)
        create_user(app_db, f'admin@{key}.test', role='Admin', organization_id=org_id)
        create_user(app_db, f'staff@{key}.test', organization_id=org_id)
        create_user(app_db, f'newcomer@{key}.test', organization_id=org_id, status='Pending')
        ids[key] = {
            'org': org_id,
            'pending': add_request(app_db, org_id, f'staff@{key}.test', 'ESCALATED_MANUAL_REVIEW'),
            'approved': add_request(app_db, org_id, f'staff@{key}.test', 'APPROVED'),
        }
    return ids


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def test_request_lists_only_show_the_admins_organization(app_db, orgs):
    admin_a = session(app_db, 'admin@a.test')
    all_ids = {r['id'] for r in admin_a.get('/api/auth/all_requests').get_json()}
    pending_ids = {r['id'] for r in admin_a.get('/api/auth/pending_approval_requests').get_json()}
    assert all_ids == {orgs['a']['pending'], orgs['a']['approved']}
    assert pending_ids == {orgs['a']['pending']}


def test_user_lists_only_show_the_admins_organization(app_db, orgs):
    super_a = session(app_db, 'super@a.test')
    emails = {u['email'] for u in super_a.get('/api/auth/users').get_json()}
    pending = {u['email'] for u in super_a.get('/api/auth/pending_users').get_json()}
    assert emails == {'super@a.test', 'admin@a.test', 'staff@a.test', 'newcomer@a.test'}
    assert pending == {'newcomer@a.test'}


def test_employee_history_only_shows_their_own_requests(app_db, orgs):
    history = session(app_db, 'staff@a.test').get('/api/auth/my_requests').get_json()
    assert {r['id'] for r in history} == {orgs['a']['pending'], orgs['a']['approved']}


@pytest.mark.parametrize('path', ['/api/auth/approve_request', '/api/auth/reject_request', '/api/auth/reopen_request'])
def test_cannot_decide_another_organizations_requests(app_db, orgs, path):
    super_a = session(app_db, 'super@a.test')
    before = query(app_db, 'SELECT * FROM Requests ORDER BY id')
    for key in ('pending', 'approved'):
        assert super_a.post(path, json={'id': orgs['b'][key]}).status_code == 404
    assert query(app_db, 'SELECT * FROM Requests ORDER BY id') == before


@pytest.mark.parametrize('path', ['/api/auth/approve_user', '/api/auth/reject_user', '/api/auth/delete_user'])
def test_cannot_manage_another_organizations_users(app_db, orgs, path):
    super_a = session(app_db, 'super@a.test')
    before = query(app_db, 'SELECT * FROM Users ORDER BY id')
    for email in ('newcomer@b.test', 'staff@b.test', 'admin@b.test', 'super@b.test'):
        assert super_a.post(path, json={'email': email}).status_code == 404
    assert query(app_db, 'SELECT * FROM Users ORDER BY id') == before


def test_can_still_manage_their_own_organization(app_db, orgs):
    super_a = session(app_db, 'super@a.test')
    assert super_a.post('/api/auth/approve_request', json={'id': orgs['a']['pending']}).status_code == 200
    assert super_a.post('/api/auth/approve_user', json={'email': 'newcomer@a.test'}).status_code == 200
    assert super_a.post('/api/auth/delete_user', json={'email': 'staff@a.test'}).status_code == 200


def test_new_request_is_saved_to_the_submitters_organization(app_db, orgs):
    response = session(app_db, 'staff@b.test').post('/api/predict', json=VALID_REQUEST)
    assert response.status_code == 200, response.get_json()
    [saved] = query(app_db, 'SELECT organization_id FROM Requests WHERE id = (SELECT MAX(id) FROM Requests)')
    assert saved['organization_id'] == orgs['b']['org']


def test_access_requests_only_notify_that_organizations_admins(app_db, orgs, monkeypatch):
    notified = []
    monkeypatch.setattr(app_db.email_service, 'sendUserRegistrationNotification', lambda to, *args: notified.append(to))
    monkeypatch.setattr(app_db.email_service, 'sendAdminRegistrationNotification', lambda to, *args: notified.append(to))

    client = app_db.app.test_client()
    code = join_code_of(app_db, orgs['a']['org'])
    assert client.post('/api/auth/request_access', json={'email': 'hire@example.com', 'role': 'User', 'join_code': code}).status_code == 200
    assert client.post('/api/auth/request_access', json={'email': 'lead@example.com', 'role': 'Admin', 'join_code': code}).status_code == 200
    # Employee requests reach Org A's admins and Super Admins; administrator requests only its Super Admins.
    assert sorted(notified) == ['admin@a.test', 'super@a.test', 'super@a.test']


@pytest.mark.parametrize('email', [SUPER_ADMIN['email'], 'super@a.test'])
def test_no_organization_account_can_manage_the_shared_model(app_db, orgs, email):
    client = session(app_db, email)
    for path in ('/api/platform/model/info', '/api/platform/model/versions', '/api/platform/model/jobs/latest'):
        assert client.get(path).status_code == 403
    assert client.post('/api/platform/model/retrain').status_code == 403
    assert client.post('/api/platform/model/activate', json={'version_id': None}).status_code == 403
