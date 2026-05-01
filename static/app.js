const form = document.querySelector("#search-form");
const startDate = document.querySelector("#start-date");
const endDate = document.querySelector("#end-date");
const destination = document.querySelector("#destination");
const shoulderDays = document.querySelector("#shoulder-days");
const keepOpen = document.querySelector("#keep-open");
const repeatEnabled = document.querySelector("#repeat-enabled");
const repeatHours = document.querySelector("#repeat-hours");
const repeatMinutes = document.querySelector("#repeat-minutes");
const repeatStatus = document.querySelector("#repeat-status");
const startButton = document.querySelector("#start-button");
const stopButton = document.querySelector("#stop-button");
const clearButton = document.querySelector("#clear-button");
const copyButton = document.querySelector("#copy-button");
const statusPill = document.querySelector("#status-pill");
const jobLine = document.querySelector("#job-line");
const logList = document.querySelector("#log-list");
const resultLink = document.querySelector("#result-link");
const resultsBody = document.querySelector("#results-body");
const summaryLine = document.querySelector("#summary-line");

let activeJobId = null;
let pollTimer = null;
let repeatTimer = null;
let countdownTimer = null;
let nextRunAt = null;
let repeatActive = false;
let latestRows = [];
let lastAlertedNightCount = 0;
const scheduledJobIds = new Set();

function isoDate(offsetDays) {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  return value.toISOString().slice(0, 10);
}

function setDefaults() {
  const tomorrow = isoDate(1);
  const thirtyDays = isoDate(30);
  startDate.min = isoDate(0);
  endDate.min = tomorrow;
  startDate.value = tomorrow;
  endDate.value = thirtyDays;
}

function syncEndMinimum() {
  endDate.min = startDate.value;
  if (!endDate.value || endDate.value < startDate.value) {
    endDate.value = startDate.value;
  }
}

function setStatus(status) {
  statusPill.textContent = status;
  statusPill.dataset.status = status.toLowerCase();
}

function formatDate(isoValue) {
  const [year, month, day] = isoValue.split("-");
  return `${month}/${day}/${year}`;
}

function getRepeatMs() {
  const hours = Math.max(0, Number(repeatHours.value || 0));
  const minutes = Math.max(0, Number(repeatMinutes.value || 0));
  return ((hours * 60) + minutes) * 60 * 1000;
}

function formatDuration(ms) {
  const totalMinutes = Math.max(0, Math.ceil(ms / 60000));
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours && minutes) {
    return `${hours}h ${minutes}m`;
  }
  if (hours) {
    return `${hours}h`;
  }
  return `${minutes}m`;
}

function buildPayload() {
  syncEndMinimum();
  return {
    destination: destination.value.trim() || "Tokyo",
    startDate: startDate.value,
    endDate: endDate.value,
    shoulderDays: Number(shoulderDays.value),
    keepOpen: keepOpen.checked,
  };
}

function renderResults(result) {
  latestRows = result?.availability || [];
  copyButton.disabled = latestRows.length === 0;

  if (!result) {
    summaryLine.textContent = "No scan results yet";
  } else {
    const windowText = `${result.completedWindows || 0}/${result.totalWindows || 0} windows`;
    const nightText = `${result.availableNightCount || 0} night${result.availableNightCount === 1 ? "" : "s"}`;
    const propertyText = `${result.propertyCount || 0} propert${result.propertyCount === 1 ? "y" : "ies"}`;
    summaryLine.textContent = `${nightText} across ${propertyText} - ${windowText}`;
  }

  if (!latestRows.length) {
    resultsBody.replaceChildren(emptyResultsRow("No available nights collected yet"));
    return;
  }

  resultsBody.replaceChildren(
    ...latestRows.map((row) => {
      const tableRow = document.createElement("tr");
      const property = document.createElement("td");
      const dates = document.createElement("td");
      property.textContent = row.property;
      dates.textContent = row.dates.map(formatDate).join(", ");
      tableRow.append(property, dates);
      return tableRow;
    }),
  );
}

function emptyResultsRow(message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 2;
  cell.className = "empty-cell";
  cell.textContent = message;
  row.append(cell);
  return row;
}

function maybeAlertAvailability(job) {
  const result = job.result;
  const availableNightCount = result?.availableNightCount || 0;
  if (availableNightCount <= lastAlertedNightCount) {
    return;
  }

  lastAlertedNightCount = availableNightCount;
  const lines = (result.availability || []).slice(0, 8).map((row) => {
    return `${row.property}: ${row.dates.map(formatDate).join(", ")}`;
  });
  const moreText = result.propertyCount > 8 ? `\n...and ${result.propertyCount - 8} more properties` : "";
  window.alert(`Complimentary room night availability found.\n\n${lines.join("\n")}${moreText}`);
}

function renderJob(job) {
  const destinationText = job.params.destination;
  const dates = `${job.params.start_date} through ${job.params.end_date}`;
  const windowText = job.result ? ` (${job.result.completedWindows}/${job.result.totalWindows})` : "";
  jobLine.textContent = `#${job.id} ${job.status}${windowText} - ${destinationText}, ${dates}`;
  setStatus(job.status);
  renderResults(job.result);
  maybeAlertAvailability(job);

  logList.replaceChildren(
    ...job.logs.map((entry) => {
      const item = document.createElement("li");
      const time = document.createElement("time");
      const message = document.createElement("span");
      time.textContent = entry.time;
      message.textContent = entry.message;
      item.append(time, message);
      return item;
    }),
  );

  const url = job.result?.url;
  if (url) {
    resultLink.href = url;
    resultLink.classList.remove("hidden");
  } else {
    resultLink.classList.add("hidden");
  }

  const queuedOrRunning = job.status === "queued" || job.status === "running";
  const busy = queuedOrRunning || job.status === "stopping";
  startButton.disabled = busy;
  startButton.textContent = busy ? "Scanning" : "Scan Nights";
  stopButton.disabled = !queuedOrRunning;

  if (!busy && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }

  if (!busy) {
    scheduleRepeatAfterJob(job);
  }
}

async function pollJob() {
  if (!activeJobId) {
    return;
  }

  const response = await fetch(`/api/jobs/${activeJobId}`);
  const job = await response.json();
  renderJob(job);
}

async function startScan({ manual = true } = {}) {
  if (manual) {
    clearRepeatTimer();
    repeatActive = repeatEnabled.checked;
  }

  if (repeatActive && getRepeatMs() <= 0) {
    setStatus("Failed");
    jobLine.textContent = "Repeat interval must be at least 1 minute.";
    repeatActive = false;
    updateRepeatStatus();
    return;
  }

  lastAlertedNightCount = 0;
  renderResults(null);

  const payload = buildPayload();
  startButton.disabled = true;
  stopButton.disabled = true;
  setStatus("Queued");
  jobLine.textContent = "Starting browser automation";
  updateRepeatStatus();

  const response = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  const data = await response.json();
  if (!response.ok) {
    setStatus("Failed");
    jobLine.textContent = data.error || "Could not start scan";
    startButton.disabled = false;
    stopButton.disabled = true;
    scheduleRepeatFailure();
    return;
  }

  activeJobId = data.jobId;
  renderJob(data.job);
  if (pollTimer) {
    clearInterval(pollTimer);
  }
  pollTimer = setInterval(pollJob, 1500);
}

async function stopCurrentScan() {
  if (!activeJobId) {
    return;
  }

  repeatActive = false;
  clearRepeatTimer();
  stopButton.disabled = true;
  setStatus("Stopping");
  jobLine.textContent = "Stopping current scan";

  const response = await fetch(`/api/jobs/${activeJobId}/stop`, { method: "POST" });
  if (response.ok) {
    const data = await response.json();
    renderJob(data.job);
  }
}

function scheduleRepeatAfterJob(job) {
  if (scheduledJobIds.has(job.id)) {
    return;
  }
  scheduledJobIds.add(job.id);

  if (!repeatActive || job.status === "stopped") {
    updateRepeatStatus();
    return;
  }

  scheduleNextRepeat();
}

function scheduleRepeatFailure() {
  if (repeatActive) {
    scheduleNextRepeat();
  }
}

function scheduleNextRepeat() {
  const intervalMs = getRepeatMs();
  if (!repeatActive || intervalMs <= 0) {
    updateRepeatStatus();
    return;
  }

  clearRepeatTimer();
  nextRunAt = Date.now() + intervalMs;
  repeatTimer = setTimeout(() => {
    repeatTimer = null;
    nextRunAt = null;
    startScan({ manual: false });
  }, intervalMs);
  countdownTimer = setInterval(updateRepeatStatus, 1000);
  updateRepeatStatus();
}

function clearRepeatTimer() {
  if (repeatTimer) {
    clearTimeout(repeatTimer);
    repeatTimer = null;
  }
  if (countdownTimer) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
  nextRunAt = null;
  updateRepeatStatus();
}

function updateRepeatStatus() {
  if (!repeatActive && !repeatEnabled.checked) {
    repeatStatus.textContent = "Repeat off";
    return;
  }

  if (nextRunAt) {
    repeatStatus.textContent = `Next scan in ${formatDuration(nextRunAt - Date.now())}`;
    return;
  }

  const intervalMs = getRepeatMs();
  if (repeatEnabled.checked) {
    repeatStatus.textContent = intervalMs > 0 ? `Repeat every ${formatDuration(intervalMs)}` : "Repeat interval needed";
  } else {
    repeatStatus.textContent = "Repeat paused";
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  startScan({ manual: true });
});

stopButton.addEventListener("click", stopCurrentScan);

startDate.addEventListener("change", syncEndMinimum);
repeatEnabled.addEventListener("change", () => {
  repeatActive = repeatEnabled.checked && Boolean(repeatTimer);
  if (!repeatEnabled.checked) {
    repeatActive = false;
    clearRepeatTimer();
  }
  updateRepeatStatus();
});
repeatHours.addEventListener("input", updateRepeatStatus);
repeatMinutes.addEventListener("input", updateRepeatStatus);

copyButton.addEventListener("click", async () => {
  if (!latestRows.length) {
    return;
  }

  const header = "Property,Dates with complimentary room nights available";
  const rows = latestRows.map((row) => {
    const dates = row.dates.map(formatDate).join("; ");
    return `"${row.property.replaceAll('"', '""')}","${dates}"`;
  });

  await navigator.clipboard.writeText([header, ...rows].join("\n"));
  copyButton.textContent = "Copied";
  setTimeout(() => {
    copyButton.textContent = "Copy CSV";
  }, 1400);
});

clearButton.addEventListener("click", () => {
  logList.replaceChildren();
  jobLine.textContent = "No scan running";
  resultLink.classList.add("hidden");
  lastAlertedNightCount = 0;
  renderResults(null);
  setStatus("Ready");
});

setDefaults();
updateRepeatStatus();
