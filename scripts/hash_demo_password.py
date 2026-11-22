"""Prompt for a demo password and print its salted verifier."""
from getpass import getpass

from backend.app.auth import hash_password


def main() -> None:
    password = getpass("Demo password: ")
    if not 1 <= len(password) <= 1024:
        raise SystemExit("Enter a password of 1 to 1024 characters.")
    if password != getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    print(hash_password(password))


if __name__ == "__main__":
    main()
