import pandas as pd
import json

df = pd.read_csv("golden20_candidates.csv")

data = [
    {
        "filename": row["filename"],
        "expert_score": row["expert_score"]
    }
    for _, row in df.iterrows()
]

with open("golden_A_legacy.json", "w") as f:
    json.dump(data, f, indent=2)