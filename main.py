from flask import Flask, g, redirect, request, jsonify, send_from_directory
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
import threading
import time
import email_service
import model_pipeline
import secrets
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import pandas as pd
import joblib
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, set_access_cookies, unset_jwt_cookies, get_jwt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps
from urllib.parse import quote

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
                'SELECT Users.id, Users.email, Users.role, Users.status, Users.organization_id, '
                'Organizations.status AS organization_status, Organizations.approval_mode, Organizations.auto_approve_above '
                'FROM Users JOIN Organizations ON Organizations.id = Users.organization_id WHERE Users.email = ?',
                (get_jwt_identity(),)
            ).fetchone()
        finally:
            conn.close()

        if not user or user['status'] != 'Active' or user['organization_id'] != get_jwt().get('organization_id'):
            return jsonify({'error': SESSION_INVALID_MESSAGE}), 401
        if user['organization_status'] != 'Active':
            return jsonify({'error': ORGANIZATION_PAUSED_MESSAGE}), 403
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
        for operation in ('UPDATE', 'DELETE'):
            cursor.execute(f'''
                CREATE TRIGGER IF NOT EXISTS audit_events_no_{operation.lower()} BEFORE {operation} ON AuditEvents
                BEGIN
                    SELECT RAISE(ABORT, 'Audit events cannot be changed or deleted');
                END
            ''')
        for statement in AUDIT_EVENT_INDEXES:
            cursor.execute(statement)
        conn.commit()
        conn.close()

REQUEST_EXTRA_COLUMNS = {
    'employee_name': 'TEXT',
    'employee_id': 'TEXT',
    'reviewed_by': 'TEXT',
    'reviewed_at': 'TIMESTAMP',
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
    for column, column_type in (('approval_mode', 'TEXT'), ('auto_approve_above', 'REAL')):
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
        data = request.get_json(silent=True) or {}
        xgb_model = artifacts['xgboost_model']
        iso_forest = artifacts['isolation_forest']
        oc_svm = artifacts['one_class_svm']
        encoders = artifacts['encoders']
        scaler = artifacts['scaler']
        features = artifacts['features']
        
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

        # Normalize Amount to INR
        rate = exchange_rates.get(currency, 1.0)
        normalized_inr = amount * rate

        # Prepare DataFrame for preprocessing
        input_data = pd.DataFrame({
            'Role': [role],
            'Department': [department],
            'Request_Type': [req_type],
            'Destination': [destination],
            'Amount_INR': [normalized_inr]
        })

        is_unknown_category = False

        # Map categorical text fields with OOV fallback
        for col in ['Role', 'Department', 'Request_Type', 'Destination']:
            if input_data[col].iloc[0] in encoders[col].classes_:
                input_data[col] = encoders[col].transform(input_data[col])
            else:
                is_unknown_category = True
                input_data[col] = 0  # Safe fallback to 0

        # Scale the normalized amount
        input_data['Amount_INR'] = scaler.transform(input_data[['Amount_INR']])

        # Reorder columns to match feature order used in training
        X_input = input_data[features]

        # XGBoost Probabilities
        xgb_prob = float(xgb_model.predict_proba(X_input)[0][1])
        
        # Anomaly Detection
        iso_pred = int(iso_forest.predict(X_input)[0])
        svm_pred = int(oc_svm.predict(X_input)[0])
        is_severe_anomaly = (iso_pred == -1) or (svm_pred == -1)

        # SHAP values show administrators which fields pushed the score up or down.
        shap_values = artifacts['shap_explainer'].shap_values(X_input)
        shap_impact = dict(zip(features, [float(v) for v in shap_values[0]]))
        
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

        # Persist to DB
        conn = get_db_connection()
        request_id = conn.execute(
            '''INSERT INTO Requests (
                role, department, request_type, destination, amount, currency, 
                normalized_amount, xgb_score, iso_score, svm_score, risk_score, 
                final_decision, submitted_by, employee_name, employee_id, organization_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id''',
            (role, department, req_type, destination, amount, currency,
             normalized_inr, xgb_prob, iso_pred, svm_pred, (1 - xgb_prob)*100,
             status, current_email, employee_name, employee_id, g.user['organization_id'])
        ).fetchall()[0][0]
        record_event(
            conn, 'request.submitted', organization_id=g.user['organization_id'], request_id=request_id,
            actor=current_email, to_status=status, details={
                'model_version': model_version_id,
                'approval_score': round(xgb_prob, 4),
                'unrecognized_category': is_unknown_category,
                'anomaly_detectors': {'isolation_forest': iso_pred == -1, 'one_class_svm': svm_pred == -1},
                'thresholds': {'auto_approve_above': auto_approve_above, 'escalate_below': ESCALATE_THRESHOLD},
                'explanation': shap_impact,
            }
        )
        conn.commit()
        conn.close()

        # Employees only learn the outcome. Scores and escalation reasons stay with administrators,
        # so nobody can map the model's boundaries by resubmitting variations of a request.
        approved = status == 'APPROVED'
        return jsonify({
            'status': 'APPROVED' if approved else 'PENDING_REVIEW',
            'message': 'Your request was approved.' if approved else 'Your request was sent to an administrator for review.',
            'normalized_inr': normalized_inr,
        })

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
        decided = conn.execute(f"SELECT COUNT(*) AS total {TRAINABLE_DECISIONS_SQL}").fetchone()['total']
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

def trainable_decisions(conn):
    rows = conn.execute(
        "SELECT Requests.id, Requests.role, Requests.department, Requests.request_type, Requests.destination, "
        f"Requests.normalized_amount, Requests.final_decision {TRAINABLE_DECISIONS_SQL}"
    ).fetchall()
    return [dict(row) for row in rows]

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

        if not comparison['accepted']:
            update_training_job(
                job_id, status='rejected', step='done', metrics=json.dumps(metrics), finished_at=utc_now_text(),
                message=f"The retrained model scored {new_score:.1%} while the current model scores {old_score:.1%}, so the current model stays active."
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
        update_training_job(
            job_id, status='succeeded', step='done', metrics=json.dumps(metrics), model_version_id=version_id,
            finished_at=utc_now_text(),
            message=f"Version {version_id} is now scoring new requests with a quality score of {new_score:.1%}{previous_text}."
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
    if not changes:
        return jsonify({'error': 'Nothing to update.'}), 400

    conn = get_db_connection()
    try:
        organization = conn.execute('SELECT is_default FROM Organizations WHERE id = ?', (organization_id,)).fetchone()
        if not organization:
            return jsonify({'error': 'Organization not found.'}), 404
        if organization['is_default'] and changes.get('status') == 'Paused':
            return jsonify({'error': 'The default organization holds the original accounts and cannot be paused.'}), 409
        # Column names come from the fixed keys above, never from the request.
        assignments = ', '.join(f'{column} = ?' for column in changes)
        conn.execute(f'UPDATE Organizations SET {assignments} WHERE id = ?', (*changes.values(), organization_id))
        record_event(conn, 'organization.updated', organization_id=organization_id, actor=g.user['email'],
                     details={'changes': {column: data[column] for column in changes}})
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
        }
    return jsonify(body)

@app.route('/api/auth/update_approval_settings', methods=['POST'])
@require_role('SuperAdmin')
def update_approval_settings():
    data = request.get_json(silent=True) or {}
    if 'approval_mode' in data:
        return jsonify({'error': 'Only Neuzem can switch an organization between shadow and automatic approval.'}), 403

    value = data.get('auto_approve_above')
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not AUTO_APPROVE_THRESHOLD <= value <= AUTO_APPROVE_THRESHOLD_MAX):
        return jsonify({
            'error': f'The auto-approval threshold must be between {AUTO_APPROVE_THRESHOLD:.0%} and {AUTO_APPROVE_THRESHOLD_MAX:.0%}.'
        }), 400
    value = round(float(value), 4)
    previous = organization_auto_approve_threshold(g.user)

    conn = get_db_connection()
    try:
        conn.execute('UPDATE Organizations SET auto_approve_above = ? WHERE id = ?', (value, g.user['organization_id']))
        record_event(conn, 'settings.updated', organization_id=g.user['organization_id'], actor=g.user['email'],
                     details={'auto_approve_above': {'from': previous, 'to': value}})
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS', 'auto_approve_above': value})

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
        'SELECT id, email, name, role, status, created_at FROM Users WHERE organization_id = ?', (g.user['organization_id'],)
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
    user = conn.execute('SELECT name, emp_id, role, created_at FROM Users WHERE email = ?', (email,)).fetchone()
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
        "submitted_by, employee_name, employee_id, created_at "
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
        "SELECT * FROM Requests WHERE final_decision LIKE 'ESCALATED%' AND organization_id = ? ORDER BY created_at DESC",
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
        "SELECT * FROM Requests WHERE organization_id = ? ORDER BY created_at DESC", (g.user['organization_id'],)
    ).fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

def is_awaiting_review(decision):
    return (decision or '').startswith('ESCALATED')

def is_decided(decision):
    return decision in ('APPROVED', 'REJECTED')

COMMENT_MAX_LENGTH = 1000

def change_request_decision(new_decision, allowed_from, conflict_message, action, reason_required=False):
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
            'SELECT id, submitted_by, final_decision FROM Requests WHERE id = ? AND organization_id = ?',
            (req_id, g.user['organization_id'])
        ).fetchone()
        if not target:
            return jsonify({'error': 'Request not found.'}), 404
        if target['submitted_by'] == g.user['email']:
            return jsonify({'error': 'You cannot review your own request.'}), 403
        if not allowed_from(target['final_decision']):
            return jsonify({'error': conflict_message}), 409
        if reason_required and not comment:
            return jsonify({'error': 'Please give a reason. It is saved in the request history.'}), 400

        reviewer = g.user['email'] if is_decided(new_decision) else None
        # Matching the status that was checked stops two admins acting at once from overwriting each other.
        cursor = conn.execute(
            f"UPDATE Requests SET final_decision = ?, reviewed_by = ?, reviewed_at = {'CURRENT_TIMESTAMP' if reviewer else 'NULL'} "
            'WHERE id = ? AND organization_id = ? AND final_decision = ?',
            (new_decision, reviewer, target['id'], g.user['organization_id'], target['final_decision'])
        )
        if not cursor.rowcount:
            return jsonify({'error': conflict_message}), 409
        # The decision and its history entry are saved together, or neither is.
        record_event(
            conn, action, organization_id=g.user['organization_id'], request_id=target['id'], actor=g.user['email'],
            from_status=target['final_decision'], to_status=new_decision, comment=comment
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/approve_request', methods=['POST'])
@require_role('Admin')
def approve_request():
    return change_request_decision(
        'APPROVED', is_awaiting_review, 'Only requests awaiting review can be approved.', 'request.approved'
    )

@app.route('/api/auth/reject_request', methods=['POST'])
@require_role('Admin')
def reject_request():
    return change_request_decision(
        'REJECTED', is_awaiting_review, 'Only requests awaiting review can be rejected.', 'request.rejected', reason_required=True
    )

@app.route('/api/auth/reopen_request', methods=['POST'])
@require_role('Admin')
def reopen_request():
    return change_request_decision(
        'ESCALATED_MANUAL_REVIEW', is_decided, 'Only approved or rejected requests can be moved back to pending.',
        'request.reopened', reason_required=True
    )

@app.route('/api/auth/request_history', methods=['GET'])
@require_role('Admin')
def request_history():
    request_id = request.args.get('id', type=int)
    if request_id is None:
        return jsonify({'error': 'Request ID is required'}), 400

    conn = get_db_connection()
    try:
        if not conn.execute('SELECT id FROM Requests WHERE id = ? AND organization_id = ?', (request_id, g.user['organization_id'])).fetchone():
            return jsonify({'error': 'Request not found.'}), 404
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
