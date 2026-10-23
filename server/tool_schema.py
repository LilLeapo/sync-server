#!/usr/bin/env python3
"""Agent 工具参数 schema —— 借鉴 Schemastery（shigma/schemastery）的写法。

    params = S.object({
        "keyword": S.string().description("检索关键词"),
        "limit": S.integer().min(1).max(30).default(10).description("最多返回条数"),
        "room": S.string().description("微信群 roomId 或群名"),
    })
    params({"limit": "50"})        # → {"limit": 30}
    params.to_json()               # → 发给模型的 JSON Schema

- 链式声明：default / required / description / min / max；S.enum 限定取值
- schema 可直接调用：校验并规范化（补默认值、数字字符串转数字、数字夹到 min/max），
  不合法抛 SchemaError，消息会作为工具结果回给模型，让它改参数重试
- to_json() 输出 JSON Schema；S.from_json() 把现成的 JSON Schema（如插件工具）转成 schema，
  所有工具都走同一套校验
- 与 Schemastery 的差别：数字参数接受数字字符串（模型常把 10 写成 "10"）；
  数字的 min/max 是夹取而不是报错；对象默认丢掉未声明的字段；没传又没默认值的
  字段（含对象/数组）视为缺省、不出现在结果里，而不是补成空容器
"""
from __future__ import annotations

import copy
import math

_MISSING = object()


class SchemaError(ValueError):
    pass


class Schema:
    type_name = ""

    def __init__(self):
        self._default = _MISSING
        self._required = False
        self._description = ""
        self._enum: list | None = None

    def _clone(self):
        return copy.copy(self)

    def default(self, value):
        out = self._clone()
        out._default = value
        return out

    def required(self, flag: bool = True):
        out = self._clone()
        out._required = flag
        return out

    def description(self, text: str):
        out = self._clone()
        out._description = text
        return out

    @property
    def is_required(self) -> bool:
        return self._required

    def __call__(self, value=None, path: str = "参数"):
        if value is None or (isinstance(value, str) and not value.strip() and self.type_name != "string"):
            if self._default is not _MISSING:
                return copy.deepcopy(self._default)
            if self._required:
                raise SchemaError(f"{path} 必填")
            return self._empty()
        out = self._check(value, path)
        if self._enum is not None and out not in self._enum:
            raise SchemaError(f"{path} 只能取 {', '.join(map(str, self._enum))}，收到 {value!r}")
        return out

    def _empty(self):
        return None

    def _check(self, value, path: str):
        return value

    def _json(self) -> dict:
        return {"type": self.type_name} if self.type_name else {}

    def to_json(self) -> dict:
        out = self._json()
        if self._enum is not None:
            out["enum"] = list(self._enum)
        if self._description:
            out["description"] = self._description
        if self._default is not _MISSING and self._default is not None:
            out["default"] = self._default
        return out


class String(Schema):
    type_name = "string"

    def __init__(self):
        super().__init__()
        self._max = None

    def max(self, n: int):
        out = self._clone()
        out._max = n
        return out

    def _check(self, value, path):
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise SchemaError(f"{path} 应为字符串，收到 {type(value).__name__}")
        if isinstance(value, float):  # 不丢精度：110650.5 → "110650.5"，1112000.0 → "1112000"，1e23 → "1e+23"
            text = str(int(value)) if value.is_integer() and abs(value) < 2 ** 53 else repr(value)
        else:
            text = str(value).strip()
        if self._max is not None and len(text) > self._max:
            raise SchemaError(f"{path} 不能超过 {self._max} 字")
        return text

    def _json(self):
        out = {"type": "string"}
        if self._max is not None:
            out["maxLength"] = self._max
        return out


class Number(Schema):
    type_name = "number"

    def __init__(self):
        super().__init__()
        self._min = None
        self._max = None

    def min(self, n):
        out = self._clone()
        out._min = n
        return out

    def max(self, n):
        out = self._clone()
        out._max = n
        return out

    def _number(self, value, path):
        if isinstance(value, bool):
            raise SchemaError(f"{path} 应为数字，收到布尔值")
        if isinstance(value, str):
            try:
                value = float(value.strip().replace(",", ""))
            except ValueError:
                raise SchemaError(f"{path} 应为数字，收到 {value!r}") from None
        if not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
            raise SchemaError(f"{path} 应为数字，收到 {value!r}")
        return value

    def _clamp(self, value):
        if self._min is not None and value < self._min:
            value = self._min
        if self._max is not None and value > self._max:
            value = self._max
        return value

    def _check(self, value, path):
        value = self._number(value, path)
        if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
            value = int(value) if self.type_name == "integer" else value
        return self._clamp(value)

    def _json(self):
        out = {"type": self.type_name}
        if self._min is not None:
            out["minimum"] = self._min
        if self._max is not None:
            out["maximum"] = self._max
        return out


class Integer(Number):
    type_name = "integer"

    def _check(self, value, path):
        value = self._number(value, path)
        if isinstance(value, float):
            if not value.is_integer():
                raise SchemaError(f"{path} 应为整数，收到 {value!r}")
            value = int(value)
        return self._clamp(value)


class Boolean(Schema):
    type_name = "boolean"

    def _check(self, value, path):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
            return value.strip().lower() in ("true", "1", "yes")
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise SchemaError(f"{path} 应为 true/false，收到 {value!r}")


class Array(Schema):
    type_name = "array"

    def __init__(self, item: Schema):
        super().__init__()
        self.item = item
        self._max = None

    def max(self, n: int):
        out = self._clone()
        out._max = n
        return out

    def _check(self, value, path):
        if not isinstance(value, (list, tuple)):
            value = [value]  # Schemastery 的 listable 写法：单个值也接受
        if self._max is not None and len(value) > self._max:
            raise SchemaError(f"{path} 最多 {self._max} 项")
        return [self.item(v, f"{path}[{i}]") for i, v in enumerate(value)]

    def _json(self):
        out = {"type": "array", "items": self.item.to_json()}
        if self._max is not None:
            out["maxItems"] = self._max
        return out


class Object(Schema):
    type_name = "object"

    def __init__(self, props: dict[str, Schema] | None = None, loose: bool = False):
        super().__init__()
        self.props = dict(props or {})
        self.loose = loose

    def _check(self, value, path):
        if not isinstance(value, dict):
            raise SchemaError(f"{path or '参数'} 应为对象，收到 {type(value).__name__}")
        out = dict(value) if self.loose else {}
        for key, schema in self.props.items():
            got = schema(value.get(key), f"{path}.{key}" if path else key)
            # 没传又没默认值的字段不写进结果：handler 里 params.get(k, 默认) 仍按「缺省」处理
            if got is None:
                out.pop(key, None)
            else:
                out[key] = got
        return out

    def _json(self):
        out = {"type": "object", "properties": {k: s.to_json() for k, s in self.props.items()}}
        required = [k for k, s in self.props.items() if s.is_required]
        if required:
            out["required"] = required
        return out


class Any(Schema):
    type_name = ""


class S:
    """工厂：S.string() / S.number() / S.integer() / S.boolean() / S.array(x) / S.object({...}) / S.enum([...])"""

    string = String
    number = Number
    integer = Integer
    boolean = Boolean
    array = Array
    object = Object
    any = Any

    @staticmethod
    def enum(values: list):
        schema = String() if all(isinstance(v, str) for v in values) else Any()
        schema._enum = list(values)
        return schema

    @staticmethod
    def from_json(js) -> Schema:
        """JSON Schema → Schema（覆盖常用子集：type/properties/required/items/enum/default/min/max）。"""
        if isinstance(js, Schema):
            return js
        if not isinstance(js, dict):
            return Any()
        kind = js.get("type")
        if "enum" in js and isinstance(js["enum"], list):
            schema = S.enum(js["enum"])
        elif kind == "object":
            required = set(js.get("required") or [])
            props = {}
            for key, sub in (js.get("properties") or {}).items():
                prop = S.from_json(sub)
                props[key] = prop.required() if key in required else prop
            schema = Object(props, loose=js.get("additionalProperties", True) is not False)
        elif kind == "string":
            schema = String()
            if isinstance(js.get("maxLength"), int):
                schema = schema.max(js["maxLength"])
        elif kind in ("number", "integer"):
            schema = Integer() if kind == "integer" else Number()
            if isinstance(js.get("minimum"), (int, float)):
                schema = schema.min(js["minimum"])
            if isinstance(js.get("maximum"), (int, float)):
                schema = schema.max(js["maximum"])
        elif kind == "boolean":
            schema = Boolean()
        elif kind == "array":
            schema = Array(S.from_json(js.get("items") or {}))
        else:
            schema = Any()
        if js.get("description"):
            schema = schema.description(str(js["description"]))
        if "default" in js:
            schema = schema.default(js["default"])
        return schema


def tool_schema(spec: dict) -> Schema:
    """工具规格里的参数 schema：优先 spec["schema"]，否则由 parameters（JSON Schema）转换。"""
    schema = spec.get("schema")
    if isinstance(schema, Schema):
        return schema
    return S.from_json(spec.get("parameters") or {"type": "object", "properties": {}})


def tool_parameters(spec: dict) -> dict:
    """发给模型的 JSON Schema。"""
    params = spec.get("parameters")
    if isinstance(params, dict):
        return params
    return tool_schema(spec).to_json()
