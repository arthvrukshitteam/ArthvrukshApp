"""Minimal async Mongo stand-in for endpoint tests: just the query
operators and cursor methods the code under test uses. No external
dependency, same spirit as test_universe_filtering's fake."""
from __future__ import annotations

import re
from typing import Any, Dict, List

_MISSING = object()


def _get(doc: Dict[str, Any], path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if isinstance(cur, list):
            vals = [x.get(part, _MISSING) for x in cur if isinstance(x, dict)]
            vals = [v for v in vals if v is not _MISSING]
            return vals if vals else _MISSING
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _cmp(value: Any, cond: Any) -> bool:
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, operand in cond.items():
            if op == "$in":
                if value is _MISSING:
                    if None not in operand:  # Mongo: {$in: [null]} matches a missing field
                        return False
                    continue
                vals = value if isinstance(value, list) else [value]
                if not any(v in operand for v in vals):
                    return False
            elif op == "$gte":
                if value is _MISSING or value is None or not value >= operand:
                    return False
            elif op == "$lt":
                if value is _MISSING or value is None or not value < operand:
                    return False
            elif op == "$ne":
                if value is not _MISSING and value == operand:
                    return False
            elif op == "$exists":
                if (value is not _MISSING) != bool(operand):
                    return False
            elif op == "$regex":
                flags = re.IGNORECASE if "i" in cond.get("$options", "") else 0
                if value is _MISSING or not re.search(operand, str(value), flags):
                    return False
            elif op == "$options":
                continue
            else:
                raise AssertionError(f"unhandled operator {op}")
        return True
    if isinstance(value, list):
        return cond in value
    return value == cond


def match(query: Dict[str, Any], doc: Dict[str, Any]) -> bool:
    for key, cond in (query or {}).items():
        if key == "$and":
            if not all(match(q, doc) for q in cond):
                return False
        elif key == "$or":
            if not any(match(q, doc) for q in cond):
                return False
        elif not _cmp(_get(doc, key), cond):
            return False
    return True


def project(doc: Dict[str, Any], projection: Dict[str, Any] | None) -> Dict[str, Any]:
    if not projection:
        return dict(doc)
    include = [k for k, v in projection.items() if v and k != "_id"]
    exclude = [k for k, v in projection.items() if not v]
    if include:
        return {k: doc[k] for k in include if k in doc}
    return {k: v for k, v in doc.items() if k not in exclude}


class Cursor:
    def __init__(self, docs: List[Dict[str, Any]]):
        self._docs = docs

    def sort(self, keys, direction=None):
        if isinstance(keys, str):
            keys = [(keys, direction or 1)]
        for field, d in reversed(keys):
            self._docs.sort(
                key=lambda x: (x.get(field) is None, x.get(field) if x.get(field) is not None else 0),
                reverse=(d == -1),
            )
            if d == -1:
                # Mongo puts missing/null LAST in a descending sort.
                nulls = [x for x in self._docs if x.get(field) is None]
                rest = [x for x in self._docs if x.get(field) is not None]
                self._docs = rest + nulls
        return self

    def skip(self, n):
        self._docs = self._docs[n:]
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    def batch_size(self, _n):
        return self

    def __aiter__(self):
        async def gen():
            for d in self._docs:
                yield d
        return gen()


class Collection:
    def __init__(self, name: str, docs=None):
        self.name = name
        self.docs: List[Dict[str, Any]] = list(docs or [])

    def find(self, query=None, projection=None):
        return Cursor([project(d, projection) for d in self.docs if match(query or {}, d)])

    async def find_one(self, query=None, projection=None):
        for d in self.docs:
            if match(query or {}, d):
                return project(d, projection)
        return None

    async def count_documents(self, query=None):
        return sum(1 for d in self.docs if match(query or {}, d))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if match(query, d):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new = {k: v for k, v in query.items() if not k.startswith("$")}
            new.update(update.get("$set", {}))
            self.docs.append(new)

    async def create_index(self, *_a, **_kw):
        return "ok"


class FakeDB:
    def __init__(self, **collections):
        self._colls = {name: Collection(name, docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = Collection(name)
        return self._colls[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]
