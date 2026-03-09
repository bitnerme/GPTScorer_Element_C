import os
import sys
import json
import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from elements.element_C.scorer_app_C import apply_calibration_pipeline
from elements.element_C.score_with_API_C import score_document
from scripts.shared.utils import extract_text_with_fallback

BIAS_THRESHOLD = 0.25
MAE_THRESHOLD = 0.25
CI_THRESHOLD = 0.50

CONFIG_DIR = "../../config"

CURRENT_JSON = os.path.join(CONFIG_DIR, "golden_C_current.json")
LEGACY_JSON = os.path.join(CONFIG_DIR, "golden_C_legacy.json")

CURRENT_DOC_DIR = "golden_current_documents"
LEGACY_DOC_DIR = "golden_legacy_documents"

REBUILD_CACHE = False

def run_validation(json_path, doc_dir, label):

    CACHE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_current_scores.json" if label.lower() == "current" else "golden20_legacy_scores.json"
    )

    with open(json_path, encoding="utf-8") as f:
        cases = json.load(f)

    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    else:
        cache = {}

    diffs = []

    print(f"\nRunning Golden Validation: {label}")
    print("------------------------------------")

    rows = []

    for case in cases:

        filename = case["filename"]
        expert = case["expert_score"]

        path = os.path.join(doc_dir, filename)

        print("OPENING:", repr(path))

        blended = "v1.13" if label == "legacy" else "v1.15"

        if filename in cache and not REBUILD_CACHE:

            print(f"Using cached scores for {filename}")
            result = cache[filename]

        else:

            content = extract_text_with_fallback(path)
            print("TEXT LENGTH:", len(content))

            try:
                result = score_document(
                    filename,
                    content,
                    blended_model=blended
                )
            except Exception as e:
                print("Skipping case:", filename)
                print(e)
                continue

            cache[filename] = result

        row = {
            "filename": filename,
            "expert_score": expert,
            **result
        }

        rows.append(row)
    
    df = pd.DataFrame(rows)

    df = apply_calibration_pipeline(df, label.lower())

    # compute differences vs expert
    diffs = df["element_score_calibrated"] - df["expert_score"]

    bias = diffs.mean()
    mae = np.abs(diffs).mean()
    std = diffs.std(ddof=1)

    mean_score = df["element_score_calibrated"].mean()

    half_ci = 1.96 * std / np.sqrt(len(diffs))
    full_ci = 2 * half_ci
    n = len(diffs)

    print("\nSummary")
    print("-------")
    print(f"Sample size: {n}")
    print(f"Bias: {bias:.3f}")
    print(f"MAE: {mae:.3f}")
    print(f"95% CI half-width: ±{half_ci:.3f}")
    print(f"95% CI full width: {full_ci:.3f}")
    print(f"Mean calibrated score: {df['element_score_calibrated'].mean():.3f}")

    baseline_file = (
        "../../config/golden20_metrics_current.json"
        if label.lower() == "current"
        else "../../config/golden20_metrics_legacy.json"
    )

    if os.path.exists(baseline_file):

        with open(baseline_file, "r") as f:
            baseline = json.load(f)
            metrics = {
                "sample_size": int(len(diffs)),
                "bias": float(bias),
                "mae": float(mae),
                "ci_half": float(half_ci),
                "ci_full": float(full_ci),
                "mean_calibrated_score": float(mean_score)
            }

        bias_diff = abs(metrics["bias"] - baseline["bias"])
        mae_diff = abs(metrics["mae"] - baseline["mae"])
        ci_diff = abs(metrics["ci_full"] - baseline["ci_full"])

        failures = []

        if bias_diff > BIAS_THRESHOLD:
            failures.append("bias_shift")

        if mae_diff > MAE_THRESHOLD:
            failures.append("mae_shift")

        if ci_diff > CI_THRESHOLD:
            failures.append("ci_shift")

        print("\nGolden20 Regression Check")
        print("-------------------------")

        print("Baseline Bias:", baseline["bias"])
        print("Current Bias :", metrics["bias"])
        print("Diff:", bias_diff)

        print("\nBaseline MAE:", baseline["mae"])
        print("Current MAE :", metrics["mae"])
        print("Diff:", mae_diff)

        print("\nBaseline CI:", baseline["ci_full"])
        print("Current CI :", metrics["ci_full"])
        print("Diff:", ci_diff)

    SAVE_BASELINE = False

    SAVE_INTERMEDIATE = True

    #df.to_csv("golden_debug.csv", index=False)

    if REBUILD_CACHE:
        print(f"Saving cache to {CACHE_FILE}")
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

if __name__ == "__main__":

    run_validation(CURRENT_JSON, CURRENT_DOC_DIR, "current")

    run_validation(LEGACY_JSON, LEGACY_DOC_DIR, "legacy")