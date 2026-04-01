from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple
import pandas as pd
import math

# =========================
# Calibration Constants (FROZEN)
# =========================

# Legacy is bias only
LEGACY_BIAS_OFFSET = 0.72

# Current linear calibration (variance + bias alignment)
CURRENT_A = 1.289
CURRENT_B = -1.267


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

    n = len(keys)
    if n == 0:
        return {}

    orig = {}
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

    for _ in range(abs(delta)):
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

    # Normalize subscores
    for k in range(1, 7):
        df[f"C{k}"] = (
            pd.to_numeric(df.get(f"C{k}"), errors="coerce")
            .fillna(0)
            .round()
            .astype(int)
        )

    df["element_score_raw"] = df[[f"C{k}" for k in range(1, 7)]].mean(axis=1)

    # Apply calibration (FROZEN MODELS)
    if mode == "legacy":
        a = 1.0
        b = LEGACY_BIAS_OFFSET
    else:
        a = CURRENT_A
        b = CURRENT_B

    df["element_score_calibrated"] = (
        a * df["element_score_raw"] + b
    ).clip(0.0, 5.0)

    # Initialize finals
    for k in range(1, 7):
        df[f"C{k}_final"] = df[f"C{k}"]

    keys = [f"C{k}" for k in range(1, 7)]

    # Reconcile to calibrated target
    for idx, row in df.iterrows():
        rec = reconcile_integer_subscores(
            row=row.to_dict(),
            keys=keys,
            target_element_col="element_score_calibrated",
            flag_suffix="_flag",
            soft_block_nonallowed=True
        )
        for k, v in rec.items():
            df.loc[idx, f"{k}_final"] = v

    df["element_score_final"] = df[
        [f"C{k}_final" for k in range(1, 7)]
    ].mean(axis=1)

    return df