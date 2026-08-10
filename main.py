from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
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
# Security configuration
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
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# Lock down CORS to only support specific origins
allowed_origins = os.getenv('CORS_ORIGINS', 'http://localhost:5000').split(',')
CORS(app, supports_credentials=True, origins=allowed_origins)

@app.before_request
def log_request_cookies():
    if request.path.startswith('/api/auth'):
        print(f"[DEBUG Cookie Check] Path: {request.path}")
        print(f"[DEBUG Cookie Check] Cookies parsed: {list(request.cookies.keys())}")
        print(f"[DEBUG Cookie Check] Cookie Header: {request.headers.get('Cookie', 'None')}")

@app.after_request
def log_response_errors(response):
    if request.path.startswith('/api/auth') and response.status_code >= 400:
        print(f"[DEBUG Auth Error] Status: {response.status_code}")
        print(f"[DEBUG Auth Error] Payload: {response.get_data(as_text=True)}")
    return response

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

# ==========================================
# 1. AUTHENTICATION & DATABASE CONFIGURATION
# ==========================================
DB_FILE = 'auth.db'

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
        # Convert SQLite ? placeholders to PostgreSQL %s
        query = query.replace('?', '%s')
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
try:
    artifacts = joblib.load("ensemble_ai_model.pkl")
    xgb_model = artifacts['xgboost_model']
    iso_forest = artifacts['isolation_forest']
    oc_svm = artifacts['one_class_svm']
    explainer = artifacts['shap_explainer']
    encoders = artifacts['encoders']
    scaler = artifacts['scaler']
    features = artifacts['features']
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
    try:
        current_email = get_jwt_identity()
        data = request.json
        
        # Extract inputs
        role = data.get('Role')
        department = data.get('Department')
        req_type = data.get('Request_Type')
        destination = data.get('Destination')
        amount = data.get('Amount')
        currency = data.get('Currency')

        required_fields = ['Role', 'Department', 'Request_Type', 'Destination', 'Currency']
        if not all(data.get(f) for f in required_fields):
            return jsonify({'error': f'Missing required fields.'}), 400
            
        try:
            amount = float(amount)
            if amount <= 0:
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

    except Exception as e:
        return jsonify({
            'error': str(e),
            'status': 'ESCALATED_SYSTEM_ERROR',
            'message': 'An internal system error occurred during AI processing.'
        }), 500


# ==========================================
# 5. AUTHENTICATION API ROUTES
# ==========================================
@app.route('/api/auth/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.json
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
        return jsonify({'status': 'SETUP_REQUIRED', 'message': 'You have been approved! Please set up your password.'}), 200
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
    data = request.json
    email = data.get('email')
    role = data.get('role', 'User')

    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    if role not in ['User', 'Admin']:
        return jsonify({'error': 'Invalid role requested.'}), 400

    conn = get_db_connection()
    existing_user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
    if existing_user and existing_user['role'] == 'SuperAdmin':
        conn.close()
        return jsonify({'error': 'This email is reserved for Super Admins.'}), 400
    try:
        conn.execute(
            'INSERT INTO Users (email, role, status) VALUES (?, ?, ?)',
            (email, role, 'Pending')
        )
        conn.commit()
    except sqlite3.IntegrityError:
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
def setup_password():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (email,)).fetchone()
    
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
        
    if user['status'] != 'Approved_Awaiting_Password':
        conn.close()
        return jsonify({'error': 'User is not authorized to set a password at this time.'}), 403

    hashed_pw = generate_password_hash(password)
    
    conn.execute(
        'UPDATE Users SET password_hash = ?, status = ? WHERE email = ?',
        (hashed_pw, 'Active', email)
    )
    conn.commit()
    conn.close()

    name = email.split('@')[0]
    email_service.sendWelcomeEmail(email, name)

    redirect_page = 'admin.html' if user['role'] in ['Admin', 'SuperAdmin'] else 'user.html'
    return jsonify({
        'status': 'SUCCESS', 
        'message': 'Password set successfully. Account is now active.',
        'redirect': redirect_page,
        'role': user['role']
    })

@app.route('/api/auth/pending_users', methods=['GET'])
@require_role('Admin')
def get_pending_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, role, status, created_at FROM Users WHERE status = "Pending"').fetchall()
    conn.close()
    
    users_list = [dict(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/users', methods=['GET'])
@require_role('Admin')
def get_all_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, name, role, status, created_at FROM Users').fetchall()
    conn.close()
    
    users_list = [dict(u) for u in users]
    return jsonify(users_list)
@app.route('/api/auth/debug_cookie', methods=['GET'])
def debug_cookie():
    return jsonify({
        "cookies_received": list(request.cookies.keys()),
        "cookie_header": request.headers.get('Cookie', 'None'),
        "jwt_config_name": app.config.get('JWT_ACCESS_COOKIE_NAME')
    })

@app.route('/api/auth/delete_user', methods=['POST'])
@require_role('Admin')
def delete_user():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
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
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
    cursor = conn.execute(
        'UPDATE Users SET status = ? WHERE email = ? AND status = "Pending"',
        ('Approved_Awaiting_Password', email)
    )
    conn.commit()
    
    if cursor.rowcount > 0:
        user = conn.execute('SELECT role FROM Users WHERE email = ?', (email,)).fetchone()
        if user:
            name = email.split('@')[0]
            if user['role'] == 'Admin':
                email_service.sendAdminApprovedEmail(email, name)
            else:
                email_service.sendUserApprovedEmail(email, name)
                
    conn.close()
    
    return jsonify({'status': 'SUCCESS', 'message': f'User {email} approved. Awaiting password setup.'})

@app.route('/api/auth/reject_user', methods=['POST'])
@require_role('Admin')
def reject_user():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400

    conn = get_db_connection()
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
    email = request.args.get('email')
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT name, emp_id, role, created_at FROM Users WHERE email = ?', (email,)).fetchone()
    conn.close()
    
    if user:
        return jsonify(dict(user))
    return jsonify({'error': 'User not found'}), 404

@app.route('/api/auth/update_profile', methods=['POST'])
@jwt_required()
def update_profile():
    data = request.json
    email = data.get('email')
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
    
    requests_list = [dict(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/pending_approval_requests', methods=['GET'])
@require_role('Admin')
def pending_approval_requests():
    conn = get_db_connection()
    requests = conn.execute("SELECT * FROM Requests WHERE final_decision LIKE 'ESCALATED%' ORDER BY created_at DESC").fetchall()
    conn.close()
    
    requests_list = [dict(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/all_requests', methods=['GET'])
@require_role('Admin')
def all_requests():
    conn = get_db_connection()
    requests = conn.execute("SELECT * FROM Requests ORDER BY created_at DESC").fetchall()
    conn.close()
    
    requests_list = [dict(r) for r in requests]
    return jsonify(requests_list)

@app.route('/api/auth/approve_request', methods=['POST'])
@require_role('Admin')
def approve_request():
    data = request.json
    req_id = data.get('id')
    if not req_id:
        return jsonify({'error': 'Request ID is required'}), 400
        
    conn = get_db_connection()
    conn.execute("UPDATE Requests SET final_decision = 'APPROVED' WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/reject_request', methods=['POST'])
@require_role('Admin')
def reject_request():
    data = request.json
    req_id = data.get('id')
    if not req_id:
        return jsonify({'error': 'Request ID is required'}), 400
        
    conn = get_db_connection()
    conn.execute("UPDATE Requests SET final_decision = 'REJECTED' WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'SUCCESS'})

@app.route('/api/auth/request_password_reset', methods=['POST'])
@limiter.limit("3 per hour")
def request_password_reset():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (email,)).fetchone()
    
    if not user:
        conn.close()
        # Prevent user enumeration
        return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'}), 200
        
    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
    
    conn.execute('UPDATE Users SET reset_token = ?, reset_expiry = ? WHERE email = ?', (token, expiry, email))
    conn.commit()
    conn.close()
    
    # Generate dynamic links based on the request host
    host_url = request.host_url.rstrip('/')
    reset_link = f"{host_url}/index.html?reset_token={token}"
    reject_link = f"{host_url}/api/auth/reject_reset?token={token}"
    
    email_service.sendPasswordResetEmail(email, reset_link, reject_link)
    
    return jsonify({'status': 'SUCCESS', 'message': 'If the email exists, a password reset request has been generated.'})

@app.route('/api/auth/reset_password', methods=['POST'])
def reset_password():
    data = request.json
    token = data.get('token')
    new_password = data.get('password')
    
    if not token or not new_password:
        return jsonify({'error': 'Token and password are required'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE reset_token = ?', (token,)).fetchone()
    
    if not user:
        conn.close()
        return jsonify({'error': 'Invalid or expired token'}), 400
        
    # Check expiry
    expiry_dt = datetime.strptime(user['reset_expiry'], '%Y-%m-%d %H:%M:%S')
    if datetime.utcnow() > expiry_dt:
        conn.close()
        return jsonify({'error': 'Reset token has expired'}), 400
        
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
    conn.execute('UPDATE Users SET reset_token = NULL, reset_expiry = NULL WHERE reset_token = ?', (token,))
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
