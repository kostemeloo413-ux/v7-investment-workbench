import unittest,sys,copy,datetime,hashlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
import simulated_trading as paper

class PaperQuoteAdmissionTests(unittest.TestCase):
    def quote(self,observed='2026-10-12T09:39:00+08:00',price=12):
        fields=['']*40;fields[2]='600000';fields[3]=str(price)
        try:fields[30]=datetime.datetime.fromisoformat(observed).strftime('%Y%m%d%H%M%S')
        except ValueError:fields[30]=''
        raw='~'.join(fields)
        return {'provider':'Tencent','code':'600000','price':price,'observed_at':observed,'raw_payload':raw,'response_sha256':hashlib.sha256(raw.encode('utf-8')).hexdigest()}
    def usable(self,q,**kw):
        return paper.quote_is_usable(q,'sh600000',now=datetime.datetime.fromisoformat('2026-10-12T09:40:00+08:00'),**kw)
    def test_fresh_source_quote_passes_but_old_receipt_cannot_renew(self):
        self.assertTrue(self.usable(self.quote()))
        q=self.quote('2026-10-12T09:38:29+08:00');q['received_at']='2026-10-12T09:40:00+08:00'
        self.assertFalse(self.usable(q))
    def test_wrong_day_future_naive_missing_or_nonfinite_quote_blocks(self):
        for q in [None,{},self.quote('2026-10-09T15:00:00+08:00'),self.quote('2026-10-12T09:41:00+08:00'),self.quote('2026-10-12T09:39:00'),self.quote(price=float('nan'))]:
            with self.subTest(q=q):self.assertFalse(self.usable(q))
    def test_payload_hash_or_observation_edit_cannot_pass(self):
        q=self.quote();q['observed_at']='2026-10-12T09:39:30+08:00';self.assertFalse(self.usable(q))
        q=self.quote();q['raw_payload']+='tampered';self.assertFalse(self.usable(q))
    def test_auction_quote_does_not_become_post_open_quote(self):
        q=self.quote('2026-10-12T09:30:00+08:00')
        self.assertFalse(paper.quote_is_usable(q,'sh600000',now=datetime.datetime.fromisoformat('2026-10-12T09:30:30+08:00')))
    def test_exit_missing_quote_preserves_account_and_has_no_fill(self):
        account={'cash':1234};before=copy.deepcopy(account)
        with patch.object(paper,'fetch_prices',return_value={}):
            result=paper.execute_sell(account,'test','sh600000','test','600000',100,15,'risk')
        self.assertIsNone(result);self.assertEqual(account,before)
    def test_close_quote_is_exit_only_and_never_previous_day(self):
        now=datetime.datetime.fromisoformat('2026-10-12T15:08:00+08:00')
        q=self.quote('2026-10-12T15:00:00+08:00')
        self.assertTrue(paper.quote_is_usable(q,'sh600000',for_buy=False,now=now))
        self.assertFalse(paper.quote_is_usable(q,'sh600000',now=now))
        self.assertFalse(paper.quote_is_usable(self.quote('2026-10-09T15:00:00+08:00'),'sh600000',for_buy=False,now=now))
