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
from core.schema import detect_subelement_count
import json
from fastapi import FastAPI, Form
from scripts.shared.validate_golden20 import validate_golden20

app = FastAPI()

PROJECT_ROOT = Path(__file__).resolve().parent

global LAST_RUN_WAS_SCORING
global last_metrics, last_mode, last_results

last_results = None
last_metrics = None
last_mode = None
LAST_RUN_WAS_SCORING = False
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

def get_golden_paths(element, mode):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    label = element.lower()

    if mode == "current":
        return (
            os.path.join(BASE_DIR, "config", f"element_{label}", f"golden_{label}_current.json"),
            os.path.join(BASE_DIR, "elements", f"element_{label}", "golden_current_documents"),
        )
    else:
        return (
            os.path.join(BASE_DIR, "config", f"element_{label}", f"golden_{label}_legacy.json"),
            os.path.join(BASE_DIR, "elements", f"element_{label}", "golden_legacy_documents"),
        )

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
    files: List[UploadFile] = File(...),

    # 🔥 NEW FLAGS
    rebuild_drift_baseline: bool = Form(False),
    run_regression: bool = Form(True),
    recompute_regression_scores: bool = Form(False),
    rebuild_regression_baseline: bool = Form(False),
):

    element_clean = (element or "").strip().upper()
    subelement_count = detect_subelement_count(None, element)

    print("ELEMENT RAW:", repr(element))
    print("ELEMENT CLEAN:", repr(element_clean))
    print("SUBELEMENT COUNT:", subelement_count)

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
        apply_calibration_pipeline,

        # 🔥 ADD THESE
        rebuild_drift_baseline,
        run_regression,
        recompute_regression_scores,
        rebuild_regression_baseline,
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
    apply_calibration_pipeline,

    # 🔥 NEW
    rebuild_drift_baseline,
    run_regression,
    recompute_regression_scores,
    rebuild_regression_baseline,
):

    global last_run_regression, last_recompute_regression, last_rebuild_regression
    global last_results, last_metrics, last_mode, LAST_RUN_WAS_SCORING

    last_run_regression = run_regression
    last_recompute_regression = recompute_regression_scores
    last_rebuild_regression = rebuild_regression_baseline

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

    LAST_RUN_WAS_SCORING = True

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

    if rebuild_drift_baseline:
        print("Writing baseline metrics:", last_metrics)
        save_drift_metrics(element, mode, last_metrics)
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

    results = [
        {k: clean_nan(v) for k, v in row.items()}
        for row in results
    ]

    last_results = results  

    clean_metrics = {k: clean_nan(v) for k, v in last_metrics.items()}

    job_output = {
        "results": results,
        "metrics": clean_metrics,
        "status": "COMPLETE"
    }

    # ✅ mark that scoring just happened
    LAST_RUN_WAS_SCORING = True
    last_results = results
    last_metrics = clean_metrics
    last_mode = mode.lower()   

    complete_job(job_id, job_output)

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
async def check_saved_results(
    element: str = Form(...),
    check_golden: bool = Form(True)
):
    
    global LAST_RUN_WAS_SCORING, last_results, last_metrics, last_mode
    global last_run_regression, last_recompute_regression, last_rebuild_regression

    used_previous_results = not LAST_RUN_WAS_SCORING
    last_run_regression = True
    last_recompute_regression = False
    last_rebuild_regression = False

    if last_results is None:
        return {"status": "NO RESULTS", "message": "No scoring results available."}

    element = element.upper()
    mode = (last_mode or "current").lower()

    if mode == "legacy":
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_legacy.json"
    else:
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_current.json"

    drift_result = check_drift(last_metrics, baseline_file)

    print("DEBUG last_results:", type(last_results))
    df = pd.DataFrame(last_results)

    if last_run_regression:
        json_path, doc_dir = get_golden_paths(element, mode)
        golden_validation = validate_golden20(
            df,
            element,
            mode,
            json_path,
            doc_dir,
            recompute=last_recompute_regression,
            rebuild_baseline=last_rebuild_regression
        )
    else:
        golden_validation = {"status": "SKIPPED"}

    # ✅ reset BEFORE returning
    LAST_RUN_WAS_SCORING = False

    combined_failures = list(drift_result.get("failures", []))

    status = "PASS" if not combined_failures else "FAIL"

    if status == "FAIL":
        combined_failures.append("golden_validation_failed")

    drift_status = drift_result.get("status", "UNKNOWN")

    print("CONTROLLER golden_validation:", golden_validation)

    return {
        "status": status,
        "drift_status": drift_status,
        "report": drift_result.get("report", {}),
        "current_metrics": last_metrics,
        "diagnostic_interpretation": drift_result.get("diagnostic_interpretation"),
        "element": element,
        "mode": mode,
        "golden_validation": golden_validation,   
        "used_previous_results": used_previous_results,
        "failures": combined_failures,
    }

# =========================
# Run
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("controller_app:app", host="127.0.0.1", port=8000, reload=False)