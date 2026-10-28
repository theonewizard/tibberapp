"""Print an APP_PASSWORD_HASH line for .env:  python -m app.hash_password"""

import getpass

from .security import hash_password

if __name__ == "__main__":
    first = getpass.getpass("New password: ")
    if len(first) < 12:
        raise SystemExit("Use at least 12 characters.")
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("Passwords differ.")
    # Single quotes keep Docker Compose from interpolating the $ signs in the hash.
    print(f"APP_PASSWORD_HASH='{hash_password(first)}'")
