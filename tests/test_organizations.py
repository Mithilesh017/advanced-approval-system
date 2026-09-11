from conftest import REQUEST_DETAILS, SUPER_ADMIN, join_code_of, query

# The tables as they existed before organizations, in each backend's original form.
LEGACY_SCHEMA = {
    'sqlite': [
        '''CREATE TABLE Users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, password_hash TEXT,
            role TEXT NOT NULL, status TEXT NOT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE Requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, department TEXT, request_type TEXT,
            destination TEXT, amount REAL, currency TEXT, normalized_amount REAL, xgb_score REAL,
            iso_score REAL, svm_score REAL, risk_score REAL, final_decision TEXT, submitted_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )''',
    ],
    'postgresql': [
        '''CREATE TABLE Users (
            id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT, name TEXT, emp_id TEXT,
            reset_token TEXT, reset_expiry TIMESTAMP, role TEXT NOT NULL, status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE Requests (
            id SERIAL PRIMARY KEY, role TEXT, department TEXT, request_type TEXT, destination TEXT,
            amount NUMERIC, currency TEXT, normalized_amount NUMERIC, xgb_score NUMERIC, iso_score NUMERIC,
            svm_score NUMERIC, risk_score NUMERIC, final_decision TEXT, submitted_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
    ],
}
LEGACY_ROWS = [
    "INSERT INTO Users (email, role, status) VALUES ('admin@example.com', 'Admin', 'Active')",
    "INSERT INTO Users (email, role, status) VALUES ('employee@example.com', 'User', 'Active')",
    "INSERT INTO Requests (role, final_decision, submitted_by) VALUES ('Junior Developer', 'APPROVED', 'employee@example.com')",
]


def test_fresh_database_has_one_default_organization(app_db):
    [org] = query(app_db, 'SELECT * FROM Organizations')
    assert org['id'] == app_db.DEFAULT_ORGANIZATION_ID
    assert org['is_default'] == 1
    assert org['allow_training_data'] == 1
    assert org['status'] == 'Active'
    assert len(org['join_code']) >= 12


def test_setup_can_run_repeatedly_without_duplicates(app_db):
    first_id = app_db.DEFAULT_ORGANIZATION_ID
    app_db.setup_database()
    app_db.setup_database()
    assert query(app_db, 'SELECT id FROM Organizations') == [{'id': first_id}]
    assert app_db.DEFAULT_ORGANIZATION_ID == first_id
    assert len(query(app_db, 'SELECT id FROM Users')) == 1


def test_existing_data_moves_into_default_organization(backend):
    import main

    conn = main.get_db_connection()
    for statement in LEGACY_SCHEMA[backend] + LEGACY_ROWS:
        conn.execute(statement)
    conn.commit()
    conn.close()

    main.setup_database()

    org_id = main.DEFAULT_ORGANIZATION_ID
    users = query(main, 'SELECT email, role, organization_id FROM Users ORDER BY id')
    assert [u['email'] for u in users] == ['admin@example.com', 'employee@example.com', SUPER_ADMIN['email']]
    assert {u['organization_id'] for u in users} == {org_id}
    assert query(main, 'SELECT organization_id FROM Requests') == [{'organization_id': org_id}]


def test_super_admin_from_settings_joins_default_organization(app_db):
    [admin] = query(app_db, 'SELECT role, status, organization_id FROM Users WHERE email = ?', (SUPER_ADMIN['email'],))
    assert admin == {'role': 'SuperAdmin', 'status': 'Active', 'organization_id': app_db.DEFAULT_ORGANIZATION_ID}


def test_no_super_admin_is_created_without_a_configured_email(backend, monkeypatch):
    import main

    monkeypatch.delenv('INITIAL_SUPER_ADMIN_EMAIL')
    main.setup_database()
    assert query(main, 'SELECT id FROM Users') == []


def test_access_request_with_the_default_join_link_joins_default_organization(client, app_db):
    response = client.post('/api/auth/request_access', json={
        'email': 'new.hire@example.com', 'role': 'User', 'join_code': join_code_of(app_db, app_db.DEFAULT_ORGANIZATION_ID),
    })
    assert response.status_code == 200
    [user] = query(app_db, 'SELECT organization_id FROM Users WHERE email = ?', ('new.hire@example.com',))
    assert user['organization_id'] == app_db.DEFAULT_ORGANIZATION_ID


def test_submitted_request_records_the_submitters_organization(client, app_db):
    assert client.post('/api/auth/login', json=SUPER_ADMIN).status_code == 200
    response = client.post('/api/predict', json={
        'Role': 'Junior Developer', 'Department': 'Engineering', 'Request_Type': 'Hotel Booking',
        'Destination': 'Mumbai', 'Amount': 5000, 'Currency': 'INR', **REQUEST_DETAILS,
    })
    assert response.status_code == 200, response.get_json()
    assert query(app_db, 'SELECT submitted_by, organization_id FROM Requests') == [
        {'submitted_by': SUPER_ADMIN['email'], 'organization_id': app_db.DEFAULT_ORGANIZATION_ID}
    ]
