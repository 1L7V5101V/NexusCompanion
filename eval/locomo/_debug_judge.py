"""Debug: check what the LLM judge actually outputs for a few questions."""
import asyncio, json
from openai import AsyncOpenAI

config = {
    "model": "deepseek-v4-flash",
    "base_url": "https://opencode.ai/zen/go/v1",
    "api_key": "sk-T52Sl1TlvzwZTpOk6NoEdT1OiAjOHlEqvOgKWntoO529lhUevTaJ7nnR1RNLALV1",
}
client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"])

PROMPT = """You are evaluating a memory agent's answer.

Question: {question}
Gold answer: {gold}
Predicted answer: {predicted}

Does the predicted answer CONTAIN THE SAME FACT as the gold answer?
- If gold is a date ("7 May 2023") and predicted says "May 7, 2023" -> yes
- If gold is a name list ("Oliver, Luna, Bailey") and predicted says "a dog named Luna and two cats named Oliver and Bailey" -> yes (same pets)
- If gold is "single" and predicted says "single parent" -> yes (same status)
- Ignore extra explanation, full sentences, Chinese text, or citations. Only check whether the core fact matches.
- Only answer no if the predicted answer clearly contradicts or misses the gold fact.

Reply with exactly one word: yes or no."""

async def test():
    results_dir = r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results"
    # Test a few diverse cases
    test_indices = [0, 82, 121, 152, 167, 178]
    for idx in test_indices:
        path = f"{results_dir}/{idx:04d}.json"
        data = json.loads(open(path, encoding="utf-8").read())
        prompt = PROMPT.format(
            question=data["question"].strip(),
            gold=data["gold_answer"].strip(),
            predicted=data["predicted_answer"].strip(),
        )
        resp = await client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=8,
            temperature=0.0,
        )
        content = (resp.choices[0].message.content or "").strip()
        print(f"[{idx:03d}] verdict={repr(content)}  | gold={repr(data['gold_answer'][:50])}  | pred={repr(data['predicted_answer'][:80])}")

asyncio.run(test())
