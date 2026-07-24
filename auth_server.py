from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
import os
import email_service
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

DB_FILE = 'auth.db'

# --- HARDCODED SUPER ADMIN CONFIGURATION ---
# To add a new super admin in the future, simply append their email to this list.
SUPER_ADMINS = [
    'superadmin.main.01@gmail.com'
]
# For prototype purposes, Super Admins use this password (now loaded from env).
SUPER_ADMIN_PASSWORD = os.getenv('SUPER_ADMIN_PASSWORD', 'superadminpass')

def init_db():
    """Initializes the SQLite database and creates the Users table if it doesn't exist."""
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
    conn.commit()
    conn.close()

# Initialize DB on startup
if not os.path.exists(DB_FILE):
    print("Initializing Auth Database...")
    init_db()
else:
    # Ensure table exists even if file is present
    init_db()

def check_and_add_columns():
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
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

# --- API ENDPOINTS ---

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    password = data.get('password')

    if not email:
        return jsonify({'error': 'Email is required'}), 400

    # 1. Super Admin Check
    if email in SUPER_ADMINS:
        if password == SUPER_ADMIN_PASSWORD:
            return jsonify({
                'status': 'SUCCESS',
                'role': 'SuperAdmin',
                'message': 'Welcome, Super Admin.',
                'redirect': 'super_admin.html'
            })
        else:
            return jsonify({'error': 'Invalid Super Admin password'}), 401

    # 2. Standard User Check
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
            redirect_page = 'admin.html' if user['role'] == 'Admin' else 'user.html'
            return jsonify({
                'status': 'SUCCESS',
                'role': user['role'],
                'message': 'Login successful.',
                'redirect': redirect_page
            })
        else:
            return jsonify({'error': 'Invalid password'}), 401
    
    return jsonify({'error': 'Unknown status'}), 500


@app.route('/api/auth/request_access', methods=['POST'])
def request_access():
    data = request.json
    email = data.get('email')
    role = data.get('role', 'User')  # Can request 'Admin' or 'User'

    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    if role not in ['User', 'Admin']:
        return jsonify({'error': 'Invalid role requested.'}), 400

    if email in SUPER_ADMINS:
        return jsonify({'error': 'This email is reserved for Super Admins.'}), 400

    conn = get_db_connection()
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

    # --- EMAIL NOTIFICATION ---
    if role == 'Admin':
        for sa_email in SUPER_ADMINS:
            email_service.sendAdminRegistrationNotification(sa_email, email)
    else:
        conn = get_db_connection()
        active_admins = conn.execute("SELECT email FROM Users WHERE role = 'Admin' AND status = 'Active'").fetchall()
        conn.close()
        
        admin_emails = [a['email'] for a in active_admins]
        all_notifiers = list(set(SUPER_ADMINS + admin_emails))
        
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

    # --- EMAIL NOTIFICATION ---
    name = email.split('@')[0]
    email_service.sendWelcomeEmail(email, name)

    redirect_page = 'admin.html' if user['role'] == 'Admin' else 'user.html'
    return jsonify({
        'status': 'SUCCESS', 
        'message': 'Password set successfully. Account is now active.',
        'redirect': redirect_page
    })

# The following endpoints are for SuperAdmin/Admin to approve users
@app.route('/api/auth/pending_users', methods=['GET'])
def get_pending_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, role, status, created_at FROM Users WHERE status = "Pending"').fetchall()
    conn.close()
    
    users_list = [dict(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/users', methods=['GET'])
def get_all_users():
    conn = get_db_connection()
    users = conn.execute('SELECT id, email, name, role, status, created_at FROM Users').fetchall()
    conn.close()
    
    users_list = [dict(u) for u in users]
    return jsonify(users_list)

@app.route('/api/auth/delete_user', methods=['POST'])
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

@app.route('/api/auth/request_password_reset', methods=['POST'])
def request_password_reset():
    data = request.json
    email = data.get('email')
    
    if not email:
        return jsonify({'error': 'Email is required'}), 400
        
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM Users WHERE email = ?', (email,)).fetchone()
    
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
        
    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
    
    conn.execute('UPDATE Users SET reset_token = ?, reset_expiry = ? WHERE email = ?', (token, expiry, email))
    conn.commit()
    conn.close()
    
    reset_link = f"http://localhost:5000/index.html?reset_token={token}"
    reject_link = f"http://localhost:5001/api/auth/reject_reset?token={token}"
    
    # Send the password reset email
    email_service.sendPasswordResetEmail(email, reset_link, reject_link)
    
    return jsonify({'status': 'SUCCESS', 'message': 'Password reset request generated and email sent.'})

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

if __name__ == '__main__':
    # Run the Auth Microservice on Port 5001 so it doesn't conflict with ML Server on Port 5000
    print("Starting Auth Microservice on Port 5001...")
    app.run(port=5001, debug=False)
