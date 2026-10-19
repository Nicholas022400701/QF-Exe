#!/usr/bin/env python3
"""This is a repo for QF."""
import os, sys, io, json, time, glob, base64, shutil, tarfile, tempfile, subprocess, random, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qfx_crypto as qc
REPO = os.environ.get('GITHUB_REPOSITORY', 'Nicholas022400701/QF-Exe')
API = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
TOKEN = os.environ.get('GH_TOKEN', '')
RUN_ID = os.environ.get('GITHUB_RUN_ID', '0')
SLOT = os.environ.get('QFX_SLOT', '1')
WORKERS = int(os.environ.get('QFX_WORKERS', '1') or 1)
WORKFLOW_FILE = 'qfx-worker.yml'
JOB_START = time.time()
JOB_BUDGET = int(os.environ.get('QFX_JOB_BUDGET', '19200'))
RESERVE = 420
MIN_SLICE = 600
PART = 600000
STALE_CLAIM = 7 * 3600
PENDING, RUNNING, DONE, RESULTS, STATE = 'queue/pending', 'queue/running', 'queue/done', 'results', 'state'
def log(*a):
    print(time.strftime('%H:%M:%SZ', time.gmtime()), f'[slot {SLOT}]', *a, flush=True)
def nowIso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
def sh(cmd, check=True, **kw):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    if check and r.returncode != 0:
        raise RuntimeError(f'command failed: {cmd}: {r.stderr[-600:]}')
    return r
def readJson(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default
def writeJson(p, obj):
    os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
    with open(p, 'w') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
def gitSync():
    sh('git fetch -q origin main')
    sh('git reset -q --hard origin/main')
def mutate(fn, msg, retries=40):
    for i in range(retries):
        gitSync()
        try:
            fn()
        except StopIteration:
            return False
        sh('git add -A')
        if sh('git diff --cached --quiet', check=False).returncode == 0:
            return True
        sh(['git', 'commit', '-q', '-m', msg() if callable(msg) else msg])
        if sh('git push -q origin HEAD:main', check=False).returncode == 0:
            return True
        log('push rejected, retrying', i + 1)
        time.sleep(min(15.0, 1 + random.random() * 5 + 0.5 * i))
    raise RuntimeError('could not push after retries: ' + (msg() if callable(msg) else msg))
def dispatch(taskId=''):
    if not TOKEN:
        log('no token; cannot dispatch')
        return False
    body = json.dumps({'ref': 'main', 'inputs': {'task_id': taskId}}).encode()
    req = urllib.request.Request(f'{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches', data=body, method='POST',
        headers={'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log('dispatched continuation', taskId or '(queue)', 'http', r.status)
            return True
    except Exception as e:
        log('dispatch failed:', str(e)[:200])
        return False
def packDir(d):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        t.add(d, arcname='.')
    return buf.getvalue()
def unpackTo(data, d):
    os.makedirs(d, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as t:
        try:
            t.extractall(d, filter='data')
        except TypeError:
            t.extractall(d)
def writeParts(dstDir, blob, prefix):
    b64 = base64.b64encode(blob).decode()
    os.makedirs(dstDir, exist_ok=True)
    parts = []
    for i, chunk in enumerate(qc.toParts(b64, PART)):
        name = f'{prefix}.part{i:03d}.b64'
        with open(os.path.join(dstDir, name), 'w') as f:
            f.write(chunk)
        parts.append({'name': name, 'chars': len(chunk), 'sha16': qc.sha16(chunk.encode())})
    return parts, len(blob), qc.sha16(blob)
def readParts(srcDir, prefix):
    names = sorted(glob.glob(os.path.join(srcDir, f'{prefix}.part*.b64')))
    return base64.b64decode(''.join(open(n).read().strip() for n in names))
def taskPath(stage, taskId):
    return f'{stage}/{taskId}.qfx.b64'
def claimPath(taskId):
    return f'{RUNNING}/{taskId}.claim.json'
def pendingIds():
    return sorted(os.path.basename(n)[:-8] for n in glob.glob(f'{PENDING}/*.qfx.b64'))
def claimHeldByOther(taskId):
    c = readJson(claimPath(taskId), None)
    if not c:
        return False
    mine = str(c.get('run_id')) == str(RUN_ID) and str(c.get('slot')) == str(SLOT)
    return (not mine) and time.time() - c.get('claimed_epoch', 0) <= STALE_CLAIM
def recoverStale():
    def fn():
        moved = 0
        for cp in glob.glob(f'{RUNNING}/*.claim.json'):
            tid = os.path.basename(cp)[:-11]
            c = readJson(cp, {})
            if time.time() - c.get('claimed_epoch', 0) > STALE_CLAIM and os.path.exists(taskPath(RUNNING, tid)):
                os.replace(taskPath(RUNNING, tid), taskPath(PENDING, tid))
                os.remove(cp)
                moved += 1
        if not moved:
            raise StopIteration
        log('requeued stale tasks:', moved)
    mutate(fn, f'qfx: requeue stale claims (run {RUN_ID})')
def claimNext(preferred='', losses=0):
    chosen = {'id': ''}
    def fn():
        chosen['id'] = ''
        tid = ''
        if preferred and os.path.exists(taskPath(PENDING, preferred)):
            tid = preferred
        elif preferred and os.path.exists(taskPath(RUNNING, preferred)) and not claimHeldByOther(preferred):
            tid = preferred
        else:
            ids = pendingIds()
            if not ids:
                raise StopIteration
            k = int(SLOT) if str(SLOT).isdigit() else 1
            tid = ids[(k - 1 + losses) % len(ids)]
        if os.path.exists(taskPath(PENDING, tid)):
            os.replace(taskPath(PENDING, tid), taskPath(RUNNING, tid))
        writeJson(claimPath(tid), {'run_id': RUN_ID, 'slot': SLOT, 'claimed_at': nowIso(), 'claimed_epoch': int(time.time())})
        chosen['id'] = tid
    try:
        mutate(fn, lambda: f'qfx: claim {chosen["id"]} (run {RUN_ID} slot {SLOT})')
    except RuntimeError as e:
        log('claim failed:', str(e)[:200])
        return ''
    return chosen['id']
def ownsClaim(taskId):
    c = readJson(claimPath(taskId), None)
    return bool(c) and str(c.get('run_id')) == str(RUN_ID) and str(c.get('slot')) == str(SLOT) and os.path.exists(taskPath(RUNNING, taskId))
def requeue(taskId, reason):
    def fn():
        if not os.path.exists(taskPath(RUNNING, taskId)):
            raise StopIteration
        os.replace(taskPath(RUNNING, taskId), taskPath(PENDING, taskId))
        if os.path.exists(claimPath(taskId)):
            os.remove(claimPath(taskId))
    mutate(fn, f'qfx: requeue {taskId} ({reason}, run {RUN_ID})')
def runTask(key, taskId, sliceBudget):
    work = tempfile.mkdtemp(prefix='qfx-')
    blob = base64.b64decode(open(taskPath(RUNNING, taskId)).read().strip())
    unpackTo(qc.decrypt(key, blob, taskId.encode()), work)
    spec = readJson(os.path.join(work, 'task.json'), {}) or {}
    meta = readJson(f'{STATE}/{taskId}/meta.json', {}) or {}
    attempt = int(meta.get('next_attempt', 1))
    maxAttempts = int(spec.get('max_attempts', 3))
    budget = int(max(60, min(int(spec.get('budget_seconds', 19800)), sliceBudget)))
    outDir, stateDir = os.path.join(work, '_out'), os.path.join(work, '_state')
    os.makedirs(outDir, exist_ok=True)
    os.makedirs(stateDir, exist_ok=True)
    if glob.glob(f'{STATE}/{taskId}/state.part*.b64'):
        unpackTo(qc.decrypt(key, readParts(f'{STATE}/{taskId}', 'state'), taskId.encode()), stateDir)
    for name in (spec.get('assets') or []):
        name = os.path.basename(str(name))
        if not glob.glob(f'assets/{name}/asset.part*.b64'):
            raise RuntimeError(f'asset {name} missing')
        unpackTo(qc.decrypt(key, readParts(f'assets/{name}', 'asset'), f'asset:{name}'.encode()), os.path.join(work, 'assets', name))
        log('asset', name, 'unpacked')
    env = dict(os.environ)
    for k in ('QFX_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'):
        env.pop(k, None)
    for k, v in (spec.get('env') or {}).items():
        k = str(k)
        if k.isidentifier() and not k.startswith(('GH_', 'GITHUB_', 'ACTIONS_')) and k not in ('QFX_KEY', 'QFX_KEY_FILE', 'PATH', 'LD_PRELOAD'):
            env[k] = str(v)
    env.update(QFX_TASK_ID=taskId, QFX_WORK=work, QFX_OUT=outDir, QFX_STATE=stateDir, QFX_ATTEMPT=str(attempt),
               QFX_BUDGET_SECONDS=str(budget), QFX_DEADLINE=str(int(time.time() + budget)), QFX_RUN_ID=RUN_ID)
    log('run', taskId, 'attempt', attempt, 'budget', budget)
    t0 = time.time()
    with open(os.path.join(outDir, '_stdout.log'), 'wb') as so, open(os.path.join(outDir, '_stderr.log'), 'wb') as se:
        if not os.path.exists(os.path.join(work, 'run.sh')):
            se.write(b'run.sh missing\n')
            rc = 127
        else:
            rc = subprocess.call(['timeout', '--kill-after=30', str(budget), 'bash', 'run.sh'], cwd=work, env=env, stdout=so, stderr=se)
    dur = round(time.time() - t0, 1)
    cont = rc == 75 or (rc in (124, 137) and attempt < maxAttempts)
    outcome = 'continue' if cont else ('done' if rc == 0 else 'failed')
    log('finished', taskId, 'rc', rc, 'outcome', outcome, 'seconds', dur)
    resBlob = qc.encrypt(key, packDir(outDir), taskId.encode())
    stBlob = qc.encrypt(key, packDir(stateDir), taskId.encode()) if cont else None
    rec = {'task_id': taskId, 'attempt': attempt, 'rc': rc, 'outcome': outcome, 'seconds': dur, 'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0)),
           'finished_at': nowIso(), 'run_id': RUN_ID, 'slot': SLOT}
    def fn():
        rdir = f'{RESULTS}/{taskId}'
        parts, n, h = writeParts(rdir, resBlob, f'attempt{attempt:02d}')
        rec.update(parts=parts, blob_bytes=n, blob_sha16=h)
        st = readJson(f'{rdir}/status.json', {}) or {}
        hist = [x for x in st.get('attempts', []) if x.get('attempt') != attempt] + [rec]
        writeJson(f'{rdir}/status.json', {'task_id': taskId, 'outcome': outcome, 'last_attempt': attempt, 'updated_at': nowIso(), 'attempts': hist})
        shutil.rmtree(f'{STATE}/{taskId}', ignore_errors=True)
        if cont:
            writeParts(f'{STATE}/{taskId}', stBlob, 'state')
            writeJson(f'{STATE}/{taskId}/meta.json', {'task_id': taskId, 'next_attempt': attempt + 1, 'saved_at': nowIso()})
        else:
            if os.path.exists(taskPath(RUNNING, taskId)):
                os.replace(taskPath(RUNNING, taskId), taskPath(DONE, taskId))
            if os.path.exists(claimPath(taskId)):
                os.remove(claimPath(taskId))
    mutate(fn, f'qfx: {outcome} {taskId} attempt {attempt} rc {rc} ({dur}s, run {RUN_ID})')
    shutil.rmtree(work, ignore_errors=True)
    return cont and ('fresh' if spec.get('fresh_job') else 'same')
def main():
    key = qc.loadKey()
    sh('git config user.name qfx-worker')
    sh('git config user.email qfx-worker@users.noreply.github.com')
    time.sleep(random.random() * min(30.0, 1.5 * WORKERS))
    preferred = os.environ.get('QFX_TASK_ID', '').strip()
    handled, losses = 0, 0
    while True:
        gitSync()
        recoverStale()
        remaining = JOB_BUDGET - (time.time() - JOB_START)
        if remaining < MIN_SLICE + RESERVE:
            log('job budget low; handing over to a fresh job')
            if pendingIds():
                dispatch('')
            return
        taskId = claimNext(preferred, losses)
        preferred = ''
        if not taskId:
            log('nothing claimable; handled', handled)
            return
        if not ownsClaim(taskId):
            log('claim verification failed for', taskId)
            losses += 1
            continue
        try:
            cont = runTask(key, taskId, remaining - RESERVE)
        except Exception as e:
            log('task error', taskId, str(e)[:300])
            def fn():
                if not ownsClaim(taskId):
                    raise StopIteration
                os.replace(taskPath(RUNNING, taskId), taskPath(DONE, taskId))
                os.remove(claimPath(taskId))
                st = readJson(f'{RESULTS}/{taskId}/status.json', {}) or {}
                st.update({'task_id': taskId, 'outcome': 'failed', 'error': str(e)[:300], 'updated_at': nowIso(), 'run_id': RUN_ID})
                writeJson(f'{RESULTS}/{taskId}/status.json', st)
            mutate(fn, f'qfx: failed {taskId} (worker error, run {RUN_ID})')
            cont = False
        handled += 1
        losses += 1
        if cont == 'same' and JOB_BUDGET - (time.time() - JOB_START) > MIN_SLICE + RESERVE:
            preferred = taskId
        elif cont:
            requeue(taskId, 'continue in fresh job' if cont == 'fresh' else 'job budget low')
            dispatch('')
if __name__ == '__main__':
    main()
