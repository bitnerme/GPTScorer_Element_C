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
from flask import request
from core.diagnostics import interpret_diagnostics

app = FastAPI()

PROJECT_ROOT = Path(__file__).resolve().parent

# NOTE: globals used for single-user/local environment (not thread-safe)

global LAST_RUN_WAS_SCORING
global last_metrics, last_mode, last_results

last_results = None
last_metrics = None
last_mode = None
LAST_RUN_WAS_SCORING = False
SAVE_BASELINE = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def save_drift_baseline_to_file(current_metrics, baseline_file):

    baseline_data = {
        "sample_size": current_metrics.get("sample_size"),
        "n_valid_api": current_metrics.get("n_valid_api"),
        "n_valid_final": current_metrics.get("n_valid_final"),
        "api_mean": current_metrics.get("api_mean"),
        "api_std": current_metrics.get("api_std"),
        "final_mean": current_metrics.get("final_mean"),
        "final_std": current_metrics.get("final_std")
    }

    with open(baseline_file, "w") as f:
        json.dump(baseline_data, f, indent=4)

    print(f"✅ Drift baseline saved to {baseline_file}")

def get_golden_paths(element, mode):
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    folder_label = element.upper()
    file_label = element.upper()

    if mode == "current":
        return (
            os.path.join(BASE_DIR, "config", f"element_{folder_label}", f"golden_{file_label}_current.json"),
            os.path.join(BASE_DIR, "elements", f"element_{folder_label}", "golden_current_documents"),
        )
    else:
        return (
            os.path.join(BASE_DIR, "config", f"element_{folder_label}", f"golden_{file_label}_legacy.json"),
            os.path.join(BASE_DIR, "elements", f"element_{folder_label}", "golden_legacy_documents"),
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
):

    element_clean = (element or "").strip().upper()
    subelement_count = detect_subelement_count(None, element)

    print("ELEMENT RAW:", repr(element))
    print("ELEMENT CLEAN:", repr(element_clean))
    print("SUBELEMENT COUNT:", subelement_count)

    score_mod, app_mod = load_element_modules(element)

    score_documents_ = score_mod.score_documents_with_api
    # C{i} = post-rule scores (input to calibration)
    # C{i}_api = raw GPT output (used for drift detection)
    apply_calibration_pipeline = app_mod.apply_calibration_pipeline

    file_payloads = []

    for file in files:
        content = await file.read()
        file_payloads.append({
            "filename": file.filename,
            "content": content
        })

    job_id = create_job(len(file_payloads), element, 4)

    apply_rules = getattr(score_mod, "apply_element_l_rules", None)

    background_tasks.add_task(
        process_files_background,
        job_id,
        file_payloads,
        element,
        mode,
        score_documents_,
        apply_calibration_pipeline,
        apply_rules,
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
    apply_rules,
):

    global last_results, last_metrics, last_mode, LAST_RUN_WAS_SCORING

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
            print("Processing CSV:", filename)  # CSV inputs are assumed pre-scored (bypass API + rules pipeline)

            df_one = pd.read_csv(BytesIO(content), engine="python", on_bad_lines="warn")

            if apply_rules is not None:
                blended = get_blended_model(element, mode)
                df_one = pd.DataFrame([
                    apply_rules(row.to_dict(), blended)
                    for _, row in df_one.iterrows()
                ])

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

    df = apply_calibration_pipeline(df, mode.lower())  

    subelement_count = detect_subelement_count(df, element)
    
    print(df[[f"{element}{i}_final" for i in range(1,subelement_count+1)]].head(1))

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
        "L1_api",
        "L2_api",
        "L1_rule",
        "L2_rule",
        "L1_final",
        "L2_final",

        "element_score_raw",
        "element_score_rule",
        "element_score_final",
        "element_score_calibrated",

        "calibration_delta",

        "identified_recommendations",
        "valid_project_recommendations",
        "valid_project_recommendations_count",
        "non_counting_recommendations",

        "flags",
        "rationales",
        "narrative_feedback"
    ]

    print("DF COLUMNS BEFORE FILTER:")
    print(df.columns.tolist())

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

    print("METRICS SET:", last_metrics)

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

    # Rebuild export columns after all derived fields are created
    valid_cols = ["filename"]

    if element == "L":
        valid_cols += [
            "L1_api", "L1_rule", "L1_raw", "L1_final",
            "L2_api", "L2_rule", "L2_raw", "L2_final",
            "element_score_raw",
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
            "narrative_feedback",
        ]
    else:
        for i in range(1, 10):
            col = f"{element}{i}"
            final_col = f"{element}{i}_final"
            if col in df.columns:
                valid_cols.append(col)
            if final_col in df.columns:
                valid_cols.append(final_col)

        valid_cols += [
            "element_score_raw",
            "element_score_final",
            "element_score_calibrated",
            "calibration_delta",
            "flags",
            "rationales",
            "narrative_feedback",
        ]

    valid_cols = [c for c in valid_cols if c in df.columns]
    valid_cols = list(dict.fromkeys(valid_cols))

    df = df[valid_cols]
    df = df.where(pd.notnull(df), None)

    print("🚨 FINAL PAYLOAD KEYS:")
    print(df.columns.tolist())

    dupes = df.columns[df.columns.duplicated()].tolist()
    print("DUPLICATE COLUMNS:", dupes)

    df = df.loc[:, ~df.columns.duplicated()]
    print("COLUMNS AFTER DEDUPE:", df.columns.tolist())

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
    rebuild_drift_baseline: str = Form("false"),
    run_regression: str = Form("true"),
    recompute_regression_scores: str = Form("false"),
    rebuild_regression_baseline: str = Form("false"),
):

    golden_validation = None

    print("USING CONTROLLER CHECK_SAVED_RESULTS")

    # ✅ normalize here
    rebuild_drift_baseline = rebuild_drift_baseline == "true"
    run_regression = run_regression == "true"
    recompute_regression_scores = recompute_regression_scores == "true"
    rebuild_regression_baseline = rebuild_regression_baseline == "true"

    print("🔥 ENTERED check_saved_results")
    global LAST_RUN_WAS_SCORING, last_results, last_metrics, last_mode
 

    used_previous_results = not LAST_RUN_WAS_SCORING

    global last_save_drift_baseline

    last_save_drift_baseline = rebuild_drift_baseline

    if last_results is None:
        return {"status": "NO RESULTS", "message": "No scoring results available."}

    element = element.upper()
    mode = (last_mode or "current").lower()

    if mode == "legacy":
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_legacy.json"
    else:
        baseline_file = PROJECT_ROOT / "config" / f"element_{element}" / "baseline_metrics_current.json"

    # Recompute metrics from current results

    current_metrics = last_metrics
    
    drift_result = check_drift(current_metrics, baseline_file)
    print("CONTROLLER check_drift ID:", id(check_drift))
    if drift_result is None:
        raise ValueError("check_drift() returned None — expected dict")

    if last_save_drift_baseline:
        save_drift_baseline_to_file(current_metrics, baseline_file)
        last_save_drift_baseline = False
        print("Saving baseline with metrics:", current_metrics)

    failures = drift_result.get("failures", [])

    api_drift = any(f in failures for f in ["api_mean_shift", "api_std_shift"])
    final_drift = any(f in failures for f in ["final_mean_shift", "final_std_shift"])

    # You may already have these — keep consistent with your system
    golden_fail = golden_validation.get("status") == "FAIL" if golden_validation else False
    production_drift = False  # or whatever logic you use

    diagnosis = interpret_diagnostics(
        api_drift,
        final_drift,
        golden_fail,
        production_drift
    )

    drift_result["diagnostic_interpretation"] = diagnosis


    df = pd.DataFrame(last_results)

    print("RECOMPUTE FLAG (controller):", recompute_regression_scores)
    print("LAST_RUN_WAS_SCORING:", LAST_RUN_WAS_SCORING)

    if run_regression:
        json_path, doc_dir = get_golden_paths(element, mode)
        golden_validation = validate_golden20(
            df,
            element,
            mode,
            json_path,
            doc_dir,
            recompute=recompute_regression_scores,
            rebuild_baseline=rebuild_regression_baseline)
    else:
        golden_validation = {"status": "SKIPPED"}

    # ✅ reset BEFORE returning
    LAST_RUN_WAS_SCORING = False

    combined_failures = list(drift_result.get("failures", []))

    status = "PASS" if not combined_failures else "FAIL"

    # Add regression failure ONLY if regression actually failed
    if golden_validation.get("status") == "FAIL":
        combined_failures.append("golden_validation_failed")

    drift_status = drift_result.get("status", "UNKNOWN")

    print("CONTROLLER golden_validation:", golden_validation)

    print("DIAG INTERPRETATION:", drift_result.get("diagnostic_interpretation"))

    print("RETURNING drift_result:", drift_result)

    return {
        "status": status,
        "drift_status": drift_status,
        "report": drift_result.get("report", {}),
        "current_metrics": last_metrics,
        "diagnostic_interpretation": drift_result.get("diagnostic_interpretation"),
        "sample_warning": drift_result.get("sample_warning"),
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