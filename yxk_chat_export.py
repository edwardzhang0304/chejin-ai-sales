#!/usr/bin/env python3
"""
Export accessible YiXiaoKe WeChat chat records.

This script only uses the documented frontend APIs and requires a valid account
token (or login payload) with permission to read the requested employees/chats.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_BASE_URL = "https://call.yixiaoke.net"
DEFAULT_START = "2000-01-01 00:00:00"
PAGE_LIMIT = 100
CHAT_SOURCE_DETAIL_TYPE = 22


class ApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class WechatAccount:
    wechat_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Talker:
    id: Any
    wx_type: int
    source_detail_type: int
    kind: str
    raw: dict[str, Any]


class YxkClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        timeout: float,
        sleep_seconds: float,
        retries: int,
        retry_sleep: float,
        verbose: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.sleep_seconds = sleep_seconds
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.verbose = verbose

    def login(self, payload: dict[str, Any]) -> str:
        data = self.post("/yxk/frontend/auth/accesstoken", payload, auth=False)
        token = first_present(data, ("token", "accessToken", "access_token"))
        if not token:
            raise ApiError("Login succeeded but no token/accessToken field was found.")
        self.token = str(token)
        return self.token

    def login_with_app_credentials(self, app_id: str, app_secret: str) -> str:
        variants = [
            {"appId": app_id, "appSecret": app_secret},
            {"appid": app_id, "appSecret": app_secret},
            {"appId": app_id, "secret": app_secret},
            {"appid": app_id, "secret": app_secret},
        ]
        errors = []
        for payload in variants:
            try:
                return self.login(payload)
            except ApiError as exc:
                errors.append(str(exc))
        raise ApiError("Could not get token with APP credentials. Last error: " + errors[-1])

    def post(self, path: str, payload: dict[str, Any], auth: bool = True) -> dict[str, Any]:
        if auth and not self.token:
            raise ApiError("Missing token. Pass --token, set YXK_TOKEN, or use --login-json/--login-file.")

        url = self.base_url + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = str(self.token)

        if self.verbose:
            print(f"POST {path} {json.dumps(redact(payload), ensure_ascii=False)}", file=sys.stderr)

        last_error: Exception | None = None
        for attempt in range(1, self.retries + 2):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise ApiError(f"HTTP {exc.code} from {path}: {detail[:500]}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt <= self.retries:
                    print(
                        f"Network timeout/error calling {path}; retry {attempt}/{self.retries} after {self.retry_sleep:g}s",
                        file=sys.stderr,
                    )
                    time.sleep(self.retry_sleep)
                    continue
                raise ApiError(f"Network error calling {path}: {exc}") from exc
        else:
            raise ApiError(f"Network error calling {path}: {last_error}")

        if self.sleep_seconds > 0:
            time.sleep(self.sleep_seconds)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiError(f"Non-JSON response from {path}: {raw[:500]}") from exc

        code = data.get("code")
        if code not in (None, 0, 200, "0", "200"):
            message = data.get("msg") or data.get("message") or data.get("error") or ""
            raise ApiError(f"API error from {path}: code={code} message={message}")
        return data


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.lower() in {"password", "passwd", "pwd", "token", "authorization", "appsecret", "secret"}:
                out[key] = "***"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def unwrap_data(data: Any) -> Any:
    current = data
    for key in ("data", "result"):
        if isinstance(current, dict) and key in current:
            current = current[key]
    return current


def extract_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    root = unwrap_data(data)
    if isinstance(root, list):
        return [item for item in root if isinstance(item, dict)]
    if not isinstance(root, dict):
        return []

    for key in ("list", "records", "rows", "items", "data"):
        items = root.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]

    for value in root.values():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    return []


def first_present(data: Any, keys: Iterable[str]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = first_present(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = first_present(item, keys)
            if found not in (None, ""):
                return found
    return None


def parse_time(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    normalized = text.replace("T", " ")
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    normalized = normalized.split(".")[0]
    normalized = normalized[:19] if len(normalized) >= 19 else normalized
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(normalized, fmt)
            if fmt == "%Y-%m-%d":
                return parsed.replace(hour=0, minute=0, second=0)
            return parsed
        except ValueError:
            continue
    return None


def format_time(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def previous_second(value: str) -> str:
    parsed = parse_time(value)
    if not parsed:
        return value
    return format_time(parsed - dt.timedelta(seconds=1))


def in_time_range(send_time: Any, start: str, end: str) -> bool:
    parsed = parse_time(send_time)
    if not parsed:
        return True
    start_time = parse_time(start)
    end_time = parse_time(end)
    if start_time and parsed < start_time:
        return False
    if end_time and parsed > end_time:
        return False
    return True


def normalize_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def normalize_talker_id(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
        return stripped
    return value


def unique_key(message: dict[str, Any], wechat_id: str, talker_id: Any) -> str:
    explicit_id = first_present(message, ("id", "msgId", "msgid", "messageId", "clientMsgId"))
    if explicit_id:
        return f"id:{explicit_id}"
    fallback = [
        wechat_id,
        str(talker_id),
        str(message.get("sendTime", "")),
        str(message.get("sendType", "")),
        str(message.get("msgType", "")),
        str(message.get("name", "")),
        str(message.get("content", "")),
    ]
    return "hash:" + "\x1f".join(fallback)


def paged_query(
    client: YxkClient,
    path: str,
    payload: dict[str, Any],
    id_keys: tuple[str, ...],
    max_pages: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for page in range(1, max_pages + 1):
        page_payload = dict(payload)
        page_payload.update({"page": page, "limit": PAGE_LIMIT})
        data = client.post(path, page_payload)
        items = extract_list(data)
        if not items:
            break

        new_count = 0
        for item in items:
            item_id = first_present(item, id_keys) or json.dumps(item, sort_keys=True, ensure_ascii=False)
            item_id = str(item_id)
            if item_id in seen:
                continue
            seen.add(item_id)
            rows.append(item)
            new_count += 1

        if new_count == 0 or len(items) < PAGE_LIMIT:
            break

    return rows


def query_wechat_accounts(client: YxkClient, max_pages: int) -> list[WechatAccount]:
    rows = paged_query(
        client,
        "/yxk/frontend/wxhp/wxmg/wxmgas/query",
        {},
        ("csIdWechatId", "wechatId", "id"),
        max_pages,
    )
    accounts: list[WechatAccount] = []
    seen: set[str] = set()
    for row in rows:
        wechat_id = normalize_id(first_present(row, ("csIdWechatId", "wechatId")))
        if not wechat_id or wechat_id in seen:
            continue
        seen.add(wechat_id)
        accounts.append(WechatAccount(wechat_id=wechat_id, raw=row))
    return accounts


def query_talkers(client: YxkClient, wechat_id: str, max_pages: int) -> list[Talker]:
    configs = [
        ("friend", "/yxk/frontend/common/wx/fdquery", 0, 7),
        ("group", "/yxk/frontend/common/wx/fgquery", 2, 8),
    ]
    talkers: list[Talker] = []
    seen: set[tuple[int, str]] = set()

    for kind, path, wx_type, detail_type in configs:
        payload = {
            "sourceType": 1,
            "sourceDetailType": detail_type,
            "wechatId": wechat_id,
            "wxType": wx_type,
        }
        rows = paged_query(client, path, payload, ("id", "talker", "userName", "wxid"), max_pages)
        for row in rows:
            talker_id = normalize_talker_id(first_present(row, ("id", "talker", "userName", "wxid")))
            if not talker_id:
                continue
            key = (wx_type, str(talker_id))
            if key in seen:
                continue
            seen.add(key)
            talkers.append(
                Talker(
                    id=talker_id,
                    wx_type=wx_type,
                    source_detail_type=detail_type,
                    kind=kind,
                    raw=row,
                )
            )
    return talkers


def fetch_messages(
    client: YxkClient,
    wechat_id: str,
    talker: Talker,
    start: str,
    end: str,
    max_pages: int,
    source_detail_type: int,
    no_time_filter: bool,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    max_send_time = end

    for _ in range(max_pages):
        payload = {
            "sourceType": 1,
            "sourceDetailType": source_detail_type,
            "wechatId": wechat_id,
            "talker": talker.id,
            "wxType": talker.wx_type,
            "msgId": None,
            "sendTimes": [] if no_time_filter else [start, end],
            "keyWords": "",
            "maxSendTime": max_send_time if not no_time_filter or max_send_time != end else "",
            "minSendTime": "" if no_time_filter else start,
        }
        if not no_time_filter:
            payload.update({"page": 1, "limit": PAGE_LIMIT})
        data = client.post("/yxk/frontend/common/wx/fgmquery", payload)
        items = extract_list(data)
        if not items:
            break

        page_new = []
        send_times = []
        for item in items:
            key = unique_key(item, wechat_id, talker.id)
            if key in seen:
                continue
            seen.add(key)
            enriched = {
                "wechatId": wechat_id,
                "talker": talker.id,
                "talkerKind": talker.kind,
                "wxType": talker.wx_type,
                "sourceDetailType": source_detail_type,
                "message": item,
            }
            send_time = item.get("sendTime")
            if in_time_range(send_time, start, end):
                page_new.append(enriched)
            if parse_time(send_time):
                send_times.append(str(send_time))

        if not page_new:
            break

        messages.extend(page_new)
        if (not no_time_filter and len(items) < PAGE_LIMIT) or not send_times:
            break

        oldest = min(send_times, key=lambda value: parse_time(value) or dt.datetime.max)
        next_max = previous_second(oldest)
        if next_max == max_send_time or (parse_time(next_max) and parse_time(start) and parse_time(next_max) < parse_time(start)):
            break
        max_send_time = next_max

    messages.sort(
        key=lambda item: parse_time(item["message"].get("sendTime")) or dt.datetime.min
    )
    return messages


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    fields = [
        "wechatId",
        "talker",
        "talkerKind",
        "wxType",
        "sourceDetailType",
        "sendTime",
        "sendType",
        "msgType",
        "name",
        "content",
        "avatar",
    ]
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            msg = row.get("message", {})
            writer.writerow(
                {
                    "wechatId": row.get("wechatId"),
                    "talker": row.get("talker"),
                    "talkerKind": row.get("talkerKind"),
                    "wxType": row.get("wxType"),
                    "sourceDetailType": row.get("sourceDetailType"),
                    "sendTime": msg.get("sendTime"),
                    "sendType": msg.get("sendType"),
                    "msgType": msg.get("msgType"),
                    "name": msg.get("name"),
                    "content": msg.get("content"),
                    "avatar": msg.get("avatar"),
                }
            )
            count += 1
    return count


def load_json_arg(text: str | None, path: str | None) -> dict[str, Any] | None:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if text:
        return json.loads(text)
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export all accessible YiXiaoKe employee friend/group chat records."
    )
    parser.add_argument("--base-url", default=os.getenv("YXK_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--token", default=os.getenv("YXK_TOKEN"))
    parser.add_argument("--app-id", default=os.getenv("YXK_APP_ID"), help="Developer APP ID.")
    parser.add_argument("--app-secret", default=os.getenv("YXK_APP_SECRET"), help="Developer appSecret.")
    parser.add_argument("--login-json", help="JSON body for /yxk/frontend/auth/accesstoken.")
    parser.add_argument("--login-file", help="File containing login JSON body.")
    parser.add_argument("--wechat-id", action="append", help="Limit export to one employee wechatId. Repeatable.")
    parser.add_argument("--talker", action="append", help="Limit export/probe to one talker id. Repeatable.")
    parser.add_argument("--wx-type", type=int, default=0, help="wxType for --talker. 0 friend, 2 group.")
    parser.add_argument("--chat-source-detail-type", type=int, default=CHAT_SOURCE_DETAIL_TYPE)
    parser.add_argument("--start", default=os.getenv("YXK_START", DEFAULT_START))
    parser.add_argument("--end", default=os.getenv("YXK_END") or format_time(dt.datetime.now()))
    parser.add_argument("--no-time-filter", action="store_true", help="Match web payload: sendTimes=[], maxSendTime='', minSendTime=''.")
    parser.add_argument("--out-dir", default="yxk_chat_export")
    parser.add_argument("--format", choices=("jsonl", "csv", "both"), default="jsonl")
    parser.add_argument("--resume", action="store_true", help="Skip talkers whose per-chat JSONL file already has messages.")
    parser.add_argument("--max-pages", type=int, default=10000)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--sleep", type=float, default=0.2, help="Delay after every API request.")
    parser.add_argument("--retries", type=int, default=3, help="Retry transient network errors this many times.")
    parser.add_argument("--retry-sleep", type=float, default=3, help="Seconds to wait between retries.")
    parser.add_argument("--skip-errors", action="store_true", help="Log failed accounts/talkers and continue.")
    parser.add_argument("--probe", action="store_true", help="Only call fgmquery once and print the raw response.")
    parser.add_argument("--probe-scan", action="store_true", help="Scan talkers until one fgmquery response has messages.")
    parser.add_argument("--probe-limit", type=int, default=50, help="Max talkers to try per account in --probe-scan.")
    parser.add_argument("--probe-total-limit", type=int, default=100, help="Max total fgmquery calls in --probe-scan.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    chat_source_detail_type = args.chat_source_detail_type
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    login_payload = load_json_arg(args.login_json, args.login_file)
    client = YxkClient(args.base_url, args.token, args.timeout, args.sleep, args.retries, args.retry_sleep, args.verbose)
    if args.token:
        print("Using token from --token/YXK_TOKEN.", file=sys.stderr)
    elif login_payload:
        client.login(login_payload)
        print("Logged in successfully.", file=sys.stderr)
    elif args.app_id and args.app_secret:
        client.login_with_app_credentials(args.app_id, args.app_secret)
        print("Logged in successfully.", file=sys.stderr)

    if args.wechat_id:
        accounts = [WechatAccount(wechat_id=value, raw={}) for value in args.wechat_id]
    else:
        accounts = query_wechat_accounts(client, args.max_pages)

    if not accounts:
        raise ApiError("No employee wechat accounts found. Try --wechat-id or check account permissions.")

    write_jsonl(out_dir / "wechat_accounts.jsonl", (account.raw | {"wechatId": account.wechat_id} for account in accounts))
    print(f"Found {len(accounts)} employee wechat account(s).", file=sys.stderr)

    if args.probe or args.probe_scan:
        tried = 0
        matched = False
        probe_accounts = accounts if args.probe_scan else accounts[:1]
        samples: list[dict[str, Any]] = []
        for account in probe_accounts:
            if args.talker:
                talkers = [
                    Talker(
                        id=normalize_talker_id(value),
                        wx_type=args.wx_type,
                        source_detail_type=chat_source_detail_type,
                        kind="group" if args.wx_type == 2 else "friend",
                        raw={},
                    )
                    for value in args.talker
                ]
            else:
                talkers = query_talkers(client, account.wechat_id, args.max_pages)
            if not talkers:
                samples.append({"wechatId": account.wechat_id, "error": "no talkers"})
                continue
            for talker in talkers[: args.probe_limit]:
                tried += 1
                if tried > args.probe_total_limit:
                    print(json.dumps({"tried": tried - 1, "matched": matched, "samples": samples[:20]}, ensure_ascii=False, indent=2))
                    return 0
                print(
                    f"Probe {tried}/{args.probe_total_limit}: wechatId={account.wechat_id} {talker.kind} talker={talker.id}",
                    file=sys.stderr,
                    flush=True,
                )
                payload = {
                    "sourceType": 1,
                    "sourceDetailType": chat_source_detail_type,
                    "wechatId": account.wechat_id,
                    "talker": talker.id,
                    "wxType": talker.wx_type,
                    "msgId": None,
                    "sendTimes": [] if args.no_time_filter else [args.start, args.end],
                    "keyWords": "",
                    "maxSendTime": "" if args.no_time_filter else args.end,
                    "minSendTime": "" if args.no_time_filter else args.start,
                }
                if not args.no_time_filter:
                    payload.update({"page": 1, "limit": PAGE_LIMIT})
                response = client.post("/yxk/frontend/common/wx/fgmquery", payload)
                items = extract_list(response)
                sample = {
                    "payload": payload,
                    "count": unwrap_data(response).get("count") if isinstance(unwrap_data(response), dict) else None,
                    "listLength": len(items),
                    "response": response if items else {"code": response.get("code"), "msg": response.get("msg"), "data": unwrap_data(response)},
                }
                samples.append(sample)
                if items or not args.probe_scan:
                    matched = bool(items)
                    print(json.dumps({"tried": tried, "matched": matched, "sample": sample}, ensure_ascii=False, indent=2))
                    return 0
        print(json.dumps({"tried": tried, "matched": matched, "samples": samples[:20]}, ensure_ascii=False, indent=2))
        return 0

    total_talkers = 0
    total_messages = 0
    all_talkers: list[dict[str, Any]] = []
    all_messages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    for index, account in enumerate(accounts, start=1):
        print(f"[{index}/{len(accounts)}] Querying talkers for wechatId={account.wechat_id}", file=sys.stderr)
        if args.talker:
            talkers = [
                Talker(
                    id=normalize_talker_id(value),
                    wx_type=args.wx_type,
                    source_detail_type=chat_source_detail_type,
                    kind="group" if args.wx_type == 2 else "friend",
                    raw={},
                )
                for value in args.talker
            ]
        else:
            try:
                talkers = query_talkers(client, account.wechat_id, args.max_pages)
            except ApiError as exc:
                failures.append({"wechatId": account.wechat_id, "stage": "query_talkers", "error": str(exc)})
                if args.skip_errors:
                    print(f"  Skipping account after error: {exc}", file=sys.stderr)
                    continue
                raise
        total_talkers += len(talkers)
        for talker in talkers:
            all_talkers.append(
                {
                    "wechatId": account.wechat_id,
                    "talker": talker.id,
                    "kind": talker.kind,
                    "wxType": talker.wx_type,
                    "sourceDetailType": chat_source_detail_type,
                    "raw": talker.raw,
                }
            )

        for talker_index, talker in enumerate(talkers, start=1):
            stem = safe_filename(f"{account.wechat_id}_{talker.kind}_{talker.id}")
            jsonl_path = out_dir / f"{stem}.jsonl"
            if args.resume and jsonl_path.exists() and jsonl_path.stat().st_size > 0:
                existing_messages = read_jsonl(jsonl_path)
                total_messages += len(existing_messages)
                all_messages.extend(existing_messages)
                print(
                    f"  [{talker_index}/{len(talkers)}] Skipping existing {talker.kind} talker={talker.id} ({len(existing_messages)} messages)",
                    file=sys.stderr,
                )
                continue

            print(
                f"  [{talker_index}/{len(talkers)}] Fetching {talker.kind} talker={talker.id}",
                file=sys.stderr,
            )
            try:
                messages = fetch_messages(
                    client,
                    account.wechat_id,
                    talker,
                    args.start,
                    args.end,
                    args.max_pages,
                    chat_source_detail_type,
                    args.no_time_filter,
                )
            except ApiError as exc:
                failures.append(
                    {
                        "wechatId": account.wechat_id,
                        "talker": talker.id,
                        "kind": talker.kind,
                        "stage": "fetch_messages",
                        "error": str(exc),
                    }
                )
                if args.skip_errors:
                    print(f"    Skipping talker after error: {exc}", file=sys.stderr)
                    continue
                raise
            total_messages += len(messages)
            all_messages.extend(messages)

            if args.format in ("jsonl", "both"):
                write_jsonl(jsonl_path, messages)
            if args.format in ("csv", "both"):
                write_csv(out_dir / f"{stem}.csv", messages)

    write_jsonl(out_dir / "talkers.jsonl", all_talkers)
    if failures:
        write_jsonl(out_dir / "failures.jsonl", failures)
    if args.format in ("jsonl", "both"):
        write_jsonl(out_dir / "all_messages.jsonl", all_messages)
    if args.format in ("csv", "both"):
        write_csv(out_dir / "all_messages.csv", all_messages)

    summary = {
        "baseUrl": args.base_url,
        "start": args.start,
        "end": args.end,
        "accounts": len(accounts),
        "talkers": total_talkers,
        "messages": total_messages,
        "format": args.format,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)[:180]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
