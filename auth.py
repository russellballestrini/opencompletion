"""Authentication module for email OTP-based authentication"""

import os
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from functools import wraps

from flask import session, jsonify, request
from models import db, User, OTPToken


def generate_otp():
    """Generate a 6-digit OTP code"""
    return ''.join([str(random.randint(0, 9)) for _ in range(6)])


def send_otp_email(email, otp_code):
    """Send OTP code to user's email via SMTP

    Attempts to send via localhost:25 first. If that fails, tries configured SMTP.
    Falls back to console output if all methods fail.

    Optional environment variables (only needed if localhost SMTP unavailable):
    - SMTP_HOST: SMTP server hostname (e.g., smtp.gmail.com)
    - SMTP_PORT: SMTP server port (e.g., 587)
    - SMTP_USER: SMTP username/email
    - SMTP_PASSWORD: SMTP password or app-specific password
    - SMTP_FROM_EMAIL: Email address to send from
    - SMTP_FROM_NAME: Display name for sender
    """
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = int(os.environ.get('SMTP_PORT', '587')) if smtp_host else 587
    smtp_user = os.environ.get('SMTP_USER')
    smtp_password = os.environ.get('SMTP_PASSWORD')
    from_email = os.environ.get('SMTP_FROM_EMAIL', smtp_user or 'noreply@opencompletion.local')
    from_name = os.environ.get('SMTP_FROM_NAME', 'OpenCompletion')

    # Create message
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f'Your OpenCompletion verification code: {otp_code}'
    msg['From'] = f'{from_name} <{from_email}>'
    msg['To'] = email

    # Plain text version
    text = f"""
Your OpenCompletion verification code is: {otp_code}

This code will expire in 10 minutes.

If you didn't request this code, you can safely ignore this email.
"""

    # HTML version
    html = f"""
<html>
  <body style="font-family: Arial, sans-serif; padding: 20px;">
    <h2>Your OpenCompletion Verification Code</h2>
    <p>Enter this code to complete your authentication:</p>
    <h1 style="background-color: #f0f0f0; padding: 15px; text-align: center; letter-spacing: 5px;">
      {otp_code}
    </h1>
    <p style="color: #666;">This code will expire in 10 minutes.</p>
    <p style="color: #999; font-size: 12px;">
      If you didn't request this code, you can safely ignore this email.
    </p>
  </body>
</html>
"""

    # Attach both versions
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    # Try localhost:25 first (common for development with local mail server)
    try:
        with smtplib.SMTP('localhost', 25, timeout=2) as server:
            server.send_message(msg)
        print(f"[INFO] OTP sent via localhost:25 to {email}")
        return True
    except (ConnectionRefusedError, OSError, smtplib.SMTPException) as e:
        # Localhost not available, try configured SMTP if available
        if smtp_host and smtp_user and smtp_password:
            try:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_password)
                    server.send_message(msg)
                print(f"[INFO] OTP sent via {smtp_host} to {email}")
                return True
            except Exception as smtp_error:
                print(f"[ERROR] Failed to send OTP via {smtp_host}: {smtp_error}")

        # Fall back to console output
        print(f"\n{'='*60}")
        print(f"[DEVELOPMENT] OTP Email - localhost:25 unavailable")
        print(f"{'='*60}")
        print(f"To: {email}")
        print(f"Subject: Your OpenCompletion verification code: {otp_code}")
        print(f"\nOTP CODE: {otp_code}")
        print(f"\nThis code expires in 10 minutes.")
        print(f"{'='*60}\n")
        # Return True to allow development workflow
        return True


def create_otp_token(email):
    """Create and store an OTP token for the given email"""
    # Invalidate any existing unused OTP tokens for this email
    existing_tokens = OTPToken.query.filter_by(email=email, used=False).all()
    for token in existing_tokens:
        token.used = True

    # Generate new OTP
    otp_code = generate_otp()
    otp_token = OTPToken(email=email, otp_code=otp_code)

    db.session.add(otp_token)
    db.session.commit()

    return otp_token


def verify_otp(email, otp_code):
    """Verify an OTP code for the given email

    Returns:
        - OTPToken object if valid
        - None if invalid
    """
    otp_token = OTPToken.query.filter_by(
        email=email,
        otp_code=otp_code,
        used=False
    ).first()

    if otp_token and otp_token.is_valid():
        # Mark as used
        otp_token.used = True
        db.session.commit()
        return otp_token

    return None


def get_or_create_user(email):
    """Get existing user by email or return None if doesn't exist"""
    return User.query.filter_by(email=email).first()


def create_user(email, display_name):
    """Create a new user with email and display name"""
    # Check if display name is already taken
    existing_user = User.query.filter_by(display_name=display_name).first()
    if existing_user:
        return None, "Display name already taken"

    # Check if email already exists
    existing_email = User.query.filter_by(email=email).first()
    if existing_email:
        return None, "Email already registered"

    user = User(email=email, display_name=display_name)
    db.session.add(user)
    db.session.commit()

    return user, None


def login_user(user):
    """Create session for authenticated user"""
    session['user_id'] = user.id
    session['user_email'] = user.email
    session['display_name'] = user.display_name
    session.permanent = True  # Use permanent session

    # Update last login
    user.last_login = datetime.utcnow()
    db.session.commit()


def logout_user():
    """Clear user session"""
    session.pop('user_id', None)
    session.pop('user_email', None)
    session.pop('display_name', None)


def get_current_user():
    """Get currently authenticated user from session"""
    user_id = session.get('user_id')
    if user_id:
        return User.query.get(user_id)
    return None


def require_auth(f):
    """Decorator to require authentication for a route"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Authentication required'}), 401
        return f(*args, **kwargs)
    return decorated_function


def is_authenticated():
    """Check if current request is authenticated"""
    return 'user_id' in session
