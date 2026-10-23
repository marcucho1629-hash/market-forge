import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from scanner.store import Store
from scanner.v136_live import runtime as r

OPEN=datetime(2026,10,23,13,30,tzinfo=timezone.utc)
def cal(day): return {'is_open':True,'open':OPEN.isoformat(),'close':(OPEN+timedelta(hours=6,minutes=30)).isoformat()}
def row(i,price=368):
    old=368 if i<=30 else 367.988 if i==31 else 368.35
    close=368+(.012 if i%2 else -.012) if i<=30 else price
    wick=.09 if i<=30 else .04
    return ['TSLA',int((OPEN+timedelta(minutes=i)).timestamp()*1000),old,max(old,close)+wick,min(old,close)-wick,close,100 if i<=30 else 400]
def data(rows,now):return {'source':r.VERSION,'bank':1,'emitted':int(now.timestamp()*1000),'bars':rows}

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name+'/db')
        r.control(self.store,True,OPEN)
        r.delivery(self.store,lambda x:None,OPEN)
    def feed(self,rows,minute):
        now=OPEN+timedelta(minutes=minute,seconds=2)
        return r.ingest(self.store,data(rows,now),now,cal)
    def warm(self):
        for i in range(1,31):self.feed([row(i)],i)
    def test_entry_exit_persistence_dedup(self):
        self.warm();self.feed([row(31,368.35)],31)
        out=[];r.delivery(self.store,out.append,OPEN+timedelta(minutes=31,seconds=3))
        self.assertEqual(len(out),1);self.assertIn(' MC',out[0])
        self.store=Store(self.store.url)
        self.feed([row(31,368.35),row(32,367.20)],32)
        r.delivery(self.store,out.append,OPEN+timedelta(minutes=32,seconds=3))
        self.assertEqual(len(out),2);self.assertIn('FAST_X',out[1])
        self.feed([row(32,367.20)],32)
        self.assertEqual(r.delivery(self.store,out.append,OPEN+timedelta(minutes=32,seconds=4)),[])
    def test_gap_then_recovery(self):
        self.feed([row(2)],2)
        with self.store.transaction() as tx:self.assertEqual(tx.get(r.PREFIX+'2026-10-23:TSLA')['processed'],0)
        self.feed([row(1),row(2)],2)
        with self.store.transaction() as tx:self.assertEqual(tx.get(r.PREFIX+'2026-10-23:TSLA')['processed'],row(2)[1])
    def test_wrong_bank_stale_future(self):
        now=OPEN+timedelta(minutes=1,seconds=2)
        for bad in (data([row(2)],now),data([row(1)],now)|{'bank':2},data([row(1)],now)|{'emitted':0}):
            with self.assertRaises(ValueError):r.ingest(self.store,bad,now,cal)
    def test_off_and_expiry(self):
        r.control(self.store,False,OPEN);self.warm();self.feed([row(31,368.35)],31)
        self.assertEqual(r.delivery(self.store,lambda x:self.fail(),OPEN+timedelta(minutes=31)),[])
        with self.store.transaction() as tx:self.assertFalse(r.enabled(tx,OPEN+timedelta(days=8)))
    def test_exit_not_sent_for_failed_entry(self):
        self.warm();self.feed([row(31,368.35)],31)
        def fail(x):raise RuntimeError()
        r.delivery(self.store,fail,OPEN+timedelta(minutes=31,seconds=3))
        self.feed([row(32,367.20)],32)
        self.assertEqual(r.delivery(self.store,lambda x:self.fail(),OPEN+timedelta(minutes=32,seconds=3)),[])
    def test_conflicting_revision_halts(self):
        self.warm();bad=row(30);bad[6]=101
        self.feed([bad,row(31,368.35)],31)
        with self.store.transaction() as tx:self.assertIn('halted',tx.get(r.PREFIX+'2026-10-23:TSLA'))
        self.assertFalse(any(' MC' in item for item in self.messages(31)))
    def messages(self,i):
        out=[];r.delivery(self.store,out.append,OPEN+timedelta(minutes=i,seconds=3));return out

if __name__=='__main__':unittest.main()
