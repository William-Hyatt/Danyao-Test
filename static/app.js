const form = document.querySelector("#search-form");
const startDate = document.querySelector("#start-date");
const endDate = document.querySelector("#end-date");
const destination = document.querySelector("#destination");
const shoulderDays = document.querySelector("#shoulder-days");
const keepOpen = document.querySelector("#keep-open");
const startButton = document.querySelector("#start-button");
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
let latestRows = [];

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

function renderJob(job) {
  const destinationText = job.params.destination;
  const dates = `${job.params.start_date} through ${job.params.end_date}`;
  const windowText = job.result ? ` (${job.result.completedWindows}/${job.result.totalWindows})` : "";
  jobLine.textContent = `#${job.id} ${job.status}${windowText} - ${destinationText}, ${dates}`;
  setStatus(job.status);
  renderResults(job.result);

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

  const running = job.status === "queued" || job.status === "running";
  startButton.disabled = running;
  startButton.textContent = running ? "Scanning" : "Scan Nights";

  if (!running && pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
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

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  syncEndMinimum();
  renderResults(null);

  const payload = {
    destination: destination.value.trim() || "Tokyo",
    startDate: startDate.value,
    endDate: endDate.value,
    shoulderDays: Number(shoulderDays.value),
    keepOpen: keepOpen.checked,
  };

  startButton.disabled = true;
  setStatus("Queued");
  jobLine.textContent = "Starting browser automation";

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
    return;
  }

  activeJobId = data.jobId;
  renderJob(data.job);
  pollTimer = setInterval(pollJob, 1500);
});

startDate.addEventListener("change", syncEndMinimum);

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
  renderResults(null);
  setStatus("Ready");
});

setDefaults();
