from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from decimal import Decimal
import hashlib
import math
import re
import sqlite3
import os
import email_service
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
import pandas as pd
import joblib
from flask_jwt_extended import JWTManager, create_access_token, jwt_required, get_jwt_identity, set_access_cookies, unset_jwt_cookies, get_jwt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from functools import wraps

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

def require_role(role):
    def wrapper(fn):
        @wraps(fn)
        @jwt_required()
        def decorator(*args, **kwargs):
            claims = get_jwt()
            if claims.get('role') != role and claims.get('role') != 'SuperAdmin':
                return jsonify({"error": "Insufficient permissions"}), 403
            return fn(*args, **kwargs)
        return decorator
    return wrapper

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

def token_expired(expiry):
    if not expiry:
        return True
    # SQLite returns the stored string; PostgreSQL returns a datetime.
    if isinstance(expiry, str):
        expiry = datetime.strptime(expiry, '%Y-%m-%d %H:%M:%S')
    return datetime.utcnow() > expiry

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

# ==========================================
# 1. AUTHENTICATION & DATABASE CONFIGURATION
# ==========================================
DB_FILE = 'auth.db'

try:
    import psycopg2
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
except ImportError:
    DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

# Initialize Super Admin via environment variables if provided
def bootstrap_super_admin():
    sa_email = os.getenv('INITIAL_SUPER_ADMIN_EMAIL', 'superadmin.main.01@gmail.com')
    sa_password = os.getenv('INITIAL_SUPER_ADMIN_PASSWORD', os.getenv('SUPER_ADMIN_PASSWORD'))
    
    if not sa_email or not sa_password:
        return
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (sa_email,)).fetchone()
    if not user:
        from werkzeug.security import generate_password_hash
        conn.execute(
            'INSERT INTO Users (email, password_hash, role, status) VALUES (?, ?, ?, ?)',
            (sa_email, generate_password_hash(sa_password), 'SuperAdmin', 'Active')
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
        conn.commit()
        conn.close()

# Initialize DB on startup
print("Initializing Auth Database...")
init_db()

def check_and_add_columns():
    DATABASE_URL = os.getenv('DATABASE_URL')
    if DATABASE_URL:
        return # Postgres init handles all columns

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
        
    conn.commit()
    conn.close()

check_and_add_columns()

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

bootstrap_super_admin()


# ==========================================
# 2. MACHINE LEARNING CONFIGURATION
# ==========================================
print("Loading ensemble model artifacts...")
MODEL_READY = False
try:
    artifacts = joblib.load("ensemble_ai_model.pkl")
    xgb_model = artifacts['xgboost_model']
    iso_forest = artifacts['isolation_forest']
    oc_svm = artifacts['one_class_svm']
    explainer = artifacts['shap_explainer']
    encoders = artifacts['encoders']
    scaler = artifacts['scaler']
    features = artifacts['features']
    MODEL_READY = True
    print("Model artifacts loaded successfully.")
except Exception as e:
    print(f"Error loading model artifacts: {e}")

exchange_rates = {
    'INR': 1.0,
    'USD': 83.50,
    'EUR': 90.20,
    'GBP': 105.00,
    'SGD': 61.30
}


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


# ==========================================
# 4. MACHINE LEARNING API ROUTES
# ==========================================
@app.route('/api/predict', methods=['POST'])
@limiter.limit("20 per minute")
@jwt_required()
def predict():
    if not MODEL_READY:
        return jsonify({
            'error': 'The AI model is currently unavailable. Please try again later.',
            'status': 'ESCALATED_SYSTEM_ERROR'
        }), 503

    try:
        current_email = get_jwt_identity()
        data = request.get_json(silent=True) or {}
        
        # Extract inputs
        role = data.get('Role')
        department = data.get('Department')
        req_type = data.get('Request_Type')
        destination = data.get('Destination')
        amount = data.get('Amount')
        currency = data.get('Currency')

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
        confidence_pct = round(xgb_prob * 100, 1)
        
        # Anomaly Detection
        iso_pred = int(iso_forest.predict(X_input)[0])
        svm_pred = int(oc_svm.predict(X_input)[0])
        is_severe_anomaly = (iso_pred == -1) or (svm_pred == -1)
        
        # SHAP Explainability
        shap_values = explainer.shap_values(X_input)
        shap_impact = dict(zip(features, [float(v) for v in shap_values[0]]))

        # Decision Routing Logic (Confidence Based Triage)
        if is_unknown_category:
            status = "ESCALATED_UNKNOWN"
            message = "Unrecognized category detected (Out-Of-Vocabulary). Manual review required."
        elif is_severe_anomaly:
            status = "ESCALATED_ANOMALY"
            message = "Unusual data distribution detected by Anomaly Detectors. Flagged as anomaly."
        elif xgb_prob > 0.8:
            status = "APPROVED"
            message = "Auto-Approved based on high confidence."
        elif xgb_prob < 0.2:
            status = "ESCALATED_POLICY"  
            message = "Auto-Rejected based on low confidence. Manual review / policy enforcement required."
        else:
            status = "ESCALATED_MANUAL_REVIEW"
            message = "Marginal confidence score. Sent to HR for manual review (Grey Area)."

        # Persist to DB
        conn = get_db_connection()
        conn.execute(
            '''INSERT INTO Requests (
                role, department, request_type, destination, amount, currency, 
                normalized_amount, xgb_score, iso_score, svm_score, risk_score, 
                final_decision, submitted_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (role, department, req_type, destination, amount, currency, 
             normalized_inr, xgb_prob, iso_pred, svm_pred, (1 - xgb_prob)*100, 
             status, current_email)
        )
        conn.commit()
        conn.close()

        return jsonify({
            'status': status,
            'message': message,
            'confidence': confidence_pct,
            'normalized_inr': normalized_inr,
            'shap_explanations': shap_impact
        })

    except Exception:
        app.logger.exception("Prediction failed")
        return jsonify({
            'error': 'An internal system error occurred during AI processing.',
            'status': 'ESCALATED_SYSTEM_ERROR',
            'message': 'An internal system error occurred during AI processing.'
        }), 500


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
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (email,)).fetchone()
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
            redirect_page = 'admin.html' if user['role'] in ['Admin', 'SuperAdmin'] else 'user.html'
            resp = jsonify({
                'status': 'SUCCESS',
                'role': user['role'],
                'message': 'Login successful.',
                'redirect': redirect_page
            })
            access_token = create_access_token(identity=str(user['email']), additional_claims={'role': user['role']})
            set_access_cookies(resp, access_token)
            return resp
        else:
            return jsonify({'error': 'Invalid password'}), 401
    
    return jsonify({'error': 'Unknown status'}), 500


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
    existing_user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
    if existing_user and existing_user['role'] == 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'This email is reserved for Super Admins.'}), 400
    if existing_user:
        conn.close()
        return jsonify({'error': 'Email already exists or is pending.'}), 409
    try:
        conn.execute(
            'INSERT INTO Users (email, role, status) VALUES (?, ?, ?)',
            (email, role, 'Pending')
        )
        conn.commit()
    except DB_INTEGRITY_ERRORS:
        return jsonify({'error': 'Email already exists or is pending.'}), 409
    finally:
        conn.close()

    if role == 'Admin':
        conn = get_db_connection()
        super_admins = conn.execute("SELECT email FROM Users WHERE role = 'SuperAdmin' AND status = 'Active'").fetchall()
        conn.close()
        for sa in super_admins:
            email_service.sendAdminRegistrationNotification(sa['email'], email)
    else:
        conn = get_db_connection()
        active_admins = conn.execute("SELECT email FROM Users WHERE role IN ('Admin', 'SuperAdmin') AND status = 'Active'").fetchall()
        conn.close()
        
        all_notifiers = [a['email'] for a in active_admins]
        
        for notify_email in all_notifiers:
            email_service.sendUserRegistrationNotification(notify_email, email, role)

    return jsonify({'status': 'SUCCESS', 'message': 'Access request submitted successfully. Awaiting approval.'})


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

    email_service.sendWelcomeEmail(email, email.split('@')[0])

    redirect_page = 'admin.html' if user['role'] in ['Admin', 'SuperAdmin'] else 'user.html'
    resp = jsonify({
        'status': 'SUCCESS',
        'message': 'Password set successfully. Account is now active.',
        'redirect': redirect_page,
        'role': user['role'],
        'email': email
    })
    set_access_cookies(resp, create_access_token(identity=email, additional_claims={'role': user['role']}))
    return resp

@app.route('/api/auth/pending_users', methods=['GET'])
@require_role('Admin')
def get_pending_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, role, status, created_at FROM Users WHERE status = "Pending"').fetchall()
    conn.close()
    
    users_list = [to_json_row(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/users', methods=['GET'])
@require_role('Admin')
def get_all_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, name, role, status, created_at FROM Users').fetchall()
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
    target = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
    if target and get_jwt().get('role') != 'SuperAdmin' and target['role'] != 'User':
        conn.close()
        return jsonify({'status': 'ERROR', 'message': 'Admins can only remove employee accounts.'}), 403

    cursor = conn.execute('DELETE FROM Users WHERE email = ?', (email,))
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
    target = conn.execute('SELECT role FROM Users WHERE email = ? AND status = "Pending"', (email,)).fetchone()
    if target and target['role'] == 'Admin' and get_jwt().get('role') != 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'Only a Super Admin can approve administrator requests.'}), 403

    cursor = conn.execute(
        'UPDATE Users SET status = ? WHERE email = ? AND status = "Pending"',
        ('Approved_Awaiting_Password', email)
    )
    conn.commit()
    
    if cursor.rowcount > 0:
        user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if user:
            name = email.split('@')[0]
            setup_link = f"{public_base_url()}/index.html?setup_token={issue_token(conn, email, SETUP_TOKEN_TTL)}"
            if user['role'] == 'Admin':
                email_service.sendAdminApprovedEmail(email, name, setup_link)
            else:
                email_service.sendUserApprovedEmail(email, name, setup_link)
                
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
    target = conn.execute('SELECT role FROM Users WHERE email = ? AND status = "Pending"', (email,)).fetchone()
    if target and target['role'] == 'Admin' and get_jwt().get('role') != 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'Only a Super Admin can reject administrator requests.'}), 403

    cursor = conn.execute(
        'UPDATE Users SET status = ? WHERE email = ? AND status = "Pending"',
        ('Rejected', email)
    )
    conn.commit()
    
    if cursor.rowcount > 0:
        user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if user:
            name = email.split('@')[0]
            if user['role'] == 'Admin':
                email_service.sendAdminRejectedEmail(email, name)
            else:
                email_service.sendUserRejectedEmail(email, name)
                
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': f'User {email} rejected.'})

@app.route('/api/auth/get_profile', methods=['GET'])
@jwt_required()
def get_profile():
    email = get_jwt_identity()
        
    conn = get_db_connection()
    user = conn.execute('SELECT name, emp_id, role, created_at FROM Users WHERE email = ?', (email,)).fetchone()
    conn.close()
    
    if user:
        return jsonify(to_json_row(user))
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/auth/update_profile', methods=['POST'])
@jwt_required()
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
@jwt_required()
def my_requests():
    current_email = get_jwt_identity()
    conn = get_db_connection()
    requests = conn.execute('SELECT * FROM Requests WHERE submitted_by = ? ORDER BY created_at DESC', (current_email,)).fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/pending_approval_requests', methods=['GET'])
@require_role('Admin')
def pending_approval_requests():
    conn = get_db_connection()
    requests = conn.execute("SELECT * FROM Requests WHERE final_decision LIKE 'ESCALATED%' ORDER BY created_at DESC").fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/all_requests', methods=['GET'])
@require_role('Admin')
def all_requests():
    conn = get_db_connection()
    requests = conn.execute("SELECT * FROM Requests ORDER BY created_at DESC").fetchall()
    conn.close()
    
    requests_list = [to_json_row(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/approve_request', methods=['POST'])
@require_role('Admin')
def approve_request():
    data = request.get_json(silent=True) or {}
    req_id = data.get('id')
    if not req_id:
        return jsonify({'error': 'Request ID is required'}), 400
        
    conn = get_db_connection()
    cursor = conn.execute("UPDATE Requests SET final_decision = 'APPROVED' WHERE id = ?", (req_id,))
    conn.commit()
    updated = cursor.rowcount
    conn.close()
    if not updated:
        return jsonify({'error': 'Request not found.'}), 404
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/reject_request', methods=['POST'])
@require_role('Admin')
def reject_request():
    data = request.get_json(silent=True) or {}
    req_id = data.get('id')
    if not req_id:
        return jsonify({'error': 'Request ID is required'}), 400
        
    conn = get_db_connection()
    cursor = conn.execute("UPDATE Requests SET final_decision = 'REJECTED' WHERE id = ?", (req_id,))
    conn.commit()
    updated = cursor.rowcount
    conn.close()
    if not updated:
        return jsonify({'error': 'Request not found.'}), 404
    return jsonify({'status': 'SUCCESS'})

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
        if user['role'] == 'Admin':
            email_service.sendAdminApprovedEmail(email, name, setup_link)
        else:
            email_service.sendUserApprovedEmail(email, name, setup_link)
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
