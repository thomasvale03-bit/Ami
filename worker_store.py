"""Durable queue only. No browser, vendor, email or webhook execution."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS queue_jobs (
                  ref TEXT PRIMARY KEY, raw TEXT NOT NULL, state TEXT NOT NULL,
                  created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0, claim_token TEXT,
                  lease_until REAL, metadata TEXT NOT NULL DEFAULT '{}',
                  reason TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS queue_batches (slot TEXT PRIMARY KEY, refs TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intake_receipts (
                  key TEXT PRIMARY KEY, source TEXT NOT NULL, payload TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                  created REAL NOT NULL, due REAL NOT NULL, lease_until REAL);
                CREATE TABLE IF NOT EXISTS legacy_reviews (
                  ref TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, decision TEXT NOT NULL,
                  reason TEXT NOT NULL, reviewed_at REAL NOT NULL);

            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row, *, token=False):
        if row is None:
            return None
        out = dict(row)
        for name in ('raw', 'metadata'):
            out[name] = json.loads(out[name])
        if not token:
            out.pop('claim_token', None)
        return out

    def enqueue(self, ref, raw, now=None, reason=''):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO queue_jobs(ref,raw,state,created,updated,due,reason)
                          VALUES(?,?,?,?,?,?,?)''',
                       (ref, json.dumps(raw), 'manual' if reason else 'queued', now, now, now, reason))
        return self.get(ref)

    def get(self, ref):
        with self.connect() as db:
            return self.decode(db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone())

    def listing(self, state=None, limit=50):
        with self.connect() as db:
            if state:
                rows = db.execute('SELECT * FROM queue_jobs WHERE state=? ORDER BY created,ref LIMIT ?', (state, limit))
            else:
                rows = db.execute('SELECT * FROM queue_jobs ORDER BY created DESC,ref LIMIT ?', (limit,))
            return [self.decode(r) for r in rows]

    def status(self):
        with self.connect() as db:
            return {'counts': dict(db.execute('SELECT state,count(*) FROM queue_jobs GROUP BY state').fetchall()),
                    'execution': 'amyi-own-desktop-only', 'timezone': 'America/Phoenix'}

    @staticmethod
    def expire(db, now):
        # Lost sessions and expired human pauses NEVER silently requeue/rebook.
        db.execute("""UPDATE queue_jobs SET state='manual',reason='Desktop lease/human checkpoint expired; outcome requires review',
                      updated=? WHERE state IN ('assigned','awaiting_human_verification') AND lease_until<=?""", (now, now))

    def claim(self, slot, now, *, allow_new, limit=10):
        """Claim up to `limit` runnable jobs.

        Candidates are ordered by soonest source event date (then arrival), so
        tonight's guests are booked before next month's. Jobs that fail the
        source policy (past date, held, JEWEL after Nov 21 ...) are moved to
        manual on the way and do NOT use up the claim: the next runnable job is
        handed out instead. `slot=None` (worker mode) records no hourly batch;
        the single-lane active-job check still prevents double assignment.
        """
        with self.connect() as db:
            self.expire(db, now)
            # One execution lane; a second caller gets the same in-flight jobs.
            active = db.execute("SELECT * FROM queue_jobs WHERE state IN ('assigned','awaiting_human_verification') ORDER BY created,ref").fetchall()
            if active:
                return [self.decode(r, token=True) for r in active]
            if slot is not None:
                old = db.execute('SELECT refs FROM queue_batches WHERE slot=?', (slot,)).fetchone()
                if old:
                    refs = json.loads(old['refs'])
                    return [self.decode(db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone(), token=True) for ref in refs]
            if not allow_new:
                raise Conflict('New jobs are claimed only in the hourly pickup window (:00 through :09 Phoenix)')
            rows = db.execute("""SELECT * FROM queue_jobs WHERE state IN ('queued','retry') AND due<=?
                ORDER BY COALESCE(json_extract(raw,'$.start_date'),'9999-12-31'),created,ref""", (now,)).fetchall()
            from queue_service import source_policy, PHOENIX
            from datetime import datetime
            today = datetime.fromtimestamp(now, PHOENIX).date()
            refs = []
            for row in rows:
                if len(refs) >= limit:
                    break
                ref = row['ref']
                reason = source_policy(ref, json.loads(row['raw']), today)
                if reason:
                    db.execute("UPDATE queue_jobs SET state='manual',reason=?,updated=? WHERE ref=?", (reason, now, ref))
                    continue
                refs.append(ref)
                db.execute("""UPDATE queue_jobs SET state='assigned',claim_token=?,lease_until=?,
                              attempts=attempts+1,updated=? WHERE ref=?""", (secrets.token_urlsafe(32), now + 3600, now, ref))
            if slot is not None:
                db.execute('INSERT INTO queue_batches VALUES(?,?)', (slot, json.dumps(refs)))
            return [self.decode(db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone(), token=True) for ref in refs]

    def exceptions(self, limit=100):
        """Jobs a person must look at: manual, uncertain submits, and paused or
        retry-pending work. Newest first. No claim tokens."""
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM queue_jobs WHERE state IN
                ('manual','submission_uncertain','awaiting_human_verification','retry')
                ORDER BY updated DESC,ref LIMIT ?""", (limit,)).fetchall()
            return [self.decode(r) for r in rows]

    def owner_requeue(self, ref, approval, now):
        """Owner-approved: put ONE manual job back to queued. Only for a job
        that was never submitted (no booking/submitting progress), so it can
        never cause a rebook of completed work. The approval is recorded."""
        approval = str(approval or '').strip()
        if not approval:
            raise Conflict('owner approval (message id) required')
        with self.connect() as db:
            row = db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone()
            if row is None:
                raise Conflict('Unknown job')
            # Owner may also pull a pre-submit retry job forward (Thomas, Nov 15
            # 2026: list opened tonight, don't wait for the hourly retry).
            if row['state'] not in ('manual', 'retry'):
                raise Conflict('Only a manual or retry job can be requeued')
            meta = json.loads(row['metadata'] or '{}')
            progress = meta.get('progress') or {}
            if progress.get('submitting') or progress.get('bookings') or meta.get('evidence'):
                raise Conflict('Job has submission progress; manual review required')
            meta['owner_requeue'] = {'approval': approval[:200], 'at': now, 'previous_reason': row['reason']}
            db.execute("UPDATE queue_jobs SET state='queued',reason=?,metadata=?,updated=?,due=?,claim_token=NULL WHERE ref=?",
                       ('Owner requeue ' + approval[:200], json.dumps(meta), now, now, ref))
        return self.get(ref)

    def owner_complete(self, ref, approval, now):
        """Owner-approved: mark ONE manual job done from the verified receipts
        the worker already saved when completion was refused. No booking and
        no email happens here. The saved evidence must pass the same
        validate_completion check a live completion would (exact requested
        venue/date receipts, party counts, order ids, guest+team email ids), so
        a venue fallback (e.g. Hakkasan->JEWEL) is still refused."""
        approval = str(approval or '').strip()
        if not approval:
            raise Conflict('owner approval (message id) required')
        with self.connect() as db:
            row = db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone()
            if row is None:
                raise Conflict('Unknown job')
            if row['state'] != 'manual':
                raise Conflict('Only a manual job can be owner-completed')
            meta = json.loads(row['metadata'] or '{}')
            evidence = (meta.get('resolution') or {}).get('evidence')
            if not isinstance(evidence, dict):
                raise Conflict('No saved worker receipts on this job')
            from queue_service import validate_completion
            checked = validate_completion(json.loads(row['raw']), {'evidence': evidence})
            meta['evidence'] = evidence
            meta['venue_changes'] = checked['venue_changes']
            meta['completed_at'] = now
            meta['owner_complete'] = {'approval': approval[:200], 'at': now, 'previous_reason': row['reason']}
            db.execute("UPDATE queue_jobs SET state='done',reason=?,metadata=?,updated=?,claim_token=NULL WHERE ref=?",
                       ('Completed from saved receipts (owner ' + approval[:200] + ')', json.dumps(meta), now, ref))
        return self.get(ref)

    def all_jobs(self, states=None):
        """Every job (optionally only these states), oldest first. No claim tokens."""
        with self.connect() as db:
            if states:
                marks = ','.join('?' * len(states))
                rows = db.execute(f'SELECT * FROM queue_jobs WHERE state IN ({marks}) ORDER BY created,ref',
                                  tuple(states)).fetchall()
            else:
                rows = db.execute('SELECT * FROM queue_jobs ORDER BY created,ref').fetchall()
            return [self.decode(r) for r in rows]

    def set_meta(self, ref, key, value):
        """Record bookkeeping (e.g. the flag alert result) without a state change."""
        with self.connect() as db:
            row = db.execute('SELECT metadata FROM queue_jobs WHERE ref=?', (ref,)).fetchone()
            if row is None:
                raise Conflict('Unknown job')
            meta = json.loads(row['metadata'] or '{}')
            meta[key] = value
            db.execute('UPDATE queue_jobs SET metadata=? WHERE ref=?', (json.dumps(meta), ref))

    @staticmethod
    def saved_bookings(meta):
        """Verified booking records the worker saved (final evidence, the
        refused-completion evidence, or per-night progress checkpoints)."""
        out, seen = [], set()
        sources = [(meta.get('evidence') or {}).get('bookings'),
                   ((meta.get('resolution') or {}).get('evidence') or {}).get('bookings'),
                   list(((meta.get('progress') or {}).get('bookings') or {}).values())]
        for records in sources:
            for rec in records or []:
                if not isinstance(rec, dict) or rec.get('verified') is not True:
                    continue
                key = rec.get('order_id') or (rec.get('venue'), rec.get('date'), rec.get('system'))
                if key in seen:
                    continue
                seen.add(key)
                out.append(rec)
        return out

    def owner_reconcile(self, ref, approval, now, lookup_prior=None):
        """Owner-approved: mark ONE manual/submission_uncertain job done from the
        verified bookings the worker already recorded (refused-completion
        evidence or per-night progress). Never books and never sends email.

        The bookings must pass validate_completion (exact requested dates,
        party counts, order ids; approved same-night venue fallbacks only), so
        a partial job (a night not booked yet) stays manual. A guest receipt is
        required: the saved send id, or the earlier identical email found by
        lookup_prior(raw, 'guest') when /send-branded refused it as a
        duplicate. A missing team receipt is recorded, not invented."""
        approval = str(approval or '').strip()
        if not approval:
            raise Conflict('owner approval (message id) required')
        with self.connect() as db:
            row = db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone()
            if row is None:
                raise Conflict('Unknown job')
            if row['state'] not in ('manual', 'submission_uncertain'):
                raise Conflict('Only a manual or submission_uncertain job can be reconciled')
            meta = json.loads(row['metadata'] or '{}')
            raw = json.loads(row['raw'])
            bookings = self.saved_bookings(meta)
            if not bookings:
                raise Conflict('No verified booking saved on this job')
            receipts = {}
            for source in (((meta.get('resolution') or {}).get('evidence') or {}).get('email_receipts') or []):
                if isinstance(source, dict) and source.get('id') and source.get('kind') in ('guest', 'team'):
                    receipts.setdefault(source['kind'], {'kind': source['kind'], 'id': source['id']})
            for kind, mid in ((meta.get('progress') or {}).get('emails') or {}).items():
                if mid and kind in ('guest', 'team'):
                    receipts.setdefault(kind, {'kind': kind, 'id': mid})
            if 'guest' not in receipts and lookup_prior:
                prior = lookup_prior(raw, 'guest')
                if prior and prior.get('id'):
                    receipts['guest'] = {'kind': 'guest', 'id': prior['id'], 'prior_send': prior}
            if 'guest' not in receipts:
                raise Conflict('No guest email receipt saved or found in the send log; stays manual')
            evidence = {'verified': True, 'worker': 'owner-reconcile', 'bookings': bookings,
                        'email_receipts': [receipts[k] for k in ('guest', 'team') if k in receipts]}
            from queue_service import validate_completion
            checked = validate_completion(raw, {'evidence': evidence}, require_team_receipt=False)
            meta['evidence'] = evidence
            meta['venue_changes'] = checked['venue_changes']
            meta['completed_at'] = now
            meta['owner_reconcile'] = {'approval': approval[:200], 'at': now, 'previous_state': row['state'],
                                       'previous_reason': row['reason'],
                                       'team_receipt_missing': 'team' not in receipts}
            db.execute("UPDATE queue_jobs SET state='done',reason=?,metadata=?,updated=?,claim_token=NULL WHERE ref=?",
                       ('Reconciled from saved verified bookings (owner ' + approval[:200] + '); no booking/email',
                        json.dumps(meta), now, ref))
        return self.get(ref)

    def update(self, ref, token, action, metadata, now, reason=''):
        if not isinstance(metadata, dict):
            raise Conflict('metadata must be an object')
        with self.connect() as db:
            self.expire(db, now)
            row = db.execute('SELECT * FROM queue_jobs WHERE ref=?', (ref,)).fetchone()
            if row is None:
                raise Conflict('Unknown job')
            if not isinstance(token, str) or not token or not secrets.compare_digest(row['claim_token'] or '', token):
                raise Conflict('Invalid job claim')
            saved = json.loads(row['metadata'])
            # Exact completion retry is idempotent; no new claim or side effects.
            if row['state'] == 'done' and action == 'complete' and saved.get('evidence') == metadata.get('evidence'):
                return self.decode(row)
            if row['state'] not in ('assigned', 'awaiting_human_verification'):
                raise Conflict('Job is not active; manual review required')
            state, lease, due = row['state'], row['lease_until'], row['due']
            if action == 'pause_human':
                if state != 'assigned' or not metadata.get('desktop_session_id') or not reason:
                    raise Conflict('Human pause requires a local session ID and reason')
                state, lease = 'awaiting_human_verification', now + 900
                saved['human'] = {'desktop_session_id': metadata['desktop_session_id'], 'paused_at': now,
                                  'reason': reason, 'expires_at': lease}
            elif action == 'resume_human':
                human = saved.get('human', {})
                if (state != 'awaiting_human_verification'
                        or metadata.get('desktop_session_id') != human.get('desktop_session_id')
                        or metadata.get('human_cleared') is not True
                        or metadata.get('clearance_checked') is not True):
                    raise Conflict('AMYI must observe clearance in the SAME local tab/session')
                state, lease = 'assigned', now + 3600
                saved['human']['cleared_at'] = now
            elif action == 'retry':
                if state != 'assigned' or metadata.get('submission_attempted') is not False or not reason:
                    raise Conflict('Retry allowed only for explicit pre-submit failure')
                state = 'retry' if row['attempts'] < 3 else 'manual'
                due = (int(now // 3600) + 1) * 3600
                saved['last_retry'] = {'reason': reason, 'at': now}
            elif action == 'progress':
                # Worker checkpoint: record per-night bookings/sends as they
                # happen so a crash or email failure never causes a rebook.
                if state != 'assigned' or not isinstance(metadata.get('progress'), dict):
                    raise Conflict('Progress requires an assigned job and a progress object')
                merged = dict(saved.get('progress') or {})
                for key, value in metadata['progress'].items():
                    if isinstance(value, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **value}
                    else:
                        merged[key] = value
                saved['progress'] = merged
            elif action in ('uncertain', 'manual'):
                if not reason:
                    raise Conflict('Actionable reason required')
                state = 'submission_uncertain' if action == 'uncertain' else 'manual'
                saved['resolution'] = metadata
            elif action == 'complete':
                if state != 'assigned':
                    raise Conflict('Paused verification cannot be marked complete')
                from queue_service import validate_completion
                checked = validate_completion(json.loads(row['raw']), metadata)
                state = 'done'
                saved['evidence'] = metadata['evidence']
                saved['venue_changes'] = checked['venue_changes']
                saved['completed_at'] = now
            else:
                raise Conflict('Unknown action')
            db.execute('''UPDATE queue_jobs SET state=?,metadata=?,reason=?,updated=?,lease_until=?,due=? WHERE ref=?''',
                       (state, json.dumps(saved), reason, now, lease, due, ref))
        return self.get(ref)

    def intake(self, key, source, payload, now):
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO intake_receipts(key,source,payload,created,due,lease_until)
                          VALUES(?,?,?,?,?,?)''', (key, source, json.dumps(payload), now, now, now + 300))
        return key

    def pending_intake(self, now, limit=5):
        # Durable lease avoids overlapping recovery passes. A process dying after
        # Gmail insert is recovered through the existing intake dedupe, not booking.
        with self.connect() as db:
            db.execute("UPDATE intake_receipts SET state='review' WHERE attempts>=3 AND state='pending'")
            rows = db.execute("""SELECT * FROM intake_receipts WHERE state='pending' AND attempts<3
                AND due<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY created LIMIT ?""", (now, now, limit)).fetchall()
            for row in rows:
                db.execute('UPDATE intake_receipts SET attempts=attempts+1,lease_until=? WHERE key=?', (now + 300, row['key']))
        return [{**dict(r), 'payload': json.loads(r['payload'])} for r in rows]

    def finish_intake(self, key, ok, now):
        with self.connect() as db:
            db.execute("UPDATE intake_receipts SET state=?,due=?,lease_until=NULL WHERE key=?",
                       ('bridged' if ok else 'pending', now + 300, key))

    def intake_listing(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT key,source,state,attempts,created,due FROM intake_receipts ORDER BY created DESC LIMIT 100')]

    def legacy_review(self, ref, fingerprint, decision, reason, now):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO legacy_reviews VALUES(?,?,?,?,?)', (ref, fingerprint, decision, reason, now))

    def legacy_reviews(self):
        with self.connect() as db:
            return {r['ref']: dict(r) for r in db.execute('SELECT * FROM legacy_reviews')}

    def intake_get(self, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM intake_receipts WHERE key=?', (key,)).fetchone()
            if not row:
                return None
            return {**dict(row), 'payload': json.loads(row['payload'])}
