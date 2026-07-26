from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple, Any
import math
from core.job_manager import update_phase, update_progress,complete_job
import numpy as np
from scripts.shared.utils import sanitize_for_json
import pandas as pd
import logging
import json
import re
import openai

def _parse_reconciliation_json(response_text: str) -> Dict[str, Any]:
    """
    Parse the reconciliation API response into a dictionary.

    Accepts:
    - plain JSON
    - JSON wrapped in ```json fences
    - JSON surrounded by minor extra text
    """

    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("Narrative reconciliation returned an empty response.")

    text = response_text.strip()

    # Remove Markdown code fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    # First try the whole response.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: extract the outermost JSON object.
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise ValueError(
                "Narrative reconciliation response did not contain a JSON object."
            )

        candidate = text[start:end + 1]

        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Narrative reconciliation returned invalid JSON."
            ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            "Narrative reconciliation response must be a JSON object."
        )

    required_fields = {
        "B1_rationale",
        "B2_rationale",
        "narrative_feedback",
    }

    missing = required_fields - parsed.keys()

    if missing:
        raise ValueError(
            "Narrative reconciliation response is missing required fields: "
            + ", ".join(sorted(missing))
        )

    return parsed

logger = logging.getLogger(__name__)

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"    

STRICT_SCORE_LANGUAGE_RULES = """
    STRICT SCORE-LANGUAGE ALIGNMENT

    The wording used for each subelement MUST match its official final score.

    Score 5:
    - May use: exceptional, comprehensive, exemplary, fully developed,
    consistently thorough, outstanding.
    - Use only when the official final score is 5.

    Score 4:
    - May use: strong, thorough, well-developed, detailed, well-supported.
    - Do not use: exceptional, exemplary, comprehensive, outstanding.

    Score 3:
    - Use: adequate, generally clear, reasonably developed, sufficient,
    generally supported, satisfactory.
    - MUST NOT use: excels, exceptional, comprehensive, thorough,
    strongly developed, highly detailed, well-developed, outstanding.

    Score 2:
    - Use: limited, partial, somewhat developed, incomplete, overly general.
    - MUST NOT describe the work as strong, thorough, clear and complete,
    comprehensive, or well-developed.

    Score 1:
    - Use: minimal, sparse, weakly developed, substantially incomplete.

    Score 0:
    - State that qualifying evidence was absent, not demonstrated, or not provided.

    Before returning the JSON, inspect every evaluative adjective and verify that it
    does not imply a higher score than the official final score.
    """

NARRATIVE_RECONCILIATION_SYSTEM_PROMPT = """
You are a rigorous engineering education evaluator.

A document may be eloquent, historically important, or professionally written
while still receiving moderate engineering-rubric scores. Evaluate only its
alignment with the engineering rubric.

You will receive:
1. The student document.
2. Original subelement rationales and narrative feedback.
3. Official final subelement scores produced by the complete scoring pipeline.

The official final scores are fixed. Do not change, challenge, or recalculate them.

Your task is to revise the subelement rationales and overall narrative so they
accurately support the official final scores while remaining faithful to the
evidence in the student document.

Requirements:
- Provide a rationale for every subelement.
- Preserve valid factual observations from the original feedback.
- Do not invent evidence, weaknesses, accomplishments, or omissions.
- Moderate evaluative language when the original language is too strong for
  the final score.
- Strengthen evaluative language only when the document supports it.
- Explain limitations using actual document evidence.
- Do not mention raw scores, API scores, calibration, adjustment, prompts,
  artificial intelligence, or reconciliation.
- Do not say evidence is absent when it is present.
- Keep each subelement rationale concise and evidence-based.
- Write the overall narrative as one professional, student-facing paragraph.
- Return valid JSON only.

The overall narrative is intended for students and teachers, not rubric developers.

Do not organize the narrative by rubric subelement.

Instead, synthesize the overall performance into:
- primary strengths,
- major opportunities for improvement,
- concise recommendations.

The detailed subelement rationales already explain each rubric criterion individually.

Avoid evaluative adjectives that imply a higher performance level than the official final score.

Do not refer to the student unless the document explicitly identifies itself as student work.

...

{STRICT_SCORE_LANGUAGE_RULES}

...

""".strip()

SCORE_LANGUAGE_GUIDANCE = {
    0: {
        "preferred": [
            "not demonstrated",
            "no qualifying evidence was provided",
            "absent",
        ],
        "prohibited": [
            "adequate", "clear", "strong", "thorough",
            "well-developed", "comprehensive", "exceptional",
        ],
    },

    1: {
        "preferred": [
            "minimal",
            "sparse",
            "weakly developed",
            "substantially incomplete",
        ],
        "prohibited": [
            "adequate", "strong", "thorough",
            "well-developed", "comprehensive", "exceptional",
        ],
    },

    2: {
        "preferred": [
            "limited",
            "partial",
            "somewhat developed",
            "incomplete",
            "overly general",
        ],
        "prohibited": [
            "strong", "thorough",
            "well-developed", "comprehensive",
            "exceptional", "excels",
        ],
    },

    3: {
        "preferred": [
            "adequate",
            "generally clear",
            "reasonably developed",
            "generally supported",
            "satisfactory",
        ],
        "prohibited": [
            "excels",
            "exceptional",
            "comprehensive",
            "thorough",
            "well-developed",
            "highly detailed",
            "outstanding",
        ],
    },

    4: {
        "preferred": [
            "strong",
            "well-developed",
            "detailed",
            "well-supported",
            "generally thorough",
        ],
        "prohibited": [
            "exceptional",
            "exemplary",
            "outstanding",
        ],
    },

    5: {
        "preferred": [
            "exceptional",
            "comprehensive",
            "exemplary",
            "consistently thorough",
            "fully developed",
        ],
        "prohibited": [],
    },
}

# =========================
# Calibration Constants (FROZEN)
# =========================
# Legacy linear calibration (variance + bias alignment)
LEGACY_A = 1.0
LEGACY_B = 0.0

# Current linear calibration (variance + bias alignment)
CURRENT_A = 1.3 #1.3  
CURRENT_B = -1.3 #-1.4  


# =========================
# Flag Policy
# =========================

@dataclass(frozen=True)
class FlagPolicy:
    allowed: Tuple[str, ...] = ("", "ci-ok", "ok", "none")
    blocked: Tuple[str, ...] = ("ci-fail", "critical", "block", "red flag")

def get_editorial_adjustment(raw_score, final_score):
    """
    Generate an editorial instruction describing how much the evaluative
    language should be moderated or strengthened based on the movement
    from the original API score to the authoritative final score.

    This is an editing instruction only. It does not imply that the
    underlying evidence has changed.
    """

    if raw_score is None or final_score is None:
        return (
            "Preserve the existing evaluative tone. Make only minor wording "
            "changes needed for clarity and consistency."
        )

    raw = int(round(float(raw_score)))
    final = int(round(float(final_score)))
    delta = final - raw

    if delta <= -3:
        return (
            "Substantially reduce the intensity of the evaluative language. "
            "Remove superlatives, strong praise, broad claims of completeness, "
            "and unnecessary certainty while preserving all factual observations."
        )

    elif delta == -2:
        return (
            "Clearly reduce the intensity of the evaluative language. "
            "Replace strong praise, intensifiers, and claims of thoroughness "
            "or completeness with more measured wording."
        )

    elif delta == -1:
        return (
            "Slightly reduce the intensity of the evaluative language. "
            "Soften strong adjectives, adverbs, and certainty while preserving "
            "the underlying observations."
        )

    elif delta == 0:
        return (
            "Preserve the existing evaluative tone. "
            "Only improve wording for clarity or consistency."
        )

    elif delta == 1:
        return (
            "Slightly strengthen the evaluative language. "
            "Increase positive emphasis where supported by the existing "
            "observations without adding new evidence."
        )

    elif delta == 2:
        return (
            "Clearly strengthen the evaluative language. "
            "Use more confident positive wording while preserving the original "
            "factual observations."
        )

    else:  # delta >= 3
        return (
            "Substantially strengthen the evaluative language. "
            "Replace unnecessarily tentative judgments with clearly positive, "
            "confident wording while preserving all factual observations."
        )

def call_narrative_reconciliation_api(
    prompt: str,
    mode: str,
) -> Dict[str, Any]:
    """
    Make the second API call that reconciles feedback to official final scores.
    Uses the same model family selected by the Element B scoring system.
    """

    blended_version = "v1.2" if mode == "legacy" else "v1.4b"
    if mode == "legacy":
        gpt_model = GPT_MODEL_LEGACY
    else:
        gpt_model = GPT_MODEL_CURRENT

    messages = [
        {
            "role": "system",
            "content": NARRATIVE_RECONCILIATION_SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": prompt,
        },
    ]

    print("RECONCILIATION CALL STARTED")

    response = openai.ChatCompletion.create(
        model=gpt_model,
        messages=messages,
        temperature=0,
        top_p=1,
        max_tokens=1800,
    )

    response_text = response.choices[0].message.content

    print("Narrative reconciliation completed successfully.")

    return _parse_reconciliation_json(response_text)

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

    for step_index in range(steps):
        best_k = None
        best_cost = float("inf")

        search_order = (
            adjustable
            if step_index == 0
            else reversed(adjustable)
        )

        for k in search_order:
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

def generate_calibrated_feedback(
    row: Dict[str, Any],
    mode: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Reconcile Element B subelement rationales and overall narrative feedback
    to the official B1_final...B2_final scores.

    The original API-generated rationales and narrative are preserved in
    separate audit columns.

    Reconciliation is normally required when:
    - at least one final score differs from its post-rule score, or
    - a required rationale is missing.

    Set force=True to reconcile every scored document during testing.
    """

    result = dict(row)

    # Preserve the original narrative before replacing it.
    result.setdefault(
        "narrative_feedback_original",
        str(result.get("narrative_feedback", "") or ""),
    )

    for i in range(1, 3):
        original_col = f"B{i}_rationale_original"
        rationale_col = f"B{i}_rationale"

        result.setdefault(
            original_col,
            str(result.get(rationale_col, "") or ""),
        )

    final_scores: Dict[str, int] = {}
    original_scores: Dict[str, int] = {}

    for i in range(1, 3):
        key = f"B{i}"

        final_key = f"{key}_final"
        final_value = result.get(final_key)
        if final_value is None:
            raise ValueError(f"Missing required final score: {final_key}")

        final_scores[key] = int(round(float(final_value)))

        # Original rationales were generated using the raw/API scores.
        raw_key = f"{key}_raw"
        raw_value = result.get(raw_key)

        # Support _api as an equivalent legacy/current name.
        if raw_value is None:
            raw_key = f"{key}_api"
            raw_value = result.get(raw_key)

        if raw_value is None:
            raw_key = key
            raw_value = result.get(raw_key)

        if raw_value is None:
            raise ValueError(
                f"Missing required original score: {key}_raw, {key}_api, or {key}"
            )

        original_scores[key] = int(round(float(raw_value)))
        
    score_changed = any(
        final_scores[key] != original_scores[key]
        for key in final_scores
    )

    editorial_guidance = {
        key: {
            "original_score": original_scores[key],
            "final_score": final_scores[key],
            "delta": final_scores[key] - original_scores[key],
            "instruction": get_editorial_adjustment(
                original_scores[key],
                final_scores[key],
            ),
        }
        for key in final_scores
    }

    rationale_missing = any(
        not str(result.get(f"B{i}_rationale", "") or "").strip()
        for i in range(1, 3)
    )

    should_reconcile = force or score_changed or rationale_missing

    result["narrative_reconciliation_required"] = should_reconcile

    if not should_reconcile:
        result["narrative_reconciliation_status"] = "not_required"
        result["narrative_reconciliation_error"] = ""
        return result

    source_text = str(result.get("text", "") or "").strip()
    original_narrative = str(
        result.get("narrative_feedback_original", "") or ""
    ).strip()

    # Need enough context to safely reconcile feedback.
    # This commonly occurs during replay/CSV processing where the original
    # document text and narrative may not be available.

    if not source_text and not original_narrative:
        result["narrative_reconciliation_status"] = (
            "skipped_insufficient_feedback_context"
        )
        result["narrative_reconciliation_error"] = ""
        return result

    original_rationales = {
        key: str(row.get(f"{key}_rationale", "") or "").strip()
        for key in final_scores
    }
    
    for key in final_scores:
        rationale_col = f"{key}_rationale"
        original_col = f"{key}_rationale_original"

        if rationale_col in result:
            result.setdefault(original_col, result.get(rationale_col, ""))

    score_guidance = {
        key: {
            "official_final_score": score,
            "preferred_language":
                SCORE_LANGUAGE_GUIDANCE[score]["preferred"],
            "prohibited_language":
                SCORE_LANGUAGE_GUIDANCE[score]["prohibited"],
        }
        for key, score in final_scores.items()
    }

    prompt = f"""
    ELEMENT
    -------
    Element B

    EDITORIAL ADJUSTMENTS
    ---------------------
    The following editorial instructions are authoritative.

    They were generated before this editing step and must be followed exactly.

    Do not question, reinterpret, or explain them.

    Apply the assigned editorial instruction independently to each subelement rationale.

    {json.dumps(editorial_guidance, indent=2)}

    WRITING LEVEL GUIDE
    -------------------

    The final scores are authoritative.

    Edit the rationales and narrative so that the overall tone, reasoning, and degree of praise naturally match the final score.

    Use these score levels as a writing guide:

    5 = exceptional, comprehensive, consistently strong
    4 = strong, well developed, thorough
    3 = adequate, developed, generally supported
    2 = limited, partially developed, somewhat general, incomplete in important areas
    1 = minimal, weakly developed, little supporting evidence
    0 = not demonstrated or absent

    Do not simply replace adjectives. Rewrite the explanation so that the reasoning and overall impression are consistent with the final score while remaining evidence-based and constructive.

    ORIGINAL SUBELEMENT RATIONALES
    ------------------------------
    {json.dumps(original_rationales, indent=2)}

    ORIGINAL OVERALL NARRATIVE
    --------------------------
    {original_narrative}

    ROLE
    ----
    You are the second editor in a technical publishing workflow.

    The technical evaluation has already been completed.

    Your responsibility is editorial only.

    You are NOT evaluating the student work.
    You are NOT rescoring the student work.
    You are NOT applying the rubric.
    You are NOT determining whether the original judgments are correct.

    Your job is simply to revise the intensity of the evaluative language according to the editorial instruction provided for each rationale.

    HUMAN SCORER STYLE
    ------------------
    Write like an experienced rubric scorer rather than a promotional reviewer.

    For moderate or limited performance:
    - qualify strengths in the same sentence in which they are described;
    - avoid beginning with unqualified praise and adding limitations only afterward;
    - prefer balanced constructions such as:
    "generally clear, although..."
    "provides relevant detail, but..."
    "identifies several stakeholders, though..."
    "offers some support; however..."

    Do not force every rationale into the same sentence pattern.

    IMPORTANT DISTINCTION
    ---------------------

    Each rationale contains two different kinds of statements.

    FACTUAL OBSERVATIONS

    These describe what the student work contains.

    Examples:

    • identifies the problem
    • includes several examples
    • cites no external sources
    • discusses stakeholder groups
    • presents historical context
    • lists specific grievances

    These are factual observations.

    Preserve them.

    EVALUATIVE JUDGMENTS

    These describe how good the work is.

    Examples include:

    • very clear
    • exceptionally clear
    • comprehensive
    • extensive
    • thorough
    • excellent
    • strong
    • robust
    • convincing
    • highly effective
    • fully developed
    • well elaborated
    • clearly demonstrates

    These are editorial opinions.

    They are NOT factual observations.

    Freely modify these according to the supplied editorial instruction.

    When uncertain whether wording is factual or evaluative, treat it as evaluative.

    EDITORIAL PROCEDURE
    -------------------

    For every rationale:

    1. Preserve the factual observations.

    2. Identify every adjective, adverb, phrase or sentence that evaluates quality rather than describing content.

    3. Apply the supplied editorial instruction.

    4. Make the fewest wording changes necessary.

    5. Preserve the original organization whenever practical.

    6. Do not invent evidence.

    7. Do not remove evidence.

    8. Do not leave strong evaluative wording unchanged merely because you believe it is accurate.

    Your task is editorial revision, not technical review.

    OVERALL NARRATIVE
    -----------------

    After revising all six rationales, revise the overall narrative.

    The narrative must:

    • reflect the revised rationales;
    • preserve the original observations;
    • preserve the original recommendations;
    • match the overall editorial tone implied by the six editorial instructions;
    • not become more positive than the revised rationales;
    • not become more negative than the revised rationales;
    • not introduce new strengths or weaknesses;
    • not mention scores, calibration, AI, prompts, or editing instructions.

    FINAL CHECK
    -----------

    Before returning your response, verify:

    • every rationale follows its editorial instruction;
    • factual observations were preserved;
    • evaluative wording was revised where instructed;
    • no new evidence was introduced;
    • the narrative reflects the revised rationales.

    Return valid JSON using exactly this structure:

    {{
    "B1_rationale": "...",
    "B2_rationale": "...",
    "narrative_feedback": "..."
    }}

    Return JSON only.
    """.strip()

    try:
        reconciled = call_narrative_reconciliation_api(
            prompt=prompt,
            mode=mode,
        )

        for i in range(1, 3):
            rationale_col = f"B{i}_rationale"
            rationale = str(
                reconciled.get(rationale_col, "") or ""
            ).strip()

            if not rationale:
                raise ValueError(
                    f"Reconciliation response omitted {rationale_col}"
                )

            result[rationale_col] = rationale

        narrative = str(
            reconciled.get("narrative_feedback", "") or ""
        ).strip()

        if not narrative:
            raise ValueError(
                "Reconciliation response omitted narrative_feedback"
            )

        result["narrative_feedback"] = narrative
        result["narrative_reconciliation_status"] = "reconciled"
        result["narrative_reconciliation_error"] = ""

    except Exception as exc:
        logger.exception("Element A narrative reconciliation failed.")

        # Preserve the usable original feedback rather than failing scoring.
        result["narrative_feedback"] = result[
            "narrative_feedback_original"
        ]

        for i in range(1, 3):
            result[f"B{i}_rationale"] = result[
                f"B{i}_rationale_original"
            ]

        result["narrative_reconciliation_status"] = (
            "failed_original_preserved"
        )
        result["narrative_reconciliation_error"] = str(exc)

    
    for i in range(1, 3):
        key = f"B{i}_rationale"
        print(f"{key}: {result.get(key)}")

    print(result.get("narrative_feedback"))

    print(
        "HINTS LEAVING RECONCILIATION:",
        result.get("scoring_hints")
    )

    return result

# =========================
# Calibration Pipeline (USED BY CONTROLLER)
# =========================

def apply_calibration_pipeline(
    df: pd.DataFrame,
    mode: str,
    job_id: str | None = None,
    progress_offset: int = 0,
    reconcile_feedback: bool = True,
) -> pd.DataFrame:
    """
    Apply frozen calibration to Element A scores.

    Expected input columns:
      B1..B2         post-rule integer-like scores
      B1_flag..B2_flag  optional adjustment flags

    Output columns added:
      element_score_raw
      element_score_target
      B1_final..B2_final
      element_score_final
    """

    df = df.copy()

    def numeric_series(df, column, default=0):
        if column in df.columns:
            return pd.to_numeric(df[column], errors="coerce").fillna(default)

        return pd.Series(default, index=df.index, dtype=float)


    def ensure_text_column(df, column):
        if column not in df.columns:
            df[column] = ""
        else:
            df[column] = df[column].fillna("").astype(str)


    ensure_text_column(df, "narrative_feedback")
    ensure_text_column(df, "scoring_hints")
    ensure_text_column(df, "rationales")

    for i in range(1, 3):
        ensure_text_column(df, f"B{i}_rationale")

    print("STARTING CALIBRATION:", job_id)

    # Normalize subscores
    for k in range(1, 3):
        col = f"B{k}"
        df[col] = (
            numeric_series(df, col)
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

    print("CALIBRATION FINISHED:", job_id)

    # Build subelement-level editorial guidance from the movement between
    # the original post-rule/API score and the authoritative final score.
    for k in range(1, 3):
        raw_col = f"B{k}_api"
        final_col = f"B{k}_final"
        delta_col = f"B{k}_editorial_delta"
        instruction_col = f"B{k}_editorial_adjustment"

        df[delta_col] = df[final_col] - df[raw_col]

        df[instruction_col] = df.apply(
            lambda row, raw_col=raw_col, final_col=final_col:
                get_editorial_adjustment(
                    row[raw_col],
                    row[final_col],
                ),
            axis=1,
        )

    if job_id is not None:
        update_phase(
            job_id,
            phase="Finalizing Feedback",
            completed=progress_offset,
            total=2 * len(df),
        )

    # Reconcile the original API rationales and narrative to the now-authoritative
    # final subelement scores.
    reconciled_rows = []

    #total_docs = len(df)

    for idx, (_, row) in enumerate(df.iterrows()):

        reconciled_rows = []

        if reconcile_feedback:
            
            for idx, (_, row) in enumerate(df.iterrows()):

                reconciled_rows.append(
                    generate_calibrated_feedback(
                        row=row.to_dict(),
                        mode=mode,
                        force=True,
                    )
                )

                if job_id is not None:
                    update_progress(
                        job_id,
                        progress_offset + idx + 1,
                    )

            df = pd.DataFrame(reconciled_rows)

        else:
            print("Feedback reconciliation disabled; continuing without reconciliation.")

    print("COLUMNS AFTER RECONCILIATION:")
    print(df.columns.tolist())

    if not df.empty:
        print(
            "HINTS AFTER RECONCILIATION:",
            df.iloc[0].get("scoring_hints")
        )

    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.astype(object).where(pd.notna(df), None)

    results = sanitize_for_json(
        df.to_dict(orient="records")
    )

    if job_id is not None:
        complete_job(
            job_id,
            {
                "results": results
            },
        )

    return df
