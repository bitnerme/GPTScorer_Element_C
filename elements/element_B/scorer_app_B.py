from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple
import math

import pandas as pd

# =========================
# Calibration Constants (FROZEN)
# =========================
# Legacy linear calibration (variance + bias alignment)
LEGACY_A = 1.0
LEGACY_B = -0.25

# Current linear calibration (variance + bias alignment)
CURRENT_A = 1.30  
CURRENT_B = -1.4  


# =========================
# Flag Policy
# =========================

@dataclass(frozen=True)
class FlagPolicy:
    allowed: Tuple[str, ...] = ("", "ci-ok", "ok", "none")
    blocked: Tuple[str, ...] = ("ci-fail", "critical", "block", "red flag")


# =========================
# Subscore Reconciliation
# =========================

def reconcile_integer_subscores(
    row: dict,
    keys: Sequence[str],
    target_element_col: str,
    flag_suffix: str = "_flag",
    min_score: int = 0,
    max_score: int = 5,
    flag_policy: FlagPolicy = FlagPolicy(),
    preference_weight: Optional[Dict[str, float]] = None,
    soft_block_nonallowed: bool = True,
) -> Dict[str, int]:
    """
    Reconcile integer subelement scores to match the closest achievable mean
    to the calibrated target.

    B{i}       = post-rule score (input to calibration)
    B{i}_final = reconciled integer score after calibration
    """

    n = len(keys)
    if n == 0:
        return {}

    orig: Dict[str, int] = {}
    for k in keys:
        v = row.get(k, None)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            raise ValueError(f"Missing subscore {k}")
        orig[k] = int(round(float(v)))

    for k in keys:
        orig[k] = max(min_score, min(max_score, orig[k]))

    target = float(row[target_element_col])

    adjustable = []
    base_cost = {}

    def _norm_flag(x):
        return str(x).strip().lower()

    for k in keys:
        f = _norm_flag(row.get(f"{k}{flag_suffix}", ""))

        if f in flag_policy.blocked:
            base_cost[k] = float("inf")
            continue

        if f in flag_policy.allowed:
            adjustable.append(k)
            base_cost[k] = 1.0
        else:
            if soft_block_nonallowed:
                adjustable.append(k)
                base_cost[k] = 5.0
            else:
                base_cost[k] = float("inf")

    rec = orig.copy()

    if not adjustable:
        return rec

    current_sum = sum(rec.values())
    desired_sum = int(round(target * n))

    min_possible = 0
    max_possible = 0
    for k in keys:
        if k in adjustable:
            min_possible += min_score
            max_possible += max_score
        else:
            min_possible += rec[k]
            max_possible += rec[k]

    desired_sum = max(min_possible, min(max_possible, desired_sum))
    delta = desired_sum - current_sum

    if delta == 0:
        return rec

    w = preference_weight or {}

    def step_cost(k: str, direction: int) -> float:
        if base_cost[k] == float("inf"):
            return float("inf")

        pw = float(w.get(k, 1.0))
        s = rec[k]

        if direction > 0:
            directional = 1.0 + (s / max_score)
        else:
            directional = 1.0 + ((max_score - s) / max_score)

        return base_cost[k] * pw * directional

    direction = 1 if delta > 0 else -1

    # Preserve existing B behavior: cap large adjustments
    steps = min(abs(delta), 2)

    for _ in range(steps):
        best_k = None
        best_cost = float("inf")

        for k in adjustable:
            if direction > 0 and rec[k] >= max_score:
                continue
            if direction < 0 and rec[k] <= min_score:
                continue

            c = step_cost(k, direction)
            if c < best_cost:
                best_cost = c
                best_k = k

        if best_k is None or best_cost == float("inf"):
            break

        rec[best_k] += direction

    return rec


# =========================
# Calibration Pipeline (USED BY CONTROLLER)
# =========================

def apply_calibration_pipeline(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    """
    Apply frozen calibration to Element B scores.

    Expected input columns:
      B1..B2            post-rule integer-like scores
      B1_flag..B2_flag  optional adjustment flags

    Output columns added:
      element_score_raw
      element_score_target
      B1_final..B2_final
      element_score_final
    """

    df = df.copy()

    # Normalize subscores
    for k in range(1, 3):
        col = f"B{k}"
        api_col = f"B{k}_api"
        raw_col = f"B{k}_raw"
        gpt_col = f"B{k}_gpt"

        if col in df.columns:
            source = df[col]
        elif api_col in df.columns:
            source = df[api_col]
        elif raw_col in df.columns:
            source = df[raw_col]
        elif gpt_col in df.columns:
            source = df[gpt_col]
        else:
            source = pd.Series(0, index=df.index)

        df[col] = (
            pd.to_numeric(source, errors="coerce")
            .fillna(0)
            .round()
            .astype(int)
        )

    df["element_score_raw"] = df[[f"B{k}" for k in range(1, 3)]].mean(axis=1)

    # Apply frozen calibration
    if mode == "legacy":
        a = LEGACY_A
        b = LEGACY_B
    else:
        a = CURRENT_A
        b = CURRENT_B

    df["element_score_target"] = (
        a * df["element_score_raw"] + b
    ).clip(0.0, 5.0)

    # Initialize finals from post-rule scores
    for k in range(1, 3):
        df[f"B{k}_final"] = df[f"B{k}"]

    keys = [f"B{k}" for k in range(1, 3)]

    # Reconcile integer subscores to calibrated target
    for idx, row in df.iterrows():
        rec = reconcile_integer_subscores(
            row=row.to_dict(),
            keys=keys,
            target_element_col="element_score_target",
            flag_suffix="_flag",
            soft_block_nonallowed=True,
        )
        for k, v in rec.items():
            df.loc[idx, f"{k}_final"] = v

    df["element_score_final"] = df[
        [f"B{k}_final" for k in range(1, 3)]
    ].mean(axis=1)

    return df