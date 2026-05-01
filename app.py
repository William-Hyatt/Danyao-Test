from __future__ import annotations

import json
import mimetypes
import os
import threading
import time
import traceback
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from hyatt_availability_automation import StopRequested, run_hyatt_availability_period_scan


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HOST = os.environ.get("HYATT_APP_HOST", "127.0.0.1")
PORT = int(os.environ.get("HYATT_APP_PORT", "8765"))

JOBS: dict[str, dict] = {}
CANCEL_EVENTS: dict[str, threading.Event] = {}
JOBS_LOCK = threading.Lock()
JOB_COUNTER = 0


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_iso_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid YYYY-MM-DD date") from exc


def validate_payload(payload: dict) -> dict:
    destination = str(payload.get("destination") or "Tokyo").strip()
    if not destination:
        destination = "Tokyo"

    start_date = parse_iso_date(str(payload.get("startDate") or ""), "startDate")
    end_date = parse_iso_date(str(payload.get("endDate") or ""), "endDate")

    if end_date < start_date:
        raise ValueError("endDate must be on or after startDate")

    try:
        shoulder_days = int(payload.get("shoulderDays") or 7)
    except (TypeError, ValueError) as exc:
        raise ValueError("shoulderDays must be a number") from exc

    if shoulder_days < 0 or shoulder_days > 14:
        raise ValueError("shoulderDays must be between 0 and 14")

    return {
        "destination": destination,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "shoulder_days": shoulder_days,
        "keep_open": bool(payload.get("keepOpen", False)),
    }


def append_log(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["logs"].append({"time": utc_stamp(), "message": message})
        job["updatedAt"] = utc_stamp()


def create_job(params: dict) -> str:
    global JOB_COUNTER
    with JOBS_LOCK:
        JOB_COUNTER += 1
        job_id = str(JOB_COUNTER)
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "params": params,
            "logs": [{"time": utc_stamp(), "message": "Queued single-night availability scan."}],
            "result": None,
            "error": None,
            "stopRequested": False,
            "createdAt": utc_stamp(),
            "updatedAt": utc_stamp(),
        }
        CANCEL_EVENTS[job_id] = threading.Event()
    return job_id


def set_job_status(job_id: str, status: str, **extra: object) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["status"] = status
        job["updatedAt"] = utc_stamp()
        for key, value in extra.items():
            job[key] = value


def set_job_result(job_id: str, result: dict) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["result"] = result
        job["updatedAt"] = utc_stamp()


def request_job_stop(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None

        event = CANCEL_EVENTS.get(job_id)
        if event:
            event.set()

        job["stopRequested"] = True
        if job["status"] in ("queued", "running"):
            job["status"] = "stopping"
            job["logs"].append({"time": utc_stamp(), "message": "Stop requested. Waiting for the current browser step to finish."})
        job["updatedAt"] = utc_stamp()
        return json.loads(json.dumps(job))


def is_job_stop_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        event = CANCEL_EVENTS.get(job_id)
        return bool(event and event.is_set())


def clear_cancel_event(job_id: str) -> None:
    with JOBS_LOCK:
        CANCEL_EVENTS.pop(job_id, None)


def run_job(job_id: str) -> None:
    with JOBS_LOCK:
        params = dict(JOBS[job_id]["params"])

    set_job_status(job_id, "running")
    append_log(job_id, "Opening HyattConnect in a Playwright-controlled browser.")

    try:
        result = run_hyatt_availability_period_scan(
            destination=params["destination"],
            start_date=params["start_date"],
            end_date=params["end_date"],
            shoulder_days=params["shoulder_days"],
            keep_open=params["keep_open"],
            logger=lambda message: append_log(job_id, message),
            progress_callback=lambda partial: set_job_result(job_id, partial),
            should_stop=lambda: is_job_stop_requested(job_id),
        )
        if is_job_stop_requested(job_id):
            set_job_status(job_id, "stopped", result=result)
            append_log(job_id, "Automation stopped.")
        else:
            set_job_status(job_id, "completed", result=result)
            append_log(job_id, "Automation finished.")
    except StopRequested as exc:
        set_job_status(job_id, "stopped")
        append_log(job_id, str(exc) or "Automation stopped.")
    except Exception as exc:  # noqa: BLE001 - surface automation errors to the UI
        set_job_status(
            job_id,
            "failed",
            error={"message": str(exc), "traceback": traceback.format_exc(limit=6)},
        )
        append_log(job_id, f"Automation failed: {exc}")
    finally:
        clear_cancel_event(job_id)


class HyattAvailabilityHandler(BaseHTTPRequestHandler):
    server_version = "HyattAvailabilityApp/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/jobs":
            self.send_json(list_jobs())
            return

        if path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            job = get_job(job_id)
            if not job:
                self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json(job)
            return

        if path in ("", "/"):
            self.serve_static_file(STATIC_DIR / "index.html")
            return

        if path.startswith("/static/"):
            target = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if not str(target).startswith(str(STATIC_DIR.resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self.serve_static_file(target)
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/api/jobs/") and path.endswith("/stop"):
            parts = path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 4 else ""
            job = request_job_stop(job_id)
            if not job:
                self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_json({"job": job}, HTTPStatus.ACCEPTED)
            return

        if parsed.path != "/api/search":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            raw_length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(raw_length) or b"{}")
            params = validate_payload(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        job_id = create_job(params)
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()

        self.send_json({"jobId": job_id, "job": get_job(job_id)}, HTTPStatus.ACCEPTED)

    def serve_static_file(self, target: Path) -> None:
        if not target.exists() or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {format % args}")


def get_job(job_id: str) -> dict | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return json.loads(json.dumps(job)) if job else None


def list_jobs() -> list[dict]:
    with JOBS_LOCK:
        jobs = list(JOBS.values())
        return json.loads(json.dumps(jobs[-20:]))


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), HyattAvailabilityHandler)
    print(f"Hyatt availability app running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping app.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
