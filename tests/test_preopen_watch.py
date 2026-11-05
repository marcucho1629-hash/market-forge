import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scanner.store import Store
from scanner.preopen import core as c, runtime as r

START=datetime(2026,11,4,13,tzinfo=timezone.utc)
CUTOFF=START+timedelta(minutes=80)
RECEIVED=CUTOFF-timedelta(seconds=40)


def row(t,p,v=10000):
    return dict(t=t.isoformat(),o=p-.01,h=p+.04,l=p-.03,c=p,v=v,n=10,vw=p)


def sample():
    seed=[row(START-timedelta(days=1)+timedelta(minutes=5*i),100+i*.002) for i in range(78)]
    five=[row(START+timedelta(minutes=5*i),100.16+i*.002) for i in range(15)]
    one=[dict(b,t=(c.stamp(b['t'])+timedelta(minutes=1)).isoformat()) for b in five]
    return seed,one,five


class CoreTests(unittest.TestCase):
    def test_universe_unique_150_and_free(self):
        cfg=c.config();self.assertEqual(len(set(cfg['symbols'])),150);self.assertEqual(cfg['feed'],'iex')

    def test_native_sparse_is_not_missing_and_no_imputation(self):
        seed,one,five=sample(); result=c.evaluate('AAPL',seed,one,five,START,CUTOFF,RECEIVED,c.config())
        self.assertEqual(result['quality'],'SPARSE_RECONCILED')
        self.assertEqual(result['minute_bars'],15)
        self.assertFalse(result['strict_minute_policy_pass'])
        self.assertEqual(result['catalyst'],'UNKNOWN')

    def test_late_capture_blocked(self):
        result=c.evaluate('AAPL',*sample(),START,CUTOFF,CUTOFF+timedelta(seconds=1),c.config())
        self.assertEqual(result['quality'],'LATE_RECEIPT')

    def test_future_bar_not_used(self):
        seed,one,five=sample()
        baseline=c.evaluate('AAPL',seed,one,five,START,CUTOFF,RECEIVED,c.config())
        future=row(CUTOFF,10000)
        result=c.evaluate('AAPL',seed,one+[future],five+[future],START,CUTOFF,RECEIVED,c.config())
        self.assertEqual(baseline,result)

    def test_mismatch_and_invalid_rejected(self):
        seed,one,five=sample(); one[0]['v']=1
        result=c.evaluate('AAPL',seed,one,five,START,CUTOFF,RECEIVED,c.config())
        self.assertIn('aggregation_mismatch',result['reasons'])
        one[0]['c']=float('nan')
        self.assertEqual(c.evaluate('AAPL',seed,one,five,START,CUTOFF,RECEIVED,c.config())['quality'],'INVALID')

    def test_bull_bear_reflection(self):
        seed,one,five=sample(); original=c.evaluate('AAPL',seed,one,five,START,CUTOFF,RECEIVED,c.config())
        def mirror(rows):
            return [b|dict(o=200-b['o'],h=200-b['l'],l=200-b['h'],c=200-b['c'],vw=200-b['vw']) for b in rows]
        other=c.evaluate('AAPL',mirror(seed),mirror(one),mirror(five),START,CUTOFF,RECEIVED,c.config())
        self.assertEqual(original['direction'],'BULLISH');self.assertEqual(other['direction'],'BEARISH')
        self.assertEqual(original['score'],other['score']);self.assertEqual(original['technical_pass'],other['technical_pass'])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name+'/test.db');self.sent=[]
        self.cfg=c.config()
        self.session=dict(date='2026-11-04',open='09:30',close='16:00')
        with self.store.transaction() as tx:tx.put(r.PREFIX+'control',dict(enabled=True))

    def test_finish_dedup_and_unrelated_outbox_untouched(self):
        with self.store.transaction() as tx:tx.enqueue('day-existing',dict(text='existing'))
        with patch.object(r,'utc',return_value=CUTOFF):
            r.finish(self.store,self.sent.append,CUTOFF,self.session,self.cfg)
            r.finish(self.store,self.sent.append,CUTOFF,self.session,self.cfg)
        self.assertEqual(len(self.sent),1);self.assertIn('WATCH ONLY',self.sent[0])
        with self.store.transaction() as tx:self.assertEqual(tx.execute('SELECT status FROM mf_outbox WHERE id=?',('day-existing',)).fetchone()[0],'pending')

    def test_holiday_never_fetches_or_sends(self):
        with patch.object(r,'session_info',return_value=(None,None)),patch.object(r,'capture') as fetch:
            self.assertEqual(r.worker(self.store,self.sent.append,CUTOFF)['status'],'market_closed')
            fetch.assert_not_called();self.assertFalse(self.sent)

    def test_off_hours_no_catchup(self):
        with patch.object(r,'session_info') as cal:
            self.assertEqual(r.worker(self.store,self.sent.append,CUTOFF+timedelta(minutes=11))['status'],'outside_preopen_window')
            cal.assert_not_called()

    def test_disabled(self):
        with self.store.transaction() as tx:tx.put(r.PREFIX+'control',dict(enabled=False))
        self.assertEqual(r.worker(self.store,self.sent.append,CUTOFF)['status'],'disabled')

    def test_late_snapshot_does_not_replace_on_time(self):
        seed,one,five=sample();day=self.session['date']
        good=dict(start=START.isoformat(),end=(CUTOFF-timedelta(minutes=5)).isoformat(),config_hash=c.config_hash(self.cfg),symbols={'AAPL':dict(seed=seed,**{'1Min':one,'5Min':five},errors={},received={'1Min':RECEIVED.isoformat(),'5Min':RECEIVED.isoformat()})})
        late=json.loads(json.dumps(good));late['symbols']['AAPL']['received']['5Min']=(CUTOFF+timedelta(seconds=1)).isoformat();late['symbols']['AAPL']['errors']={'5Min':'late_error'}
        with self.store.transaction() as tx:
            tx.put('good',good);tx.put('late',late);tx.put(r.PREFIX+day+':captures',['good','late'])
        result=r.build_report(self.store,day,CUTOFF,self.cfg)
        aapl=next(x for x in result['rows'] if x['ticker']=='AAPL')
        self.assertEqual(aapl['quality'],'SPARSE_RECONCILED')
        self.assertEqual(len(result['rows']),150)

    def test_pagination_failure_not_empty_success(self):
        with patch.object(r,'api',return_value={'bars':{},'next_page_token':'same'}):
            with self.assertRaises(r.DataError):r.fetch(['AAPL'],'5Min',START,CUTOFF)


if __name__=='__main__':unittest.main()
