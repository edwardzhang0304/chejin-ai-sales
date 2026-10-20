"""Fixed read templates and scoped response checks, no arbitrary SQL API.

Reading a bounded range does not prove all-history coverage or business order.
Durable cursors, overlap/recovery and native business dispatch are separate work.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import re
from dataclasses import dataclass
from pathlib import PureWindowsPath

from .hook_facts import ORDER_FIELDS
from .hook_inbox import HookReceiveScope
from .hook_outbox import HookDeliveryScope
from .shared_rules import hook_contract_rules as rules

HISTORY_FIELDS = (*ORDER_FIELDS, "MsgSvrID", "Type", "IsSender", "StrTalker", "StrContent")
INTEGER_FIELDS = (*ORDER_FIELDS, "MsgSvrID", "Type", "IsSender")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _decimal(value: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value):
        raise ValueError("HOOK_HISTORY_INTEGER_INVALID")
    result = int(value)
    return result


def _cursor(value: str) -> int:
    result = _decimal(value)
    if result > 2**63 - 1:
        raise ValueError("HOOK_HISTORY_CURSOR_INVALID")
    return result


def _data(response: dict) -> list:
    if (not isinstance(response, dict) or not isinstance(response.get("data"), list)
            or ("code" in response and (type(response["code"]) is not int or response["code"] != 0))):
        raise ValueError("HOOK_HISTORY_RESPONSE_UNCONFIRMED")
    return response["data"]


@dataclass(frozen=True)
class HookMessageDatabase:
    name: str
    path: str
    handle: str
    generation: str


@dataclass(frozen=True)
class HookHistoryCatalog:
    scope: HookReceiveScope
    databases: tuple[HookMessageDatabase, ...]
    fingerprint: str

    @classmethod
    def capture(cls, scope: HookReceiveScope, response: dict) -> HookHistoryCatalog:
        rows = _data(response)
        if not 0 < len(rows) <= 256:
            raise ValueError("HOOK_HISTORY_CATALOG_INVALID")
        databases, names = [], set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("dbpath"), str):
                raise ValueError("HOOK_HISTORY_CATALOG_INVALID")
            path = PureWindowsPath(row["dbpath"])
            if not re.fullmatch(r"MSG(?:0|[1-9][0-9]*)\.db", path.name):
                if re.fullmatch(r"msg[0-9]+\.db", path.name, re.IGNORECASE):
                    raise ValueError("HOOK_HISTORY_DATABASE_NAME_UNSUPPORTED")
                continue
            handle = row.get("dbhandle")
            if (not path.is_absolute() or path.drive.startswith("\\\\") or ".." in path.parts
                    or scope.self_wxid.casefold() not in [x.casefold() for x in path.parts]
                    or path.name in names or not isinstance(handle, str) or not 0 < len(handle) <= 128
                    or any(ord(c) < 32 or ord(c) == 127 for c in handle)):
                raise ValueError("HOOK_HISTORY_DATABASE_SCOPE_INVALID")
            names.add(path.name)
            normalized = ntpath.normcase(ntpath.normpath(str(path)))
            generation = _digest([scope.self_wxid, scope.account_epoch, scope.process_generation, normalized, handle])
            databases.append(HookMessageDatabase(path.name, str(path), handle, generation))
        if not databases:
            raise ValueError("HOOK_HISTORY_MESSAGE_DATABASE_MISSING")
        databases.sort(key=lambda db: int(db.name[3:-3]))
        fingerprint = _digest([[db.name, db.generation] for db in databases])
        return cls(scope, tuple(databases), fingerprint)

    def database(self, name: str) -> HookMessageDatabase:
        matches = [database for database in self.databases if database.name == name]
        if len(matches) != 1:
            raise ValueError("HOOK_HISTORY_DATABASE_NOT_IN_CATALOG")
        return matches[0]


@dataclass(frozen=True)
class HookHistoryQuery:
    database: HookMessageDatabase
    kind: str
    delivery_scope: HookDeliveryScope | None = None
    after_local_id: str = "0"
    upper_local_id: str | None = None
    page_size: int = 100
    since_create_time: str | None = None
    native_msg_id: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.database, HookMessageDatabase)
                or not re.fullmatch(r"MSG(?:0|[1-9][0-9]*)\.db", self.database.name)
                or not isinstance(self.kind, str) or self.kind not in {"schema", "bound", "page", "count", "boundary_meta"}
                or type(self.page_size) is not int or not 1 <= self.page_size <= 200):
            raise ValueError("HOOK_HISTORY_QUERY_INVALID")
        if self.since_create_time is not None:
            _decimal(self.since_create_time)
        if self.kind == "boundary_meta":
            if self.since_create_time is None or not isinstance(self.native_msg_id,str) or not _decimal(self.native_msg_id):
                raise ValueError("HOOK_HISTORY_PARENT_METADATA_SCOPE_REQUIRED")
        elif self.native_msg_id is not None:
            raise ValueError("HOOK_HISTORY_QUERY_INVALID")
        lower = _cursor(self.after_local_id)
        if self.kind != "schema" and not isinstance(self.delivery_scope, HookDeliveryScope):
            raise ValueError("HOOK_HISTORY_AUTHORIZED_PEER_REQUIRED")
        if self.kind in {"page", "count"}:
            if self.upper_local_id is None or _cursor(self.upper_local_id) < lower:
                raise ValueError("HOOK_HISTORY_QUERY_BOUND_INVALID")
        elif self.upper_local_id is not None or (self.kind == "schema" and (self.delivery_scope is not None or lower != 0)):
            raise ValueError("HOOK_HISTORY_QUERY_INVALID")

    def validate_scope(self, catalog: HookHistoryCatalog) -> None:
        if catalog.database(self.database.name) != self.database:
            raise ValueError("HOOK_HISTORY_DATABASE_GENERATION_CHANGED")
        if self.delivery_scope is not None:
            if (self.delivery_scope.self_wxid != catalog.scope.self_wxid
                    or self.delivery_scope.account_epoch != catalog.scope.account_epoch):
                raise ValueError("HOOK_HISTORY_AUTHORIZATION_SCOPE_MISMATCH")

    def payload(self) -> dict:
        if self.kind == "boundary_meta":
            sql = ("SELECT CAST(MsgSvrID AS TEXT) AS native_msg_id,CAST(CreateTime AS TEXT) AS create_time FROM MSG WHERE "
                f"StrTalker='{self.delivery_scope.peer_wxid}' AND CAST(MsgSvrID AS TEXT)='{_decimal(self.native_msg_id)}' "
                f"AND CreateTime<{_decimal(self.since_create_time)} LIMIT 1")
        elif self.kind == "schema":
            sql = "SELECT name,sql FROM sqlite_master WHERE type='table' AND name='MSG' LIMIT 1"
        else:
            # HookDeliveryScope validates the wxid; never quote arbitrary input.
            predicate = f"StrTalker='{self.delivery_scope.peer_wxid}' AND localId>{_decimal(self.after_local_id)}"
            if self.since_create_time is not None:
                predicate += f' AND CreateTime>={_decimal(self.since_create_time)}'
            if self.kind == "bound":
                sql = (f"SELECT CAST(COALESCE(MAX(localId),{_decimal(self.after_local_id)}) AS TEXT) AS upper_local_id, "
                    f"CAST(COUNT(*) AS TEXT) AS row_count FROM MSG WHERE {predicate}")
            elif self.kind == "count":
                sql = (f"SELECT CAST(COUNT(*) AS TEXT) AS row_count FROM MSG WHERE {predicate} "
                    f"AND localId<={_decimal(self.upper_local_id)}")
            else:
                selection = ",".join(f"CAST({field} AS TEXT) AS {field}" for field in INTEGER_FIELDS)
                sql = (f"SELECT {selection},StrTalker,StrContent FROM MSG WHERE {predicate} "
                    # Qualify the numeric source column: the output alias localId
                    # is TEXT for lossless transport and would sort lexically.
                    f"AND localId<={_decimal(self.upper_local_id)} ORDER BY MSG.localId ASC LIMIT {self.page_size}")
        return {"dbname": self.database.name, "sql": sql}

    def validate_response(self, response: dict) -> dict:
        if self.kind == "page" and response == {}:
            if self.after_local_id != self.upper_local_id:
                raise ValueError("HOOK_HISTORY_EMPTY_OBJECT_WITHOUT_TERMINAL_BOUND")
            return response
        rows = [] if self.kind == "boundary_meta" and response == {} else _data(response)
        if self.kind == "boundary_meta":
            if len(rows)>1:raise ValueError("HOOK_HISTORY_PARENT_METADATA_INVALID")
            for row in rows:
                if (not isinstance(row,dict) or set(row)!={'native_msg_id','create_time'}
                        or row['native_msg_id']!=self.native_msg_id
                        or _decimal(row['create_time'])>=_decimal(self.since_create_time)):
                    raise ValueError("HOOK_HISTORY_PARENT_METADATA_INVALID")
        elif self.kind == "schema":
            if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("name") != "MSG":
                raise ValueError("HOOK_HISTORY_SCHEMA_UNSUPPORTED")
            ddl = rows[0].get("sql")
            if (not isinstance(ddl, str) or not re.match(r"^\s*CREATE\s+TABLE\s+MSG\s*\(", ddl, re.I)
                    or any(not re.search(r"(?:\(|,)\s*" + field + r"\s+", ddl, re.I) for field in HISTORY_FIELDS)):
                raise ValueError("HOOK_HISTORY_SCHEMA_UNSUPPORTED")
        elif self.kind in {"count", "bound"}:
            expected = {"row_count", "upper_local_id"} if self.kind == "bound" else {"row_count"}
            if len(rows) != 1 or not isinstance(rows[0], dict) or set(rows[0]) != expected:
                raise ValueError("HOOK_HISTORY_AGGREGATE_INVALID")
            count = _decimal(rows[0]["row_count"])
            if self.kind == "bound":
                upper, lower = _cursor(rows[0]["upper_local_id"]), _cursor(self.after_local_id)
                if upper < lower or count > upper - lower or (count == 0) != (upper == lower):
                    raise ValueError("HOOK_HISTORY_AGGREGATE_INVALID")
        else:
            if len(rows) > self.page_size:
                raise ValueError("HOOK_HISTORY_PAGE_LIMIT_EXCEEDED")
            previous = _decimal(self.after_local_id)
            upper = _decimal(self.upper_local_id)
            for row in rows:
                if (not isinstance(row, dict) or set(row) != set(HISTORY_FIELDS)
                        or row.get("StrTalker") != self.delivery_scope.peer_wxid
                        or (row.get("StrContent") is not None and not isinstance(row.get("StrContent"), str))):
                    raise ValueError("HOOK_HISTORY_PAGE_SCOPE_INVALID")
                for field in INTEGER_FIELDS:
                    if row[field] is not None:
                        _decimal(row[field])
                if self.since_create_time is not None and _decimal(row['CreateTime'])<_decimal(self.since_create_time):
                    raise ValueError('HOOK_HISTORY_ROW_SCOPE_INVALID')
                local = _cursor(row["localId"])
                if not previous < local <= upper:
                    raise ValueError("HOOK_HISTORY_PAGE_CURSOR_INVALID")
                previous = local
            if not rows and _decimal(self.after_local_id) != upper:
                raise ValueError("HOOK_HISTORY_EMPTY_PAGE_BEFORE_BOUND")
        return response


@dataclass(frozen=True)
class HookHistoryCapture:
    catalog: HookHistoryCatalog
    query: HookHistoryQuery
    raw_body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.raw_body, bytes) or not 0 < len(self.raw_body) <= rules.MAX_BYTES:
            raise ValueError("HOOK_HISTORY_CAPTURE_SIZE_INVALID")
        self.query.validate_scope(self.catalog)
        self.query.validate_response(self.payload)

    @property
    def payload(self) -> dict:
        return rules.loads(self.raw_body.decode("utf-8"))
