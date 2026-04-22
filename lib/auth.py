"""
RECON Auth Helper — extract user identity from Authentik forward-auth headers.
"""
from functools import wraps
from flask import request, jsonify


def get_user_id():
    """Return X-Authentik-Username or None."""
    return request.headers.get('X-Authentik-Username')


def require_auth(f):
    """Decorator: 401 if no Authentik auth header."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user_id = get_user_id()
        if not user_id:
            return jsonify({'error': 'Authentication required'}), 401
        request.user_id = user_id
        return f(*args, **kwargs)
    return wrapper
