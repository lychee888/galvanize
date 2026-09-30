"""Durable per-relay inbox and acknowledgements (SQLite, no external service).

Receipt and delivery are separate: fetching durably stages events; only successful
delivery acknowledges them. A crash after an external side effect but before its
ack commit may repeat that event: consumers must use its stable event_id.
"""
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager

from .paths import ensure_home


class RelayState:
    def __init__(self, url):
        identity = hashlib.sha256(url.rstrip('/').encode()).hexdigest()
        self.path = ensure_home() / f'relay-{identity}.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, event TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '');
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def cursor(self, name='ack'):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
            return row[0] if row else '0'

    def stage(self, events, fetched_cursor):
        with self.db() as db:
            for event in events:
                event_id = str(event.get('id') or '') if isinstance(event, dict) else ''
                if not event_id:
                    # A malformed record must have a stable quarantine identity.
                    event_id = 'invalid-' + hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO events(id,event,state,error) VALUES (?,?,'quarantined',?)",
                               (event_id, json.dumps(event), 'Missing stable event id'))
                else:
                    db.execute('INSERT OR IGNORE INTO events(id,event) VALUES (?,?)', (event_id, json.dumps(event)))
            if fetched_cursor:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('fetch', str(fetched_cursor)))

    def pending(self, now=None):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM events WHERE state='pending' AND next_attempt<=? ORDER BY id LIMIT 100",
                (time.time() if now is None else now,))]

    def finish(self, event_id, state, error='', max_attempts=5, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            row = db.execute('SELECT attempts FROM events WHERE id=?', (event_id,)).fetchone()
            attempts = row[0] + 1
            if state == 'pending' and attempts >= max_attempts:
                state = 'failed'
            delay = min(60, 2 ** min(attempts, 6)) if state == 'pending' else 0
            db.execute('UPDATE events SET state=?, attempts=?, next_attempt=?, error=? WHERE id=?',
                       (state, attempts, now + delay, error[:1000], event_id))
            # Delivery progress stops at the first unacknowledged event. Fetch
            # progress is independent so one failed item cannot starve others.
            cursor = '0'
            for item in db.execute('SELECT id,state FROM events ORDER BY id'):
                if item['state'] not in ('ack', 'quarantined'):
                    break
                cursor = item['id']
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('ack', cursor))

    def failures(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,state,attempts,error FROM events WHERE state IN ('failed','quarantined') OR error!='' ORDER BY id")]

    def retry(self, event_id):
        with self.db() as db:
            return db.execute("UPDATE events SET state='pending',attempts=0,next_attempt=0,error='' WHERE id=? AND state='failed'",
                              (event_id,)).rowcount == 1
