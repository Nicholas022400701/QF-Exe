#!/usr/bin/env python3
"""This is a repo for QF."""
import os, glob, json, time
MAX_WORKERS = 40
STALE_CLAIM = 7 * 3600
def main():
    forced = os.environ.get('QFX_WORKERS', '').strip()
    target = os.environ.get('QFX_TASK_ID', '').strip()
    pending = len(glob.glob('queue/pending/*.qfx.b64'))
    stale = 0
    for cp in glob.glob('queue/running/*.claim.json'):
        try:
            c = json.load(open(cp))
        except Exception:
            c = {}
        if time.time() - c.get('claimed_epoch', 0) > STALE_CLAIM:
            stale += 1
    if forced:
        n = int(forced)
    elif target:
        n = max(1, pending + stale)
    else:
        n = pending + stale
    n = max(0, min(MAX_WORKERS, n))
    matrix = {'slot': list(range(1, n + 1))}
    print(f'event {os.environ.get("QFX_EVENT")} pending {pending} stale {stale} forced {forced!r} target {target!r} -> workers {n}')
    with open(os.environ.get('GITHUB_OUTPUT', '/dev/stdout'), 'a') as f:
        f.write(f'count={n}\n')
        f.write(f'matrix={json.dumps(matrix)}\n')
if __name__ == '__main__':
    main()
