import pytest

from conftest import (
    PASSWORD, PUBLIC_ENDPOINTS, SUPER_ADMIN, add_request, create_organization, create_platform_owner,
    create_user, execute, login, query,
)

def platform_routes(app):
    return [
        (method, rule.rule)
        for rule in app.url_map.iter_rules() if rule.rule.startswith('/api/platform/')
        for method in rule.methods - {'HEAD', 'OPTIONS'}
    ]


def session(app_db, email):
    client = app_db.app.test_client()
    login(client, email)
    return client


@pytest.fixture
def owner(app_db):
    return session(app_db, create_platform_owner(app_db))


@pytest.fixture
def sent(app_db, monkeypatch):
    emails = []
    monkeypatch.setattr(app_db.email_service, 'sendOrganizationCreatedEmail', lambda *args: emails.append(args))
    return emails


def test_platform_owner_is_created_from_settings_outside_any_organization(tmp_path, monkeypatch):
    import main

    monkeypatch.setenv('PLATFORM_OWNER_EMAIL', 'founder@neuzem.test')
    monkeypatch.setenv('PLATFORM_OWNER_PASSWORD', PASSWORD)
    monkeypatch.setattr(main, 'DB_FILE', str(tmp_path / 'platform.db'))
    main.setup_database()
    main.setup_database()  # A restart must not move the Platform Owner into the default organization.

    assert query(main, 'SELECT role, status, organization_id FROM Users WHERE email = ?', ('founder@neuzem.test',)) == [
        {'role': 'PlatformOwner', 'status': 'Active', 'organization_id': None}
    ]


def test_platform_owner_setting_never_takes_over_a_company_account(tmp_path, monkeypatch):
    import main

    monkeypatch.setenv('PLATFORM_OWNER_EMAIL', SUPER_ADMIN['email'])
    monkeypatch.setenv('PLATFORM_OWNER_PASSWORD', PASSWORD)
    monkeypatch.setattr(main, 'DB_FILE', str(tmp_path / 'takeover.db'))
    main.setup_database()

    assert query(main, 'SELECT role FROM Users WHERE email = ?', (SUPER_ADMIN['email'],)) == [{'role': 'SuperAdmin'}]
    assert query(main, "SELECT id FROM Users WHERE role = 'PlatformOwner'") == []


def test_platform_owner_logs_in_to_the_platform_page(app_db):
    client = app_db.app.test_client()
    assert login(client, create_platform_owner(app_db)).get_json()['redirect'] == 'platform.html'
    assert client.get('/api/platform/organizations').status_code == 200


def test_platform_owner_cannot_reach_any_organizations_data(app_db, owner):
    org_id = create_organization(app_db, 'Acme Pvt Ltd')
    create_user(app_db, 'staff@acme.test', organization_id=org_id)
    add_request(app_db, org_id, 'staff@acme.test')

    checked = 0
    for rule in app_db.app.url_map.iter_rules():
        if not rule.rule.startswith('/api/') or rule.rule.startswith('/api/platform/') or rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            status = owner.open(rule.rule, method=method, json={}).status_code
            assert status in (401, 403), f'{method} {rule.rule} answered {status} for a Platform Owner'
            checked += 1
    assert checked >= 15


@pytest.mark.parametrize('role', ['SuperAdmin', 'Admin', 'User'])
def test_organization_accounts_cannot_use_the_platform(app_db, role):
    if role == 'SuperAdmin':
        email = SUPER_ADMIN['email']
    else:
        email = f'{role.lower()}@example.com'
        create_user(app_db, email, role=role)
    client = session(app_db, email)

    routes = platform_routes(app_db.app)
    assert len(routes) >= 8
    for method, path in routes:
        assert client.open(path, method=method, json={'id': 1, 'status': 'Paused', 'version_id': None}).status_code == 403
    assert query(app_db, 'SELECT status FROM Organizations') == [{'status': 'Active'}]


def test_creating_an_organization_invites_its_super_admin(app_db, owner, sent):
    response = owner.post('/api/platform/create_organization', json={
        'name': '  Acme   Pvt Ltd ', 'super_admin_email': 'boss@acme.test', 'allow_training_data': True,
    })
    assert response.status_code == 201, response.get_json()
    assert 'setup_token' not in response.get_data(as_text=True)
    org_id = response.get_json()['organization_id']

    assert query(app_db, 'SELECT name, status, allow_training_data, is_default, created_by FROM Organizations WHERE id = ?', (org_id,)) == [
        {'name': 'Acme Pvt Ltd', 'status': 'Active', 'allow_training_data': 1, 'is_default': 0, 'created_by': 'founder@neuzem.test'}
    ]
    assert query(app_db, 'SELECT role, status, organization_id FROM Users WHERE email = ?', ('boss@acme.test',)) == [
        {'role': 'SuperAdmin', 'status': 'Approved_Awaiting_Password', 'organization_id': org_id}
    ]

    # Only the Super Admin's email carries the setup link, and it activates their account in the new organization.
    [(to, name, setup_link)] = sent
    assert (to, name) == ('boss@acme.test', 'Acme Pvt Ltd')
    boss = app_db.app.test_client()
    setup = boss.post('/api/auth/setup_password', json={'token': setup_link.split('setup_token=')[1], 'password': PASSWORD})
    assert setup.status_code == 200
    assert setup.get_json()['redirect'] == 'admin.html'
    assert boss.get('/api/auth/organization').get_json()['name'] == 'Acme Pvt Ltd'


def test_new_organizations_do_not_share_training_data_unless_asked(app_db, owner, sent):
    response = owner.post('/api/platform/create_organization', json={'name': 'Quiet Co', 'super_admin_email': 'boss@quiet.test'})
    assert response.status_code == 201
    assert query(app_db, "SELECT allow_training_data FROM Organizations WHERE name = 'Quiet Co'") == [{'allow_training_data': 0}]


@pytest.mark.parametrize('body, status', [
    ({'name': 'A', 'super_admin_email': 'boss@new.test'}, 400),
    ({'name': 'New Co', 'super_admin_email': 'not-an-email'}, 400),
    ({'name': 'New Co', 'super_admin_email': 'boss@new.test', 'allow_training_data': 'yes'}, 400),
    ({'name': 'default   ORGANIZATION', 'super_admin_email': 'boss@new.test'}, 409),
    ({'name': 'New Co', 'super_admin_email': SUPER_ADMIN['email']}, 409),
])
def test_invalid_organizations_are_not_created(app_db, owner, sent, body, status):
    before = (query(app_db, 'SELECT * FROM Organizations'), query(app_db, 'SELECT * FROM Users'))
    assert owner.post('/api/platform/create_organization', json=body).status_code == status
    assert (query(app_db, 'SELECT * FROM Organizations'), query(app_db, 'SELECT * FROM Users')) == before
    assert sent == []


def test_organization_list_shows_settings_and_size_but_no_records(app_db, owner):
    org_id = create_organization(app_db, 'Acme Pvt Ltd')
    create_user(app_db, 'boss@acme.test', role='SuperAdmin', organization_id=org_id)
    create_user(app_db, 'staff@acme.test', organization_id=org_id)
    create_user(app_db, 'newcomer@acme.test', organization_id=org_id, status='Pending')
    add_request(app_db, org_id, 'staff@acme.test')

    organizations = owner.get('/api/platform/organizations').get_json()['organizations']
    assert [o['name'] for o in organizations] == ['Default Organization', 'Acme Pvt Ltd']

    acme = organizations[1]
    assert set(acme) == {
        'id', 'name', 'status', 'allow_training_data', 'is_default', 'created_by', 'created_at',
        'active_users', 'requests', 'super_admins',
    }
    assert (acme['active_users'], acme['requests'], acme['is_default'], acme['allow_training_data']) == (2, 1, False, False)
    assert acme['super_admins'] == [{'email': 'boss@acme.test', 'status': 'Active'}]


def test_pausing_and_resuming_an_organization(app_db, owner):
    org_id = create_organization(app_db, 'Acme Pvt Ltd')
    create_user(app_db, 'boss@acme.test', role='SuperAdmin', organization_id=org_id)
    boss = session(app_db, 'boss@acme.test')

    assert owner.post('/api/platform/update_organization', json={'id': org_id, 'status': 'Paused'}).status_code == 200
    assert boss.get('/api/auth/all_requests').status_code == 403
    paused_login = app_db.app.test_client().post('/api/auth/login', json={'email': 'boss@acme.test', 'password': PASSWORD})
    assert paused_login.status_code == 403

    assert owner.post('/api/platform/update_organization', json={'id': org_id, 'status': 'Active'}).status_code == 200
    assert boss.get('/api/auth/all_requests').status_code == 200


def test_training_data_consent_can_be_changed(app_db, owner):
    org_id = create_organization(app_db, 'Acme Pvt Ltd')
    for allow, stored in ((True, 1), (False, 0)):
        assert owner.post('/api/platform/update_organization', json={'id': org_id, 'allow_training_data': allow}).status_code == 200
        assert query(app_db, 'SELECT allow_training_data FROM Organizations WHERE id = ?', (org_id,)) == [{'allow_training_data': stored}]


def test_default_organization_cannot_be_paused(app_db, owner):
    response = owner.post('/api/platform/update_organization', json={'id': app_db.DEFAULT_ORGANIZATION_ID, 'status': 'Paused'})
    assert response.status_code == 409
    assert query(app_db, 'SELECT status FROM Organizations WHERE id = ?', (app_db.DEFAULT_ORGANIZATION_ID,)) == [{'status': 'Active'}]


@pytest.mark.parametrize('body, status', [
    ({}, 400),
    ({'id': '1', 'status': 'Paused'}, 400),
    ({'id': True, 'status': 'Paused'}, 400),
    ({'id': 1}, 400),
    ({'id': 1, 'status': 'Deleted'}, 400),
    ({'id': 1, 'allow_training_data': 'no'}, 400),
    ({'id': 9999, 'status': 'Paused'}, 404),
])
def test_invalid_organization_updates_are_refused(app_db, owner, body, status):
    before = query(app_db, 'SELECT * FROM Organizations')
    assert owner.post('/api/platform/update_organization', json=body).status_code == status
    assert query(app_db, 'SELECT * FROM Organizations') == before
