from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
import os
from pathlib import Path
import tempfile
from io import BytesIO

from elements.element_E.score_with_API_E import score_documents_with_api
from scripts.shared.utils import check_drift
from core.job_manager import create_job, update_progress, complete_job, get_job
from core.diagnostics import interpret_diagnostics
from core.schema import (
    get_element_from_file,
    detect_subelement_count,
    build_score_cols
)
from typing import Dict, List, Sequence, Optional
import math
import numpy as np

app = FastAPI()

ELEMENT_CALIBRATION = {
    "E": {
        "current": {"a": 1.35, "b": -1.36},
        "legacy": {"a": 1.18, "b": 0.20},  
    }
}

last_metrics = None
last_mode = "current"

ELEMENT_PREFIX = "E"
SUBELEMENT_COUNT = 5  # ← from your table

E3_LOW_PENALTY = 0.15
E4_LOW_PENALTY = 0.15
E5_LOW_PENALTY = 0.00

def apply_structural_penalty(row):
    e3 = float(row["E3"])
    e4 = float(row["E4"])
    e5 = float(row["E5"])

    penalty = 0.0

    # Strong failure: both E3 and E4 weak
    if e3 <= 1 and e4 <= 1:
        penalty += E3_LOW_PENALTY + E4_LOW_PENALTY

    # Moderate failure: one of E3/E4 weak
    elif e3 <= 1:
        penalty += E3_LOW_PENALTY

    elif e4 <= 1:
        penalty += E4_LOW_PENALTY

    # Minor: weak E5
    if e5 <= 1:
        penalty += E5_LOW_PENALTY

    return penalty

def clean_nan_deep(obj):
    if obj is None:
        return None

    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj

    if isinstance(obj, (np.floating,)):
        val = float(obj)
        if math.isnan(val) or math.isinf(val):
            return None
        return val

    if isinstance(obj, (np.integer,)):
        return int(obj)

    if isinstance(obj, dict):
        return {k: clean_nan_deep(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [clean_nan_deep(v) for v in obj]

    return obj

def apply_legacy_rank_scaling(df, raw_col="element_score_raw", out_col="element_score_ranked"):
    df = df.copy()

    n = len(df)
    if n <= 1:
        df[out_col] = df[raw_col]
        return df

    ranks = df[raw_col].rank(method="average", ascending=True)

    # Map lowest raw element score to 0, highest to 5
    df[out_col] = 5 * (ranks - 1) / (n - 1)

    return df

# ============================================================
# Drift Check Endpoint
# ============================================================
@app.post("/check_saved_results")
async def check_saved_results():
    global last_metrics, last_mode

    if last_metrics is None:
        return {"status": "NO RESULTS", "message": "Run scoring first."}

    baseline_file = Path("config/element_E/baseline_metrics_current.json")

    drift_result = check_drift(last_metrics, baseline_file)

    failures = drift_result.get("failures", [])

    api_drift = any(f.startswith("api_") for f in failures)
    final_drift = any(f.startswith("final_") for f in failures)

    diagnosis = interpret_diagnostics(
        api_drift,
        final_drift,
        False,
        False
    )

    drift_result["diagnostic_interpretation"] = diagnosis
    drift_result["current_metrics"] = last_metrics

    return drift_result


# ============================================================
# Project Structure
# ============================================================
PROJECT_ROOT = Path(__file__).resolve().parents[2]

SIMPLE_UI_DIR = PROJECT_ROOT / "simple_ui"
SHARED_UI_DIR = SIMPLE_UI_DIR / "shared"

app.mount("/static", StaticFiles(directory=SHARED_UI_DIR), name="static")


# ============================================================
# Metrics
# ============================================================
def compute_gpt_metrics(df: pd.DataFrame) -> dict:
    subs = [f"E{i}" for i in range(1, 6)]

    df["element_score_raw"] = df[subs].mean(axis=1)

    final_subs = [f"E{i}_final" for i in range(1, 6)]
    if all(c in df.columns for c in final_subs):
        df["element_score_final"] = df[final_subs].mean(axis=1)
    else:
        df["element_score_final"] = df["element_score_raw"]

    return {
        "sample_size": len(df),
        "api_mean": float(df["element_score_raw"].mean()),
        "api_std": float(df["element_score_raw"].std(ddof=0)),
        "final_mean": float(df["element_score_final"].mean()),
        "final_std": float(df["element_score_final"].std(ddof=0)),
    }


# ============================================================
# UI Root
# ============================================================
@app.get("/", response_class=HTMLResponse)
def root():
    with open(SHARED_UI_DIR / "index.html", encoding="utf-8") as f:
        html = f.read()

    return HTMLResponse(html.replace("__ELEMENT__", "E"))

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
    # Example: {"E2": 0.8, "E4": 0.9, "E1": 1.0, ...}
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


# ============================================================
# Calibration
# ============================================================
def apply_calibration_pipeline(df, mode):
    mode_key = (mode or "current").lower()

    cal = ELEMENT_CALIBRATION["E"].get(mode_key)
    if cal is None:
        raise ValueError(f"Unknown calibration mode for Element E: {mode_key}")

    a = cal["a"]
    b = cal["b"]

    print("CALIBRATION MODE:", mode_key)
    print("a, b:", a, b)

    # --- Normalize / clean input ---
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]

    df = df.dropna(how="all").reset_index(drop=True)

    # Restore expected working columns E1–E5 from replay CSV variants
    for i in range(1, 6):
        base_col = f"E{i}"
        raw_col = f"E{i}_raw"
        api_col = f"E{i}_api"

        if base_col not in df.columns:
            if raw_col in df.columns:
                df[base_col] = df[raw_col]
            elif api_col in df.columns:
                df[base_col] = df[api_col]

    sub_cols = [f"E{i}" for i in range(1, 6)]

    missing = [c for c in sub_cols if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required score columns before calibration: {missing}. "
            f"Available columns: {df.columns.tolist()}"
        )

    # Force numeric
    for c in sub_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Drop rows where all subscore values are blank
    df = df.dropna(how="all", subset=sub_cols).reset_index(drop=True)

    # Check for partially missing subscore rows
    bad_rows = df[df[sub_cols].isna().any(axis=1)]
    if not bad_rows.empty:
        print("=== ROWS WITH MISSING SUBSCORES ===")
        print(bad_rows[["filename", *sub_cols]].to_string(index=False))
        raise ValueError("Missing subscore values found before calibration")

    # Preserve raw scores for output
    for i in range(1, 6):
        df[f"E{i}_raw"] = df[f"E{i}"]


    # Raw element score
    df["element_score_raw"] = df[sub_cols].mean(axis=1)

    # Ranking first for legacy
    if mode_key == "legacy":
        df = apply_legacy_rank_scaling(
            df,
            raw_col="element_score_raw",
            out_col="element_score_ranked"
        )

        # Apply penalty AFTER ranking
        df["element_score_calibration_input"] = df.apply(
            lambda row: max(
                0,
                row["element_score_ranked"] - apply_structural_penalty(row)
            ),
            axis=1
        )
    else:
        df["element_score_ranked"] = df["element_score_raw"]
        df["element_score_calibration_input"] = df["element_score_raw"]

    # --- OVERRIDE RANKING AND RULES FOR BASELINE TEST ---
    df["element_score_ranked"] = df["element_score_raw"]
    df["element_score_calibration_input"] = df["element_score_raw"]

    # --- Linear calibration ---
    df["element_score_calibrated"] = (
        a * df["element_score_calibration_input"] + b
    ).clip(0, 5)

    print("=== E CALIBRATION DEBUG ===")
    print("mode:", mode_key)

    print(df[[
        "filename",
        "element_score_ranked",
        "element_score_calibration_input",
        "element_score_calibrated"
    ]].head(20))

    print("Penalty summary:")
    print(df.apply(apply_structural_penalty, axis=1).value_counts().sort_index())

    debug_cols = [
        "filename",
        *sub_cols,
        "element_score_raw",
        "element_score_ranked",
        "element_score_calibration_input",
        "element_score_calibrated",
    ]

    debug_cols = [c for c in debug_cols if c in df.columns]

    print(df[debug_cols].head())

    # --- Reconcile integer subscores once, using calibrated target ---
    final_rows = []

    for _, row in df.iterrows():
        row_dict = row.to_dict()

        reconciled = reconcile_integer_subscores(
            row=row_dict,
            keys=sub_cols,
            target_element_col="element_score_calibrated"
        )

        for k, v in reconciled.items():
            row_dict[f"{k}_final"] = v

        achieved = sum(reconciled.values()) / len(sub_cols)
        target = float(row_dict["element_score_calibrated"])

        if abs(achieved - target) > 0.25:
            row_dict["calibration_flag"] = "RECON_MISMATCH"

        final_rows.append(row_dict)

    df = pd.DataFrame(final_rows)

    # --- Final authoritative element score ---
    final_cols = [f"E{i}_final" for i in range(1, 6)]
    df["element_score_final"] = df[final_cols].mean(axis=1)

    return df

# ============================================================
# Scoring Endpoint
# ============================================================
@app.post("/score")
async def score_element_E(
    background_tasks: BackgroundTasks,
    mode: str = Form(...),
    files: List[UploadFile] = File(...)
):
    mode = (mode or "").strip().lower()
    if mode not in ("legacy", "current"):
        mode = "current"

    file_payloads = [
        {"filename": f.filename, "content": await f.read()}
        for f in files
    ]

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

            blended = "v1.3a" if mode == "legacy" else "v1.7r"

            df_one = score_documents_with_api(
                documents,
                blended_version=blended
            )

        dfs.append(df_one)
        update_progress(job_id, i + 1)

    df = pd.concat(dfs, ignore_index=True)

    df = apply_calibration_pipeline(df, mode)
    last_metrics = compute_gpt_metrics(df)

    element = get_element_from_file(__file__)
    sub_count = detect_subelement_count(df, element)
    cols = build_score_cols(element, sub_count)

    results = df[[c for c in cols if c in df.columns]].to_dict("records")

    complete_job(job_id, results)


@app.get("/progress/{job_id}")
def progress(job_id: str):
    job = get_job(job_id)
    return clean_nan_deep(job) if job else {"error": "Invalid job ID"}