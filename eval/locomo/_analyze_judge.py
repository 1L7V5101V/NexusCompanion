"""Analyze false negatives from rejudge results."""
from __future__ import annotations
import json
import re
from pathlib import Path

results_path = Path("eval/locomo/results/20260720_114401_relaxed_judge.json")
data = json.loads(results_path.read_text(encoding="utf-8"))

false_negs = [r for r in data["results"] if not r.get("judge_correct") and r.get("gold_answer", "").strip()]
false_pos = [r for r in data["results"] if r.get("judge_correct") and not r.get("gold_answer", "").strip()]

print(f"Total: {len(data['results'])}")
print(f"Correct: {sum(1 for r in data['results'] if r.get('judge_correct'))}")
print(f"False negatives: {len(false_negs)}")
print(f"Adversarial incorrectly denied: {len(false_pos)}")
print()

# Categories of false negatives
date_keywords = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday","week before","month", "2022", "2023", "ago"]
print("=== False negatives with potential DATE equivalence ===")
count = 0
for r in false_negs:
    g = r["gold_answer"].strip()
    p = re.sub(r"§cited:[^\s]*§", "", r["predicted_answer"]).strip()
    if any(k in g.lower() for k in date_keywords):
        count += 1
        if count <= 25:
            print(f"  Q: {r['question'][:70]}")
            print(f"    Gold:  {g[:120]}")
            print(f"    Pred:  {p[:120]}")
            print()

print(f"Total date-equivalence candidates: {count} / {len(false_negs)}")
print()

# Non-date false negatives
print("=== Non-date false negatives (first 20) ===")
nondate_count = 0
for r in false_negs:
    g = r["gold_answer"].strip()
    if not any(k in g.lower() for k in date_keywords):
        nondate_count += 1
        if nondate_count <= 20:
            p = re.sub(r"§cited:[^\s]*§", "", r["predicted_answer"]).strip()
            print(f"  Q: {r['question'][:70]}")
            print(f"    Gold:  {g[:150]}")
            print(f"    Pred:  {p[:150]}")
            print()

print(f"Total non-date candidates: {nondate_count} / {len(false_negs)}")
