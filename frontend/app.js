(function () {
  // Cache DOM references once for render/update steps.
  const bodyEl = document.getElementById("movers-body");
  const statusEl = document.getElementById("status");
  const yearEl = document.getElementById("year");
  const coverageEl = document.getElementById("coverage-value");
  const closedDaysEl = document.getElementById("closed-days");
  const missingDaysEl = document.getElementById("missing-days");
  const largestEl = document.getElementById("metric-largest");
  const frequentEl = document.getElementById("metric-frequent");
  const averageEl = document.getElementById("metric-average");

  if (yearEl) {
    yearEl.textContent = new Date().getFullYear().toString();
  }

  // Shared formatting helpers.
  function formatPercent(value) {
    const rounded = Number(value).toFixed(2);
    const withSign = value > 0 ? `+${rounded}` : rounded;
    return `${withSign}%`;
  }

  function formatMoney(value) {
    return Number(value).toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    });
  }

  function parseIsoDate(value) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day));
  }

  function toIsoDate(dateObj) {
    return dateObj.toISOString().slice(0, 10);
  }

  function formatDateList(values) {
    if (!values.length) {
      return "None";
    }
    return values
      .map((value) => {
        const dateObj = parseIsoDate(value);
        return dateObj.toLocaleDateString(undefined, {
          month: "short",
          day: "2-digit",
          timeZone: "UTC",
        });
      })
      .join(", ");
  }

  function addDaysUtc(dateObj, days) {
    const copy = new Date(dateObj);
    copy.setUTCDate(copy.getUTCDate() + days);
    return copy;
  }

  function getExpectedWeekdays(latestIsoDate, count) {
    const results = [];
    let cursor = parseIsoDate(latestIsoDate);

    while (results.length < count) {
      const weekday = cursor.getUTCDay();
      if (weekday !== 0 && weekday !== 6) {
        results.push(toIsoDate(cursor));
      }
      cursor = addDaysUtc(cursor, -1);
    }
    return results;
  }

  function getWeekendDaysBetween(startIsoDate, endIsoDate) {
    const start = parseIsoDate(startIsoDate);
    const end = parseIsoDate(endIsoDate);
    const weekends = [];

    for (let cursor = start; cursor <= end; cursor = addDaysUtc(cursor, 1)) {
      const weekday = cursor.getUTCDay();
      if (weekday === 0 || weekday === 6) {
        weekends.push(toIsoDate(cursor));
      }
    }
    return weekends;
  }

  function getCoverageDetails(items) {
    const dateSet = new Set(items.map((item) => item.date));
    const sortedDates = [...dateSet].sort();
    const latestDate = sortedDates[sortedDates.length - 1];
    const expectedTradingDates = getExpectedWeekdays(latestDate, 7);
    const missingTradingDays = expectedTradingDates.filter((dateValue) => !dateSet.has(dateValue));
    const earliestExpected = expectedTradingDates[expectedTradingDates.length - 1];
    const weekendClosedDays = getWeekendDaysBetween(earliestExpected, latestDate);

    return {
      expectedCount: expectedTradingDates.length,
      presentCount: expectedTradingDates.length - missingTradingDays.length,
      missingTradingDays,
      weekendClosedDays,
    };
  }

  function renderDataQuality(items) {
    // Coverage uses expected weekday trading dates for the latest window.
    const details = getCoverageDetails(items);
    coverageEl.textContent = `${details.presentCount}/${details.expectedCount} trading days`;
    closedDaysEl.textContent = formatDateList(details.weekendClosedDays);
    missingDaysEl.textContent = formatDateList(details.missingTradingDays);
  }

  function renderMetrics(items) {
    // Metric 1: largest absolute move in the current payload.
    const largestMove = items.reduce((winner, current) => {
      if (!winner) {
        return current;
      }
      return Math.abs(Number(current.percentChange)) > Math.abs(Number(winner.percentChange)) ? current : winner;
    }, null);

    const tickerCounts = {};
    items.forEach((item) => {
      const ticker = item.ticker;
      tickerCounts[ticker] = (tickerCounts[ticker] || 0) + 1;
    });

    // Metric 2: most frequent winner ticker.
    const mostFrequent = Object.entries(tickerCounts).sort((a, b) => {
      if (b[1] !== a[1]) {
        return b[1] - a[1];
      }
      return a[0].localeCompare(b[0]);
    })[0];

    // Metric 3: average absolute percent move.
    const avgAbsMove =
      items.reduce((sum, item) => sum + Math.abs(Number(item.percentChange)), 0) / Math.max(items.length, 1);

    largestEl.textContent = largestMove
      ? `${largestMove.ticker} (${formatPercent(Number(largestMove.percentChange))})`
      : "N/A";
    frequentEl.textContent = mostFrequent ? `${mostFrequent[0]} (${mostFrequent[1]} days)` : "N/A";
    averageEl.textContent = `${avgAbsMove.toFixed(2)}%`;
  }

  function renderRows(items) {
    bodyEl.innerHTML = "";
    items.forEach((item) => {
      const tr = document.createElement("tr");
      const pctClass = Number(item.percentChange) >= 0 ? "gain" : "loss";

      tr.innerHTML = `
        <td>${item.date}</td>
        <td class="ticker">${item.ticker}</td>
        <td class="pct ${pctClass}">${formatPercent(Number(item.percentChange))}</td>
        <td>${formatMoney(Number(item.closingPrice))}</td>
      `;
      bodyEl.appendChild(tr);
    });
  }

  async function load() {
    try {
      // config.js is generated by Terraform and provides the API stage URL.
      const apiBaseUrl = window.APP_CONFIG && window.APP_CONFIG.apiBaseUrl;
      if (!apiBaseUrl) {
        throw new Error("Missing API base URL. Check frontend/config.js.");
      }

      const response = await fetch(`${apiBaseUrl}/movers`);
      if (!response.ok) {
        throw new Error(`API request failed with status ${response.status}`);
      }

      const payload = await response.json();
      const items = Array.isArray(payload) ? payload : payload.items;

      if (!items || items.length === 0) {
        statusEl.textContent = "No data yet. Wait for the first scheduled ingestion run.";
        return;
      }

      // Render table + derived summary panels from the same source payload.
      renderRows(items);
      renderDataQuality(items);
      renderMetrics(items);
      statusEl.textContent = "";
    } catch (error) {
      statusEl.textContent = `Unable to load movers: ${error.message}`;
      statusEl.classList.add("error");
    }
  }

  load();
})();
