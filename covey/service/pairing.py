#!/usr/bin/env python3
"""Issue a one-time owner pairing code for an explicitly configured bot app."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import time

from covey.core.state import read_json, write_durable_json as save


def issue(state, app_id):
    identity = state / 'bot-identity.json'
    if identity.exists() and read_json(identity).get('owner'):
        raise ValueError('bot already has an owner; pairing cannot replace it')
    code = secrets.token_urlsafe(24)
    path = state / 'owner-pairing.json'
    save(path, {'app_id': app_id, 'sha256': hashlib.sha256(code.encode()).hexdigest(),
                'expires_at': time.time() + 86400})
    return code, path


def main():
    p = argparse.ArgumentParser(prog="covey_pairing.py", description=__doc__)
    p.add_argument('--state-dir', type=Path, required=True)
    p.add_argument('--app-id', required=True)
    args = p.parse_args()
    code, path = issue(args.state_dir.expanduser().resolve(), args.app_id)
    print(json.dumps({'pairing_file': str(path), 'message': '/pair ' + code, 'expires_in_hours': 24}))


if __name__ == '__main__':
    main()
