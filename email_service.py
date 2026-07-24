import os
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pytz
from dotenv import load_dotenv
import threading

# Load environment variables
load_dotenv()

# Configuration
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_USERNAME = os.getenv('SMTP_USERNAME', '')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
FROM_EMAIL = os.getenv('FROM_EMAIL', SMTP_USERNAME)
FROM_NAME = os.getenv('FROM_NAME', 'AMS Support')

# Setup Logging
logger = logging.getLogger('EmailService')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
if not logger.hasHandlers():
    logger.addHandler(ch)

def get_ist_time():
    """Returns current time in IST in 12-hour format."""
    try:
        ist_tz = pytz.timezone('Asia/Kolkata')
        ist_now = datetime.now(ist_tz)
        return ist_now.strftime('%I:%M %p, %b %d, %Y')
    except Exception:
        return datetime.now().strftime('%I:%M %p, %b %d, %Y')

def _send_email_task(to_email, subject, html_content):
    if not SMTP_USERNAME or not SMTP_PASSWORD:
        logger.warning(f"Email Sent (Mock) | Recipient: {to_email} | Subject: {subject} | Timestamp: {get_ist_time()} | Status: Failed | Reason: SMTP credentials missing")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = to_email

        part = MIMEText(html_content, "html")
        msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())
            
        logger.info(f"Email Sent | Recipient: {to_email} | Subject: {subject} | Timestamp: {get_ist_time()} | Status: Success | Delivery Result: OK")
        return True
    except Exception as e:
        logger.error(f"Email Sent | Recipient: {to_email} | Subject: {subject} | Timestamp: {get_ist_time()} | Status: Error | Delivery Result: {str(e)}")
        return False

def _send_email_async(to_email, subject, html_content):
    """Sends email in a background thread to prevent blocking the main application."""
    thread = threading.Thread(target=_send_email_task, args=(to_email, subject, html_content))
    thread.daemon = True
    thread.start()

def _get_base_template(content):
    return f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
            <div style="text-align: center; margin-bottom: 20px;">
                <h2 style="color: #0056b3;">Advanced Approval Management System</h2>
            </div>
            <div style="background-color: #f9f9f9; padding: 20px; border-radius: 5px;">
                {content}
            </div>
            <div style="margin-top: 20px; font-size: 12px; color: #777; text-align: center;">
                <p>Generated on: {get_ist_time()} (IST)</p>
                <p>&copy; {datetime.now().year} AMS. All rights reserved.</p>
            </div>
        </body>
    </html>
    """

def sendUserRegistrationNotification(admin_email, user_email, requested_role):
    subject = "New User Registration Request"
    content = f"""
    <h3>Hello Admin,</h3>
    <p>A new user has requested access to the Advanced Approval Management System.</p>
    <h4>User Information</h4>
    <ul>
        <li><strong>Email:</strong> {user_email}</li>
        <li><strong>Requested Role:</strong> {requested_role}</li>
        <li><strong>Registration Time:</strong> {get_ist_time()}</li>
        <li><strong>Current Status:</strong> Pending Approval</li>
    </ul>
    <p>Please review this request from the Admin Dashboard.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(admin_email, subject, html)

def sendUserApprovedEmail(user_email, user_name):
    subject = "Your Account Has Been Approved"
    content = f"""
    <h3>Hello {user_name},</h3>
    <p>Congratulations.</p>
    <p>Your account has been approved by the Administrator.</p>
    <p>You now have access to the Advanced Approval Management System.</p>
    <p>You may now log in and begin submitting approval requests.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(user_email, subject, html)

def sendUserRejectedEmail(user_email, user_name):
    subject = "Account Registration Update"
    content = f"""
    <h3>Hello {user_name},</h3>
    <p>Your registration request has been reviewed.</p>
    <p>Unfortunately, your account has not been approved.</p>
    <p>If you believe this is an error, please contact the system administrator.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(user_email, subject, html)

def sendAdminRegistrationNotification(superadmin_email, admin_email):
    subject = "New Administrator Registration Request"
    content = f"""
    <h3>Hello Super Admin,</h3>
    <p>A new administrator account has been requested.</p>
    <h4>Applicant Details</h4>
    <ul>
        <li><strong>Email:</strong> {admin_email}</li>
        <li><strong>Registration Time:</strong> {get_ist_time()}</li>
        <li><strong>Status:</strong> Pending Approval</li>
    </ul>
    <p>Please review this request from the Super Admin Dashboard.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(superadmin_email, subject, html)

def sendAdminApprovedEmail(admin_email, admin_name):
    subject = "Administrator Access Approved"
    content = f"""
    <h3>Hello {admin_name},</h3>
    <p>Congratulations.</p>
    <p>Your administrator account has been approved.</p>
    <p>You now have access to the Admin Dashboard.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(admin_email, subject, html)

def sendAdminRejectedEmail(admin_email, admin_name):
    subject = "Administrator Registration Update"
    content = f"""
    <h3>Hello {admin_name},</h3>
    <p>Your administrator registration request has been reviewed.</p>
    <p>Unfortunately, your request has been rejected.</p>
    <p>Please contact the Super Administrator if additional information is required.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(admin_email, subject, html)

def sendWelcomeEmail(user_email, user_name):
    subject = "Welcome to the Advanced Approval Management System"
    content = f"""
    <h3>Hello {user_name},</h3>
    <p>Welcome to the Advanced Approval Management System.</p>
    <p>We are pleased to have you onboard.</p>
    <p>Thank you.</p>
    """
    html = _get_base_template(content)
    _send_email_async(user_email, subject, html)

def sendPasswordResetEmail(user_email, reset_link, reject_link):
    subject = "Password Reset Request"
    content = f"""
    <h3>Hello,</h3>
    <p>We received a request to reset your password for your Advanced Approval Management System account.</p>
    <p>Please confirm your request by clicking one of the buttons below:</p>
    <div style="margin: 20px 0;">
        <a href="{reset_link}" style="background-color: #28a745; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block; margin-right: 10px;">Reset Password</a>
        <a href="{reject_link}" style="background-color: #dc3545; color: white; padding: 10px 20px; text-decoration: none; border-radius: 5px; display: inline-block;">Reject this Request</a>
    </div>
    <p style="font-size: 13px; color: #555; background: #eee; padding: 10px; border-left: 4px solid #0056b3;">
        <strong>Note:</strong> After you confirm the reset password, please set the new Password by logging into your account through the Portal. Thank You.
    </p>
    <p>If you did not request this, please click "Reject this Request" immediately.</p>
    """
    html = _get_base_template(content)
    _send_email_async(user_email, subject, html)
