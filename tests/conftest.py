import os
import secrets
import sys
import tempfile
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = 'correct-horse-battery'

# Tests must never touch a real database, send real email or require HTTPS cookies,
# whatever the local .env contains. load_dotenv() does not override variables set here.
os.environ.update({
    'DATABASE_URL': '',
    'SQLITE_PATH': os.path.join(tempfile.mkdtemp(prefix='aams-tests-'), 'import.db'),
    'SENDGRID_API_KEY': '',
    'SMTP_USERNAME': '',
    'SMTP_PASSWORD': '',
    'RENDER': '',
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


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    """A fresh, fully migrated SQLite database for each test."""
    monkeypatch.setattr(main, 'DB_FILE', str(tmp_path / 'test.db'))
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


def create_organization(module, name, status='Active'):
    rows = execute(
        module, 'INSERT INTO Organizations (name, join_code, status) VALUES (?, ?, ?) RETURNING id',
        (name, secrets.token_urlsafe(9), status)
    )
    return rows[0][0]


def create_user(module, email, role='User', organization_id=None, status='Active'):
    execute(
        module, 'INSERT INTO Users (email, password_hash, role, status, organization_id) VALUES (?, ?, ?, ?, ?)',
        (email, generate_password_hash(PASSWORD), role, status, organization_id or module.DEFAULT_ORGANIZATION_ID)
    )


def add_request(module, organization_id, submitted_by, final_decision='ESCALATED_MANUAL_REVIEW'):
    rows = execute(
        module,
        'INSERT INTO Requests (role, department, request_type, destination, amount, currency, normalized_amount, '
        'xgb_score, final_decision, submitted_by, organization_id) '
        "VALUES ('Junior Developer', 'Engineering', 'Hotel Booking', 'Mumbai', 5000, 'INR', 5000, 0.5, ?, ?, ?) RETURNING id",
        (final_decision, submitted_by, organization_id)
    )
    return rows[0][0]


def login(client, email, password=PASSWORD):
    response = client.post('/api/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 200, response.get_json()
    return response
