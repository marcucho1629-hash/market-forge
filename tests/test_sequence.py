import unittest
from datetime import datetime,timedelta
from scanner.v136_live.engine import Engine,Bar,ET

START=datetime(2026,10,28,9,30,tzinfo=ET)
def bar(m,start=START,close=100):
    return Bar('SPY',start+timedelta(minutes=m),100,max(101,close),99,close,1000)
class Scripted(Engine):
    def __init__(self,schedule):super().__init__();self.schedule=schedule
    def _entry_side(self,w,vwap):return self.schedule.get((w.tf,w.latest.end.minute-30),0)
    def _risk(self,*args):return []
    def _trend(self,*args):return 1

def run(e,n):return [v for m in range(1,n+1) for v in e.on_bar(bar(m))]
class Tests(unittest.TestCase):
    def test_first_one_two_three(self):
        e=Scripted({(1,1):1,(2,2):1,(3,3):1})
        self.assertEqual([(v.action,v.time.minute,v.timeframe) for v in run(e,9)],[('MC',33,3)])
    def test_short_symmetry(self):
        self.assertEqual(run(Scripted({(1,1):-1,(2,2):-1,(3,3):-1}),3)[0].action,'SCMP')
    def test_trend_alignment_alone_is_not_entry(self):
        self.assertEqual(run(Scripted({(3,3):1}),9),[])
    def test_missing_and_opposite(self):
        for signals in ({(1,1):1,(3,3):1},{(1,1):1,(2,2):-1,(3,3):1}):
            self.assertEqual(run(Scripted(signals),3),[])
    def test_old_cycle_not_reused(self):
        self.assertEqual(run(Scripted({(1,1):1,(2,2):1,(3,6):1}),6),[])
    def test_overlapping_two_minute_cannot_bridge_cycle(self):
        self.assertEqual(run(Scripted({(1,4):1,(2,4):1,(3,6):1}),6),[])
    def test_second_cycle(self):
        self.assertEqual(run(Scripted({(1,4):1,(2,6):1,(3,6):1}),6)[0].time.minute,36)
    def test_seed_only_indicators(self):
        e=Engine();prior=START-timedelta(days=1)
        e.seed([bar(m,prior) for m in range(1,391)])
        self.assertIsNone(e.states['SPY'].position)
        self.assertEqual(e.on_bar(bar(1)),[])
        s=e.states['SPY'];self.assertEqual(s.total_volume,1000)
        self.assertGreater(len(s.windows[3].history),4)
        self.assertEqual(s.episode,0)
    def test_seed_rejects_gaps(self):
        with self.assertRaises(ValueError):Engine().seed([bar(1),bar(3)])
    def test_real_seeded_opening_signal(self):
        e=Engine();e.seed([bar(m,START-timedelta(days=1)) for m in range(1,391)])
        rows=[]
        for m in (1,2,3):
            op=(100,103,103.4)[m-1];cl=(103,103.4,103.8)[m-1]
            rows+=e.on_bar(Bar('SPY',START+timedelta(minutes=m),op,cl+.1,op-.1,cl,10000))
        self.assertEqual([(v.action,v.time.minute) for v in rows],[('MC',33)])
if __name__=='__main__':unittest.main()
