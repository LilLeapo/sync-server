"""Supplier cases: deterministic replay and condition-aware comparison."""
import copy
import json
import os
import tempfile
import unittest

from server.quote_messages import analysis_units, timestamp
from server.quote_rules import QuoteLedger, compare_quotes

NOW = timestamp("2026-10-09T12:00:00+08:00")


def source(text, seq=1, at=NOW, **extra):
    return {"roomId": "suppliers@chatroom", "sender": "采购", "senderId": "buyer",
            "time": at, "receivedAt": at, "msgSeq": seq, "msgSvrId": str(seq),
            "part": "message", "sourceId": str(seq), "source": "文字", "content": text, **extra}


def quote(price=110800, supplier="湖北桦昌", **details):
    return {"event_type": "quote", "供应商": supplier, "项目": "低氧铜杆", "价格": str(price),
            "单位": "元/吨", "evidence": f"{supplier} {price}元/吨", "certainty": "explicit",
            "details": {"pricing": "fixed", "amount": price, "currency": "CNY", "unit": "元/吨",
                        "tax": "included", "delivery": "delivered", "destination": "宜兴",
                        "availability": "available", **details}}


def ingest(ledger, item, seq=1, **extra):
    return ledger.ingest([item], source(item.get("evidence", ""), seq, **extra))


class TestRules(unittest.TestCase):
    def test_pickup_discount_has_evidence_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "events.jsonl")
            ledger = QuoteLedger(path)
            item = quote(pickup_discount=150, pickup_region="湖北")
            item["evidence"] += "送到，自提减150"
            rows = ingest(ledger, item)
            self.assertEqual(len(rows), 2)
            pickup = next(row for row in rows if row["details"]["delivery"] == "pickup")
            self.assertEqual(pickup["details"]["amount"], 110650)
            self.assertIsNone(pickup["details"]["destination"])
            self.assertEqual(pickup["calculation"][0]["rule"], "pickup_discount")
            restored = QuoteLedger(path)
            self.assertEqual(restored.current(), ledger.current())
            ingest(restored, item)
            self.assertEqual(len(restored.events), 1)

    def test_rejected_bid_is_not_a_quote(self):
        ledger = QuoteLedger()
        bid = {"event_type": "rejection", "供应商": "劲帆", "项目": "低氧铜杆",
               "价格": "110100", "details": {"amount": 110100}, "certainty": "explicit",
               "evidence": "劲帆110100不出，还是最低110200"}
        self.assertEqual(ingest(ledger, bid), [])
        ingest(ledger, quote(110200, "劲帆"), 2)
        self.assertEqual([row["details"]["amount"] for row in ledger.current()], [110200])
        self.assertEqual(ledger.events[0]["event_type"], "rejection")

    def test_adjustment_only_updates_one_unambiguous_scheme(self):
        ledger = QuoteLedger()
        old = ingest(ledger, quote())[0]
        change = {"event_type": "adjustment", "供应商": "湖北桦昌", "certainty": "explicit",
                  "evidence": "湖北桦昌下调50", "details": {"delta": -50}}
        ingest(ledger, change, 2, at=NOW + 1000)
        self.assertEqual(ledger.current()[0]["details"]["amount"], 110750)
        self.assertEqual(ledger.quotes[old["quoteId"]]["status"], "superseded")
        ingest(ledger, quote(110900, invoice_period="下月票"), 3, at=NOW + 2000)
        ingest(ledger, change, 4, at=NOW + 3000)
        self.assertEqual(len(ledger.current()), 2)
        self.assertEqual(ledger.history()[0]["outcome"]["reasons"], ["target_not_unique"])

    def test_no_price_stock_message_removes_offer_and_is_replayable(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = QuoteLedger(os.path.join(root, "events.jsonl"))
            ingest(ledger, quote())
            change = {"event_type": "stock", "供应商": "湖北桦昌", "certainty": "explicit",
                      "evidence": "湖北桦昌没货了", "details": {"availability": "unavailable"}}
            ingest(ledger, change, 2, at=NOW + 1000)
            self.assertFalse([row for row in ledger.current() if row["status"] == "active"])
            restored = QuoteLedger(ledger.path)
            self.assertEqual(restored.current(), ledger.current())
            self.assertEqual(restored.history()[0]["outcome"]["status"], "applied")

    def test_original_time_prevents_late_forward_overwriting_new_price(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(110900), at=NOW + 60000)
        ingest(ledger, quote(110800), 2, at=NOW)
        active = [row for row in ledger.current() if row["status"] == "active"]
        self.assertEqual(active[0]["details"]["amount"], 110900)

    def test_tax_and_delivery_variants_coexist(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(110600, "金瑞", delivery_max_days=None, quality="保阻"))
        ingest(ledger, quote(110750, "金瑞", delivery_max_days=4, delivery_basis="unknown"), 2)
        ingest(ledger, quote(110650, "金瑞", delivery_max_days=4, invoice_period="下月票"), 3)
        self.assertEqual(len(ledger.current()), 3)
        result = compare_quotes(ledger.current(), {"destination": "宜兴", "max_delivery_days": 3,
                                                   "as_of": NOW, "tax": "included"})
        self.assertEqual(result["groups"], [])
        self.assertEqual(len(result["needs_confirmation"]), 3)

    def test_surcharge_does_not_upgrade_base_quality(self):
        ledger = QuoteLedger()
        item = quote(110800, "宝祺", quality="1710左右", quality_surcharge={"amount": 100,
                     "resistance_max": 1710, "resistance_unit": "quote_code"})
        rows = ingest(ledger, item)
        self.assertIsNone(rows[0]["details"]["resistance_max"])
        self.assertEqual(rows[1]["details"]["amount"], 110900)
        result = compare_quotes(rows, {"destination": "宜兴", "resistance_max": 1710, "as_of": NOW})
        self.assertEqual(result["groups"][0]["quotes"][0]["price"], 110900)
        self.assertEqual(len(result["needs_confirmation"]), 1)

    def test_partial_inferred_or_conflicting_evidence_cannot_rank(self):
        for extra, change in (({"partial": True}, {}), ({}, {"certainty": "context"}),
                              ({}, {"evidence": "不存在的原文"})):
            ledger = QuoteLedger()
            item = quote()
            item.update(change)
            rows = ledger.ingest([item], source("湖北桦昌 110800元/吨", **extra))
            result = compare_quotes(rows, {"destination": "宜兴", "as_of": NOW})
            self.assertEqual(result["groups"], [])
            self.assertTrue(result["needs_confirmation"])

    def test_comparison_separates_conditions_and_does_not_double_charge_freight(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(110800, freight=150, freight_destination="宜兴"))
        ingest(ledger, quote(110700, "金瑞", invoice_period="下月票"), 2)
        result = compare_quotes(ledger.current(), {"destination": "宜兴", "as_of": NOW})
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(sorted(g["quotes"][0]["price"] for g in result["groups"]), [110700, 110800])

    def test_futures_requires_matching_contract_fresh_sourced_benchmark(self):
        ledger = QuoteLedger()
        item = quote(pricing="futures", amount=None, premium=-300, benchmark="沪铜", contract="2610")
        item["价格"] = "2610-300"
        rows = ingest(ledger, item)
        params = {"destination": "宜兴", "as_of": NOW}
        self.assertFalse(compare_quotes(rows, params)["groups"])
        params["benchmark"] = {"name": "沪铜", "contract": "2610", "price": 111000,
                               "at": NOW, "source": "用户提供"}
        self.assertEqual(compare_quotes(rows, params)["groups"][0]["quotes"][0]["price"], 110700)
        params["benchmark"]["contract"] = "2609"
        self.assertFalse(compare_quotes(rows, params)["groups"])

    def test_stale_quotes_not_ranked(self):
        ledger = QuoteLedger()
        rows = ingest(ledger, quote())
        result = compare_quotes(rows, {"destination": "宜兴", "as_of": NOW + 25 * 3600000})
        self.assertFalse(result["groups"])
        self.assertTrue(result["needs_confirmation"])

    def test_stock_quantity_requires_batch_and_does_not_invent_truck_weight(self):
        ledger = QuoteLedger()
        row = ingest(ledger, quote(available_quantity=5, quantity_unit="车"))[0]
        purchase = {"event_type": "purchase", "供应商": "湖北桦昌", "certainty": "explicit",
                    "details": {"available_quantity": 5, "quantity_unit": "车"}, "evidence": "五车都拿了"}
        ingest(ledger, purchase, 2)
        self.assertEqual(ledger.current()[0]["details"]["available_quantity"], 5)
        purchase["target_id"] = row["quoteId"]
        ingest(ledger, purchase, 3)
        self.assertFalse([row for row in ledger.current() if row["status"] == "active"])

    def test_stock_update_preserves_price_age_and_updates_all_derived_choices(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(pickup_discount=150, pickup_region="湖北"))
        change = {"event_type": "stock", "供应商": "湖北桦昌", "certainty": "explicit",
                  "evidence": "湖北桦昌没货了", "details": {"availability": "unavailable"}}
        ingest(ledger, change, 2, at=NOW + 25 * 3600000)
        self.assertEqual(len(ledger.current()), 2)
        self.assertTrue(all(row["status"] == "unavailable" for row in ledger.current()))
        change.update(evidence="湖北桦昌有货了", details={"availability": "available"})
        ingest(ledger, change, 3, at=NOW + 26 * 3600000)
        self.assertTrue(all(row["status"] == "active" and row["time"] == NOW for row in ledger.current()))
        result = compare_quotes(ledger.current(), {"destination": "宜兴", "as_of": NOW + 26 * 3600000})
        self.assertFalse(result["groups"])

    def test_price_change_after_stock_update_invalidates_old_derived_price(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(pickup_discount=150, pickup_region="湖北"))
        ingest(ledger, {"event_type": "stock", "供应商": "湖北桦昌", "certainty": "explicit",
                       "evidence": "湖北桦昌有货", "details": {"availability": "available"}}, 2, at=NOW + 1000)
        base = next(row for row in ledger.current() if row["details"]["delivery"] == "delivered")
        ingest(ledger, {"event_type": "adjustment", "供应商": "湖北桦昌", "target_id": base["quoteId"],
                       "certainty": "explicit", "evidence": "湖北桦昌下调50", "details": {"delta": -50}}, 3, at=NOW + 2000)
        rows = ledger.current()
        self.assertEqual(next(row for row in rows if row["status"] == "active")["details"]["amount"], 110750)
        pickup = next(row for row in rows if row["details"]["delivery"] == "pickup")
        self.assertIn("derived_basis_superseded", pickup["warnings"])
        self.assertEqual(pickup["status"], "needs_confirmation")

    def test_explicit_amendment_repairs_missing_supplier_without_refreshing_price_time(self):
        ledger = QuoteLedger()
        old = ingest(ledger, quote(supplier=""))[0]
        self.assertEqual(old["status"], "needs_confirmation")
        item = {"event_type": "amendment", "供应商": "湖北桦昌", "target_id": old["quoteId"],
                "certainty": "explicit", "evidence": "上面是湖北桦昌的报价", "changes": {"供应商": "湖北桦昌"}}
        ingest(ledger, item, 2, at=NOW + 1000)
        current = ledger.current()[0]
        self.assertEqual(current["供应商"], "湖北桦昌")
        self.assertEqual(current["status"], "active")
        self.assertEqual(current["time"], NOW)
        self.assertEqual(current["priceEvidence"], old["evidence"])

    def test_explicit_amendment_does_not_clear_unrelated_uncertainty(self):
        ledger = QuoteLedger()
        old = ingest(ledger, quote(supplier=""), partial=True)[0]
        item = {"event_type": "amendment", "target_id": old["quoteId"], "certainty": "explicit",
                "evidence": "供应商湖北桦昌", "changes": {"供应商": "湖北桦昌"}}
        ingest(ledger, item, 2, at=NOW + 1000)
        current = next(row for row in ledger.current() if row["quoteId"] != old["quoteId"])
        self.assertEqual(current["status"], "needs_confirmation")
        self.assertIn("partial_source", current["warnings"])

    def test_arithmetic_conflict_is_preserved_for_confirmation(self):
        ledger = QuoteLedger()
        item = quote(benchmark_amount=110000, premium=700)
        rows = ingest(ledger, item)
        self.assertIn("price_expression_mismatch", rows[0]["warnings"])
        self.assertFalse(compare_quotes(rows, {"destination": "宜兴", "as_of": NOW})["groups"])

    def test_invalid_derived_conditions_do_not_create_purchasable_prices(self):
        for details in ({"pickup_discount": 120000}, {"quality_surcharge": {
            "amount": 100, "resistance_max": -1710, "resistance_unit": "quote_code"}}):
            rows = ingest(QuoteLedger(), quote(**details))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "needs_confirmation")

    def test_explicit_wan_prices_are_supported_without_expanding_unlabelled_shorthand(self):
        for price, text, valid in ((110000, "铜杆11万", True), (12000, "1.2万元", True),
                                   (110800, "铜杆1088", False), (110800, "铜杆110800.5", False),
                                   (110800, "110800 150运费", True), (110800, "-110800", False)):
            item = quote(price)
            item["evidence"] = text
            rows = ingest(QuoteLedger(), item)
            self.assertEqual("price_not_in_evidence" not in rows[0]["warnings"], valid)

    def test_futures_adjustment_preserves_contract_in_display_price(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(pricing="futures", amount=None, premium=-300, benchmark="沪铜", contract="2610"))
        ingest(ledger, {"event_type": "adjustment", "供应商": "湖北桦昌", "certainty": "explicit",
                       "evidence": "湖北桦昌下调50", "details": {"delta": -50}}, 2, at=NOW + 1000)
        self.assertEqual(ledger.current()[0]["价格"], "2610-350.0")

    def test_ledger_reader_resets_on_larger_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "events.jsonl")
            writer = QuoteLedger(path)
            ingest(writer, quote())
            reader = QuoteLedger(path)
            replacement = path + ".tmp"
            rebuilt = QuoteLedger(replacement)
            ingest(rebuilt, quote(110900), 2)
            ingest(rebuilt, quote(110950, "劲帆"), 3)
            self.assertGreater(os.path.getsize(replacement), os.path.getsize(path))
            os.replace(replacement, path)
            self.assertEqual(len(reader.current()), 2)
            self.assertNotIn(110800, [row["details"]["amount"] for row in reader.current()])

    def test_historical_state_precedes_later_price_and_stock_events(self):
        ledger = QuoteLedger()
        ingest(ledger, quote())
        ingest(ledger, quote(110900), 2, at=NOW + 1000)
        ingest(ledger, {"event_type": "stock", "供应商": "湖北桦昌", "certainty": "explicit",
                       "evidence": "湖北桦昌没货", "details": {"availability": "unavailable"}}, 3, at=NOW + 2000)
        past = ledger.at(NOW)
        self.assertEqual(past[0]["details"]["amount"], 110800)
        self.assertEqual(past[0]["status"], "active")
        self.assertEqual(ledger.current()[0]["status"], "unavailable")

    def test_different_products_and_delivery_terms_are_never_ranked_together(self):
        ledger = QuoteLedger()
        ingest(ledger, quote(delivery_max_days=1, delivery_basis="arrival"))
        ingest(ledger, quote(110750, delivery_max_days=3, delivery_basis="arrival"), 2)
        item = quote(24250, "铝投")
        item["项目"] = "铝杆"
        ingest(ledger, item, 3)
        result = compare_quotes(ledger.current(), {"destination": "宜兴", "as_of": NOW})
        self.assertEqual(len(result["groups"]), 3)


class TestMessageBoundaries(unittest.TestCase):
    def test_forward_uses_each_original_sender_time_and_skips_unavailable_images(self):
        content = '''<msg><appmsg><type>19</type><recorditem><![CDATA[<recordinfo><datalist>
        <dataitem datatype="1" dataid="a"><sourcename>甲</sourcename><sourcetime>2026-9-17 09:05</sourcetime><datadesc>铜杆110800</datadesc></dataitem>
        <dataitem datatype="2"><datadesc>图片</datadesc></dataitem>
        <dataitem datatype="1" dataid="b"><sourcename>乙</sourcename><sourcetime>2026-9-18 09:05</sourcetime><datadesc>铝杆24250</datadesc></dataitem>
        </datalist></recordinfo>]]></recorditem></appmsg></msg>'''
        units = analysis_units({"seq": 9, "msg": {"type": 49, "content": content, "createTime": NOW}})
        self.assertEqual(len(units), 2)
        self.assertEqual(units[0]["time"], timestamp("2026-09-17T09:05:00+08:00"))
        self.assertEqual(units[1]["sender"], "乙")

    def test_preview_is_explicitly_partial_and_has_no_original_time(self):
        units = analysis_units({"msg": {"type": 49, "content": "<appmsg><type>40</type><des>铜杆110800</des></appmsg>", "createTime": NOW}})
        self.assertTrue(units[0]["partial"])
        self.assertIsNone(units[0]["time"])


if __name__ == "__main__":
    unittest.main()
