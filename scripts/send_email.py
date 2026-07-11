#!/usr/bin/env python
"""Send plain-text email via Amazon SES.

Usable as a library (`from send_email import send_email`) or as a CLI.

AWS credentials are resolved by boto3's standard chain (env vars,
~/.aws/credentials, IAM role) -- this script never reads or echoes secrets.

Config via environment (or pass explicitly):
  AWS_REGION       SES region              (default: us-east-1)
  SES_FROM_ADDRESS default From: address   (required if --from not given)

CLI:
  python scripts/send_email.py \
      --to "<recipient>" "<recipient>" \
      --subject "Report ready" \
      --body "The nightly report finished."

  # body from stdin:
  echo "hello" | python scripts/send_email.py --to "<recipient>" --subject Hi
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_REGION = "us-east-1"


def send_email(
    *,
    sender: str,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    reply_to: list[str] | None = None,
    region: str | None = None,
) -> str:
    """Send a plain-text email through Amazon SES.

    Returns the SES message ID. Raises ClientError/BotoCoreError on failure.
    """
    if not to:
        raise ValueError("at least one recipient is required")
    if not sender:
        raise ValueError("a sender address is required")

    client = boto3.client(
        "ses", region_name=region or os.environ.get("AWS_REGION", DEFAULT_REGION)
    )

    destination: dict[str, list[str]] = {"ToAddresses": to}
    if cc:
        destination["CcAddresses"] = cc
    if bcc:
        destination["BccAddresses"] = bcc

    kwargs: dict = {
        "Source": sender,
        "Destination": destination,
        "Message": {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    }
    if reply_to:
        kwargs["ReplyToAddresses"] = reply_to

    response = client.send_email(**kwargs)
    return response["MessageId"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a plain-text email via Amazon SES.")
    parser.add_argument("--to", nargs="+", required=True, help="recipient address(es)")
    parser.add_argument("--subject", required=True, help="email subject")
    parser.add_argument(
        "--body",
        help="email body text; if omitted, read from stdin",
    )
    parser.add_argument(
        "--from",
        dest="sender",
        default=os.environ.get("SES_FROM_ADDRESS"),
        help="sender address (default: $SES_FROM_ADDRESS)",
    )
    parser.add_argument("--cc", nargs="+", help="CC address(es)")
    parser.add_argument("--bcc", nargs="+", help="BCC address(es)")
    parser.add_argument("--reply-to", nargs="+", help="Reply-To address(es)")
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", DEFAULT_REGION),
        help=f"AWS SES region (default: {DEFAULT_REGION})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if not args.sender:
        print(
            "error: no sender address (pass --from or set SES_FROM_ADDRESS)",
            file=sys.stderr,
        )
        return 2

    body = args.body if args.body is not None else sys.stdin.read()

    try:
        message_id = send_email(
            sender=args.sender,
            to=args.to,
            subject=args.subject,
            body=body,
            cc=args.cc,
            bcc=args.bcc,
            reply_to=args.reply_to,
            region=args.region,
        )
    except (ClientError, BotoCoreError) as exc:
        print(f"error: failed to send email: {exc}", file=sys.stderr)
        return 1

    print(f"sent: {message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
