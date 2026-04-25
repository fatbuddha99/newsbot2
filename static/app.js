const state = {
  focusMode: true,
  query: "",
  activeView: "insight",
  llmMode: "auto",
  currentScanToken: 0,
  deepDiveLoadedFor: "",
  narrativeShiftLoadedFor: "",
  lastInsightAnalysis: null,
  lastInsightQuery: "",
  lastDeepDiveData: null,
  lastDeepDiveQuery: "",
  lastNarrativeShiftData: null,
  lastNarrativeShiftQuery: "",
};

const terminalLog = document.getElementById("terminalLog");
const headlinesList = document.getElementById("headlinesList");
const analysisOutput = document.getElementById("analysisOutput");
const analysisTitle = document.getElementById("analysisTitle");
const analysisStatusBadge = document.getElementById("analysisStatusBadge");
const engineValue = document.getElementById("engineValue");
const queryInput = document.getElementById("queryInput");
const modeValue = document.getElementById("modeValue");
const storyCount = document.getElementById("storyCount");
const refreshStatus = document.getElementById("refreshStatus");
const focusToggle = document.getElementById("focusToggle");
const refreshButton = document.getElementById("refreshButton");
const llmToggle = document.getElementById("llmToggle");
const commandForm = document.getElementById("commandForm");
const analysisSwitcher = document.querySelector(".analysis-switcher");
const insightViewButton = document.getElementById("insightViewButton");
const deepDiveViewButton = document.getElementById("deepDiveViewButton");
const narrativeShiftViewButton = document.getElementById("narrativeShiftViewButton");
const analysisView = document.getElementById("analysisView");
const deepDiveView = document.getElementById("deepDiveView");
const narrativeShiftView = document.getElementById("narrativeShiftView");
const deepDiveEmpty = document.getElementById("deepDiveEmpty");
const deepDiveContent = document.getElementById("deepDiveContent");
const deepDiveHeader = document.getElementById("deepDiveHeader");
const deepDiveMetrics = document.getElementById("deepDiveMetrics");
const deepDiveGrowthSignal = document.getElementById("deepDiveGrowthSignal");
const deepDiveTableBody = document.getElementById("deepDiveTableBody");
const deepDiveHeadlineContext = document.getElementById("deepDiveHeadlineContext");
const deepDiveSections = document.getElementById("deepDiveSections");
const narrativeShiftEmpty = document.getElementById("narrativeShiftEmpty");
const narrativeShiftContent = document.getElementById("narrativeShiftContent");
const narrativeShiftHeader = document.getElementById("narrativeShiftHeader");
const narrativeShiftMetrics = document.getElementById("narrativeShiftMetrics");
const narrativeShiftHeadlines = document.getElementById("narrativeShiftHeadlines");
const narrativeShiftSections = document.getElementById("narrativeShiftSections");
const AUTO_REFRESH_MS = 15 * 60 * 1000;
const LLM_MODES = ["auto", "gemini", "openai"];

function queryLooksLikeTickerOrCompany(query) {
  const trimmed = (query || "").trim();
  if (!trimmed) {
    return false;
  }
  if (/^[A-Z.\-]{1,6}$/.test(trimmed)) {
    return true;
  }
  return trimmed.split(/\s+/).length <= 3 && !/\b(and|or|with|for|from|today|news|latest)\b/i.test(trimmed);
}

function llmModeLabel(mode) {
  return (mode || "auto").toUpperCase();
}

function syncAnalysisSwitcher() {
  if (!analysisSwitcher) {
    return;
  }
  if (queryLooksLikeTickerOrCompany(state.query)) {
    analysisSwitcher.prepend(deepDiveViewButton);
    if (deepDiveViewButton.nextElementSibling !== insightViewButton) {
      deepDiveViewButton.after(insightViewButton);
    }
  } else {
    analysisSwitcher.prepend(insightViewButton);
    if (insightViewButton.nextElementSibling !== deepDiveViewButton) {
      insightViewButton.after(deepDiveViewButton);
    }
  }
  if (analysisSwitcher.lastElementChild !== narrativeShiftViewButton) {
    analysisSwitcher.append(narrativeShiftViewButton);
  }
}

function logLine(text, tone = "neutral") {
  const line = document.createElement("div");
  line.className = `terminal-line terminal-line--${tone}`;
  line.textContent = text;
  terminalLog.prepend(line);
}

function renderHeadlines(items) {
  headlinesList.innerHTML = "";

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "headline-card headline-card--empty";
    empty.textContent = "No stories matched the current scan.";
    headlinesList.append(empty);
    return;
  }

  items.forEach((item, index) => {
    const card = document.createElement("article");
    card.className = "headline-card";

    const scoreClass = item.signalScore > 0 ? "score--high" : item.signalScore < 0 ? "score--low" : "score--flat";
    const matches = Array.isArray(item.scoreMatches) && item.scoreMatches.length
      ? item.scoreMatches.join(" | ")
      : "No matched heuristics";

    card.innerHTML = `
      <div class="headline-top">
        <span class="headline-rank">${String(index + 1).padStart(2, "0")}</span>
        <span class="headline-source">${item.source}</span>
        <span class="headline-score ${scoreClass}">SIG ${item.signalScore}</span>
      </div>
      <a class="headline-title" href="${item.link}" target="_blank" rel="noreferrer">${item.title}</a>
      <div class="headline-meta">
        <span>${item.pubDate || "No timestamp"}</span>
        <span>${matches}</span>
      </div>
    `;

    headlinesList.append(card);
  });
}

function renderAnalysis(analysis) {
  if (!analysis) {
    analysisOutput.textContent = "No analysis returned.";
    return;
  }

  if (analysis.ok) {
    analysisOutput.textContent = analysis.text;
    return;
  }

  analysisOutput.textContent = analysis.error || "Analysis unavailable.";
}

function setInsightLoading(isLoading) {
  if (!analysisStatusBadge) {
    return;
  }
  const show = isLoading && state.activeView === "insight";
  analysisStatusBadge.classList.toggle("analysis-status-badge--hidden", !show);
}

function setActiveView(view) {
  state.activeView = view;
  analysisTitle.textContent = view === "insight"
    ? "Strategic Insight"
    : view === "deepDive"
      ? "Financial Deep Dive"
      : "Narrative Shift";
  analysisView.classList.toggle("analysis-view--hidden", view !== "insight");
  deepDiveView.classList.toggle("analysis-view--hidden", view !== "deepDive");
  narrativeShiftView.classList.toggle("analysis-view--hidden", view !== "narrativeShift");
  insightViewButton.classList.toggle("is-active", view === "insight");
  deepDiveViewButton.classList.toggle("is-active", view === "deepDive");
  narrativeShiftViewButton.classList.toggle("is-active", view === "narrativeShift");
  setInsightLoading(false);
  if (view === "insight") {
    if (state.lastInsightAnalysis && state.lastInsightQuery === state.query) {
      renderAnalysis(state.lastInsightAnalysis);
    } else if (state.query) {
      renderAnalysis({ error: "Insight is still loading for this query." });
    }
  }
  syncAnalysisSwitcher();
}

function renderDeepDive(data) {
  deepDiveTableBody.innerHTML = "";
  deepDiveSections.innerHTML = "";
  deepDiveMetrics.innerHTML = "";
  deepDiveHeadlineContext.innerHTML = "";
  deepDiveGrowthSignal.innerHTML = "";

  if (!data || !data.ok) {
    deepDiveEmpty.textContent = (data && data.error) || "Search a ticker or company, then open Deep Dive.";
    deepDiveEmpty.classList.remove("deep-dive-empty--hidden");
    deepDiveContent.classList.add("deep-dive-content--hidden");
    return;
  }

  deepDiveEmpty.classList.add("deep-dive-empty--hidden");
  deepDiveContent.classList.remove("deep-dive-content--hidden");

  const company = data.company || {};
  const metrics = data.metrics || {};
  const sections = (data.narrative && data.narrative.sections) || {};
  const latestHeadlines = data.latestHeadlines || [];
  const headlineAnalysis = data.headlineAnalysis || {};

  deepDiveHeader.innerHTML = `
    <div>
      <p class="eyebrow">Financial Analysis</p>
      <h3>${company.longName || company.shortName || company.symbol || "Company"}</h3>
    </div>
    <div class="deep-dive-company-meta">
      <span>${company.symbol || ""}</span>
      <span>${company.sector || "Sector N/A"}</span>
      <span>${company.industry || "Industry N/A"}</span>
    </div>
  `;

  const metricCards = [
    ["Latest Quarter", metrics.latestQuarter || "N/A"],
    ["Revenue", metrics.latestRevenue || "N/A"],
    ["Diluted EPS", metrics.latestEps || "N/A"],
    ["Quarter-End Price", metrics.latestPrice || "N/A"],
    ["Quarter-End TTM P/E", metrics.latestPe || "N/A"],
    ["Current Price", metrics.currentPrice || "N/A"],
    ["Current TTM P/E", metrics.currentPe || "N/A"],
    ["Move vs Quarter-End", metrics.moveVsLastQuarter != null ? `${metrics.moveVsLastQuarter.toFixed(1)}%` : "N/A"],
    ["Valuation Stage", (metrics.peCompressionFrame && metrics.peCompressionFrame.stage) || "N/A"],
  ];
  deepDiveMetrics.innerHTML = metricCards
    .map(
      ([label, value]) => `
        <div class="deep-dive-metric">
          <span>${label}</span>
          <strong>${value}</strong>
        </div>
      `,
    )
    .join("");

  const growthScore = metrics.growthScore != null ? metrics.growthScore : "N/A";
  const growthLabel = metrics.growthLabel || "Neutral";
  const growthClass = typeof growthScore === "number"
    ? growthScore >= 8
      ? "growth-score--excellent"
      : growthScore >= 6
        ? "growth-score--good"
        : growthScore >= 4
          ? "growth-score--neutral"
          : "growth-score--weak"
    : "growth-score--neutral";

  deepDiveGrowthSignal.innerHTML = `
    <div class="growth-signal-card">
      <div class="growth-signal-score ${growthClass}">${growthScore}/10</div>
      <div class="growth-signal-copy">
        <strong>${growthLabel} growth profile</strong>
        <p>${metrics.growthSummary || "Growth signal unavailable."}</p>
      </div>
    </div>
  `;

  (data.rows || []).forEach((row) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${row.label}</td>
      <td>${row.revenue}</td>
      <td>${row.epsDisplay}</td>
      <td>${row.peDisplay}</td>
      <td>${row.closePriceDisplay}</td>
      <td>${row.trendNote}</td>
    `;
    deepDiveTableBody.append(tr);
  });

  const headlineItemsHtml = latestHeadlines.length
    ? latestHeadlines
        .map(
          (item) => `
            <li>
              <a href="${item.link}" target="_blank" rel="noreferrer">${item.title}</a>
              <span>[${item.source}] SIG ${item.signalScore}</span>
            </li>
          `,
        )
        .join("")
    : "<li>No recent ticker headlines found.</li>";

  deepDiveHeadlineContext.innerHTML = `
    <section class="deep-dive-section">
      <h4>Latest Headlines Context</h4>
      <ul class="deep-dive-headline-list">${headlineItemsHtml}</ul>
      <p class="deep-dive-headline-analysis">${sections.headlineContext || (headlineAnalysis.ok ? headlineAnalysis.text : "Headline context analysis unavailable.")}</p>
    </section>
  `;

  const orderedSections = [
    ["What is the company business", sections.business],
    ["How does it make money", sections.money],
    ["Moat vs competition", sections.moat],
    ["Financial performance", sections.financialPerformance],
    ["PE compression frame", sections.valuationFrame || (metrics.peCompressionFrame && metrics.peCompressionFrame.summary)],
    ["Fair value and re-rating lens", sections.fairValue],
    ["Projection and risk", sections.projectionRisk],
  ];
  deepDiveSections.innerHTML = orderedSections
    .map(
      ([title, body], index) => `
        <section class="deep-dive-section">
          <h4>${index + 1}. ${title}</h4>
          <p>${body || "Analysis unavailable."}</p>
        </section>
      `,
    )
    .join("");
}

async function loadDeepDive() {
  const activeQuery = state.query;
  const matchingDeepDiveData = state.lastDeepDiveQuery === activeQuery ? state.lastDeepDiveData : null;

  if (!state.query) {
    renderDeepDive({ ok: false, error: "Enter a ticker or company query first." });
    return;
  }

  if (state.deepDiveLoadedFor === state.query) {
    return;
  }

  renderDeepDive(matchingDeepDiveData || { ok: false, error: "Loading financial deep dive..." });

  try {
    const baseResponse = await fetch(`/api/deep-dive-base?query=${encodeURIComponent(activeQuery)}`);
    if (!baseResponse.ok) {
      throw new Error(`Deep dive base failed with status ${baseResponse.status}`);
    }
    const baseData = await baseResponse.json();
    if (state.query !== activeQuery) {
      return;
    }
    if (baseData.ok) {
      state.lastDeepDiveData = {
        ...baseData,
        narrative: matchingDeepDiveData && matchingDeepDiveData.company && matchingDeepDiveData.company.symbol === baseData.company.symbol
          ? matchingDeepDiveData.narrative
          : undefined,
      };
      state.lastDeepDiveQuery = activeQuery;
      renderDeepDive(state.lastDeepDiveData);
    } else {
      renderDeepDive(baseData);
    }

    const fullResponse = await fetch(`/api/deep-dive?query=${encodeURIComponent(activeQuery)}&llm=${encodeURIComponent(state.llmMode)}`);
    if (!fullResponse.ok) {
      throw new Error(`Deep dive failed with status ${fullResponse.status}`);
    }

    const fullData = await fullResponse.json();
    if (state.query !== activeQuery) {
      return;
    }
    if (fullData.ok) {
      state.lastDeepDiveData = fullData;
      state.lastDeepDiveQuery = activeQuery;
      renderDeepDive(fullData);
      state.deepDiveLoadedFor = activeQuery;
    } else if (matchingDeepDiveData) {
      renderDeepDive(matchingDeepDiveData);
      logLine(`Deep dive unavailable: ${fullData.error}`, "warn");
    } else {
      renderDeepDive(fullData);
      logLine(`Deep dive unavailable: ${fullData.error}`, "warn");
    }
  } catch (error) {
    if (state.query !== activeQuery) {
      return;
    }
    renderDeepDive(matchingDeepDiveData || { ok: false, error: String(error) });
    logLine(`Deep dive error: ${error}`, "error");
  }
}

function renderNarrativeShift(data) {
  narrativeShiftMetrics.innerHTML = "";
  narrativeShiftHeadlines.innerHTML = "";
  narrativeShiftSections.innerHTML = "";

  if (!data || !data.ok) {
    narrativeShiftEmpty.textContent = (data && data.error) || "Search a ticker or company, then open Narrative Shift.";
    narrativeShiftEmpty.classList.remove("deep-dive-empty--hidden");
    narrativeShiftContent.classList.add("deep-dive-content--hidden");
    return;
  }

  narrativeShiftEmpty.classList.add("deep-dive-empty--hidden");
  narrativeShiftContent.classList.remove("deep-dive-content--hidden");

  const company = data.company || {};
  const stageInfo = data.stageInfo || {};
  const metrics = data.metrics || {};
  const sections = (data.analysis && data.analysis.sections) || {};
  const latestHeadlines = data.latestHeadlines || [];

  narrativeShiftHeader.innerHTML = `
    <div>
      <p class="eyebrow">Narrative Re-Rating</p>
      <h3>${company.longName || company.shortName || company.symbol || "Company"}</h3>
    </div>
    <div class="deep-dive-company-meta">
      <span>${company.symbol || ""}</span>
      <span>${stageInfo.stage || "Stage N/A"}</span>
      <span>${company.sector || "Sector N/A"}</span>
    </div>
  `;

  const metricCards = [
    ["Current Stage", stageInfo.stage || "N/A"],
    ["TTM P/E", metrics.latestPe || "N/A"],
    ["Revenue 8Q", metrics.revenueGrowth8Q != null ? `${metrics.revenueGrowth8Q.toFixed(1)}%` : "N/A"],
    ["EPS 8Q", metrics.epsGrowth8Q != null ? `${metrics.epsGrowth8Q.toFixed(1)}%` : "N/A"],
    ["P/E Shift", metrics.peCompression8Q != null ? `${metrics.peCompression8Q.toFixed(1)}%` : "N/A"],
  ];
  narrativeShiftMetrics.innerHTML = metricCards
    .map(
      ([label, value]) => `
        <div class="deep-dive-metric">
          <span>${label}</span>
          <strong>${value}</strong>
        </div>
      `,
    )
    .join("");

  const headlineItemsHtml = latestHeadlines.length
    ? latestHeadlines
        .map(
          (item) => `
            <li>
              <a href="${item.link}" target="_blank" rel="noreferrer">${item.title}</a>
              <span>[${item.source}] SIG ${item.signalScore}</span>
            </li>
          `,
        )
        .join("")
    : "<li>No recent ticker headlines found.</li>";

  narrativeShiftHeadlines.innerHTML = `
    <section class="deep-dive-section">
      <h4>Latest Trigger Headlines</h4>
      <ul class="deep-dive-headline-list">${headlineItemsHtml}</ul>
    </section>
  `;

  const orderedSections = [
    ["Current stage in 6-stage cycle", sections.currentStage || stageInfo.summary],
    ["Old narrative", sections.oldNarrative],
    ["Event", sections.event],
    ["Possible new narrative", sections.possibleNewNarrative],
    ["Does this change fair value?", sections.fairValueChange],
    ["Re-rating verdict", sections.reratingVerdict],
    ["What confirms it?", sections.confirms],
    ["What invalidates it?", sections.invalidates],
  ];
  narrativeShiftSections.innerHTML = orderedSections
    .map(
      ([title, body]) => `
        <section class="deep-dive-section">
          <h4>${title}</h4>
          <p>${body || "Analysis unavailable."}</p>
        </section>
      `,
    )
    .join("");
}

async function loadNarrativeShift() {
  const activeQuery = state.query;
  const matchingNarrativeData = state.lastNarrativeShiftQuery === activeQuery ? state.lastNarrativeShiftData : null;

  if (!state.query) {
    renderNarrativeShift({ ok: false, error: "Enter a ticker or company query first." });
    return;
  }

  if (state.narrativeShiftLoadedFor === state.query) {
    return;
  }

  renderNarrativeShift(matchingNarrativeData || { ok: false, error: "Loading narrative shift analysis..." });

  try {
    const baseResponse = await fetch(`/api/narrative-shift-base?query=${encodeURIComponent(activeQuery)}`);
    if (!baseResponse.ok) {
      throw new Error(`Narrative shift base failed with status ${baseResponse.status}`);
    }
    const baseData = await baseResponse.json();
    if (state.query !== activeQuery) {
      return;
    }
    if (baseData.ok) {
      state.lastNarrativeShiftData = {
        ...baseData,
        analysis: matchingNarrativeData && matchingNarrativeData.company && matchingNarrativeData.company.symbol === baseData.company.symbol
          ? matchingNarrativeData.analysis
          : undefined,
      };
      state.lastNarrativeShiftQuery = activeQuery;
      renderNarrativeShift(state.lastNarrativeShiftData);
    } else {
      renderNarrativeShift(baseData);
    }

    const fullResponse = await fetch(`/api/narrative-shift?query=${encodeURIComponent(activeQuery)}&llm=${encodeURIComponent(state.llmMode)}`);
    if (!fullResponse.ok) {
      throw new Error(`Narrative shift failed with status ${fullResponse.status}`);
    }
    const fullData = await fullResponse.json();
    if (state.query !== activeQuery) {
      return;
    }
    if (fullData.ok) {
      state.lastNarrativeShiftData = fullData;
      state.lastNarrativeShiftQuery = activeQuery;
      renderNarrativeShift(fullData);
      state.narrativeShiftLoadedFor = activeQuery;
    } else if (matchingNarrativeData) {
      renderNarrativeShift(matchingNarrativeData);
      logLine(`Narrative shift unavailable: ${fullData.error}`, "warn");
    } else {
      renderNarrativeShift(fullData);
      logLine(`Narrative shift unavailable: ${fullData.error}`, "warn");
    }
  } catch (error) {
    if (state.query !== activeQuery) {
      return;
    }
    renderNarrativeShift(matchingNarrativeData || { ok: false, error: String(error) });
    logLine(`Narrative shift error: ${error}`, "error");
  }
}

function syncModeUi() {
  modeValue.textContent = state.focusMode ? "FOCUS" : "FULL";
  focusToggle.textContent = `Focus: ${state.focusMode ? "ON" : "OFF"}`;
  refreshStatus.textContent = "Every 15 min";
  if (llmToggle) {
    llmToggle.textContent = `LLM: ${llmModeLabel(state.llmMode)}`;
  }
  if (engineValue) {
    engineValue.textContent = `Python RSS + ${llmModeLabel(state.llmMode)}`;
  }
}

async function runScan() {
  const scanToken = Date.now();
  state.currentScanToken = scanToken;
  const preferDeepDive = queryLooksLikeTickerOrCompany(state.query);

  if (preferDeepDive && state.activeView === "insight") {
    setActiveView("deepDive");
  } else if (!preferDeepDive && state.activeView === "deepDive") {
    setActiveView("insight");
  } else {
    syncAnalysisSwitcher();
  }

  const baseParams = new URLSearchParams({
    focus: state.focusMode ? "1" : "0",
    analysis: "0",
    llm: state.llmMode,
  });

  if (state.query) {
    baseParams.set("query", state.query);
  }

  try {
    const baseResponse = await fetch(`/api/scan?${baseParams.toString()}`);
    if (!baseResponse.ok) {
      throw new Error(`Scan failed with status ${baseResponse.status}`);
    }

    const baseData = await baseResponse.json();
    if (state.currentScanToken !== scanToken) {
      return;
    }

    renderHeadlines(baseData.items || []);
    storyCount.textContent = `${baseData.shownItems || 0} stories`;
    state.deepDiveLoadedFor = "";
    state.narrativeShiftLoadedFor = "";
    if (state.activeView === "insight") {
      setInsightLoading(true);
      analysisOutput.textContent = "Analyzing headlines...";
    }
    if (state.activeView === "deepDive") {
      loadDeepDive();
    } else if (state.activeView === "narrativeShift") {
      loadNarrativeShift();
    }
    if (preferDeepDive && state.query) {
      window.setTimeout(() => {
        if (state.currentScanToken !== scanToken || state.query !== baseData.query) {
          return;
        }
        loadDeepDive();
        loadNarrativeShift();
      }, 0);
    }

    const errorCount = Array.isArray(baseData.sourceErrors) ? baseData.sourceErrors.length : 0;
    logLine(`Headlines loaded. ${baseData.totalItems || 0} scored items, ${errorCount} source errors.`, errorCount ? "warn" : "success");

    if (errorCount) {
      baseData.sourceErrors.forEach((sourceError) => {
        logLine(`[${sourceError.source}] ${sourceError.error}`, "warn");
      });
    }

    const analysisParams = new URLSearchParams(baseParams);
    analysisParams.set("analysis", "1");
    const analysisResponse = await fetch(`/api/scan?${analysisParams.toString()}`);
    if (!analysisResponse.ok) {
      throw new Error(`Analysis failed with status ${analysisResponse.status}`);
    }

    const data = await analysisResponse.json();
    if (state.currentScanToken !== scanToken) {
      return;
    }

    if (data.analysis && data.analysis.ok) {
      state.lastInsightAnalysis = data.analysis;
      state.lastInsightQuery = state.query;
      if (state.activeView === "insight") {
        setInsightLoading(false);
        renderAnalysis(data.analysis);
      }
    } else {
      if (state.activeView === "insight") {
        setInsightLoading(false);
        if (state.lastInsightAnalysis && state.lastInsightQuery === state.query) {
          renderAnalysis(state.lastInsightAnalysis);
        } else {
          renderAnalysis(data.analysis);
        }
      }
    }
    logLine("Insight analysis ready.", data.analysis && data.analysis.ok ? "success" : "warn");

    if (data.analysis && data.analysis.error && !data.analysis.ok) {
      logLine(`AI analysis unavailable: ${data.analysis.error}`, "warn");
      if (state.lastInsightAnalysis && state.lastInsightQuery === state.query) {
        logLine(`Preserving last successful insight analysis${state.lastInsightQuery ? ` for ${state.lastInsightQuery || "GLOBAL"}` : ""}.`, "info");
      }
    }
  } catch (error) {
    if (state.currentScanToken !== scanToken) {
      return;
    }
    if (!headlinesList.children.length) {
      renderHeadlines([]);
      storyCount.textContent = "0 stories";
    }
    if (state.activeView === "insight") {
      setInsightLoading(false);
      if (state.lastInsightAnalysis && state.lastInsightQuery === state.query) {
        renderAnalysis(state.lastInsightAnalysis);
      } else {
        renderAnalysis({ error: String(error) });
      }
    }
    logLine(`Terminal error: ${error}`, "error");
  }
}

commandForm.addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = queryInput.value.trim();
  runScan();
});

focusToggle.addEventListener("click", () => {
  state.focusMode = !state.focusMode;
  syncModeUi();
  logLine(`Focus mode ${state.focusMode ? "enabled" : "disabled"}.`, "info");
  runScan();
});

refreshButton.addEventListener("click", () => {
  logLine("Manual refresh requested.", "info");
  runScan();
});

llmToggle.addEventListener("click", () => {
  const currentIndex = LLM_MODES.indexOf(state.llmMode);
  state.llmMode = LLM_MODES[(currentIndex + 1) % LLM_MODES.length];
  state.lastInsightAnalysis = null;
  state.lastInsightQuery = "";
  state.lastDeepDiveData = null;
  state.lastDeepDiveQuery = "";
  state.lastNarrativeShiftData = null;
  state.lastNarrativeShiftQuery = "";
  state.deepDiveLoadedFor = "";
  state.narrativeShiftLoadedFor = "";
  syncModeUi();
  logLine(`LLM engine set to ${llmModeLabel(state.llmMode)}.`, "info");
  runScan();
});

insightViewButton.addEventListener("click", () => {
  setActiveView("insight");
});

deepDiveViewButton.addEventListener("click", () => {
  setActiveView("deepDive");
  loadDeepDive();
});

narrativeShiftViewButton.addEventListener("click", () => {
  setActiveView("narrativeShift");
  loadNarrativeShift();
});

syncModeUi();
setActiveView("insight");
syncAnalysisSwitcher();
runScan();
window.setInterval(() => {
  logLine("Auto refresh triggered (15 min cadence).", "info");
  runScan();
}, AUTO_REFRESH_MS);
