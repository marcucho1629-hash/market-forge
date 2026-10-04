"""Durable, isolated shadow storage. Local research only; no production imports."""
import json,sqlite3
from dataclasses import asdict
from datetime import datetime
from .collector import Collector
from .lifecycle import Candidate

DATES={'prep_at','bar_end','last_observed','last_closed_observed','send_at','confirmed_at','chart_x_at'}
def dump(value):
 return json.dumps(value,default=lambda x:x.isoformat() if isinstance(x,datetime) else str(x),sort_keys=True,allow_nan=False)
def restore_state(raw):
 d=json.loads(raw)
 for key in DATES:
  if d.get(key) is not None:d[key]=datetime.fromisoformat(d[key])
 return Candidate(**d)

class ShadowStore:
 def __init__(self,path,experiment_id,config_hash):
  self.db=sqlite3.connect(path,timeout=15)
  self.experiment=experiment_id;self.config=config_hash
  self.db.executescript('''
  CREATE TABLE IF NOT EXISTS states(experiment TEXT,config TEXT,engine TEXT,candidate TEXT,state TEXT,metadata TEXT,PRIMARY KEY(experiment,config,engine,candidate));
  CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,experiment TEXT,config TEXT,event TEXT);
  CREATE TABLE IF NOT EXISTS receipts(experiment TEXT,config TEXT,receipt TEXT,payload TEXT,PRIMARY KEY(experiment,config,receipt));
  ''')
 def close(self):self.db.close()
 def _load(self):
  c=Collector(self.experiment,self.config)
  for engine,cid,state,meta in self.db.execute('SELECT engine,candidate,state,metadata FROM states WHERE experiment=? AND config=?',(self.experiment,self.config)):
   c.states[(engine,cid)]=restore_state(state)
   m=json.loads(meta);m['source_bar_start']=datetime.fromisoformat(m['source_bar_start']);c.metadata[(engine,cid)]=m
  return c
 def _save(self,c):
  for (engine,cid),s in c.states.items():
   self.db.execute('INSERT OR REPLACE INTO states VALUES(?,?,?,?,?,?)',(self.experiment,self.config,engine,cid,dump(asdict(s)),dump(c.metadata[(engine,cid)])))
  for event in c.events:self.db.execute('INSERT INTO events(experiment,config,event) VALUES(?,?,?)',(self.experiment,self.config,dump(event)))
 def ingest_batch(self,receipt_id,rows):
  if not receipt_id:raise ValueError('receipt id required')
  payload=dump(rows)
  self.db.execute('BEGIN IMMEDIATE')
  try:
   old=self.db.execute('SELECT payload FROM receipts WHERE experiment=? AND config=? AND receipt=?',(self.experiment,self.config,receipt_id)).fetchone()
   if old:
    if old[0]!=payload:raise ValueError('receipt reused with different payload')
    self.db.rollback();return False
   c=self._load()
   for row in rows:c.ingest(row)
   self._save(c)
   self.db.execute('INSERT INTO receipts VALUES(?,?,?,?)',(self.experiment,self.config,receipt_id,payload))
   self.db.commit();return True
  except BaseException:
   self.db.rollback();raise
 def sweep(self,now):
  self.db.execute('BEGIN IMMEDIATE')
  try:
   c=self._load();c.sweep(now);self._save(c);self.db.commit()
  except BaseException:self.db.rollback();raise
 def rows(self):return self._load().rows()
 def events(self):return [json.loads(r[0]) for r in self.db.execute('SELECT event FROM events WHERE experiment=? AND config=? ORDER BY id',(self.experiment,self.config))]
