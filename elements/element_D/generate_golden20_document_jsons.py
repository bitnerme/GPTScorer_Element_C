import pandas as pd
import json

df = pd.read_csv("golden20_candidates.csv")

# Remove blank / invalid filename rows first
df = df[df["filename"].notna()].copy()
df["filename"] = df["filename"].astype(str).str.strip()
df = df[df["filename"] != ""]

print(f"Generating JSON for {len(df)} Golden20 cases")

data = [
    {
        "filename": row["filename"],
        "expert_score": row["expert_score"]
    }
    for _, row in df.iterrows()
]

with open("golden_D_current.json", "w") as f:
    json.dump(data, f, indent=2)