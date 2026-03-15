"""
Akasic Benchmark on LongMemEval Dataset

使用 Akasic memory2（SQLite + 向量搜索）对 LongMemEval 数据集进行基准测试。
与 LoCoMo 的主要区别：
  - 对话格式为 user-assistant（贴近生产环境）
  - 每个 question 自带独立的 haystack_sessions
  - ingest 走 LMEExtractor（提取 event / profile / preference）
  - retrieve 覆盖三类：event + profile + preference

下载数据：
  pip install huggingface_hub
  huggingface-cli download xiaowu0162/longmemeval --repo-type dataset --local-dir benchmark/data/longmemeval

用法（从 benchmark/ 目录运行）：
  cd benchmark/
  python run_longmemeval.py [options]

选项：
  --config PATH           config.json 路径（默认: ../config.json）
  --db-path PATH          benchmark 专用 DB 路径（默认: /tmp/akasic_benchmark/lme.db）
  --data PATH             longmemeval JSON 路径（默认: data/longmemeval/longmemeval_s.json）
  --max-samples N         最多处理 N 个 question（默认: 全部）
  --question-type TYPE    只测指定 question_type（逗号分隔，默认: 全部）
  --max-workers N         并发线程数（默认: 2）
  --skip-ingest           跳过 ingest（使用已有 DB）
  --use-flash             response/evaluate 走 light model（更快）
  --output PATH           结果 JSON 路径（默认: result_lme.json）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import dotenv

dotenv.load_dotenv()

_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config_loader import BenchmarkComponents, load_benchmark_components
from evaluate_agent import EvaluateAgent
from lme_ingestor import LMEProductionIngestor, IngestFact
from memory2.hyde_enhancer import _union_dedup
from memory2.store import MemoryStore2
from memu.utils import setup_logging

logger = setup_logging(__name__, enable_flush=True)

LME_SCOPE_CHANNEL = "lme_benchmark"
DEFAULT_LME_DB = Path("/tmp/akasic_benchmark/lme.db")

QUESTION_TYPE_NAMES = {
    "single-session-user": "SS-User",
    "single-session-assistant": "SS-Asst",
    "single-session-preference": "SS-Pref",
    "multi-session": "Multi",
    "knowledge-update": "KnowUpd",
    "temporal-reasoning": "Temporal",
    "abstention": "Abstain",
}


# ─── Ingest ───────────────────────────────────────────────────────────

class LMEMemAgent:
    def __init__(self, components: BenchmarkComponents) -> None:
        self._store = components.store
        self._embedder = components.embedder
        self._ingestor = LMEProductionIngestor(
            light_client=components.light_llm_client,
            light_model=components.light_model,
        )

    def ingest_haystack(
        self,
        question_id: str,
        haystack_sessions: list[list[dict]],
        haystack_dates: list[str],
        concurrency: int = 12,
    ) -> int:
        """
        摄取一个 question 的所有 haystack sessions，返回写入 fact 数。
        所有 session 并发提取（3 条 pipeline × concurrency 组），embed+upsert 串行。
        """
        n_sessions = len(haystack_sessions)
        logger.info("  extracting %d sessions (concurrency=%d)...", n_sessions, concurrency)

        # 并发提取：所有 session 同时跑三条 pipeline
        all_session_facts = self._ingestor.extract_all_sessions_sync(
            sessions=haystack_sessions,
            dates=haystack_dates,
            concurrency=concurrency,
        )

        # 统计
        by_source: dict[str, int] = {}
        total_extracted = 0
        for facts in all_session_facts:
            for f in facts:
                by_source[f.source] = by_source.get(f.source, 0) + 1
                total_extracted += 1
        logger.info("  extracted %d facts total %s", total_extracted, by_source)

        # 展平所有 facts，批量 embed，再串行 upsert（避免 SQLite 并发写）
        flat_facts: list[IngestFact] = []
        flat_refs: list[str] = []
        for sess_idx, facts in enumerate(all_session_facts):
            for fact_idx, fact in enumerate(facts):
                flat_facts.append(fact)
                flat_refs.append(f"lme:{question_id}:{sess_idx}:{fact_idx}")

        logger.info("  embedding %d facts (batch)...", len(flat_facts))
        try:
            embeddings = self._embedder._embed_batch_sync([f.summary for f in flat_facts])
        except Exception as exc:
            logger.warning("batch embed failed qid=%s: %s", question_id, exc)
            return 0

        total_saved = 0
        for fact, source_ref, embedding in zip(flat_facts, flat_refs, embeddings):
            if not embedding:
                continue
            try:
                self._store.upsert_item(
                    memory_type=fact.memory_type,
                    summary=fact.summary,
                    embedding=embedding,
                    source_ref=source_ref,
                    extra={
                        "scope_channel": LME_SCOPE_CHANNEL,
                        "scope_chat_id": question_id,
                        "lme_source": fact.source,
                    },
                    happened_at=fact.happened_at,
                )
                total_saved += 1
            except Exception as exc:
                logger.warning("upsert_item failed qid=%s: %s", question_id, exc)
        return total_saved

    def clear_question_memory(self, question_id: str) -> None:
        try:
            self._store._db.execute(
                """DELETE FROM memory_items
                   WHERE json_extract(extra_json,'$.scope_channel')=?
                     AND json_extract(extra_json,'$.scope_chat_id')=?""",
                (LME_SCOPE_CHANNEL, question_id),
            )
            self._store._db.commit()
        except Exception as exc:
            logger.warning("clear_question_memory failed qid=%s: %s", question_id, exc)


# ─── Response ────────────────────────────────────────────────────────

class LMEResponseAgent:
    """从记忆库检索后，生成对 LongMemEval 问题的回答。"""

    def __init__(self, components: BenchmarkComponents, use_hyde: bool = False) -> None:
        self._retriever = components.retriever
        self._llm = components.llm_client
        self._model = components.model
        # HyDE：直接持有 light 客户端做同步调用，避免 asyncio.to_thread 在线程池中嵌套导致的死锁
        self._hyde_llm = components.light_llm_client if use_hyde else None
        self._hyde_model = components.light_model if use_hyde else None

    def answer_question(self, question: str, question_id: str) -> dict:
        try:
            logger.info("  retrieving for: %s", question[:80])
            items = asyncio.run(self._retrieve(question, question_id))
            logger.info("  retrieved %d items (pre-filter)", len(items))
            context_text, _ = self._retriever.build_injection_block(items)
            if not context_text:
                context_text = "\n".join(item.get("summary", "") for item in items)
            logger.info("  context_text len=%d", len(context_text))
            logger.info("  calling LLM for answer...")
            answer = self._generate_answer(question, context_text)
            logger.info("  answer: %s", answer[:100])
            return {
                "answer": answer,
                "retrieved_content": context_text,
                "retrieved_count": len(items),
            }
        except Exception as exc:
            logger.error("answer_question failed qid=%s: %s", question_id, exc)
            return {"answer": "", "retrieved_content": "", "retrieved_count": 0}

    async def _retrieve(self, question: str, question_id: str) -> list[dict]:
        async def _retrieve_fn(query: str, top_k: int | None = None) -> list[dict]:
            return await self._retriever.retrieve(
                query=query,
                memory_types=["event", "profile", "preference"],
                scope_channel=LME_SCOPE_CHANNEL,
                scope_chat_id=question_id,
                require_scope_match=True,
                top_k=top_k,
            )

        raw_items = await _retrieve_fn(question)

        if self._hyde_llm is not None:
            hypothesis = self._generate_hyde_hypothesis(question)
            if hypothesis:
                logger.debug("hyde hypothesis: %r", hypothesis[:80])
                hyde_items = await _retrieve_fn(hypothesis)
                raw_items = _union_dedup(raw_items, hyde_items)

        return raw_items

    def _generate_hyde_hypothesis(self, query: str) -> str | None:
        """同步生成假想记忆条目（不使用 asyncio.to_thread，避免线程池死锁）。"""
        prompt = (
            "你是个人助手的记忆系统。根据用户提问，生成一条"
            "**如果该信息存在于记忆数据库中会长什么样**的假想条目。\n"
            "规则：\n"
            "- 第三人称（\"用户...\"），与数据库条目语体一致（简洁的事实陈述）\n"
            "- 只输出那一条文本，不要解释\n\n"
            f"用户提问：{query}\n"
            "假想记忆条目："
        )
        try:
            resp = self._hyde_llm.chat.completions.create(
                model=self._hyde_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=80,
                temperature=0.1,
            )
            return (resp.choices[0].message.content or "").strip() or None
        except Exception as exc:
            logger.debug("hyde hypothesis failed: %s", exc)
            return None

    def _generate_answer(self, question: str, context: str) -> str:
        prompt = (
            "Based on the conversation history below, answer the question concisely.\n"
            "Rules:\n"
            "- Use the EXACT numbers, names, and values from the history. "
            "Never substitute with values from your general knowledge.\n"
            "- You may make reasonable inferences (e.g., if someone visits a city "
            "to meet their sister, that city is likely where the sister lives).\n"
            "- Pay attention to completion status: 'preparing to attend' or 'planning' "
            "means not done yet; 'recently completed' or 'already did' means done.\n"
            "- Only say 'I don't know' if there is truly no relevant information.\n\n"
            f"History:\n{context}\n\n"
            f"Question: {question}\n\n"
            "Answer (≤30 words):"
        )
        try:
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                temperature=0.1,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.error("LLM answer failed: %s", exc)
            return ""


# ─── Main Tester ─────────────────────────────────────────────────────

class AkasicLMETester:
    def __init__(
        self,
        config_path: str,
        db_path: str | None = None,
        max_workers: int = 2,
        question_type_filter: list[str] | None = None,
        use_flash: bool = False,
        use_hyde: bool = False,
    ) -> None:
        self.components = load_benchmark_components(
            config_path=config_path,
            db_path=db_path or DEFAULT_LME_DB,
        )

        if use_flash:
            from dataclasses import replace
            qa_components = replace(
                self.components,
                llm_client=self.components.light_llm_client,
                model=self.components.light_model,
            )
        else:
            qa_components = self.components

        self.mem_agent = LMEMemAgent(components=self.components)
        self.response_agent = LMEResponseAgent(components=qa_components, use_hyde=use_hyde)
        self.evaluate_agent = EvaluateAgent(
            chat_deployment=qa_components.model,
            api_key=str(qa_components.llm_client.api_key),
            azure_endpoint=str(qa_components.llm_client.base_url),
        )

        self.max_workers = max_workers
        self.question_type_filter = question_type_filter
        self.processing_time = 0.0

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.error_log_file = f"lme_error_log_{ts}.txt"
        with open(self.error_log_file, "w", encoding="utf-8") as f:
            f.write(f"Akasic LongMemEval Benchmark Error Log - {datetime.now()}\n{'='*80}\n")

    def _process_question(self, item: dict, skip_ingest: bool, idx: int, total: int) -> dict:
        qid = str(item.get("question_id", f"q{idx}"))
        qtype = str(item.get("question_type", ""))
        question = str(item.get("question", ""))
        answer = item.get("answer")

        if not question or answer is None:
            return {"skip": True}

        if self.question_type_filter and qtype not in self.question_type_filter:
            return {"skip": True}

        answer = str(answer)
        haystack_sessions: list[list[dict]] = item.get("haystack_sessions", [])
        haystack_dates: list[str] = item.get("haystack_dates", [])

        # 日期列表不足时用空字符串补齐
        while len(haystack_dates) < len(haystack_sessions):
            haystack_dates.append("")

        # Ingest
        if not skip_ingest:
            logger.info(f"[{idx+1}/{total}] {qid} ({qtype}) ingest start — {len(haystack_sessions)} sessions")
            fact_count = self.mem_agent.ingest_haystack(
                question_id=qid,
                haystack_sessions=haystack_sessions,
                haystack_dates=haystack_dates,
            )
            logger.info(f"[{idx+1}/{total}] {qid} ingest done — {fact_count} facts total")
        else:
            logger.info(f"[{idx+1}/{total}] {qid} ({qtype}) skip ingest")

        # Answer
        logger.info(f"[{idx+1}/{total}] {qid} answering...")
        resp = self.response_agent.answer_question(question, qid)
        generated_answer = resp.get("answer", "")
        retrieved_content = resp.get("retrieved_content", "")

        # Evaluate
        logger.info(f"[{idx+1}/{total}] {qid} evaluating...")
        eval_result = self.evaluate_agent.evaluate_answer_accuracy(
            question=question,
            generated_answer=generated_answer,
            standard_answer=answer,
        )
        is_correct = eval_result.get("is_correct", False)
        explanation = eval_result.get("explanation", "")

        mark = "✓" if is_correct else "✗"
        logger.info(
            f"[{idx+1}/{total}] {qid} {mark} type={qtype} "
            f"gen=\"{generated_answer[:60]}\" std=\"{answer[:60]}\""
        )

        if not is_correct:
            self._log_error(
                qid=qid,
                qtype=qtype,
                question=question,
                generated_answer=generated_answer,
                standard_answer=answer,
                retrieved_content=retrieved_content,
                explanation=explanation,
            )

        return {
            "skip": False,
            "question_id": qid,
            "question_type": qtype,
            "question": question,
            "generated_answer": generated_answer,
            "standard_answer": answer,
            "is_correct": is_correct,
            "retrieved_count": resp.get("retrieved_count", 0),
            "explanation": explanation,
        }

    def _log_error(self, **kw) -> None:
        try:
            lines = [
                f"\n{'='*80}",
                f"QID: {kw['qid']}  TYPE: {kw['qtype']}",
                f"QUESTION:\n{kw['question']}\n",
                f"RETRIEVED CONTENT:\n{kw.get('retrieved_content','')}\n",
                f"GENERATED ANSWER:\n{kw['generated_answer']}\n",
                f"STANDARD ANSWER:\n{kw['standard_answer']}\n",
                f"EXPLANATION:\n{kw.get('explanation','')}\n",
                f"{'='*80}\n",
            ]
            with open(self.error_log_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass

    def run(self, data: list[dict], skip_ingest: bool = False) -> dict:
        t0 = time.time()
        total = len(data)
        results: list[dict] = []

        # 顺序处理（ingest 和 query 绑定 question_id，不互相干扰）
        for idx, item in enumerate(data):
            r = self._process_question(item, skip_ingest=skip_ingest, idx=idx, total=total)
            if not r.get("skip"):
                results.append(r)

        self.processing_time = time.time() - t0
        return self._compile_results(results)

    def _compile_results(self, results: list[dict]) -> dict:
        total = len(results)
        correct = sum(1 for r in results if r.get("is_correct"))
        overall_acc = correct / total if total else 0.0

        by_type: dict[str, dict] = {}
        for r in results:
            qt = r.get("question_type", "unknown")
            if qt not in by_type:
                by_type[qt] = {"total": 0, "correct": 0}
            by_type[qt]["total"] += 1
            if r.get("is_correct"):
                by_type[qt]["correct"] += 1

        type_stats: dict = {}
        for qt, counts in sorted(by_type.items()):
            acc = counts["correct"] / counts["total"] if counts["total"] else 0.0
            label = QUESTION_TYPE_NAMES.get(qt, qt)
            type_stats[label] = {
                "correct": counts["correct"],
                "total": counts["total"],
                "accuracy": round(acc, 4),
            }

        summary = {
            "overall_accuracy": round(overall_acc, 4),
            "total_questions": total,
            "correct_answers": correct,
            "processing_time_s": round(self.processing_time, 1),
            "config_model": self.components.model,
            "config_embed_model": self.components.embedder._model,
            "config_top_k": self.components.retriever._top_k,
            "type_stats": type_stats,
        }

        logger.info("\n" + "=" * 60)
        logger.info(f"OVERALL ACCURACY: {overall_acc:.1%}  ({correct}/{total})")
        logger.info("=" * 60)
        for label, stats in type_stats.items():
            logger.info(
                f"  {label}: {stats['accuracy']:.1%} "
                f"({stats['correct']}/{stats['total']})"
            )
        logger.info(f"Processing time: {self.processing_time:.1f}s")
        logger.info("=" * 60)

        return {"summary": summary, "details": results}


# ─── CLI ─────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Akasic LongMemEval Benchmark")
    p.add_argument("--config", default=str(_PROJECT_ROOT / "config.json"))
    p.add_argument("--db-path", default=str(DEFAULT_LME_DB))
    p.add_argument("--data", default=str(_HERE / "data" / "longmemeval" / "longmemeval_s"))
    p.add_argument("--max-samples", type=int, default=None)
    p.add_argument("--question-type", default=None, help="逗号分隔的 question_type（默认全部）")
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--use-flash", action="store_true", help="response/evaluate 走 light model")
    p.add_argument("--use-hyde", action="store_true", help="检索时启用 HyDE 增强")
    p.add_argument("--question-ids", default=None, help="只测指定 question_id（逗号分隔）")
    p.add_argument("--output", default="result_lme.json")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    question_type_filter = (
        [qt.strip() for qt in args.question_type.split(",")]
        if args.question_type
        else None
    )

    data_path = Path(args.data)
    if not data_path.exists():
        logger.error(f"Data file not found: {data_path}")
        logger.error(
            "Download with:\n"
            "  pip install huggingface_hub\n"
            "  huggingface-cli download xiaowu0162/longmemeval "
            "--repo-type dataset --local-dir benchmark/data/longmemeval"
        )
        sys.exit(1)

    data: list[dict] = json.loads(data_path.read_text(encoding="utf-8"))
    if args.max_samples:
        data = data[: args.max_samples]
    if args.question_ids:
        qid_set = {q.strip() for q in args.question_ids.split(",")}
        data = [q for q in data if q.get("question_id", "") in qid_set]
        logger.info(f"Filtered to {len(data)} questions by --question-ids")
    logger.info(f"Loaded {len(data)} questions from {data_path}")

    tester = AkasicLMETester(
        config_path=args.config,
        db_path=args.db_path,
        question_type_filter=question_type_filter,
        use_flash=args.use_flash,
        use_hyde=args.use_hyde,
    )
    results = tester.run(data, skip_ingest=args.skip_ingest)

    out_path = Path(args.output)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
