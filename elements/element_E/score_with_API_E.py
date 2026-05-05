import json
import argparse
import pandas as pd
from pathlib import Path
import os


from scripts.shared.utils import (
    call_gpt_with_backoff,
    extract_text_with_fallback
)

# ============================================================
# Model Selection
# ============================================================
GPT_MODEL_LEGACY = "gpt-4o-mini"
GPT_MODEL_CURRENT = "gpt-4o-mini"


def get_gpt_model(blended_version: str) -> str:
    if blended_version in ("v1.2", "v1.2a"):
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.7r":
        return GPT_MODEL_CURRENT
    else:
        raise ValueError(f"Unknown blended model version: {blended_version}")


# ============================================================
# Prompt Builder
# ============================================================
def build_prompt(content: str, blended_version: str) -> str:
    mode = "legacy" if blended_version in ("v1.2", "v1.2a") else "current"

    legacy_block = ""

    if mode == "legacy":
        legacy_block = """
CRITICAL LEGACY SCORING REQUIREMENT:

Your task is to evaluate how well the student demonstrates each criterion using the evidence provided.

You should:
- Use explicit evidence from the document whenever possible
- Avoid making unsupported assumptions
- Recognize both strong and weak evidence when it is clearly present

IMPORTANT BALANCE:

- Do NOT assume evidence that is not reasonably supported
- BUT also do NOT ignore clear evidence when it is present, even if imperfectly written
- Student work may express ideas imperfectly—interpret reasonable evidence fairly

SCORING INTENT:

- Scores should reflect the strength of evidence, not just its presence or absence
- If strong ideas are present but not perfectly explained, scores of 3–4 may still be appropriate
- Reserve scores of 0–1 for truly missing or minimal evidence

Evidence rules:
- If evidence is clearly missing or unsupported, score 0 or 1.
- If evidence is present but weak, unclear, or partially developed, use 2 or 3.
- If evidence is specific, detailed, clearly explained, and directly connected to design decisions, score 4 or 5.

High-score guardrails:
- Assign 4 or 5 when the document provides clear and relevant evidence, even if not perfectly explained.
- Do NOT assign 4 or 5 for general STEM vocabulary alone.
- Do NOT assign 4 or 5 for expert review unless the document clearly shows who reviewed it, why they were qualified, and what feedback or verification resulted.
- Well-supported ideas that are not perfectly explained may still justify scores of 3–4.

Low-score guardrails:
- If no clear evidence supports a criterion, score 0 or 1.
- If evidence is only a brief claim with no detail, score 1.
- If the document only says an expert was consulted but gives no credentials or useful feedback, E3–E5 should remain low.
- If testing, verification, or review results are absent, E5 should be 0 or 1.

Criterion-specific cautions:
- E1 requires STEM principles connected to design requirements.
- E2 requires STEM evidence used to justify the solution, not just STEM terms.
- E3 requires clear evidence that expert review occurred.
- E4 requires expert credentials and/or multiple credible reviewers.
- E5 requires specific review results, feedback, testing, or verification.

Before assigning each score, ask:
“What explicit evidence in the document supports this score?”

Use professional judgment to interpret student work fairly. Do not default to extreme scores when moderate evidence exists.
"""

    return f"""

SCORING PRINCIPLES:
- Base scores strictly on explicit evidence
- Do not assume missing elements
- Use full 0–5 scale when justified
- Do not reward vague STEM references or unsupported expert claims
- Do not reward expert review unless supported by clear evidence and credentials

Rubric:

E1. STEM principles applied to design requirements
E2. STEM substantiation of solution
E3. Evidence of expert review
E4. Expert credentials and count
E5. Review results and verification

Student Document:
\"\"\"{content}\"\"\"

IMPORTANT SCORING GUIDANCE:

- Score each criterion independently
- Use explicit evidence from the document, not assumptions
- Do not award high scores merely because STEM terms appear
- STEM must be clearly applied to design decisions
- Do not award expert-review credit based only on vague claims
- For scores of 4–5, evidence must be clear and well developed

HIGH SCORE CRITERIA:

- Scores of 4–5 require explicit, specific, and well-explained evidence
- General or vague references to STEM concepts should not exceed a score of 2–3
- Mention of expert review without clear credentials or documented feedback should not exceed a score of 2
- Strong scores require clear linkage between evidence and design decisions

NARRATIVE REQUIREMENTS:

- Length: 170–220 words
- Structure: 2–3 paragraphs
- Audience: student-facing, professional, constructive tone
- Must explain BOTH strengths and weaknesses
- Must reference criteria where helpful (e.g., E2, E4)
- Must include 2–4 specific, actionable recommendations

CONTENT GUIDELINES:

- Do not repeat rubric language verbatim
- Do not simply restate scores
- Focus on:
  - quality of STEM justification (E2)
  - strength of expert review evidence (E3–E4)
  - usefulness of verification (E5)
- Highlight missing or weak evidence
- Avoid generic phrases like "good job" or "needs improvement"

STYLE CONSTRAINTS:

- No bullet points
- No headings
- No mention of scoring process or AI
- Keep sentences clear and readable

{legacy_block}

Return ONLY valid JSON:

{{
  "E1": {{"score": X, "rationale": "..."}},
  "E2": {{"score": X, "rationale": "..."}},
  "E3": {{"score": X, "rationale": "..."}},
  "E4": {{"score": X, "rationale": "..."}},
  "E5": {{"score": X, "rationale": "..."}},
  "narrative_feedback": "170–220 word narrative written in 2–3 paragraphs"
}}
"""

# ============================================================
# Single Document Scoring
# ============================================================
def score_document(filename, content, blended_version):

    prompt = build_prompt(content, blended_version)

    print("PROMPT LENGTH:", len(prompt))

    response = call_gpt_with_backoff(
        prompt=prompt,
        system="You are a rigorous engineering design evaluator applying the Element E rubric.",
        model_order=[get_gpt_model(blended_version)]
    )

    try:
        response_dict = json.loads(response)
    except Exception:
        response_dict = {}

    row = {
        "filename": filename,
        "text": content
    }

    for i in range(1, 6):
        key = f"E{i}"
        score = response_dict.get(key, {}).get("score", 0)

        try:
            score = int(score)
        except Exception:
            score = 0

        row[key] = score
        row[f"{key}_api"] = score

    row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

    return row


# ============================================================
# Batch Scoring
# ============================================================
def score_documents_with_api(documents, blended_version: str):

    
    rows = []

    for idx, doc in enumerate(documents, start=1):
        filename = doc["filename"]
        path = doc["path"]

        text = extract_text_with_fallback(path)

        row = score_document(filename, text, blended_version)
        row["Case"] = idx

        rows.append(row)

    df = pd.DataFrame(rows)

    # --- Define column groups ---
    id_cols = ["Case", "filename"]  

    base_cols = [f"E{i}" for i in range(1, 6)]
    api_cols = [f"E{i}_api" for i in range(1, 6)]
    flag_cols = [f"E{i}_flag" for i in range(1, 6)]
    rat_cols = [f"E{i}_rationale" for i in range(1, 6)]

    other_cols = ["narrative_feedback"]

    # --- Keep only columns that actually exist ---
    ordered_cols = [
        *[c for c in id_cols if c in df.columns],
        *[c for c in base_cols if c in df.columns],
        *[c for c in api_cols if c in df.columns],
        *[c for c in flag_cols if c in df.columns],
        *[c for c in rat_cols if c in df.columns],
        *[c for c in other_cols if c in df.columns],
    ]

    df = df[ordered_cols]

    return df


# ============================================================
# CLI
# ============================================================
def run_cli(folder, output, blended_version):

    folder_path = Path(folder)

    if not folder_path.exists():
        raise ValueError(f"Folder does not exist: {folder}")

    files = [
        f for f in folder_path.glob("*.*")
        if not f.name.startswith("~$")
        and f.suffix.lower() in [".docx", ".pdf", ".txt"]
    ]

    documents = [
        {"filename": f.name, "path": str(f)}
        for f in files
    ]

    df = score_documents_with_api(
        documents,
        blended_version=blended_version
    )

    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(output_path, index=False)

    print(f"\nScoring complete: {len(df)} documents")
    print(f"Saved to: {output_path}")

# ============================================================
# Entry Point
# ============================================================
if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--folder", required=True, help="Input folder of documents")
    parser.add_argument("--output", required=True, help="Output CSV file")
    parser.add_argument("--blended-version", required=True, help="Blended model version (e.g., v1.7r)")

    args = parser.parse_args()

    run_cli(
        folder=args.folder,
        output=args.output,
        blended_version=args.blended_version
    )