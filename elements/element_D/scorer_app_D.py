from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
import math
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi import Request
from fastapi import Form
import pandas as pd
import joblib
import os
from pathlib import Path
import tempfile
from typing import List
from elements.element_D.score_with_API_D import score_documents_with_api
from scripts.shared.utils import (
    extract_text_from_file,
    call_gpt_with_backoff,
    check_drift
)
from dataclasses import dataclass
from typing import Tuple
from fastapi import BackgroundTasks
from core.job_manager import create_job, update_progress, complete_job, get_job, jobs
from io import BytesIO
from core.diagnostics import interpret_diagnostics
import json
from core.schema import build_score_cols
import __main__
from core.schema import (
    get_element_from_file,
    detect_subelement_count,
    build_score_cols
)

app = FastAPI()

SAVE_BASELINE = False

last_metrics = None
last_mode = "current"

ELEMENT_PREFIX = "D"
SUBELEMENT_COUNT = 4

# =========================
# Linear Calibration
# =========================
# Legacy linear calibration (variance + bias alignment)
LEGACY_A = 1.52
LEGACY_B = -1.33
# Current linear calibration (variance + bias alignment)
CURRENT_A = 1.74
CURRENT_B = -2.50

progress_tracker = {}

@app.post("/check_saved_results")
async def check_saved_results():

    global last_metrics

    if last_metrics is None:
        return {"status": "NO RESULTS", "message": "Run scoring first."}

    global last_mode

    if last_mode == "legacy":
        baseline_file = Path("config/element_D/baseline_metrics_legacy.json")
    else:
        baseline_file = Path("config/element_D/baseline_metrics_current.json")

    drift_result = check_drift(last_metrics, baseline_file)

    failures = drift_result.get("failures", [])

    api_drift = any(f.startswith("api_") for f in failures)
    final_drift = any(f.startswith("final_") for f in failures)

    golden_fail = False
    production_drift = False

    print("\nDEBUG FLAGS")
    print("api_drift:", api_drift)
    print("final_drift:", final_drift)
    print("golden_fail:", golden_fail)
    print("production_drift:", production_drift)

    diagnosis = interpret_diagnostics(
        api_drift,
        final_drift,
        golden_fail,
        production_drift
    )

    print("\nROOT CAUSE ANALYSIS")
    print("-------------------")
    print(diagnosis)

    # attach interpretation to response sent to UI
    drift_result["diagnostic_interpretation"] = diagnosis

    print("CURRENT METRICS:", last_metrics)

    drift_result["current_metrics"] = last_metrics

    return drift_result

print("### RUNNING THIS scorer_app_D.py ###")
print(__file__)

# ----------------------------------------------------
# Project Structure Anchoring
# ----------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]

ELEMENT_NAME = "element_D"   # Change per element
ELEMENT_CODE = "D"           # Change per element

MODELS_DIR = PROJECT_ROOT / "models" / ELEMENT_NAME
DATA_DIR = PROJECT_ROOT / "data" / ELEMENT_NAME
OUTPUTS_DIR = PROJECT_ROOT / "outputs" / ELEMENT_NAME

SIMPLE_UI_DIR = PROJECT_ROOT / "simple_ui"
SHARED_UI_DIR = SIMPLE_UI_DIR / "shared"

# Mount static folder for JSfrom fastapi.staticfiles import StaticFiles
app.mount(
    "/static",
    StaticFiles(directory=SHARED_UI_DIR),
    name="static"
)

import pandas as pd

def compute_gpt_metrics(df: pd.DataFrame) -> dict:
    df = df.copy()

    # ---- API element score (raw) ----
    raw_subs = [f"D{i}" for i in range(1, 5)]

    for c in raw_subs:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if "element_score_raw" not in df.columns:
        df["element_score_raw"] = df[raw_subs].mean(axis=1)

    df["element_score_raw"] = pd.to_numeric(df["element_score_raw"], errors="coerce")

    # ---- FINAL element score ----
    if "element_score_target" in df.columns:
        df["element_score_final"] = pd.to_numeric(df["element_score_target"], errors="coerce")
        final_source = "element_score_target"
    else:
        final_subs = [f"D{i}_final" for i in range(1, 5)]
        if all(c in df.columns for c in final_subs):
            for c in final_subs:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["element_score_final"] = df[final_subs].mean(axis=1)
            final_source = "D*_final"
        else:
            df["element_score_final"] = df[raw_subs].mean(axis=1)
            final_source = "D* (fallback)"

    df["element_score_final"] = pd.to_numeric(df["element_score_final"], errors="coerce")

    api_s = df["element_score_raw"].dropna()
    fin_s = df["element_score_final"].dropna()

    return {
        "sample_size": int(len(df)),
        "n_valid_api": int(api_s.shape[0]),
        "n_valid_final": int(fin_s.shape[0]),
        "api_mean": float(api_s.mean()) if len(api_s) else None,
        "api_std": float(api_s.std(ddof=0)) if len(api_s) else None,
        "final_mean": float(fin_s.mean()) if len(fin_s) else None,
        "final_std": float(fin_s.std(ddof=0)) if len(fin_s) else None,
        "final_source": final_source,
    }


@app.get("/", response_class=HTMLResponse)
def root():
    element = "D"

    with open(SHARED_UI_DIR / "index.html", encoding="utf-8") as f:
        html = f.read()

    html = html.replace("__ELEMENT__", element)

    return HTMLResponse(html)
print("==================")


# ----------------------------------------------------
# Reconcile Subelements
# ----------------------------------------------------

@dataclass(frozen=True)
class FlagPolicy:
    allowed: Tuple[str, ...] = ("", "ci-ok", "ok", "none")
    blocked: Tuple[str, ...] = ("ci-fail", "critical", "block", "red flag")

def reconcile_integer_subscores(
    row: dict,
    keys: Sequence[str],
    target_element_col: str,
    flag_suffix: str = "_flag",
    min_score: int = 0,
    max_score: int = 5,
    flag_policy: FlagPolicy = FlagPolicy(),
    # Optional per-criterion preference weights: lower = prefer adjusting this criterion
    # Example: {"D2": 0.8, "D4": 0.9, "D1": 1.0, ...}
    preference_weight: Optional[Dict[str, float]] = None,
    # If True, treat non-allowed non-blocked flags as “adjustable but expensive”.
    # If False, only allowed flags are adjustable.
    soft_block_nonallowed: bool = True,
) -> Dict[str, int]:
    """
    Reconcile integer subelement scores to match the closest achievable mean to the calibrated target.
    Minimizes movement (fewest ±1 steps) and uses informed priority based on flags + current values.

    Returns dict mapping each key -> recommended integer score.
    """
    n = len(keys)
    if n == 0:
        return {}

    # 1) Read original integer scores
    orig: Dict[str, int] = {}
    for k in keys:
        v = row.get(k, None)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            raise ValueError(f"Missing subscore {k}")
        orig[k] = int(round(float(v)))

    # clamp originals to bounds (defensive)
    for k in keys:
        orig[k] = max(min_score, min(max_score, orig[k]))

    target = float(row[target_element_col])

    # 2) Decide adjustability and per-criterion base costs from flags
    adjustable: List[str] = []
    base_cost: Dict[str, float] = {}

    def _norm_flag(x):
        return str(x).strip().lower()

    for k in keys:
        f = _norm_flag(row.get(f"{k}{flag_suffix}", ""))

        if f in flag_policy.blocked:
            base_cost[k] = float("inf")  # never adjust
            continue

        if f in flag_policy.allowed:
            adjustable.append(k)
            base_cost[k] = 1.0
        else:
            if soft_block_nonallowed:
                # still adjustable, but expensive
                adjustable.append(k)
                base_cost[k] = 5.0
            else:
                base_cost[k] = float("inf")

    rec = orig.copy()

    # If nothing is adjustable, return originals
    if not adjustable:
        return rec

    # 3) Choose the closest achievable integer sum to calibrated target
    current_sum = sum(rec.values())
    desired_sum = int(round(target * n))

    # Feasible sum range given bounds and adjustability
    min_possible = 0
    max_possible = 0
    for k in keys:
        if k in adjustable:
            min_possible += min_score
            max_possible += max_score
        else:
            min_possible += rec[k]
            max_possible += rec[k]

    # clamp desired_sum to feasible range
    if desired_sum < min_possible:
        desired_sum = min_possible
    elif desired_sum > max_possible:
        desired_sum = max_possible

    # We will move by integer steps until current_sum == desired_sum
    delta = desired_sum - current_sum
    if delta == 0:
        return rec

    # 4) Stepwise min-cost adjustments (greedy with convex-ish costs)
    # Cost encodes:
    # - flag cost (base_cost)
    # - preference_weight (optional)
    # - “expert-like” direction: when increasing, prefer low scores; when decreasing, prefer high scores
    w = preference_weight or {}

    def step_cost(k: str, direction: int) -> float:
        # direction: +1 (increase) or -1 (decrease)
        if base_cost[k] == float("inf"):
            return float("inf")

        # apply optional preference weights (default 1.0)
        pw = float(w.get(k, 1.0))

        # directional “expert-like” cost:
        # - when increasing: lower current score => cheaper
        # - when decreasing: higher current score => cheaper
        s = rec[k]
        if direction > 0:
            directional = 1.0 + (s / max_score)  # higher s => a bit more expensive to increase
        else:
            directional = 1.0 + ((max_score - s) / max_score)  # lower s => more expensive to decrease

        return base_cost[k] * pw * directional

    # perform |delta| unit moves
    direction = 1 if delta > 0 else -1
    steps = abs(delta)

    # 🔥 NEW: cap large adjustments
    if abs(delta) > 2:
        steps = 2
    else:
        steps = abs(delta)

    for _ in range(steps):
        best_k = None
        best_cost = float("inf")

        for k in adjustable:
            # check bounds for this move
            if direction > 0 and rec[k] >= max_score:
                continue
            if direction < 0 and rec[k] <= min_score:
                continue

            c = step_cost(k, direction)
            if c < best_cost:
                best_cost = c
                best_k = k

        # If no valid move exists (should be rare due to feasible clamp), stop
        if best_k is None or best_cost == float("inf"):
            break

        rec[best_k] += direction

    return rec

# Scoring endpoint
@app.post("/score")
async def score_element_c(
    background_tasks: BackgroundTasks,
    mode: str = Form(...),
    files: List[UploadFile] = File(...)
):
    mode = (mode or "").strip().lower()

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

def apply_calibration_pipeline(df, mode):

    # normalize subscores
    for k in range(1,5):
        col = f"D{k}"
        if col not in df.columns:
            df[col] = 0

        df[col] = (
            pd.to_numeric(df[col], errors="coerce")
            .fillna(0)
            .round()
            .astype(int)
        )

    # compute raw element score
    df["element_score_raw"] = df[[f"D{k}" for k in range(1,5)]].mean(axis=1)

    if mode == "legacy":
        a = LEGACY_A
        b = LEGACY_B
    else:
        a = CURRENT_A
        b = CURRENT_B

    df["element_score_target"] = (
        a * df["element_score_raw"] + b
    ).clip(0.0, 5.0)

    # initialize finals
    for k in range(1,5):
        df[f"D{k}_final"] = df[f"D{k}"]

    keys = [f"D{k}" for k in range(1,5)]

    # reconcile
    for idx,row in df.iterrows():
        rec = reconcile_integer_subscores(
            row=row.to_dict(),
            keys=keys,
            target_element_col="element_score_target",
            flag_suffix="_flag",
            soft_block_nonallowed=True
        )

        for k,v in rec.items():
            df.loc[idx,f"{k}_final"] = v

    df["element_score_final"] = df[[f"D{k}_final" for k in range(1,5)]].mean(axis=1)

    return df

def process_files_background(job_id: str, file_payloads, mode: str):
    print("ENTERED process_files_background")

    progress_tracker[job_id] = {
        "completed": 0,
        "total": len(file_payloads)
    }

    mode = (mode or "").strip().lower()
    mode = (mode or "").strip().lower()
    if mode not in ("legacy", "current"):
        mode = "current"

    global last_metrics, last_mode
    last_mode = mode

    dfs = []

    # ============================================================
    # 1) FILE LOOP
    # ============================================================
    for i, file_data in enumerate(file_payloads):
        filename = file_data["filename"]
        content = file_data["content"]

        print("PROCESSING:", filename)

        if filename.lower().endswith(".csv"):
            df_one = pd.read_csv(BytesIO(content), engine="python", on_bad_lines="warn")
            
            # Detect element + subelements
            element = get_element_from_file(__file__)
            subelement_count = detect_subelement_count(df_one, element)

            # Build schema
            score_cols = build_score_cols(element, subelement_count)
            
            # Ensure narrative_feedback column exists
            if "narrative_feedback" not in df_one.columns:
                df_one["narrative_feedback"] = ""

            print("After CSV load:", df_one["narrative_feedback"].iloc[0][:50])

            print("CSV columns:", list(df_one.columns))

        elif filename.lower().endswith((".pdf", ".docx")):
            suffix = os.path.splitext(filename)[1]

            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(content)
                tmp.flush()
                tmp_path = tmp.name

            documents = [{"filename": filename, "path": tmp_path}]
            blended = "v1.8d" if mode == "legacy" else "v2.0"

            df_one = score_documents_with_api(
                documents,
                blended_version=blended
            )

        else:
            raise ValueError(f"Unsupported file type: {filename}")

        dfs.append(df_one)
        update_progress(job_id, i + 1)

    if not dfs:
        complete_job(job_id, [])
        return

    # ============================================================
    # 2) CONCAT
    # ============================================================
    df = pd.concat(dfs, ignore_index=True)

    if "doc_id" in df.columns:
        df["filename"] = df["doc_id"]

    print("Documents scored:", len(df))

    # ============================================================
    # 3-8) Apply Calibration Pipeline
    # ============================================================
    df = apply_calibration_pipeline(df, mode.lower())

    df["calibration_delta"] = (
        df["element_score_final"] - df["element_score_raw"]
    )

    # make schema column
    df["element_score_calibrated"] = df["element_score_final"]

    # ============================================================
    # 8.5) Compute Metrics for Drift Detection
    # ============================================================

    print("RAW element mean:", df["element_score_raw"].mean())

    if "element_score_target" in df.columns:
        print("CALIBRATED TARGET mean:", df["element_score_target"].mean())

    if "element_score_final" in df.columns:
        print("FINAL element mean:", df["element_score_final"].mean())

    # compute from final subscores
    final_cols = [f"D{i}_final" for i in range(1,5)]
    if all(c in df.columns for c in final_cols):
        print("MEAN(C*_final):", df[final_cols].mean(axis=1).mean())

    raw_cols = [f"D{i}" for i in range(1,5)]
    if all(c in df.columns for c in raw_cols):
        print("MEAN(C*):", df[raw_cols].mean(axis=1).mean())

    if len(df) > 0:
        gpt_metrics = compute_gpt_metrics(df)
        last_metrics = gpt_metrics
        print("METRICS:", last_metrics)
    else:
        print("⚠️ No rows available for metrics.")

    # ============================================================
    # 9) Combine Flags + Rationales
    # ============================================================
    flag_cols = [f"D{k}_flag" for k in range(1, 5) if f"D{k}_flag" in df.columns]
    rat_cols = [f"D{k}_rationale" for k in range(1, 5) if f"D{k}_rationale" in df.columns]

    df["flags"] = (
        df[flag_cols]
        .apply(lambda r: " | ".join(str(x) for x in r if pd.notna(x) and str(x).strip()), axis=1)
        if flag_cols else ""
    )

    df["rationales"] = (
        df[rat_cols]
        .apply(lambda r: " | ".join(str(x) for x in r if pd.notna(x) and str(x).strip()), axis=1)
        if rat_cols else ""
    )

    # ============================================================
    # 10) Finalize Output
    # ============================================================
    element = get_element_from_file(__file__)

    # Detect number of rubric subscores dynamically
    subelement_count = detect_subelement_count(df, element)

    score_cols = build_score_cols(element, subelement_count)

    print("score_cols:", score_cols)
    print("df columns:", df.columns.tolist())

    safe_cols = [c for c in score_cols if c in df.columns]
    df = df.fillna("")

    print("=== NARRATIVE DEBUG ===")
    print("Column exists:", "narrative_feedback" in df.columns)
    print("Non-null count:", df["narrative_feedback"].notna().sum())
    print("Sample values:")
    print(df["narrative_feedback"].head(3))
    print("=======================")

    results = df[safe_cols].to_dict(orient="records")


    complete_job(job_id, results)

    # ============================================================
    # 11) Repeat each time the baseline changes
    # ============================================================
    if SAVE_BASELINE:
        print("Writing baseline metrics:", last_metrics)

        if last_metrics is not None and last_mode == "legacy":
            with open("config/element_D/baseline_metrics_legacy.json", "w") as f:
                json.dump(last_metrics, f, indent=2)
        elif last_metrics is not None and last_mode == "current":
            with open("config/element_D/baseline_metrics_current.json", "w") as f:
                json.dump(last_metrics, f, indent=2)

        print("Baseline metrics written for:", last_mode)

@app.get("/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "Invalid job ID"}
    return job

# CLI run
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("scorer_app_D:app", host="127.0.0.1", port=8000, reload=True)
