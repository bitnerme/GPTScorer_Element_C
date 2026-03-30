from pathlib import Path


def get_element_from_file(file_path: str) -> str:
    """
    Extract element letter from scorer file path.

    Example:
        elements/element_C/scorer_app_C.py → "C"
    """
    p = Path(file_path)

    for part in p.parts:
        if part.startswith("element_"):
            return part.split("_")[1]

    raise ValueError(f"Could not determine element from path: {file_path}")


def detect_subelement_count(df, element: str) -> int:
    element_clean = (element or "").strip().upper()

    return {
        "A": 6,
        "B": 2,
        "C": 6,
        "D": 4,
    }.get(element_clean, 4)

def build_score_cols(element: str, count: int):

    raw_cols = [f"{element}{i}" for i in range(1, count + 1)]
    final_cols = [f"{element}{i}_final" for i in range(1, count + 1)]

    return (
        ["filename"]
        + raw_cols
        + final_cols
        + [
            "element_score_raw",
            "element_score_calibrated",
            "calibration_delta",
            "flags",
            "rationales",
            "narrative_feedback",
        ]
    )