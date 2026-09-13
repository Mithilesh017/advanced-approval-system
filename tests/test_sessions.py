from flask_jwt_extended import create_access_token, decode_token

from conftest import PASSWORD, PUBLIC_ENDPOINTS, SUPER_ADMIN, create_organization, create_user, execute, login


def session_claims(client, module):
    cookie = client.get_cookie(module.app.config['JWT_ACCESS_COOKIE_NAME'])
    with module.app.app_context():
        return decode_token(cookie.value)


def test_every_non_public_api_route_requires_login(client, app_db):
    checked = 0
    for rule in app_db.app.url_map.iter_rules():
        if not rule.rule.startswith('/api/') or rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            response = client.open(rule.rule, method=method, json={})
            assert response.status_code == 401, f'{method} {rule.rule} answered {response.status_code} without a login'
            checked += 1
    assert checked >= 20


def test_login_session_carries_the_organization(client, app_db):
    login(client, SUPER_ADMIN['email'])
    claims = session_claims(client, app_db)
    assert claims['sub'] == SUPER_ADMIN['email']
    assert claims['organization_id'] == app_db.DEFAULT_ORGANIZATION_ID
    assert 'role' not in claims


def test_account_setup_session_carries_the_organization(client, app_db):
    org_id = create_organization(app_db, 'Acme')
    create_user(app_db, 'new@acme.test', organization_id=org_id, status='Approved_Awaiting_Password')
    conn = app_db.get_db_connection()
    token = app_db.issue_token(conn, 'new@acme.test', app_db.SETUP_TOKEN_TTL)
    conn.close()

    response = client.post('/api/auth/setup_password', json={'token': token, 'password': PASSWORD})
    assert response.status_code == 200, response.get_json()
    assert session_claims(client, app_db)['organization_id'] == org_id


def test_removed_account_loses_access_immediately(client, app_db):
    create_user(app_db, 'employee@example.com')
    login(client, 'employee@example.com')
    assert client.get('/api/auth/my_requests').status_code == 200

    execute(app_db, 'DELETE FROM Users WHERE email = ?', ('employee@example.com',))
    assert client.get('/api/auth/my_requests').status_code == 401


def test_role_change_takes_effect_immediately(client, app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    login(client, 'manager@example.com')
    assert client.get('/api/auth/all_requests').status_code == 200

    execute(app_db, "UPDATE Users SET role = 'User' WHERE email = ?", ('manager@example.com',))
    refused = client.get('/api/auth/all_requests')
    assert refused.status_code == 403
    assert 'X-Organization-Paused' not in refused.headers  # An ordinary refusal must not sign the person out.


def test_session_for_a_different_organization_is_rejected(client, app_db):
    create_user(app_db, 'mover@example.com')
    login(client, 'mover@example.com')

    other_org = create_organization(app_db, 'Other Co')
    execute(app_db, 'UPDATE Users SET organization_id = ? WHERE email = ?', (other_org, 'mover@example.com'))
    assert client.get('/api/auth/my_requests').status_code == 401


def test_session_from_before_organizations_is_rejected(client, app_db):
    create_user(app_db, 'old@example.com')
    with app_db.app.app_context():
        old_token = create_access_token(identity='old@example.com', additional_claims={'role': 'User'})
    client.set_cookie(app_db.app.config['JWT_ACCESS_COOKIE_NAME'], old_token)
    assert client.get('/api/auth/my_requests').status_code == 401


def test_paused_organization_blocks_open_sessions_and_new_logins(app_db):
    org_id = create_organization(app_db, 'Paused Co')
    create_user(app_db, 'staff@paused.test', organization_id=org_id)
    open_session = app_db.app.test_client()
    login(open_session, 'staff@paused.test')

    execute(app_db, "UPDATE Organizations SET status = 'Paused' WHERE id = ?", (org_id,))

    response = open_session.get('/api/auth/my_requests')
    assert response.status_code == 403
    assert 'paused' in response.get_json()['error']
    # The portals read this header to show the paused message and sign the person out.
    assert response.headers.get('X-Organization-Paused') == '1'

    new_login = app_db.app.test_client().post('/api/auth/login', json={'email': 'staff@paused.test', 'password': PASSWORD})
    assert new_login.status_code == 403
    assert new_login.get_json()['status'] == 'PAUSED'

    # A wrong password must not reveal that the organization is paused.
    wrong_password = app_db.app.test_client().post('/api/auth/login', json={'email': 'staff@paused.test', 'password': 'wrong-password'})
    assert wrong_password.status_code == 401


def test_employee_cannot_use_admin_features(client, app_db):
    create_user(app_db, 'employee@example.com')
    login(client, 'employee@example.com')
    assert client.get('/api/auth/all_requests').status_code == 403


def test_admin_cannot_use_super_admin_features(client, app_db):
    create_user(app_db, 'manager@example.com', role='Admin')
    create_user(app_db, 'candidate@example.com', role='Admin', status='Pending')
    login(client, 'manager@example.com')
    assert client.post('/api/auth/approve_user', json={'email': 'candidate@example.com'}).status_code == 403


def test_super_admin_can_use_admin_features(client, app_db):
    login(client, SUPER_ADMIN['email'])
    assert client.get('/api/auth/all_requests').status_code == 200
