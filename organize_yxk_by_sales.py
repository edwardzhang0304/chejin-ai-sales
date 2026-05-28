#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SUMMARY_FILES = {
    "wechat_accounts.jsonl",
    "talkers.jsonl",
    "all_messages.jsonl",
    "failures.jsonl",
}

MSG_TYPE_LABELS = {
    "1": "文本",
    "3": "图片",
    "34": "语音",
    "47": "表情",
    "49": "链接/卡片",
    "10000": "系统消息",
    "1090519089": "文件",
}

SEND_TYPE_LABELS = {
    0: "对方发来",
    1: "销售发出",
    2: "系统消息",
    "0": "对方发来",
    "1": "销售发出",
    "2": "系统消息",
}


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    return count


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)[:160] or "unknown"


def load_accounts(export_dir: Path) -> dict[str, dict[str, Any]]:
    path = export_dir / "wechat_accounts.jsonl"
    accounts: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return accounts
    for row in read_jsonl(path):
        wechat_id = str(row.get("wechatId") or row.get("csIdWechatId") or "")
        if wechat_id:
            accounts[wechat_id] = row
    return accounts


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)

    parts = []
    for key in ("title", "desc", "file_name", "file_path", "url", "voice_text", "file_type_txt"):
        value = content.get(key)
        if value not in (None, ""):
            parts.append(str(value))

    other_files = content.get("other_file")
    if isinstance(other_files, list):
        for item in other_files:
            if isinstance(item, dict):
                name = item.get("file_name") or ""
                path = item.get("file_path") or ""
                if name or path:
                    parts.append("附件: " + " ".join(part for part in (name, path) if part))

    return "\n".join(parts) if parts else json.dumps(content, ensure_ascii=False)


def row_sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("销售姓名", "")),
        str(row.get("发送时间", "")),
        str(row.get("消息ID", "")),
    )


def normalize_row(raw: dict[str, Any], accounts: dict[str, dict[str, Any]], source_file: Path) -> dict[str, Any]:
    wechat_id = str(raw.get("wechatId") or "")
    account = accounts.get(wechat_id, {})
    msg = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    send_type = msg.get("sendType")
    msg_type = str(msg.get("msgType", ""))
    sender_name = msg.get("name") or ""
    employee_name = account.get("csIdName") or ""
    employee_wechat_name = account.get("csIdWechatName") or ""
    department = account.get("cdIdName") or ""

    if send_type in (1, "1"):
        direction = "销售 -> 对方"
        talker_name = ""
    elif send_type in (0, "0"):
        direction = "对方 -> 销售"
        talker_name = sender_name
    else:
        direction = "系统"
        talker_name = sender_name

    content = msg.get("content")
    return {
        "销售姓名": employee_name,
        "部门": department,
        "员工微信昵称": employee_wechat_name,
        "员工微信ID": wechat_id,
        "会话类型": raw.get("talkerKind") or "",
        "对方ID": raw.get("talker"),
        "对方昵称": talker_name,
        "方向": direction,
        "发送方": sender_name,
        "发送类型": SEND_TYPE_LABELS.get(send_type, str(send_type)),
        "发送时间": msg.get("sendTime") or "",
        "消息类型": MSG_TYPE_LABELS.get(msg_type, msg_type),
        "msgType": msg_type,
        "消息ID": msg.get("id") or "",
        "内容": content_text(content),
        "内容JSON": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
        "头像": msg.get("avatar") or "",
        "源文件": source_file.name,
        "_raw": raw,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "销售姓名",
        "部门",
        "员工微信昵称",
        "员工微信ID",
        "会话类型",
        "对方ID",
        "对方昵称",
        "方向",
        "发送方",
        "发送类型",
        "发送时间",
        "消息类型",
        "msgType",
        "消息ID",
        "内容",
        "内容JSON",
        "头像",
        "源文件",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Organize YiXiaoKe exported chat JSONL files by salesperson.")
    parser.add_argument("--in-dir", default="yxk_chat_export_2026")
    parser.add_argument("--out-dir", default="yxk_chat_by_sales")
    parser.add_argument("--write-jsonl", action="store_true", help="Also write normalized JSONL files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir)
    per_sales_dir = out_dir / "按销售"
    per_sales_dir.mkdir(parents=True, exist_ok=True)

    accounts = load_accounts(in_dir)
    rows: list[dict[str, Any]] = []
    seen_ids: set[tuple[str, str, str]] = set()

    for path in sorted(in_dir.glob("*.jsonl")):
        if path.name in SUMMARY_FILES or path.stat().st_size == 0:
            continue
        for raw in read_jsonl(path):
            if not isinstance(raw, dict) or "message" not in raw:
                continue
            msg = raw.get("message") if isinstance(raw.get("message"), dict) else {}
            dedupe_key = (
                str(raw.get("wechatId") or ""),
                str(raw.get("talker") or ""),
                str(msg.get("id") or msg.get("sendTime") or json.dumps(msg, sort_keys=True, ensure_ascii=False)),
            )
            if dedupe_key in seen_ids:
                continue
            seen_ids.add(dedupe_key)
            rows.append(normalize_row(raw, accounts, path))

    rows.sort(key=row_sort_key)
    write_csv(out_dir / "all_sales_messages.csv", rows)
    if args.write_jsonl:
        write_jsonl(out_dir / "all_sales_messages.jsonl", ({k: v for k, v in row.items() if k != "_raw"} for row in rows))

    by_sales: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        sales_name = str(row.get("销售姓名") or "未知销售")
        by_sales[sales_name].append(row)

    summary_rows = []
    for sales_name, sales_rows in sorted(by_sales.items()):
        sales_rows.sort(key=row_sort_key)
        account_ids = sorted({str(row.get("员工微信ID") or "") for row in sales_rows if row.get("员工微信ID")})
        filename_base = safe_filename(f"{sales_name}_{len(sales_rows)}条")
        write_csv(per_sales_dir / f"{filename_base}.csv", sales_rows)
        if args.write_jsonl:
            write_jsonl(
                per_sales_dir / f"{filename_base}.jsonl",
                ({k: v for k, v in row.items() if k != "_raw"} for row in sales_rows),
            )
        summary_rows.append(
            {
                "销售姓名": sales_name,
                "消息数": len(sales_rows),
                "员工微信ID数": len(account_ids),
                "员工微信ID": " / ".join(account_ids),
                "文件": f"按销售/{filename_base}.csv",
            }
        )

    with (out_dir / "summary_by_sales.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        fields = ["销售姓名", "消息数", "员工微信ID数", "员工微信ID", "文件"]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)

    print(json.dumps({"messages": len(rows), "salespeople": len(by_sales), "outDir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
