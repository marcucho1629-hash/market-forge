import unittest,tempfile,os
from datetime import timedelta,timezone
from test_sequence import START,bar
from scanner.store import Store
from scanner.v136_live.runtime import prior_seed,ingest,status,PREFIX,VERSION
class SeedRuntime(unittest.TestCase):
 def setUp(self):
  f,self.path=tempfile.mkstemp();os.close(f);self.store=Store(self.path)
 def tearDown(self):os.unlink(self.path)
 def test_legacy_seed_and_no_historical_notifications(self):
  prior=START-timedelta(days=1)
  rows=[]
  for m in range(1,391):
   b=bar(m,prior);rows.append(['SPY',int(b.end.timestamp()*1000),b.open,b.high,b.low,b.close,b.volume])
  with self.store.transaction() as tx:tx.put('research137aligned:2026-10-27:SPY',{'bars':rows})
  now=START+timedelta(minutes=1,seconds=3)
  b=bar(1);data={'source':VERSION,'bank':1,'emitted':int(now.timestamp()*1000),'bars':[['SPY',int(b.end.timestamp()*1000),100,101,99,100,1000]]}
  cal=lambda d:{'is_open':True,'open':START.isoformat(),'close':(START+timedelta(minutes=390)).isoformat()}
  ingest(self.store,data,now,cal)
  s=status(self.store,now)
  self.assertEqual(s['symbols']['SPY']['seed_count'],390)
  self.assertEqual(s['symbols']['SPY']['warmup_mode'],'prior_session')
  self.assertNotIn('seed_bars',s['symbols']['SPY'])
  self.assertEqual(s['events'],[])
  ingest(self.store,data,now,cal)
  self.assertEqual(status(self.store,now)['symbols']['SPY']['bar_count'],1)
 def test_missing_seed_explicit(self):
  with self.store.transaction() as tx:self.assertEqual(prior_seed(tx,'SPY','2026-10-28'),[])
