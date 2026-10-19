#!/usr/bin/env python3
"""This is a repo for QF."""
import os, sys, io, re, json, time, glob, base64, tarfile, argparse, secrets
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qfx_crypto as qc
OWNER, REPO, BRANCH = 'Nicholas022400701', 'QF-Exe', 'main'
BASE = '/data/qf-exe'
MAX_B64 = 900000
def packDir(d):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        t.add(d, arcname='.')
    return buf.getvalue()
def encryptTask(d, tid, spec):
    with open(os.path.join(d, 'task.json'), 'w') as f:
        json.dump(spec, f)
    b64 = base64.b64encode(qc.encrypt(qc.loadKey(), packDir(d), tid.encode())).decode()
    if len(b64) > MAX_B64:
        raise SystemExit(f'task payload {len(b64)} chars exceeds {MAX_B64}; let run.sh download large inputs instead')
    return b64
def submit(a):
    if not os.path.exists(os.path.join(a.dir, 'run.sh')):
        raise SystemExit('task dir needs run.sh')
    slug = re.sub(r'[^a-z0-9]+', '-', a.slug.lower()).strip('-')[:24] or 'task'
    envs = dict(kv.split('=', 1) for kv in (a.env or []))
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    shards = max(1, a.shards)
    files, ids = [], []
    for i in range(shards):
        tid = stamp + f'-{slug}' + (f'-s{i:02d}' if shards > 1 else '') + f'-{secrets.token_hex(2)}'
        e = dict(envs)
        if shards > 1:
            e.update(QFX_SHARD=str(i), QFX_SHARDS=str(shards))
        spec = {'budget_seconds': a.budget, 'max_attempts': a.max_attempts, 'fresh_job': bool(a.fresh_job), 'env': e, 'assets': a.asset or []}
        b64 = encryptTask(a.dir, tid, spec)
        files.append({'path': f'queue/pending/{tid}.qfx.b64', 'content': b64})
        ids.append(tid)
        print('task_id', tid, 'payload_chars', len(b64), 'sha16', qc.sha16(b64.encode()))
    out = os.path.join(BASE, 'outbox', ids[0] if shards == 1 else stamp + f'-{slug}-shards')
    os.makedirs(out, exist_ok=True)
    batches, cur, size = [], [], 0
    for f in files:
        if cur and size + len(f['content']) > MAX_B64:
            batches.append(cur); cur, size = [], 0
        cur.append(f); size += len(f['content'])
    batches.append(cur)
    for j, b in enumerate(batches):
        args = {'owner': OWNER, 'repo': REPO, 'branch': BRANCH, 'message': f'qfx: submit {slug} ({len(b)} task(s))', 'files': b}
        p = os.path.join(out, 'push_args.json' if len(batches) == 1 else f'push_args_{j:02d}.json')
        with open(p, 'w') as f:
            json.dump(args, f)
        print('push_args', p)
    with open(os.path.join(out, 'task_ids.txt'), 'w') as f:
        f.write('\n'.join(ids) + '\n')
def asset(a):
    name = re.sub(r'[^A-Za-z0-9_.-]+', '-', a.name).strip('-')
    blob = qc.encrypt(qc.loadKey(), packDir(a.dir), f'asset:{name}'.encode())
    b64 = base64.b64encode(blob).decode()
    parts = [b64[i:i + 600000] for i in range(0, len(b64), 600000)] or ['']
    out = os.path.join(BASE, 'outbox', f'asset-{name}')
    os.makedirs(out, exist_ok=True)
    batches, cur, size = [], [], 0
    for i, ptxt in enumerate(parts):
        f = {'path': f'assets/{name}/asset.part{i:03d}.b64', 'content': ptxt}
        if cur and size + len(ptxt) > MAX_B64:
            batches.append(cur); cur, size = [], 0
        cur.append(f); size += len(ptxt)
    batches.append(cur)
    for j, b in enumerate(batches):
        args = {'owner': OWNER, 'repo': REPO, 'branch': BRANCH, 'message': f'qfx: asset {name} ({j + 1}/{len(batches)})', 'files': b}
        p = os.path.join(out, f'push_args_{j:02d}.json')
        with open(p, 'w') as fh:
            json.dump(args, fh)
        print('push_args', p)
    print('asset', name, 'blob_bytes', len(blob), 'sha16', qc.sha16(blob), 'parts', len(parts), 'pushes', len(batches))
def paths(a):
    print(f'results/{a.task_id}/status.json')
    print(f'results/{a.task_id}/')
def extractB64(text):
    runs = re.findall(r'[A-Za-z0-9+/]{64,}={0,2}', text)
    return max(runs, key=len) if runs else ''
def collect(a):
    chunks = []
    for p in a.files:
        raw = open(p, 'r', errors='replace').read()
        body = extractB64(raw.replace('\\n', '').replace('\\/', '/'))
        m = re.search(r'part(\d{3})', os.path.basename(p)) or re.search(r'part(\d{3})\.b64', raw)
        idx = int(m.group(1)) if m else len(chunks)
        chunks.append((idx, body))
    chunks.sort()
    blob = base64.b64decode(''.join(c for _, c in chunks))
    data = qc.decrypt(qc.loadKey(), blob, a.task_id.encode())
    dst = os.path.join(BASE, 'inbox', a.task_id, f'attempt{a.attempt:02d}' if a.attempt else 'latest')
    os.makedirs(dst, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        try:
            t.extractall(dst, filter='data')
        except TypeError:
            t.extractall(dst)
    print('decrypted', len(blob), 'bytes blob sha16', qc.sha16(blob), '->', dst)
    for root, _, files in os.walk(dst):
        for f in sorted(files):
            fp = os.path.join(root, f)
            print(' ', os.path.relpath(fp, dst), os.path.getsize(fp))
def dispatch(a):
    args = {'method': 'run_workflow', 'owner': OWNER, 'repo': REPO, 'workflow_id': 'qfx-worker.yml', 'ref': BRANCH,
            'inputs': {'task_id': a.task_id or '', 'workers': str(a.workers) if a.workers else ''}}
    p = os.path.join(BASE, 'outbox', f'dispatch_{int(time.time())}.json')
    with open(p, 'w') as f:
        json.dump(args, f)
    print('dispatch_args', p)
if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('submit'); s.add_argument('dir'); s.add_argument('--slug', required=True); s.add_argument('--budget', type=int, default=19800); s.add_argument('--max-attempts', type=int, default=3); s.add_argument('--fresh-job', action='store_true', help='each continuation attempt starts in a fresh job'); s.add_argument('--env', action='append', metavar='K=V'); s.add_argument('--shards', type=int, default=1); s.add_argument('--asset', action='append', metavar='NAME', help='shared asset to unpack into $QFX_WORK/assets/NAME/'); s.set_defaults(fn=submit)
    s = sp.add_parser('asset'); s.add_argument('dir'); s.add_argument('--name', required=True); s.set_defaults(fn=asset)
    s = sp.add_parser('paths'); s.add_argument('task_id'); s.set_defaults(fn=paths)
    s = sp.add_parser('collect'); s.add_argument('task_id'); s.add_argument('files', nargs='+'); s.add_argument('--attempt', type=int, default=0); s.set_defaults(fn=collect)
    s = sp.add_parser('dispatch'); s.add_argument('task_id', nargs='?', default=''); s.add_argument('--workers', type=int, default=0); s.set_defaults(fn=dispatch)
    a = ap.parse_args()
    a.fn(a)
