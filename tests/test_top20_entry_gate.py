"""Top20 entry-only Telegram gating regression; legacy exits remain unfiltered."""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock,patch

from scanner.store import Store,drain,TOP20_ENTRY_FILTERED
from scanner.top20_entry_gate import entry_from_text,telegram_decision,set_enabled,status

NOW=datetime(2026,11,10,16,50,tzinfo=timezone.utc)
DAY="2026-11-10"

def snap(now=NOW):
    return dict(day=DAY,mode="WATCH_ONLY",at=(now-timedelta(minutes=1)).isoformat(),
        bar_cutoff=(now-timedelta(minutes=3)).isoformat(),ready_count=145,
        selected=[dict(ticker="AVGO",direction="BULLISH",eligible=True,chase="LOW",score=82),
                  dict(ticker="HIMS",direction="BEARISH",eligible=True,chase="MED",score=77),
                  dict(ticker="NVDA",direction="BULLISH",eligible=True,chase="HIGH",score=89)])

class Parsing(unittest.TestCase):
    def test_all_new_entry_formats(self):
        cases={
            "🚀 AVGO | MC | $321\nMomentum: HIGH":("AVGO","MC"),
            "🚨 HIMS | SCMP | $22":("HIMS","SCMP"),
            "⚡ AVGO MC · 상승\n$123":("AVGO","MC"),
            "🟢 HIMS SCMP · 하락\n$123":("HIMS","SCMP"),
            "DAY TEST AVGO MC\n$321":("AVGO","MC"),
            "TEST ⚡ AVGO EARLY MC\n$321":("AVGO","MC"),
            "TEST HIMS ⚡ EARLY SCMP\n$321":("HIMS","SCMP"),
            "TEST · AVGO EARLY MC\n$123":("AVGO","MC"),
            "TEST · IEX · 1분+2분\nHIMS SCMP · 하락":("HIMS","SCMP"),
        }
        for message, expected in cases.items():
            with self.subTest(message=message):self.assertEqual(entry_from_text(message),expected)
    def test_exit_and_followup_messages_always_unclassified(self):
        for txt in (
            "✖ AVGO X · 상승 후보 종료",
            "🟠 AVGO | SX | $321",
            "TEST AVGO MC · ✅ 봉 마감 확인\n새 진입 아님",
            "TEST AVGO SCMP · ✖ 후보 취소",
            "TEST · AVGO 확정 MC\n5분봉 확정 · 새 진입 아님",
            "DAY TEST AVGO DATA_CONFLICT",
            "DAY TEST AVGO DATA_GAP:123",
            "MARKET FORGE AI | INTRADAY V3 09:00 MT\nMC/SCMP 기존 엔진 분리",
            "SWING TEST 연결 테스트",
            "TEST · AVGO X\n종료"
        ):
            with self.subTest(txt=txt):self.assertIsNone(entry_from_text(txt))

class Gate(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store=Store(tmp.name+"/db.sqlite3")
        with self.store.transaction() as tx:
            tx.put("intraday-v3:"+DAY+":latest",snap())
    def check(self,message,now=NOW):
        return telegram_decision(self.store,message,now)
    def test_matching_only_direction(self):
        self.assertTrue(self.check("🚀 AVGO | MC | $123")["allow"])
        self.assertEqual(status(self.store,NOW)["low_entry_bullish"],["AVGO"])
        self.assertEqual(status(self.store,NOW)["low_entry_bearish"],[])
        self.assertFalse(self.check("🚨 HIMS | SCMP | $12")["allow"])
        self.assertEqual(self.check("🚨 HIMS | SCMP | $12")["reason"],"chase_not_low")
        self.assertFalse(self.check("🚨 AVGO | SCMP | $123")["allow"])
        self.assertFalse(self.check("🚀 HIMS | MC | $123")["allow"])
        self.assertFalse(self.check("🚀 TSLA | MC | $123")["allow"])
        self.assertFalse(self.check("🚀 NVDA | MC | $123")["allow"])
        self.assertEqual(self.check("🚀 NVDA | MC | $123")["reason"],"chase_not_low")
    def test_opening_0720_watch_cannot_masquerade_as_verified_low(self):
        morning=datetime(2026,11,10,14,35,tzinfo=timezone.utc)
        with self.store.transaction() as tx:
            tx.put("preopen-watch-v1:"+DAY+":report",
                   dict(day=DAY,cutoff="2026-11-10T14:20:00+00:00",
                        prior_rth_context=dict(selected=[
                            dict(ticker="NVDA",direction="BULLISH",selected=True),
                            dict(ticker="HIMS",direction="BEARISH",selected=True)])))
        self.assertFalse(self.check("🚀 NVDA | MC | $123",morning)["allow"])
        self.assertFalse(self.check("🚨 HIMS | SCMP | $123",morning)["allow"])
        self.assertEqual(self.check("🚨 HIMS | SCMP | $123",morning)["reason"],
                         "chase_low_unverified_before_0800")
        self.assertFalse(self.check("🚀 TSLA | MC | $123",morning)["allow"])
        self.assertFalse(self.check("🚀 NVDA | MC | $123",NOW)["allow"])
    def test_med_remains_top10_but_low_is_required_for_new_alerts(self):
        with self.store.transaction() as tx:
            snap_now=snap()
            self.assertIn("HIMS",[r["ticker"] for r in snap_now["selected"]])
        self.assertEqual(self.check("🚨 HIMS | SCMP | $123")["reason"],"chase_not_low")
        with self.store.transaction() as tx:
            snapshot=tx.get("intraday-v3:"+DAY+":latest")
            snapshot["selected"][1]["chase"]="LOW"
            snapshot["at"]=(NOW-timedelta(seconds=25)).isoformat()
            tx.put("intraday-v3:"+DAY+":latest",snapshot)
        self.assertTrue(self.check("🚨 HIMS | SCMP | $123")["allow"])
        self.assertEqual(status(self.store,NOW)["low_entry_bearish"],["HIMS"])
        self.assertEqual(self.check("🚨 HIMS | SCMP | $123")["reason"],"top10_low_matched")
        # This allows a NEW incoming event; it does not replay any previously
        # filtered SCMP event or manufacture a signal from the scan itself.
    def test_unknown_chase_fails_closed_even_if_selected(self):
        with self.store.transaction() as tx:
            latest=tx.get("intraday-v3:"+DAY+":latest")
            latest["selected"][0].pop("chase")
            tx.put("intraday-v3:"+DAY+":latest",latest)
        self.assertEqual(self.check("🚀 AVGO | MC | $123")["reason"],"chase_not_low")
    def test_explicit_chop_and_late_chase_never_pass_entry_gate(self):
        with self.store.transaction() as tx:
            current=snap()
            current["selected"][0]["choppy"]=True
            current["selected"][1]["chase"]="LOW"
            current["selected"][1]["late_chase"]=True
            tx.put("intraday-v3:"+DAY+":latest",current)
        self.assertFalse(self.check("🚀 AVGO | MC | $123")["allow"])
        self.assertFalse(self.check("🚨 HIMS | SCMP | $123")["allow"])
    def test_stale_missing_and_old_session_fail_closed(self):
        self.assertEqual(self.check("🚀 AVGO | MC | $123",NOW+timedelta(minutes=13))["reason"],"stale_snapshot")
        with self.store.transaction() as tx:
            s=snap();s["day"]="2026-11-09"
            tx.put("intraday-v3:"+DAY+":latest",s)
        self.assertEqual(self.check("🚀 AVGO | MC | $123")["reason"],"missing_current_day_snapshot")
        self.assertTrue(self.check("✖ AVGO X · 상승 후보 종료")["allow"])
    def test_admin_disable_reversible_and_logged(self):
        set_enabled(self.store,False,NOW)
        self.assertTrue(self.check("🚀 TSLA | MC | $123")["allow"])
        self.assertFalse(status(self.store,NOW)["enabled"])
        self.assertEqual(status(self.store,NOW)["required_chase"],"LOW")
        set_enabled(self.store,True,NOW)
        self.assertFalse(self.check("🚀 TSLA | MC | $123")["allow"])
    def test_outbox_suppression_is_never_mislabeled_sent(self):
        with self.store.transaction() as tx:
            tx.enqueue("x1",{"text":"🚀 TSLA | MC | $123"})
            tx.enqueue("x2",{"text":"✖ TSLA X · exit"})
        sent=[]
        def send(msg):
            if entry_from_text(msg) and not self.check(msg)["allow"]:
                return TOP20_ENTRY_FILTERED
            sent.append(msg)
            return 200
        result=drain(self.store,send,now=NOW)
        self.assertEqual([r["status"] for r in result],["filtered_top20","sent"])
        self.assertEqual(sent,["✖ TSLA X · exit"])
    def test_actual_send_telegram_wrapper(self):
        import index
        response=Mock(status_code=200)
        response.json.return_value={"ok":True}
        with patch.dict("os.environ",{"TELEGRAM_BOT_TOKEN":"unit-test-token","TELEGRAM_CHAT_ID":"unit-test-chat"}),patch.object(index,"storage",return_value=self.store),patch.object(index.requests,"post",return_value=response) as post:
            self.assertIs(index.send_telegram("🚀 TSLA | MC | $123"),TOP20_ENTRY_FILTERED)
            post.assert_not_called()
            self.assertEqual(index.send_telegram("✖ TSLA X · 상승 후보 종료"),200)
            post.assert_called_once()

if __name__=="__main__":
    unittest.main()
