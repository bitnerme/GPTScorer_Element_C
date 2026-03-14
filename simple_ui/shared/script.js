console.log("uploadForm at load:", document.getElementById("uploadForm"));
console.log("script loaded");

async function pollProgress(jobId) {
    const progressBar = document.getElementById("progressBar");
    const progressText = document.getElementById("progressText");
    const progressContainer = document.getElementById("progressContainer");

    if (progressContainer) {
        progressContainer.style.display = "block";
    }




    const interval = setInterval(async () => {
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

                clearInterval(interval);

                // Force full progress bar
                if (progressBar) progressBar.style.width = "100%";

                setTimeout(() => {

                    const progressContainer = document.getElementById("progressContainer");
                    if (progressContainer) {
                        progressContainer.style.display = "none";
                    }

                    const downloadToggle = document.getElementById("downloadCSVCheckbox");

                    if (downloadToggle && downloadToggle.checked) {
                        window.lastResults = data.results;
                        downloadCSV();
                    } else {
                        displayResults(data);
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
    e.preventDefault();   // <-- REQUIRED

    const fileInput = document.getElementById("fileInput");


    const formData = new FormData();
    for (const file of fileInput.files) {
        formData.append("files", file);
    }
    
    if (!fileInput.files.length) {
        alert("No file selected.");
    }

    const mode = document.getElementById("modeSelect").value;
    formData.append("mode", mode);
    console.log("Selected mode:", mode);  // ← add this

    const downloadToggle = document.querySelector("#downloadCSVCheckbox");

    try {
        const response = await fetch("/score", {
            method: "POST",
            body: formData,
        });

        if (!response.ok) throw new Error(`Server error: ${response.status}`);

        document.getElementById("progressContainer").style.display = "block";
        document.getElementById("progressBar").style.width = "0%";
            
        const data = await response.json();
        const jobId = data.job_id;
        pollProgress(jobId);

    } catch (error) {
        console.error("Upload failed:", error);
        alert("Upload failed. Check the console for details.");
    }
});

async function checkSavedResults() {

    const response = await fetch("/check_saved_results", {
        method: "POST"
    });

    const data = await response.json();

    const diagDiv = document.getElementById("adminDiagnostics");

    if (!diagDiv) return;

    diagDiv.style.display = "block";

    if (!data.report) {
        diagDiv.innerHTML = `
            <h3>Admin Diagnostics</h3>
            <div style="color:#c62828;font-weight:bold;">
                No drift metrics available.
            </div>
        `;
        return;
    }

    let statusColor = data.status === "PASS" ? "#2e7d32" : "#c62828";

    diagDiv.innerHTML = `
        <h3>Admin Diagnostics</h3>

        <div style="border:1px solid #ccc; padding:10px;">
        <b>Model Stability Check</b><br><br>

        <b>Absolute Metrics</b><br>
        API mean: ${data.current_metrics.api_mean.toFixed(4)}<br>
        API std: ${data.current_metrics.api_std.toFixed(4)}<br>
        Final mean: ${data.current_metrics.final_mean.toFixed(4)}<br>
        Final std: ${data.current_metrics.final_std.toFixed(4)}<br><br>

        <b>Drift vs Baseline</b><br>
        API mean diff: ${data.report.api_mean_diff.toFixed(4)}<br>
        API std diff: ${data.report.api_std_diff.toFixed(4)}<br>
        Final mean diff: ${data.report.final_mean_diff.toFixed(4)}<br>
        Final std diff: ${data.report.final_std_diff.toFixed(4)}<br><br>

        <span style="color:${statusColor}; font-weight:bold;">
        Status: ${data.status}
        </span>
        </div>

        ${
            data.diagnostic_interpretation
            ? `<div style="border:1px solid #bbb;background:#f7f7f7;padding:10px;margin-top:15px;">
               <b>Root Cause Analysis</b><br><br>
               ${data.diagnostic_interpretation}
               </div>`
            : ""
        }
    `;

}

function displayResults(payload) {

    window.lastPayload = payload;

    const results = payload.results;
    const element = payload.element;
    const count = payload.subelement_count;

    const title = document.getElementById("pageTitle");
    if (title) {
        title.innerText = `Element ${element} Scoring`;
    }

    const resultsDiv = document.getElementById("resultOutput");
    const resultsSection = document.getElementById("results-section");

    resultsDiv.innerHTML = "";

    if (!results || results.length === 0) {
        resultsDiv.textContent = "No results returned.";
        return;
    }

    results.forEach(result => {

        const fileName = document.createElement("h4");
        fileName.textContent = result.filename;
        resultsDiv.appendChild(fileName);

        for (let i = 1; i <= count; i++) {

            const score = result[`_${i}_final`];

            const p = document.createElement("p");
            p.textContent = `${element}${i}: ${score ?? ""}`;

            resultsDiv.appendChild(p);
        }

        if (result.element_score_calibrated !== undefined) {

            const elementScore = document.createElement("p");
            elementScore.style.fontWeight = "bold";

            elementScore.textContent =
                `Element Score: ${result.element_score_calibrated}`;

            resultsDiv.appendChild(elementScore);
        }

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
}

function escapeCSV(value) {
    if (value === null || value === undefined) return "";

    const stringValue = String(value);

    // If value contains comma, quote, or newline → wrap in quotes
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

    if (!window.lastPayload || !window.lastPayload.results || window.lastPayload.results.length === 0) {
        alert("No results to download.");
        return;
    }

    const payload = window.lastPayload;
    const results = payload.results;
    const element = payload.element;
    const count = payload.subelement_count;

    const headers = ["filename"];

    for (let i = 1; i <= count; i++) {
        headers.push(`${element}${i}`);
    }

    headers.push(
        "element_score_raw",
        "element_score_calibrated",
        "calibration_delta",
        "flags",
        "rationales",
        "narrative_feedback"
    );

    const rows = results.map(result => {

        const row = [escapeCSV(result.filename)];

        for (let i = 1; i <= count; i++) {

            let score = result[`_${i}_final`];

            if (score === undefined) {
                score = result[`${element}${i}_final`];
            }

            row.push(escapeCSV(score));
        }

        row.push(
            escapeCSV(result.element_score_raw),
            escapeCSV(result.element_score_calibrated),
            escapeCSV(result.calibration_delta),
            escapeCSV(result.flags),
            escapeCSV(result.rationales),
            escapeCSV(result.narrative_feedback)
        );

        return row;
    });

    const csvContent = [headers.join(",")]
        .concat(rows.map(r => r.join(",")))
        .join("\n");

    const blob = new Blob([csvContent], { type: "text/csv" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = "scoring_results.csv";
    a.click();

    URL.revokeObjectURL(url);
}