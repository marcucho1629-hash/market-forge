import unittest
from datetime import datetime,timezone,timedelta
from scanner.simple.router import normalize,process,expire
class RoutingTests(unittest.TestCase):
 def setUp(self):self.t=datetime(2026,10,14,13,30,tzinfo=timezone.utc);self.s={};self.session={'open':self.t.isoformat(),'close':(self.t+timedelta(hours=6,minutes=30)).isoformat()}
 def row(self,minute=0,tf='1',side=1,chop=False,exit='',exit_side=0):
  t=self.t+timedelta(minutes=minute);end=t+timedelta(minutes=int(tf))
  raw=dict(ticker='MSFT',feed='BATS:MSFT',timeframe=tf,bar_start=t.isoformat(),bar_end=end.isoformat(),closed=True,mc=side==1,scmp=side==-1,choppy=chop,exit=exit,exit_side=exit_side,price=100.)
  return normalize(raw,end+timedelta(seconds=1),self.session)
 def test_early_then_same_bar_confirm(self):
  a=process(self.s,self.row());b=process(self.s,self.row(tf='5'))
  self.assertEqual([e['action'] for e in a+b],['EARLY','CONFIRM']);self.assertEqual(a[0]['candidate_id'],b[0]['candidate_id'])
 def test_duplicate_early(self):
  process(self.s,self.row());self.assertEqual(process(self.s,self.row(minute=1)),[])
 def test_opposite_close_never_confirms_early(self):
  process(self.s,self.row());a=process(self.s,self.row(tf='5',side=-1))
  self.assertEqual([e['action'] for e in a],['UNCONFIRMED','CONFIRM'])
 def test_no_close_signal_resolves_early(self):
  process(self.s,self.row());self.assertEqual(process(self.s,self.row(tf='5',side=0))[0]['action'],'UNCONFIRMED')
 def test_no_early_after_opening(self):self.assertEqual(process(self.s,self.row(minute=30)),[])
 def test_no_early_at_five_minute_boundary(self):self.assertEqual(process(self.s,self.row(minute=4)),[])
 def test_midday_chop_blocks_entry_not_exit(self):
  first=process(self.s,self.row(tf='5'));r=self.row(minute=30,tf='5',side=-1,chop=True,exit='X',exit_side=1)
  a=process(self.s,r);self.assertEqual([e['action'] for e in a],['X']);self.assertEqual(a[0]['candidate_id'],first[0]['candidate_id'])
 def test_unrelated_exit_not_linked(self):
  process(self.s,self.row(tf='5'));self.assertEqual(process(self.s,self.row(minute=5,tf='5',side=0,exit='X',exit_side=-1)),[])
 def test_missing_close_distinct_from_failure(self):
  process(self.s,self.row());a=expire(self.s,self.t+timedelta(minutes=7));self.assertEqual(a[0]['action'],'CLOSE_MISSING');self.assertEqual(expire(self.s,self.t+timedelta(minutes=8)),[])
 def test_next_bucket_is_not_confirmation(self):
  process(self.s,self.row());process(self.s,self.row(minute=5,tf='5'));self.assertEqual(next(iter(self.s['pending'].values()))['phase'],'EARLY')
 def test_late_early_cannot_reopen_closed_bucket(self):
  process(self.s,self.row(tf='5'));self.assertEqual(process(self.s,self.row()),[])
 def test_short_symmetric(self):
  self.assertEqual(process(self.s,self.row(side=-1))[0]['side'],-1);self.assertEqual(process(self.s,self.row(tf='5',side=-1))[0]['action'],'CONFIRM')
if __name__=='__main__':unittest.main()

class ValidationTests(RoutingTests):
 def test_future_bar_rejected(self):
  r=self.row()
  with self.assertRaises(ValueError):normalize(r,self.t,self.session)
 def test_stale_bar_rejected(self):
  r=self.row()
  with self.assertRaises(ValueError):normalize(r,self.t+timedelta(minutes=4),self.session)
 def test_nonfinite_price_rejected(self):
  r=self.row();r['price']=float('nan')
  with self.assertRaises(ValueError):normalize(r,self.t+timedelta(minutes=1,seconds=1),self.session)
 def test_partial_bar_rejected(self):
  r=self.row();r['closed']=False
  with self.assertRaises(ValueError):normalize(r,self.t+timedelta(minutes=1,seconds=1),self.session)
