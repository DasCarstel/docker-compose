#!/usr/bin/env python3
"""
Encryption Sidecar for Linkwarden Archives
- Decrypts .enc files from /encrypted to /decrypted on startup
- Syncs changes back every SYNC_INTERVAL seconds (default: 120s)
- Uses AES-256-GCM with scrypt key derivation
- Only encrypts changed files (CPU efficient)
"""

import os
import sys
import time
import signal
import hashlib
import logging
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# Config
ENCRYPTED_DIR = Path("/encrypted")
DECRYPTED_DIR = Path("/decrypted")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "120"))  # 2 minutes
PASSWORD = os.environ["ENCRYPTION_PASSWORD"]
SALT_FILE = ENCRYPTED_DIR / ".salt"
METADATA_FILE = DECRYPTED_DIR / ".sync_metadata"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

shutdown = False

def derive_key(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    return kdf.derive(password.encode())

def get_or_create_salt() -> bytes:
    if SALT_FILE.exists():
        return SALT_FILE.read_bytes()
    salt = os.urandom(16)
    SALT_FILE.write_bytes(salt)
    return salt

def encrypt_file(plain_path: Path, key: bytes) -> Path:
    """Encrypt a single file, returns path to encrypted file"""
    enc_path = ENCRYPTED_DIR / (plain_path.relative_to(DECRYPTED_DIR).as_posix() + ".enc")
    enc_path.parent.mkdir(parents=True, exist_ok=True)
    
    data = plain_path.read_bytes()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, data, None)
    
    enc_path.write_bytes(nonce + ciphertext)
    return enc_path

def decrypt_file(enc_path: Path, key: bytes) -> Path:
    """Decrypt a single file, returns path to decrypted file"""
    rel = enc_path.relative_to(ENCRYPTED_DIR).as_posix()
    if rel.endswith(".enc"):
        rel = rel[:-4]
    plain_path = DECRYPTED_DIR / rel
    plain_path.parent.mkdir(parents=True, exist_ok=True)
    
    data = enc_path.read_bytes()
    nonce, ciphertext = data[:12], data[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    
    plain_path.write_bytes(plaintext)
    return plain_path

def get_file_hash(path: Path) -> str:
    """Quick hash for change detection"""
    try:
        stat = path.stat()
        return f"{stat.st_mtime:.3f}:{stat.st_size}"
    except:
        return ""

def load_metadata() -> dict:
    """Load last sync metadata"""
    if METADATA_FILE.exists():
        try:
            import json
            return json.loads(METADATA_FILE.read_text())
        except:
            return {}
    return {}

def save_metadata(metadata: dict):
    """Save sync metadata"""
    import json
    METADATA_FILE.write_text(json.dumps(metadata))

def decrypt_all(key: bytes):
    """Decrypt all .enc files on startup"""
    logger.info("Decrypting all files...")
    count = 0
    for enc_path in ENCRYPTED_DIR.rglob("*.enc"):
        try:
            decrypt_file(enc_path, key)
            count += 1
        except Exception as e:
            logger.error(f"Failed to decrypt {enc_path}: {e}")
    logger.info(f"Decrypted {count} files")

def sync_to_encrypted(key: bytes, metadata: dict) -> dict:
    """Sync changed files from decrypted to encrypted"""
    new_metadata = {}
    changed = 0
    
    for plain_path in DECRYPTED_DIR.rglob("*"):
        if plain_path.is_file() and plain_path.name == ".sync_metadata":
            continue
        
        rel = plain_path.relative_to(DECRYPTED_DIR).as_posix()
        current_hash = get_file_hash(plain_path)
        old_hash = metadata.get(rel, "")
        
        if current_hash != old_hash:
            try:
                encrypt_file(plain_path, key)
                new_metadata[rel] = current_hash
                changed += 1
            except Exception as e:
                logger.error(f"Failed to encrypt {plain_path}: {e}")
        else:
            new_metadata[rel] = current_hash
    
    if changed:
        save_metadata(new_metadata)
        logger.info(f"Synced {changed} changed files to encrypted storage")
    
    return new_metadata

def main():
    global shutdown
    
    def handle_signal(sig, frame):
        global shutdown
        logger.info("Shutdown signal received, syncing before exit...")
        shutdown = True
    
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    
    # Setup
    salt = get_or_create_salt()
    key = derive_key(PASSWORD, salt)
    logger.info("Key derived successfully")
    
    # Initial decrypt
    decrypt_all(key)
    
    # Load metadata
    metadata = load_metadata()
    
    # Main loop
    logger.info(f"Starting sync loop (interval: {SYNC_INTERVAL}s)")
    while not shutdown:
        time.sleep(SYNC_INTERVAL)
        if not shutdown:
            metadata = sync_to_encrypted(key, metadata)
    
    # Final sync on shutdown
    logger.info("Final sync before shutdown...")
    sync_to_encrypted(key, metadata)
    logger.info("Encryption sidecar stopped")

if __name__ == "__main__":
    main()
