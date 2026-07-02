console.log("SCRIPT VERSION FINAL through Element F");

document.addEventListener("DOMContentLoaded", () => {

    const elementDropdown = document.getElementById("element");
    const title = document.getElementById("title");

    function updateTitle() {
        if (!elementDropdown || !title) return;
        const selected = elementDropdown.value;
        title.innerText = `Element ${selected} Scoring`;
    }

    updateTitle();

    if (elementDropdown) {
        elementDropdown.addEventListener("change", updateTitle);
    }

});

window.displayResults = function(payload) {
    console.log("Rendering payload:", payload);
    if (window.lockResults) return;
    if (window.currentView === "diagnostics") {
        console.log("Skipping render — diagnostics mode");
        return;
    }

    const resultSection = document.getElementById("results-section");
    const resultOutput = document.getElementById("resultOutput");

    if (!resultSection || !resultOutput) {
        console.error("Result elements not found in DOM");
        return;
    }

    // 🔥 CRITICAL: Handle nested structure
    const actualResults = payload?.results?.results;

    if (!actualResults || actualResults.length === 0) {
        resultOutput.textContent = "No results returned.";
        resultSection.style.display = "block";
        return;
    }

    let output = "";

    actualResults.forEach((result, index) => {
        output += `Document: ${result.filename || `Document ${index + 1}`}\n\n`;

        const prefix = (payload.element || "").toUpperCase();

        Object.keys(result)
            .filter(key => key.startsWith(prefix) && key.endsWith("_final"))
            .sort()
            .forEach(key => {
                const label = key.replace("_final", "");
                output += `${label}: ${result[key]}\n`;
            });

        if (result.narrative_feedback) {
            output += `\nRationale:\n${result.narrative_feedback}\n`;
        }

        if (payload.diagnostics?.diagnostic_interpretation) {
            output += `\nDiagnostics:\n${payload.diagnostics.diagnostic_interpretation}\n`;
        }

        output += "\n-----------------------------\n\n";
    });

    resultOutput.textContent = output;
    resultSection.style.display = "block";
};
async function pollProgress(jobId) {

    const progressBar = document.getElementById("progressBar");
    const progressText = document.getElementById("progressText");
    const progressContainer = document.getElementById("progressContainer");

    if (progressContainer) {
        progressContainer.style.display = "block";
    }

    window.pollingActive = true;   // ✅ ensure ON

    const interval = setInterval(async () => {

        // 🔥 STOP if polling disabled
        if (window.pollingActive === false) {
            console.log("Polling stopped — clearing interval");
            clearInterval(interval);
            return;
        }

        // 🔥 STOP if not in scoring view
        if (window.currentView !== "scoring" && window.pollingActive === false) {
            clearInterval(interval);
            return;
        }

        try {
            const response = await fetch(`/progress/${jobId}`);
            const data = await response.json();

            const completed = data.completed ?? 0;
            const total = data.total ?? 0;

            const percent = total > 0 ? (completed / total) * 100 : 0;

            if (progressBar) {
                progressBar.style.width = percent + "%";
            }

            if (progressText) {
                progressText.textContent = `Scored ${completed} of ${total} documents`;
            }

            if (data.status === "done") {

                window.pollingActive = false;   // ✅ STOP ALL POLLING
                clearInterval(interval);

                // Force full progress bar
                if (progressBar) progressBar.style.width = "100%";

                window.renderTimeout = setTimeout(() => {

                    if (window.currentView !== "scoring") return;

                    const progressContainer = document.getElementById("progressContainer");
                    if (progressContainer) {
                        progressContainer.style.display = "none";
                    }

                    const payload = data.output || data;

                    payload.diagnostics = {
                        failures:
                            data.failures ||
                            data.output?.failures ||
                            data.output?.diagnostics?.failures ||
                            [],

                        diagnostic_interpretation:
                            data.diagnostic_interpretation ||
                            data.output?.diagnostic_interpretation ||
                            data.output?.diagnostics?.diagnostic_interpretation ||
                            null
                    };

                    // ✅ ONLY store scoring payloads
                    
                    if (payload?.results?.results) {
                        window.lastPayload = payload;
                        window.lastResults = payload.results.results;
                    }

                    const downloadToggle = document.getElementById("downloadCSVCheckbox");

                    if (
                        downloadToggle &&
                        downloadToggle.checked &&
                        !window.csvDownloaded &&
                        payload?.results?.results
                    ) {
                        window.csvDownloaded = true;
                        downloadCSV(window.lastPayload);
                    } else if (payload?.results?.results) {
                        displayResults(payload);
                    }

                }, 500);
            }

        } catch (err) {
            console.error("Polling error:", err);
            clearInterval(interval);
        }

    }, 1000);
}
document.getElementById("uploadForm").addEventListener("submit", async (e) => {
    e.preventDefault(); 
    
    window.lockResults = false;
    window.currentView = "scoring";

    const fileInput = document.getElementById("fileInput");

    const scoreFormData = new FormData();

    const element = document.getElementById("element").value;
    scoreFormData.append("element", element);

    console.log("Selected element:", element);

    for (const file of fileInput.files) {
        scoreFormData.append("files", file);
    }
    
    if (!fileInput.files.length) {
        alert("No file selected.");
        return;
    }

    const legacyChecked = document.getElementById("legacyToggle")?.checked;

    const mode = legacyChecked ? "legacy" : "current";

    scoreFormData.append("mode", mode);

    console.log("Selected mode:", mode);

    try {
        const response = await fetch("/score", {
            method: "POST",
            body: scoreFormData,
        });

        if (!response.ok) throw new Error(`Server error: ${response.status}`);

        document.getElementById("progressContainer").style.display = "block";
        document.getElementById("progressBar").style.width = "0%";
            
        const data = await response.json();
        const jobId = data.job_id;

        window.subelementCount = data.subelement_count || window.subelementCount || 1;
        const subelementDefaults = {
            A: 6,
            B: 4,
            C: 4,
            D: 4,
            E: 4,
            F: 1,
            G: 4,
            H: 5,
            I: 4,
            J: 3,
            K: 3,
            L: 2
            };

        window.subelementCount =
        data.subelement_count ||
        window.subelementCount ||
        subelementDefaults[element] ||
        1;

        window.currentView = "scoring";  
        window.pollingActive = true;      

        pollProgress(jobId);

    } catch (error) {
        console.error("Upload failed:", error);
        alert("Upload failed. Check the console for details.");
    }
});

function safeFixed(value) {
    const num = Number(value);
    return isFinite(num) ? num.toFixed(4) : "N/A";
}

//function toggleRegressionDetails() {
//    const el = document.getElementById("regressionDetails");
//    if (!el) return;

//   el.style.display = el.style.display === "none" ? "block" : "none";
//}


function renderTopCases(cases) {
    if (!cases || !cases.length) return "";

    let html = "<b>Top 5 Worst Cases</b><br><br>";

    cases.forEach(c => {
        html += `
            ${c.filename}: diff=${safeFixed(c.diff)}<br>
        `;
    });

    return html;
}

window.toggleRegressionDetails = function (id) {
    const el = document.getElementById(id);
    if (!el) {
        console.warn("No element found for:", id);
        return;
    }

    el.style.display = (el.style.display === "none") ? "block" : "none";
};


async function checkSavedResults() {
    const diagnosticsFormData = new FormData();
    const element = document.getElementById("element").value;

    const rebuildDrift = document.getElementById("rebuild_drift_baseline").checked;
    const runRegression = document.getElementById("run_regression").checked;
    const recomputeRegression = document.getElementById("recompute_regression_scores").checked;
    const rebuildRegression = document.getElementById("rebuild_regression_baseline").checked;

    if (rebuildDrift) {
        const confirmed = confirm(
            "This will overwrite the DRIFT baseline.\n\n" +
            "Only do this if you are intentionally updating model stability reference metrics.\n\n" +
            "Continue?"
        );
        if (!confirmed) return;
    }

    if (rebuildRegression) {
        const confirmed = confirm(
            "This will overwrite the REGRESSION baseline (Golden dataset).\n\n" +
            "Only do this if you are intentionally updating validation standards.\n\n" +
            "Continue?"
        );
        if (!confirmed) return;
    }

    diagnosticsFormData.append("element", element);
    diagnosticsFormData.append("rebuild_drift_baseline", rebuildDrift ? "true" : "false");
    diagnosticsFormData.append("run_regression", runRegression ? "true" : "false");
    diagnosticsFormData.append("recompute_regression_scores", recomputeRegression ? "true" : "false");
    diagnosticsFormData.append("rebuild_regression_baseline", rebuildRegression ? "true" : "false");

    console.log("Diagnostics options:", {
        rebuildDrift,
        runRegression,
        recomputeRegression,
        rebuildRegression
    });

   

    // --- LOCK ---
    if (window.diagnosticsRunning) return;
    window.diagnosticsRunning = true;

    const btn = document.getElementById("checkSavedResultsBtn");
    if (!btn) return;

    btn.disabled = true;
    const originalText = btn.innerText;
    btn.innerText = "Running...";

    try {

        const resultsSection = document.getElementById("results-section");
        const resultOutput = document.getElementById("resultOutput");

        window.currentView = "diagnostics";
        window.pollingActive = false;

        const resultsDiv = document.getElementById("resultOutput");

        // 🔥 HARD STOP any scoring UI updates
        window.lockResults = true;

        if (window.renderTimeout) {
            clearTimeout(window.renderTimeout);
        }

        // Switch UI to diagnostics mode
        //const progressBar = document.getElementById("progressBarContainer");
        const progressBar = document.getElementById("progressBarContainer");
        const progressText = document.getElementById("progressText");
        const diagnosticsPanel = document.getElementById("diagnosticsContainer");

        if (progressBar) progressBar.style.display = "none";
        if (progressText) progressText.style.display = "none";
        if (diagnosticsPanel) diagnosticsPanel.style.display = "block";

        const progressContainer = document.getElementById("progressContainer");
        if (progressContainer) {
            progressContainer.style.display = "none";
        }

        const saveDrift = document.getElementById("rebuild_drift_baseline").checked;
        diagnosticsFormData.append(
            "rebuild_drift_baseline",
            saveDrift ? "true" : "false"
        );

        for (const [k, v] of diagnosticsFormData.entries()) {
            console.log("FORMDATA", k, v);
        }

        const response = await fetch("/check_saved_results", {
            method: "POST",
            body: diagnosticsFormData
        });

         for (const [k, v] of diagnosticsFormData.entries()) {
            console.log("FORMDATA", k, v);
        }

        const data = await response.json();
        const payload = data;  

        console.log("FULL RESPONSE:", data);
        console.log("GOLDEN VALIDATION:", data.golden_validation);

         // Debug (optional but very helpful)
        console.log("Diagnostics options:", {
            rebuildDrift,
            runRegression,
            recomputeRegression,
            rebuildRegression
        });

        const diagDiv = document.getElementById("adminDiagnostics");
        if (!diagDiv) return;

        diagDiv.style.display = "block";

        // ==========================
        // Build content progressively
        // ==========================
        let infoMessage = "";

        if (data.failures && data.failures.length > 0) {
            const labels = {
                api_mean_shift: "API mean shifted",
                api_std_shift: "API variability changed",
                final_mean_shift: "Final score average shifted",
                final_std_shift: "Final score variability changed",
                golden_validation_failed: "Regression validation failed"
            };

            const readable = data.failures.map(f => labels[f] || f);

            infoMessage = "Drift detected: " + readable.join("; ");
        } else {
            infoMessage = "No significant drift detected.";
        }
        
        let html = `
            <h3>Admin Diagnostics</h3>
            ${infoMessage || ""}
        `;
        // --------------------------
        // No drift metrics case
        // --------------------------
        if (!data.report) {
            html += `
                <div style="color:#c62828;font-weight:bold;">
                    No drift metrics available.
                </div>
            `;
        }
        // --------------------------
        // Drift section
        // --------------------------
        const statusColor = data.status === "PASS" ? "#2e7d32" : "#c62828";

        html += `
            <div style="border:1px solid #ccc; padding:10px;">
                <b>Model Stability Check</b><br><br>

                <b>Absolute Metrics</b><br>
                API mean: ${safeFixed(data.current_metrics.api_mean)}<br> 
                API std: ${safeFixed(data.current_metrics.api_std)}<br>   
                Final mean: ${safeFixed(data.current_metrics.final_mean)}<br>  
                Final std: ${safeFixed(data.current_metrics.final_std)}<br><br> 

                API mean diff: ${safeFixed(data.report.api_mean_diff)}<br> 
                API std diff: ${safeFixed(data.report.api_std_diff)}<br>  
                Final mean diff: ${safeFixed(data.report.final_mean_diff)}<br>  
                Final std diff: ${safeFixed(data.report.final_std_diff)}<br>  

                <span style="color:${statusColor}; font-weight:bold;">
                    Status: ${data.status}
                </span>

            </div>
        `;

        if (data.failures && data.failures.length) {
            html += `
                <div style="margin-top:10px;">
                    <b>Triggered Drift Signals:</b><br>
                    ${data.failures.map(f => {
                        const labels = {
                            api_mean_shift: "API mean shifted",
                            api_std_shift: "API variability changed",
                            final_mean_shift: "Final score average shifted",
                            final_std_shift: "Final score variability changed",
                            golden_validation_failed: "Regression validation failed"
                        };
                        return labels[f] || f;
                    }).join("<br>")}
                </div>
            `;
        }

        if (data.sample_warning) {
            html += `
                <div style="margin-top:10px; color:#ef6c00;">
                    <b>Sample Size Warning:</b><br>
                    ${data.sample_warning}
                </div>
            `;
        }

        // --------------------------
        // Root cause section
        // --------------------------
        if (data.diagnostic_interpretation) {
            html += `
                <div style="border:1px solid #bbb;background:#f7f7f7;padding:10px;margin-top:15px;">
                    <b>Root Cause Analysis</b><br><br>
                    ${data.diagnostic_interpretation}
                </div>
            `
        };
    
        console.log("DIAG INTERPRETATION:", data.diagnostic_interpretation);
        // --------------------------
        // (Optional) Golden validation
        // --------------------------
        if (data.golden_validation) {
            const gv = data.golden_validation;
            const color = gv.status === "PASS" ? "#2e7d32" : "#c62828";

            html += `
                <div style="border:1px solid #ccc; padding:10px; margin-top:10px;">
                    <b>Golden20 Validation</b><br><br>

                    Status: <span style="color:${color}; font-weight:bold;">
                        ${gv.status}
                    </span><br><br>

                    ${gv.summary || ""}<br><br>

                    MAE: ${safeFixed(gv.metrics?.mae)} (Δ ${safeFixed(gv.metrics?.mae_diff)})<br>
                    Bias: ${safeFixed(gv.metrics?.bias)} (Δ ${safeFixed(gv.metrics?.bias_diff)})<br><br>

                    <div style="cursor:pointer; color:#1565c0;" onclick="toggleRegressionDetails('regressionDetails')">
                        ▶ Show detailed diagnostics
                    </div>

                    <div id="regressionDetails" style="display:none; margin-top:10px;">
                        ${renderTopCases(gv.top_cases)}
                    </div>
                </div>
            `;
        }

        // ==========================
        // Final render (ONLY ONCE)
        // ==========================
        diagDiv.innerHTML = html;

        if (payload?.results?.results) {
            window.lastPayload = payload;
            window.lastResults = payload.results.results;
        }
            
        //const title = document.getElementById("pageTitle");
        const title = document.getElementById("title");
        if (title) {
            title.innerText = `Element ${element} Scoring`;
        }

        const results = payload?.results?.results || [];

        results.forEach(result => {
            
            const fileName = document.createElement("h4");
            fileName.textContent = result.filename;
            resultsDiv.appendChild(fileName);

            const subKeys = Object.keys(result)
                .filter(k => k.startsWith(element) && !k.includes("_"))
                .sort((a, b) => {
                    const na = parseInt(a.slice(1));
                    const nb = parseInt(b.slice(1));
                    return na - nb;
                });

            subKeys.forEach(k => {

                const score = result[`${k}_final`] ?? result[k] ?? "";

                const p = document.createElement("p");
                p.textContent = `${k}: ${score}`;

                resultsDiv.appendChild(p);

            });

            if (result.narrative_feedback) {

                const rationaleBlock = document.createElement("div");

                const label = document.createElement("strong");
                label.textContent = "Rationale:";

                const paragraph = document.createElement("p");
                paragraph.textContent = result.narrative_feedback;

                rationaleBlock.appendChild(label);
                rationaleBlock.appendChild(paragraph);

                resultsDiv.appendChild(rationaleBlock);
            }

            resultsDiv.appendChild(document.createElement("hr"));
        });

        resultsSection.style.display = "block";

    } catch (err) {
        console.error(err);
    } finally {

        btn.disabled = false;
        btn.innerText = originalText;
        window.diagnosticsRunning = false;
    }
    document.getElementById("checkSavedResultsBtn").scrollIntoView({
        behavior: "smooth",
        block: "center"
    });
}

function escapeCSV(value) {
    if (value === null || value === undefined) return "";

    // 🔥 handle arrays (THIS is your missing piece)
    if (Array.isArray(value)) {
        value = value.join(" | ");
    }

    const stringValue = String(value);

    if (
        stringValue.includes(",") ||
        stringValue.includes('"') ||
        stringValue.includes("\n")
    ) {
        return `"${stringValue.replace(/"/g, '""')}"`;
    }

    return stringValue;
}

function downloadCSV() {
    const payload = window.lastPayload;

    if (!payload) {
        alert("No results available.");
        return;
    }

    const results = payload?.results?.results || payload?.results;

    if (!Array.isArray(results) || results.length === 0) {
        alert("Invalid results format.");
        console.error("Bad payload:", payload);
        return;
    }

    const firstRow = results[0];

    const scoreBases = Object.keys(firstRow)
        .map(k => {
            const m = k.match(/^([A-L]\d+)(?:_raw|_api|_rule|_final)?$/);
            return m ? m[1] : null;
        })
        .filter(Boolean)
        .filter((v, i, arr) => arr.indexOf(v) === i)
        .sort((a, b) => parseInt(a.slice(1), 10) - parseInt(b.slice(1), 10));

    // Normalize replay rows so bare subelement scores are also available as *_api.
    // This protects replay paths that return G1/G2/... instead of G1_api/G2_api/...
    for (const row of results) {
        for (const k of scoreBases) {
            if (row[`${k}_api`] === undefined && row[k] !== undefined) {
                row[`${k}_api`] = row[k];
            }
        }
    }

    console.log("scoreBases:", scoreBases);
    console.log("first result row:", results[0]);

    let headers = [
        "filename",

        "ocr_used",
        "initial_text_length",
        "ocr_text_length",
        "final_text_length",
        "extraction_method",
        
        ...scoreBases.map(k => `${k}_api_pass1`),
        ...scoreBases.map(k => `${k}_api_pass2`),
        ...scoreBases.map(k => `${k}_api_pass3`),
        ...scoreBases.map(k => `${k}_api`),
        ...scoreBases.map(k => `${k}_rule`),
        ...scoreBases.map(k => `${k}_final`),

        "element_score_api",
        "element_score_rule",
        "element_score_final",
        "element_score_delta",
        "element_score_calibrated",
        "calibration_delta",

        "identified_recommendations",
        "valid_project_recommendations",
        "valid_project_recommendations_count",
        "non_counting_recommendations",

        "flags",
        "rationales",
        "narrative_feedback"
    ];

    headers = headers.filter(h =>
        results.some(row => Object.prototype.hasOwnProperty.call(row, h))
    );

    const csvRows = [];
    csvRows.push(headers.join(","));

    for (const row of results) {
        csvRows.push(
            headers.map(h => escapeCSV(row[h] ?? "")).join(",")
        );
    }

    const csvContent = csvRows.join("\n");
    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);

    console.log("scoreBases =", scoreBases);

    if (results && results.length > 0) {
        console.log("First replay row:");
        console.log(results[0]);

        console.log("Replay row keys:");
        console.log(Object.keys(results[0]).sort());
    }

    console.log("CSV headers:");
    console.log(headers);

    const link = document.createElement("a");
    link.href = url;
    link.download = "results.csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    URL.revokeObjectURL(url);
}