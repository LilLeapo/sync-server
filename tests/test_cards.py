"""数据卡片：parse_card 入口检查、card_html 渲染、CardMaker 落盘与幂等、build_maker 配置。"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server import headless_chrome  # noqa: E402
from server.cards import (CARD_FIELDS, CardError, CardFailed, CardMaker, build_maker, card_html,  # noqa: E402
                          parse_card)
from server.store import Store  # noqa: E402
from server.tool_schema import S  # noqa: E402

SCHEMA = S.object(CARD_FIELDS, loose=True)
PNG = b"\x89PNG\r\n\x1a\nfake"
AT = 1792726800000  # 2026-10-23 11:40:00+08:00


def section(columns, rows, **extra):
    return {"columns": [c if isinstance(c, dict) else {"name": c} for c in columns], "rows": rows, **extra}


def card(*sections, **extra):
    return {"title": "电解铜报价", "sections": list(sections) or [section(["供应商"], [["富利"]])], **extra}


def parse(raw):
    return parse_card(SCHEMA(raw))


def problems(raw) -> str:
    try:
        parse(raw)
    except CardError as e:
        return str(e)
    raise AssertionError("CardError not raised")


class TestParseCard(unittest.TestCase):
    def test_happy_path_types_cells_by_column_kind(self):
        parsed = parse(card(section(
            ["供应商", {"name": "价格", "kind": "number", "unit": "元/吨"}, {"name": "涨跌", "kind": "change"},
             {"name": "时间", "kind": "time"}],
            [["富利铜业", 111200, -350, AT], ["湖北桦昌", "沪铜2611+550", "+0.32", None]], heading="一口价"),
            subtitle="送到宜兴", note="仅供参考"))
        [sec] = parsed.sections
        self.assertEqual((parsed.title, parsed.subtitle, parsed.note, sec.heading), ("电解铜报价", "送到宜兴", "仅供参考", "一口价"))
        self.assertEqual([(c.name, c.kind, c.unit) for c in sec.columns],
                         [("供应商", "text", ""), ("价格", "number", "元/吨"), ("涨跌", "change", ""), ("时间", "time", "")])
        self.assertEqual(sec.rows, (("富利铜业", Decimal(111200), Decimal(-350), AT),
                                    ("湖北桦昌", "沪铜2611+550", Decimal("0.32"), None)))

    def test_number_cells(self):
        [sec] = parse(card(section([{"name": "价", "kind": "number"}],
                                   [["111,200"], [110650.5], ["-350"], ["电议"], [1112000.0]]))).sections
        self.assertEqual([r[0] for r in sec.rows],
                         [Decimal(111200), Decimal("110650.5"), Decimal(-350), "电议", Decimal("1112000.0")])

    def test_text_cells_take_numbers_losslessly(self):
        [sec] = parse(card(section(["备注"], [[110650.5], [8], ["无氧杆 8mm"]]))).sections
        self.assertEqual([r[0] for r in sec.rows], ["110650.5", "8", "无氧杆 8mm"])

    def test_time_cells(self):
        [sec] = parse(card(section([{"name": "时间", "kind": "time"}], [
            [AT], ["2026-10-23T11:40:00+08:00"], ["2026-10-23T03:40:00Z"], ["2026-10-23 11:40"],
            ["2026-10-23T11:40:59.5+0800"], ["10-23 09:12"], ["今天上午"]]))).sections
        self.assertEqual([r[0] for r in sec.rows], [AT, AT, AT, AT, AT + 59000, "10-23 09:12", "今天上午"])

    def test_bad_time_numbers_and_bools_are_rejected(self):
        message = problems(card(section(["名", {"name": "时间", "kind": "time"}],
                                        [[True, 1792726800], ["x", "2026-02-30T10:00"]])))
        self.assertIn("第1节第1行「名」应填文字、数字或 null，收到 true/false", message)
        self.assertIn("第1节第1行「时间」应填毫秒时间戳或 ISO 时间", message)
        self.assertIn("第1节第2行「时间」不是有效的时间", message)

    def test_row_width_must_match_columns(self):
        self.assertIn("第1节第2行有 1 格，但 columns 有 2 列",
                      problems(card(section(["供应商", "品名"], [["a", "b"], ["c"]]))))

    def test_unknown_keys_at_every_level_name_allowed_keys(self):
        message = problems({**card(section([{"name": "价", "type": "number"}], [["1"]], style="dark")), "as_of": "x"})
        self.assertIn("卡片有未知字段 as_of，可用：title、subtitle、sections、note", message)
        self.assertIn("第1节有未知字段 style，可用：heading、columns、rows", message)
        self.assertIn("第1节第1列有未知字段 type，可用：name、kind、unit", message)

    def test_unknown_key_check_follows_the_schema(self):
        with mock.patch.dict(CARD_FIELDS, {"footer": S.string()}):
            self.assertEqual(parse_card(S.object(CARD_FIELDS, loose=True)(card(footer="x"))).title, "电解铜报价")

    def test_counts(self):
        many = [section(["名"], [[str(i)] for i in range(30)]), section(["名"], [[str(i)] for i in range(11)])]
        self.assertIn("全卡合计 41 行，最多 40 行", problems(card(*many)))
        self.assertEqual(sum(len(s.rows) for s in parse(card(many[0], section(["名"], [["x"]] * 10))).sections), 40)
        self.assertIn("sections 至少要有 1 节", problems({"title": "t", "sections": []}))
        self.assertIn("第1节没有 rows", problems(card(section(["名"], []))))
        self.assertIn("超过 40 字", problems(card(section(["名"], [["长" * 41]]))))

    def test_reports_several_problems_at_once_capped_at_eight(self):
        message = problems(card(section(["名"], [[True]] * 10)))
        self.assertEqual(message.count("收到 true/false"), 8)
        self.assertTrue(message.endswith("（另有 2 处）"), message)


class TestCardHtml(unittest.TestCase):
    def render(self, raw, made_ms=AT):
        return card_html(parse(raw), made_ms)

    def test_escapes_model_text_and_blocks_fetches(self):
        doc = self.render(card(section(["<b>名</b>"], [["<script>alert(1)</script>"]]), note="<img src=x>"))
        self.assertNotIn("<script>", doc)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", doc)
        self.assertNotIn("<img", doc)
        self.assertIn("default-src 'none'; style-src 'unsafe-inline'", doc)
        self.assertIn('<div id="card">', doc)

    def test_numbers_changes_nulls_and_times(self):
        doc = self.render(card(section(
            [{"name": "价格", "kind": "number", "unit": "元/吨"}, {"name": "涨跌", "kind": "change"},
             {"name": "时间", "kind": "time"}],
            [[110650, 300, AT], [110650.5, -100, "2026-10-23T03:40:00Z"], [None, 0, "收盘"]])))
        self.assertIn("价格（元/吨）", doc)
        self.assertIn(">110,650<", doc)
        self.assertIn(">110,650.5<", doc)
        self.assertRegex(doc, r'<td class="n up">\+300</td>')
        self.assertRegex(doc, r'<td class="n down">-100</td>')
        self.assertRegex(doc, r'<td class="n flat">0</td>')
        self.assertIn(">—<", doc)
        self.assertEqual(doc.count(">10-23 11:40<"), 2)
        self.assertIn(">收盘<", doc)

    def test_footer_width_and_determinism(self):
        doc = self.render(card(), made_ms=AT + 5000)
        self.assertIn("生成于 2026-10-23 11:40", doc)
        self.assertIn("#card{width:640px}", doc)
        self.assertEqual(doc, self.render(card(), made_ms=AT + 50000))
        self.assertIn("#card{width:900px}", self.render(card(section(list("abcdefg"), [list("1234567")]))))


class TestCardMaker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.renders = []
        self.now = AT / 1000

    def render(self, doc, element_id):
        self.renders.append((doc, element_id))
        return PNG

    def make(self, raw, render=None):
        return CardMaker(render or self.render, clock=lambda: self.now).make(SCHEMA(raw), self.root)

    def files(self):
        return [f for _, _, names in os.walk(self.root) for f in names]

    def test_writes_png_that_store_serves(self):
        made = self.make(card())
        self.assertRegex(made.rel, r"^images/cards/20261023/[0-9a-f]{20}\.png$")
        with open(Store(self.root).image_path(made.rel), "rb") as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(made.summary(), f"卡片 {made.rel}：1 行。")
        self.assertEqual(self.renders[0][1], "card")

    def test_same_card_same_minute_renders_once(self):
        first = self.make(card())
        self.now += 20
        self.assertEqual(self.make(card()), first)
        self.assertEqual(len(self.renders), 1)
        self.now += 60
        self.assertNotEqual(self.make(card()).rel, first.rel)
        self.assertEqual(len(self.renders), 2)

    def test_bad_card_is_card_error_before_rendering(self):
        with self.assertRaises(CardError):
            self.make(card(section(["a", "b"], [["x"]])))
        self.assertEqual(self.renders, [])

    def test_render_failure_leaves_no_file(self):
        def boom(doc, element_id):
            raise RuntimeError("超过 20 秒没有完成")
        with self.assertRaises(CardFailed) as cm:
            self.make(card(), boom)
        self.assertIn("超过 20 秒", str(cm.exception))
        self.assertEqual(self.files(), [])

    def test_failed_replace_leaves_no_tmp(self):
        with mock.patch("server.cards.os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(CardFailed) as cm:
                self.make(card())
        self.assertIn("保存卡片失败", str(cm.exception))
        self.assertEqual(self.files(), [], "neither the png nor its tmp file is left")


class TestBuildMaker(unittest.TestCase):
    def build(self, cfg, **kwargs):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            maker = build_maker(cfg, **kwargs)
        return maker, err.getvalue()

    def test_none_and_disabled(self):
        self.assertEqual(self.build(None), (None, ""))
        self.assertIsNone(self.build({"card": {"enabled": False}})[0])

    def test_bad_configured_path_does_not_fall_back(self):
        maker, log = self.build({"card": {"chrome": "/nonexistent/chrome"}}, which=lambda n: "/usr/bin/" + n)
        self.assertIsNone(maker)
        self.assertIn("[cards] card.chrome=/nonexistent/chrome", log)

    def test_auto_detect_uses_which(self):
        with mock.patch.object(headless_chrome, "MAC_APPS", ()):
            maker, log = self.build({}, which=lambda n: None)
            self.assertIsNone(maker)
            self.assertIn("没找到 Chrome", log)
            found = {"chromium": sys.executable}.get
            self.assertIsInstance(self.build({"card": {"timeout": 5}}, which=found)[0], CardMaker)


if __name__ == "__main__":
    unittest.main()
