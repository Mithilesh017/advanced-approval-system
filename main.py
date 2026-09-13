from flask import Flask, g, redirect, request, jsonify, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from decimal import Decimal
import hashlib
import io
import json
import math
import re
import sqlite3
import os
import tempfile
import threading
import time
import email_service
import model_pipeline
import secrets
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
import joblib
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, set_access_cookies, unset_jwt_cookies, get_jwt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
import zipfile
from urllib.parse import quote
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)

# Render terminates TLS at a proxy; trust exactly one hop so rate limits see the real client IP.
if os.getenv('RENDER'):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# Security configuration
if not os.getenv('JWT_SECRET_KEY'):
    print("[WARNING] JWT_SECRET_KEY is not set. A random key is used, so sessions reset on restart and break across multiple workers.")
app.config['JWT_SECRET_KEY'] = os.getenv('JWT_SECRET_KEY', secrets.token_hex(32))
app.config['JWT_TOKEN_LOCATION'] = ['cookies']
app.config['JWT_COOKIE_SECURE'] = os.getenv('RENDER', '') != '' or os.getenv('ENVIRONMENT', '') == 'production'  # Auto-detect Render (HTTPS) or explicit production
app.config['JWT_COOKIE_CSRF_PROTECT'] = False  # Mitigated via SameSite
app.config['JWT_COOKIE_SAMESITE'] = 'Lax'  # Lax allows same-origin fetch + top-level navigations
app.config['JWT_ACCESS_COOKIE_PATH'] = '/'  # Scoped to all routes for reliable cookie delivery
app.config['JWT_ACCESS_COOKIE_NAME'] = 'ams_access_token' # Changed name to bypass stale cookies
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = timedelta(hours=int(os.getenv('JWT_ACCESS_TOKEN_HOURS', '8')))
# Receipts travel with requests: at most 5 files of 5 MB each, plus the form fields.
app.config['MAX_CONTENT_LENGTH'] = 26 * 1024 * 1024

jwt = JWTManager(app)

# Startup diagnostic — visible in Render logs
print(f"[JWT Config] Secure={app.config['JWT_COOKIE_SECURE']}, SameSite={app.config['JWT_COOKIE_SAMESITE']}, Path={app.config['JWT_ACCESS_COOKIE_PATH']}, Name={app.config['JWT_ACCESS_COOKIE_NAME']}, RENDER_ENV={os.getenv('RENDER', 'NOT_SET')}")

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["10000 per day", "2000 per hour"],
    storage_uri="memory://"
)

# Lock down CORS to only support specific origins
allowed_origins = os.getenv('CORS_ORIGINS', 'http://localhost:5000').split(',')
CORS(app, supports_credentials=True, origins=allowed_origins)

ORGANIZATION_PAUSED_MESSAGE = "Your organization's access is paused. Please contact your administrator."
SESSION_INVALID_MESSAGE = 'Your session is no longer valid. Please log in again.'

def create_session_token(user):
    return create_access_token(identity=str(user['email']), additional_claims={'organization_id': user['organization_id']})

def require_login(fn):
    # Account status, role and organization are re-read on every request, so removing an account,
    # changing a role or pausing an organization takes effect immediately instead of when the session expires.
    @wraps(fn)
    @jwt_required()
    def decorator(*args, **kwargs):
        conn = get_db_connection()
        try:
            user = conn.execute(
                'SELECT Users.id, Users.email, Users.role, Users.status, Users.organization_id, Users.manager_email, '
                'Organizations.status AS organization_status, Organizations.approval_mode, Organizations.auto_approve_above, '
                'Organizations.second_approval_above, Organizations.spot_check_percent '
                'FROM Users JOIN Organizations ON Organizations.id = Users.organization_id WHERE Users.email = ?',
                (get_jwt_identity(),)
            ).fetchone()
        finally:
            conn.close()

        if not user or user['status'] != 'Active' or user['organization_id'] != get_jwt().get('organization_id'):
            return jsonify({'error': SESSION_INVALID_MESSAGE}), 401
        if user['organization_status'] != 'Active':
            # The header lets the portals tell a paused organization apart from an ordinary refusal and sign people out.
            return jsonify({'error': ORGANIZATION_PAUSED_MESSAGE}), 403, {'X-Organization-Paused': '1'}
        g.user = dict(user)
        return fn(*args, **kwargs)
    return decorator

def require_role(role):
    def wrapper(fn):
        @wraps(fn)
        @require_login
        def decorator(*args, **kwargs):
            if g.user['role'] != role and g.user['role'] != 'SuperAdmin':
                return jsonify({"error": "Insufficient permissions"}), 403
            return fn(*args, **kwargs)
        return decorator
    return wrapper

PLATFORM_OWNER_ROLE = 'PlatformOwner'
HOME_PAGES = {PLATFORM_OWNER_ROLE: 'platform.html', 'SuperAdmin': 'admin.html', 'Admin': 'admin.html'}

def home_page(role):
    return HOME_PAGES.get(role, 'user.html')

def require_platform_owner(fn):
    # Platform Owners run the Neuzem platform and belong to no organization, so require_login,
    # which needs an organization, never lets them into any organization's data.
    @wraps(fn)
    @jwt_required()
    def decorator(*args, **kwargs):
        conn = get_db_connection()
        try:
            user = conn.execute(
                'SELECT id, email, role, status, organization_id FROM Users WHERE email = ?', (get_jwt_identity(),)
            ).fetchone()
        finally:
            conn.close()

        if not user or user['status'] != 'Active' or user['organization_id'] != get_jwt().get('organization_id'):
            return jsonify({'error': SESSION_INVALID_MESSAGE}), 401
        if user['role'] != PLATFORM_OWNER_ROLE or user['organization_id'] is not None:
            return jsonify({"error": "Insufficient permissions"}), 403
        g.user = dict(user)
        return fn(*args, **kwargs)
    return decorator

EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
SETUP_TOKEN_TTL = timedelta(hours=72)
RESET_TOKEN_TTL = timedelta(minutes=15)

def is_valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and EMAIL_PATTERN.fullmatch(value) is not None

def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()

def issue_token(conn, email, ttl):
    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + ttl).strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('UPDATE Users SET reset_token = ?, reset_expiry = ? WHERE email = ?', (hash_token(token), expiry, email))
    conn.commit()
    return token

def as_datetime(value):
    # SQLite returns the stored string; PostgreSQL returns a datetime.
    if isinstance(value, str):
        return datetime.strptime(value[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
    return value

def token_expired(expiry):
    if not expiry:
        return True
    return datetime.utcnow() > as_datetime(expiry)

def public_base_url():
    return (os.getenv('APP_BASE_URL') or request.host_url).rstrip('/')

SQLITE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

def to_json_row(row):
    # Both databases store CURRENT_TIMESTAMP in UTC; emit one ISO-8601 format so every browser parses it.
    result = {}
    for key, value in dict(row).items():
        if isinstance(value, datetime):
            value = value.strftime('%Y-%m-%dT%H:%M:%SZ')
        elif isinstance(value, Decimal):
            value = float(value)
        elif key.endswith('_at') and isinstance(value, str) and SQLITE_TIMESTAMP.fullmatch(value):
            value = value.replace(' ', 'T') + 'Z'
        result[key] = value
    return result

def record_event(conn, action, *, organization_id=None, request_id=None, actor=None,
                 from_status=None, to_status=None, comment=None, details=None):
    # The audit log is append-only: the database itself refuses to change or delete these rows.
    # Callers write the event in the same transaction as the change it describes.
    conn.execute(
        'INSERT INTO AuditEvents (organization_id, request_id, actor_email, action, from_status, to_status, comment, details) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (organization_id, request_id, actor, action, from_status, to_status, comment,
         json.dumps(details) if details is not None else None)
    )

# ==========================================
# 1. AUTHENTICATION & DATABASE CONFIGURATION
# ==========================================
DB_FILE = os.getenv('SQLITE_PATH', 'auth.db')
DEFAULT_ORGANIZATION_NAME = 'Default Organization'
DEFAULT_ORGANIZATION_ID = None  # Set by setup_database()

try:
    import psycopg2
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
except ImportError:
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

# Initialize Super Admin via environment variables if provided
def bootstrap_super_admin():
    sa_email = os.getenv('INITIAL_SUPER_ADMIN_EMAIL')
    sa_password = os.getenv('INITIAL_SUPER_ADMIN_PASSWORD', os.getenv('SUPER_ADMIN_PASSWORD'))

    if not sa_email or not sa_password:
        return

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (sa_email,)).fetchone()
    if not user:
        conn.execute(
            'INSERT INTO Users (email, password_hash, role, status, organization_id) VALUES (?, ?, ?, ?, ?)',
            (sa_email, generate_password_hash(sa_password), 'SuperAdmin', 'Active', DEFAULT_ORGANIZATION_ID)
        )
        conn.commit()
    conn.close()

class PostgresWrapper:
    def __init__(self, conn):
        self.conn = conn
        
    def execute(self, query, params=()):
        import psycopg2.extras
        # Escape literal % in LIKE clauses before placeholder conversion
        # Split on ? to preserve placeholders, escape % in non-placeholder parts
        parts = query.split('?')
        parts = [p.replace('%', '%%') for p in parts]
        query = '%s'.join(parts)
        # PostgreSQL requires single quotes for strings
        query = query.replace('"Pending"', "'Pending'")
        
        cursor = self.conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute(query, params)
        return cursor
        
    def commit(self):
        self.conn.commit()
        
    def close(self):
        self.conn.close()

# At most one organization can be the default, even when several workers start at once.
ORGANIZATIONS_SINGLE_DEFAULT_INDEX = (
    'CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_single_default ON Organizations (is_default) WHERE is_default = 1'
)

# The audit log may only be added to. One definition of the guard, used when the database is created
# and again after the one operation allowed to erase history: closing an organization.
SQLITE_AUDIT_TRIGGERS = tuple(
    f"CREATE TRIGGER IF NOT EXISTS audit_events_no_{operation.lower()} BEFORE {operation} ON AuditEvents "
    "BEGIN SELECT RAISE(ABORT, 'Audit events cannot be changed or deleted'); END"
    for operation in ('UPDATE', 'DELETE')
)

AUDIT_EVENT_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_audit_events_request ON AuditEvents (request_id)',
    'CREATE INDEX IF NOT EXISTS idx_audit_events_organization ON AuditEvents (organization_id)',
)

def init_db():
    DATABASE_URL = os.getenv('DATABASE_URL')
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                name TEXT,
                emp_id TEXT,
                reset_token TEXT,
                reset_expiry TIMESTAMP,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Requests (
                id SERIAL PRIMARY KEY,
                role TEXT,
                department TEXT,
                request_type TEXT,
                destination TEXT,
                amount NUMERIC,
                currency TEXT,
                normalized_amount NUMERIC,
                xgb_score NUMERIC,
                iso_score NUMERIC,
                svm_score NUMERIC,
                risk_score NUMERIC,
                final_decision TEXT,
                submitted_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ModelVersions (
                id SERIAL PRIMARY KEY,
                artifact BYTEA NOT NULL,
                metrics TEXT,
                created_by TEXT,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS TrainingJobs (
                id SERIAL PRIMARY KEY,
                status TEXT NOT NULL,
                step TEXT,
                message TEXT,
                metrics TEXT,
                started_by TEXT,
                model_version_id INTEGER,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Organizations (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                join_code TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'Active',
                allow_training_data INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(ORGANIZATIONS_SINGLE_DEFAULT_INDEX)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AuditEvents (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER REFERENCES Organizations(id),
                request_id INTEGER REFERENCES Requests(id),
                actor_email TEXT,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                comment TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'reject_audit_event_changes') THEN
                    CREATE FUNCTION reject_audit_event_changes() RETURNS trigger AS $body$
                    BEGIN
                        RAISE EXCEPTION 'Audit events cannot be changed or deleted';
                    END;
                    $body$ LANGUAGE plpgsql;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'audit_events_append_only') THEN
                    CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON AuditEvents
                        FOR EACH ROW EXECUTE FUNCTION reject_audit_event_changes();
                END IF;
            END
            $$
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS PolicyRules (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                rule_type TEXT NOT NULL,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_policy_rules_organization ON PolicyRules (organization_id)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Receipts (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                request_id INTEGER NOT NULL REFERENCES Requests(id),
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                content BYTEA NOT NULL,
                uploaded_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_receipts_request ON Receipts (request_id)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS SpotChecks (
                id SERIAL PRIMARY KEY,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                request_id INTEGER NOT NULL UNIQUE REFERENCES Requests(id),
                verdict TEXT,
                reviewed_by TEXT,
                reviewed_at TIMESTAMP,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_spot_checks_organization ON SpotChecks (organization_id)')
        for statement in AUDIT_EVENT_INDEXES:
            cursor.execute(statement)
        conn.commit()
        conn.close()
    else:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT,
                department TEXT,
                request_type TEXT,
                destination TEXT,
                amount REAL,
                currency TEXT,
                normalized_amount REAL,
                xgb_score REAL,
                iso_score REAL,
                svm_score REAL,
                risk_score REAL,
                final_decision TEXT,
                submitted_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ModelVersions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artifact BLOB NOT NULL,
                metrics TEXT,
                created_by TEXT,
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS TrainingJobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                step TEXT,
                message TEXT,
                metrics TEXT,
                started_by TEXT,
                model_version_id INTEGER,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                finished_at DATETIME
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Organizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                join_code TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'Active',
                allow_training_data INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(ORGANIZATIONS_SINGLE_DEFAULT_INDEX)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS AuditEvents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER REFERENCES Organizations(id),
                request_id INTEGER REFERENCES Requests(id),
                actor_email TEXT,
                action TEXT NOT NULL,
                from_status TEXT,
                to_status TEXT,
                comment TEXT,
                details TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        for statement in SQLITE_AUDIT_TRIGGERS:
            cursor.execute(statement)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS PolicyRules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                rule_type TEXT NOT NULL,
                name TEXT NOT NULL,
                config TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_policy_rules_organization ON PolicyRules (organization_id)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                request_id INTEGER NOT NULL REFERENCES Requests(id),
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                content BLOB NOT NULL,
                uploaded_by TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_receipts_request ON Receipts (request_id)')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS SpotChecks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                organization_id INTEGER NOT NULL REFERENCES Organizations(id),
                request_id INTEGER NOT NULL UNIQUE REFERENCES Requests(id),
                verdict TEXT,
                reviewed_by TEXT,
                reviewed_at DATETIME,
                comment TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_spot_checks_organization ON SpotChecks (organization_id)')
        for statement in AUDIT_EVENT_INDEXES:
            cursor.execute(statement)
        conn.commit()
        conn.close()

REQUEST_EXTRA_COLUMNS = {
    'employee_name': 'TEXT',
    'employee_id': 'TEXT',
    'reviewed_by': 'TEXT',
    'reviewed_at': 'TIMESTAMP',
    # What the AI decided on its own and the approval mode in force, kept to measure agreement with people.
    'ai_decision': 'TEXT',
    'approval_mode': 'TEXT',
    'policy_violations': 'TEXT',
    # Dates are stored as ISO text (YYYY-MM-DD) so SQLite and PostgreSQL return the same value.
    'purpose': 'TEXT',
    'expense_date': 'TEXT',
    'end_date': 'TEXT',
    # Who must decide a request that is waiting: the employee's manager, or NULL when the admins decide.
    'approver_email': 'TEXT',
    # Set when a large request has one approval and is waiting for a second one from an administrator.
    'first_approved_by': 'TEXT',
}

ORGANIZATION_SCOPED_TABLES = ('Users', 'Requests')

def check_and_add_columns():
    DATABASE_URL = os.getenv('DATABASE_URL')
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        cursor = conn.cursor()
        for column, column_type in REQUEST_EXTRA_COLUMNS.items():
            cursor.execute(f'ALTER TABLE Requests ADD COLUMN IF NOT EXISTS {column} {column_type}')
        for table in ORGANIZATION_SCOPED_TABLES:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS organization_id INTEGER REFERENCES Organizations(id)')
            cursor.execute(f'CREATE INDEX IF NOT EXISTS idx_{table.lower()}_organization ON {table} (organization_id)')
        cursor.execute('ALTER TABLE Organizations ADD COLUMN IF NOT EXISTS approval_mode TEXT')
        cursor.execute('ALTER TABLE Organizations ADD COLUMN IF NOT EXISTS auto_approve_above DOUBLE PRECISION')
        cursor.execute('ALTER TABLE Organizations ADD COLUMN IF NOT EXISTS second_approval_above DOUBLE PRECISION')
        cursor.execute('ALTER TABLE Organizations ADD COLUMN IF NOT EXISTS spot_check_percent INTEGER')
        cursor.execute('ALTER TABLE Users ADD COLUMN IF NOT EXISTS manager_email TEXT')
        conn.commit()
        conn.close()
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(Users)")
    columns = [col[1] for col in cursor.fetchall()]
    
    if 'name' not in columns:
        cursor.execute("ALTER TABLE Users ADD COLUMN name TEXT")
    if 'emp_id' not in columns:
        cursor.execute("ALTER TABLE Users ADD COLUMN emp_id TEXT")
    if 'reset_token' not in columns:
        cursor.execute("ALTER TABLE Users ADD COLUMN reset_token TEXT")
    if 'reset_expiry' not in columns:
        cursor.execute("ALTER TABLE Users ADD COLUMN reset_expiry DATETIME")
    if 'manager_email' not in columns:
        cursor.execute("ALTER TABLE Users ADD COLUMN manager_email TEXT")

    cursor.execute("PRAGMA table_info(Requests)")
    request_columns = {col[1] for col in cursor.fetchall()}
    for column, column_type in REQUEST_EXTRA_COLUMNS.items():
        if column not in request_columns:
            cursor.execute(f"ALTER TABLE Requests ADD COLUMN {column} {column_type.replace('TIMESTAMP', 'DATETIME')}")

    for table in ORGANIZATION_SCOPED_TABLES:
        cursor.execute(f"PRAGMA table_info({table})")
        if 'organization_id' not in {col[1] for col in cursor.fetchall()}:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER REFERENCES Organizations(id)")
        cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_{table.lower()}_organization ON {table} (organization_id)")

    cursor.execute("PRAGMA table_info(Organizations)")
    organization_columns = {col[1] for col in cursor.fetchall()}
    for column, column_type in (('approval_mode', 'TEXT'), ('auto_approve_above', 'REAL'), ('second_approval_above', 'REAL'),
                                ('spot_check_percent', 'INTEGER')):
        if column not in organization_columns:
            cursor.execute(f"ALTER TABLE Organizations ADD COLUMN {column} {column_type}")

    conn.commit()
    conn.close()

def get_db_connection():
    DATABASE_URL = os.getenv('DATABASE_URL')
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return PostgresWrapper(conn)
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

def ensure_default_organization():
    # Everything created before organizations existed belongs to one default organization.
    # It keeps contributing to model training, as all data did before.
    conn = get_db_connection()
    try:
        conn.execute(
            'INSERT INTO Organizations (name, join_code, allow_training_data, is_default, approval_mode) VALUES (?, ?, 1, 1, ?) '
            'ON CONFLICT DO NOTHING',
            (DEFAULT_ORGANIZATION_NAME, secrets.token_urlsafe(9), 'automatic')
        )
        organization_id = conn.execute('SELECT id FROM Organizations WHERE is_default = 1').fetchone()['id']
        # Platform Owners are the only accounts that belong to no organization.
        conn.execute(
            'UPDATE Users SET organization_id = ? WHERE organization_id IS NULL AND role != ?', (organization_id, PLATFORM_OWNER_ROLE)
        )
        conn.execute('UPDATE Requests SET organization_id = ? WHERE organization_id IS NULL', (organization_id,))
        # Organizations created before approval modes existed keep automatic approval; new ones start in shadow mode.
        conn.execute("UPDATE Organizations SET approval_mode = 'automatic' WHERE approval_mode IS NULL")
        conn.commit()
    finally:
        conn.close()
    return organization_id

def bootstrap_platform_owner():
    email = os.getenv('PLATFORM_OWNER_EMAIL')
    password = os.getenv('PLATFORM_OWNER_PASSWORD')
    if not email or not password:
        return

    conn = get_db_connection()
    try:
        existing = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if existing is None:
            conn.execute(
                'INSERT INTO Users (email, password_hash, role, status) VALUES (?, ?, ?, ?)',
                (email, generate_password_hash(password), PLATFORM_OWNER_ROLE, 'Active')
            )
            conn.commit()
        elif existing['role'] != PLATFORM_OWNER_ROLE:
            print(f"[WARNING] PLATFORM_OWNER_EMAIL already belongs to a {existing['role']} account, so no Platform Owner was created.")
    finally:
        conn.close()

def setup_database():
    global DEFAULT_ORGANIZATION_ID
    init_db()
    check_and_add_columns()
    DEFAULT_ORGANIZATION_ID = ensure_default_organization()
    bootstrap_super_admin()
    bootstrap_platform_owner()

print("Initializing Auth Database...")
setup_database()


# ==========================================
# 2. MACHINE LEARNING CONFIGURATION
# ==========================================
BUNDLED_MODEL_PATH = 'ensemble_ai_model.pkl'
BASE_TRAINING_CSV = 'combined_corporate_approval_data.csv'
MODEL_REFRESH_SECONDS = 30
MODEL_VERSIONS_KEPT = 5
TRAINING_JOB_STALE_AFTER = timedelta(minutes=30)

_model_lock = threading.Lock()
_model_state = {'artifacts': None, 'version_id': None, 'checked_at': None, 'unloadable_version_id': None}
_bundled_artifacts = None

def load_bundled_artifacts():
    global _bundled_artifacts
    if _bundled_artifacts is None:
        _bundled_artifacts = joblib.load(BUNDLED_MODEL_PATH)
    return _bundled_artifacts

def bundled_file_timestamp():
    return datetime.fromtimestamp(os.path.getmtime(BUNDLED_MODEL_PATH), timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def get_active_model(force=False):
    # Each worker process re-checks the database periodically, so a retrain or rollback reaches all of them.
    with _model_lock:
        now = time.monotonic()
        fresh = _model_state['checked_at'] is not None and now - _model_state['checked_at'] < MODEL_REFRESH_SECONDS
        if fresh and not force and _model_state['artifacts'] is not None:
            return _model_state['artifacts'], _model_state['version_id']

        try:
            conn = get_db_connection()
            try:
                row = conn.execute('SELECT id FROM ModelVersions WHERE is_active = 1 ORDER BY id DESC LIMIT 1').fetchone()
                version_id = row['id'] if row else None
                if version_id != _model_state['unloadable_version_id']:
                    _model_state['unloadable_version_id'] = None

                if version_id is None:
                    artifacts = load_bundled_artifacts()
                elif version_id == _model_state['version_id'] and _model_state['artifacts'] is not None:
                    artifacts = _model_state['artifacts']
                elif version_id == _model_state['unloadable_version_id'] and not force:
                    # This version already failed to load, so keep using the original model without retrying on every refresh.
                    artifacts, version_id = load_bundled_artifacts(), None
                else:
                    blob = conn.execute('SELECT artifact FROM ModelVersions WHERE id = ?', (version_id,)).fetchone()['artifact']
                    try:
                        artifacts = joblib.load(io.BytesIO(bytes(blob)))
                    except Exception:
                        app.logger.exception('Model version %s could not be loaded; the original model is scoring requests instead', version_id)
                        _model_state['unloadable_version_id'] = version_id
                        artifacts, version_id = load_bundled_artifacts(), None
            finally:
                conn.close()
            _model_state.update(artifacts=artifacts, version_id=version_id)
        except Exception:
            app.logger.exception("Could not load the active model")
            if _model_state['artifacts'] is None:
                try:
                    _model_state.update(artifacts=load_bundled_artifacts(), version_id=None)
                except Exception:
                    app.logger.exception("Could not load the bundled model")

        _model_state['checked_at'] = now
        return _model_state['artifacts'], _model_state['version_id']

print("Loading ensemble model artifacts...")
if get_active_model(force=True)[0] is not None:
    print(f"Model artifacts loaded successfully (active: {'version ' + str(_model_state['version_id']) if _model_state['version_id'] else 'bundled model'}).")
else:
    print("Error loading model artifacts: no usable model was found.")

exchange_rates = {
    'INR': 1.0,
    'USD': 83.50,
    'EUR': 90.20,
    'GBP': 105.00,
    'SGD': 61.30
}

AUTO_APPROVE_THRESHOLD = 0.8
ESCALATE_THRESHOLD = 0.2
# Organizations may raise their auto-approval threshold up to this value, but never below the default.
AUTO_APPROVE_THRESHOLD_MAX = 0.99
APPROVAL_MODES = ('shadow', 'automatic')

def organization_auto_approve_threshold(organization):
    value = organization.get('auto_approve_above')
    return AUTO_APPROVE_THRESHOLD if value is None else max(AUTO_APPROVE_THRESHOLD, float(value))

# A request worth at least this much needs two approvals. Nothing is set until a Super Admin turns it on.
SECOND_APPROVAL_MAX_INR = 1_000_000_000.0

def organization_second_approval_amount(organization):
    value = organization.get('second_approval_above')
    return None if value is None else float(value)

def needs_second_approval(organization, amount_inr):
    above = organization_second_approval_amount(organization)
    return above is not None and float(amount_inr or 0) >= above


# ==========================================
# SPOT CHECKS ON AUTOMATIC APPROVALS
# ==========================================
# Nobody looks at a request the AI approves on its own, so a random few are checked afterwards by a person.
# Their verdicts are the only honest measure of how good those approvals really are.
SPOT_CHECK_MIN_PERCENT = 5
SPOT_CHECK_MAX_PERCENT = 100
SPOT_CHECK_VERDICTS = ('CORRECT', 'WRONG')

def organization_spot_check_percent(organization):
    value = organization.get('spot_check_percent')
    if value is None:
        return SPOT_CHECK_MIN_PERCENT
    return max(SPOT_CHECK_MIN_PERCENT, min(SPOT_CHECK_MAX_PERCENT, int(value)))

def spot_check_selected(percent):
    """True for a random share of automatic approvals. Tests replace this to choose exactly which ones."""
    return percent > 0 and secrets.randbelow(100) < percent


# ==========================================
# DECISION QUALITY
# ==========================================
# What the AI decided is stored next to what a person decided, so the two can be compared on real work.
QUALITY_WINDOWS = (30, 90, 365)
QUALITY_DEFAULT_WINDOW = 90
# Below this many decisions the rates jump around too much to mean anything.
QUALITY_ENOUGH_DECISIONS = 20

def share(part, whole):
    return None if not whole else round(part / whole, 4)


# Before an organization is allowed to let the AI approve on its own, its own numbers have to earn it.
AUTOMATIC_APPROVAL_WINDOW = 90
AUTOMATIC_APPROVAL_MIN_AGREEMENT = 0.85
AUTOMATIC_APPROVAL_MAX_MISSED = 0.05
AUTOMATIC_APPROVAL_MAX_SPOT_CHECK_WRONG = 0.10

def automatic_approval_readiness(quality):
    """Each check a company must pass before the AI may approve without a person. Rates are shares, not percentages."""
    comparison, spot_checks = quality['comparison'], quality['spot_checks']
    checks = [{
        'name': 'decisions',
        'label': f"People have decided at least {QUALITY_ENOUGH_DECISIONS} requests the AI also scored",
        'value': comparison['pairs'],
        'target': QUALITY_ENOUGH_DECISIONS,
        'passed': comparison['pairs'] >= QUALITY_ENOUGH_DECISIONS,
    }, {
        'name': 'agreement',
        'label': f"The AI and the people agree on at least {AUTOMATIC_APPROVAL_MIN_AGREEMENT:.0%} of them",
        'value': comparison['agreement_rate'],
        'target': AUTOMATIC_APPROVAL_MIN_AGREEMENT,
        'passed': comparison['agreement_rate'] is not None and comparison['agreement_rate'] >= AUTOMATIC_APPROVAL_MIN_AGREEMENT,
    }, {
        'name': 'missed_problems',
        'label': f"At most {AUTOMATIC_APPROVAL_MAX_MISSED:.0%} of what the AI would approve was refused by a person",
        'value': comparison['missed_problem_rate'],
        'target': AUTOMATIC_APPROVAL_MAX_MISSED,
        'passed': comparison['missed_problem_rate'] is not None and comparison['missed_problem_rate'] <= AUTOMATIC_APPROVAL_MAX_MISSED,
    }]
    # Spot checks only exist once an organization is already approving automatically, so they count only when there are some.
    if spot_checks['done']:
        checks.append({
            'name': 'spot_checks',
            'label': f"At most {AUTOMATIC_APPROVAL_MAX_SPOT_CHECK_WRONG:.0%} of the checked automatic approvals were wrong",
            'value': spot_checks['wrong_rate'],
            'target': AUTOMATIC_APPROVAL_MAX_SPOT_CHECK_WRONG,
            'passed': spot_checks['wrong_rate'] <= AUTOMATIC_APPROVAL_MAX_SPOT_CHECK_WRONG,
        })
    return {
        'ready': all(check['passed'] for check in checks),
        'days': quality['days'],
        'checks': checks,
        'blocked_by': [check['label'] for check in checks if not check['passed']],
    }

# ==========================================
# WEEK BY WEEK MONITORING
# ==========================================
# The work a company sends in changes over time. Comparing the last week with the weeks before it
# shows when the AI is suddenly meeting requests it was never trained for.
MONITORING_WEEKS = 12
DRIFT_MIN_REQUESTS = 10
DRIFT_RATE_JUMP = 0.15
DRIFT_UNUSUAL_JUMP = 0.10
DRIFT_SCORE_MOVE = 0.10
DRIFT_VOLUME_CHANGE = 0.6

# ==========================================
# WHO THE DECISIONS FALL ON
# ==========================================
# The same system can treat two departments very differently without anybody noticing.
# These numbers do not prove unfairness; they show where somebody should go and look.
FAIRNESS_FIELDS = ('department', 'role')
FAIRNESS_MIN_REQUESTS = 10
FAIRNESS_GAP = 0.15

def group_summary(rows):
    finished = [row for row in rows if is_decided(row['final_decision'])]
    approved = sum(1 for row in finished if row['final_decision'] == 'APPROVED')
    needed_person = sum(1 for row in rows if not (row['final_decision'] == 'APPROVED' and not row['reviewed_by']))
    return {
        'requests': len(rows),
        'decided': len(finished),
        'approved': approved,
        'approval_rate': share(approved, len(finished)),
        'needed_person': needed_person,
        'needed_person_rate': share(needed_person, len(rows)),
    }

def fairness_report(conn, organization_id, days):
    since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        'SELECT role, department, final_decision, reviewed_by FROM Requests WHERE organization_id = ? AND created_at >= ?',
        (organization_id, since)
    ).fetchall()
    overall = group_summary(rows)

    report = {'days': days, 'minimum_requests': FAIRNESS_MIN_REQUESTS, 'overall': overall, 'notes': []}
    for field in FAIRNESS_FIELDS:
        buckets = {}
        for row in rows:
            buckets.setdefault((row[field] or '').strip() or 'Not given', []).append(row)

        groups = []
        for name, items in buckets.items():
            group = {'value': name, **group_summary(items)}
            group['compared'] = group['requests'] >= FAIRNESS_MIN_REQUESTS
            group['approval_gap'] = bool(
                group['compared'] and group['decided'] >= FAIRNESS_MIN_REQUESTS and group['approval_rate'] is not None
                and overall['approval_rate'] is not None and overall['approval_rate'] - group['approval_rate'] > FAIRNESS_GAP
            )
            group['review_gap'] = bool(
                group['compared'] and group['needed_person_rate'] is not None and overall['needed_person_rate'] is not None
                and group['needed_person_rate'] - overall['needed_person_rate'] > FAIRNESS_GAP
            )
            groups.append(group)
            if group['approval_gap']:
                report['notes'].append({'field': field, 'value': name, 'message':
                    f"{name} requests are approved {group['approval_rate']:.0%} of the time, "
                    f"against {overall['approval_rate']:.0%} across the company."})
            # A rejected request always went past a person, so a group flagged for approvals is not flagged twice.
            if group['review_gap'] and not group['approval_gap']:
                report['notes'].append({'field': field, 'value': name, 'message':
                    f"{group['needed_person_rate']:.0%} of {name} requests are sent to a person, "
                    f"against {overall['needed_person_rate']:.0%} across the company."})

        report[f'by_{field}'] = sorted(groups, key=lambda group: (-group['requests'], group['value']))
    return report


def summarize_week(rows):
    total = len(rows)
    needed_person = sum(1 for row in rows if not (row['final_decision'] == 'APPROVED' and not row['reviewed_by']))
    unknown = sum(1 for row in rows if row['ai_decision'] == 'ESCALATED_UNKNOWN')
    unusual = sum(1 for row in rows if row['ai_decision'] == 'ESCALATED_ANOMALY')
    scores = [float(row['xgb_score']) for row in rows if row['xgb_score'] is not None]
    return {
        'requests': total,
        'needed_person': needed_person,
        'needed_person_rate': share(needed_person, total),
        'unknown_category': unknown,
        'unknown_category_rate': share(unknown, total),
        'unusual': unusual,
        'unusual_rate': share(unusual, total),
        'average_score': round(sum(scores) / len(scores), 4) if scores else None,
    }

def moved_up(latest, baseline, limit):
    return latest is not None and baseline is not None and latest - baseline > limit

def drift_warnings(latest, baseline, rates=True):
    """Plain sentences about what changed, never a number on its own."""
    warnings = []
    def add(name, message):
        warnings.append({'name': name, 'message': message})

    # A week with few requests says nothing about rates, but a week that is suddenly empty says plenty.
    if not rates:
        volume_warnings(latest, baseline, add)
        return warnings

    if moved_up(latest['needed_person_rate'], baseline['needed_person_rate'], DRIFT_RATE_JUMP):
        add('needed_person', f"People are being asked to decide {latest['needed_person_rate']:.0%} of requests, "
                             f"against {baseline['needed_person_rate']:.0%} in the weeks before.")
    if moved_up(latest['unknown_category_rate'], baseline['unknown_category_rate'], DRIFT_UNUSUAL_JUMP):
        add('unknown_category', f"{latest['unknown_category_rate']:.0%} of requests used a role, department, type or "
                                f"destination the AI has never seen, against {baseline['unknown_category_rate']:.0%} before. "
                                'The AI is working outside what it was trained on.')
    if moved_up(latest['unusual_rate'], baseline['unusual_rate'], DRIFT_UNUSUAL_JUMP):
        add('unusual', f"{latest['unusual_rate']:.0%} of requests looked unusual to the AI, "
                       f"against {baseline['unusual_rate']:.0%} before.")
    if (latest['average_score'] is not None and baseline['average_score'] is not None
            and abs(latest['average_score'] - baseline['average_score']) > DRIFT_SCORE_MOVE):
        direction = 'up' if latest['average_score'] > baseline['average_score'] else 'down'
        add('average_score', f"The AI's average approval score moved {direction} to {latest['average_score']:.0%}, "
                             f"from {baseline['average_score']:.0%} in the weeks before.")

    volume_warnings(latest, baseline, add)
    return warnings

def volume_warnings(latest, baseline, add):
    weekly_baseline = baseline['requests'] / max(1, baseline['weeks'])
    if weekly_baseline < DRIFT_MIN_REQUESTS:
        return
    if latest['requests'] > weekly_baseline * (1 + DRIFT_VOLUME_CHANGE):
        add('volume', f"{latest['requests']} requests came in this week, well above the usual "
                      f"{weekly_baseline:.0f} a week.")
    elif latest['requests'] < weekly_baseline * (1 - DRIFT_VOLUME_CHANGE):
        add('volume', f"Only {latest['requests']} requests came in this week, well below the usual "
                      f"{weekly_baseline:.0f} a week.")

def weekly_monitoring(conn, organization_id, weeks=MONITORING_WEEKS):
    """The last few weeks side by side, plus a warning whenever the newest week stands out."""
    today = datetime.utcnow()
    since = (today - timedelta(days=7 * weeks)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        'SELECT created_at, final_decision, ai_decision, xgb_score, reviewed_by FROM Requests '
        'WHERE organization_id = ? AND created_at >= ?',
        (organization_id, since)
    ).fetchall()

    buckets = [[] for _ in range(weeks)]
    for row in rows:
        index = (today - as_datetime(row['created_at'])).days // 7
        if 0 <= index < weeks:
            buckets[index].append(row)

    listed = []
    for index in range(weeks - 1, -1, -1):
        week = summarize_week(buckets[index])
        week['starting'] = (today - timedelta(days=7 * (index + 1))).strftime('%Y-%m-%d')
        week['ending'] = (today - timedelta(days=7 * index)).strftime('%Y-%m-%d')
        listed.append(week)

    latest = listed[-1]
    earlier = [row for bucket in buckets[1:] for row in bucket]
    baseline = summarize_week(earlier)
    baseline['weeks'] = sum(1 for bucket in buckets[1:] if bucket)
    enough = latest['requests'] >= DRIFT_MIN_REQUESTS and baseline['requests'] >= DRIFT_MIN_REQUESTS
    return {
        'weeks': listed,
        'latest': latest,
        'baseline': baseline,
        'enough_data': enough,
        'minimum_requests': DRIFT_MIN_REQUESTS,
        'warnings': drift_warnings(latest, baseline, rates=enough) if baseline['requests'] else [],
    }


def decision_quality(conn, organization_id, days):
    """How often the AI and the people agreed, plus volume, speed and the spot-check answers."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    rows = conn.execute(
        'SELECT ai_decision, final_decision, reviewed_by, created_at, reviewed_at '
        'FROM Requests WHERE organization_id = ? AND created_at >= ?',
        (organization_id, since)
    ).fetchall()

    totals = {'requests': len(rows), 'automatic_approvals': 0, 'decided_by_people': 0, 'waiting': 0}
    agreed = missed = unnecessary = ai_approved_pairs = ai_flagged_pairs = 0
    hours = []
    for row in rows:
        decision, recommendation = row['final_decision'], row['ai_decision']
        if is_awaiting_review(decision):
            totals['waiting'] += 1
        elif decision == 'APPROVED' and not row['reviewed_by']:
            totals['automatic_approvals'] += 1

        if not is_decided(decision) or not row['reviewed_by']:
            continue
        totals['decided_by_people'] += 1
        if row['reviewed_at'] and row['created_at']:
            hours.append((as_datetime(row['reviewed_at']) - as_datetime(row['created_at'])).total_seconds() / 3600)
        if not recommendation:
            continue
        # The AI only ever says "approve" or "a person should look at this", so that is what is compared.
        ai_approved = recommendation == 'APPROVED'
        person_approved = decision == 'APPROVED'
        if ai_approved:
            ai_approved_pairs += 1
        else:
            ai_flagged_pairs += 1
        if ai_approved and not person_approved:
            missed += 1  # The AI would have let through something a person refused.
        elif not ai_approved and person_approved:
            unnecessary += 1  # Safe, but somebody was asked to look at a request that was fine.
        else:
            agreed += 1

    checks = conn.execute(
        'SELECT verdict FROM SpotChecks WHERE organization_id = ? AND created_at >= ?', (organization_id, since)
    ).fetchall()
    done = [check['verdict'] for check in checks if check['verdict']]
    wrong = [verdict for verdict in done if verdict == 'WRONG']

    pairs = ai_approved_pairs + ai_flagged_pairs
    return {
        'days': days,
        'enough_decisions': pairs >= QUALITY_ENOUGH_DECISIONS,
        'minimum_decisions': QUALITY_ENOUGH_DECISIONS,
        'totals': totals,
        'comparison': {
            'pairs': pairs, 'agreed': agreed, 'missed_problems': missed, 'unnecessary_reviews': unnecessary,
            'ai_approved': ai_approved_pairs, 'ai_flagged': ai_flagged_pairs,
            'agreement_rate': share(agreed, pairs),
            'missed_problem_rate': share(missed, ai_approved_pairs),
            'unnecessary_review_rate': share(unnecessary, ai_flagged_pairs),
        },
        'spot_checks': {
            'done': len(done), 'waiting': len(checks) - len(done), 'wrong': len(wrong),
            'wrong_rate': share(len(wrong), len(done)),
        },
        'speed': {
            'decided': len(hours),
            'average_hours': round(sum(hours) / len(hours), 1) if hours else None,
            'slowest_hours': round(max(hours), 1) if hours else None,
        },
    }


# ==========================================
# WAITING-APPROVER NOTICES
# ==========================================
def request_reference(request_id):
    return f"REQ_{int(request_id):04d}"

def approvers_to_notify(conn, organization_id, approver_email):
    """The manager a request is waiting for, or every administrator when it waits for the admins."""
    if approver_email:
        rows = conn.execute(
            "SELECT email, role FROM Users WHERE email = ? AND organization_id = ? AND status = 'Active'",
            (approver_email, organization_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT email, role FROM Users WHERE organization_id = ? AND status = 'Active' AND role IN ('Admin', 'SuperAdmin')",
            (organization_id,)
        ).fetchall()
    return [(row["email"], home_page(row["role"])) for row in rows]

def notify_waiting_approvers(recipients, summary, organization_name, note=None):
    """Email trouble must never fail a decision that is already saved, so every send is guarded."""
    for email, page in recipients:
        try:
            email_service.sendApprovalWaitingEmail(email, summary, f"{public_base_url()}/{page}", organization_name, note)
        except Exception:
            app.logger.exception("Could not tell %s that a request is waiting for them", email)

def waiting_notice(conn, organization_id, request_id, approver_email, note=None):
    """Collects what the notice needs while the connection is open. Call the returned function after the commit."""
    row = conn.execute(
        "SELECT Requests.id, Requests.submitted_by, Requests.employee_name, Requests.amount, Requests.currency, "
        "Requests.request_type, Requests.destination, Requests.purpose, Organizations.name AS organization_name "
        "FROM Requests JOIN Organizations ON Organizations.id = Requests.organization_id "
        "WHERE Requests.id = ? AND Requests.organization_id = ?",
        (request_id, organization_id)
    ).fetchone()
    recipients = approvers_to_notify(conn, organization_id, approver_email)
    if not row or not recipients:
        return lambda: None
    summary = {
        "reference": request_reference(row["id"]),
        "employee": row["employee_name"] or row["submitted_by"],
        "amount": f"{row['amount']} {row['currency']}",
        "details": f"{row['request_type']} - {row['destination']}",
        "purpose": row["purpose"],
    }
    organization_name = row["organization_name"]
    return lambda: notify_waiting_approvers(recipients, summary, organization_name, note)


# ==========================================
# REQUEST DETAILS
# ==========================================
PURPOSE_MIN_LENGTH = 10
PURPOSE_MAX_LENGTH = 500
REQUEST_DATE_WINDOW = timedelta(days=366)
TRIP_MAX_DAYS = 90

def parse_request_dates(start_value, end_value):
    """Returns (expense date, end date or None, None) as ISO strings, or (None, None, error message)."""
    def parse(value):
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None

    start = parse(start_value)
    if start is None:
        return None, None, 'Please give the expense date, for example 2026-09-12.'
    today = datetime.utcnow().date()
    if not today - REQUEST_DATE_WINDOW <= start <= today + REQUEST_DATE_WINDOW:
        return None, None, 'The expense date must be within a year of today.'

    if end_value in (None, ''):
        return start.isoformat(), None, None
    end = parse(end_value)
    if end is None:
        return None, None, 'The end date must be a date, for example 2026-09-14.'
    if not start <= end <= start + timedelta(days=TRIP_MAX_DAYS):
        return None, None, f'The end date must be on or after the expense date and within {TRIP_MAX_DAYS} days of it.'
    return start.isoformat(), end.isoformat(), None


# ==========================================
# RECEIPTS
# ==========================================
RECEIPT_MAX_BYTES = 5 * 1024 * 1024
RECEIPTS_PER_REQUEST = 5
# A receipt's type is read from its content, never from its name or what the browser claims.
RECEIPT_SIGNATURES = (
    (b'%PDF-', 'application/pdf', '.pdf'),
    (b'\x89PNG\r\n\x1a\n', 'image/png', '.png'),
    (b'\xff\xd8\xff', 'image/jpeg', '.jpg'),
)
RECEIPT_COUNT_SQL = '(SELECT COUNT(*) FROM Receipts WHERE Receipts.request_id = Requests.id) AS receipt_count'

def read_receipt_uploads(files):
    """Returns (receipts, None) for valid uploads, or (None, error message)."""
    files = [upload for upload in files if upload and upload.filename]
    if len(files) > RECEIPTS_PER_REQUEST:
        return None, f'Attach at most {RECEIPTS_PER_REQUEST} receipts to a request.'
    receipts = []
    for upload in files:
        content = upload.read(RECEIPT_MAX_BYTES + 1)
        if not content:
            return None, 'One of the receipt files is empty.'
        if len(content) > RECEIPT_MAX_BYTES:
            return None, 'Each receipt must be 5 MB or smaller.'
        kind = next(((mime, extension) for signature, mime, extension in RECEIPT_SIGNATURES if content.startswith(signature)), None)
        if kind is None:
            return None, 'Receipts must be PDF, JPG or PNG files.'
        stem = os.path.splitext(secure_filename(upload.filename))[0][:100] or 'receipt'
        receipts.append({
            'filename': stem + kind[1], 'content_type': kind[0], 'size': len(content),
            'sha256': hashlib.sha256(content).hexdigest(), 'content': content,
        })
    return receipts, None

def save_receipts(conn, organization_id, request_id, uploader, receipts):
    for receipt in receipts:
        receipt_id = conn.execute(
            'INSERT INTO Receipts (organization_id, request_id, filename, content_type, size, sha256, content, uploaded_by) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id',
            (organization_id, request_id, receipt['filename'], receipt['content_type'], receipt['size'],
             receipt['sha256'], receipt['content'], uploader)
        ).fetchall()[0][0]
        details = {key: receipt[key] for key in ('filename', 'content_type', 'size', 'sha256')}
        record_event(conn, 'receipt.added', organization_id=organization_id, request_id=request_id, actor=uploader,
                     details={'receipt_id': receipt_id, **details})

def is_manager_of(conn, employee_email):
    return bool(conn.execute(
        'SELECT 1 FROM Users WHERE email = ? AND organization_id = ? AND manager_email = ?',
        (employee_email, g.user['organization_id'], g.user['email'])
    ).fetchone())

def can_review_request(conn, request_id):
    # Admins review every request in their organization; managers review their direct reports' requests.
    if g.user['role'] in ('Admin', 'SuperAdmin'):
        return True
    row = conn.execute(
        'SELECT submitted_by FROM Requests WHERE id = ? AND organization_id = ?', (request_id, g.user['organization_id'])
    ).fetchone()
    return bool(row) and is_manager_of(conn, row['submitted_by'])

def can_view_request(conn, request_id):
    # Employees see receipts on their own requests; their manager and the organization's admins see them too.
    row = conn.execute(
        'SELECT submitted_by FROM Requests WHERE id = ? AND organization_id = ?', (request_id, g.user['organization_id'])
    ).fetchone()
    return bool(row) and (row['submitted_by'] == g.user['email'] or can_review_request(conn, request_id))

@app.errorhandler(413)
def upload_too_large(error):
    return jsonify({'error': f'The upload is too large. Attach at most {RECEIPTS_PER_REQUEST} receipts of 5 MB each.'}), 413

@app.route('/api/auth/request_receipts', methods=['GET'])
@require_login
def request_receipts():
    request_id = request.args.get('id', type=int)
    if request_id is None:
        return jsonify({'error': 'Request ID is required'}), 400

    conn = get_db_connection()
    try:
        if not can_view_request(conn, request_id):
            return jsonify({'error': 'Request not found.'}), 404
        rows = conn.execute(
            'SELECT id, filename, content_type, size, uploaded_by, created_at FROM Receipts '
            'WHERE request_id = ? AND organization_id = ? ORDER BY id',
            (request_id, g.user['organization_id'])
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'receipts': [to_json_row(row) for row in rows]})

@app.route('/api/auth/receipt', methods=['GET'])
@require_login
def download_receipt():
    receipt_id = request.args.get('id', type=int)
    if receipt_id is None:
        return jsonify({'error': 'Receipt ID is required'}), 400

    conn = get_db_connection()
    try:
        receipt = conn.execute(
            'SELECT request_id, filename, content_type, content FROM Receipts WHERE id = ? AND organization_id = ?',
            (receipt_id, g.user['organization_id'])
        ).fetchone()
        if not receipt or not can_view_request(conn, receipt['request_id']):
            return jsonify({'error': 'Receipt not found.'}), 404
    finally:
        conn.close()

    # Images open in the browser; PDFs download, so no uploaded document runs inside this site.
    response = send_file(
        io.BytesIO(bytes(receipt['content'])), mimetype=receipt['content_type'], download_name=receipt['filename'],
        as_attachment=receipt['content_type'] == 'application/pdf', etag=False
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['Content-Security-Policy'] = "default-src 'none'; img-src 'self'"
    return response

@app.route('/api/auth/add_receipts', methods=['POST'])
@limiter.limit("30 per hour")
@require_login
def add_receipts():
    request_id = request.form.get('request_id', type=int)
    if request_id is None:
        return jsonify({'error': 'Request ID is required'}), 400
    receipts, error = read_receipt_uploads(request.files.getlist('receipts'))
    if error:
        return jsonify({'error': error}), 400
    if not receipts:
        return jsonify({'error': 'Choose at least one receipt.'}), 400

    conn = get_db_connection()
    try:
        target = conn.execute(
            'SELECT id, submitted_by, final_decision FROM Requests WHERE id = ? AND organization_id = ?',
            (request_id, g.user['organization_id'])
        ).fetchone()
        if not target or target['submitted_by'] != g.user['email']:
            return jsonify({'error': 'Request not found.'}), 404
        if target['final_decision'] == 'REJECTED':
            return jsonify({'error': "Receipts can't be added to a rejected request."}), 409
        existing = conn.execute('SELECT COUNT(*) AS total FROM Receipts WHERE request_id = ?', (target['id'],)).fetchone()['total']
        if existing + len(receipts) > RECEIPTS_PER_REQUEST:
            return jsonify({'error': f'A request can have at most {RECEIPTS_PER_REQUEST} receipts.'}), 409
        save_receipts(conn, g.user['organization_id'], target['id'], g.user['email'], receipts)
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS', 'added': len(receipts)}), 201


# ==========================================
# POLICY RULES
# ==========================================
POLICY_RULE_TYPES = ('amount_limit', 'duplicate_request', 'always_review', 'receipt_required')
POLICY_REVIEW_FIELDS = {'request_type': 'Request type', 'destination': 'Destination', 'role': 'Role', 'department': 'Department'}
POLICY_RULES_PER_ORGANIZATION = 100
POLICY_TEXT_MAX_LENGTH = 100

def validate_policy_config(rule_type, config):
    """Returns (clean config, None) for a valid rule configuration, or (None, error message)."""
    if not isinstance(config, dict):
        return None, 'config must be an object.'

    if rule_type == 'amount_limit':
        texts = {}
        for key in ('role', 'request_type'):
            value = config.get(key)
            if value is not None and (not isinstance(value, str) or len(value.strip()) > POLICY_TEXT_MAX_LENGTH):
                return None, f'{key} must be text of at most {POLICY_TEXT_MAX_LENGTH} characters, or empty for any.'
            texts[key] = value.strip() if value and value.strip() else None
        limit = config.get('max_amount_inr')
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or not 0 < limit <= 1_000_000_000:
            return None, 'max_amount_inr must be a positive amount in INR.'
        return {**texts, 'max_amount_inr': float(limit)}, None

    if rule_type == 'duplicate_request':
        days = config.get('window_days')
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 90:
            return None, 'window_days must be a whole number from 1 to 90.'
        return {'window_days': days}, None

    if rule_type == 'receipt_required':
        above = config.get('above_amount_inr', 0)
        if isinstance(above, bool) or not isinstance(above, (int, float)) or not math.isfinite(above) or not 0 <= above <= 1_000_000_000:
            return None, 'above_amount_inr must be an amount in INR, or 0 to require a receipt on every request.'
        return {'above_amount_inr': float(above)}, None

    field, values = config.get('field'), config.get('values')
    if not isinstance(field, str) or field not in POLICY_REVIEW_FIELDS:
        return None, f"field must be one of: {', '.join(POLICY_REVIEW_FIELDS)}."
    if (not isinstance(values, list) or not 1 <= len(values) <= 50
            or not all(isinstance(v, str) and v.strip() and len(v.strip()) <= POLICY_TEXT_MAX_LENGTH for v in values)):
        return None, f'values must list 1 to 50 texts of at most {POLICY_TEXT_MAX_LENGTH} characters.'
    return {'field': field, 'values': sorted({v.strip() for v in values})}, None

def text_matches(expected, actual):
    return not expected or str(actual or '').strip().lower() == expected.lower()

def policy_rule_reason(conn, rule_type, config, organization_id, submitter, fields):
    if rule_type == 'amount_limit':
        covered = text_matches(config.get('role'), fields['role']) and text_matches(config.get('request_type'), fields['request_type'])
        if covered and fields['amount_inr'] > config['max_amount_inr']:
            return f"Amount ₹{fields['amount_inr']:,.0f} is above the ₹{config['max_amount_inr']:,.0f} limit."
    elif rule_type == 'duplicate_request':
        since = (datetime.utcnow() - timedelta(days=config['window_days'])).strftime('%Y-%m-%d %H:%M:%S')
        earlier = conn.execute(
            'SELECT COUNT(*) AS total FROM Requests WHERE organization_id = ? AND submitted_by = ? AND request_type = ? '
            'AND amount = ? AND currency = ? AND created_at >= ?',
            (organization_id, submitter, fields['request_type'], fields['amount'], fields['currency'], since)
        ).fetchone()['total']
        if earlier:
            return f"The same employee submitted this request type and amount within the last {config['window_days']} days."
    elif rule_type == 'receipt_required':
        if fields['receipt_count'] == 0 and fields['amount_inr'] > config['above_amount_inr']:
            if config['above_amount_inr']:
                return f"No receipt was attached for an amount above ₹{config['above_amount_inr']:,.0f}."
            return 'No receipt was attached.'
    elif rule_type == 'always_review':
        value = str(fields[config['field']] or '').strip()
        if value.lower() in {v.lower() for v in config['values']}:
            return f'{POLICY_REVIEW_FIELDS[config["field"]]} "{value}" always needs review.'
    return None

def policy_violations(conn, organization_id, submitter, fields):
    rules = conn.execute(
        'SELECT id, rule_type, name, config FROM PolicyRules WHERE organization_id = ? AND is_active = 1 ORDER BY id',
        (organization_id,)
    ).fetchall()
    violations = []
    for rule in rules:
        reason = policy_rule_reason(conn, rule['rule_type'], json.loads(rule['config']), organization_id, submitter, fields)
        if reason:
            violations.append({'rule_id': rule['id'], 'name': rule['name'], 'reason': reason})
    return violations

def policy_rule_json(row):
    rule = to_json_row(row)
    rule['config'] = json.loads(rule['config'])
    rule['is_active'] = bool(rule['is_active'])
    return rule

def clean_rule_name(value):
    name = ' '.join(value.split()) if isinstance(value, str) else ''
    return name if 2 <= len(name) <= 120 else None

@app.route('/api/auth/policy_rules', methods=['GET'])
@require_role('Admin')
def list_policy_rules():
    conn = get_db_connection()
    try:
        rows = conn.execute(
            'SELECT id, rule_type, name, config, is_active, created_by, created_at FROM PolicyRules '
            'WHERE organization_id = ? ORDER BY is_active DESC, id',
            (g.user['organization_id'],)
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'rules': [policy_rule_json(row) for row in rows]})

@app.route('/api/auth/create_policy_rule', methods=['POST'])
@require_role('SuperAdmin')
def create_policy_rule():
    data = request.get_json(silent=True) or {}
    rule_type = data.get('rule_type')
    if not isinstance(rule_type, str) or rule_type not in POLICY_RULE_TYPES:
        return jsonify({'error': f"rule_type must be one of: {', '.join(POLICY_RULE_TYPES)}."}), 400
    name = clean_rule_name(data.get('name'))
    if not name:
        return jsonify({'error': 'Give the rule a name of 2 to 120 characters.'}), 400
    config, error = validate_policy_config(rule_type, data.get('config'))
    if error:
        return jsonify({'error': error}), 400

    conn = get_db_connection()
    try:
        active = conn.execute(
            'SELECT COUNT(*) AS total FROM PolicyRules WHERE organization_id = ? AND is_active = 1', (g.user['organization_id'],)
        ).fetchone()['total']
        if active >= POLICY_RULES_PER_ORGANIZATION:
            return jsonify({'error': f'An organization can have at most {POLICY_RULES_PER_ORGANIZATION} active rules. Turn off rules you no longer need.'}), 409
        rule_id = conn.execute(
            'INSERT INTO PolicyRules (organization_id, rule_type, name, config, created_by) VALUES (?, ?, ?, ?, ?) RETURNING id',
            (g.user['organization_id'], rule_type, name, json.dumps(config), g.user['email'])
        ).fetchall()[0][0]
        record_event(conn, 'policy_rule.created', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={'rule_id': rule_id, 'rule_type': rule_type, 'name': name, 'config': config})
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS', 'rule_id': rule_id}), 201

@app.route('/api/auth/update_policy_rule', methods=['POST'])
@require_role('SuperAdmin')
def update_policy_rule():
    # Rules are never deleted, only turned off, so every request's recorded violations still point at a real rule.
    data = request.get_json(silent=True) or {}
    rule_id = data.get('id')
    if isinstance(rule_id, bool) or not isinstance(rule_id, int):
        return jsonify({'error': 'id must be a rule number.'}), 400

    conn = get_db_connection()
    try:
        rule = conn.execute(
            'SELECT id, rule_type, name, config, is_active FROM PolicyRules WHERE id = ? AND organization_id = ?',
            (rule_id, g.user['organization_id'])
        ).fetchone()
        if not rule:
            return jsonify({'error': 'Policy rule not found.'}), 404

        before = {'name': rule['name'], 'config': json.loads(rule['config']), 'is_active': bool(rule['is_active'])}
        after = dict(before)
        if not any(key in data for key in after):
            return jsonify({'error': 'Nothing to update.'}), 400
        if 'name' in data:
            after['name'] = clean_rule_name(data['name'])
            if not after['name']:
                return jsonify({'error': 'Give the rule a name of 2 to 120 characters.'}), 400
        if 'config' in data:
            after['config'], error = validate_policy_config(rule['rule_type'], data['config'])
            if error:
                return jsonify({'error': error}), 400
        if 'is_active' in data:
            if not isinstance(data['is_active'], bool):
                return jsonify({'error': 'is_active must be true or false.'}), 400
            after['is_active'] = data['is_active']

        changed = [key for key in after if after[key] != before[key]]
        if changed:
            conn.execute(
                'UPDATE PolicyRules SET name = ?, config = ?, is_active = ? WHERE id = ? AND organization_id = ?',
                (after['name'], json.dumps(after['config']), int(after['is_active']), rule['id'], g.user['organization_id'])
            )
            record_event(conn, 'policy_rule.updated', organization_id=g.user['organization_id'], actor=g.user['email'],
                         details={'rule_id': rule['id'], 'from': {key: before[key] for key in changed},
                                  'to': {key: after[key] for key in changed}})
            conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS'})


# ==========================================
# 3. STATIC FILE ROUTING (FRONTEND)
# ==========================================
@app.route('/<path:filename>')
def serve_static(filename):
    # Security: Only allow serving specific safe extensions to prevent directory traversal
    allowed_extensions = {'.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.json'}
    ext = os.path.splitext(filename)[1].lower()
    
    if ext in allowed_extensions and os.path.exists(filename):
        return send_from_directory('.', filename)
    return "Not Found or Access Denied", 404

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/join/<code>')
def join_page(code):
    return redirect(f"/index.html?join={quote(code, safe='')}")


# ==========================================
# 4. MACHINE LEARNING API ROUTES
# ==========================================
TYPICAL_AMOUNT_SAMPLE = 200

def organization_typical_amount(conn, organization_id):
    """What a normal request costs in this organization, so the AI can judge a size against their own work."""
    rows = conn.execute(
        'SELECT normalized_amount FROM Requests WHERE organization_id = ? AND normalized_amount IS NOT NULL '
        'ORDER BY id DESC LIMIT ?',
        (organization_id, TYPICAL_AMOUNT_SAMPLE)
    ).fetchall()
    if len(rows) < model_pipeline.TYPICAL_AMOUNT_MIN_ROWS:
        return None  # Too new to have a normal of their own; the model falls back to the base data.
    return model_pipeline.typical_amount([row['normalized_amount'] for row in rows])


@app.route('/api/predict', methods=['POST'])
@limiter.limit("20 per minute")
@require_login
def predict():
    artifacts, model_version_id = get_active_model()
    if artifacts is None:
        return jsonify({
            'error': 'The AI model is currently unavailable. Please try again later.',
            'status': 'ESCALATED_SYSTEM_ERROR'
        }), 503

    try:
        current_email = get_jwt_identity()
        # The employee portal sends a form so receipt files travel with the request; other clients may send JSON.
        data = request.form.to_dict() if request.mimetype == 'multipart/form-data' else (request.get_json(silent=True) or {})
        xgb_model = artifacts['xgboost_model']
        iso_forest = artifacts['isolation_forest']
        oc_svm = artifacts['one_class_svm']
        
        # Extract inputs
        role = data.get('Role')
        department = data.get('Department')
        req_type = data.get('Request_Type')
        destination = data.get('Destination')
        amount = data.get('Amount')
        currency = data.get('Currency')
        employee_name = str(data.get('Employee_Name') or '').strip()[:100] or None
        employee_id = str(data.get('Employee_ID') or '').strip()[:50] or None

        required_fields = ['Role', 'Department', 'Request_Type', 'Destination', 'Currency']
        if not all(data.get(f) for f in required_fields):
            return jsonify({'error': 'Missing required fields.'}), 400

        if currency not in exchange_rates:
            return jsonify({'error': 'Unsupported currency.'}), 400
            
        try:
            amount = float(amount)
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({'error': 'Amount must be a positive number.'}), 400

        purpose = str(data.get('Purpose') or '').strip() if isinstance(data.get('Purpose'), (str, type(None))) else ''
        if not PURPOSE_MIN_LENGTH <= len(purpose) <= PURPOSE_MAX_LENGTH:
            return jsonify({'error': f'Please describe the business purpose in {PURPOSE_MIN_LENGTH} to {PURPOSE_MAX_LENGTH} characters.'}), 400
        expense_date, end_date, date_error = parse_request_dates(data.get('Expense_Date'), data.get('End_Date'))
        if date_error:
            return jsonify({'error': date_error}), 400
        receipts, receipt_error = read_receipt_uploads(request.files.getlist('receipts'))
        if receipt_error:
            return jsonify({'error': receipt_error}), 400

        # Normalize Amount to INR
        rate = exchange_rates.get(currency, 1.0)
        normalized_inr = amount * rate

        conn = get_db_connection()
        # Training and scoring build the model's columns in the same one place, so they cannot drift apart.
        X_input, unknown_columns = model_pipeline.prepare_request(artifacts, {
            'Role': role, 'Department': department, 'Request_Type': req_type,
            'Destination': destination, 'Amount_INR': normalized_inr,
            'typical_amount': organization_typical_amount(conn, g.user['organization_id']),
        })
        is_unknown_category = bool(unknown_columns)

        # XGBoost Probabilities
        xgb_prob = float(xgb_model.predict_proba(X_input)[0][1])
        
        # Anomaly Detection
        iso_pred = int(iso_forest.predict(X_input)[0])
        svm_pred = int(oc_svm.predict(X_input)[0])
        is_severe_anomaly = (iso_pred == -1) or (svm_pred == -1)

        # SHAP values show administrators which fields pushed the score up or down.
        shap_values = artifacts['shap_explainer'].shap_values(X_input)
        shap_impact = dict(zip(X_input.columns, [float(v) for v in shap_values[0]]))
        
        auto_approve_above = organization_auto_approve_threshold(g.user)

        # Decision Routing Logic (Confidence Based Triage)
        if is_unknown_category:
            status = "ESCALATED_UNKNOWN"
        elif is_severe_anomaly:
            status = "ESCALATED_ANOMALY"
        elif xgb_prob > auto_approve_above:
            status = "APPROVED"
        elif xgb_prob < ESCALATE_THRESHOLD:
            status = "ESCALATED_POLICY"
        else:
            status = "ESCALATED_MANUAL_REVIEW"

        # In shadow mode the AI only recommends, so a request it would approve still waits for a person.
        # Any mode other than an explicit "automatic" is treated as shadow, so a bad value never auto-approves.
        ai_decision = status
        approval_mode = 'automatic' if g.user['approval_mode'] == 'automatic' else 'shadow'
        if approval_mode == 'shadow' and ai_decision == 'APPROVED':
            status = 'ESCALATED_SHADOW'

        # Company policy rules are checked before anything is saved. A broken rule always sends the request to a
        # person; rules never approve or reject on their own, and the AI's own decision is still recorded.
        violations = policy_violations(conn, g.user['organization_id'], current_email, {
            'role': role, 'department': department, 'request_type': req_type, 'destination': destination,
            'amount': amount, 'currency': currency, 'amount_inr': normalized_inr, 'receipt_count': len(receipts),
        })
        if violations:
            status = 'ESCALATED_RULE'
        # A large request always needs two people, so it never passes on the AI's word alone.
        if status == 'APPROVED' and needs_second_approval(g.user, normalized_inr):
            status = 'ESCALATED_HIGH_VALUE'
        # A request that needs a person goes to the employee's manager first; without a manager, the admins decide.
        approver_email = g.user['manager_email'] if status.startswith('ESCALATED') else None
        request_id = conn.execute(
            '''INSERT INTO Requests (
                role, department, request_type, destination, amount, currency, 
                normalized_amount, xgb_score, iso_score, svm_score, risk_score, 
                final_decision, submitted_by, employee_name, employee_id, organization_id, ai_decision, approval_mode,
                policy_violations, purpose, expense_date, end_date, approver_email
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id''',
            (role, department, req_type, destination, amount, currency,
             normalized_inr, xgb_prob, iso_pred, svm_pred, (1 - xgb_prob)*100,
             status, current_email, employee_name, employee_id, g.user['organization_id'], ai_decision, approval_mode,
             json.dumps(violations) if violations else None, purpose, expense_date, end_date, approver_email)
        ).fetchall()[0][0]
        record_event(
            conn, 'request.submitted', organization_id=g.user['organization_id'], request_id=request_id,
            actor=current_email, to_status=status, details={
                'model_version': model_version_id,
                'approval_mode': approval_mode,
                'ai_recommendation': ai_decision,
                'policy_violations': violations,
                'approver': approver_email,
                'approval_score': round(xgb_prob, 4),
                'unrecognized_category': is_unknown_category,
                'anomaly_detectors': {'isolation_forest': iso_pred == -1, 'one_class_svm': svm_pred == -1},
                'thresholds': {'auto_approve_above': auto_approve_above, 'escalate_below': ESCALATE_THRESHOLD,
                               'second_approval_above': organization_second_approval_amount(g.user)},
                'explanation': shap_impact,
            }
        )
        if status == 'APPROVED' and approval_mode == 'automatic':
            spot_check_percent = organization_spot_check_percent(g.user)
            if spot_check_selected(spot_check_percent):
                conn.execute(
                    'INSERT INTO SpotChecks (organization_id, request_id) VALUES (?, ?)', (g.user['organization_id'], request_id)
                )
                record_event(conn, 'spotcheck.sampled', organization_id=g.user['organization_id'], request_id=request_id,
                             details={'percent': spot_check_percent})
        save_receipts(conn, g.user['organization_id'], request_id, current_email, receipts)
        # Whoever must decide is told once the request is safely saved.
        send_notice = waiting_notice(conn, g.user['organization_id'], request_id, approver_email) if status.startswith('ESCALATED') else None
        conn.commit()
        conn.close()
        if send_notice:
            send_notice()

        # Employees only learn the outcome. Scores and escalation reasons stay with administrators,
        # so nobody can map the model's boundaries by resubmitting variations of a request.
        approved = status == 'APPROVED'
        return jsonify({
            'status': 'APPROVED' if approved else 'PENDING_REVIEW',
            'message': 'Your request was approved.' if approved else 'Your request was sent to an administrator for review.',
            'normalized_inr': normalized_inr,
        })

    except HTTPException:
        raise  # For example an upload over the size limit, answered by its own handler.
    except Exception:
        app.logger.exception("Prediction failed")
        return jsonify({
            'error': 'An internal system error occurred during AI processing.',
            'status': 'ESCALATED_SYSTEM_ERROR',
            'message': 'An internal system error occurred during AI processing.'
        }), 500


@app.route('/api/platform/model/info', methods=['GET'])
@require_platform_owner
def model_info():
    artifacts, version_id = get_active_model()
    if artifacts is None:
        return jsonify({'ready': False})

    conn = get_db_connection()
    try:
        decided = trainable_decision_count(conn)
    finally:
        conn.close()

    metrics = artifacts.get('metrics') or {}
    return jsonify({
        'ready': True,
        'version_id': version_id,
        'load_problem': (
            f"Version {_model_state['unloadable_version_id']} is marked active but could not be loaded, so the original model "
            'is scoring requests. Switch to a working version or retrain the model.'
        ) if _model_state['unloadable_version_id'] is not None else None,
        'components': [
            {'name': 'XGBoost classifier', 'purpose': 'Scores how likely a request is to be approved'},
            {'name': 'Isolation Forest', 'purpose': 'Flags requests with unusual patterns'},
            {'name': 'One-Class SVM', 'purpose': 'Second anomaly detector for unusual requests'},
            {'name': 'SHAP explainer', 'purpose': 'Explains which fields drove each score'},
        ],
        'features': list(artifacts['features']),
        'vocabulary': {col: len(artifacts['encoders'][col].classes_) for col in model_pipeline.CATEGORICAL_FEATURES},
        'thresholds': {'auto_approve_above': AUTO_APPROVE_THRESHOLD, 'escalate_below': ESCALATE_THRESHOLD},
        'trained_at': artifacts.get('trained_at') or bundled_file_timestamp(),
        'metrics': metrics,
        'new_decisions_since_training': max(0, int(decided) - int(metrics.get('feedback_rows', 0))),
    })


REQUEST_FEATURE_COLUMNS = dict(zip(model_pipeline.CATEGORICAL_FEATURES, ('role', 'department', 'request_type', 'destination')))

@app.route('/api/model/form_options', methods=['GET'])
@require_login
def model_form_options():
    artifacts, _ = get_active_model()
    if artifacts is None:
        return jsonify({'error': 'The AI model is currently unavailable. Please try again later.'}), 503

    options = {col: set(values) for col, values in model_pipeline.standard_form_options(artifacts).items()}

    # Beyond the shared base list, offer only values this organization has used and the model has learned,
    # so one organization's own roles, departments or destinations never appear in another's form.
    conn = get_db_connection()
    try:
        used = conn.execute(
            'SELECT DISTINCT role, department, request_type, destination FROM Requests WHERE organization_id = ?',
            (g.user['organization_id'],)
        ).fetchall()
    finally:
        conn.close()
    for feature, column in REQUEST_FEATURE_COLUMNS.items():
        learned = set(map(str, artifacts['encoders'][feature].classes_))
        options[feature].update(str(row[column]) for row in used if row[column] is not None and str(row[column]) in learned)

    return jsonify({'options': {col: sorted(values) for col, values in options.items()}, 'currencies': list(exchange_rates)})


def utc_now_text():
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

def update_training_job(job_id, **fields):
    assignments = ', '.join(f'{name} = ?' for name in fields)
    conn = get_db_connection()
    conn.execute(f'UPDATE TrainingJobs SET {assignments} WHERE id = ?', (*fields.values(), job_id))
    conn.commit()
    conn.close()

def training_job_to_json(row):
    job = to_json_row(row)
    job['metrics'] = json.loads(job['metrics']) if job.get('metrics') else None
    if job['status'] == 'running' and datetime.utcnow() - as_datetime(row['started_at']) > TRAINING_JOB_STALE_AFTER:
        job['status'] = 'failed'
        job['message'] = 'Retraining was interrupted, most likely by a server restart. Please try again.'
    return job

# The model only learns from requests an administrator decided by hand, in organizations that agreed to share their data.
TRAINABLE_DECISIONS_SQL = (
    "FROM Requests JOIN Organizations ON Organizations.id = Requests.organization_id "
    "WHERE Requests.reviewed_by IS NOT NULL AND Requests.final_decision IN ('APPROVED', 'REJECTED') "
    "AND Organizations.allow_training_data = 1"
)

# A spot check is a person's verdict on an approval nobody else ever saw. Without these the model only ever
# learns from the requests that were hard enough to reach a person, which is not what it decides on.
SPOT_CHECKED_APPROVALS_SQL = (
    "FROM SpotChecks JOIN Requests ON Requests.id = SpotChecks.request_id "
    "JOIN Organizations ON Organizations.id = Requests.organization_id "
    "WHERE SpotChecks.verdict IS NOT NULL AND Requests.reviewed_by IS NULL "
    "AND Requests.final_decision = 'APPROVED' AND Organizations.allow_training_data = 1"
)
REQUEST_TRAINING_COLUMNS = (
    "Requests.id, Requests.role, Requests.department, Requests.request_type, Requests.destination, "
    "Requests.normalized_amount, Requests.organization_id"
)

def trainable_decisions(conn):
    rows = conn.execute(
        f"SELECT {REQUEST_TRAINING_COLUMNS}, Requests.final_decision {TRAINABLE_DECISIONS_SQL}"
    ).fetchall()
    # An approval marked wrong in a spot check teaches the model exactly what it got wrong.
    checked = conn.execute(
        f"SELECT {REQUEST_TRAINING_COLUMNS}, "
        "CASE WHEN SpotChecks.verdict = 'WRONG' THEN 'REJECTED' ELSE 'APPROVED' END AS final_decision "
        f"{SPOT_CHECKED_APPROVALS_SQL}"
    ).fetchall()
    return [dict(row) for row in rows] + [dict(row) for row in checked]

def trainable_decision_count(conn):
    return sum(
        conn.execute(f'SELECT COUNT(*) AS total {clause}').fetchone()['total']
        for clause in (TRAINABLE_DECISIONS_SQL, SPOT_CHECKED_APPROVALS_SQL)
    )

def run_training_job(job_id, started_by):
    try:
        update_training_job(job_id, step='collecting')
        current, _ = get_active_model(force=True)
        base = model_pipeline.base_training_data(current, BASE_TRAINING_CSV)
        conn = get_db_connection()
        try:
            decisions = trainable_decisions(conn)
        finally:
            conn.close()
        feedback = model_pipeline.feedback_training_data(decisions)

        update_training_job(job_id, step='training')
        candidate, holdout = model_pipeline.train_ensemble(base, feedback)

        update_training_job(job_id, step='evaluating')
        comparison = model_pipeline.compare_with_current(candidate, holdout, current)
        metrics = dict(candidate['metrics'])
        metrics['previous_roc_auc'] = (comparison['current'] or {}).get('roc_auc')
        candidate['metrics'] = metrics
        new_score, old_score = metrics['roc_auc'], metrics['previous_roc_auc']

        # A holdout with only one kind of answer gives no score, and the message must not fail over that.
        new_text = f"scored {new_score:.1%}" if new_score is not None else "could not be scored"
        if not comparison['accepted']:
            update_training_job(
                job_id, status='rejected', step='done', metrics=json.dumps(metrics), finished_at=utc_now_text(),
                message=f"The retrained model {new_text} while the current model scores {old_score:.1%}, so the current model stays active."
            )
            return

        update_training_job(job_id, step='publishing')
        buffer = io.BytesIO()
        joblib.dump(candidate, buffer)
        conn = get_db_connection()
        version_id = conn.execute(
            'INSERT INTO ModelVersions (artifact, metrics, created_by, is_active) VALUES (?, ?, ?, 0) RETURNING id',
            (buffer.getvalue(), json.dumps(metrics), started_by)
        ).fetchall()[0][0]
        conn.execute('UPDATE ModelVersions SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END', (version_id,))
        conn.execute(
            'DELETE FROM ModelVersions WHERE is_active = 0 AND id NOT IN (SELECT id FROM ModelVersions ORDER BY id DESC LIMIT ?)',
            (MODEL_VERSIONS_KEPT,)
        )
        conn.commit()
        conn.close()
        get_active_model(force=True)

        previous_text = f" (previous model: {old_score:.1%})" if old_score is not None else ""
        score_text = f" with a quality score of {new_score:.1%}" if new_score is not None else ""
        update_training_job(
            job_id, status='succeeded', step='done', metrics=json.dumps(metrics), model_version_id=version_id,
            finished_at=utc_now_text(),
            message=f"Version {version_id} is now scoring new requests{score_text}{previous_text}."
        )
    except model_pipeline.TrainingDataMissing as exc:
        update_training_job(job_id, status='failed', step='done', message=str(exc), finished_at=utc_now_text())
    except Exception:
        app.logger.exception("Model retraining failed")
        update_training_job(
            job_id, status='failed', step='done', finished_at=utc_now_text(),
            message='Retraining failed because of a server error. The current model is still active.'
        )


@app.route('/api/platform/model/retrain', methods=['POST'])
@limiter.limit("10 per hour")
@require_platform_owner
def retrain_model():
    started_by = get_jwt_identity()
    conn = get_db_connection()
    latest = conn.execute('SELECT * FROM TrainingJobs ORDER BY id DESC LIMIT 1').fetchone()
    if latest and latest['status'] == 'running':
        if training_job_to_json(latest)['status'] == 'running':
            conn.close()
            return jsonify({'error': 'A retraining job is already running.'}), 409
        conn.execute(
            "UPDATE TrainingJobs SET status = 'failed', step = 'done', message = ? WHERE id = ?",
            ('Retraining was interrupted, most likely by a server restart.', latest['id'])
        )

    job_id = conn.execute(
        "INSERT INTO TrainingJobs (status, step, started_by) VALUES ('running', 'queued', ?) RETURNING id",
        (started_by,)
    ).fetchall()[0][0]
    record_event(conn, 'model.retrain_started', actor=started_by, details={'job_id': job_id})
    conn.commit()
    conn.close()

    threading.Thread(target=run_training_job, args=(job_id, started_by), daemon=True).start()
    return jsonify({'status': 'STARTED', 'job_id': job_id}), 202


@app.route('/api/platform/model/jobs/latest', methods=['GET'])
@require_platform_owner
def latest_training_job():
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM TrainingJobs ORDER BY id DESC LIMIT 1').fetchone()
    conn.close()
    return jsonify({'job': training_job_to_json(row) if row else None})


@app.route('/api/platform/model/versions', methods=['GET'])
@require_platform_owner
def model_versions():
    conn = get_db_connection()
    rows = conn.execute('SELECT id, metrics, created_by, is_active, created_at FROM ModelVersions ORDER BY id DESC').fetchall()
    conn.close()

    versions = []
    for row in rows:
        version = to_json_row(row)
        version['metrics'] = json.loads(version['metrics']) if version.get('metrics') else None
        version['is_active'] = bool(version['is_active'])
        versions.append(version)

    try:
        bundled = load_bundled_artifacts()
        bundled_info = {'available': True, 'trained_at': bundled.get('trained_at') or bundled_file_timestamp(), 'metrics': bundled.get('metrics')}
    except Exception:
        bundled_info = {'available': False, 'trained_at': None, 'metrics': None}
    bundled_info['is_active'] = not any(v['is_active'] for v in versions)
    return jsonify({'versions': versions, 'bundled': bundled_info})


@app.route('/api/platform/model/activate', methods=['POST'])
@require_platform_owner
def activate_model_version():
    data = request.get_json(silent=True) or {}
    version_id = data.get('version_id')
    if version_id is not None and (isinstance(version_id, bool) or not isinstance(version_id, int)):
        return jsonify({'error': 'version_id must be a model version number, or null for the original model.'}), 400

    conn = get_db_connection()
    if version_id is None:
        try:
            load_bundled_artifacts()
        except Exception:
            conn.close()
            return jsonify({'error': 'The original model file could not be loaded.'}), 409
        conn.execute('UPDATE ModelVersions SET is_active = 0')
    else:
        if not conn.execute('SELECT id FROM ModelVersions WHERE id = ?', (version_id,)).fetchone():
            conn.close()
            return jsonify({'error': 'Model version not found.'}), 404
        conn.execute('UPDATE ModelVersions SET is_active = CASE WHEN id = ? THEN 1 ELSE 0 END', (version_id,))
    record_event(conn, 'model.activated', actor=g.user['email'], details={'version_id': version_id})
    conn.commit()
    conn.close()

    get_active_model(force=True)
    return jsonify({'status': 'SUCCESS'})


# ==========================================
# PLATFORM (NEUZEM) API ROUTES
# ==========================================
ORGANIZATION_STATUSES = ('Active', 'Paused')

@app.route('/api/platform/organizations', methods=['GET'])
@require_platform_owner
def platform_organizations():
    # Neuzem sees each organization's settings, size and Super Admin contacts, never its requests or employee records.
    conn = get_db_connection()
    try:
        organizations = conn.execute(
            "SELECT id, name, status, allow_training_data, is_default, created_by, created_at, approval_mode, auto_approve_above, "
            "(SELECT COUNT(*) FROM Users WHERE Users.organization_id = Organizations.id AND Users.status = 'Active') AS active_users, "
            "(SELECT COUNT(*) FROM Requests WHERE Requests.organization_id = Organizations.id) AS requests "
            "FROM Organizations ORDER BY is_default DESC, name"
        ).fetchall()
        super_admins = conn.execute("SELECT organization_id, email, status FROM Users WHERE role = 'SuperAdmin' ORDER BY email").fetchall()
    finally:
        conn.close()

    contacts = {}
    for admin in super_admins:
        contacts.setdefault(admin['organization_id'], []).append({'email': admin['email'], 'status': admin['status']})

    result = []
    for row in organizations:
        organization = to_json_row(row)
        organization['allow_training_data'] = bool(organization['allow_training_data'])
        organization['is_default'] = bool(organization['is_default'])
        organization['auto_approve_above'] = organization_auto_approve_threshold(organization)
        organization['super_admins'] = contacts.get(row['id'], [])
        result.append(organization)
    return jsonify({'organizations': result})

@app.route('/api/platform/create_organization', methods=['POST'])
@limiter.limit("30 per hour")
@require_platform_owner
def platform_create_organization():
    data = request.get_json(silent=True) or {}
    name = ' '.join(str(data.get('name') or '').split())
    email = str(data.get('super_admin_email') or '').strip()
    allow_training_data = data.get('allow_training_data', False)

    if not 2 <= len(name) <= 100:
        return jsonify({'error': 'Organization name must be 2 to 100 characters.'}), 400
    if not is_valid_email(email):
        return jsonify({'error': "Please enter a valid email address for the organization's Super Admin."}), 400
    if not isinstance(allow_training_data, bool):
        return jsonify({'error': 'allow_training_data must be true or false.'}), 400

    conn = get_db_connection()
    try:
        if conn.execute('SELECT id FROM Organizations WHERE LOWER(name) = LOWER(?)', (name,)).fetchone():
            return jsonify({'error': 'An organization with this name already exists.'}), 409
        if conn.execute('SELECT id FROM Users WHERE email = ?', (email,)).fetchone():
            return jsonify({'error': 'This email already has an account. Each email can belong to only one organization.'}), 409

        organization_id = conn.execute(
            # New organizations start in shadow mode: the AI only recommends until Neuzem switches them to automatic.
            'INSERT INTO Organizations (name, join_code, allow_training_data, created_by, approval_mode) VALUES (?, ?, ?, ?, ?) RETURNING id',
            (name, secrets.token_urlsafe(9), int(allow_training_data), g.user['email'], 'shadow')
        ).fetchall()[0][0]
        conn.execute(
            'INSERT INTO Users (email, role, status, organization_id) VALUES (?, ?, ?, ?)',
            (email, 'SuperAdmin', 'Approved_Awaiting_Password', organization_id)
        )
        record_event(conn, 'organization.created', organization_id=organization_id, actor=g.user['email'],
                     details={'name': name, 'super_admin_email': email, 'allow_training_data': allow_training_data,
                              'approval_mode': 'shadow'})
        conn.commit()
        # The setup link goes only to the Super Admin's inbox, so Neuzem never knows their password.
        setup_link = f"{public_base_url()}/index.html?setup_token={issue_token(conn, email, SETUP_TOKEN_TTL)}"
    except DB_INTEGRITY_ERRORS:
        return jsonify({'error': 'This organization or email was just added. Refresh the list and try again.'}), 409
    finally:
        conn.close()

    email_service.sendOrganizationCreatedEmail(email, name, setup_link)
    return jsonify({
        'status': 'SUCCESS',
        'organization_id': organization_id,
        'message': f'{name} was created. A setup link was emailed to {email}.',
    }), 201

@app.route('/api/platform/decision_quality', methods=['GET'])
@require_platform_owner
def platform_decision_quality():
    # Neuzem sees how well an organization's decisions are going, never the requests behind them.
    organization_id = request.args.get('organization_id', type=int)
    days = request.args.get('days', type=int) or AUTOMATIC_APPROVAL_WINDOW
    if days not in QUALITY_WINDOWS:
        return jsonify({'error': 'Choose one of these windows: ' + ', '.join(f'{window} days' for window in QUALITY_WINDOWS) + '.'}), 400

    conn = get_db_connection()
    try:
        if not organization_id or not conn.execute('SELECT id FROM Organizations WHERE id = ?', (organization_id,)).fetchone():
            return jsonify({'error': 'Organization not found.'}), 404
        quality = decision_quality(conn, organization_id, days)
        monitoring = weekly_monitoring(conn, organization_id)
    finally:
        conn.close()
    return jsonify({
        **quality, 'readiness': automatic_approval_readiness(quality), 'drift_warnings': monitoring['warnings'],
    })

# ==========================================
# CLOSING AN ORGANIZATION
# ==========================================
# When a company leaves, everything that names a person goes: accounts, receipts, purposes, comments and the
# history. What stays is the shape of their decisions with nobody's name on it, which is what the AI learns from
# and which their agreement already covers. A company that never agreed to share data is not trained on either
# way, because closing them does not change that permission.
ORGANIZATION_CLOSED = 'Closed'
CLOSED_MARKER = 'closed'

def erase_audit_events(conn, organization_id):
    """Lifts the append-only guard for this one deletion, inside the same transaction that puts it back."""
    if os.getenv('DATABASE_URL'):
        conn.execute('ALTER TABLE AuditEvents DISABLE TRIGGER audit_events_append_only')
        conn.execute('DELETE FROM AuditEvents WHERE organization_id = ?', (organization_id,))
        conn.execute('ALTER TABLE AuditEvents ENABLE TRIGGER audit_events_append_only')
        return
    for name in ('audit_events_no_update', 'audit_events_no_delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS {name}')
    conn.execute('DELETE FROM AuditEvents WHERE organization_id = ?', (organization_id,))
    for statement in SQLITE_AUDIT_TRIGGERS:
        conn.execute(statement)

@app.route('/api/platform/close_organization', methods=['POST'])
@require_platform_owner
def close_organization():
    data = request.get_json(silent=True) or {}
    organization_id = data.get('id')
    if isinstance(organization_id, bool) or not isinstance(organization_id, int):
        return jsonify({'error': 'id must be an organization number.'}), 400

    conn = get_db_connection()
    try:
        organization = conn.execute(
            'SELECT id, name, status, is_default FROM Organizations WHERE id = ?', (organization_id,)
        ).fetchone()
        if not organization:
            return jsonify({'error': 'Organization not found.'}), 404
        if organization['is_default']:
            return jsonify({'error': 'The default organization holds the original accounts and cannot be closed.'}), 409
        if organization['status'] == ORGANIZATION_CLOSED:
            return jsonify({'error': 'This organization is already closed.'}), 409
        confirmation = data.get('name')
        if not isinstance(confirmation, str) or confirmation.strip() != organization['name']:
            return jsonify({'error': "Type the organization's name exactly as it is written to confirm."}), 400

        counts = {
            table.lower(): conn.execute(
                f'SELECT COUNT(*) AS total FROM {table} WHERE organization_id = ?', (organization_id,)
            ).fetchone()['total']
            for table in ('Users', 'Requests', 'Receipts', 'PolicyRules', 'AuditEvents')
        }

        # Everything that names a person.
        conn.execute('DELETE FROM Receipts WHERE organization_id = ?', (organization_id,))
        conn.execute('DELETE FROM PolicyRules WHERE organization_id = ?', (organization_id,))
        conn.execute('DELETE FROM Users WHERE organization_id = ?', (organization_id,))
        conn.execute(
            'UPDATE SpotChecks SET reviewed_by = ?, comment = NULL WHERE organization_id = ? AND reviewed_by IS NOT NULL',
            (CLOSED_MARKER, organization_id)
        )
        conn.execute(
            'UPDATE Requests SET submitted_by = ?, employee_name = NULL, employee_id = NULL, purpose = NULL, '
            'approver_email = NULL, first_approved_by = NULL WHERE organization_id = ?',
            (CLOSED_MARKER, organization_id)
        )
        # Only where somebody really did decide, so an approval the AI made alone still counts as one.
        conn.execute(
            'UPDATE Requests SET reviewed_by = ? WHERE organization_id = ? AND reviewed_by IS NOT NULL',
            (CLOSED_MARKER, organization_id)
        )
        erase_audit_events(conn, organization_id)

        # A new join code kills the old invitation link.
        conn.execute(
            'UPDATE Organizations SET status = ?, join_code = ? WHERE id = ?',
            (ORGANIZATION_CLOSED, secrets.token_urlsafe(9), organization_id)
        )
        # The one line that outlives the history it replaced.
        record_event(conn, 'organization.closed', organization_id=organization_id, actor=g.user['email'], details={
            'accounts_removed': counts['users'], 'receipts_removed': counts['receipts'],
            'rules_removed': counts['policyrules'], 'history_entries_removed': counts['auditevents'],
            'anonymous_requests_kept': counts['requests'],
        })
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS', 'anonymous_requests_kept': counts['requests']})

@app.route('/api/platform/update_organization', methods=['POST'])
@require_platform_owner
def platform_update_organization():
    data = request.get_json(silent=True) or {}
    organization_id = data.get('id')
    if isinstance(organization_id, bool) or not isinstance(organization_id, int):
        return jsonify({'error': 'id must be an organization number.'}), 400

    changes = {}
    if 'status' in data:
        if data['status'] not in ORGANIZATION_STATUSES:
            return jsonify({'error': 'status must be Active or Paused.'}), 400
        changes['status'] = data['status']
    if 'approval_mode' in data:
        if data['approval_mode'] not in APPROVAL_MODES:
            return jsonify({'error': 'approval_mode must be shadow or automatic.'}), 400
        changes['approval_mode'] = data['approval_mode']
    if 'allow_training_data' in data:
        if not isinstance(data['allow_training_data'], bool):
            return jsonify({'error': 'allow_training_data must be true or false.'}), 400
        changes['allow_training_data'] = int(data['allow_training_data'])
    forced = data.get('force', False)
    if not isinstance(forced, bool):
        return jsonify({'error': 'force must be true or false.'}), 400
    if not changes:
        return jsonify({'error': 'Nothing to update.'}), 400

    conn = get_db_connection()
    try:
        organization = conn.execute('SELECT is_default, status FROM Organizations WHERE id = ?', (organization_id,)).fetchone()
        if not organization:
            return jsonify({'error': 'Organization not found.'}), 404
        # A company that has left cannot be reopened or agree to anything new; it can only stop sharing its data.
        if organization['status'] == ORGANIZATION_CLOSED and any(
                column != 'allow_training_data' or value for column, value in changes.items()):
            return jsonify({'error': 'This organization is closed. Its data sharing can be turned off, but nothing else can change.'}), 409
        if organization['is_default'] and changes.get('status') == 'Paused':
            return jsonify({'error': 'The default organization holds the original accounts and cannot be paused.'}), 409
        # An organization only stops using shadow mode once its own decisions show the AI can be trusted.
        readiness = None
        if changes.get('approval_mode') == 'automatic':
            readiness = automatic_approval_readiness(decision_quality(conn, organization_id, AUTOMATIC_APPROVAL_WINDOW))
            if not readiness['ready'] and not forced:
                return jsonify({
                    'error': 'This organization is not ready for automatic approval yet.', 'readiness': readiness,
                }), 409
        # Column names come from the fixed keys above, never from the request.
        assignments = ', '.join(f'{column} = ?' for column in changes)
        conn.execute(f'UPDATE Organizations SET {assignments} WHERE id = ?', (*changes.values(), organization_id))
        details = {'changes': {column: data[column] for column in changes}}
        if readiness is not None:
            details['readiness'] = {'ready': readiness['ready'], 'blocked_by': readiness['blocked_by']}
            if not readiness['ready']:
                details['forced'] = True  # Neuzem overruled the gate, and the audit log says so.
        record_event(conn, 'organization.updated', organization_id=organization_id, actor=g.user['email'], details=details)
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS'})


# ==========================================
# 5. AUTHENTICATION API ROUTES
# ==========================================
@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    password = data.get('password')

    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
    user = conn.execute(
        'SELECT Users.*, Organizations.status AS organization_status FROM Users '
        'LEFT JOIN Organizations ON Organizations.id = Users.organization_id WHERE Users.email = ?',
        (email,)
    ).fetchone()
    conn.close()

    if not user:
        return jsonify({'error': 'User not found. Please request access first.'}), 404

    user_status = user['status']
    
    if user_status == 'Pending':
        return jsonify({'status': 'PENDING', 'message': 'Your account is still pending administrator approval.'}), 403
    elif user_status == 'Rejected':
        return jsonify({'status': 'REJECTED', 'message': 'Your access request was rejected.'}), 403
    elif user_status == 'Approved_Awaiting_Password':
        return jsonify({'status': 'SETUP_REQUIRED', 'message': 'You have been approved! Use the setup link we emailed you to create your password.'}), 200
    elif user_status == 'Active':
        if not password:
            return jsonify({'error': 'Password required.'}), 400
            
        if check_password_hash(user['password_hash'], password):
            if user['role'] != PLATFORM_OWNER_ROLE and user['organization_status'] != 'Active':
                return jsonify({'status': 'PAUSED', 'error': ORGANIZATION_PAUSED_MESSAGE}), 403
            redirect_page = home_page(user['role'])
            resp = jsonify({
                'status': 'SUCCESS',
                'role': user['role'],
                'message': 'Login successful.',
                'redirect': redirect_page
            })
            set_access_cookies(resp, create_session_token(user))
            return resp
        else:
            return jsonify({'error': 'Invalid password'}), 401
    
    return jsonify({'error': 'Unknown status'}), 500


JOIN_LINK_INVALID_MESSAGE = 'This join link is not valid. Ask your company administrator for a current link.'

def active_organization_by_join_code(conn, code):
    if not isinstance(code, str) or not code:
        return None
    return conn.execute("SELECT id, name FROM Organizations WHERE join_code = ? AND status = 'Active'", (code,)).fetchone()

def organization_name_of(email):
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT Organizations.name FROM Users JOIN Organizations ON Organizations.id = Users.organization_id WHERE Users.email = ?',
            (email,)
        ).fetchone()
    finally:
        conn.close()
    return row['name'] if row else None

@app.route('/api/auth/join_info', methods=['GET'])
@limiter.limit("30 per minute")
def join_info():
    conn = get_db_connection()
    try:
        organization = active_organization_by_join_code(conn, request.args.get('code'))
    finally:
        conn.close()
    if not organization:
        return jsonify({'error': JOIN_LINK_INVALID_MESSAGE}), 404
    return jsonify({'organization_name': organization['name']})

# ==========================================
# TAKING YOUR DATA WITH YOU
# ==========================================
# A company must be able to walk away with everything it put in, in a form it can still read
# without this system. Passwords, join links and other organizations' data are never in it.
EXPORT_TABLES = (
    ('users', 'SELECT email, name, emp_id, role, status, manager_email, created_at FROM Users '
              'WHERE organization_id = ? ORDER BY id'),
    ('requests', 'SELECT * FROM Requests WHERE organization_id = ? ORDER BY id'),
    ('receipts', 'SELECT id, request_id, filename, content_type, size, sha256, uploaded_by, created_at FROM Receipts '
                 'WHERE organization_id = ? ORDER BY id'),
    ('history', 'SELECT id, request_id, actor_email, action, from_status, to_status, comment, details, created_at '
                'FROM AuditEvents WHERE organization_id = ? ORDER BY id'),
    ('policy_rules', 'SELECT id, rule_type, name, config, is_active, created_by, created_at FROM PolicyRules '
                     'WHERE organization_id = ? ORDER BY id'),
    ('spot_checks', 'SELECT id, request_id, verdict, reviewed_by, reviewed_at, comment, created_at FROM SpotChecks '
                    'WHERE organization_id = ? ORDER BY id'),
)
EXPORT_ORGANIZATION_SQL = (
    'SELECT id, name, status, is_default, allow_training_data, approval_mode, auto_approve_above, '
    'second_approval_above, spot_check_percent, created_by, created_at FROM Organizations WHERE id = ?'
)
EXPORT_README = """Your data from the Advanced Approval Management System
======================================================

data.json holds everything this system stores about your organization:

  organization   your settings
  users          the people in your organization, without passwords
  requests       every request with its decision and scores
  receipts       what each receipt file is; the files themselves are in the receipts folder
  history        who did what and when, in order
  policy_rules   your company rules
  spot_checks    the answers your administrators gave on automatic approvals

The receipts folder holds the original files exactly as they were uploaded.
Nothing here belongs to any other organization, and no passwords or login links are included.
"""

def export_file_name(organization_name):
    slug = re.sub(r'[^A-Za-z0-9]+', '-', organization_name or 'organization').strip('-').lower() or 'organization'
    return f"{slug}-export-{datetime.utcnow().strftime('%Y-%m-%d')}.zip"

@app.route('/api/auth/export', methods=['GET'])
@limiter.limit("3 per hour")
@require_role('SuperAdmin')
def export_organization():
    organization_id = g.user['organization_id']
    conn = get_db_connection()
    try:
        organization = conn.execute(EXPORT_ORGANIZATION_SQL, (organization_id,)).fetchone()
        data = {
            'exported_at': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'exported_by': g.user['email'],
            'organization': to_json_row(organization),
        }
        for name, sql in EXPORT_TABLES:
            data[name] = [to_json_row(row) for row in conn.execute(sql, (organization_id,)).fetchall()]

        # Written to a temporary file rather than held in memory, so a company with many receipts is fine.
        archive = tempfile.TemporaryFile()
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr('README.txt', EXPORT_README)
            bundle.writestr('data.json', json.dumps(data, indent=2, default=str))
            files = conn.execute(
                'SELECT id, filename, content FROM Receipts WHERE organization_id = ? ORDER BY id', (organization_id,)
            ).fetchall()
            for receipt in files:
                bundle.writestr(f"receipts/{receipt['id']}-{secure_filename(receipt['filename'])}", bytes(receipt['content']))

        record_event(conn, 'organization.exported', organization_id=organization_id, actor=g.user['email'],
                     details={name: len(data[name]) for name, _ in EXPORT_TABLES})
        conn.commit()
    finally:
        conn.close()

    archive.seek(0)
    response = send_file(
        archive, mimetype='application/zip', as_attachment=True,
        download_name=export_file_name(data['organization']['name']), etag=False
    )
    response.headers['Cache-Control'] = 'private, no-store'
    return response

@app.route('/api/auth/organization', methods=['GET'])
@require_login
def current_organization():
    conn = get_db_connection()
    try:
        organization = conn.execute('SELECT name, join_code FROM Organizations WHERE id = ?', (g.user['organization_id'],)).fetchone()
    finally:
        conn.close()

    # Admins approve access requests, so only they hand out the join link.
    is_admin = g.user['role'] in ('Admin', 'SuperAdmin')
    body = {
        'name': organization['name'],
        'join_link': f"{public_base_url()}/join/{quote(organization['join_code'], safe='')}" if is_admin else None,
    }
    # Employees never see the thresholds, so nobody can tune requests to slip past them.
    if is_admin:
        body['approval_settings'] = {
            'approval_mode': g.user['approval_mode'],
            'auto_approve_above': organization_auto_approve_threshold(g.user),
            'minimum_auto_approve_above': AUTO_APPROVE_THRESHOLD,
            'maximum_auto_approve_above': AUTO_APPROVE_THRESHOLD_MAX,
            'second_approval_above': organization_second_approval_amount(g.user),
            'maximum_second_approval_above': SECOND_APPROVAL_MAX_INR,
            'spot_check_percent': organization_spot_check_percent(g.user),
            'minimum_spot_check_percent': SPOT_CHECK_MIN_PERCENT,
            'maximum_spot_check_percent': SPOT_CHECK_MAX_PERCENT,
        }
    return jsonify(body)

@app.route('/api/auth/update_approval_settings', methods=['POST'])
@require_role('SuperAdmin')
def update_approval_settings():
    data = request.get_json(silent=True) or {}
    if 'approval_mode' in data:
        return jsonify({'error': 'Only Neuzem can switch an organization between shadow and automatic approval.'}), 403

    changes = {}
    if 'auto_approve_above' in data:
        value = data['auto_approve_above']
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not AUTO_APPROVE_THRESHOLD <= value <= AUTO_APPROVE_THRESHOLD_MAX):
            return jsonify({
                'error': f'The auto-approval threshold must be between {AUTO_APPROVE_THRESHOLD:.0%} and {AUTO_APPROVE_THRESHOLD_MAX:.0%}.'
            }), 400
        changes['auto_approve_above'] = (organization_auto_approve_threshold(g.user), round(float(value), 4))

    if 'second_approval_above' in data:
        # None turns the second approval off; any amount from ₹1 upwards turns it on.
        value = data['second_approval_above']
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not 1 <= value <= SECOND_APPROVAL_MAX_INR):
            return jsonify({
                'error': f'The second-approval amount must be between ₹1 and ₹{SECOND_APPROVAL_MAX_INR:,.0f}, or empty to turn it off.'
            }), 400
        changes['second_approval_above'] = (organization_second_approval_amount(g.user),
                                            None if value is None else round(float(value), 2))

    if 'spot_check_percent' in data:
        value = data['spot_check_percent']
        if (isinstance(value, bool) or not isinstance(value, int)
                or not SPOT_CHECK_MIN_PERCENT <= value <= SPOT_CHECK_MAX_PERCENT):
            return jsonify({
                'error': f'The share of approvals to check must be a whole number between {SPOT_CHECK_MIN_PERCENT}% and {SPOT_CHECK_MAX_PERCENT}%.'
            }), 400
        changes['spot_check_percent'] = (organization_spot_check_percent(g.user), value)

    if not changes:
        return jsonify({'error': 'There is nothing to change.'}), 400

    conn = get_db_connection()
    try:
        for column, (previous, value) in changes.items():
            conn.execute(f'UPDATE Organizations SET {column} = ? WHERE id = ?', (value, g.user['organization_id']))
        record_event(conn, 'settings.updated', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={column: {'from': previous, 'to': value} for column, (previous, value) in changes.items()})
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS', **{column: value for column, (_, value) in changes.items()}})

@app.route('/api/auth/set_manager', methods=['POST'])
@require_role('Admin')
def set_manager():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    manager_email = data.get('manager_email') or None
    if not isinstance(email, str) or (manager_email is not None and not isinstance(manager_email, str)):
        return jsonify({'error': 'email and manager_email must be email addresses.'}), 400
    if manager_email == email:
        return jsonify({'error': 'An employee cannot be their own manager.'}), 400

    organization_id = g.user['organization_id']
    conn = get_db_connection()
    try:
        employee = conn.execute(
            'SELECT email, role, manager_email FROM Users WHERE email = ? AND organization_id = ?', (email, organization_id)
        ).fetchone()
        if not employee:
            return jsonify({'error': 'User not found.'}), 404
        # Admins manage employees' reporting lines; only Super Admins change an administrator's manager.
        if employee['role'] != 'User' and g.user['role'] != 'SuperAdmin':
            return jsonify({'error': "Only a Super Admin can change an administrator's manager."}), 403

        if manager_email:
            manager = conn.execute(
                "SELECT email FROM Users WHERE email = ? AND organization_id = ? AND status = 'Active'", (manager_email, organization_id)
            ).fetchone()
            if not manager:
                return jsonify({'error': 'The manager must be an active account in your organization.'}), 404
            # Walk up from the new manager: reaching the employee again would make a reporting loop.
            current, seen = manager_email, set()
            while current and current not in seen:
                if current == email:
                    return jsonify({'error': 'This would make a loop where people manage each other.'}), 409
                seen.add(current)
                row = conn.execute('SELECT manager_email FROM Users WHERE email = ? AND organization_id = ?', (current, organization_id)).fetchone()
                current = row['manager_email'] if row else None

        if employee['manager_email'] != manager_email:
            conn.execute('UPDATE Users SET manager_email = ? WHERE email = ? AND organization_id = ?', (manager_email, email, organization_id))
            # Requests already waiting for a decision move to the new manager, or to the admins when there is none.
            # A request already waiting for its second approval stays with the administrators.
            conn.execute(
                "UPDATE Requests SET approver_email = ? WHERE submitted_by = ? AND organization_id = ? "
                "AND final_decision LIKE 'ESCALATED%' AND final_decision != 'ESCALATED_SECOND_APPROVAL'",
                (manager_email, email, organization_id)
            )
            record_event(conn, 'user.manager_changed', organization_id=organization_id, actor=g.user['email'],
                         details={'email': email, 'from': employee['manager_email'], 'to': manager_email})
            conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/team_requests', methods=['GET'])
@require_login
def team_requests():
    # A manager sees the requests of the people who report to them; approver_email shows which are waiting for them.
    conn = get_db_connection()
    try:
        rows = conn.execute(
            f"SELECT Requests.*, {RECEIPT_COUNT_SQL} FROM Requests "
            "JOIN Users ON Users.email = Requests.submitted_by AND Users.organization_id = Requests.organization_id "
            "WHERE Requests.organization_id = ? AND Users.manager_email = ? ORDER BY Requests.created_at DESC",
            (g.user['organization_id'], g.user['email'])
        ).fetchall()
    finally:
        conn.close()
    return jsonify({'requests': [to_json_row(row) for row in rows]})

SPOT_CHECKS_LISTED = 100

@app.route('/api/auth/spot_checks', methods=['GET'])
@require_role('Admin')
def spot_checks():
    # The approvals nobody saw, picked at random for a person to look at afterwards.
    conn = get_db_connection()
    try:
        rows = conn.execute(
            'SELECT SpotChecks.id, SpotChecks.verdict, SpotChecks.reviewed_by, SpotChecks.reviewed_at, SpotChecks.comment, '
            'SpotChecks.created_at, Requests.id AS request_id, Requests.submitted_by, Requests.employee_name, '
            'Requests.request_type, Requests.destination, Requests.amount, Requests.currency, Requests.purpose, '
            'Requests.expense_date, Requests.end_date, Requests.xgb_score, Requests.final_decision, '
            f'{RECEIPT_COUNT_SQL} '
            'FROM SpotChecks JOIN Requests ON Requests.id = SpotChecks.request_id '
            'WHERE SpotChecks.organization_id = ? ORDER BY SpotChecks.id DESC LIMIT ?',
            (g.user['organization_id'], SPOT_CHECKS_LISTED)
        ).fetchall()
    finally:
        conn.close()
    checks = [to_json_row(row) for row in rows]
    return jsonify({
        'checks': checks,
        'waiting': sum(1 for check in checks if check['verdict'] is None),
        'spot_check_percent': organization_spot_check_percent(g.user),
    })

@app.route('/api/auth/review_spot_check', methods=['POST'])
@require_role('Admin')
def review_spot_check():
    data = request.get_json(silent=True) or {}
    check_id = data.get('id')
    verdict = data.get('verdict')
    if not check_id or verdict not in SPOT_CHECK_VERDICTS:
        return jsonify({'error': 'Say whether the automatic approval was right or wrong.'}), 400
    comment = data.get('comment')
    if comment is not None and not isinstance(comment, str):
        return jsonify({'error': 'The comment must be text.'}), 400
    comment = (comment or '').strip()[:COMMENT_MAX_LENGTH] or None
    if verdict == 'WRONG' and not comment:
        return jsonify({'error': 'Please say what was wrong with this approval. It is saved in the request history.'}), 400

    conn = get_db_connection()
    try:
        check = conn.execute(
            'SELECT id, request_id, verdict FROM SpotChecks WHERE id = ? AND organization_id = ?',
            (check_id, g.user['organization_id'])
        ).fetchone()
        if not check:
            return jsonify({'error': 'Spot check not found.'}), 404
        # Matching the empty verdict stops two administrators checking the same approval at once.
        cursor = conn.execute(
            'UPDATE SpotChecks SET verdict = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP, comment = ? '
            'WHERE id = ? AND organization_id = ? AND verdict IS NULL',
            (verdict, g.user['email'], comment, check['id'], g.user['organization_id'])
        )
        if not cursor.rowcount:
            return jsonify({'error': 'This spot check has already been done.'}), 409
        record_event(conn, 'spotcheck.reviewed', organization_id=g.user['organization_id'], request_id=check['request_id'],
                     actor=g.user['email'], comment=comment, details={'verdict': verdict})
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/decision_quality', methods=['GET'])
@require_role('Admin')
def organization_decision_quality():
    days = request.args.get('days', type=int) or QUALITY_DEFAULT_WINDOW
    if days not in QUALITY_WINDOWS:
        return jsonify({'error': 'Choose one of these windows: ' + ', '.join(f'{window} days' for window in QUALITY_WINDOWS) + '.'}), 400
    conn = get_db_connection()
    try:
        quality = decision_quality(conn, g.user['organization_id'], days)
        # Readiness is judged on the same window as Neuzem's gate, whichever period is on screen.
        gate_quality = quality if days == AUTOMATIC_APPROVAL_WINDOW else decision_quality(
            conn, g.user['organization_id'], AUTOMATIC_APPROVAL_WINDOW)
    finally:
        conn.close()
    return jsonify({**quality, 'readiness': automatic_approval_readiness(gate_quality)})

@app.route('/api/auth/monitoring', methods=['GET'])
@require_role('Admin')
def organization_monitoring():
    conn = get_db_connection()
    try:
        return jsonify(weekly_monitoring(conn, g.user['organization_id']))
    finally:
        conn.close()

@app.route('/api/auth/fairness', methods=['GET'])
@require_role('Admin')
def organization_fairness():
    days = request.args.get('days', type=int) or QUALITY_DEFAULT_WINDOW
    if days not in QUALITY_WINDOWS:
        return jsonify({'error': 'Choose one of these windows: ' + ', '.join(f'{window} days' for window in QUALITY_WINDOWS) + '.'}), 400
    conn = get_db_connection()
    try:
        return jsonify(fairness_report(conn, g.user['organization_id'], days))
    finally:
        conn.close()

@app.route('/api/auth/request_access', methods=['POST'])
@limiter.limit("5 per hour")
def request_access():
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip()
    role = data.get('role', 'User')

    if not email:
        return jsonify({'error': 'Email is required'}), 400

    if not is_valid_email(email):
        return jsonify({'error': 'Please enter a valid email address.'}), 400
        
    if role not in ['User', 'Admin']:
        return jsonify({'error': 'Invalid role requested.'}), 400

    conn = get_db_connection()
    organization = active_organization_by_join_code(conn, data.get('join_code'))
    if not organization:
        conn.close()
        return jsonify({'error': JOIN_LINK_INVALID_MESSAGE}), 400
    existing_user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
    if existing_user and existing_user['role'] == 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'This email is reserved for Super Admins.'}), 400
    if existing_user:
        conn.close()
        return jsonify({'error': 'Email already exists or is pending.'}), 409
    try:
        conn.execute(
            'INSERT INTO Users (email, role, status, organization_id) VALUES (?, ?, ?, ?)',
            (email, role, 'Pending', organization['id'])
        )
        conn.commit()
    except DB_INTEGRITY_ERRORS:
        return jsonify({'error': 'Email already exists or is pending.'}), 409
    finally:
        conn.close()

    if role == 'Admin':
        conn = get_db_connection()
        super_admins = conn.execute(
            "SELECT email FROM Users WHERE role = 'SuperAdmin' AND status = 'Active' AND organization_id = ?",
            (organization['id'],)
        ).fetchall()
        conn.close()
        for sa in super_admins:
            email_service.sendAdminRegistrationNotification(sa['email'], email, organization['name'])
    else:
        conn = get_db_connection()
        active_admins = conn.execute(
            "SELECT email FROM Users WHERE role IN ('Admin', 'SuperAdmin') AND status = 'Active' AND organization_id = ?",
            (organization['id'],)
        ).fetchall()
        conn.close()
        
        all_notifiers = [a['email'] for a in active_admins]
        
        for notify_email in all_notifiers:
            email_service.sendUserRegistrationNotification(notify_email, email, role, organization['name'])

    return jsonify({'status': 'SUCCESS', 'message': f"Your request to join {organization['name']} was sent. An administrator will review it."})


@app.route('/api/auth/setup_password', methods=['POST'])
@limiter.limit("10 per hour")
def setup_password():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    password = data.get('password')

    if not token or not password:
        return jsonify({'error': 'A valid setup link and a password are required.'}), 400

    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE reset_token = ?', (hash_token(token),)).fetchone()

    if not user or user['status'] != 'Approved_Awaiting_Password' or token_expired(user['reset_expiry']):
        conn.close()
        return jsonify({'error': 'This setup link is invalid or has expired. Try logging in to request a new one.'}), 400

    email = user['email']
    conn.execute(
        'UPDATE Users SET password_hash = ?, status = ?, reset_token = NULL, reset_expiry = NULL WHERE id = ?',
        (generate_password_hash(password), 'Active', user['id'])
    )
    conn.commit()
    conn.close()

    email_service.sendWelcomeEmail(email, email.split('@')[0], organization_name_of(email))

    redirect_page = home_page(user['role'])
    resp = jsonify({
        'status': 'SUCCESS',
        'message': 'Password set successfully. Account is now active.',
        'redirect': redirect_page,
        'role': user['role'],
        'email': email
    })
    set_access_cookies(resp, create_session_token(user))
    return resp

@app.route('/api/auth/pending_users', methods=['GET'])
@require_role('Admin')
def get_pending_users():
    conn = get_db_connection()
    users = conn.execute(
        'SELECT id, email, role, status, created_at FROM Users WHERE status = "Pending" AND organization_id = ?',
        (g.user['organization_id'],)
    ).fetchall()
    conn.close()
    
    users_list = [to_json_row(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/users', methods=['GET'])
@require_role('Admin')
def get_all_users():
    conn = get_db_connection()
    users = conn.execute(
        'SELECT id, email, name, role, status, manager_email, created_at FROM Users WHERE organization_id = ?', (g.user['organization_id'],)
    ).fetchall()
    conn.close()
    
    users_list = [to_json_row(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/delete_user', methods=['POST'])
@require_role('Admin')
def delete_user():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    if email == get_jwt_identity():
        return jsonify({'status': 'ERROR', 'message': 'You cannot delete your own account.'}), 403

    conn = get_db_connection()
    target = conn.execute(
        'SELECT role FROM Users WHERE email = ? AND organization_id = ?', (email, g.user['organization_id'])
    ).fetchone()
    if target and g.user['role'] != 'SuperAdmin' and target['role'] != 'User':
        conn.close()
        return jsonify({'status': 'ERROR', 'message': 'Admins can only remove employee accounts.'}), 403

    cursor = conn.execute('DELETE FROM Users WHERE email = ? AND organization_id = ?', (email, g.user['organization_id']))
    if cursor.rowcount:
        # People who reported to the removed account lose their manager, and requests waiting for it go to the admins.
        conn.execute('UPDATE Users SET manager_email = NULL WHERE manager_email = ? AND organization_id = ?', (email, g.user['organization_id']))
        conn.execute('UPDATE Requests SET approver_email = NULL WHERE approver_email = ? AND organization_id = ?', (email, g.user['organization_id']))
        record_event(conn, 'user.deleted', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={'email': email, 'role': target['role'] if target else None})
    conn.commit()
    conn.close()
    
    if cursor.rowcount > 0:
        return jsonify({'status': 'SUCCESS', 'message': f'User {email} deleted successfully.'})
    else:
        return jsonify({'status': 'ERROR', 'message': f'User {email} not found.'}), 404

@app.route('/api/auth/approve_user', methods=['POST'])
@require_role('Admin')
def approve_user():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
    target = conn.execute(
        'SELECT role FROM Users WHERE email = ? AND status = "Pending" AND organization_id = ?',
        (email, g.user['organization_id'])
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({'error': 'No pending access request was found for this email.'}), 404
    if target['role'] == 'Admin' and g.user['role'] != 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'Only a Super Admin can approve administrator requests.'}), 403

    cursor = conn.execute(
        'UPDATE Users SET status = ? WHERE email = ? AND status = "Pending" AND organization_id = ?',
        ('Approved_Awaiting_Password', email, g.user['organization_id'])
    )
    if cursor.rowcount:
        record_event(conn, 'user.approved', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={'email': email, 'role': target['role']})
    conn.commit()
    
    if cursor.rowcount > 0:
        user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if user:
            name = email.split('@')[0]
            setup_link = f"{public_base_url()}/index.html?setup_token={issue_token(conn, email, SETUP_TOKEN_TTL)}"
            if user['role'] == 'Admin':
                email_service.sendAdminApprovedEmail(email, name, setup_link, organization_name_of(email))
            else:
                email_service.sendUserApprovedEmail(email, name, setup_link, organization_name_of(email))
                
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': f'User {email} approved. Awaiting password setup.'})

@app.route('/api/auth/reject_user', methods=['POST'])
@require_role('Admin')
def reject_user():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
    target = conn.execute(
        'SELECT role FROM Users WHERE email = ? AND status = "Pending" AND organization_id = ?',
        (email, g.user['organization_id'])
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({'error': 'No pending access request was found for this email.'}), 404
    if target['role'] == 'Admin' and g.user['role'] != 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'Only a Super Admin can reject administrator requests.'}), 403

    cursor = conn.execute(
        'UPDATE Users SET status = ? WHERE email = ? AND status = "Pending" AND organization_id = ?',
        ('Rejected', email, g.user['organization_id'])
    )
    if cursor.rowcount:
        record_event(conn, 'user.rejected', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={'email': email, 'role': target['role']})
    conn.commit()
    
    if cursor.rowcount > 0:
        user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if user:
            name = email.split('@')[0]
            if user['role'] == 'Admin':
                email_service.sendAdminRejectedEmail(email, name, organization_name_of(email))
            else:
                email_service.sendUserRejectedEmail(email, name, organization_name_of(email))
                
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': f'User {email} rejected.'})

@app.route('/api/auth/get_profile', methods=['GET'])
@require_login
def get_profile():
    email = get_jwt_identity()
        
    conn = get_db_connection()
    user = conn.execute(
        'SELECT name, emp_id, role, manager_email, created_at, '
        '(SELECT COUNT(*) FROM Users AS reports WHERE reports.manager_email = Users.email '
        'AND reports.organization_id = Users.organization_id) AS report_count '
        'FROM Users WHERE email = ?',
        (email,)
    ).fetchone()
    conn.close()
    
    if user:
        return jsonify(to_json_row(user))
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/auth/update_profile', methods=['POST'])
@require_login
def update_profile():
    data = request.get_json(silent=True) or {}
    email = get_jwt_identity()
    name = data.get('name')
    emp_id = data.get('emp_id')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = get_db_connection()
    conn.execute('UPDATE Users SET name = ?, emp_id = ? WHERE email = ?', (name, emp_id, email))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': 'Profile updated successfully.'})

@app.route('/api/auth/my_requests', methods=['GET'])
@require_login
def my_requests():
    current_email = get_jwt_identity()
    conn = get_db_connection()
    # Scores and escalation reasons are for administrators only.
    requests = conn.execute(
        "SELECT id, role, department, request_type, destination, amount, currency, normalized_amount, "
        "CASE WHEN final_decision LIKE 'ESCALATED%' THEN 'ESCALATED' ELSE final_decision END AS final_decision, "
        f"submitted_by, employee_name, employee_id, purpose, expense_date, end_date, created_at, {RECEIPT_COUNT_SQL} "
        "FROM Requests WHERE submitted_by = ? AND organization_id = ? ORDER BY created_at DESC",
        (current_email, g.user['organization_id'])
    ).fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/pending_approval_requests', methods=['GET'])
@require_role('Admin')
def pending_approval_requests():
    conn = get_db_connection()
    requests = conn.execute(
        f"SELECT *, {RECEIPT_COUNT_SQL} FROM Requests WHERE final_decision LIKE 'ESCALATED%' AND organization_id = ? ORDER BY created_at DESC",
        (g.user['organization_id'],)
    ).fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/all_requests', methods=['GET'])
@require_role('Admin')
def all_requests():
    conn = get_db_connection()
    requests = conn.execute(
        f"SELECT *, {RECEIPT_COUNT_SQL} FROM Requests WHERE organization_id = ? ORDER BY created_at DESC", (g.user['organization_id'],)
    ).fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

def is_awaiting_review(decision):
    return (decision or '').startswith('ESCALATED')

def is_decided(decision):
    return decision in ('APPROVED', 'REJECTED')

COMMENT_MAX_LENGTH = 1000

def change_request_decision(new_decision, allowed_from, conflict_message, action, reason_required=False, managers_allowed=False):
    data = request.get_json(silent=True) or {}
    req_id = data.get('id')
    if not req_id:
        return jsonify({'error': 'Request ID is required'}), 400
    comment = data.get('comment')
    if comment is not None and not isinstance(comment, str):
        return jsonify({'error': 'The comment must be text.'}), 400
    comment = (comment or '').strip()[:COMMENT_MAX_LENGTH] or None

    conn = get_db_connection()
    try:
        target = conn.execute(
            'SELECT id, submitted_by, final_decision, approver_email, normalized_amount, first_approved_by '
            'FROM Requests WHERE id = ? AND organization_id = ?',
            (req_id, g.user['organization_id'])
        ).fetchone()
        if not target:
            return jsonify({'error': 'Request not found.'}), 404
        # Admins decide any request in their organization; a manager decides only the requests waiting for them.
        is_assigned_manager = managers_allowed and target['approver_email'] == g.user['email']
        if g.user['role'] not in ('Admin', 'SuperAdmin') and not is_assigned_manager:
            return jsonify({'error': 'Request not found.'}), 404
        if target['submitted_by'] == g.user['email']:
            return jsonify({'error': 'You cannot review your own request.'}), 403
        if not allowed_from(target['final_decision']):
            return jsonify({'error': conflict_message}), 409
        if reason_required and not comment:
            return jsonify({'error': 'Please give a reason. It is saved in the request history.'}), 400

        details = {'decided_as': 'manager' if is_assigned_manager else 'admin'}
        first_approver = target['first_approved_by']
        if new_decision == 'APPROVED':
            if first_approver == g.user['email']:
                return jsonify({'error': 'You gave the first approval, so somebody else must give the second one.'}), 403
            if first_approver is None and needs_second_approval(g.user, target['normalized_amount']):
                # A large request is only half approved: an administrator must add the second approval.
                new_decision, action = 'ESCALATED_SECOND_APPROVAL', 'request.first_approved'
                first_approver = g.user['email']
                details['second_approval_above'] = organization_second_approval_amount(g.user)
            elif first_approver:
                details['first_approved_by'] = first_approver

        reviewer = g.user['email'] if is_decided(new_decision) else None
        next_approver = None
        if new_decision == 'ESCALATED_SECOND_APPROVAL':
            pass  # No single approver: any other administrator can give the second approval.
        elif not is_decided(new_decision):
            # A reopened request starts over with the employee's current manager, or with the admins if they have none.
            submitter = conn.execute(
                'SELECT manager_email FROM Users WHERE email = ? AND organization_id = ?',
                (target['submitted_by'], g.user['organization_id'])
            ).fetchone()
            next_approver = submitter['manager_email'] if submitter else None
            first_approver = None
        # Matching the status that was checked stops two people acting at once from overwriting each other.
        cursor = conn.execute(
            f"UPDATE Requests SET final_decision = ?, reviewed_by = ?, reviewed_at = {'CURRENT_TIMESTAMP' if reviewer else 'NULL'}, "
            'approver_email = ?, first_approved_by = ? WHERE id = ? AND organization_id = ? AND final_decision = ?',
            (new_decision, reviewer, next_approver, first_approver, target['id'], g.user['organization_id'], target['final_decision'])
        )
        if not cursor.rowcount:
            return jsonify({'error': conflict_message}), 409
        # The decision and its history entry are saved together, or neither is.
        record_event(
            conn, action, organization_id=g.user['organization_id'], request_id=target['id'], actor=g.user['email'],
            from_status=target['final_decision'], to_status=new_decision, comment=comment, details=details
        )
        send_notice = None
        if is_awaiting_review(new_decision):
            note = ('This request already has one approval and needs a second one from an administrator.'
                    if new_decision == 'ESCALATED_SECOND_APPROVAL' else None)
            send_notice = waiting_notice(conn, g.user['organization_id'], target['id'], next_approver, note)
        conn.commit()
    finally:
        conn.close()
    if send_notice:
        send_notice()
    return jsonify({'status': 'SUCCESS', 'final_decision': new_decision})

@app.route('/api/auth/approve_request', methods=['POST'])
@require_login
def approve_request():
    return change_request_decision(
        'APPROVED', is_awaiting_review, 'Only requests awaiting review can be approved.', 'request.approved',
        managers_allowed=True
    )

@app.route('/api/auth/reject_request', methods=['POST'])
@require_login
def reject_request():
    return change_request_decision(
        'REJECTED', is_awaiting_review, 'Only requests awaiting review can be rejected.', 'request.rejected',
        reason_required=True, managers_allowed=True
    )

@app.route('/api/auth/reopen_request', methods=['POST'])
@require_role('Admin')
def reopen_request():
    return change_request_decision(
        'ESCALATED_MANUAL_REVIEW', is_decided, 'Only approved or rejected requests can be moved back to pending.',
        'request.reopened', reason_required=True
    )

@app.route('/api/auth/request_history', methods=['GET'])
@require_login
def request_history():
    request_id = request.args.get('id', type=int)
    if request_id is None:
        return jsonify({'error': 'Request ID is required'}), 400

    conn = get_db_connection()
    try:
        if not conn.execute('SELECT id FROM Requests WHERE id = ? AND organization_id = ?', (request_id, g.user['organization_id'])).fetchone():
            return jsonify({'error': 'Request not found.'}), 404
        if not can_review_request(conn, request_id):
            return jsonify({'error': "Only administrators and the employee's manager can see this history."}), 403
        rows = conn.execute(
            'SELECT action, actor_email, from_status, to_status, comment, details, created_at FROM AuditEvents '
            'WHERE request_id = ? AND organization_id = ? ORDER BY id',
            (request_id, g.user['organization_id'])
        ).fetchall()
    finally:
        conn.close()

    events = []
    for row in rows:
        event = to_json_row(row)
        event['details'] = json.loads(event['details']) if event['details'] else None
        events.append(event)
    return jsonify({'events': events})

@app.route('/api/auth/request_password_reset', methods=['POST'])
@limiter.limit("3 per hour")
def request_password_reset():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (email,)).fetchone()
    
    if not user:
        conn.close()
        # Prevent user enumeration
        return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'}), 200
        
    if user['status'] == 'Approved_Awaiting_Password':
        setup_link = f"{public_base_url()}/index.html?setup_token={issue_token(conn, email, SETUP_TOKEN_TTL)}"
        conn.close()
        name = email.split('@')[0]
        if user['role'] in ('Admin', 'SuperAdmin'):
            email_service.sendAdminApprovedEmail(email, name, setup_link, organization_name_of(email))
        else:
            email_service.sendUserApprovedEmail(email, name, setup_link, organization_name_of(email))
        return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'})

    if user['status'] != 'Active':
        conn.close()
        return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'})

    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + RESET_TOKEN_TTL).strftime('%Y-%m-%d %H:%M:%S')
    
    conn.execute('UPDATE Users SET reset_token = ?, reset_expiry = ? WHERE email = ?', (hash_token(token), expiry, email))
    conn.commit()
    conn.close()
    
    host_url = public_base_url()
    reset_link = f"{host_url}/index.html?reset_token={token}"
    reject_link = f"{host_url}/api/auth/reject_reset?token={token}"
    
    email_service.sendPasswordResetEmail(email, reset_link, reject_link)
    
    return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'})

@app.route('/api/auth/reset_password', methods=['POST'])
def reset_password():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    new_password = data.get('password')
    
    if not token or not new_password:
        return jsonify({'error': 'Token and password are required'}), 400

    if len(new_password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters.'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE reset_token = ?', (hash_token(token),)).fetchone()
    
    if not user:
        conn.close()
        return jsonify({'error': 'Invalid or expired token'}), 400
        
    if user['status'] != 'Active' or token_expired(user['reset_expiry']):
        conn.close()
        return jsonify({'error': 'This reset link is invalid or has expired.'}), 400
        
    hashed_pw = generate_password_hash(new_password, method='pbkdf2:sha256')
    
    conn.execute('UPDATE Users SET password_hash = ?, reset_token = NULL, reset_expiry = NULL WHERE id = ?', (hashed_pw, user['id']))
    conn.commit()
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': 'Password has been reset successfully.'})

@app.route('/api/auth/reject_reset', methods=['GET'])
def reject_reset():
    token = request.args.get('token')
    if not token:
        return "Invalid token", 400
        
    conn = get_db_connection()
    conn.execute('UPDATE Users SET reset_token = NULL, reset_expiry = NULL WHERE reset_token = ?', (hash_token(token),))
    conn.commit()
    conn.close()
    
    return "<h3>Password Reset Request Cancelled</h3><p>Your password reset request has been safely invalidated. You can now close this tab.</p>", 200


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    resp = jsonify({'status': 'SUCCESS', 'message': 'Logged out successfully.'})
    unset_jwt_cookies(resp)
    return resp


if __name__ == '__main__':
    print("Starting Unified AAMS Application on Port 5000...")
    app.run(port=5000, debug=False)
