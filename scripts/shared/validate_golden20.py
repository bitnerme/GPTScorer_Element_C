import os
import sys
import json
import numpy as np
import pandas as pd
import importlib

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from scripts.shared.utils import extract_text_with_fallback

BIAS_THRESHOLD = 0.25
MAE_THRESHOLD = 0.25
CI_THRESHOLD = 0.50

REBUILD_CACHE = False
SAVE_INTERMEDIATE = True
PROMOTE_TO_BASELINE = False

if REBUILD_CACHE and PROMOTE_TO_BASELINE:
    print("⚠️ WARNING: You are rebuilding cache AND promoting baseline in one run.")

def get_blended_model(el, label):

    if el == "A":
        return "v1.0" if label == "legacy" else "v1.2"

    if el == "C":
        return "v1.13" if label == "legacy" else "v1.15"

    if el == "D":
        return "v1.8d" if label == "legacy" else "v2.0"

    return "v1.0"

def element_has_scorer(el):

    element_dir = os.path.join(ROOT, "elements", f"element_{el}")
    config_dir = os.path.join(ROOT, "config", f"element_{el}")

    current_dir = os.path.join(element_dir, "golden_current_documents")
    legacy_dir = os.path.join(element_dir, "golden_legacy_documents")

    current_json = os.path.join(config_dir, f"golden_{el}_current.json")
    legacy_json = os.path.join(config_dir, f"golden_{el}_legacy.json")

    return (
        os.path.exists(current_dir)
        and os.path.exists(legacy_dir)
        and os.path.exists(current_json)
        and os.path.exists(legacy_json)
    )

def load_modules(el):
    score_mod = importlib.import_module(f"elements.element_{el}.score_with_API_{el}")
    app_mod = importlib.import_module(f"elements.element_{el}.scorer_app_{el}")

    score_document = score_mod.score_document
    apply_calibration_pipeline = app_mod.apply_calibration_pipeline

    return score_document, apply_calibration_pipeline

def run_validation(el, json_path, doc_dir, label):

    ELEMENT_DIR = os.path.join(ROOT, "elements", f"element_{el}")
    CONFIG_DIR = os.path.join(ROOT, "config", f"element_{el}")
    
    CACHE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_current_scores.json" if label.lower() == "current" else "golden20_legacy_scores.json"
    )
    
    print("CACHE FILE:", CACHE_FILE)
    print("CACHE EXISTS:", os.path.exists(CACHE_FILE))

    score_document, apply_calibration_pipeline = load_modules(el)

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

    print(f"\nRunning Golden Validation: {label}")
    print("------------------------------------")

    rows = []

    for case in cases:

        filename = case["filename"]
        expert = case["expert_score"]

        path = os.path.join(doc_dir, filename)

        blended = get_blended_model(el, label) 

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
    for k in range(1,7):

        # new schema (_1,_2)
        if f"A{k}" not in df.columns and f"_{k}" in df.columns:
            df[f"A{k}"] = df[f"_{k}"]

        # new schema with _final
        if f"A{k}" not in df.columns and f"_{k}_final" in df.columns:
            df[f"A{k}"] = df[f"_{k}_final"]

        # old C schema (C1,C2,...)
        if f"A{k}" not in df.columns and f"C{k}" in df.columns:
            df[f"A{k}"] = df[f"C{k}"]

    df = apply_calibration_pipeline(df, label.lower())

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

    print("Filename:", row["filename"])
    print("Expert:", row["expert_score"])
    print("Model :", row["element_score_calibrated"])

    prefix = el  # "A", "C", or "D"

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
        "golden20_metrics_current.json" if label.lower() == "current"
        else "golden20_metrics_legacy.json"
    )

    CANDIDATE_FILE = os.path.join(
        CONFIG_DIR,
        "golden20_metrics_current_candidate.json" if label.lower() == "current"
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

    title = f"Summary (Element {el} — {label.upper()})"
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
        title = f"Regression Verdict (Element {el} — {label.upper()})"
        print("\n" + title)
        print("-" * len(title))

        failures = []

        if bias_diff > BIAS_THRESHOLD:
            failures.append(f"bias_shift ({bias_diff:.3f})")

        if mae_diff > MAE_THRESHOLD:
            failures.append(f"mae_shift ({mae_diff:.3f})")

        if ci_diff > CI_THRESHOLD:
            failures.append(f"ci_shift ({ci_diff:.3f})")

        if failures:
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

        title = f"Metric Deltas (Element {el} — {label.upper()})"
        print("\n" + title)
        print("-" * len(title))

        ci_half_diff = abs(half_ci - baseline.get("ci_half", baseline["ci_full"] / 2))

        print(f"Bias Δ: {bias_diff:.3f}")
        print(f"MAE  Δ: {mae_diff:.3f}")
        print(f"CI half Δ: {ci_half_diff:.3f}")
        print(f"CI full Δ: {ci_diff:.3f}")

        failures = []

        if bias_diff > BIAS_THRESHOLD:
            failures.append("bias_shift")

        if mae_diff > MAE_THRESHOLD:
            failures.append("mae_shift")

        if ci_diff > CI_THRESHOLD:
            failures.append("ci_shift")

        title = f"Golden20 Regression Check (Element {el} — {label.upper()})"
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

if __name__ == "__main__":

    elements = [
        d.split("_")[1]
        for d in os.listdir(os.path.join(ROOT, "elements"))
        if d.startswith("element_")
    ]

     # If user passed an element argument, restrict to that element
    if len(sys.argv) > 1:
        elements = [sys.argv[1].upper()]

    for el in elements:

        ELEMENT_DIR = os.path.join(ROOT, "elements", f"element_{el}")

        CONFIG_DIR = os.path.join(ROOT, "config", f"element_{el}")

        CURRENT_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_current_documents")
        LEGACY_DOC_DIR = os.path.join(ELEMENT_DIR, "golden_legacy_documents")

        CURRENT_JSON = os.path.join(CONFIG_DIR, f"golden_{el}_current.json")
        LEGACY_JSON = os.path.join(CONFIG_DIR, f"golden_{el}_legacy.json")

        if not element_has_scorer(el):
            print(f"Skipping element {el}: scorer not implemented")
            continue

        run_validation(el, CURRENT_JSON, CURRENT_DOC_DIR, "current")
        run_validation(el, LEGACY_JSON, LEGACY_DOC_DIR, "legacy")