"""Short unique codes for bank / M-Pesa reference matching."""
import secrets

# Ambiguous characters dropped so codes stay easy to read aloud (no 0/O/1/I).
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_match_code(prefix="PG", length=5):
    """Short unique-looking code for bank references (e.g. PG7K2M, CM4NP8)."""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    return f"{prefix}{body}"
