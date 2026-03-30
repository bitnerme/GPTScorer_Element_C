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
    report = run_validation(
        element,
        json_path,
        doc_dir,
        mode,
        recompute=recompute,
        rebuild_baseline=rebuild_baseline
    )
    return report

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

def element_has_scorer(el):

    element_dir = os.path.join(ROOT, "elements", f"element_{element}")
    config_dir = os.path.join(ROOT, "config", f"element_{element}")

    current_dir = os.path.join(element_dir, "golden_current_documents")
    legacy_dir = os.path.join(element_dir, "golden_legacy_documents")

    current_json = os.path.join(config_dir, f"golden_{element}_current.json")
    legacy_json = os.path.join(config_dir, f"golden_{element}_legacy.json")

    return (
        os.path.exists(current_dir)
        and os.path.exists(legacy_dir)
        and os.path.exists(current_json)
        and os.path.exists(legacy_json)
    )

def load_modules(element):
    score_mod = importlib.import_module(f"elements.element_{element}.score_with_API_{element}")
    app_mod = importlib.import_module(f"elements.element_{element}.scorer_app_{element}")

    score_document = score_mod.score_document
    apply_calibration_pipeline = app_mod.apply_calibration_pipeline

    return score_document, apply_calibration_pipeline

def run_validation(element, json_path, doc_dir, mode, recompute=False, rebuild_baseline=False):

    ELEMENT_DIR = os.path.join(ROOT, "elements", f"element_{element}")
    CONFIG_DIR = os.path.join(ROOT, "config", f"element_{element}")
    
    CACHE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_current_scores.json" if mode.lower() == "current" else "golden20_legacy_scores.json"
    )
    
    print("MODE:", mode)
    print("CONFIG_DIR:", CONFIG_DIR)
    print("CACHE_FILE:", CACHE_FILE)
    print("CACHE EXISTS:", os.path.exists(CACHE_FILE))

    REBUILD_CACHE = recompute
    PROMOTE_TO_BASELINE = rebuild_baseline

    if REBUILD_CACHE and PROMOTE_TO_BASELINE:
        print("⚠️ WARNING: You are rebuilding cache AND promoting baseline in one run.")

    score_document, apply_calibration_pipeline = load_modules(element)

    with open(json_path, encoding="utf-8") as f:
        cases = json.load(f)

    if REBUILD_CACHE:
        print("Rebuilding cache — ignoring existing cache file")
        cache = {}
    elif os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
    else:
        cache = {}

    diffs = []

    print(f"\nRunning Golden Validation: {mode}")
    print("------------------------------------")

    rows = []

    for case in cases:

        filename = case["filename"]
        expert = case["expert_score"]

        path = os.path.join(doc_dir, filename)

        blended = get_blended_model(element,mode) 

        print("OPENING:", repr(path))

        if filename in cache and not REBUILD_CACHE:

            print(f"Using cached scores for {filename}")
            result = cache[filename]

        else:

            if not REBUILD_CACHE:
                raise RuntimeError("Cache missing but REBUILD_CACHE is False. Refusing to call API.")

            content = extract_text_with_fallback(path)

            result = score_document(
                filename,
                content,
                blended_model=blended
            )

            cache[filename] = result

        row = {
            "filename": filename,
            "expert_score": expert,
            **result
        }

        rows.append(row)
    
    df = pd.DataFrame(rows)

    # compatibility bridge for cached schemas
  
    df = normalize_columns(df, element) 

    df = apply_calibration_pipeline(df, mode.lower())

    if "element_score_final" in df.columns:
        df["element_score_calibrated"] = df["element_score_final"]
    elif "element_score_calibrated" not in df.columns and "element_score_api" in df.columns:
        df["element_score_calibrated"] = df["element_score_api"]

    # compute differences vs expert
    diffs = df["element_score_calibrated"] - df["expert_score"]

    bias = diffs.mean()
    mae = np.abs(diffs).mean()
    std = diffs.std(ddof=1)

    mean_score = df["element_score_calibrated"].mean()

    # =========================
    # TOP ERROR CASES (DEBUG)
    # =========================
    df["abs_diff"] = np.abs(df["element_score_calibrated"] - df["expert_score"])

    top5 = df.sort_values("abs_diff", ascending=False).head(5)

    print("\nTop 5 Worst Cases")
    print("------------------")

    for _, row in top5.iterrows():
        print(f"{row['filename']}: diff={row['abs_diff']:.3f}")

    print("\nDetailed Debug (Top Case)")
    print("--------------------------")

    row = top5.iloc[0]

    top_cases = [
        {
            "filename": row["filename"],
            "diff": float(row["abs_diff"])
        }
        for _, row in top5.iterrows()
    ]

    print("Filename:", row["filename"])
    print("Expert:", row["expert_score"])
    print("Model :", row["element_score_calibrated"])

    prefix = element  # "A", "B", "C", or "D"

    # detect subelements dynamically
    sub_keys = sorted([
        k for k in df.columns
        if k.startswith(prefix) and k[1:].isdigit()
    ])

    for k in sub_keys:
        raw = row.get(k)
        final = row.get(f"{k}_final", raw)
        print(f"{k}: {raw} → {final}")

    BASELINE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_metrics_current.json" if mode.lower() == "current"
        else "golden20_metrics_legacy.json"
    )

    CANDIDATE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_metrics_current_candidate.json" if mode == "current"
        else "golden20_metrics_legacy_candidate.json"
    )

    print("Baseline file path:", BASELINE_FILE)
    print("Candidate file path:", CANDIDATE_FILE)

    # =========================
    # SUMMARY
    # =========================
    n = len(diffs)

    half_ci = 1.96 * std / np.sqrt(n)
    full_ci = 2 * half_ci

    title = f"Summary (Element {element} — {element.capitalize()})"
    print("\n" + title)
    print("-" * len(title))

    print(f"Sample size: {n}")
    print(f"Bias: {bias:.3f}")
    print(f"MAE: {mae:.3f}")
    print(f"95% CI half-width: ±{half_ci:.3f}")
    print(f"95% CI full width: {full_ci:.3f}")
    print(f"Mean calibrated score: {df['element_score_calibrated'].mean():.3f}")

    print("DEBUG: reached post-summary")

    print("Baseline file path:", BASELINE_FILE)
    print("Baseline exists:", os.path.exists(BASELINE_FILE))

    if os.path.exists(BASELINE_FILE):

        with open(BASELINE_FILE, "r") as f:
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

        # =========================
        # REGRESSION VERDICT
        # =========================
        title = f"Regression Verdict (Element {element} — {element.capitalize()})"
        print("\n" + title)
        print("-" * len(title))

        failures = []

        if bias_diff > BIAS_THRESHOLD:
            failures.append(f"bias_shift ({bias_diff:.3f})")

        if mae_diff > MAE_THRESHOLD:
            failures.append(f"mae_shift ({mae_diff:.3f})")

        if ci_diff > CI_THRESHOLD:
            failures.append(f"ci_shift ({ci_diff:.3f})")

        status = "FAIL" if failures else "PASS"

        if status == "FAIL":
            print("❌ FAIL")
            for f in failures:
                print(" -", f)
        else:
            print("✅ PASS (within thresholds)")

        print("\nThresholds")
        print("----------")
        print(f"Bias threshold: {BIAS_THRESHOLD}")
        print(f"MAE threshold : {MAE_THRESHOLD}")
        print(f"CI threshold  : {CI_THRESHOLD}")

        title = f"Metric Deltas (Element {element} — {mode.capitalize()})"
        print("\n" + title)
        print("-" * len(title))

        ci_half_diff = abs(half_ci - baseline.get("ci_half", baseline["ci_full"] / 2))

        print(f"Bias Δ: {bias_diff:.3f}")
        print(f"MAE  Δ: {mae_diff:.3f}")
        print(f"CI half Δ: {ci_half_diff:.3f}")
        print(f"CI full Δ: {ci_diff:.3f}")

        title = f"Golden20 Regression Check (Element {element} — {mode.capitalize()})"
        print("\n" + title)
        print("-" * len(title))

        print(f"Baseline Bias: {baseline['bias']:.3f}")
        print(f"Current Bias : {bias:.3f}")
        print(f"Diff         : {bias_diff:.3f}")

        print(f"\nBaseline MAE: {baseline['mae']:.3f}")
        print(f"Current MAE : {mae:.3f}")
        print(f"Diff        : {mae_diff:.3f}")

        print(f"\nBaseline CI half: ±{baseline.get('ci_half', baseline['ci_full']/2):.3f}")
        print(f"Current CI half : ±{half_ci:.3f}")
        print(f"Diff            : {ci_half_diff:.3f}")

        print(f"\nBaseline CI full: {baseline['ci_full']:.3f}")
        print(f"Current CI full : {full_ci:.3f}")
        print(f"Diff            : {ci_diff:.3f}")

    if PROMOTE_TO_BASELINE:
        print(f"\n💾 Saving baseline metrics to {BASELINE_FILE}")
        with open(BASELINE_FILE, "w") as f:
            json.dump({
                "sample_size": int(n),
                "bias": float(bias),
                "mae": float(mae),
                "ci_half": float(half_ci),
                "ci_full": float(full_ci),
                "mean_calibrated_score": float(mean_score)
            }, f, indent=2)

    if REBUILD_CACHE:
        print(f"Saving cache to {CACHE_FILE}")
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)

    # -----------------------------
    # Regression summary (KEEP THIS)
    # -----------------------------
    summary_lines = []

    summary_lines.append(f"Bias drift: {bias_diff:.3f}")
    summary_lines.append(f"MAE drift: {mae_diff:.3f}")
    summary_lines.append(f"CI drift: {ci_diff:.3f}")

    summary = "<br>".join(summary_lines) if summary_lines else "No regression issues detected."

    print("DEBUG REGRESSION:")
    print("mae_diff:", mae_diff)
    print("bias_diff:", bias_diff)
    print("ci_diff:", ci_diff)
    print("status:", status)

    return {
        "status": status,
        "summary": summary,
        "metrics": {
            "mae": mae,
            "mae_diff": mae_diff,
            "bias": bias,
            "bias_diff": bias_diff
        },
        "top_cases": top_cases   
    }

if __name__ == "__main__":

    elements = [
        d.split("_")[1]
        for d in os.listdir(os.path.join(ROOT, "elements"))
        if d.startswith("element_")
    ]

     # If user passed an element argument, restrict to that element
    if len(sys.argv) > 1:
        elements = [sys.argv[1].upper()]

    for element in elements:

        ELEMENT_DIR = os.path.join(ROOT, "elements", f"element_{element}")

        CONFIG_DIR = os.path.join(ROOT, "config", f"element_{element}")

        CURRENT_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_current_documents")
        LEGACY_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_legacy_documents")

        CURRENT_JSON = os.path.join(CONFIG_DIR, f"golden_{element}_current.json")
        LEGACY_JSON = os.path.join(CONFIG_DIR, f"golden_{element}_legacy.json")

        if not element_has_scorer(el):
            print(f"Skipping element {element}: scorer not implemented")
            continue

        run_validation(el, CURRENT_JSON, CURRENT_DOC_DIR, "current")
        run_validation(el, LEGACY_JSON, LEGACY_DOC_DIR, "legacy")