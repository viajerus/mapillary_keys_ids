"""Enrich a CSV using numeric image IDs, with durable progress and blank fallbacks."""

import argparse
from contextlib import contextmanager
import csv
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from utils import get_image_metadata, set_image_request


OUTPUT_FIELDS = ["image_key", "image_id", "lat", "lon", "alt", "capture_date"]
STATUS_FIELDS = ["row_number", "image_key", "image_id", "status", "reason"]
DEFAULT_INPUT = Path(__file__).resolve().parent / "mapillary_image_key_image_id.csv"


def validate_output_paths(input_path, output_path):
    """Reject input collisions before opening snapshots, checkpoints, or locks."""
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    status_path = output_path.with_name(output_path.stem + "_status.csv")
    cache_path = output_path.with_suffix(".sqlite3")
    generated_paths = [
        output_path,
        status_path,
        cache_path,
        output_path.with_suffix(".lock"),
        output_path.with_suffix(output_path.suffix + ".tmp"),
        status_path.with_suffix(status_path.suffix + ".tmp"),
        *(cache_path.with_name(cache_path.name + suffix) for suffix in ("-journal", "-wal", "-shm")),
    ]
    paths = [input_path, *(path.resolve() for path in generated_paths)]
    if len(set(paths)) != len(paths):
        raise ValueError("Input and generated output paths must be different.")
    return input_path, output_path, status_path, cache_path


@contextmanager
def output_lock(output):
    """Prevent two CLI runs from spending requests on the same output."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(".lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another process is already writing this output. Wait for it to finish.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def numeric_id(value):
    """Normalize integer/whole-decimal text exactly, without float rounding."""
    value = (value or "").strip()
    integer, separator, fraction = value.partition(".")
    if separator and (not fraction or fraction.strip("0")):
        return None
    if integer and integer.isascii() and integer.isdecimal() and integer.strip("0"):
        return integer.lstrip("0")
    return None


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not {"image_key", "image_id"} <= set(reader.fieldnames):
            raise ValueError("Input CSV must contain image_key and image_id columns.")
        rows = []
        for row in reader:
            if None in row:
                raise ValueError(f"Extra CSV values at line {reader.line_num}.")
            record = {name: row.get(name) or "" for name in OUTPUT_FIELDS[:2]}
            normalized = numeric_id(record["image_id"])
            if normalized is not None:
                record["image_id"] = normalized
            rows.append(record)
    return rows


def export_csvs(rows, database, output, status_output):
    """Write full snapshots; pending/invalid/failed rows retain blank attributes."""
    cache = {
        image_id: (status, json.loads(payload) if payload else {}, reason)
        for image_id, status, payload, reason in database.execute(
            "SELECT image_id, status, payload, reason FROM results"
        )
    }
    output_temp = output.with_suffix(output.suffix + ".tmp")
    status_temp = status_output.with_suffix(status_output.suffix + ".tmp")
    with output_temp.open("w", encoding="utf-8", newline="") as data_file, status_temp.open(
        "w", encoding="utf-8", newline=""
    ) as status_file:
        writer = csv.DictWriter(data_file, fieldnames=OUTPUT_FIELDS)
        statuses = csv.DictWriter(status_file, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        statuses.writeheader()
        for row_number, row in enumerate(rows, start=2):
            image_id = numeric_id(row["image_id"])
            if image_id is None:
                reason = "empty_image_id" if not row["image_id"].strip() else "invalid_image_id"
                status, metadata = "skipped", {}
            else:
                status, metadata, reason = cache.get(image_id, ("pending", {}, "not_processed"))
            writer.writerow({
                **row,
                "lat": metadata.get("lat"),
                "lon": metadata.get("lon"),
                "alt": metadata.get("altitude"),
                "capture_date": metadata.get("capture_date"),
            })
            statuses.writerow({
                "row_number": row_number, **row, "status": status, "reason": reason,
            })
    output_temp.replace(output)
    status_temp.replace(status_output)


def retry_delay(response, attempt):
    """Honor Retry-After seconds or HTTP dates, otherwise use exponential delay."""
    delay = min(60.0, 2.0 ** (attempt + 1))
    if response is not None:
        value = response.headers.get("Retry-After")
        if value:
            try:
                specified = float(value)
            except ValueError:
                try:
                    specified = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    specified = 0
            if math.isfinite(specified):
                delay = max(delay, specified)
    return delay


def fetch_metadata(image_id, token, session, pace, attempts=5):
    """Retry temporary failures; stop the batch for authentication or sustained outages."""
    for attempt in range(attempts):
        delay = pace["next"] - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        pace["next"] = time.monotonic() + pace["interval"]
        response = None
        try:
            return get_image_metadata(image_id, token, session=session), ""
        except requests.HTTPError as error:
            response = error.response
            status = response.status_code
            try:
                detail = response.json().get("error", {})
                if not isinstance(detail, dict):
                    detail = {}
            except (ValueError, AttributeError):
                detail = {}
            message = str(detail.get("message", "")).lower()
            throttled = (
                status == 429
                or detail.get("code") in (4, 17, 32, 613, 80004)
                or "request limit" in message
                or "rate limit" in message
                or "request limit" in response.headers.get("WWW-Authenticate", "").lower()
            )
            if not throttled and (status in (401, 403) or detail.get("code") == 190):
                raise RuntimeError(f"Authorization denied (HTTP {status}); check the token and permissions.") from None
            if not throttled and status != 408 and status < 500:
                return None, f"http_{status}"
        except (requests.Timeout, requests.ConnectionError):
            pass
        except requests.RequestException:
            raise RuntimeError("Request failed; saved progress can be resumed.") from None
        except ValueError:
            return None, "invalid_or_missing_metadata"
        if attempt == attempts - 1:
            raise RuntimeError("Repeated throttling or temporary network/API failure; retry this run later.")
        delay = retry_delay(response, attempt)
        print(f"Temporary failure for {image_id}; retrying in {delay:.1f}s.", flush=True)
        time.sleep(delay)


def process_csv(input_path, output_path, rate=2.0, limit=None, retry_errors=False, prepare_only=False):
    input_path, output_path, status_path, cache_path = validate_output_paths(input_path, output_path)
    if not math.isfinite(rate) or rate <= 0 or (limit is not None and limit < 1):
        raise ValueError("Rate and limit must be positive.")
    if not cache_path.exists() and (output_path.exists() or status_path.exists()):
        raise ValueError("Output exists without its checkpoint. Choose a new output filename.")
    rows = read_rows(input_path)
    ids = list(dict.fromkeys(value for row in rows if (value := numeric_id(row["image_id"]))))
    invalid = sum(numeric_id(row["image_id"]) is None for row in rows)
    print(f"Rows: {len(rows)}; unique numeric IDs: {len(ids)}; skipped rows: {invalid}.", flush=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(cache_path)
    try:
        database.execute(
            "CREATE TABLE IF NOT EXISTS results (image_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL, payload TEXT, reason TEXT NOT NULL)"
        )
        database.commit()
        export_csvs(rows, database, output_path, status_path)
        completed = dict(database.execute("SELECT image_id, status FROM results"))
        pending = [image_id for image_id in ids if image_id not in completed or (
            retry_errors and completed[image_id] == "error"
        )]
        if limit is not None:
            pending = pending[:limit]
        print(f"IDs to fetch this run: {len(pending)}. Output: {output_path}", flush=True)
        if prepare_only or not pending:
            return
        # Resolve .env once; never log the token or put it in a URL.
        token = set_image_request(pending[0])["headers"]["Authorization"].removeprefix("OAuth ")
        pace = {"next": 0.0, "interval": 1.0 / rate}
        consecutive_errors = 0
        try:
            with requests.Session() as session:
                for number, image_id in enumerate(pending, start=1):
                    metadata, reason = fetch_metadata(image_id, token, session, pace)
                    status = "ok" if metadata is not None else "error"
                    database.execute(
                        "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?)",
                        (image_id, status, json.dumps(metadata) if metadata is not None else None, reason),
                    )
                    database.commit()
                    consecutive_errors = consecutive_errors + 1 if status == "error" else 0
                    if number == 1 or number % 100 == 0:
                        export_csvs(rows, database, output_path, status_path)
                        print(f"Processed {number}/{len(pending)} this run; last status: {status}.", flush=True)
                    if consecutive_errors >= 10:
                        raise RuntimeError("Stopped after 10 consecutive image errors. Review status CSV before retrying.")
        finally:
            export_csvs(rows, database, output_path, status_path)
        print("Run finished. See status CSV for skipped or failed rows.", flush=True)
    finally:
        database.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rate", type=float, default=2.0, help="Maximum HTTP request starts per second (default: 2)")
    parser.add_argument("--limit", type=int, help="Maximum unique IDs to process this run")
    parser.add_argument("--retry-errors", action="store_true", help="Retry previously failed IDs as well as pending IDs")
    parser.add_argument("--prepare-only", action="store_true", help="Create CSV snapshots without API requests")
    args = parser.parse_args()
    output = args.output or args.input.with_name(args.input.stem + "_metadata.csv")
    try:
        input_path, output, _, _ = validate_output_paths(args.input, output)
        with output_lock(output):
            process_csv(input_path, output, args.rate, args.limit, args.retry_errors, args.prepare_only)
    except KeyboardInterrupt:
        print("Interrupted. Rerun the same command to resume.", flush=True)
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print(f"Stopped: {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
