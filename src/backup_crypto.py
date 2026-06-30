"""Encrypted, consistent SQLite backups — standard library only.

Builds an authenticated-encrypted snapshot of the database:

  * The snapshot is taken with SQLite's online-backup API, so it is internally
    consistent even while the bot keeps writing (the DB runs in WAL mode).
  * The snapshot bytes are encrypted with an authenticated stream cipher built on
    BLAKE2: a keyed-BLAKE2 keystream in counter mode (XORed with the plaintext),
    plus a keyed-BLAKE2 tag over the ciphertext (encrypt-then-MAC). This uses only
    the Python standard library, so the bot needs no third-party crypto package.
    Whatever stores the file only ever sees ciphertext it cannot read or tamper
    with undetected.

The key is read from the BACKUP_KEY environment variable and is DIFFERENT per
project. Keep it out of the repo and store a copy somewhere other than the
server (password manager + offline copy): if the key is lost, every existing
backup becomes permanently unreadable.

Restore a backup with restore_backup.py.
"""
import hashlib
import hmac
import os
import sqlite3
import struct
import tempfile

ENC_SUFFIX = ".enc"
_MAGIC = b"BKP1"

def _consistent_snapshot_bytes(db_path):
    """Return the bytes of a consistent copy of db_path (safe under WAL writes)."""
    fd, tmp = tempfile.mkstemp(suffix=".dbsnap")
    os.close(fd)
    leftovers = (tmp, tmp + "-wal", tmp + "-shm", tmp + "-journal")
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(tmp)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        for path in leftovers:
            try:
                os.remove(path)
            except OSError:
                pass

def _master_key():
    key = os.environ.get("BACKUP_KEY")
    if not key:
        raise RuntimeError(
            "BACKUP_KEY is not set - refusing to produce an unencrypted backup"
        )
    return key.encode("utf-8")

def _subkeys(master):
    base = hashlib.sha256(master).digest()
    k_enc = hashlib.blake2b(b"enc", key=base, digest_size=32).digest()
    k_mac = hashlib.blake2b(b"mac", key=base, digest_size=32).digest()
    return k_enc, k_mac

def _keystream(k_enc, nonce, length):
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hashlib.blake2b(nonce + struct.pack(">Q", counter), key=k_enc, digest_size=64).digest()
        counter += 1
    return bytes(out[:length])

def _xor(a, b):
    if not a:
        return b""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")

def encrypt_bytes(master, plaintext):
    """Authenticated-encrypt plaintext. master = BACKUP_KEY bytes. Returns bytes."""
    k_enc, k_mac = _subkeys(master)
    nonce = os.urandom(16)
    ciphertext = _xor(plaintext, _keystream(k_enc, nonce, len(plaintext)))
    tag = hashlib.blake2b(nonce + ciphertext, key=k_mac, digest_size=32).digest()
    return _MAGIC + nonce + tag + ciphertext

def decrypt_bytes(master, blob):
    """Verify and decrypt a blob produced by encrypt_bytes. Returns plaintext bytes."""
    if len(blob) < 52 or blob[:4] != _MAGIC:
        raise ValueError("Unrecognized backup format")
    nonce, tag, ciphertext = blob[4:20], blob[20:52], blob[52:]
    k_enc, k_mac = _subkeys(master)
    expected = hashlib.blake2b(nonce + ciphertext, key=k_mac, digest_size=32).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Authentication failed - wrong key or corrupted backup")
    return _xor(ciphertext, _keystream(k_enc, nonce, len(ciphertext)))

def build_encrypted_backup(db_path):
    """Consistent snapshot of db_path, encrypted and authenticated. Returns bytes."""
    return encrypt_bytes(_master_key(), _consistent_snapshot_bytes(db_path))

def encrypted_filename(db_path):
    """Suggested upload filename, e.g. 'bridge.db' -> 'bridge.db.enc'."""
    return os.path.basename(db_path) + ENC_SUFFIX
