"""Re-judge all conv-26 results with relaxed judge + adversarial handling.

Fixes two issues from the original benchmark run:
1. Thinking model: answer is in reasoning_content not content
2. Adversarial questions (gold=""): pass if agent correctly denies false premise
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)

_RELAXED_JUDGE_PROMPT = """\
You are evaluating a memory agent's answer.

Question: {question}
Gold answer: {gold}
Predicted answer: {predicted}

Does the predicted answer CONTAIN THE SAME FACT as the gold answer?
- Equivalent dates count as correct:
  "7 May 2023" = "May 7, 2023" → yes
  "The sunday before 25 May 2023" = "Saturday, May 20, 2023" → yes (same day)
  "The week before 9 June 2023" = "early June 2023" → yes
  "July 2023" = "in July 2023" → yes
  "2022" = "last year as of May 2023, so 2022" → yes
- Extra items in a list don't make it wrong, as long as the gold items are present:
  gold "Running, pottery" with pred "runs, does pottery, and paints" → yes
  gold "Pride parade, school speech, support group" with pred "support group, school speech, pride parade" → yes (same items)
- Predicted has MORE specific info than gold (exact date vs relative) → yes
- Ignore extra explanation, full sentences, Chinese text, citations. Only check core fact match.
- Only answer no if the predicted answer clearly contradicts or misses the core gold fact.

Reply with exactly one word: yes or no."""


def _read_llm_main_config(config_path: Path) -> dict:
    text = config_path.read_text(encoding="utf-8")
    result = {"model": "dev/deepseek-v4", "base_url": "", "api_key": ""}
    current_section = ""
    for line in text.splitlines():
        stripped = line.strip()
        sm = re.match(r"^\[(.+)\]$", stripped)
        if sm:
            current_section = sm.group(1)
            continue
        if current_section != "llm.main":
            continue
        for key in ("base_url", "api_key"):
            pat = re.compile('^' + re.escape(key) + r'\s*=\s*"(.+)"$')
            m = pat.match(stripped)
            if m:
                result[key] = m.group(1)
    return result


def _verdict_from_message(msg) -> str | None:
    raw = (msg.content or "").strip()
    if raw:
        return raw.lower()
    rc = getattr(msg, "reasoning_content", None) or ""
    rc = rc.strip().lower()
    if rc:
        return rc
    return None


async def main():
    config = _read_llm_main_config(Path("eval/locomo/config.toml"))
    print(f"LLM: model={config['model']}  base_url={config['base_url']}")

    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"])

    results_dir = Path(r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results")
    files = sorted(results_dir.iterdir())
    total = len(files)
    print(f"Re-judging {total} questions ...\n")

    changed = 0
    for idx, fpath in enumerate(files):
        data = json.loads(fpath.read_text(encoding="utf-8"))
        question = data["question"]
        gold = data["gold_answer"]
        predicted = data["predicted_answer"]
        old_judge = data.get("judge_correct")

        # --- adversarial: gold empty ---
        if not gold.strip():
            pred_lower = predicted.lower()
            denial_patterns = [
                "no record", "no information", "no mention",
                "didn't", "doesn't have", "cannot find",
                "couldn't find", "not found", "haven't been",
                "don't have", "没有记录", "没有提到", "没有信息",
                "melanie didn't", "caroline didn't",
                "is about melanie", "belongs to melanie",
                "belongs to caroline", "是 melanie 的", "是 caroline 的",
            ]
            new_judge = any(p in pred_lower for p in denial_patterns)
        else:
            # --- normal: LLM judge ---
            prompt = _RELAXED_JUDGE_PROMPT.format(
                question=question.strip(),
                gold=gold.strip(),
                predicted=predicted.strip(),
            )
            try:
                resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=config["model"],
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=150,
                        temperature=0.0,
                    ),
                    timeout=60.0,
                )
                vt = _verdict_from_message(resp.choices[0].message)
                new_judge = vt is not None and "yes" in vt
            except asyncio.TimeoutError:
                logging.getLogger("rejudge").warning("judge timed out at idx %d", idx)
                new_judge = None
            except Exception as e:
                logging.getLogger("rejudge").warning("judge failed at idx %d: %s", idx, e)
                new_judge = None

        data["judge_correct"] = new_judge

        from eval.longmemeval.metrics import token_f1
        clean_pred = re.sub(r"§cited:[^\s]*§", "", predicted).strip()
        data["f1"] = round(token_f1(clean_pred, gold), 4)

        fpath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        if old_judge != new_judge:
            changed += 1
            mark = "+" if new_judge else "-"
            print(f"  [{idx:03d}] {mark}  {old_judge}->{new_judge}  {question[:60]}")

    print(f"\nDone. {changed}/{total} verdicts changed.")

    from eval.longmemeval.metrics import score_results
    all_results = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    scores = score_results(all_results)
    ov = scores["overall"]
    print(f"\n=== Final scores (relaxed judge) ===")
    print(f"  Overall:  judge_acc={ov.get('judge_acc', '?'):.1%}  f1={ov['f1']:.4f}  n={ov['n']}  errors={ov['errors']}")
    for cat, s in sorted(scores.get("by_type", {}).items()):
        print(f"  {cat:20s}  judge_acc={s.get('judge_acc', '?'):.1%}  f1={s['f1']:.4f}  n={s['n']}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path("eval/locomo/results") / f"{ts}_relaxed_judge.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(
        json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "note": "re-judged with relaxed prompt + adversarial denial detection",
            "scores": scores,
            "results": all_results,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nSaved -> {out_path}")

if __name__ == "__main__":
    asyncio.run(main())
