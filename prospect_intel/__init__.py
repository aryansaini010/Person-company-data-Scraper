"""Prospect intelligence core. Loads .env (keys, URLs, quotas) if present."""
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass
