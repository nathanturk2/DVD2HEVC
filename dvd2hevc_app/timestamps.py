"""Read current and historical job timestamps on every supported Python minor."""
from datetime import datetime
import re


def parse_timestamp(value):
    text = str(value or '').strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    # strftime('%z') writes +HHMM; Python 3.10 fromisoformat needs +HH:MM.
    text = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', text)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
