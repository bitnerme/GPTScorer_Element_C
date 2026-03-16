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
    """
    Detect number of subelements by scanning dataframe columns.

    Example:
        A1..A6 → 6
        C1..C6 → 6
        D1..D4 → 4
    """

    raw_cols = [
        c for c in df.columns
        if c.startswith(element) and "_" not in c
    ]

    return len(raw_cols)


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