from __future__ import annotations

import argparse
import csv
import signal
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from hyatt_availability_automation import StopRequested, run_hyatt_availability_period_scan


ROOT = Path(__file__).resolve().parent


def default_start_date() -> date:
    return date.today() + timedelta(days=1)


def default_end_date() -> date:
    return date.today() + timedelta(days=30)


def prompt_text(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def prompt_bool(label: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1"}


def parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a YYYY-MM-DD date.") from exc


def parse_int(value: str, field_name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number.") from exc

    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Hyatt colleague comp night scanner without the local website UI.",
    )
    parser.add_argument("--destination", help="Destination, city, hotel, or location to search.")
    parser.add_argument("--start-date", help="Period start date in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="Period end date in YYYY-MM-DD format.")
    parser.add_argument("--shoulder-days", type=int, help="Shoulder days to use, from 0 to 14.")
    parser.add_argument("--keep-open", action="store_true", help="Keep the Playwright browser open after the scan.")
    parser.add_argument("--repeat", action="store_true", help="Repeat the same scan until you press Ctrl+C.")
    parser.add_argument("--repeat-hours", type=int, default=0, help="Hours between repeated scans.")
    parser.add_argument("--repeat-minutes", type=int, default=30, help="Minutes between repeated scans.")
    parser.add_argument("--save-csv", action="store_true", help="Save final results to a CSV file.")
    parser.add_argument("--no-prompt", action="store_true", help="Use command-line arguments and defaults without prompts.")
    return parser


def collect_config(args: argparse.Namespace) -> dict:
    destination_default = args.destination or "Tokyo"
    start_default = args.start_date or default_start_date().isoformat()
    end_default = args.end_date or default_end_date().isoformat()
    shoulder_default = str(args.shoulder_days if args.shoulder_days is not None else 7)

    if args.no_prompt:
        destination = destination_default
        start_text = start_default
        end_text = end_default
        shoulder_text = shoulder_default
        keep_open = args.keep_open
        repeat = args.repeat
        repeat_hours = args.repeat_hours
        repeat_minutes = args.repeat_minutes
        save_csv = args.save_csv
    else:
        print("Hyatt Comp Night Scanner")
        print("Terminal version. This does not start the local website UI.")
        print()
        destination = prompt_text("Destination", destination_default)
        start_text = prompt_text("Period start date", start_default)
        end_text = prompt_text("Period end date", end_default)
        shoulder_text = prompt_text("Shoulder days (0-14)", shoulder_default)
        keep_open = prompt_bool("Keep Playwright browser open after the scan", args.keep_open)
        repeat = prompt_bool("Repeat this scan automatically", args.repeat)
        repeat_hours = args.repeat_hours
        repeat_minutes = args.repeat_minutes
        if repeat:
            repeat_hours = parse_int(prompt_text("Repeat interval hours", str(repeat_hours)), "repeat hours", 0, 168)
            repeat_minutes = parse_int(prompt_text("Repeat interval minutes", str(repeat_minutes)), "repeat minutes", 0, 59)
        save_csv = prompt_bool("Save final results to CSV", args.save_csv)
        print()

    start = parse_date(start_text, "start date")
    end = parse_date(end_text, "end date")
    if end < start:
        raise ValueError("end date must be on or after start date.")

    shoulder_days = parse_int(str(shoulder_text), "shoulder days", 0, 14)
    repeat_hours = parse_int(str(repeat_hours), "repeat hours", 0, 168)
    repeat_minutes = parse_int(str(repeat_minutes), "repeat minutes", 0, 59)
    repeat_seconds = ((repeat_hours * 60) + repeat_minutes) * 60
    if repeat and repeat_seconds <= 0:
        raise ValueError("repeat interval must be at least 1 minute.")

    return {
        "destination": destination.strip() or "Tokyo",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "shoulder_days": shoulder_days,
        "keep_open": keep_open,
        "repeat": repeat,
        "repeat_seconds": repeat_seconds,
        "save_csv": save_csv,
    }


def print_log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def summarize_result(result: dict) -> str:
    return (
        f"{result.get('availableNightCount', 0)} night(s) across "
        f"{result.get('propertyCount', 0)} property/properties; "
        f"{result.get('completedWindows', 0)}/{result.get('totalWindows', 0)} windows complete."
    )


def print_result_table(result: dict) -> None:
    rows = result.get("availability") or []
    print()
    print("Available Complimentary Nights")
    print("=" * 32)
    print(summarize_result(result))
    print()

    if not rows:
        print("No complimentary nights found.")
        return

    property_width = min(max(len("Property"), *(len(row["property"]) for row in rows)), 42)
    print(f"{'Property'.ljust(property_width)} | Dates with complimentary room nights available")
    print(f"{'-' * property_width}-+-{'-' * 56}")
    for row in rows:
        property_name = row["property"]
        dates = ", ".join(format_date(value) for value in row["dates"])
        print(f"{property_name[:property_width].ljust(property_width)} | {dates}")


def format_date(iso_value: str) -> str:
    parsed = date.fromisoformat(iso_value)
    return parsed.strftime("%m/%d/%Y")


def save_result_csv(result: dict) -> Path | None:
    rows = result.get("availability") or []
    if not rows:
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = ROOT / f"hyatt_comp_nights_{timestamp}.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Property", "Dates with complimentary room nights available"])
        for row in rows:
            writer.writerow([row["property"], "; ".join(format_date(value) for value in row["dates"])])
    return target


def wait_for_repeat(seconds: int, stop_requested: list[bool]) -> None:
    next_run = time.monotonic() + seconds
    while not stop_requested[0]:
        remaining = int(next_run - time.monotonic())
        if remaining <= 0:
            return
        minutes = remaining // 60
        seconds_part = remaining % 60
        print(f"\rNext scan in {minutes:02d}:{seconds_part:02d}. Press Ctrl+C to stop.", end="", flush=True)
        time.sleep(1)
    print()


def run_once(config: dict, stop_requested: list[bool]) -> dict:
    last_alerted_nights = 0

    def progress(result: dict) -> None:
        nonlocal last_alerted_nights
        print_log(f"Progress: {summarize_result(result)}")
        available_nights = result.get("availableNightCount", 0)
        if available_nights > last_alerted_nights:
            last_alerted_nights = available_nights
            print("\aAvailability found.")
            for row in (result.get("availability") or [])[:8]:
                dates = ", ".join(format_date(value) for value in row["dates"])
                print(f"  {row['property']}: {dates}")

    return run_hyatt_availability_period_scan(
        destination=config["destination"],
        start_date=config["start_date"],
        end_date=config["end_date"],
        shoulder_days=config["shoulder_days"],
        keep_open=config["keep_open"],
        logger=print_log,
        progress_callback=progress,
        should_stop=lambda: stop_requested[0],
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = collect_config(args)
    except ValueError as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 2

    stop_requested = [False]
    hard_stop = [False]

    def handle_interrupt(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
        if stop_requested[0]:
            hard_stop[0] = True
            raise KeyboardInterrupt
        stop_requested[0] = True
        print()
        print_log("Stop requested. Waiting for the current browser checkpoint to finish.")

    signal.signal(signal.SIGINT, handle_interrupt)

    run_number = 1
    while True:
        try:
            stop_requested[0] = False
            print_log(f"Starting scan #{run_number}.")
            result = run_once(config, stop_requested)
            print_result_table(result)
            if config["save_csv"]:
                csv_path = save_result_csv(result)
                if csv_path:
                    print()
                    print(f"Saved CSV: {csv_path}")
        except StopRequested as exc:
            print_log(str(exc) or "Automation stopped.")
            return 130
        except KeyboardInterrupt:
            print()
            print_log("Stopped.")
            return 130
        except Exception as exc:  # noqa: BLE001 - keep the terminal app from closing without context
            print_log(f"Automation failed: {exc}")
            return 1

        if not config["repeat"] or stop_requested[0] or hard_stop[0]:
            return 0

        print()
        wait_for_repeat(config["repeat_seconds"], stop_requested)
        if stop_requested[0]:
            print_log("Repeat stopped.")
            return 130
        run_number += 1


if __name__ == "__main__":
    raise SystemExit(main())
