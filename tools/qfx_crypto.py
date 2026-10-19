#!/usr/bin/env python3
"""This is a repo for QF."""
import os, sys, hashlib, base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
MAGIC = b'QFX1'
DEFAULT_KEY_FILE = '/data/qf-exe/keys/qfx_master.key'
def loadKey(path=None):
    hexKey = os.environ.get('QFX_KEY', '').strip()
    if not hexKey:
        p = path or os.environ.get('QFX_KEY_FILE') or DEFAULT_KEY_FILE
        with open(p) as f:
            hexKey = f.read().strip()
    key = bytes.fromhex(hexKey)
    if len(key) != 32:
        raise SystemExit('QFX key must be 32 bytes in hex')
    return key
def encrypt(key, data, aad=b''):
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC + aad)
def decrypt(key, blob, aad=b''):
    if blob[:4] != MAGIC:
        raise ValueError('not a QFX1 blob')
    return AESGCM(key).decrypt(blob[4:16], blob[16:], MAGIC + aad)
def sha16(b):
    return hashlib.sha256(b).hexdigest()[:16]
def toParts(text, size=600000):
    return [text[i:i + size] for i in range(0, len(text), size)]
if __name__ == '__main__':
    if len(sys.argv) != 5 or sys.argv[1] not in ('enc', 'dec'):
        raise SystemExit('usage: qfx_crypto.py enc|dec TASK_ID IN OUT')
    mode, tid, src, dst = sys.argv[1:]
    key = loadKey()
    data = open(src, 'rb').read()
    out = encrypt(key, data, tid.encode()) if mode == 'enc' else decrypt(key, data, tid.encode())
    open(dst, 'wb').write(out)
    print(mode, tid, len(data), '->', len(out), sha16(out))
