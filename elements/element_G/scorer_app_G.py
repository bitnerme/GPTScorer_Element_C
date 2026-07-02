from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
import math
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
import os
from pathlib import Path
import tempfile
from io import BytesIO
import json

from elements.element_G.score_with_API_G import score_documents_with_api
from scripts.shared.utils import (
    extract_text_from_file,
    call_gpt_with_backoff,
    check_drift
)
from core.job_manager import create_job, update_progress, complete_job, get_job
from core.diagnostics import interpret_diagnostics
from core.schema import (
    get_element_from_file,
    detect_subelement_count,
    build_score_cols
)

app = FastAPI()

SAVE_BASELINE = False

last_metrics = None
last_mode = "current"

ELEMENT_PREFIX = "G"
SUBELEMENT_COUNT = 4

# =========================
# Linear Calibration
# =========================
LEGACY_A = 0.87 
LEGACY_B = 0.0

CURRENT_A = 0.8 
CURRENT_B = -0.1 

progress_tracker = {}

# ============================================================
# Drift Check Endpoint
# ============================================================
@app.post("/check_saved_results")
async def check_saved_results():
    global last_metrics, last_mode

    if last_metrics is None:
        return {"status": "NO RESULTS", "message": "Run scoring first."}

    if last_mode == "legacy":
        baseline_file = Path("config/element_G/baseline_metrics_legacy.json")
    else:
        baseline_file = Path("config/element_G/baseline_metrics_current.json")

    drift_result = check_drift(last_metrics, baseline_file)

    failures = drift_result.get("failures", [])

    api_drift = any(f.startswith("api_") for f in failures)
    final_drift = any(f.startswith("final_") for f in failures)

    golden_fail = False
    production_drift = False

    diagnosis = interpret_diagnostics(
        api_drift,
        final_drift,
        golden_fail,
        production_drift
    )

    drift_result["diagnostic_interpretation"] = diagnosis
    drift_result["current_metrics"] = last_metrics

    return drift_result


# ----------------------------------------------------
# Project Structure Anchoring
# ----------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ELEMENT_NAME = "element_G"
ELEMENT_CODE = "G"

MODELS_DIR = PROJECT_ROOT / "models" / ELEMENT_NAME
DATA_DIR = PROJECT_ROOT / "data" / ELEMENT_NAME
OUTPUTS_DIR = PROJECT_ROOT / "outputs" / ELEMENT_NAME

SIMPLE_UI_DIR = PROJECT_ROOT / "simple_ui"
SHARED_UI_DIR = SIMPLE_UI_DIR / "shared"

app.mount(
    "/static",
    StaticFiles(directory=SHARED_UI_DIR),
    name="static"
)

# ============================================================
# Metrics
# ============================================================
def compute_gpt_metrics(df: pd.DataFrame) -> dict:
    df = df.copy()

    raw_subs = [f"G{i}" for i in range(1, 5)]

    for c in raw_subs:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df["element_score_raw"] = df[raw_subs].mean(axis=1)

    final_subs = [f"G{i}_final" for i in range(1, 5)]
    if all(c in df.columns for c in final_subs):
        df["element_score_final"] = df[final_subs].mean(axis=1)
    else:
        df["element_score_final"] = df["element_score_raw"]

    api_s = df["element_score_raw"].dropna()
    fin_s = df["element_score_final"].dropna()

    return {
        "sample_size": int(len(df)),
        "api_mean": float(api_s.mean()) if len(api_s) else None,
        "api_std": float(api_s.std(ddof=0)) if len(api_s) else None,
        "final_mean": float(fin_s.mean()) if len(fin_s) else None,
        "final_std": float(fin_s.std(ddof=0)) if len(fin_s) else None,
    }


# ============================================================
# UI Root
# ============================================================
@app.get("/", response_class=HTMLResponse)
def root():
    with open(SHARED_UI_DIR / "index.html", encoding="utf-8") as f:
        html = f.read()

    return HTMLResponse(html.replace("__ELEMENT__", "G"))


# ============================================================
# Reconciliation
# ============================================================
@dataclass(frozen=True)
class FlagPolicy:
    allowed: Tuple[str, ...] = ("", "ci-ok", "ok", "none")
    blocked: Tuple[str, ...] = ("ci-fail", "critical", "block", "red flag")


def reconcile_integer_subscores(
    row: dict,
    keys: Sequence[str],
    target_element_col: str,
    min_score: int = 0,
    max_score: int = 5,
) -> Dict[str, int]:

    n = len(keys)
    orig = {k: int(row.get(k, 0)) for k in keys}

    target = float(row[target_element_col])
    desired_sum = int(round(target * n))
    current_sum = sum(orig.values())

    delta = desired_sum - current_sum

    rec = orig.copy()

    direction = 1 if delta > 0 else -1

    for _ in range(min(abs(delta), 2)):
        for k in keys:
            if direction > 0 and rec[k] < max_score:
                rec[k] += 1
                break
            elif direction < 0 and rec[k] > min_score:
                rec[k] -= 1
                break

    return rec


# ============================================================
# Calibration Pipeline
# ============================================================
def apply_calibration_pipeline(df, mode):

    for k in range(1, 5):
        col = f"G{k}"
        df[col] = pd.to_numeric(df.get(col, 0), errors="coerce").fillna(0)

    df["element_score_raw"] = df[[f"G{k}" for k in range(1, 5)]].mean(axis=1)

    if mode == "legacy":
        a, b = LEGACY_A, LEGACY_B
    else:
        a, b = CURRENT_A, CURRENT_B

    df["element_score_target"] = (a * df["element_score_raw"] + b).clip(0, 5)

    for k in range(1, 5):
        df[f"G{k}_final"] = df[f"G{k}"]

    for idx, row in df.iterrows():
        rec = reconcile_integer_subscores(
            row=row.to_dict(),
            keys=[f"G{k}" for k in range(1, 5)],
            target_element_col="element_score_target"
        )
        for k, v in rec.items():
            df.loc[idx, f"{k}_final"] = v

    df["element_score_final"] = df[[f"G{k}_final" for k in range(1, 5)]].mean(axis=1)

    return df


# ============================================================
# Scoring Endpoint
# ============================================================
@app.post("/score")
async def score_element_g(
    background_tasks: BackgroundTasks,
    mode: str = Form(...),
    files: List[UploadFile] = File(...)
):
    mode = (mode or "").strip().lower()
    if mode not in ("legacy", "current"):
        mode = "current"

    file_payloads = []
    for file in files:
        content = await file.read()
        file_payloads.append({
            "filename": file.filename,
            "content": content
        })

    job_id = create_job(len(file_payloads), ELEMENT_PREFIX, SUBELEMENT_COUNT)

    background_tasks.add_task(
        process_files_background,
        job_id,
        file_payloads,
        mode
    )

    return {"job_id": job_id}


# ============================================================
# Background Processing
# ============================================================
def process_files_background(job_id: str, file_payloads, mode: str):
    global last_metrics, last_mode

    last_mode = mode

    dfs = []

    for i, file_data in enumerate(file_payloads):
        filename = file_data["filename"]
        content = file_data["content"]

        if filename.lower().endswith(".csv"):
            df_one = pd.read_csv(BytesIO(content))
        else:
            suffix = os.path.splitext(filename)[1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp.flush()
                tmp_path = tmp.name

            documents = [{"filename": filename, "path": tmp_path}]
            blended = "v1.41" if mode == "legacy" else "v1.42"

            df_one = score_documents_with_api(documents, blended)

        dfs.append(df_one)
        update_progress(job_id, i + 1)

    df = pd.concat(dfs, ignore_index=True)

    df = apply_calibration_pipeline(df, mode)

    last_metrics = compute_gpt_metrics(df)

    element = get_element_from_file(__file__)
    sub_count = detect_subelement_count(df, element)
    cols = build_score_cols(element, sub_count)

    df = df.fillna("")
    results = df[[c for c in cols if c in df.columns]].to_dict("records")

    complete_job(job_id, results)


# ============================================================
# Progress Endpoint
# ============================================================
@app.get("/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "Invalid job ID"}
    return job


# ============================================================
# CLI Run
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("scorer_app_G:app", host="127.0.0.1", port=8000, reload=True)