"""
Akasic Benchmark on LoCoMo-10 Dataset

使用 Akasic memory2（SQLite + 向量搜索）对 LoCoMo 数据集进行基准测试。

用法（从 benchmark/ 目录运行）：
  cd benchmark/
 python run_locomo.py [options]

选项：
  --config PATH        config.json 路径（默认: ../config.json）
  --db-path PATH       benchmark 专用 DB 路径（默认: /tmp/akasic_benchmark/memory2.db）
  --ingest-mode MODE   gold | llm（默认: llm）
  --data PATH          locomo10.json 路径（默认: data/locomo10.json）
  --max-workers N      QA 并发线程数（默认: 3）
  --max-samples N      最多处理 N 个 conversation（默认: 全部）
  --category N[,N...]  只测 指定 category（逗号分隔，默认: 全部）
  --skip-ingest        跳过 session 摄取（使用已有 DB）
  --output PATH        结果输出 JSON 路径（默认: result_akasic.json）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import dotenv

dotenv.load_dotenv()

# 将项目根目录加入 path
_HERE = Path(__file__).parent
_PROJECT_ROOT = _HERE.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
# 将 benchmark/ 自身加入 path（使 memu 等可被 import）
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from akasic_mem_agent import AkasicMemAgent
from akasic_response_agent import AkasicResponseAgent
from config_loader import DEFAULT_BENCHMARK_DB, load_benchmark_components
from evaluate_agent import EvaluateAgent
from memu.utils import setup_logging

logger = setup_logging(__name__, enable_flush=True)

# ── 分类名称映射（与原 locomo_test.py 一致）──────────────────────────
CATEGORY_NAMES: dict[int, str] = {
    1: "Single-hop",
    2: "Single-hop (temporal)",
    3: "Multi-hop",
    4: "Adversarial",
    5: "Open-domain",
}


# ─────────────────────────── 主测试类 ────────────────────────────────


class AkasicLoCoMoTester:
    def __init__(
        self,
        config_path: str,
        db_path: str | None = None,
        ingest_mode: str = "llm",
        max_workers: int = 3,
        category_filter: list[int] | None = None,
        light_model_override: str | None = None,
        use_flash: bool = False,
        score_threshold_event: float | None = None,
        inject_max_event_profile: int | None = None,
    ) -> None:
        self.components = load_benchmark_components(
            config_path=config_path,
            db_path=db_path,
            light_model_override=light_model_override,
            score_threshold_event_override=score_threshold_event,
            inject_max_event_profile_override=inject_max_event_profile,
        )
        self.mem_agent = AkasicMemAgent(
            components=self.components,
            ingest_mode=ingest_mode,
        )
        # use_flash: response 和 evaluate 都走 light 端点，速度更快
        if use_flash:
            from dataclasses import replace
            qa_components = replace(
                self.components,
                llm_client=self.components.light_llm_client,
                model=self.components.light_model,
            )
        else:
            qa_components = self.components
        self.response_agent = AkasicResponseAgent(components=qa_components)
        self.evaluate_agent = EvaluateAgent(
            chat_deployment=qa_components.model,
            api_key=str(qa_components.llm_client.api_key),
            azure_endpoint=str(qa_components.llm_client.base_url),
        )

        self.ingest_mode = ingest_mode
        self.max_workers = max_workers
        self.category_filter = category_filter
        self.results: list[dict] = []
        self.processing_time = 0.0

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.error_log_file = f"qa_error_log_{ts}.txt"
        with open(self.error_log_file, "w", encoding="utf-8") as f:
            f.write(f"Akasic LoCoMo Benchmark Error Log - {datetime.now()}\n{'='*80}\n")

    # ── session 摄取 ──────────────────────────────────────────────────

    def _ingest_conversation(
        self,
        item: dict,
        sample_id: str,
        characters: list[str],
    ) -> dict:
        conv_data = item.get("conversation", {})
        observations = item.get("observation", {})
        session_keys = sorted(
            [k for k in conv_data if k.startswith("session_") and not k.endswith("_date_time")],
            key=lambda x: int(x.split("_")[1]) if x.split("_")[1].isdigit() else 0,
        )
        results = []
        for sk in session_keys:
            date_key = f"{sk}_date_time"
            session_date = conv_data.get(date_key, "Unknown Date")
            utterances = conv_data.get(sk, [])
            if not utterances:
                continue
            gold_observations = observations.get(f"{sk}_observation")
            r = self.mem_agent.update_character_memory(
                session_data=utterances,
                session_date=session_date,
                characters=characters,
                gold_observations=gold_observations,
            )
            results.append({"session": sk, "date": session_date, **r})
            logger.info(f"[{sample_id}] {sk} ({session_date}) ingested")
        return {"sessions": results}

    # ── 单条 QA ───────────────────────────────────────────────────────

    def _answer_qa(
        self,
        qa: dict,
        characters: list[str],
        qa_idx: int,
    ) -> dict:
        question = qa.get("question", "")
        standard_answer = qa.get("answer")
        category = qa.get("category", 0)

        if not question or standard_answer is None:
            return {"skip": True}
        standard_answer = str(standard_answer)

        if self.category_filter and category not in self.category_filter:
            return {"skip": True}

        resp = self.response_agent.answer_question(question, characters)
        generated_answer = resp.get("answer", "")
        retrieved_events = resp.get("retrieved_events", [])
        retrieved_content = resp.get("retrieved_content", "")

        # 评分
        eval_result = self.evaluate_agent.evaluate_answer_accuracy(
            question=question,
            generated_answer=generated_answer,
            standard_answer=standard_answer,
        )
        is_correct = eval_result.get("is_correct", False)
        explanation = eval_result.get("explanation", "")

        if not is_correct:
            self._log_error(
                qa_idx=qa_idx,
                question=question,
                generated_answer=generated_answer,
                standard_answer=standard_answer,
                category=category,
                retrieved_content=retrieved_content,
                evidence=str(qa.get("evidence", "")),
                explanation=explanation,
                retrieved_events=retrieved_events,
            )

        return {
            "skip": False,
            "question": question,
            "generated_answer": generated_answer,
            "standard_answer": standard_answer,
            "is_correct": is_correct,
            "category": category,
            "explanation": explanation,
            "retrieved_events_count": len(retrieved_events),
        }

    # ── 错误日志 ──────────────────────────────────────────────────────

    def _log_error(self, **kwargs) -> None:
        try:
            lines = [
                f"\n{'='*80}",
                f"QA INDEX: {kwargs['qa_idx'] + 1}",
                f"CATEGORY: {kwargs.get('category')}",
                f"QUESTION:\n{kwargs['question']}\n",
                f"EVIDENCE: {kwargs.get('evidence', '')}\n",
                f"RETRIEVED CONTENT:\n{kwargs.get('retrieved_content', '')}\n",
                f"GENERATED ANSWER:\n{kwargs['generated_answer']}\n",
                f"STANDARD ANSWER:\n{kwargs['standard_answer']}\n",
                f"EXPLANATION:\n{kwargs.get('explanation', '')}\n",
                f"{'='*80}\n",
            ]
            with open(self.error_log_file, "a", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass

    # ── 主流程 ────────────────────────────────────────────────────────

    def run(
        self,
        data: list[dict],
        skip_ingest: bool = False,
    ) -> dict:
        t0 = time.time()
        all_qa_results: list[dict] = []

        for item in data:
            sample_id = str(item.get("sample_id", "?"))
            conv_data = item.get("conversation", {})
            speaker_a = conv_data.get("speaker_a", "SpeakerA")
            speaker_b = conv_data.get("speaker_b", "SpeakerB")
            characters = [speaker_a, speaker_b]
            qa_list: list[dict] = item.get("qa", [])

            logger.info(
                f"\n{'='*60}\nSample {sample_id}: {speaker_a} & {speaker_b} "
                f"| QAs: {len(qa_list)}\n{'='*60}"
            )

            # 摄取对话（skip_ingest 时保留已有数据，不清库）
            if not skip_ingest:
                self.mem_agent.clear_character_memory(characters)
                self._ingest_conversation(item, sample_id, characters)

            # 并发回答 QA
            qa_results: list[dict] = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._answer_qa, qa, characters, idx): idx
                    for idx, qa in enumerate(qa_list)
                }
                done = 0
                for fut in as_completed(futures):
                    r = fut.result()
                    done += 1
                    if not r.get("skip"):
                        qa_results.append(r)
                        correct_so_far = sum(1 for x in qa_results if x.get("is_correct"))
                        mark = "✓" if r.get("is_correct") else "✗"
                        logger.info(
                            f"[{sample_id}] QA {done}/{len(qa_list)} {mark} "
                            f"cat={r.get('category')} acc={correct_so_far}/{len(qa_results)}"
                        )

            all_qa_results.extend(qa_results)

            # 本 sample 统计
            correct = sum(1 for r in qa_results if r.get("is_correct"))
            total = len(qa_results)
            logger.info(f"[{sample_id}] accuracy: {correct}/{total}")

        self.processing_time = time.time() - t0
        return self._compile_results(all_qa_results)

    # ── 汇总统计 ──────────────────────────────────────────────────────

    def _compile_results(self, qa_results: list[dict]) -> dict:
        total = len(qa_results)
        correct = sum(1 for r in qa_results if r.get("is_correct"))
        overall_acc = correct / total if total else 0.0

        # 按分类统计
        by_cat: dict[int, dict] = {}
        for r in qa_results:
            cat = r.get("category", 0)
            if cat not in by_cat:
                by_cat[cat] = {"total": 0, "correct": 0}
            by_cat[cat]["total"] += 1
            if r.get("is_correct"):
                by_cat[cat]["correct"] += 1

        category_stats = {}
        for cat, counts in sorted(by_cat.items()):
            cat_name = CATEGORY_NAMES.get(cat, f"Category {cat}")
            acc = counts["correct"] / counts["total"] if counts["total"] else 0
            category_stats[cat_name] = {
                "correct": counts["correct"],
                "total": counts["total"],
                "accuracy": round(acc, 4),
            }

        summary = {
            "overall_accuracy": round(overall_acc, 4),
            "total_questions": total,
            "correct_answers": correct,
            "processing_time_s": round(self.processing_time, 1),
            "benchmark_scope": (
                "episodic retrieval (event + profile). "
                "Does NOT cover procedure/preference extraction or query routing."
            ),
            "ingest_mode": self.ingest_mode,
            "config_model": self.components.model,
            "config_embed_model": self.components.embedder._model,
            "config_score_threshold_event": self.components.retriever._score_thresholds["event"],
            "config_top_k": self.components.retriever._top_k,
            "category_stats": category_stats,
        }

        # 打印结果
        logger.info("\n" + "=" * 60)
        logger.info(f"OVERALL ACCURACY: {overall_acc:.1%}  ({correct}/{total})")
        logger.info("=" * 60)
        for cat_name, stats in category_stats.items():
            logger.info(
                f"  {cat_name}: {stats['accuracy']:.1%} "
                f"({stats['correct']}/{stats['total']})"
            )
        logger.info(f"Processing time: {self.processing_time:.1f}s")
        logger.info("=" * 60)

        return {"summary": summary, "details": qa_results}


# ────────────────────────── CLI 入口 ─────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Akasic LoCoMo Benchmark")
    p.add_argument("--config", default=str(_PROJECT_ROOT / "config.json"))
    p.add_argument("--db-path", default=str(DEFAULT_BENCHMARK_DB), help="benchmark 专用 DB 路径")
    p.add_argument("--ingest-mode", choices=["gold", "llm"], default="llm")
    p.add_argument("--data", default=str(_HERE / "data" / "locomo10.json"))
    p.add_argument("--max-workers", type=int, default=3)
    p.add_argument("--max-samples", type=int, default=None)
    p.add_argument("--category", default=None, help="逗号分隔的 category 编号")
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--output", default="result_akasic.json")
    p.add_argument("--light-model", default=None, help="覆盖 light model（默认用 config.json 的 light_model）")
    p.add_argument("--use-flash", action="store_true", help="response/evaluate 用 light model（更快）")
    p.add_argument("--score-threshold-event", type=float, default=None, help="覆盖 event 召回阈值（默认用 config.json）")
    p.add_argument("--inject-max-event", type=int, default=None, help="每次最多注入几条 event/profile（默认用 config.json）")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    db_path = args.db_path
    category_filter = [int(c) for c in args.category.split(",")] if args.category else None

    # 加载数据
    data_path = Path(args.data)
    if not data_path.exists():
        logger.error(f"Data file not found: {data_path}")
        sys.exit(1)
    data: list[dict] = json.loads(data_path.read_text(encoding="utf-8"))
    if args.max_samples:
        data = data[: args.max_samples]
    logger.info(f"Loaded {len(data)} conversations from {data_path}")

    tester = AkasicLoCoMoTester(
        config_path=args.config,
        db_path=db_path,
        ingest_mode=args.ingest_mode,
        max_workers=args.max_workers,
        category_filter=category_filter,
        use_flash=args.use_flash,
        score_threshold_event=args.score_threshold_event,
        inject_max_event_profile=args.inject_max_event,
    )

    results = tester.run(data, skip_ingest=args.skip_ingest)

    # 保存结果
    out_path = Path(args.output)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
