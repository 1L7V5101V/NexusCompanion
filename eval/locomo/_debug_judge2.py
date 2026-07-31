"""Debug: inspect full LLM response to see where the verdict lives."""
import asyncio, json
from openai import AsyncOpenAI

config = {
    "model": "deepseek-v4-flash",
    "base_url": "https://opencode.ai/zen/go/v1",
    "api_key": "sk-T52Sl1TlvzwZTpOk6NoEdT1OiAjOHlEqvOgKWntoO529lhUevTaJ7nnR1RNLALV1",
}
client = AsyncOpenAI(base_url=config["base_url"], api_key=config["api_key"])

async def test():
    data = json.loads(open(r"C:\Users\HP\AppData\Local\Temp\locomo_bench\conv-26\qa_results\0082.json", encoding="utf-8").read())
    prompt = f"""You are a judge. Question: {data['question']} Gold: {data['gold_answer']} Predicted: {data['predicted_answer']}
Reply with exactly one word: yes or no."""
    
    resp = await client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8,
        temperature=0.0,
    )
    # Inspect the full response
    print("=== Full response type ===", type(resp))
    print("=== choices[0] dir ===", [a for a in dir(resp.choices[0]) if not a.startswith("_")])
    msg = resp.choices[0].message
    print("=== message dir ===", [a for a in dir(msg) if not a.startswith("_")])
    print("=== content ===", repr(msg.content))
    # Check for delta or other fields
    print("=== model ===", resp.model)
    print("=== object ===", resp.object)
    
    # Try to_dict
    try:
        print("=== full dump ===", resp.model_dump_json(indent=2)[:2000])
    except:
        print(json.dumps({"id": resp.id, "object": resp.object, "model": resp.model, "choices": [{"finish_reason": c.finish_reason, "index": c.index} for c in resp.choices]}, indent=2))

asyncio.run(test())
