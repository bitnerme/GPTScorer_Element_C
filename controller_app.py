import importlib
import tempfile
import os
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from typing import List
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from core.job_manager import create_job, update_progress, complete_job
import pandas as pd
from io import BytesIO
import math
from scripts.shared.utils import (
    check_drift,
    get_blended_model
)
import json

app = FastAPI()

PROJECT_ROOT = Path(__file__).resolve().parent


SUBELEMENT_MAP = {
    "A": 6,
    "B": 2,
    "C": 6,
    "D": 4,
}

SAVE_BASELINE = False

def save_drift_metrics(element, mode, metrics):
    if metrics is None:
        return

    element = element.upper()
    mode = mode.lower()

    base_path = os.path.join("config", f"element_{element}")
    os.makedirs(base_path, exist_ok=True)

    filename = f"baseline_metrics_{mode}.json"
    path = os.path.join(base_path, filename)

    with open(path, "w") as f:
        json.dump(metrics, f, indent=2)

    print(f"📊 Baseline metrics written: {path}")

# Mount UI
app.mount(
    "/static",
    StaticFiles(directory=PROJECT_ROOT / "simple_ui" / "shared"),
    name="static"
)

# =========================
# Dynamic Loader
# =========================
def load_element_modules(element):
    element = element.upper()

    score_mod = importlib.import_module(
        f"elements.element_{element}.score_with_API_{element}"
    )
    app_mod = importlib.import_module(
        f"elements.element_{element}.scorer_app_{element}"
    )

    return score_mod, app_mod

# =========================
# Get Diagnostic Metrics
# =========================

last_metrics = None
last_mode = None

def compute_gpt_metrics(df: pd.DataFrame, element: str) -> dict:
    sub_cols = sorted([
        c for c in df.columns
        if c.startswith(element) and c[len(element):].isdigit()
    ])

    for c in sub_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "element_score_raw" not in df.columns and sub_cols:
        df["element_score_raw"] = df[sub_cols].mean(axis=1)

    if "element_score_final" not in df.columns:
        final_cols = [f"{c}_final" for c in sub_cols if f"{c}_final" in df.columns]
        if final_cols:
            df["element_score_final"] = df[final_cols].mean(axis=1)
        elif "element_score_calibrated" in df.columns:
            df["element_score_final"] = pd.to_numeric(df["element_score_calibrated"], errors="coerce")
        else:
            df["element_score_final"] = df["element_score_raw"]

    api_s = pd.to_numeric(df["element_score_raw"], errors="coerce").dropna()
    fin_s = pd.to_numeric(df["element_score_final"], errors="coerce").dropna()

    return {
        "sample_size": int(len(df)),
        "n_valid_api": int(api_s.shape[0]),
        "n_valid_final": int(fin_s.shape[0]),
        "api_mean": float(api_s.mean()) if len(api_s) else None,
        "api_std": float(api_s.std(ddof=0)) if len(api_s) else None,
        "final_mean": float(fin_s.mean()) if len(fin_s) else None,
        "final_std": float(fin_s.std(ddof=0)) if len(fin_s) else None,
    }

# =========================
# Clean CSV file
# =========================

def clean_nan(obj):
    if isinstance(obj, float) and math.isnan(obj):
        return None
    return obj

# =========================
# UI Entry
# =========================
@app.get("/", response_class=HTMLResponse)
def root():
    with open(PROJECT_ROOT / "simple_ui" / "shared" / "index.html", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# =========================
# Score Endpoint
# =========================
@app.post("/score")
async def score(
    background_tasks: BackgroundTasks,
    element: str = Form(...),
    mode: str = Form(...),
    files: List[UploadFile] = File(...)
):
    element = element.upper()

    subelement_count = SUBELEMENT_MAP.get(element, 4)

    score_mod, app_mod = load_element_modules(element)

    score_documents_with_api = score_mod.score_documents_with_api
    apply_calibration_pipeline = app_mod.apply_calibration_pipeline

    file_payloads = []

    for file in files:
        content = await file.read()
        file_payloads.append({
            "filename": file.filename,
            "content": content
        })

    job_id = create_job(len(file_payloads), element, 4)

    background_tasks.add_task(
        process_files_background,
        job_id,
        file_payloads,
        element,
        mode,
        score_documents_with_api,
        apply_calibration_pipeline
    )

    return {
    "job_id": job_id,
    "element": element,
    "subelement_count": subelement_count
    }

# =========================
# Background Processing
# =========================
def process_files_background(
    job_id,
    file_payloads,
    element,
    mode,
    score_documents_with_api,
    apply_calibration_pipeline
):

    dfs = []

    for i, file_data in enumerate(file_payloads):
        filename = file_data["filename"]
        content = file_data["content"]

        suffix = os.path.splitext(filename)[1]

        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(content)
            tmp.flush()
            tmp_path = tmp.name

        blended = get_blended_model(element, mode)

        if filename.lower().endswith(".csv"):
            print("Processing CSV:", filename)

            df_one = pd.read_csv(BytesIO(content), engine="python", on_bad_lines="warn")

        else:
            print("Processing document:", filename)

            documents = [{"filename": filename, "path": tmp_path}]

            df_one = score_documents_with_api(
                documents,
                blended_version=blended
            )

        dfs.append(df_one)

        update_progress(job_id, i + 1)

    if not dfs:
        complete_job(job_id, [])
        return

    df = pd.concat(dfs, ignore_index=True)

    # Map GPT scores to base scores (critical step)
    for col in df.columns:
        if col.endswith("_gpt"):
            base_col = col.replace("_gpt", "")
            df[base_col] = df[col]

    gpt_cols = [c for c in df.columns if c.endswith("_gpt")]
    base_cols = [c.replace("_gpt", "") for c in gpt_cols if c.replace("_gpt", "") in df.columns]

    df = apply_calibration_pipeline(df, mode.lower())  

    valid_cols = ["filename"]

    # dynamic subelements
    for i in range(1, 10):  # safe upper bound
        col = f"{element}{i}"
        if col in df.columns:
            valid_cols.append(col)

        final_col = f"{element}{i}_final"
        if final_col in df.columns:
            valid_cols.append(final_col)

    # add known fields
    valid_cols += [
        "element_score_raw",
        "element_score_final",
        "element_score_calibrated",
        "calibration_delta",
        "flags",
        "rationales",
        "narrative_feedback"
    ]

    # keep only existing ones
    valid_cols = [c for c in valid_cols if c in df.columns]

    df = df[valid_cols]

    df = df.where(pd.notnull(df), None)

    print("🚨 FINAL PAYLOAD KEYS:")
    print(df.columns.tolist())

    global last_metrics, last_mode

    last_mode = mode.lower()
    last_metrics = compute_gpt_metrics(df.copy(), element)
    print("METRICS:", last_metrics)

    # ---- Compute metrics ----
    raw_cols = [c for c in df.columns if c.startswith(element) and c[len(element):].isdigit()]
    final_cols = [f"{c}_final" for c in raw_cols if f"{c}_final" in df.columns]

    if raw_cols:
        df["element_score_raw"] = df[raw_cols].mean(axis=1)

    if final_cols:
        df["element_score_final"] = df[final_cols].mean(axis=1)
    else:
        df["element_score_final"] = df["element_score_raw"]

    api_s = df["element_score_raw"].dropna()
    fin_s = df["element_score_final"].dropna()

    last_metrics = {
        "sample_size": int(len(df)),
        "n_valid_api": int(api_s.shape[0]),
        "n_valid_final": int(fin_s.shape[0]),
        "api_mean": float(api_s.mean()) if len(api_s) else 0,
        "api_std": float(api_s.std(ddof=0)) if len(api_s) else 0,
        "final_mean": float(fin_s.mean()) if len(fin_s) else 0,
        "final_std": float(fin_s.std(ddof=0)) if len(fin_s) else 0,
    }

    print("METRICS SET:", last_metrics)

    if SAVE_BASELINE:
        print("Writing baseline metrics:", last_metrics)
        save_drift_metrics(element, mode, last_metrics)

    # calibrated score + delta
    if "element_score_raw" in df.columns and "element_score_final" in df.columns:
        df["calibration_delta"] = df["element_score_final"] - df["element_score_raw"]
        df["element_score_calibrated"] = df["element_score_final"]

    # combine flags
    flag_cols = [c for c in df.columns if c.endswith("_flag")]
    if flag_cols:
        df["flags"] = df[flag_cols].apply(
            lambda r: " | ".join(str(x) for x in r if pd.notna(x) and str(x).strip()),
            axis=1
        )
    else:
        df["flags"] = ""

    # combine rationales
    rat_cols = [c for c in df.columns if c.endswith("_rationale")]
    if rat_cols:
        df["rationales"] = df[rat_cols].apply(
            lambda r: " | ".join(str(x) for x in r if pd.notna(x) and str(x).strip()),
            axis=1
        )
    else:
        df["rationales"] = ""

    results = df.to_dict(orient="records")

    # recursively clean all values
    results = [
        {k: clean_nan(v) for k, v in row.items()}
        for row in results
    ]

    complete_job(job_id, results)

# =========================
# Progress Endpoint
# =========================
@app.get("/progress/{job_id}")
def progress(job_id: str):
    from core.job_manager import get_job
    return get_job(job_id)

# =========================
# Diagnostics Endpoint
# =========================
@app.post("/check_saved_results")
async def check_saved_results(element: str = Form(...)):
    global last_metrics, last_mode

    if last_metrics is None:
        return {"status": "NO RESULTS", "message": "Run scoring first."}

    element = element.upper()
    mode = (last_mode or "current").lower()

    if mode == "legacy":
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_legacy.json"
    else:
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_current.json"

    drift_result = check_drift(last_metrics, baseline_file)

    report = {
        "api_mean_diff": drift_result.get("api_mean_diff", 0),
        "api_std_diff": drift_result.get("api_std_diff", 0),
        "final_mean_diff": drift_result.get("final_mean_diff", 0),
        "final_std_diff": drift_result.get("final_std_diff", 0),
    }

    return {
        "status": drift_result.get("status", "FAIL"),
        "report": drift_result.get("report", {}),
        "current_metrics": last_metrics,
        "diagnostic_interpretation": drift_result.get("diagnostic_interpretation"),
        "element": element,
        "mode": mode
    }

    #return {
    #    "status": "PASS" if not drift_result.get("failures") else "FAIL",
    #    "report": report,
    #    "current_metrics": last_metrics,
    #    "diagnostic_interpretation": drift_result.get("diagnostic_interpretation"),
    #    "element": element,
    #    "mode": mode
    #}

# =========================
# Admin: Validation
# =========================
@app.post("/validate")
async def validate(element: str = Form(None)):
    import subprocess

    cmd = ["python", "scripts/shared/validate_golden20.py"]

    if element:
        cmd.append(element.upper())

    result = subprocess.run(cmd, capture_output=True, text=True)

    return {
        "stdout": result.stdout,
        "stderr": result.stderr
    }


# =========================
# Run
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("controller_app:app", host="127.0.0.1", port=8000, reload=True)