from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

from playwright.sync_api import BrowserContext, Error, Locator, Page, TimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("HYATT_USER_DATA_DIR", ROOT / ".playwright" / "hyatt-profile"))
HYATT_HOME_URL = "https://www.hyattconnect.com/site/hc/#/home"

APP_LINK_PATTERN = re.compile(r"Colleague\s+Discount\s+Room\s+Availability", re.I)
DESTINATION_LABEL = re.compile(r"(destination|location|city|hotel|where)", re.I)
ARRIVAL_DATE_LABEL = re.compile(r"(arrival\s*date|check\s*-?\s*in|start\s*date)", re.I)
SHOULDER_DAYS_LABEL = re.compile(r"shoulder\s+days", re.I)
COLLEAGUE_COMP_LABEL = re.compile(r"colleague\s+comp", re.I)
SEARCH_BUTTON = re.compile(r"^(search|find|check availability|view availability|show rates)$", re.I)
RESULTS_TEXT = re.compile(r"(colleague\s+comp\s+rooms|property\s+name|not\s+avail|available)", re.I)
DATE_TEXT = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


Logger = Callable[[str], None]
ProgressCallback = Callable[[dict], None]
ShouldStop = Callable[[], bool]


class StopRequested(Exception):
    pass


def run_hyatt_availability_search(
    destination: str,
    check_in: str,
    check_out: str | None = None,
    keep_open: bool = False,
    logger: Logger | None = None,
    should_stop: ShouldStop | None = None,
) -> dict:
    """Compatibility wrapper for the original one-date search."""
    return run_hyatt_availability_period_scan(
        destination=destination,
        start_date=check_in,
        end_date=check_in,
        shoulder_days=0,
        keep_open=keep_open,
        logger=logger,
        should_stop=should_stop,
    )


def run_hyatt_availability_period_scan(
    destination: str,
    start_date: str,
    end_date: str,
    shoulder_days: int = 7,
    keep_open: bool = False,
    logger: Logger | None = None,
    progress_callback: ProgressCallback | None = None,
    should_stop: ShouldStop | None = None,
) -> dict:
    log = logger or (lambda message: None)
    progress = progress_callback or (lambda result: None)
    stop_requested = should_stop or (lambda: False)
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    windows = build_scan_windows(start, end, shoulder_days)
    availability: dict[str, set[str]] = {}
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = launch_context(playwright, log)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            check_stop(stop_requested)
            page.goto(HYATT_HOME_URL, wait_until="domcontentloaded", timeout=90_000)
            log("HyattConnect loaded. If SSO appears, sign in in the opened browser.")

            wait_for_app_link(page, log, stop_requested)
            check_stop(stop_requested)
            active_page = click_colleague_availability_link(context, page, log)
            active_page.bring_to_front()
            settle_page(active_page)

            for index, window in enumerate(windows, start=1):
                check_stop(stop_requested)
                arrival = window["arrival_date"]
                log(
                    "Scanning window "
                    f"{index}/{len(windows)}: {window['coverage_start']} through {window['coverage_end']}."
                )

                configure_single_night_search(
                    page=active_page,
                    destination=destination,
                    arrival_date=arrival,
                    shoulder_days=shoulder_days,
                    log=log,
                    should_stop=stop_requested,
                )
                check_stop(stop_requested)
                submit_search(active_page, log)
                wait_for_results_grid(active_page, log, stop_requested)
                check_stop(stop_requested)
                select_list_view(active_page)

                window_results = extract_available_nights(active_page, start, end)
                merge_availability(availability, window_results)
                log_window_results(window_results, log)

                progress(
                    build_result(
                        destination=destination,
                        start=start,
                        end=end,
                        shoulder_days=shoulder_days,
                        windows=windows,
                        completed_windows=index,
                        availability=availability,
                        url=safe_url(active_page),
                    )
                )

            result = build_result(
                destination=destination,
                start=start,
                end=end,
                shoulder_days=shoulder_days,
                windows=windows,
                completed_windows=len(windows),
                availability=availability,
                url=safe_url(active_page),
            )

            if keep_open:
                log("Scan is complete. Close the Playwright browser when you are finished reviewing it.")
                progress(result)
                wait_until_browser_closed(context, stop_requested)
            else:
                context.close()

            return result
        except StopRequested:
            context.close()
            raise
        except Exception:
            if not keep_open:
                context.close()
            raise


def check_stop(should_stop: ShouldStop) -> None:
    if should_stop():
        raise StopRequested("Automation stopped by user.")


def launch_context(playwright, log: Logger) -> BrowserContext:
    channel = os.environ.get("HYATT_BROWSER_CHANNEL", "msedge").strip()
    headless = os.environ.get("HYATT_HEADLESS", "false").lower() == "true"
    launch_kwargs = {
        "headless": headless,
        "viewport": {"width": 1440, "height": 950},
        "accept_downloads": True,
    }

    if channel:
        try:
            log(f"Launching browser channel: {channel}.")
            return playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                channel=channel,
                **launch_kwargs,
            )
        except Error as exc:
            log(f"Could not launch {channel}: {exc}. Falling back to bundled Chromium.")

    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        **launch_kwargs,
    )


def build_scan_windows(start: date, end: date, shoulder_days: int) -> list[dict[str, str]]:
    windows: list[dict[str, str]] = []
    cursor = start
    shoulder = timedelta(days=shoulder_days)

    while cursor <= end:
        if shoulder_days == 0:
            arrival = cursor
            coverage_start = cursor
            coverage_end = cursor
        else:
            arrival = min(cursor + shoulder, end)
            coverage_start = cursor
            coverage_end = min(end, arrival + shoulder)

        windows.append(
            {
                "arrival_date": arrival.isoformat(),
                "coverage_start": coverage_start.isoformat(),
                "coverage_end": coverage_end.isoformat(),
            }
        )
        cursor = coverage_end + timedelta(days=1)

    return windows


def wait_for_app_link(page: Page, log: Logger, should_stop: ShouldStop) -> None:
    deadline = time.monotonic() + 180
    last_filter_attempt = 0.0

    while time.monotonic() < deadline:
        check_stop(should_stop)
        if text_exists_in_any_frame(page, APP_LINK_PATTERN):
            log("Found Colleague Discount Room Availability.")
            return

        if time.monotonic() - last_filter_attempt > 8:
            last_filter_attempt = time.monotonic()
            try_fill_app_filter(page)

        time.sleep(1)

    raise TimeoutError(
        "Could not find 'Colleague Discount Room Availability' on HyattConnect. "
        "Confirm that HyattConnect finished loading and that the app is available to your account."
    )


def click_colleague_availability_link(context: BrowserContext, page: Page, log: Logger) -> Page:
    pages_before = set(context.pages)

    try:
        with context.expect_page(timeout=8_000) as popup_info:
            click_text_in_any_frame(page, APP_LINK_PATTERN)
        new_page = popup_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=60_000)
        log("Colleague availability opened in a new tab.")
        return new_page
    except TimeoutError:
        new_pages = [candidate for candidate in context.pages if candidate not in pages_before]
        if new_pages:
            new_page = new_pages[-1]
            new_page.wait_for_load_state("domcontentloaded", timeout=60_000)
            log("Colleague availability opened in a new tab.")
            return new_page

        page.wait_for_load_state("domcontentloaded", timeout=60_000)
        log("Colleague availability opened in the current tab.")
        return page


def configure_single_night_search(
    page: Page,
    destination: str,
    arrival_date: str,
    shoulder_days: int,
    log: Logger,
    should_stop: ShouldStop,
) -> None:
    settle_page(page)
    check_stop(should_stop)
    log(f"Setting destination to {destination}.")
    fill_destination(page, destination)

    check_stop(should_stop)
    log(f"Setting arrival date to {arrival_date}.")
    fill_date(page, ARRIVAL_DATE_LABEL, arrival_date)

    check_stop(should_stop)
    log(f"Setting shoulder days to +/- {shoulder_days}.")
    set_shoulder_days(page, shoulder_days)

    check_stop(should_stop)
    log("Setting search controls to 1 night, 1 room, 1 adult, 0 children.")
    set_counter_value(page, "Nights", 1)
    set_counter_value(page, "Rooms", 1)
    set_counter_value(page, "Adults", 1)
    set_counter_value(page, "Children", 0)

    check_stop(should_stop)
    log("Selecting Colleague Comp rate.")
    select_colleague_comp_rate(page)


def fill_destination(page: Page, destination: str) -> None:
    for locator in destination_locators(page):
        if try_fill(locator, destination):
            click_destination_autocomplete_option(page, destination)
            return
    raise TimeoutError("Could not find a destination field on the availability page.")


def fill_date(page: Page, label_pattern: re.Pattern, iso_value: str) -> None:
    for locator in date_locators(page, label_pattern):
        if try_fill_date(locator, iso_value):
            return
    raise TimeoutError(f"Could not find or fill date field for {label_pattern.pattern}.")


def set_shoulder_days(page: Page, shoulder_days: int) -> None:
    label = f"+/- {shoulder_days}"
    compact_label = f"+/-{shoulder_days}"

    for frame in page.frames:
        locators = [
            frame.get_by_label(SHOULDER_DAYS_LABEL).first,
            frame.locator("select[name*='shoulder' i]").first,
            frame.locator("select[id*='shoulder' i]").first,
        ]

        for locator in locators:
            if try_select_option(locator, label) or try_select_option(locator, compact_label):
                return

        field = frame.get_by_label(SHOULDER_DAYS_LABEL).first
        if try_click(field):
            option = frame.get_by_text(re.compile(rf"\+/-\s*{shoulder_days}\b")).first
            if try_click(option):
                return


def set_counter_value(page: Page, label: str, target: int) -> None:
    if try_fill_counter_input(page, label, target):
        return

    for frame in page.frames:
        for _ in range(8):
            try:
                status = frame.evaluate(COUNTER_SCRIPT, {"label": label, "target": target})
            except Error:
                break

            if status.get("done"):
                return
            if not status.get("clicked"):
                break
            time.sleep(0.25)


def try_fill_counter_input(page: Page, label: str, target: int) -> bool:
    pattern = re.compile(label, re.I)
    for frame in page.frames:
        locators = [
            frame.get_by_label(pattern).first,
            frame.locator(f"input[name*='{label}' i]").first,
            frame.locator(f"input[id*='{label}' i]").first,
            frame.locator(f"input[aria-label*='{label}' i]").first,
        ]
        for locator in locators:
            if try_fill(locator, str(target)):
                return True
    return False


def select_colleague_comp_rate(page: Page) -> None:
    for frame in page.frames:
        radio = frame.get_by_role("radio", name=COLLEAGUE_COMP_LABEL).first
        if try_check(radio):
            return
        labelled = frame.get_by_label(COLLEAGUE_COMP_LABEL).first
        if try_check(labelled):
            return
        text = frame.get_by_text(COLLEAGUE_COMP_LABEL).first
        if try_click(text):
            return


def submit_search(page: Page, log: Logger) -> None:
    log("Submitting the availability search.")
    for frame in page.frames:
        button = frame.get_by_role("button", name=SEARCH_BUTTON).first
        if try_click(button):
            settle_page(page)
            return

    for frame in page.frames:
        button = frame.get_by_text(SEARCH_BUTTON).first
        if try_click(button):
            settle_page(page)
            return

    for frame in page.frames:
        locator = frame.locator(
            "button[type='submit'], input[type='submit'], "
            "button:has-text('Search'), button:has-text('Find'), "
            "button:has-text('Availability'), button:has-text('CHECK AVAILABILITY')"
        ).first
        if try_click(locator):
            settle_page(page)
            return

    raise TimeoutError("Could not find a Check Availability button.")


def wait_for_results_grid(page: Page, log: Logger, should_stop: ShouldStop) -> None:
    deadline = time.monotonic() + 75
    while time.monotonic() < deadline:
        check_stop(should_stop)
        if text_exists_in_any_frame(page, RESULTS_TEXT):
            return
        time.sleep(1)
    log("Results grid was not detected by text, so the scraper will still attempt to read the page.")


def select_list_view(page: Page) -> None:
    for frame in page.frames:
        option = frame.get_by_text(re.compile(r"\bList\s+View\b", re.I)).first
        if try_click(option):
            settle_page(page)
            return


def extract_available_nights(page: Page, start: date, end: date) -> dict[str, set[str]]:
    matrices: list[list[list[str]]] = []

    for frame in page.frames:
        try:
            matrices.extend(frame.locator("table").evaluate_all(TABLE_MATRIX_SCRIPT))
        except Error:
            pass
        try:
            matrices.extend(frame.locator("[role='grid'], [role='table']").evaluate_all(ROLE_GRID_MATRIX_SCRIPT))
        except Error:
            pass

    return parse_available_matrices(matrices, start, end)


def parse_available_matrices(
    matrices: list[list[list[str]]],
    start: date,
    end: date,
) -> dict[str, set[str]]:
    results: dict[str, set[str]] = {}

    for matrix in matrices:
        if len(matrix) < 2:
            continue

        column_dates = extract_column_dates(matrix, start, end)
        if not column_dates:
            continue

        for row in matrix:
            if len(row) < 3:
                continue

            property_name = clean_property_name(row[0])
            if not property_name:
                continue

            for column_index, cell_text in enumerate(row):
                night = column_dates.get(column_index)
                if not night or not is_available_cell(cell_text):
                    continue
                if start <= night <= end:
                    results.setdefault(property_name, set()).add(night.isoformat())

    return results


def extract_column_dates(
    matrix: list[list[str]],
    start: date,
    end: date,
) -> dict[int, date]:
    column_dates: dict[int, date] = {}

    for row in matrix[:10]:
        for column_index, cell_text in enumerate(row):
            if column_index in column_dates:
                continue
            parsed = parse_header_date(cell_text, start, end)
            if parsed:
                column_dates[column_index] = parsed

    return column_dates


def parse_header_date(text: str, start: date, end: date) -> date | None:
    match = DATE_TEXT.search(text or "")
    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    year_text = match.group(3)

    if year_text:
        year = int(year_text)
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None

    candidates: list[date] = []
    for year in range(start.year - 1, end.year + 2):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue

    if not candidates:
        return None

    window_start = start - timedelta(days=14)
    window_end = end + timedelta(days=14)
    in_window = [candidate for candidate in candidates if window_start <= candidate <= window_end]
    if in_window:
        return min(in_window, key=lambda candidate: abs((candidate - start).days))

    return min(candidates, key=lambda candidate: abs((candidate - start).days))


def clean_property_name(value: str) -> str:
    text = " ".join((value or "").split())
    if not text:
        return ""
    if re.search(r"^(property\s+name|destination|hotel|cur\.?|currency)$", text, re.I):
        return ""
    if DATE_TEXT.search(text):
        return ""
    if re.search(r"\b(not\s+avail|available|selected)\b", text, re.I):
        return ""
    return text


def is_available_cell(value: str) -> bool:
    text = " ".join((value or "").split())
    if re.search(r"\bnot\s+avail", text, re.I):
        return False
    return bool(re.search(r"\bavailable\b", text, re.I))


def merge_availability(target: dict[str, set[str]], incoming: dict[str, set[str]]) -> None:
    for property_name, dates in incoming.items():
        target.setdefault(property_name, set()).update(dates)


def build_result(
    destination: str,
    start: date,
    end: date,
    shoulder_days: int,
    windows: list[dict[str, str]],
    completed_windows: int,
    availability: dict[str, set[str]],
    url: str,
) -> dict:
    rows = [
        {"property": property_name, "dates": sorted(dates)}
        for property_name, dates in sorted(availability.items(), key=lambda item: item[0].lower())
        if dates
    ]

    return {
        "destination": destination,
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "shoulderDays": shoulder_days,
        "singleNight": True,
        "windows": windows,
        "completedWindows": completed_windows,
        "totalWindows": len(windows),
        "availability": rows,
        "availableNightCount": sum(len(row["dates"]) for row in rows),
        "propertyCount": len(rows),
        "url": url,
    }


def log_window_results(results: dict[str, set[str]], log: Logger) -> None:
    count = sum(len(dates) for dates in results.values())
    if count == 0:
        log("No complimentary nights found in this window.")
        return
    log(f"Found {count} complimentary night(s) in this window.")


def destination_locators(page: Page) -> Iterable[Locator]:
    selectors = [
        "input[name*='destination' i]",
        "input[id*='destination' i]",
        "input[placeholder*='destination' i]",
        "input[name*='location' i]",
        "input[id*='location' i]",
        "input[placeholder*='location' i]",
        "input[name*='city' i]",
        "input[id*='city' i]",
        "input[placeholder*='city' i]",
        "input[aria-label*='destination' i]",
        "input[aria-label*='where' i]",
    ]

    for frame in page.frames:
        yield frame.get_by_label(DESTINATION_LABEL).first
        yield frame.get_by_placeholder(DESTINATION_LABEL).first
        yield frame.get_by_role("combobox", name=DESTINATION_LABEL).first
        yield frame.get_by_role("textbox", name=DESTINATION_LABEL).first
        for selector in selectors:
            yield frame.locator(selector).first


def date_locators(page: Page, label_pattern: re.Pattern) -> Iterable[Locator]:
    selectors = [
        "input[name*='arrival' i]",
        "input[id*='arrival' i]",
        "input[aria-label*='arrival' i]",
        "input[placeholder*='arrival' i]",
        "input[name*='checkin' i]",
        "input[id*='checkin' i]",
        "input[aria-label*='check in' i]",
        "input[placeholder*='check in' i]",
        "input[name*='check-in' i]",
        "input[id*='check-in' i]",
        "input[aria-label*='check-in' i]",
        "input[placeholder*='check-in' i]",
        "input[type='date']",
    ]

    for frame in page.frames:
        yield frame.get_by_label(label_pattern).first
        yield frame.get_by_placeholder(label_pattern).first
        yield frame.get_by_role("textbox", name=label_pattern).first
        yield frame.get_by_role("combobox", name=label_pattern).first
        for selector in selectors:
            yield frame.locator(selector).first


def try_fill_date(locator: Locator, iso_value: str) -> bool:
    for value in date_formats(iso_value):
        if try_fill(locator, value):
            return True
    return False


def try_fill(locator: Locator, value: str, press_enter: bool = False) -> bool:
    try:
        locator.wait_for(state="visible", timeout=1_500)
        locator.click(timeout=1_500)
        locator.fill(value, timeout=2_500)
        if press_enter:
            locator.press("Enter", timeout=1_000)
        else:
            locator.press("Tab", timeout=1_000)
        return True
    except Error:
        return try_js_fill(locator, value, press_enter)


def try_js_fill(locator: Locator, value: str, press_enter: bool = False) -> bool:
    try:
        locator.wait_for(state="visible", timeout=700)
        locator.evaluate(
            """
            (element, value) => {
              const tag = element.tagName.toLowerCase();
              const fillable = ['input', 'textarea', 'select'].includes(tag) ||
                element.getAttribute('contenteditable') === 'true';
              if (!fillable) {
                throw new Error('Element is not fillable');
              }
              element.focus();
              if (element.getAttribute('contenteditable') === 'true') {
                element.textContent = value;
              } else {
                element.value = value;
              }
              element.dispatchEvent(new Event('input', { bubbles: true }));
              element.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """,
            value,
        )
        if press_enter:
            locator.press("Enter", timeout=1_000)
        else:
            locator.press("Tab", timeout=1_000)
        return True
    except Error:
        return False


def try_select_option(locator: Locator, label: str) -> bool:
    try:
        locator.wait_for(state="visible", timeout=1_000)
        locator.select_option(label=label, timeout=2_000)
        return True
    except Error:
        return False


def try_click(locator: Locator) -> bool:
    try:
        locator.wait_for(state="visible", timeout=1_500)
        locator.click(timeout=2_500)
        return True
    except Error:
        return False


def try_check(locator: Locator) -> bool:
    try:
        locator.wait_for(state="visible", timeout=1_500)
        locator.check(timeout=2_500)
        return True
    except Error:
        return False


def click_destination_autocomplete_option(page: Page, destination: str) -> None:
    pattern = re.compile(re.escape(destination), re.I)
    time.sleep(0.3)
    for frame in page.frames:
        locators = [
            frame.get_by_role("option", name=pattern).first,
            frame.locator("[role='listbox'] [role='option']").filter(has_text=pattern).first,
            frame.locator("[role='menu'] [role='menuitem']").filter(has_text=pattern).first,
            frame.locator(".pac-item, .ui-menu-item, [class*='suggest' i], [class*='autocomplete' i]")
            .filter(has_text=pattern)
            .first,
        ]
        for locator in locators:
            if try_click_autocomplete_option(locator):
                return


def try_click_autocomplete_option(locator: Locator) -> bool:
    try:
        locator.wait_for(state="visible", timeout=900)
        is_safe = locator.evaluate(
            """
            element => {
              const unsafeSelector = [
                'a[href]',
                'table',
                '[role="grid"]',
                '[role="table"]',
                '[data-property]',
                '[class*="property" i]',
                '[class*="hotel" i]'
              ].join(',');
              if (element.matches('a[href]')) return false;
              if (element.closest(unsafeSelector)) return false;
              const optionSelector = [
                '[role="option"]',
                '[role="listbox"]',
                '[role="menu"]',
                '[role="menuitem"]',
                '.pac-item',
                '.ui-menu-item',
                '[class*="suggest" i]',
                '[class*="autocomplete" i]'
              ].join(',');
              return Boolean(element.matches(optionSelector) || element.closest(optionSelector));
            }
            """
        )
        if not is_safe:
            return False
        locator.click(timeout=1_500)
        return True
    except Error:
        return False


def try_fill_app_filter(page: Page) -> None:
    for frame in page.frames:
        for locator in (
            frame.get_by_placeholder(re.compile(r"filter applications", re.I)).first,
            frame.get_by_role("textbox", name=re.compile(r"filter", re.I)).first,
            frame.locator("input[placeholder*='filter' i]").first,
        ):
            if try_fill(locator, "Colleague Discount Room Availability"):
                return


def text_exists_in_any_frame(page: Page, pattern: re.Pattern) -> bool:
    for frame in page.frames:
        try:
            if frame.get_by_text(pattern).first.count() > 0:
                return True
        except Error:
            continue
    return False


def click_text_in_any_frame(page: Page, pattern: re.Pattern) -> None:
    for frame in page.frames:
        link_locator = frame.get_by_role("link", name=pattern).first
        if try_click(link_locator):
            return
        text_locator = frame.get_by_text(pattern).first
        if try_click(text_locator):
            return
    raise TimeoutError("Could not click the Colleague Discount Room Availability link.")


def date_formats(iso_value: str) -> list[str]:
    parsed = date.fromisoformat(iso_value)
    return [
        parsed.strftime("%m/%d/%Y"),
        f"{parsed.month}/{parsed.day}/{parsed.year}",
        parsed.isoformat(),
        parsed.strftime("%d/%m/%Y"),
    ]


def settle_page(page: Page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=30_000)
    except Error:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Error:
        pass


def safe_url(page: Page) -> str:
    try:
        return page.url
    except Error:
        return ""


def wait_until_browser_closed(context: BrowserContext, should_stop: ShouldStop) -> None:
    while True:
        try:
            check_stop(should_stop)
            if not context.pages:
                return
            time.sleep(1)
        except Error:
            return


TABLE_MATRIX_SCRIPT = """
tables => tables.map(table => {
  const rows = Array.from(table.querySelectorAll('tr'));
  return rows.map(row => {
    const cells = Array.from(row.querySelectorAll('th,td'));
    return cells.map(cell => (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim());
  }).filter(row => row.some(Boolean));
}).filter(matrix => matrix.length > 1)
"""


ROLE_GRID_MATRIX_SCRIPT = """
grids => grids.map(grid => {
  const rows = Array.from(grid.querySelectorAll('[role="row"]'));
  return rows.map(row => {
    const cells = Array.from(row.querySelectorAll('[role="columnheader"],[role="rowheader"],[role="cell"],[role="gridcell"]'));
    return cells.map(cell => (cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim());
  }).filter(row => row.some(Boolean));
}).filter(matrix => matrix.length > 1)
"""


COUNTER_SCRIPT = """
({ label, target }) => {
  const normalize = value => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const visible = element => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const labelText = normalize(label);
  const labelElements = Array.from(document.querySelectorAll('body *'))
    .filter(element => visible(element) && normalize(element.textContent) === labelText)
    .sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (ar.width * ar.height) - (br.width * br.height);
    });

  let container = labelElements[0] || null;
  for (let i = 0; container && i < 5; i += 1) {
    const text = normalize(container.innerText || container.textContent);
    const hasNumber = /\\b\\d+\\b/.test(text);
    const controls = Array.from(container.querySelectorAll('button,a,[role="button"],span,i'))
      .filter(visible)
      .filter(element => {
        const name = normalize([
          element.innerText,
          element.getAttribute('aria-label'),
          element.getAttribute('title'),
          element.className
        ].join(' '));
        return name === '+' || name === '-' || name.includes('plus') || name.includes('minus') ||
          name.includes('add') || name.includes('remove') || name.includes('increment') ||
          name.includes('decrement');
      });
    if (hasNumber && controls.length) {
      break;
    }
    container = container.parentElement;
  }

  if (!container) {
    return { done: false, clicked: false };
  }

  const text = container.innerText || container.textContent || '';
  const labelIndex = normalize(text).indexOf(labelText);
  const beforeLabel = labelIndex >= 0 ? text.slice(0, labelIndex) : text;
  const previousNumbers = Array.from(beforeLabel.matchAll(/\\b\\d+\\b/g)).map(match => Number(match[0]));
  const allNumbers = Array.from(text.matchAll(/\\b\\d+\\b/g)).map(match => Number(match[0]));
  const current = previousNumbers.length ? previousNumbers[previousNumbers.length - 1] : allNumbers[0];

  if (current === target) {
    return { done: true, clicked: false, current };
  }
  if (typeof current !== 'number' || Number.isNaN(current)) {
    return { done: false, clicked: false };
  }

  const controls = Array.from(container.querySelectorAll('button,a,[role="button"],span,i')).filter(visible);
  const plus = controls.find(element => {
    const name = normalize([element.innerText, element.getAttribute('aria-label'), element.getAttribute('title'), element.className].join(' '));
    return name === '+' || name.includes('plus') || name.includes('add') || name.includes('increment');
  });
  const minus = controls.find(element => {
    const name = normalize([element.innerText, element.getAttribute('aria-label'), element.getAttribute('title'), element.className].join(' '));
    return name === '-' || name.includes('minus') || name.includes('remove') || name.includes('decrement');
  });
  const button = current < target ? plus : minus;
  if (!button) {
    return { done: false, clicked: false, current };
  }

  button.click();
  return { done: false, clicked: true, current };
}
"""
