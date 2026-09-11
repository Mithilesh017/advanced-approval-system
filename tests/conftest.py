import os
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import pytest
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = 'correct-horse-battery'

# Set TEST_DATABASE_URL to also run every database test against PostgreSQL. Each test erases that database,
# so its name must contain "test" to make pointing it at a real database impossible by accident.
TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL', '')
if TEST_DATABASE_URL and 'test' not in urlparse(TEST_DATABASE_URL).path.lower():
    raise RuntimeError('TEST_DATABASE_URL must name a database containing "test", because the tests erase it.')
BACKENDS = ['sqlite', 'postgresql'] if TEST_DATABASE_URL else ['sqlite']

# Tests must never touch a real database, send real email or require HTTPS cookies,
# whatever the local .env contains. load_dotenv() does not override variables set here.
os.environ.update({
    'DATABASE_URL': '',
    'SQLITE_PATH': os.path.join(tempfile.mkdtemp(prefix='aams-tests-'), 'import.db'),
    'SENDGRID_API_KEY': '',
    'SMTP_USERNAME': '',
    'SMTP_PASSWORD': '',
    'RENDER': '',
    'APP_BASE_URL': '',
    'ENVIRONMENT': '',
    'JWT_SECRET_KEY': 'test-only-secret-key-with-enough-length-for-hs256',
    'INITIAL_SUPER_ADMIN_EMAIL': 'owner@example.com',
    'INITIAL_SUPER_ADMIN_PASSWORD': PASSWORD,
})

# main.py loads the bundled model and serves pages by relative path.
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import main  # noqa: E402

main.limiter.enabled = False

SUPER_ADMIN = {'email': 'owner@example.com', 'password': PASSWORD}

PUBLIC_ENDPOINTS = {
    'login', 'logout', 'request_access', 'setup_password', 'join_info',
    'request_password_reset', 'reset_password', 'reject_reset',
}


def erase_postgres_database(url):
    import psycopg2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute('DROP SCHEMA IF EXISTS public CASCADE')
            cursor.execute('CREATE SCHEMA public')
    finally:
        conn.close()


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path, monkeypatch):
    """Points the app at an empty database, once per configured backend. Returns the backend name."""
    if request.param == 'postgresql':
        erase_postgres_database(TEST_DATABASE_URL)
        monkeypatch.setenv('DATABASE_URL', TEST_DATABASE_URL)
    else:
        monkeypatch.setattr(main, 'DB_FILE', str(tmp_path / 'test.db'))
    return request.param


@pytest.fixture
def app_db(backend):
    """A fresh, fully migrated database for each test."""
    main.setup_database()
    return main


@pytest.fixture
def client(app_db):
    return app_db.app.test_client()


def query(module, sql, params=()):
    conn = module.get_db_connection()
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(module, sql, params=()):
    conn = module.get_db_connection()
    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall() if 'RETURNING' in sql else None
        conn.commit()
        return rows
    finally:
        conn.close()


def create_organization(module, name, status='Active', approval_mode='automatic'):
    rows = execute(
        module, 'INSERT INTO Organizations (name, join_code, status, approval_mode) VALUES (?, ?, ?, ?) RETURNING id',
        (name, secrets.token_urlsafe(9), status, approval_mode)
    )
    return rows[0][0]


def create_platform_owner(module, email='founder@neuzem.test'):
    execute(
        module, 'INSERT INTO Users (email, password_hash, role, status) VALUES (?, ?, ?, ?)',
        (email, generate_password_hash(PASSWORD), 'PlatformOwner', 'Active')
    )
    return email


def join_code_of(module, organization_id):
    [row] = query(module, 'SELECT join_code FROM Organizations WHERE id = ?', (organization_id,))
    return row['join_code']


def create_user(module, email, role='User', organization_id=None, status='Active'):
    execute(
        module, 'INSERT INTO Users (email, password_hash, role, status, organization_id) VALUES (?, ?, ?, ?, ?)',
        (email, generate_password_hash(PASSWORD), role, status, organization_id or module.DEFAULT_ORGANIZATION_ID)
    )


def add_request(module, organization_id, submitted_by, final_decision='ESCALATED_MANUAL_REVIEW', department='Engineering'):
    rows = execute(
        module,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, final_decision, submitted_by, organization_id) '
        "VALUES ('Junior Developer', ?, 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.5, ?, ?, ?) RETURNING id",
        (department, final_decision, submitted_by, organization_id)
    )
    return rows[0][0]


def login(client, email, password=PASSWORD):
    response = client.post('/api/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 200, response.get_json()
    return response
