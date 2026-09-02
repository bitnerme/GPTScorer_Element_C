import pandas as pd

print("Script started")

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

INPUT_FILE = "Current_GPT_2025+2026.csv"
OUTPUT_FILE = "golden20_candidates.csv"

ELEMENT = "B"

SUBELEMENT_COUNTS = {
    "A": 6,
    "B": 2,
    "C": 6,
    "D": 4,
    "E": 4,
    "F": 1,
    "G": 4,
    "H": 5,
    "I": 4,
    "J": 3,
    "K": 3,
    "L": 2,
}

YEAR_COLUMN = "SubmissionYear"

TARGET_YEARS = [2025, 2026]

BINS = [
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 4),
    (4, 5.000001),   # include exact 5.0
]

CASES_PER_BIN_PER_YEAR = 2


# ---------------------------------------------------------
# LOAD DATA
# ---------------------------------------------------------

try:
    df = pd.read_csv(INPUT_FILE, encoding="utf-8-sig")
    print("Loaded as UTF-8")
except UnicodeDecodeError:
    print("UTF-8 failed; retrying as Windows-1252")
    df = pd.read_csv(INPUT_FILE, encoding="cp1252")
    print("Loaded as Windows-1252")

print("Rows loaded:", len(df))
print("Columns:", df.columns.tolist())


# ---------------------------------------------------------
# VALIDATE
# ---------------------------------------------------------

if ELEMENT not in SUBELEMENT_COUNTS:
    raise ValueError(f"Unknown element: {ELEMENT}")

if YEAR_COLUMN not in df.columns:
    raise ValueError(
        f"Required column '{YEAR_COLUMN}' not found."
    )

subelement_count = SUBELEMENT_COUNTS[ELEMENT]

expert_cols = [
    f"{ELEMENT}{i}__expert"
    for i in range(1, subelement_count + 1)
]

missing_cols = [
    c for c in expert_cols
    if c not in df.columns
]

if missing_cols:
    raise ValueError(
        f"Missing expert columns: {missing_cols}"
    )


# ---------------------------------------------------------
# NORMALIZE YEAR
# ---------------------------------------------------------

df[YEAR_COLUMN] = pd.to_numeric(
    df[YEAR_COLUMN],
    errors="coerce"
)

df = df[df[YEAR_COLUMN].isin(TARGET_YEARS)].copy()


# ---------------------------------------------------------
# COMPUTE EXPERT ELEMENT SCORE
# ---------------------------------------------------------

df["expert_score"] = df[expert_cols].mean(axis=1)


# ---------------------------------------------------------
# CLASSIFY SCORE TYPE
# ---------------------------------------------------------

# For Element B, expert averages are typically x.0 or x.5.
# This identifies whether the score is integer or half-point.

df["fractional_part"] = (
    df["expert_score"] - df["expert_score"].astype(int)
).round(3)

df["score_type"] = df["fractional_part"].apply(
    lambda x: "integer"
    if abs(x - 0.0) < 0.001
    else "half"
    if abs(x - 0.5) < 0.001
    else "other"
)

# Useful secondary criterion:
# distance from center of score band
df["band_center_distance"] = 0.0


# ---------------------------------------------------------
# SELECT GOLDEN20
# ---------------------------------------------------------

golden_parts = []

for year in TARGET_YEARS:

    year_df = df[df[YEAR_COLUMN] == year].copy()

    print(f"\nSelecting candidates for {year}")
    print("Eligible rows:", len(year_df))

    for low, high in BINS:

        subset = year_df[
            (year_df["expert_score"] >= low) &
            (year_df["expert_score"] < high)
        ].copy()

        if subset.empty:
            print(
                f"  WARNING: {year} band {low}-{high} has no candidates."
            )
            continue

        # Center of the band, e.g. 2.5 for 2-3
        band_center = (low + min(high, 5)) / 2

        subset["band_center_distance"] = abs(
            subset["expert_score"] - band_center
        )

        integer_cases = subset[
            subset["score_type"] == "integer"
        ].copy()

        half_cases = subset[
            subset["score_type"] == "half"
        ].copy()

        selected_rows = []

        # -------------------------------------------------
        # Prefer one integer case
        # -------------------------------------------------

        if not integer_cases.empty:
            integer_cases = integer_cases.sort_values(
                ["band_center_distance", "expert_score"]
            )
            selected_rows.append(integer_cases.iloc[[0]])

        # -------------------------------------------------
        # Prefer one half-point case
        # -------------------------------------------------

        if not half_cases.empty:
            half_cases = half_cases.sort_values(
                ["band_center_distance", "expert_score"]
            )
            selected_rows.append(half_cases.iloc[[0]])

        # -------------------------------------------------
        # Fallback if one type is unavailable
        # -------------------------------------------------

        if len(selected_rows) < CASES_PER_BIN_PER_YEAR:

            already_selected_indices = set()

            for part in selected_rows:
                already_selected_indices.update(part.index.tolist())

            remaining = subset[
                ~subset.index.isin(already_selected_indices)
            ].copy()

            remaining = remaining.sort_values(
                ["band_center_distance", "expert_score"]
            )

            needed = (
                CASES_PER_BIN_PER_YEAR
                - len(selected_rows)
            )

            if needed > 0 and not remaining.empty:
                selected_rows.append(
                    remaining.head(needed)
                )

        if not selected_rows:
            continue

        selected = pd.concat(selected_rows).copy()

        selected["selection_year"] = year
        selected["selection_band"] = (
            f"{low}-{min(high, 5)}"
        )

        golden_parts.append(selected)

        print(
            f"  {year} band {low}-{min(high,5)}:"
        )

        for _, r in selected.iterrows():
            fname = r.get("filename", "UNKNOWN")
            print(
                f"    {fname} | "
                f"expert={r['expert_score']:.2f} | "
                f"type={r['score_type']}"
            )


# ---------------------------------------------------------
# COMBINE
# ---------------------------------------------------------

if not golden_parts:
    raise ValueError(
        "No Golden20 candidates were selected."
    )

golden = pd.concat(
    golden_parts,
    ignore_index=True
)


# ---------------------------------------------------------
# REPORT
# ---------------------------------------------------------

print("\nSelected count by year:")
print(
    golden[YEAR_COLUMN]
    .value_counts()
    .sort_index()
)

print("\nSelected count by year and score band:")
print(
    golden.groupby(
        [YEAR_COLUMN, "selection_band"]
    ).size()
)

print("\nSelected count by score type:")
print(
    golden["score_type"]
    .value_counts()
)

print("\nTotal selected:", len(golden))


# ---------------------------------------------------------
# SAVE
# ---------------------------------------------------------

golden.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8-sig"
)

print(f"\nWriting to {OUTPUT_FILE}")

display_cols = [
    YEAR_COLUMN,
    "expert_score",
    "score_type",
    "selection_band",
]

if "filename" in golden.columns:
    display_cols.insert(1, "filename")

print("\nGolden20 Candidates:\n")

print(
    golden[display_cols]
    .sort_values(
        [YEAR_COLUMN, "expert_score"]
    )
    .to_string(index=False)
)