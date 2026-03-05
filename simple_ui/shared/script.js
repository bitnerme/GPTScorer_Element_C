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
                        displayResults(data.results);
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

        const data = await response.json();
        const jobId = data.job_id;
        pollProgress(jobId);

    } catch (error) {
        console.error("Upload failed:", error);
        alert("Upload failed. Check the console for details.");
    }
});

async function checkStoredResults() {

    console.log("Calling drift check...");

    const response = await fetch("/check_saved_results", {
        method: "POST"
    });

    const data = await response.json();

    console.log("Drift result:", data);

    document.getElementById("resultOutput").innerText =
        "Drift Check: " + JSON.stringify(data, null, 2);
    }

function displayResults(results) {
    window.lastResults = results || [];
    console.log("displayResults called with:", results);

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

        // Detect FINAL subscores like C1_final
        const finalKeys = Object.keys(result)
            .filter(k => /^[A-L]\d_final$/.test(k))
            .sort((a, b) => {
                const letterCompare = a[0].localeCompare(b[0]);
                if (letterCompare !== 0) return letterCompare;
                return parseInt(a.slice(1)) - parseInt(b.slice(1));
            });

        // Render only final scores
        finalKeys.forEach(k => {
            const baseKey = k.replace("_final", "");
            const p = document.createElement("p");
            p.textContent = `${baseKey}: ${result[k] ?? ""}`;
            resultsDiv.appendChild(p);
        });

        const spacer = document.createElement("br");
        resultsDiv.appendChild(spacer); 

        if (result.narrative_feedback) {
            const rationaleBlock = document.createElement("div");
            rationaleBlock.style.marginTop = "10px";

            const label = document.createElement("strong");
            label.textContent = "Rationale:";
            rationaleBlock.appendChild(label);

            const paragraph = document.createElement("p");
            paragraph.style.marginTop = "6px";
            paragraph.textContent = result.narrative_feedback;

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
    if (!window.lastResults || window.lastResults.length === 0) {
        alert("No results to download.");
        return;
    }

    const first = window.lastResults[0];

    // Detect D1–D4 or C1–C6 automatically
    // Detect raw subscores like C1, D2, etc.
    const rawKeys = Object.keys(first)
        .filter(k => /^[A-L]\d$/.test(k))
        .sort((a, b) => parseInt(a.slice(1)) - parseInt(b.slice(1)));

    // Detect final subscores like C1_final
    const finalKeys = Object.keys(first)
        .filter(k => /^[A-L]\d_final$/.test(k))
        .sort((a, b) => parseInt(a.slice(1)) - parseInt(b.slice(1)));

    // Detect recommended (if applicable)
    const recommendedKeys = rawKeys
        .map(k => `${k}_recommended`)
        .filter(k => k in first);

    const headers = [
        "filename",
        ...rawKeys,
        ...finalKeys,
        "element_score_raw",
        "element_score_calibrated",
        "calibration_delta",
        ...recommendedKeys,
        "flags",
        "rationales",
        "narrative_feedback"
    ].filter(h => h in first);

    const rows = window.lastResults.map(result =>
        headers.map(h => escapeCSV(result[h]))
    );

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
