import unittest,tempfile,os
from datetime import timedelta
from unittest.mock import patch
from test_sequence import START,bar,Scripted
from scanner.v136_live.engine import Engine,Window,Position,Bar
from scanner.v136_live.runtime import ingest,status,VERSION,PREFIX,CONTROL_KEY,diagnostic_export
from scanner.store import Store

class Recovery(unittest.TestCase):
 def test_partial_aggregate_discarded(self):
  w=Window(3);self.assertFalse(w.push(bar(1),1));self.assertFalse(w.push(bar(3),3));self.assertIsNone(w.latest)
 def test_partial_seed_uses_only_complete_windows(self):
  e=Engine();e.seed([bar(m) for m in range(1,91) if m!=2],allow_gaps=True)
  self.assertEqual(len(e.states['SPY'].windows[3].history),29)
  self.assertIsNone(e.states['SPY'].position)
 def test_explicit_recovery_retains_position(self):
  e=Engine();e.on_bar(bar(1));p=Position(1,100,bar(1).end,1,101);e.states['SPY'].position=p
  e.recover_gap(bar(5));self.assertIs(e.states['SPY'].position,p)
  self.assertEqual(e.states['SPY'].entry_signals,{})
 def test_recovery_does_not_mix_old_signal(self):
  e=Scripted({(1,1):1,(2,4):1,(3,6):1});e.on_bar(bar(1));e.recover_gap(bar(4))
  self.assertEqual([v for m in range(4,7) for v in e.on_bar(bar(m))],[])
 def test_ctn_not_on_entry_candle(self):
  e=Scripted({(1,28):1,(2,-30):1,(3,-30):1})
  events=[v for m in range(1,41) for v in e.on_bar(bar(m))]
  self.assertEqual([(v.action,v.time.minute) for v in events],[('MC',0),('CTN',10)])

class Runtime(unittest.TestCase):
 def setUp(self):
  fd,self.path=tempfile.mkstemp();os.close(fd);self.store=Store(self.path)
  with self.store.transaction() as tx:tx.put(CONTROL_KEY,{'enabled':True,'expires_at':(START+timedelta(days=1)).isoformat()})
 def tearDown(self):os.unlink(self.path)
 def row(self,m):
  b=bar(m);return ['SPY',int(b.end.timestamp()*1000),100,101,99,100,1000]
 def post(self,minutes,now):
  return ingest(self.store,{'source':VERSION,'bank':1,'emitted':int(now.timestamp()*1000),'bars':[self.row(m) for m in minutes]},now,lambda d:{'is_open':True,'open':START.isoformat(),'close':(START+timedelta(minutes=390)).isoformat()})
 def test_gap_does_not_permanently_stop_ticker(self):
  now=START+timedelta(minutes=6,seconds=10);self.post([1,2,4,5,6],now)
  s=status(self.store,now)['symbols']['SPY'];self.assertEqual(s['processed'],self.row(6)[1]);self.assertEqual(len(s['gaps']),1);self.assertEqual(s['data_quality'],'partial_minutes')
  self.post([1,2,4,5,6],now)
  with self.store.transaction() as tx:self.assertEqual(tx.execute('SELECT COUNT(*) FROM mf_outbox').fetchone()[0],1)
 def test_late_backfill_cannot_rewrite_emitted_signal_history(self):
  now=START+timedelta(minutes=6,seconds=10);self.post([1,2,4,5,6],now);self.post([3],now)
  s=status(self.store,now)['symbols']['SPY'];self.assertEqual(s['data_quality'],'partial_minutes');self.assertEqual(s['ignored_backfills'],[self.row(3)[1]])
 def test_first_minute_missing_recovery(self):
  now=START+timedelta(minutes=6,seconds=10);self.post([2,3,4,5,6],now)
  self.assertEqual(status(self.store,now)['symbols']['SPY']['processed'],self.row(6)[1])
 def test_stale_entry_not_queued(self):
  e=lambda cfg=None:Scripted({(1,1):1,(2,2):1,(3,3):1})
  with patch('scanner.v136_live.runtime.Engine',e):self.post([1,2,3],START+timedelta(minutes=3,seconds=89))
  s=status(self.store,START);self.assertEqual(s['events'][0]['delivery_decision'],'stale');self.assertEqual(s['outbox'],{})
 def test_fresh_entry_expires_at_signal_plus_45(self):
  e=lambda cfg=None:Scripted({(1,1):1,(2,2):1,(3,3):1})
  with patch('scanner.v136_live.runtime.Engine',e):self.post([1,2,3],START+timedelta(minutes=3,seconds=20))
  import json
  with self.store.transaction() as tx:p=json.loads(tx.execute('SELECT payload FROM mf_outbox').fetchone()[0])
  from datetime import datetime
  self.assertEqual(datetime.fromisoformat(p['expires_at']),START+timedelta(minutes=3,seconds=45))
if __name__=='__main__':unittest.main()
