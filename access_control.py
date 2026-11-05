"""Small, side-effect-free authorization helpers for HTTP API guards."""


def admin_access_status(user_id, configured_admin_id):
    """Return None when allowed, otherwise the HTTP status to return."""
    try:
        admin_id = int(configured_admin_id)
    except (TypeError, ValueError):
        return 503
    try:
        authenticated_id = int(user_id)
    except (TypeError, ValueError):
        return 401
    return None if authenticated_id == admin_id else 403
