"""Transactional journal. SQLite is LOCAL ONLY; PostgreSQL for serverless deployments."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = '''
CREATE TABLE IF NOT EXISTS mf_kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mf_events (id TEXT PRIMARY KEY, session TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mf_events_session ON mf_events(session);
CREATE TABLE IF NOT EXISTS mf_outbox (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL);
'''


class Store:
    def __init__(self, url):
        self.url = url
        self.pg = url.startswith(('postgres://','postgresql://'))
        if os.getenv('VERCEL') and not self.pg:
            raise RuntimeError('Vercel requires durable PostgreSQL SCANNER_DATABASE_URL')

    @contextmanager
    def transaction(self):
        if self.pg:
            import psycopg
            conn = psycopg.connect(self.url, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.url, timeout=15, isolation_level=None)
        try:
            if not self.pg:
                conn.execute('BEGIN IMMEDIATE')
            else:
                # Serialize scanner state/ranking across concurrent serverless invocations.
                conn.execute('SELECT pg_advisory_xact_lock(728541902)')
            for statement in SCHEMA.split(';'):
                if statement.strip(): conn.execute(statement)
            yield Transaction(conn, self.pg)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


class Transaction:
    def __init__(self, conn, pg):
        self.conn,self.pg=conn,pg

    def execute(self, sql, args=()):
        return self.conn.execute(sql.replace('?', '%s') if self.pg else sql,args)

    def get(self,key,default=None):
        row=self.execute('SELECT value FROM mf_kv WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self,key,value):
        self.execute('INSERT INTO mf_kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (key,json.dumps(value,allow_nan=False)))

    def event(self,id,session,payload):
        self.execute('INSERT INTO mf_events(id,session,payload) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                     (id,session,json.dumps(payload,allow_nan=False)))

    def events(self,session):
        return [json.loads(x[0]) for x in self.execute('SELECT payload FROM mf_events WHERE session=? ORDER BY id',(session,)).fetchall()]

    def enqueue(self,id,payload):
        self.execute('INSERT INTO mf_outbox(id,status,payload) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                     (id,'pending',json.dumps(payload)))


def drain(store,send,limit=5,prefix=None,now=None):
    """At-most-once attempt. Ambiguous Telegram timeouts require manual reconciliation.

    Never resend an attempted message automatically: Telegram has no idempotency key.
    """
    results=[]
    for _ in range(limit):
        with store.transaction() as tx:
            row=tx.execute("SELECT id,payload FROM mf_outbox WHERE status='pending' AND (? IS NULL OR id LIKE ?) ORDER BY id LIMIT 1",(prefix, (prefix+'%') if prefix else None)).fetchone()
            if not row: break
            id,payload=row
            decoded=json.loads(payload)
            expiry=decoded.get('expires_at')
            if expiry and datetime.fromisoformat(expiry) <= (now or datetime.now(timezone.utc)):
                tx.execute("UPDATE mf_outbox SET status='expired' WHERE id=?",(id,))
                results.append({'id':id,'status':'expired'})
                continue
            tx.execute("UPDATE mf_outbox SET status='attempting' WHERE id=?",(id,))
        try:
            send(json.loads(payload)['text'])
            status='sent'
        except Exception:
            status='uncertain_or_failed'
        with store.transaction() as tx:
            tx.execute('UPDATE mf_outbox SET status=? WHERE id=?',(status,id))
        results.append({'id':id,'status':status})
    return results
