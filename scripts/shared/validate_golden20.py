# validate_golden20.py (cleaned, safe)

import os
import sys
import json
import numpy as np
import pandas as pd
import importlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from scripts.shared.utils import extract_text_with_fallback, normalize_columns

BIAS_THRESHOLD = 0.25
MAE_THRESHOLD = 0.25
CI_THRESHOLD = 0.50


def validate_golden20(df, element, mode, json_path, doc_dir, recompute=False, rebuild_baseline=False):
    return run_validation(
        element,
        json_path,
        doc_dir,
        mode,
        recompute=recompute,
        rebuild_baseline=rebuild_baseline
    )


# ============================================================
# Config helpers
# ============================================================
def get_blended_model(element, mode):
    if element == "A":
        return "v1.0" if mode == "legacy" else "v1.2"
    if element == "B":
        return "v1.2" if mode == "legacy" else "v1.4b"
    if element == "C":
        return "v1.13" if mode == "legacy" else "v1.15"
    if element == "D":
        return "v1.8d" if mode == "legacy" else "v2.0"
    return "v1.0"


def element_has_scorer(element):
    element_dir = os.path.join(ROOT, "elements", f"element_{element}")
    config_dir = os.path.join(ROOT, "config", f"element_{element}")

    return (
        os.path.exists(os.path.join(element_dir, "golden_current_documents"))
        and os.path.exists(os.path.join(element_dir, "golden_legacy_documents"))
        and os.path.exists(os.path.join(config_dir, f"golden_{element}_current.json"))
        and os.path.exists(os.path.join(config_dir, f"golden_{element}_legacy.json"))
    )


def load_modules(element):
    score_mod = importlib.import_module(f"elements.element_{element}.score_with_API_{element}")
    app_mod = importlib.import_module(f"elements.element_{element}.scorer_app_{element}")

    return score_mod.score_document, app_mod.apply_calibration_pipeline


# ============================================================
# Core Validation
# ============================================================
def run_validation(element, json_path, doc_dir, mode, recompute=False, rebuild_baseline=False):

    CONFIG_DIR = os.path.join(ROOT, "config", f"element_{element}")

    CACHE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_current_scores.json" if mode.lower() == "current"
        else "golden20_legacy_scores.json"
    )

    score_document, apply_calibration_pipeline = load_modules(element)

    with open(json_path, encoding="utf-8") as f:
        cases = json.load(f)

    # -----------------------------
    # Cache handling
    # -----------------------------
    if recompute:
        cache = {}
    elif os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    else:
        cache = {}

    rows = []

    print(f"\nRunning Golden Validation: {mode}")
    print("------------------------------------")

    for case in cases:

        filename = case["filename"]
        expert = case["expert_score"]
        path = os.path.join(doc_dir, filename)
        blended = get_blended_model(element, mode)

        if filename in cache and not recompute:
            result = cache[filename]
        else:
            if not recompute:
                raise RuntimeError("Cache missing but recompute=False. Refusing API call.")

            content = extract_text_with_fallback(path)

            result = score_document(
                filename,
                content,
                blended_model=blended
            )

            cache[filename] = result

        rows.append({
            "filename": filename,
            "expert_score": expert,
            **result
        })

    df = pd.DataFrame(rows)

    # normalize schema
    df = normalize_columns(df, element)

    # calibration
    df = apply_calibration_pipeline(df, mode.lower())

    # fallback
    if "element_score_final" in df.columns:
        df["element_score_calibrated"] = df["element_score_final"]
    elif "element_score_api" in df.columns:
        df["element_score_calibrated"] = df["element_score_api"]

    df["abs_diff"] = np.abs(df["element_score_calibrated"] - df["expert_score"])

    top5 = df.sort_values("abs_diff", ascending=False).head(5)

    top_cases = [
        {
            "filename": row["filename"],
            "diff": float(row["abs_diff"])
        }
        for _, row in top5.iterrows()
    ]

    # -----------------------------
    # Metrics
    # -----------------------------
    diffs = df["element_score_calibrated"] - df["expert_score"]

    bias = diffs.mean()
    mae = np.abs(diffs).mean()
    std = diffs.std(ddof=1)
    n = len(diffs)

    half_ci = 1.96 * std / np.sqrt(n)
    full_ci = 2 * half_ci

    # -----------------------------
    # Baseline comparison
    # -----------------------------
    BASELINE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_metrics_current.json" if mode.lower() == "current"
        else "golden20_metrics_legacy.json"
    )

    failures = []
    status = "PASS"

    if os.path.exists(BASELINE_FILE):

        with open(BASELINE_FILE, "r") as f:
            baseline = json.load(f)

        bias_diff = abs(bias - baseline["bias"])
        mae_diff = abs(mae - baseline["mae"])
        ci_diff = abs(full_ci - baseline["ci_full"])

        if bias_diff > BIAS_THRESHOLD:
            failures.append("bias_shift")
        if mae_diff > MAE_THRESHOLD:
            failures.append("mae_shift")
        if ci_diff > CI_THRESHOLD:
            failures.append("ci_shift")

        status = "FAIL" if failures else "PASS"

    else:
        bias_diff = mae_diff = ci_diff = 0.0

    # -----------------------------
    # Save baseline if requested
    # -----------------------------
    if rebuild_baseline:
        with open(BASELINE_FILE, "w") as f:
            json.dump({
                "sample_size": int(n),
                "bias": float(bias),
                "mae": float(mae),
                "ci_half": float(half_ci),
                "ci_full": float(full_ci),
                "mean_calibrated_score": float(df["element_score_calibrated"].mean())
            }, f, indent=2)

    if recompute:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    summary = "<br>".join([
        f"Bias drift: {bias_diff:.3f}",
        f"MAE drift: {mae_diff:.3f}",
        f"CI drift: {ci_diff:.3f}"
    ])

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "mae": mae,
            "bias": bias,
            "mae_diff": mae_diff,
            "bias_diff": bias_diff
        },
        "top_cases": top_cases
    }


# ============================================================
# CLI
# ============================================================
if __name__ == "__main__":

    elements = [
        d.split("_")[1]
        for d in os.listdir(os.path.join(ROOT, "elements"))
        if d.startswith("element_")
    ]

    if len(sys.argv) > 1:
        elements = [sys.argv[1].upper()]

    for element in elements:

        if not element_has_scorer(element):
            print(f"Skipping element {element}: scorer not implemented")
            continue

        ELEMENT_DIR = os.path.join(ROOT, "elements", f"element_{element}")
        CONFIG_DIR = os.path.join(ROOT, "config", f"element_{element}")

        CURRENT_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_current_documents")
        LEGACY_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_legacy_documents")

        CURRENT_JSON = os.path.join(CONFIG_DIR, f"golden_{element}_current.json")
        LEGACY_JSON = os.path.join(CONFIG_DIR, f"golden_{element}_legacy.json")

        run_validation(element, CURRENT_JSON, CURRENT_DOC_DIR, "current")
        run_validation(element, LEGACY_JSON, LEGACY_DOC_DIR, "legacy")