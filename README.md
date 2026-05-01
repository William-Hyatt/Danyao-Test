# HyattConnect Colleague Availability App

This local app uses Python Playwright to open HyattConnect, click **Colleague Discount Room Availability**, then scan single-night **Colleague Comp** availability for Tokyo or another destination across a date period.

## Run

```powershell
py app.py
```

Open:

```text
http://127.0.0.1:8765
```

The app sets the availability site to 1 night, 1 room, 1 adult, 0 children, and Colleague Comp. Results are summarized as:

```text
Property | Dates with complimentary room nights available
```

When availability appears during a scan, the app shows a browser alert immediately. Use **Stop** to request cancellation of the current scan; it will stop at the next browser checkpoint. Enable **Repeat** and set hours/minutes to rerun the same scan automatically while the app page remains open.

The first run may ask you to sign in to HyattConnect in the Playwright browser. The app uses a persistent local browser profile at `.playwright/hyatt-profile`, so your session can be reused.

## Notes

- The browser channel defaults to Microsoft Edge: `HYATT_BROWSER_CHANNEL=msedge`.
- To use bundled Chromium instead, install it with `py -m playwright install chromium`, then run with `HYATT_BROWSER_CHANNEL=` unset or set to a Chromium channel available on your machine.
- If the internal Hyatt availability page changes labels, update the selector patterns in `hyatt_availability_automation.py`.
