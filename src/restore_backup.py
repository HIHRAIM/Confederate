"""Decrypt a *.db.enc backup produced by backup_crypto (standard library only).

Usage (set BACKUP_KEY to THIS project's key first):

    python restore_backup.py bridge.db.enc bridge.db
"""
import os
import sys

from backup_crypto import decrypt_bytes

def main():
    if len(sys.argv) != 3:
        print("usage: python restore_backup.py <input.db.enc> <output.db>")
        return 2
    key = os.environ.get("BACKUP_KEY")
    if not key:
        print("set the BACKUP_KEY environment variable to this project's key")
        return 2
    with open(sys.argv[1], "rb") as f:
        blob = f.read()
    try:
        plaintext = decrypt_bytes(key.encode("utf-8"), blob)
    except Exception as e:
        print(f"decryption failed: {e}")
        return 1
    with open(sys.argv[2], "wb") as f:
        f.write(plaintext)
    print(f"restored {sys.argv[1]} -> {sys.argv[2]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
