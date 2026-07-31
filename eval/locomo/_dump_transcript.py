"""Dump all Q&A results from conv-26 as readable transcript."""
import json, os, sys

results_dir = r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results"
files = sorted(os.listdir(results_dir))
out = sys.stdout

by_cat = {}
for f in files:
    path = os.path.join(results_dir, f)
    with open(path, encoding="utf-8") as fh:
        r = json.load(fh)
    by_cat.setdefault(r["category_name"], []).append(r)

for cat in ["single_hop", "temporal", "multi_hop", "open_domain", "adversarial"]:
    items = by_cat.get(cat, [])
    print(file=out)
    print("=" * 70, file=out)
    print(f"  {cat.upper()}  ({len(items)} questions)", file=out)
    print("=" * 70, file=out)
    for r in items:
        q = r["question"][:100]
        p = r["predicted_answer"].replace("\n", " | ")[:150]
        g = r["gold_answer"][:100] if r["gold_answer"] else "(none - adversarial)"
        j = "PASS" if r.get("judge_correct") else ("FAIL" if r.get("judge_correct") is False else "?   ")
        f1 = r.get("f1", 0)
        print(file=out)
        print(f"  [{r['qa_index']:03d}] {j}  f1={f1:.2f}  [{r['elapsed_s']:.0f}s]  {r['category_name']}", file=out)
        print(f"  Q: {q}", file=out)
        print(f"  A: {p}", file=out)
        print(f"  G: {g}", file=out)
    print(file=out)
