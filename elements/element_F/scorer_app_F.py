import os
import json5
import json
import re
import argparse
import pandas as pd
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import openai
import pytesseract
from scripts.shared.utils import extract_text_with_fallback
import traceback
import shutil

SCORING_MODE = "single_evidence"
# options: "single_evidence", "ensemble_average"

NARRATIVE_MODE = "single"
# options: "single", "synthesized"

#Calibration Constants
LEGACY_A = 1.0
LEGACY_B = 0.0
CURRENT_A = 0.86
CURRENT_B = 0.0

# ============================================================
# Calibration Pipeline
# ============================================================
def apply_calibration_pipeline(df, mode):

    df = df.copy()

    df["F1"] = pd.to_numeric(df.get("F1_api", df.get("F1", 0)), errors="coerce").fillna(0)

    df["element_score_raw"] = df["F1"]

    if mode == "legacy":
        a, b = LEGACY_A, LEGACY_B
    else:
        a, b = CURRENT_A, CURRENT_B

    print("F CALIBRATION MODE:", mode, "A:", a, "B:", b)

    df["element_score_target"] = (a * df["element_score_raw"] + b).clip(0, 5)

    # Element F has only one subscore, so no reconciliation across subelements is possible.
    df["F1_final"] = df["element_score_target"].round().clip(0, 5).astype(int)

    df["element_score_final"] = df["F1_final"]

    print(df[["element_score_raw", "element_score_target", "F1_final", "element_score_final"]].head(10))

    return df

# =========================
# ELEMENT F RULE ENGINE
# =========================

def apply_element_f_rules(response_dict, blended_model):
    """
    Element F rule engine.

    Preserves pure API score as F1_api.
    Creates F1_rule as the post-rule score.
    Current first-pass rules are conservative caps only.
    """

    # Normalize API score into temporary F1
    response_dict["F1"] = int(
        response_dict.get("F1", response_dict.get("F1_api", 0))
    )

    # Always preserve API score
    response_dict["F1_api"] = int(
        response_dict.get("F1_api", response_dict["F1"])
    )

    response_dict["F1_flag"] = ""

    BYPASS_RULES = True

# Legacy v1.2: apply conservative false-positive caps
    if blended_model == "v1.2":

        if BYPASS_RULES:
            response_dict["F1_rule"] = int(response_dict["F1_api"])
            response_dict["F1_flag"] = "rules bypassed"

        else:
            api_score = response_dict["F1_api"]

            text = str(
                response_dict.get("_source_text", response_dict.get("text", ""))
            ).lower()
            rationale = str(response_dict.get("F1_rationale", "")).lower()
            combined = f"{text} {rationale}"

            false_positive_markers = [
                "decision matrix",
                "design matrix",
                "criteria matrix",
                "pugh matrix",
                "brainstorm",
                "brainstorming",
                "we chose",
                "we selected",
                "we decided",
                "selected design",
                "final design",
                "design idea",
                "design ideas",
                "design option",
                "design options",
                "design alternatives",
                "multiple ideas",
                "concept selection",
                "pros and cons",
                "advantages and disadvantages",
            ]

            strong_viability_markers = [
                "test results",
                "measured data",
                "experimental data",
                "prototype testing",
                "user testing",
                "stakeholder feedback",
                "expert feedback",
                "evidence shows",
                "quantitative evidence",
                "successful test",
                "risk mitigation",
                "failure mode",
            ]

            future_testing_markers = [
                "will test",
                "would test",
                "plan to test",
                "planning to test",
                "future testing",
                "test later",
                "after testing",
                "once we test",
            ]

            false_hits = sum(1 for m in false_positive_markers if m in combined)
            strong_hits = sum(1 for m in strong_viability_markers if m in combined)
            future_hits = sum(1 for m in future_testing_markers if m in combined)

            if api_score >= 3 and false_hits >= 1 and strong_hits <= 1:
                response_dict["F1"] = max(response_dict["F1"] - 1, 1)
                response_dict["F1_flag"] = "soften rule: design selection or description without viability evidence"

            elif api_score >= 3 and future_hits >= 1 and strong_hits == 0:
                response_dict["F1"] = max(response_dict["F1"] - 1, 1)
                response_dict["F1_flag"] = "soften rule: future testing without current viability evidence"

            response_dict["F1_rule"] = int(response_dict["F1"])

            if api_score >= 3:
                print("RULE DEBUG:", {
                    "api": api_score,
                    "false_hits": false_hits,
                    "strong_hits": strong_hits,
                    "future_hits": future_hits,
                    "flag": response_dict.get("F1_flag", "")
                })

       # Current v1.62: for now, no rule engine unless you later want one
    elif blended_model == "v1.62":
        response_dict["F1_rule"] = int(response_dict["F1_api"])

    else:
        raise ValueError(f"Unsupported blended_model version: {blended_model}")

    response_dict["element_score_api"] = response_dict["F1_api"]
    response_dict["element_score_raw"] = response_dict["element_score_api"]
    response_dict["element_score_rule"] = response_dict["F1_rule"]

    # Remove ambiguous bare score
    response_dict.pop("F1", None)

    return response_dict

def synthesize_narrative_feedback(
    content,
    final_score,
    rationales,
    blended_model,
):

    rationale_text = "\n\n".join(
        [f"Rationale {i+1}: {r}" for i, r in enumerate(rationales)]
    )

    prompt = f"""
You are generating professional student-facing engineering feedback.

The final calibrated Element F score is:
{final_score}

The following independent scoring rationales were generated:

{rationale_text}

Student document:
\"\"\"{content}\"\"\"

Write a coherent narrative feedback summary.

Requirements:
- 170–220 words
- 2–3 paragraphs
- Professional and constructive tone
- Explain strengths and weaknesses clearly
- Discuss viability realism, evidence quality, risks, constraints, or tradeoffs when relevant
- Give 2–4 specific actionable recommendations
- Do NOT mention AI, calibration, multiple passes, or scoring mechanics
- Do NOT contradict the final score

Tone calibration:
For a score of 3, describe strengths as emerging, partial, or promising rather than strong.
Avoid language that implies the response is well-supported overall.
Make clear that the viability judgment is plausible but only partially supported.

Return ONLY valid JSON:

{{
  "narrative_feedback": "..."
}}
"""

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    gpt_model = get_gpt_model(blended_model)

    response = openai.ChatCompletion.create(
        model=gpt_model,
        messages=messages,
        temperature=0.2,
        top_p=1,
        max_completion_tokens=2500
    )

    response_str = response.choices[0].message.content.strip()

    if response_str.startswith("```"):
        response_str = "\n".join(
            line for line in response_str.splitlines()
            if not line.strip().startswith("```")
        ).strip()

    first_brace = response_str.find("{")
    if first_brace != -1:
        response_str = response_str[first_brace:]

    last_brace = response_str.rfind("}")
    if last_brace != -1:
        response_str = response_str[: last_brace + 1]

    cleaned = clean_json_string(response_str)

    try:
        parsed = json5.loads(cleaned)
        return parsed.get("narrative_feedback", "")
    except Exception:
        return ""

def score_document_ensemble(filename, content, blended_model):
    pass_results = []

    for prompt_variant in PROMPT_VARIANTS:
        result = score_document(
            filename=filename,
            content=content,
            blended_model=blended_model,
            prompt_variant=prompt_variant,
            generate_narrative=False
        )
        pass_results.append(result)

    for prompt_variant in PROMPT_VARIANTS:
        result = score_document(
            filename=filename,
            content=content,
            blended_model=blended_model,
            prompt_variant=prompt_variant,
            generate_narrative=False
        )

        result["prompt_variant"] = prompt_variant
        pass_results.append(result)

    scores = [float(r["F1_api"]) for r in pass_results]
    mean_score = sum(scores) / len(scores)
    final_score = int(max(0, min(5, round(mean_score))))

    final = pass_results[0].copy()

    final_narrative = synthesize_narrative_feedback(
        content=content,
        final_score=final_score,
        rationales=[
            pass_results[0].get("F1_rationale", ""),
            pass_results[1].get("F1_rationale", ""),
            pass_results[2].get("F1_rationale", ""),
        ],
        blended_model=blended_model,
    )

    final["narrative_feedback"] = final_narrative

    final["F1_api"] = final_score
    final["F1_api_mean"] = mean_score
    final["F1_api_pass1"] = scores[0]
    final["F1_api_pass2"] = scores[1]
    final["F1_api_pass3"] = scores[2]

    final["F1_rationale_pass1"] = pass_results[0].get("F1_rationale", "")
    final["F1_rationale_pass2"] = pass_results[1].get("F1_rationale", "")
    final["F1_rationale_pass3"] = pass_results[2].get("F1_rationale", "")

    final["element_score_api"] = final_score
    final["element_score_raw"] = final_score

    final = apply_element_f_rules(final, blended_model)

    return final

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"   

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element F rubric consistently and professionally.

Your role is to evaluate student engineering design documentation and assign scores for each rubric criterion.

SCORING PRINCIPLES:

- Base scores strictly on explicit evidence in the student document.
- Do not assume missing elements are present.
- Do not reward effort or intent unless supported by clear evidence.
- When evidence is incomplete, award partial credit rather than full credit.
- Distinguish clearly between weak, adequate, strong, and exceptional submissions.
- Use the full 0–5 scoring scale when justified.

Always return valid JSON exactly in the requested format."""

NARRATIVE_INSTRUCTION = """
After assigning the F1 score and rationale, write a 170–220 word narrative feedback summary.

The summary must:
- Be written in 2–3 clear paragraphs.
- Explain the strength of the student's design viability judgment.
- Discuss how well the response connects viability to design requirements.
- Discuss the quality of credible evidence, risk analysis, constraints, or failure-mode reasoning.
- Provide 2–4 specific, actionable recommendations for improvement.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, calibration, or JSON.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

PROMPT_VARIANTS = [
    "strict_rubric",
    "evidence_focused",
    "risk_viability",
]

RUBRIC_PATH = Path(__file__).resolve().parent / "Element_F_MyDesign_Scoring_Rubric.txt"

print("RUBRIC PATH:", RUBRIC_PATH)

with open(RUBRIC_PATH, "r", encoding="utf-8") as f:
    RUBRIC_TEXT = f.read().strip()  

def sanitize_for_json(text: str) -> str:
    if text is None:
        return ""

    # Remove NULs
    text = text.replace("\x00", "")

    # Remove other problematic control chars except common whitespace
    text = re.sub(r"[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)

    # Drop unpaired surrogates / invalid unicode for JSON encoding
    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    return text

def clean_json_string(raw):
    # Remove trailing commas before } or ]
    return re.sub(r',\s*([}\]])', r'\1', raw)

def get_gpt_model(blended_version: str) -> str:
    """
    Selects the correct GPT model based on blended model version.
    """
    if blended_version == "v1.2":
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.62":
        return GPT_MODEL_CURRENT
    else:
        raise ValueError(f"Unknown blended model version: {blended_version}")

def is_truncated_json(text: str) -> bool:
    """
    Detects likely truncated JSON responses.
    """
    if not text:
        return True

    text = text.strip()

    # Must start with {
    if not text.startswith("{"):
        return False

    open_braces = text.count("{")
    close_braces = text.count("}")

    # Mismatched braces → likely truncated
    if close_braces < open_braces:
        return True

    # Doesn't properly end
    if not text.endswith("}"):
        return True

    return False


def score_document(
    filename,
    content,
    blended_model,
    prompt_variant="strict_rubric",
    generate_narrative=True
):
    content = sanitize_for_json(content)
    filename = sanitize_for_json(filename)

    # Choose prompt based on model
    if blended_model == "v1.62":
        if generate_narrative:
            json_format_instruction = """
            Return only valid JSON in exactly this format:

            {
            "F1": {
                "score": X,
                "rationale": "Briefly explain the score using evidence from the document."
            },
            "narrative_feedback": ""
            }
            """
        else:
            json_format_instruction = """
        Return only valid JSON in exactly this format:

        {
        "F1": {
            "score": X,
            "rationale": "Briefly explain the score using evidence from the document."
        }
        }
        """
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring MyDesign Element F, sub-element F1 only.

        Element F asks: What could possibly go wrong with the proposed design, and is the design realistically viable?

        Evaluate only F1: Design viability judgment.

        A strong F1 response should:
        - Make an explicit judgment about whether the proposed design is viable/realistic.
        - Connect that judgment to one or more design requirements.
        - Support the judgment with specific credible evidence present in this document and clearly connected to the proposed design.
        - Consider constraints, risks, tradeoffs, or failure modes.
        - Explain why the evidence shows the design can realistically solve the problem.

        Credible evidence may include:
        - Test results or proof-of-concept results
        - Cost tables or material constraints
        - Expert/stakeholder feedback
        - Demonstrated subsystem performance
        - Analogous examples or precedent
        - Specific risk mitigation reasoning
        - Specific design requirement analysis

        Do not give credit for evidence that is only planned, promised, or implied.

        Scoring rules:
        - Score 0: No viability discussion, blank, or irrelevant.
        - Score 1: Viability is asserted but unsupported or very vague.
        - Score 2: Viability is discussed but mostly speculative, generic, or weakly supported.
        - Score 3: Clear viability judgment supported by at least one specific, design-relevant reason or piece of evidence, though support may still be limited or incomplete.
        - Score 4: Realistic viability judgment with clear connection to one or more design requirements and supported by specific credible evidence.
        - Score 5: Strong, realistic viability judgment with multiple pieces of credible evidence and clear consideration of constraints, tradeoffs, or risks.

        Guidance:
        - Give partial credit when reasoning is present but incomplete.
        - Do not require formal testing for higher scores if reasoning and evidence are credible.
        - Do not infer evidence that is not explicitly present.
        - Do not assign scores above 3 if the evidence is generic, not specific to the design, or does not clearly demonstrate that the design will work in practice.
        - Distinguish between plausible reasoning and demonstrated viability support.

        Caps and safeguards:
        - If the response only describes the design without evaluating viability, cap at 1.
        - If the response relies mainly on future testing, planned validation, or unsupported expectations rather than present evidence, cap at 2.
        - If template placeholders are present but not filled in, cap at 1.
        - If requirements are not referenced, do not assign scores above 3.

        {json_format_instruction}
        """
        
    elif blended_model == "v1.2":

        if generate_narrative:
            json_format_instruction = """
            Return only valid JSON in exactly this format:

            {
            "F1": {
                "score": X,
                "rationale": "Briefly explain the score using evidence from the document."
            },
            "narrative_feedback": ""
            }
            """
        else:
            json_format_instruction = """
        Return only valid JSON in exactly this format:

        {
        "F1": {
            "score": X,
            "rationale": "Briefly explain the score using evidence from the document."
        }
        }
        """
        
        if prompt_variant == "strict_rubric":
            variant_instruction = """
            Apply the rubric strictly and holistically. Use the full 0-5 scale.
            """
        elif prompt_variant == "evidence_focused":
            variant_instruction = """
            Focus especially on credible evidence: testing, measurements, cost evidence,
            expert or stakeholder input, proof-of-concept results, or requirement-linked support.
            Use score 3 when evidence is partial but specific and credible enough to support
            a plausible viability judgment.
            """
        elif prompt_variant == "risk_viability":
            variant_instruction = """
            Focus especially on risks, limitations, constraints, failure modes, mitigation plans,
            tradeoffs, and tracking. Do not create a separate risk score; use this only to judge
            whether the viability judgment is realistic and well supported.
            """
        else:
            raise ValueError(f"Unknown prompt_variant: {prompt_variant}")

        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring MyDesign Element F, sub-element F1 only.

        Evaluate the student's judgment about whether the proposed design is viable and realistic.

        Do NOT treat the following as evidence of viability by themselves:
        - brainstorming multiple ideas
        - decision matrices
        - choosing between concepts
        - describing design features
        - explaining how the design works

        Consider:
        - Realism of the proposed design
        - Connection to design requirements
        - Credible evidence of viability
        - Awareness of risks, constraints, or possible failure modes

        Legacy v1.2 scoring is somewhat more permissive:
        - Give partial credit for clear logical reasoning even when formal evidence is limited.
        - Give partial credit for qualitative stakeholder input, reuse of known components, or reasonable analogies.
        - Do not require formal test data for higher scores if the viability reasoning is specific and credible.
        - Only credit evidence that appears in the current student document.
        - Do not invent or assume evidence that is not explicitly present.

        Scoring guide:

        - 0: No viability discussion or irrelevant content.

        - 1: Viability is asserted but unsupported, or the response only describes the design without evaluating whether it will work.

        - 2: Viability is discussed but is largely speculative, vague, or unsupported by concrete evidence.

        - 3: A clear viability claim is made and at least one specific supporting element is present, but evidence is incomplete, weak, or only partially convincing.

        - 4: A realistic viability judgment supported by clear, specific, and credible evidence AND explicit connection to one or more design requirements.

        - 5: A clearly realistic and well-supported design, strongly tied to major requirements, with multiple pieces of credible evidence and clear consideration of constraints, risks, or tradeoffs.

        Caps:
        - Design-description-only with no viability judgment: cap at 1.
        - Only future testing or planned validation: cap at 2.
        - Placeholder or template responses: cap at 1.

        Do NOT assign scores of 3 or higher if:
        - The response only describes the design without evaluating viability
        - The response asserts that the design will work without explaining why
        - The response relies primarily on future testing rather than current evidence

        High scores (4–5) require explicit connection between the viability judgment and one or more design requirements.

        CRITICAL DISCRIMINATION REQUIREMENT:
        - Submissions with stronger, more specific, and more credible evidence of viability must receive higher scores than those with vague or unsupported claims.
        - Submissions that only assert that a design will work without justification must score lower than those that provide explicit reasoning or evidence.
        - Distinguish carefully between vague (2), partially supported (3), and well-supported (4–5) responses.

        Prompt variant emphasis:
        {variant_instruction}

        {json_format_instruction}
        """
        
    else:
        raise ValueError(f"Unsupported blended_model version: {blended_model}")


    if generate_narrative:
        prompt = prompt + "\n\n" + NARRATIVE_INSTRUCTION

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    # === Call the API ===
    
    gpt_model = get_gpt_model(blended_model)

    try:
        response = openai.ChatCompletion.create(
            model=gpt_model,
            messages=messages,
            temperature=0,
            top_p=1,
            max_completion_tokens=2500
        )
    except Exception:
        print("FAILED FILE:", filename)
        raise

    print("MODEL BEING USED:", gpt_model)

    # === Extract text from OpenAI response (legacy SDK) ===
    try:
        response_str = response.choices[0].message.content
    except Exception as e:
        print(f"❌ Could not extract message content for {filename}: {e}")
        return {}

    if response is None:
        print(f"❌ GPT call returned None for {filename}")
        return {}

   # === Continue with parsing logic ===
    try:
        # --- Normalize ---
        response_str = response_str.strip()

        # --- Remove ALL markdown code fences ---
        if response_str.startswith("```"):
            response_str = "\n".join(
                line for line in response_str.splitlines()
                if not line.strip().startswith("```")
            ).strip()

        # --- Remove anything before first { ---
        first_brace = response_str.find("{")
        if first_brace != -1:
            response_str = response_str[first_brace:]

        # --- Trim anything after last } ---
        last_brace = response_str.rfind("}")
        if last_brace != -1:
            response_str = response_str[: last_brace + 1]

        # --- Parse JSON ---
        cleaned = clean_json_string(response_str)

        try:
            response_dict = json5.loads(cleaned)

        except Exception as e:
            print(f"⚠️ First parse failed for {filename}: {e}")

            # Check for truncation
            if is_truncated_json(cleaned):
                print("⚠️ Detected truncated JSON. Retrying once...")

                retry_response = openai.ChatCompletion.create(
                    model=gpt_model,
                    messages=messages,
                    temperature=0,
                    top_p=1,
                    max_completion_tokens=2500
                )

                try:
                    retry_str = retry_response.choices[0].message.content.strip()

                    # Clean retry response
                    if retry_str.startswith("```"):
                        retry_str = "\n".join(
                            line for line in retry_str.splitlines()
                            if not line.strip().startswith("```")
                        ).strip()

                    first_brace = retry_str.find("{")
                    if first_brace != -1:
                        retry_str = retry_str[first_brace:]

                    last_brace = retry_str.rfind("}")
                    if last_brace != -1:
                        retry_str = retry_str[: last_brace + 1]

                    retry_str = clean_json_string(retry_str)

                    response_dict = json5.loads(retry_str)
                    print("✅ Retry succeeded")

                except Exception as retry_error:
                    print(f"❌ Retry failed for {filename}: {retry_error}")
                    return {
                        "truncation_detected": 1
                    }
            else:
                print("❌ Not a truncation case. Skipping document.")
                return {
                    "truncation_detected": 1
                }

        # --- Flatten nested structure ---
        flattened = {}
        for key, value in response_dict.items():
            if isinstance(value, dict) and "score" in value and "rationale" in value:
                flattened[key] = value["score"]
                flattened[f"{key}_rationale"] = value["rationale"]
            else:
                flattened[key] = value

        response_dict = flattened

        response_dict["truncation_detected"] = 0

        for i in range(1,2):
            response_dict[f"F{i}"] = int(response_dict.get(f"F{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        response_dict["F1"] = int(response_dict.get("F1", 0))
        response_dict["F1_api"] = int(response_dict["F1"])

        # Provide source text to rule engine, then remove it before export
        response_dict["_source_text"] = content

        response_dict = apply_element_f_rules(response_dict, blended_model)

        response_dict.pop("_source_text", None)

        # --- Validate expected fields (FAIL LOUDLY) ---
        assert "F1_api" in response_dict, "Missing F1_api"
        assert "F1_rule" in response_dict, "Missing F1_rule"
        assert "F1_rationale" in response_dict, "Missing F1_rationale"

        return response_dict

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse failed for {filename}")
        print(response_str)
        return {
            "truncation_detected": 1
        }

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]



def main(folder_path, output_path, blended_version):

    all_files = sorted(
        [
            f for f in os.listdir(folder_path)
            if f.lower().endswith((".pdf", ".docx"))
            and not f.startswith("~$")
        ],
        key=natural_sort_key
    )

    results = []

    for idx, filename in enumerate(all_files, start=1):
        full_path = os.path.join(folder_path, filename)
        print(f"Scoring {filename}...")
        try:
            text = extract_text_with_fallback(full_path)
            print("EXTRACTED LENGTH:", len(text))
            #print("EXTRACTED SAMPLE:", text[:300])
            
            if (
                blended_version == "v1.2"
                and SCORING_MODE == "ensemble_average"
            ):
                response_dict = score_document_ensemble(
                    filename,
                    text,
                    blended_version
                )

            elif (
                blended_version == "v1.2"
                and SCORING_MODE == "single_evidence"
            ):
                response_dict = score_document(
                    filename=filename,
                    content=text,
                    blended_model=blended_version,
                    prompt_variant="evidence_focused",
                    generate_narrative=(NARRATIVE_MODE == "single")
                )

                if NARRATIVE_MODE == "synthesized":
                    final_score = int(response_dict["F1_api"])
                    final_narrative = synthesize_narrative_feedback(
                        content=text,
                        final_score=final_score,
                        rationales=[response_dict.get("F1_rationale", "")],
                        blended_model=blended_version,
                    )
                    response_dict["narrative_feedback"] = final_narrative

            else:
                response_dict = score_document(
                    filename=filename,
                    content=text,
                    blended_model=blended_version
                )

            if response_dict is None:
                print(f"❌ score_document returned None for {filename}")
                continue

            # Start from full response_dict so nothing is lost
            row = response_dict.copy()

            row["truncation_detected"] = response_dict.get("truncation_detected", 0)

            # Add metadata fields
            row["Case"] = idx
            row["filename"] = filename
            row["text"] = text

            row["incomplete_response"] = any(
                k not in response_dict for k in [f"F{i}" for i in range(1, 2)]
            )

            # Ensure D scores are integers
            row["F1_api"] = int(row.get("F1_api", 0))
            row["F1_rule"] = int(row.get("F1_rule", row["F1_api"]))

            print(
                filename,
                [row[f"F{i}_api"] for i in range(1, 2)],
                row["element_score_api"]
            )

            results.append(row)

        except Exception:
            print(f"\nFULL TRACEBACK for {filename}:")
            traceback.print_exc()
            print("⚠️ Skipping this document and continuing...")
            continue    

    output_df = pd.DataFrame(results)
    # Reorder columns
    core = ["Case", "filename", "text", "incomplete_response"]

    scores = []
    api_scores = ["F1_api"]
    rule_scores = ["F1_rule"]
    flags = ["F1_flag"]
    rationales = ["F1_rationale"]

    extras = [
        "F1_api_mean",
        "F1_api_pass1",
        "F1_api_pass2",
        "F1_api_pass3",
        "truncation_detected",
        "element_score_api",
        "narrative_feedback"
    ]

    print("COLUMNS BEFORE REORDER:", output_df.columns.tolist())

    ordered_columns = core + api_scores + rule_scores + flags + rationales + extras

    ordered_columns = [c for c in ordered_columns if c in output_df.columns]
    output_df = output_df[ordered_columns]

    output_df.to_csv(output_path, index=False)
    
    print("\n✅ Scoring complete. Output saved to:", output_path)

def score_documents_with_api(documents, blended_version):
    results = []

    for idx, doc in enumerate(documents, start=1):
     
        filename = doc["filename"]
        file_path = doc["path"]

        # --- Extraction ---
        text = extract_text_with_fallback(file_path) 
        
        if (
            blended_version == "v1.2"
            and SCORING_MODE == "ensemble_average"
        ):
            response_dict = score_document_ensemble(
                filename,
                text,
                blended_version
            )

        elif (
            blended_version == "v1.2"
            and SCORING_MODE == "single_evidence"
        ):
            response_dict = score_document(
                filename=filename,
                content=text,
                blended_model=blended_version,
                prompt_variant="evidence_focused",
                generate_narrative=(NARRATIVE_MODE == "single")
            )

            if NARRATIVE_MODE == "synthesized":
                final_score = int(response_dict["F1_api"])
                final_narrative = synthesize_narrative_feedback(
                    content=text,
                    final_score=final_score,
                    rationales=[response_dict.get("F1_rationale", "")],
                    blended_model=blended_version,
                )
                response_dict["narrative_feedback"] = final_narrative

        else:
            response_dict = score_document(
                filename=filename,
                content=text,
                blended_model=blended_version
            )

        if response_dict is None:
            print(f"Skipping {filename} due to failure.")
            continue

        row = {
            "Case": idx,
            "filename": filename,
            "text": text,
        }
        
        if response_dict is None:
            raise ValueError(f"response_dict is None for {filename}")

        row["F1_api"] = int(response_dict.get("F1_api", 0))
        row["F1_rule"] = int(response_dict.get("F1_rule", row["F1_api"]))
        row["F1_flag"] = response_dict.get("F1_flag", "")
        row["F1_rationale"] = response_dict.get("F1_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API scores ---
        for i in range(1, 2):
            row[f"F{i}_api"] = int(
                response_dict.get(f"F{i}_api", response_dict.get(f"F{i}", 0))
            )

        # --- Attach element API and rule scores ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )
        row["element_score_rule"] = float(response_dict.get("element_score_rule", row["element_score_api"]))
        row["element_score_raw"] = row["element_score_api"]

        #("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"F{i}_api"] for i in range(1, 2)],
                row["element_score_api"]
            )

        results.append(row)

    df_debug = pd.DataFrame(results)
    return df_debug

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.62", "v1.2"], default="v1.62",
                        help="Which blended model logic to apply (default: v1.62)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
