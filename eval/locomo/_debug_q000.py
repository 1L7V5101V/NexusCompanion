"""Debug Q000: check what judge outputs for a clearly-right case."""
import asyncio, json
from openai import AsyncOpenAI

c = AsyncOpenAI(
    base_url="https://opencode.ai/zen/go/v1",
    api_key="sk-T52Sl1TlvzwZTpOk6NoEdT1OiAjOHlEqvOgKWntoO529lhUevTaJ7nnR1RNLALV1",
)

PROMPT = """You are evaluating a memory agent's answer.

Question: {question}
Gold answer: {gold}
Predicted answer: {predicted}

Does the predicted answer CONTAIN THE SAME FACT as the gold answer?
- If gold is a date ("7 May 2023") and predicted says "May 7, 2023" -> yes
- If gold is a name list ("Oliver, Luna, Bailey") and predicted says "a dog named Luna and two cats" -> yes
- Ignore extra explanation, full sentences, Chinese text, citations. Only check core fact match.
- Only answer no if the predicted answer clearly contradicts or misses the gold fact.

Reply with exactly one word: yes or no."""

async def test():
    data = json.loads(open(r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results\0000.json", encoding="utf-8").read())
    
    prompt = PROMPT.format(
        question=data["question"].strip(),
        gold=data["gold_answer"].strip(),
        predicted="Caroline attended the LGBTQ support group on May 7, 2023.",
    )
    
    for maxtok in [50, 200, 500]:
        resp = await c.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=maxtok,
            temperature=0.0,
        )
        msg = resp.choices[0].message
        print(f"\n=== max_tokens={maxtok} finish={resp.choices[0].finish_reason} ===")
        print("content:", repr(msg.content))
        rc = getattr(msg, "reasoning_content", None)
        if rc:
            print("reasoning:", repr(rc[:500]))
    
    print("\n\n=== Full reasoning (max_tokens=500) ===")
    resp = await c.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=500,
        temperature=0.0,
    )
    msg = resp.choices[0].message
    print(getattr(msg, "reasoning_content", None) or msg.content)

asyncio.run(test())
