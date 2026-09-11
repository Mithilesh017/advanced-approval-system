import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

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
    'INITIAL_SUPER_ADMIN_PASSWORD': 'correct-horse-battery',
})

# main.py loads the bundled model and serves pages by relative path.
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import main  # noqa: E402

main.limiter.enabled = False

SUPER_ADMIN = {'email': 'owner@example.com', 'password': 'correct-horse-battery'}


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
