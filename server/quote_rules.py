"""Deterministic supplier event validation, replay, and comparison."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import re
import threading
from decimal import Decimal, InvalidOperation

from .quote_messages import CST, timestamp

LEGACY_FIELDS = ("供应商", "项目", "价格", "单位", "数量", "备注")
EVENT_TYPES = {"quote", "adjustment", "amendment", "rejection", "stock", "purchase"}
DIMENSIONS = ("brand", "diameter_mm", "resistance_max", "resistance_unit", "quality",
              "pickup_region", "destination", "delivery", "pricing", "benchmark", "contract",
              "currency", "unit", "tax", "invoice_type", "invoice_period", "payment",
              "pricing_party", "delivery_min_days", "delivery_max_days", "delivery_basis", "batch_id")
NUMBERS = {"amount", "premium", "processing_fee", "spot_premium", "freight", "pickup_discount",
           "delta", "resistance_max", "diameter_mm", "delivery_min_days", "delivery_max_days",
           "available_quantity", "benchmark_amount"}
ENUMS = {"pricing": {"fixed", "futures", "spot", "formula", "unknown"},
         "tax": {"included", "excluded", "unknown"},
         "delivery": {"pickup", "delivered", "unknown"},
         "availability": {"available", "unavailable", "unknown"},
         "delivery_basis": {"arrival", "dispatch", "pickup", "unknown"}}

EXTRACTION_CONTRACT = """
运行时输出契约（以此契约为准，Skill里的历史数值只作格式例子）：
仅输出JSON：{"events": [...]}，无相关事件为{"events": []}。
每个事件保留六个展示字段：供应商、项目、价格、单位、数量、备注；额外字段：
- event_type: quote / adjustment / amendment / rejection / stock / purchase。
- evidence: 本条待提取消息中的原文片段；certainty: explicit / context / uncertain。
- issues: 原文矛盾、疑似错字或无法确认的解释，使用字符串数组；即使数字明确也需标记疑点。
- target_id: 若为更新事件，可使用系统提供的已有quoteId；多个方案不能确定目标时留空。
- context_source_ids: 使用相邻消息推断时列出对应sourceId，certainty填context。
- details对象，未说明的字段省略或null，不能自动补含税、品牌、现货、元/吨或合约：
  brand, diameter_mm, quality（品质原文）, resistance_max（保证上限）, resistance_unit
  （原文1710类代码用quote_code；物理量保留实际单位，不擅自把171改成1710）, pickup_region,
  destination, delivery（pickup/delivered/unknown）, pricing（fixed/futures/spot/formula/unknown）,
  amount（一口价明确数值）, currency, unit, benchmark（如沪铜、沪铝、上海现货均价）, contract,
  premium（合约或现货均价的完整加减价，保留正负）, processing_fee, spot_premium（尚未确定时null）, freight,
  freight_destination（运费适用目的地）, pickup_discount（送到转自提明确减价）, tax
  （included/excluded/unknown）, invoice_type, invoice_period, payment, pricing_party,
  delivery_min_days, delivery_max_days, delivery_basis（arrival/dispatch/pickup/unknown）,
  available_quantity, quantity_unit, batch_id, availability（available/unavailable/unknown）, delta。
  quoted_at: 只有本条明确写了报价日期或时间才填写，使用带+08:00的ISO格式。
  benchmark_amount: 仅本条明确写了基准价格才填，可用于校验“均价+加价=一口价”的算术。
  quality_surcharge: {amount, resistance_max, resistance_unit}，仅用于明确“某保证上限加多少钱”。
- amendment的changes对象只能放本条明确补充/更正的六个展示字段或details字段。
只提取待提取消息；相邻消息、引用上下文和已有报价作为关联依据，不重复抽取。
一口价与点价、不同交期/发票/地区方案分别输出，不能挑一个价代替其他方案。
“1710左右”只写quality，不能写resistance_max；数字异常保留原文，不自行纠错。
“110100不出，最低110200”输出110100 rejection和110200 quote；不要把还价当报价。
无价的没货/出完/补供应商也要输出对应事件。adjustment的delta必须有明确正负。
formula保留未确定的项，不合并成已确定premium。运费、品质加价和减价的派生价格由程序算，
不要输出自行计算的固定价格。currency与unit只有明确原文或可追溯上下文时才填写。
"""


def number(value) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def numeric(value) -> float | None:
    result = number(value)
    return float(result) if result is not None and abs(result) < Decimal("1e15") else None


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()[:24]


def price_display(details: dict) -> str:
    if details.get("pricing") == "fixed" and number(details.get("amount")) is not None:
        return format(number(details["amount"]), "f")
    if details.get("pricing") in ("futures", "spot") and number(details.get("premium")) is not None:
        basis = details.get("contract") or details.get("benchmark") or "基准待确认"
        return str(basis) + format(number(details["premium"]), "+f")
    return ""


def price_in_evidence(amount, evidence: str) -> bool:
    compact = re.sub(r"[,，]", "", evidence)
    for match in re.finditer(r"(?<![\d.])([+-]?\d+(?:\.\d+)?)(万|亿)?(?![\d.])", compact):
        value = number(match.group(1))
        multiplier = {"万": Decimal(10000), "亿": Decimal(100000000)}.get(match.group(2), Decimal(1))
        if value * multiplier == number(amount):
            return True
    return False


def display_conditions(row: dict) -> str:
    details = row.get("details") or {}
    labels = (("brand", "品牌"), ("quality", "品质"), ("pickup_region", "提货"),
              ("destination", "送到"), ("invoice_type", "发票"), ("invoice_period", "开票"),
              ("payment", "付款"), ("pricing_party", "点价方"))
    parts = [label + ":" + str(details[key]) for key, label in labels if details.get(key)]
    if details.get("tax") in ("included", "excluded"):
        parts.append("含税" if details["tax"] == "included" else "不含税")
    if details.get("delivery") == "pickup":
        parts.append("自提")
    if details.get("resistance_max") is not None:
        parts.append("电阻上限:" + str(details["resistance_max"]) + ("" if details.get("resistance_unit") == "quote_code" else " " + str(details.get("resistance_unit") or "单位待确认")))
    if details.get("delivery_max_days") is not None:
        basis = {"arrival": "到货", "dispatch": "发货", "pickup": "提货"}.get(details.get("delivery_basis"), "交期口径待确认")
        parts.append(str(details["delivery_max_days"]) + "天 " + basis)
    if row.get("status") == "needs_confirmation":
        parts.append("待确认")
    if row.get("status") == "unavailable":
        parts.append("无可供货库存")
    if details.get("available_quantity") is not None:
        parts.append("可供:" + str(details["available_quantity"]) + str(details.get("quantity_unit") or "单位待确认"))
    return "；".join(parts)


def normalize_event(item: dict, source: dict, index: int) -> dict:
    warnings = ["reported:" + issue for issue in item.get("issues", []) if isinstance(issue, str)] if isinstance(item.get("issues"), list) else []
    kind = item.get("event_type") or "quote"
    if kind not in EVENT_TYPES:
        warnings.append("unknown_event_type")
        kind = "quote"
    raw_details = item.get("details") if isinstance(item.get("details"), dict) else {}
    details = {}
    allowed = set(DIMENSIONS) | NUMBERS | {"availability", "quantity_unit", "freight_destination", "quoted_at"}
    for key in allowed:
        value = raw_details.get(key)
        if key in NUMBERS:
            details[key] = numeric(value)
            if value not in (None, "") and details[key] is None:
                warnings.append("invalid_number:" + key)
        elif key in ENUMS:
            details[key] = value if value in ENUMS[key] else "unknown"
            if value not in (None, "", "unknown") and value not in ENUMS[key]:
                warnings.append("invalid_enum:" + key)
        else:
            details[key] = value.strip() if isinstance(value, str) else None
    legacy = {key: str(item.get(key) or "") for key in LEGACY_FIELDS}
    if details.get("unit") is None and legacy["单位"]:
        details["unit"] = legacy["单位"]
    # Older model responses remain displayable, but cannot supply unrecorded conditions.
    if "event_type" not in item:
        warnings.append("legacy_output")
        if re.fullmatch(r"\d+(?:\.\d+)?", legacy["价格"]):
            details["amount"] = numeric(legacy["价格"])
            details["pricing"] = "fixed"
    for key in ("amount", "pickup_discount", "freight", "available_quantity", "resistance_max",
                "diameter_mm", "delivery_min_days", "delivery_max_days"):
        if details.get(key) is not None and details[key] < 0:
            warnings.append("negative:" + key)
    if details.get("amount") == 0:
        warnings.append("zero_price")
    if details.get("pricing") == "fixed" and all(number(details.get(k)) is not None for k in ("amount", "benchmark_amount", "premium")):
        if number(details["amount"]) != number(details["benchmark_amount"]) + number(details["premium"]):
            warnings.append("price_expression_mismatch")
    if (details.get("delivery_min_days") is not None and details.get("delivery_max_days") is not None
            and details["delivery_min_days"] > details["delivery_max_days"]):
        warnings.append("invalid_delivery_range")
    evidence = item.get("evidence") if isinstance(item.get("evidence"), str) else ""
    if source.get("source") != "图片":
        if not evidence or evidence not in source.get("content", ""):
            warnings.append("evidence_missing_or_unmatched")
        amount = details.get("amount")
        if amount is not None and not price_in_evidence(amount, evidence):
            warnings.append("price_not_in_evidence")
    certainty = item.get("certainty") or "uncertain"
    if certainty != "explicit":
        warnings.append("context_requires_confirmation")
    if source.get("partial"):
        warnings.append("partial_source")
    effective_time = source.get("time")
    quoted_time = timestamp(details.get("quoted_at"))
    if details.get("quoted_at") and quoted_time is None:
        warnings.append("invalid_quoted_at")
    if quoted_time and effective_time:
        if dt.datetime.fromtimestamp(quoted_time / 1000, CST).date() != dt.datetime.fromtimestamp(effective_time / 1000, CST).date():
            warnings.append("time_conflict")
    elif quoted_time:
        effective_time = quoted_time
    if not effective_time:
        warnings.append("original_time_unknown")
    surcharge = raw_details.get("quality_surcharge")
    if isinstance(surcharge, dict):
        details["quality_surcharge"] = {"amount": numeric(surcharge.get("amount")),
                                        "resistance_max": numeric(surcharge.get("resistance_max")),
                                        "resistance_unit": surcharge.get("resistance_unit") if isinstance(surcharge.get("resistance_unit"), str) else None}
        cleaned = details["quality_surcharge"]
        if (cleaned["amount"] is None or cleaned["amount"] < 0 or cleaned["resistance_max"] is None
                or cleaned["resistance_max"] <= 0 or not cleaned["resistance_unit"]):
            warnings.append("invalid_quality_surcharge")
    if (details.get("pricing") == "fixed" and details.get("amount") is not None
            and details.get("pickup_discount") is not None and details["pickup_discount"] >= details["amount"]):
        warnings.append("invalid_pickup_discount")
    changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
    cleaned_changes = {}
    for key, value in changes.items():
        if key not in allowed | set(LEGACY_FIELDS):
            warnings.append("invalid_changes")
        elif key in NUMBERS and numeric(value) is not None:
            cleaned_changes[key] = numeric(value)
        elif key not in NUMBERS and isinstance(value, str):
            cleaned_changes[key] = value
        else:
            warnings.append("invalid_changes")
    changes = cleaned_changes
    origin = source.get("originalId")
    if source.get("part", "").startswith("forward:") and source.get("time"):
        origin = digest([source["time"], source.get("sender"), source.get("content")])
    identity = origin or [source.get("msgSvrId") or source.get("msgSeq"), source.get("part")]
    event = {**legacy, "schemaVersion": 2, "event_type": kind, "details": details,
             "evidence": evidence, "certainty": certainty, "target_id": item.get("target_id"),
             "changes": changes, "context_source_ids": item.get("context_source_ids") or [],
             "source": source, "effectiveTime": effective_time, "warnings": sorted(set(warnings))}
    # 每台设备各记一份事件；无设备的旧事件 id 不变，已落盘的 events.jsonl 仍幂等
    device = source.get("deviceId") or ""
    event["eventId"] = digest([source.get("roomId"), identity, index, event["event_type"],
                                legacy, details, evidence, changes, event["target_id"],
                                *([device] if device else [])])
    return event


def condition_key(row: dict):
    return (row.get("deviceId") or "", row.get("roomId"), row.get("供应商"), row.get("项目"),
            *(row.get("details", {}).get(key) for key in DIMENSIONS))


class QuoteLedger:
    """Append-only events; current quotes are a replayable in-memory projection."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.lock = threading.RLock()
        self.events: list[dict] = []
        self.quotes: dict[str, dict] = {}
        self.outcomes: dict[str, dict] = {}
        self.offset = 0
        self._file_identity = None
        self.refresh()

    def refresh(self):
        if not self.path or not os.path.exists(self.path):
            return
        with self.lock:
            stat = os.stat(self.path)
            identity = (stat.st_dev, stat.st_ino)
            if identity != self._file_identity or stat.st_size < self.offset:
                self.events.clear()
                self.quotes.clear()
                self.outcomes.clear()
                self.offset = 0
                self._file_identity = identity
            with open(self.path, "rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
            end = data.rfind(b"\n")
            if end < 0:
                return
            self.offset += end + 1
            for line in data[:end + 1].splitlines():
                event = json.loads(line)
                self._apply(event)

    def ingest(self, items: list[dict], source: dict) -> list[dict]:
        with self.lock:
            self.refresh()
            all_events = [normalize_event(it, source, i) for i, it in enumerate(items)]
            events = [event for event in all_events if event["eventId"] not in self.outcomes]
            if self.path and events:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as stream:
                    for event in events:
                        stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                # Replay what was persisted, including events written since the last read.
                self.refresh()
            else:
                for event in events:
                    self._apply(event)
            return [copy.deepcopy(row) for event in all_events for row in self.outcomes[event["eventId"]]["rows"]]

    def _row(self, event: dict) -> dict:
        source = event["source"]
        return {**{key: event[key] for key in LEGACY_FIELDS},
                "quoteId": event["eventId"], "eventId": event["eventId"], "schemaVersion": 2,
                "stockGroupId": event["eventId"],
                "details": copy.deepcopy(event["details"]), "evidence": event["evidence"],
                "warnings": list(event["warnings"]), "status": "needs_confirmation" if event["warnings"] else "active",
                "roomId": source.get("roomId"), "deviceId": source.get("deviceId") or "",
                "sender": source.get("sender"),
                "senderId": source.get("senderId"), "time": event["effectiveTime"],
                "receivedAt": source.get("receivedAt"), "source": source.get("source"),
                "msgSeq": source.get("msgSeq"), "imgFile": source.get("imgFile"),
                "updatedAt": event["effectiveTime"], "priceEvidence": event["evidence"],
                "provenance": copy.deepcopy(source), "calculation": []}

    def _invalidate_derivatives(self, quote_id: str):
        basis = self.quotes.get(quote_id) or {}
        for row in self.quotes.values():
            same_basis = (row.get("parentQuoteId") == quote_id or
                          (basis and not basis.get("parentQuoteId") and row.get("parentQuoteId")
                           and row.get("stockGroupId") == basis.get("stockGroupId")))
            if same_basis and row["status"] in ("active", "unavailable"):
                row["status"] = "needs_confirmation"
                row["warnings"].append("derived_basis_superseded")

    def _store(self, row: dict, state_only: bool = False):
        key = condition_key(row)
        for old in self.quotes.values():
            if old["status"] in ("active", "needs_confirmation", "unavailable") and condition_key(old) == key:
                if old.get("updatedAt") and row.get("updatedAt") and old["updatedAt"] > row["updatedAt"]:
                    row["status"] = "historical"
                elif not row["warnings"]:
                    old["status"] = "superseded"
                    if not state_only:
                        self._invalidate_derivatives(old["quoteId"])
        if row["details"].get("availability") == "unavailable" and row["status"] == "active":
            row["status"] = "unavailable"
        self.quotes[row["quoteId"]] = row

    def _targets(self, event: dict) -> list[dict]:
        result = []
        for row in self.quotes.values():
            allowed_states = {"active", "unavailable"} if event["event_type"] == "stock" else {"active"}
            if event["event_type"] == "amendment" and event.get("target_id"):
                allowed_states.add("needs_confirmation")
            if row["status"] not in allowed_states or row["roomId"] != event["source"].get("roomId"):
                continue
            if row["deviceId"] != (event["source"].get("deviceId") or ""):
                continue
            if event.get("target_id") and row["quoteId"] != event["target_id"]:
                continue
            if event["event_type"] == "amendment" and event.get("target_id"):
                result.append(row)
                continue
            if event["供应商"] and row["供应商"] != event["供应商"]:
                continue
            if event["项目"] and row["项目"] != event["项目"]:
                continue
            if any(event["details"].get(key) not in (None, "", "unknown")
                   and event["details"][key] != row["details"].get(key) for key in DIMENSIONS):
                continue
            if event["event_type"] == "rejection" and event["details"].get("amount") != row["details"].get("amount"):
                continue
            result.append(row)
        return result

    def _apply(self, event: dict):
        eid = event["eventId"]
        if eid in self.outcomes:
            return
        self.events.append(event)
        outcome = {"status": "recorded", "rows": [], "targetIds": [], "reasons": list(event["warnings"])}
        self.outcomes[eid] = outcome
        kind = event["event_type"]
        if kind == "quote":
            row = self._row(event)
            if not row["供应商"] or not row["项目"]:
                row["warnings"].append("supplier_or_product_unknown")
                row["status"] = "needs_confirmation"
            self._store(row)
            rows = [row]
            d = row["details"]
            discount = number(d.get("pickup_discount"))
            if not row["warnings"] and d.get("pricing") == "fixed" and d.get("delivery") == "delivered" and discount is not None and number(d.get("amount")) is not None:
                price = number(d["amount"]) - discount
                if price > 0:
                    derived = copy.deepcopy(row)
                    derived["quoteId"] = eid + ":pickup"
                    derived["parentQuoteId"] = eid
                    derived["details"].update(amount=float(price), delivery="pickup", destination=None, pickup_discount=None)
                    derived["价格"] = format(price, "f")
                    derived["calculation"] = [{"rule": "pickup_discount", "expression": f"{d['amount']} - {d['pickup_discount']}", "eventId": eid}]
                    self._store(derived)
                    rows.append(derived)
            surcharge = d.get("quality_surcharge")
            if not row["warnings"] and d.get("pricing") == "fixed" and isinstance(surcharge, dict):
                extra = number(surcharge.get("amount"))
                base = number(d.get("amount"))
                if base is not None and extra is not None and extra >= 0 and surcharge.get("resistance_max") and surcharge.get("resistance_unit"):
                    derived = copy.deepcopy(row)
                    derived["quoteId"] = eid + ":quality"
                    derived["parentQuoteId"] = eid
                    derived["details"].update(amount=float(base + extra), resistance_max=surcharge["resistance_max"],
                                              resistance_unit=surcharge["resistance_unit"], quality="", quality_surcharge=None)
                    derived["价格"] = format(base + extra, "f")
                    derived["calculation"] = [{"rule": "quality_surcharge", "expression": f"{base} + {extra}", "eventId": eid}]
                    self._store(derived)
                    rows.append(derived)
            outcome.update(status=row["status"], rows=rows)
            return
        if event["warnings"] or not (event["供应商"] or event.get("target_id")):
            outcome["status"] = "needs_confirmation"
            return
        targets = self._targets(event)
        if kind == "stock" and len(targets) > 1 and len({row.get("stockGroupId") for row in targets}) == 1:
            targets = targets[:1]
        if len(targets) != 1:
            outcome.update(status="needs_confirmation", reasons=outcome["reasons"] + ["target_not_unique"])
            return
        old = targets[0]
        outcome["targetIds"] = [old["quoteId"]]
        if event.get("effectiveTime") < (old.get("updatedAt") or old.get("time") or 0):
            outcome.update(status="historical", reasons=["older_than_target"])
            return
        if kind == "rejection":
            old["status"] = "rejected"
            self._invalidate_derivatives(old["quoteId"])
            outcome["status"] = "applied"
            return
        updated = copy.deepcopy(old)
        updated.update(quoteId=eid, eventId=eid, updatedAt=event["effectiveTime"], evidence=event["evidence"],
                       provenance=copy.deepcopy(event["source"]),
                       calculation=[{"rule": kind, "previousQuoteId": old["quoteId"], "eventId": eid}], status="active")
        d = updated["details"]
        if kind == "adjustment":
            field = "amount" if d.get("pricing") == "fixed" else "premium"
            delta = number(event["details"].get("delta"))
            previous = number(d.get(field))
            if delta is None or previous is None or (field == "amount" and previous + delta <= 0):
                outcome.update(status="needs_confirmation", reasons=["adjustment_basis_unknown"])
                return
            d[field] = float(previous + delta)
            updated["time"] = event["effectiveTime"]
            updated["priceEvidence"] = event["evidence"]
            updated["价格"] = price_display(d)
            updated["calculation"] = [{"rule": "adjustment", "previousQuoteId": old["quoteId"], "delta": float(delta)}]
        elif kind == "stock":
            for key in ("availability", "available_quantity", "quantity_unit"):
                if event["details"].get(key) not in (None, "unknown"):
                    d[key] = event["details"][key]
            if all(event["details"].get(k) in (None, "unknown") for k in ("availability", "available_quantity")):
                outcome.update(status="needs_confirmation", reasons=["stock_change_unknown"])
                return
        elif kind == "purchase":
            amount = number(event["details"].get("available_quantity"))
            stock = number(d.get("available_quantity"))
            if (not (event.get("target_id") or event["details"].get("batch_id")) or amount is None or stock is None
                    or amount <= 0 or amount > stock or not d.get("quantity_unit")
                    or d["quantity_unit"] != event["details"].get("quantity_unit")):
                outcome.update(status="needs_confirmation", reasons=["purchase_batch_or_quantity_unknown"])
                return
            d["available_quantity"] = float(stock - amount)
            if stock == amount:
                d["availability"] = "unavailable"
        elif kind == "amendment":
            if not event["changes"]:
                outcome.update(status="needs_confirmation", reasons=["empty_amendment"])
                return
            for key, value in event["changes"].items():
                if key in LEGACY_FIELDS:
                    updated[key] = str(value or "")
                else:
                    d[key] = numeric(value) if key in NUMBERS else value
            if any(d.get(k) is not None and d[k] < 0 for k in ("amount", "freight", "pickup_discount", "available_quantity", "resistance_max", "diameter_mm", "delivery_min_days", "delivery_max_days")) or d.get("amount") == 0:
                outcome.update(status="needs_confirmation", reasons=["invalid_amendment"])
                return
            if "amount" in event["changes"] or "premium" in event["changes"]:
                updated["time"] = event["effectiveTime"]
                updated["priceEvidence"] = event["evidence"]
                updated["价格"] = price_display(d)
            if any(key in ENUMS and value not in ENUMS[key] for key, value in d.items()):
                outcome.update(status="needs_confirmation", reasons=["invalid_amendment"])
                return
            if updated["供应商"] and updated["项目"]:
                updated["warnings"] = [warning for warning in updated["warnings"] if warning != "supplier_or_product_unknown"]
            elif "supplier_or_product_unknown" not in updated["warnings"]:
                updated["warnings"].append("supplier_or_product_unknown")
            if updated["warnings"]:
                updated["status"] = "needs_confirmation"
        old["status"] = "superseded"
        state_only = kind in ("stock", "purchase")
        if not state_only:
            self._invalidate_derivatives(old["quoteId"])
        self._store(updated, state_only=state_only)
        rows = [updated]
        # Derived delivery/quality choices represent the same stock, not extra inventory.
        if state_only:
            siblings = [row for row in self.quotes.values() if row["quoteId"] != updated["quoteId"]
                        and row.get("stockGroupId") == old.get("stockGroupId")
                        and row["status"] in ("active", "unavailable")]
            for i, sibling in enumerate(siblings):
                sibling["status"] = "superseded"
                revised = copy.deepcopy(sibling)
                revised.update(quoteId=f"{eid}:stock:{i}", eventId=eid, status="active", updatedAt=event["effectiveTime"],
                               evidence=event["evidence"], provenance=copy.deepcopy(event["source"]))
                for field in ("availability", "available_quantity", "quantity_unit"):
                    revised["details"][field] = d.get(field)
                revised["calculation"] = [{"rule": kind, "previousQuoteId": sibling["quoteId"], "eventId": eid}]
                self._store(revised, state_only=True)
                rows.append(revised)
                outcome["targetIds"].append(sibling["quoteId"])
        outcome.update(status="applied", rows=rows)

    def current(self, room: str | None = None, device: str | None = None) -> list[dict]:
        """device: None 不筛选；"" 只看无设备标识的旧数据（与 Store 的约定一致）。"""
        with self.lock:
            self.refresh()
            return copy.deepcopy([row for row in self.quotes.values()
                                  if row["status"] in ("active", "needs_confirmation", "unavailable") and (not room or row["roomId"] == room)
                                  and (device is None or row["deviceId"] == device)])

    def at(self, as_of: int, room: str | None = None, device: str | None = None) -> list[dict]:
        """Reconstruct original-time state instead of filtering today's projection."""
        with self.lock:
            self.refresh()
            events = copy.deepcopy(self.events)
        snapshot = QuoteLedger()
        for event in events:
            at = event.get("effectiveTime") or event["source"].get("receivedAt")
            if at and at <= as_of:
                snapshot._apply(event)
        return snapshot.current(room, device)

    def history(self, supplier: str = "", product: str = "", room: str | None = None, limit: int = 30,
                device: str | None = None) -> list[dict]:
        with self.lock:
            self.refresh()
            return [{**copy.deepcopy(event), "outcome": copy.deepcopy(self.outcomes[event["eventId"]])}
                    for event in reversed(self.events)
                    if (not supplier or supplier == event["供应商"])
                    and (not product or product in event["项目"])
                    and (not room or room == event["source"].get("roomId"))
                    and (device is None or device == (event["source"].get("deviceId") or ""))][:limit]


def compare_quotes(rows: list[dict], requirements: dict) -> dict:
    """Only rank independently priced, condition-compatible quotes within a group."""
    result = {"groups": [], "needs_confirmation": [], "excluded": []}
    for key in ("product", "destination", "pickup_region", "tax", "invoice_period", "invoice_type", "brand", "payment", "pricing_party", "resistance_unit", "quantity_unit", "currency", "unit"):
        if requirements.get(key) is not None and not isinstance(requirements[key], str):
            return {"error": key + "必须为字符串"}
    for key in ("resistance_max", "diameter_mm", "max_delivery_days", "quantity"):
        if requirements.get(key) is not None and (number(requirements[key]) is None or number(requirements[key]) < 0):
            return {"error": key + "必须为非负数值"}
    if requirements.get("tax") not in (None, "included", "excluded"):
        return {"error": "tax必须为included或excluded"}
    if requirements.get("destination") and requirements.get("pickup_region"):
        return {"error": "目的地与自提地点请选择一种比较口径"}
    as_of = timestamp(requirements.get("as_of")) if requirements.get("as_of") else int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    if not as_of:
        return {"error": "as_of日期无效"}
    age = number(requirements.get("max_age_hours", 24))
    if age is None or age <= 0 or age > 720:
        return {"error": "max_age_hours需为0到720之间的数值"}
    groups = {}
    for row in rows:
        if requirements.get("product") and requirements["product"] not in row.get("项目", ""):
            continue
        d = row.get("details", {})
        missing, excluded = list(row.get("warnings") or []), []
        if row.get("status") != "active":
            missing.append("报价未确认或已失效")
        if d.get("availability") == "unavailable":
            excluded.append("明确无可供货库存")
        if not row.get("time") or row["time"] > as_of or as_of - row["time"] > float(age) * 3600000:
            missing.append("报价时间未知、晚于比较时点或需要重新询价")
        for requested, field in (("tax", "tax"), ("invoice_period", "invoice_period"),
                                 ("invoice_type", "invoice_type"), ("brand", "brand"),
                                 ("payment", "payment"), ("diameter_mm", "diameter_mm"),
                                 ("pricing_party", "pricing_party")):
            value = requirements.get(requested)
            if value is not None:
                if d.get(field) in (None, "", "unknown"):
                    missing.append(field + "未明确")
                elif d[field] != value:
                    excluded.append(field + "不符合要求")
        resistance = number(requirements.get("resistance_max"))
        if requirements.get("resistance_max") is not None and resistance is None:
            return {"error": "resistance_max数值无效"}
        if resistance is not None:
            actual = number(d.get("resistance_max"))
            unit = requirements.get("resistance_unit") or "quote_code"
            if actual is None or d.get("resistance_unit") != unit:
                missing.append("电阻保证上限或单位未确认")
            elif actual > resistance:
                excluded.append("电阻不符合要求")
        days = number(requirements.get("max_delivery_days"))
        if days is not None:
            if number(d.get("delivery_max_days")) is None or d.get("delivery_basis") != "arrival":
                missing.append("到货期限未确认")
            elif number(d["delivery_max_days"]) > days:
                excluded.append("到货期限不符合要求")
        quantity = number(requirements.get("quantity"))
        if quantity is not None:
            if number(d.get("available_quantity")) is None or not requirements.get("quantity_unit") or d.get("quantity_unit") != requirements["quantity_unit"]:
                missing.append("可供数量或数量单位未确认")
            elif number(d["available_quantity"]) < quantity:
                excluded.append("可供数量不足")
        if d.get("availability") != "available":
            missing.append("可供货状态未确认")
        if d.get("currency") != requirements.get("currency", "CNY") or d.get("unit") != requirements.get("unit", "元/吨"):
            missing.append("币种或价格单位不一致或未明确")
        if d.get("tax") in (None, "unknown"):
            missing.append("税务口径未明确")
        price = number(d.get("amount")) if d.get("pricing") == "fixed" else None
        calculations = list(row.get("calculation") or [])
        if d.get("pricing") in ("futures", "spot", "formula"):
            benchmark = requirements.get("benchmark") or {}
            if not isinstance(benchmark, dict):
                return {"error": "benchmark必须为对象"}
            at = timestamp(benchmark.get("at"))
            base = number(benchmark.get("price"))
            if (not isinstance(benchmark, dict) or not d.get("benchmark") or benchmark.get("name") != d["benchmark"]
                    or benchmark.get("contract") != d.get("contract") or not benchmark.get("source")
                    or not at or at > as_of or as_of - at > 300000 or base is None):
                missing.append("缺少同基准同合约、五分钟内且有来源的价格，不能换算")
            else:
                components = ("processing_fee", "spot_premium") if d["pricing"] == "formula" else ("premium",)
                if any(number(d.get(k)) is None for k in components):
                    missing.append("价格表达式仍有未确定项")
                else:
                    price = base + sum((number(d[k]) for k in components), Decimal(0))
                    calculations.append({"rule": "benchmark_conversion", "benchmark": benchmark,
                                         "components": {k: d[k] for k in components}})
        destination = requirements.get("destination")
        pickup = requirements.get("pickup_region")
        if destination:
            if d.get("delivery") == "delivered":
                if d.get("destination") != destination:
                    missing.append("送到价适用目的地未确认")
            elif d.get("delivery") == "pickup" and d.get("freight_destination") == destination and number(d.get("freight")) is not None:
                if price is not None:
                    price += number(d["freight"])
                    calculations.append({"rule": "freight", "amount": d["freight"], "destination": destination})
            else:
                missing.append("缺少到目的地的适用运费")
        elif pickup:
            if d.get("delivery") != "pickup" or d.get("pickup_region") != pickup:
                missing.append("自提地点未确认或不匹配")
        else:
            missing.append("需指定目的地或自提地点")
        if price is None or price <= 0:
            missing.append("没有可计算的确定单价")
        candidate = {"quoteId": row.get("quoteId"), "供应商": row.get("供应商"), "项目": row.get("项目"),
                     "price": float(price) if price is not None else None, "details": d,
                     "evidence": row.get("evidence"), "priceEvidence": row.get("priceEvidence"), "provenance": row.get("provenance"),
                     "calculation": calculations, "reasons": sorted(set(excluded or missing))}
        if excluded:
            result["excluded"].append(candidate)
        elif missing:
            result["needs_confirmation"].append(candidate)
        else:
            keys = ("brand", "diameter_mm", "resistance_max", "resistance_unit", "quality", "tax",
                    "invoice_type", "invoice_period", "payment", "pricing_party", "pricing",
                    "delivery_min_days", "delivery_max_days", "delivery_basis", "currency", "unit")
            conditions = {"product": row.get("项目"), **{key: d.get(key) for key in keys}}
            group = groups.setdefault(json.dumps(conditions, sort_keys=True), {"conditions": conditions, "quotes": []})
            group["quotes"].append(candidate)
    for group in groups.values():
        group["quotes"].sort(key=lambda item: item["price"])
        result["groups"].append(group)
    result["note"] = "仅组内价格同口径；不同品质、税票、付款或点价条件的组不能直接排优劣。未设要求的条件仍需采购方确认。"
    return result
