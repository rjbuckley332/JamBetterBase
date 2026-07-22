#!/usr/bin/env python3
"""Delete old tenant recording date folders from S3.

Only immediate children named YYYY-MM-DD under:
  vps/<vps-id>/recordings/<tenant>/

are eligible. Reserved folders such as "saved" are never deleted.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
import os
import re
from typing import Iterable

import boto3


DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROTECTED_FOLDERS = {"saved", "library", "temp", "tmp", "trash", ".trash", ".archived"}


@dataclass(frozen=True)
class CleanupCandidate:
    tenant: str
    folder: str
    prefix: str
    folder_date: date


def subtract_calendar_months(value: date, months: int) -> date:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1

    month_lengths = [31, 29 if _is_leap_year(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(value.day, month_lengths[month - 1])
    return date(year, month, day)


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def parse_date_folder(name: str) -> date | None:
    if not DATE_FOLDER_RE.fullmatch(name):
        return None
    try:
        return datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None


def tenant_root_prefix(vps_id: str, tenant: str) -> str:
    return f"vps/{vps_id.strip('/')}/recordings/{tenant.strip('/')}/"


def iter_date_folder_candidates(s3, bucket: str, vps_id: str, tenant: str, cutoff: date) -> Iterable[CleanupCandidate]:
    root = tenant_root_prefix(vps_id, tenant)
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=root, Delimiter="/"):
        for item in page.get("CommonPrefixes") or []:
            prefix = item.get("Prefix") or ""
            folder = prefix[len(root) :].strip("/")
            if not folder or folder.lower() in PROTECTED_FOLDERS:
                continue
            folder_date = parse_date_folder(folder)
            if folder_date is None:
                continue
            if folder_date < cutoff:
                yield CleanupCandidate(tenant=tenant, folder=folder, prefix=prefix, folder_date=folder_date)


def delete_prefix(s3, bucket: str, prefix: str, *, dry_run: bool) -> int:
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    batch: list[dict[str, str]] = []

    def flush() -> None:
        nonlocal deleted, batch
        if not batch:
            return
        if not dry_run:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        deleted += len(batch)
        batch = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj.get("Key")
            if not key:
                continue
            batch.append({"Key": key})
            if len(batch) >= 1000:
                flush()
    flush()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.getenv("LIBRARY_S3_BUCKET") or os.getenv("S3_BUCKET"), required=False)
    parser.add_argument("--vps-id", default=os.getenv("LIBRARY_VPS_ID") or os.getenv("VPS_ID"), required=False)
    parser.add_argument("--region", default=os.getenv("LIBRARY_AWS_REGION") or os.getenv("AWS_REGION") or "us-east-2")
    parser.add_argument("--tenant", action="append", dest="tenants", help="Tenant slug. Repeat or comma-separate.")
    parser.add_argument("--months", type=int, default=1, help="Calendar months to keep. Default: 1.")
    parser.add_argument("--today", help="Override current date as YYYY-MM-DD for tests or audits.")
    parser.add_argument("--execute", action="store_true", help="Actually delete. Without this flag, runs as a dry run.")
    args = parser.parse_args()

    if not args.bucket or not args.vps_id:
        parser.error("--bucket and --vps-id are required, or set LIBRARY_S3_BUCKET/LIBRARY_VPS_ID")
    if args.months < 1:
        parser.error("--months must be at least 1")

    tenants = []
    for value in args.tenants or ["pd,vc,seigr"]:
        tenants.extend(t.strip() for t in value.split(",") if t.strip())
    tenants = sorted(set(tenants))

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else datetime.now(timezone.utc).date()
    cutoff = subtract_calendar_months(today, args.months)
    s3 = boto3.client("s3", region_name=args.region)

    mode = "DELETE" if args.execute else "DRY-RUN"
    print(f"[cleanup] mode={mode} bucket={args.bucket} vps_id={args.vps_id} cutoff_before={cutoff.isoformat()} tenants={','.join(tenants)}")

    total_folders = 0
    total_objects = 0
    for tenant in tenants:
        candidates = list(iter_date_folder_candidates(s3, args.bucket, args.vps_id, tenant, cutoff))
        if not candidates:
            print(f"[cleanup] tenant={tenant} no eligible folders")
            continue
        for candidate in candidates:
            objects = delete_prefix(s3, args.bucket, candidate.prefix, dry_run=not args.execute)
            total_folders += 1
            total_objects += objects
            print(f"[cleanup] tenant={tenant} folder={candidate.folder} objects={objects} prefix=s3://{args.bucket}/{candidate.prefix}")

    print(f"[cleanup] complete folders={total_folders} objects={total_objects} dry_run={not args.execute}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
