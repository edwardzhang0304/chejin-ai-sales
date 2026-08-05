#!/usr/bin/env python3
import argparse
import getpass
import sys

from app.core.database import SessionLocal
from app.core.request_id import new_request_id
from app.errors import AppError
from app.services import auth_service


def _password_from_input(use_stdin: bool) -> str:
    if use_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise AppError("ADMIN_PASSWORD_EMPTY", "标准输入中没有密码", 400)
        return password
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise AppError("ADMIN_PASSWORD_CONFIRM_MISMATCH", "两次输入的密码不一致", 400)
    return first


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Chejin backend admin accounts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an enabled account")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true")

    reset = subparsers.add_parser("reset-password", help="Reset password and revoke all sessions")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password-stdin", action="store_true")

    for command in ("enable", "disable"):
        item = subparsers.add_parser(command, help=f"{command.title()} an account")
        item.add_argument("--username", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    db = SessionLocal()
    try:
        if args.command == "create":
            account = auth_service.create_account(
                db,
                username=args.username,
                display_name=args.display_name,
                password=_password_from_input(args.password_stdin),
            )
            event_type = "admin_account_created"
        elif args.command == "reset-password":
            account = auth_service.reset_account_password(
                db,
                username=args.username,
                password=_password_from_input(args.password_stdin),
            )
            event_type = "admin_password_reset"
        else:
            enabled = args.command == "enable"
            account = auth_service.set_account_enabled(db, username=args.username, enabled=enabled)
            event_type = "admin_account_enabled" if enabled else "admin_account_disabled"
        auth_service.write_auth_audit(
            db,
            event_type=event_type,
            username_normalized=account.username_normalized,
            ip_address=None,
            user_agent="server-admin-command",
            request_id=new_request_id(),
            result="success",
            account=account,
        )
        db.commit()
        print(f"{args.command} succeeded: account_id={account.id} username={account.username_normalized}")
        return 0
    except AppError as exc:
        db.rollback()
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
