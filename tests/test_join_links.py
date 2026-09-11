import pytest

from conftest import PASSWORD, create_organization, create_user, execute, join_code_of, login, query

ACCOUNT_EMAILS = (
    'sendUserRegistrationNotification', 'sendAdminRegistrationNotification', 'sendUserApprovedEmail',
    'sendAdminApprovedEmail', 'sendUserRejectedEmail', 'sendAdminRejectedEmail', 'sendWelcomeEmail',
)


@pytest.fixture
def acme(app_db):
    org_id = create_organization(app_db, 'Acme Pvt Ltd')
    create_user(app_db, 'super@acme.test', role='SuperAdmin', organization_id=org_id)
    create_user(app_db, 'admin@acme.test', role='Admin', organization_id=org_id)
    create_user(app_db, 'staff@acme.test', organization_id=org_id)
    return {'id': org_id, 'code': join_code_of(app_db, org_id)}


@pytest.fixture
def sent(app_db, monkeypatch):
    """Records each account email the app asks to send, as (function name, arguments)."""
    emails = []
    for name in ACCOUNT_EMAILS:
        monkeypatch.setattr(app_db.email_service, name, lambda *args, _name=name: emails.append((_name, args)))
    return emails


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


def test_join_link_opens_the_access_request_page(client, acme):
    response = client.get(f"/join/{acme['code']}")
    assert response.status_code == 302
    assert response.headers['Location'].endswith(f"/index.html?join={acme['code']}")


def test_join_info_names_only_active_organizations(client, app_db, acme):
    response = client.get('/api/auth/join_info', query_string={'code': acme['code']})
    assert response.status_code == 200
    assert response.get_json() == {'organization_name': 'Acme Pvt Ltd'}

    assert client.get('/api/auth/join_info', query_string={'code': 'not-a-real-code'}).status_code == 404
    assert client.get('/api/auth/join_info').status_code == 404

    execute(app_db, "UPDATE Organizations SET status = 'Paused' WHERE id = ?", (acme['id'],))
    assert client.get('/api/auth/join_info', query_string={'code': acme['code']}).status_code == 404


@pytest.mark.parametrize('join_code', [None, '', 'not-a-real-code', 'paused'])
def test_access_request_needs_an_active_join_link(client, app_db, acme, sent, join_code):
    body = {'email': 'hire@example.com', 'role': 'User'}
    if join_code == 'paused':
        execute(app_db, "UPDATE Organizations SET status = 'Paused' WHERE id = ?", (acme['id'],))
        body['join_code'] = acme['code']
    elif join_code is not None:
        body['join_code'] = join_code

    assert client.post('/api/auth/request_access', json=body).status_code == 400
    assert query(app_db, 'SELECT id FROM Users WHERE email = ?', ('hire@example.com',)) == []
    assert sent == []


def test_access_request_joins_the_link_organization_and_notifies_its_admins(client, app_db, acme, sent):
    create_user(app_db, 'admin@default.test', role='Admin')
    response = client.post('/api/auth/request_access', json={'email': 'hire@example.com', 'role': 'User', 'join_code': acme['code']})
    assert response.status_code == 200
    assert 'Acme Pvt Ltd' in response.get_json()['message']

    assert query(app_db, 'SELECT organization_id, status FROM Users WHERE email = ?', ('hire@example.com',)) == [
        {'organization_id': acme['id'], 'status': 'Pending'}
    ]
    assert sorted(sent) == [
        ('sendUserRegistrationNotification', ('admin@acme.test', 'hire@example.com', 'User', 'Acme Pvt Ltd')),
        ('sendUserRegistrationNotification', ('super@acme.test', 'hire@example.com', 'User', 'Acme Pvt Ltd')),
    ]


def test_account_emails_name_the_organization(app_db, acme, sent):
    create_user(app_db, 'hire@example.com', organization_id=acme['id'], status='Pending')
    create_user(app_db, 'declined@example.com', organization_id=acme['id'], status='Pending')
    admin = session(app_db, 'super@acme.test')
    assert admin.post('/api/auth/approve_user', json={'email': 'hire@example.com'}).status_code == 200
    assert admin.post('/api/auth/reject_user', json={'email': 'declined@example.com'}).status_code == 200

    conn = app_db.get_db_connection()
    token = app_db.issue_token(conn, 'hire@example.com', app_db.SETUP_TOKEN_TTL)
    conn.close()
    assert app_db.app.test_client().post('/api/auth/setup_password', json={'token': token, 'password': PASSWORD}).status_code == 200

    by_name = {name: args for name, args in sent}
    assert by_name['sendUserApprovedEmail'][0] == 'hire@example.com'
    assert by_name['sendUserApprovedEmail'][-1] == 'Acme Pvt Ltd'
    assert by_name['sendUserRejectedEmail'] == ('declined@example.com', 'declined', 'Acme Pvt Ltd')
    assert by_name['sendWelcomeEmail'] == ('hire@example.com', 'hire', 'Acme Pvt Ltd')


def test_admins_get_their_organizations_join_link(app_db, acme):
    body = session(app_db, 'admin@acme.test').get('/api/auth/organization').get_json()
    assert body['name'] == 'Acme Pvt Ltd'
    assert body['join_link'].endswith(f"/join/{acme['code']}")


def test_employees_see_their_organization_but_not_the_join_link(app_db, acme):
    body = session(app_db, 'staff@acme.test').get('/api/auth/organization').get_json()
    assert body == {'name': 'Acme Pvt Ltd', 'join_link': None}


def test_account_emails_escape_the_organization_name(app_db, monkeypatch):
    captured = []
    monkeypatch.setattr(app_db.email_service, '_send_email_async', lambda to, subject, html: captured.append(html))
    app_db.email_service.sendWelcomeEmail('someone@example.com', 'someone', '<script>alert(1)</script>')
    assert '<script>' not in captured[0]
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in captured[0]
