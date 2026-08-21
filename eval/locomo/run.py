"""LoCoMo QA benchmark runner.

Conversation-level ingest + per-question persistence + resume support.

Usage:
  python -m eval.locomo.run --config eval/locomo/config.toml ^
      --data eval/locomo/data/locomo10.json ^
      --workspace /tmp/locomo_bench

  # Run only the first conversation:
  python -m eval.locomo.run ... --conversation-idx 0

  # Limit questions per conversation:
  python -m eval.locomo.run ... --conversation-idx 0 --limit 5

  # Resume from partial results:
  python -m eval.locomo.run ... --resume-auto

  # Skip ingest, only run QA:
  python -m eval.locomo.run ... --qa-only

  # Ingest only, skip QA:
  python -m eval.locomo.run ... --ingest-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from infra.storage.tenancy import DEFAULT_TENANT
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from bus.events import InboundMessage

logger = logging.getLogger("eval.locomo")


# ── Constants ──────────────────────────────────────────────────────────────────

_INGEST_STATE_FILE = "ingest_state.json"
_QA_RESULTS_DIR = "qa_results"
_FINALIZE_CHUNK_SIZE = 80
_DEFAULT_TIMEOUT_S = 180.0


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run LoCoMo QA benchmark.")
    p.add_argument("--config", required=True, type=Path, help="Path to config.toml")
    p.add_argument("--data", required=True, type=Path, help="Path to locomo10.json")
    p.add_argument("--workspace", type=Path, default=Path("/tmp/locomo_bench"),
                   help="Workspace root directory")
    p.add_argument("--output", type=Path, default=None,
                   help="Output JSON path (default: eval/locomo/results/<ts>.json)")
    p.add_argument("--conversation-idx", type=int, default=None,
                   help="Only process one conversation by index (0-based)")
    p.add_argument("--limit", type=int, default=0,
                   help="Max questions per conversation (0 = all)")
    p.add_argument("--workers", type=int, default=1,
                   help="Concurrent workers (not used in conv-level mode)")
    p.add_argument("--resume-auto", action="store_true",
                   help="Resume: reuse existing per-question results")
    p.add_argument("--qa-only", action="store_true",
                   help="Skip ingest entirely")
    p.add_argument("--ingest-only", action="store_true",
                   help="Run ingest only, skip QA")
    p.add_argument("--timeout", type=float, default=_DEFAULT_TIMEOUT_S,
                   help="Per-question agent timeout in seconds")
    return p


# ── Rich helpers ───────────────────────────────────────────────────────────────

def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=28),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=False,
        transient=False,
    )


def _judge_str(jc: bool | None) -> str:
    if jc is None:
        return "—"
    return "✅" if jc else "❌"


def _f1_str(f1: float) -> str:
    icon = "✅" if f1 >= 0.8 else ("⚠️" if f1 >= 0.3 else "✗")
    return f"{icon} {f1:.2f}"


# ── Ingest state management ────────────────────────────────────────────────────

def _ingest_state_path(workspace: Path) -> Path:
    return workspace / _INGEST_STATE_FILE


def _load_ingest_state(workspace: Path) -> dict | None:
    path = _ingest_state_path(workspace)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_ingest_state(workspace: Path, *, completed: bool, **extra) -> None:
    data = {"completed": completed, **extra}
    _ingest_state_path(workspace).write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Per-question result persistence ────────────────────────────────────────────

def _qa_result_path(workspace: Path, qa_index: int) -> Path:
    return workspace / _QA_RESULTS_DIR / f"{qa_index:04d}.json"


def _load_qa_result(workspace: Path, qa_index: int) -> dict | None:
    path = _qa_result_path(workspace, qa_index)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_qa_result(workspace: Path, qa_index: int, result: dict) -> None:
    path = _qa_result_path(workspace, qa_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_all_qa_results(workspace: Path) -> list[dict]:
    """Load all saved per-question results, sorted by index."""
    results_dir = workspace / _QA_RESULTS_DIR
    if not results_dir.exists():
        return []
    results: list[dict] = []
    for fpath in sorted(results_dir.iterdir()):
        if fpath.suffix == ".json":
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                results.append(data)
            except Exception as exc:
                logger.warning("failed to load %s: %s", fpath, exc)
    return results


# ── Trace writer ───────────────────────────────────────────────────────────────

def _write_trace(workspace: Path, qa_index: int, result: dict, rt, sample_id: str) -> None:
    """Write trace.log with agent config, SELF.md, and tool chain."""
    from eval.longmemeval.qa_runner import format_tool_trace

    trace_dir = workspace / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_path = trace_dir / f"{qa_index:04d}.log"

    self_md_path = workspace / "memory" / "SELF.md"
    self_md_content = self_md_path.read_text(encoding="utf-8") if self_md_path.exists() else "(missing)"
    
    cfg = rt.core.config
    agent_cfg_text = (
        f"agent_model    = {cfg.agent_model or cfg.model}\n"
        f"agent_base_url = {cfg.agent_base_url or cfg.base_url}\n"
        f"main_model     = {cfg.model}\n"
        f"main_base_url  = {cfg.base_url}\n"
        f"light_model    = {cfg.light_model or '(none)'}\n"
    )
    trace = format_tool_trace(result.get("tool_chain") or [])
    trace_path.write_text(
        f"=== Agent Config ===\n{agent_cfg_text}\n"
        f"=== SELF.md ===\n{self_md_content}\n"
        f"=== Sample ID === {sample_id}\n"
        f"=== QA Index ===  {qa_index}\n"
        f"=== Question ===\n{result.get('question', '')}\n\n"
        f"=== Gold Answer ===\n{result.get('gold_answer', '')}\n\n"
        f"=== Predicted ===\n{result.get('predicted_answer', '')}\n\n"
        f"=== Judge === {result.get('judge_correct')}\n"
        f"=== Elapsed === {result.get('elapsed_s', 0)}s\n"
        f"=== Error === {result.get('error')}\n\n"
        f"=== ReAct trace ===\n{trace}\n",
        encoding="utf-8",
    )


# ── Ingest (conversation-level, one-shot) ──────────────────────────────────────

async def _ingest_conversation(
    rt,
    conversation,
    *,
    workspace: Path,
    force: bool = False,
    on_progress=None,
) -> int:
    """Ingest all sessions of a LoCoMo conversation into memory.

    Returns total number of turns ingested.
    """
    from eval.longmemeval.ingest import _last_dialogue_pair

    session_key = conversation.merged_session_key
    sm = rt.core.session_manager
    consolidation = rt.consolidation

    # Check if already ingested
    state = _load_ingest_state(workspace)
    if state and state.get("completed") is True and not force:
        logger.info("ingest already done for %s, skip", conversation.sample_id)
        return state.get("ingested_turns", 0)

    total_turns = 0
    n_sessions = len(conversation.sessions)
    dates = conversation.session_dates

    while len(dates) < n_sessions:
        dates.append("")

    for idx, (date_str, turns) in enumerate(zip(dates, conversation.sessions)):
        # Parse date
        ts = _parse_date(date_str)

        # Clear cache and get fresh session
        sm._cache.pop((DEFAULT_TENANT, session_key), None)
        session = sm.get_or_create(DEFAULT_TENANT, session_key)

        # Insert turns
        for turn in turns:
            session.add_message(turn.role, turn.content)
            session.messages[-1]["timestamp"] = ts
            total_turns += 1

        sm.save(session)
        sm._cache.pop((DEFAULT_TENANT, session_key), None)
        session = sm.get_or_create(DEFAULT_TENANT, session_key)

        # Consolidate
        await consolidation.consolidate(session, archive_all=False)
        sm.save(session)

        # Post-response worker for invalidation
        worker = getattr(rt.core.memory_runtime, "post_response_worker", None)
        if worker is not None:
            user_msg, agent_response = _last_dialogue_pair(turns)
            if user_msg:
                await worker.run(
                    user_msg,
                    agent_response,
                    [],
                    source_ref=f"{session_key}#post:{idx}",
                    session_key=session_key,
                )

        if on_progress:
            on_progress(idx + 1, n_sessions)

    # Finalize tail chunks
    sm._cache.pop((DEFAULT_TENANT, session_key), None)
    session = sm.get_or_create(DEFAULT_TENANT, session_key)
    await _finalize_tail_chunks(rt, session)
    session.last_consolidated = len(session.messages)
    sm.save(session)

    _write_ingest_state(
        workspace,
        completed=True,
        expected_turns=total_turns,
        ingested_turns=total_turns,
        n_sessions=n_sessions,
    )

    logger.info(
        "ingest done: %s  sessions=%d  turns=%d",
        conversation.sample_id,
        n_sessions,
        total_turns,
    )
    return total_turns


async def _finalize_tail_chunks(rt, session) -> None:
    """Consolidate unarchived tail in bounded chunks."""
    remaining = session.messages[session.last_consolidated:]
    if not remaining:
        return

    session_cls = session.__class__
    for start in range(0, len(remaining), _FINALIZE_CHUNK_SIZE):
        chunk = remaining[start : start + _FINALIZE_CHUNK_SIZE]
        temp_session = session_cls(key=session.key)
        temp_session.messages = list(chunk)
        temp_session.last_consolidated = 0
        for attr in ("_channel", "_chat_id"):
            if hasattr(session, attr):
                setattr(temp_session, attr, getattr(session, attr))
        await rt.consolidation.consolidate(temp_session, archive_all=True)


def _parse_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return datetime.now(tz=timezone.utc).isoformat()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            continue
    return raw


# ── QA runner (single question) ───────────────────────────────────────────────

async def _run_single_qa(
    rt,
    qa_pair,
    conversation,
    *,
    workspace: Path,
    timeout_s: float,
    qa_index: int,
) -> dict:
    """Run one QA question and return result dict."""
    from eval.longmemeval.metrics import judge_answer, token_f1

    loop = rt.core.loop
    sample_id = conversation.sample_id

    # Create fresh QA session key
    qa_key = f"{conversation.merged_session_key}:qa:{qa_index}"

    # Clean up any previous QA state
    _purge_qa_session(rt, qa_key)

    t0 = time.monotonic()
    error: str | None = None
    predicted = ""

    try:
        msg = InboundMessage(
            channel="benchmark",
            sender="user",
            chat_id=sample_id,
            tenant_id=DEFAULT_TENANT,
            content=qa_pair.question + "\n\n[Respond in English only. One sentence or short phrase.]",
            timestamp=datetime.now(tz=timezone.utc),
        )
        outbound = await asyncio.wait_for(
            loop._process(msg, session_key=qa_key, dispatch_outbound=False),
            timeout=timeout_s,
        )
        predicted = outbound.content if outbound else ""
    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s}s"
        logger.warning("QA timeout: %s  qa_idx=%d", sample_id, qa_index)
    except Exception as exc:
        error = str(exc)
        logger.exception("QA error: %s  qa_idx=%d", sample_id, qa_index)

    elapsed = time.monotonic() - t0

    # Extract tool chain
    tool_chain = _extract_tool_chain(rt.core.session_manager, qa_key)

    # Compute metrics
    gold = qa_pair.answer
    f1 = token_f1(predicted, gold)
    em = 1.0 if predicted.strip().lower() == gold.strip().lower() else 0.0

    # LLM Judge (skip on error)
    judge_correct: bool | None = None
    if not error and predicted.strip():
        provider = rt.core.provider
        cfg = rt.core.config
        try:
            judge_correct = await judge_answer(
                provider, cfg.model,
                question=qa_pair.question,
                gold=gold,
                predicted=predicted,
            )
        except Exception as exc:
            logger.warning("judge failed: %s", exc)
            judge_correct = None

    result = {
        "qa_index": qa_index,
        "sample_id": sample_id,
        "question": qa_pair.question,
        "gold_answer": gold,
        "predicted_answer": predicted,
        "category": qa_pair.category,
        "category_name": qa_pair.category_name,
        "is_adversarial": qa_pair.is_adversarial,
        "evidence": qa_pair.evidence,
        "tool_chain": tool_chain,
        "elapsed_s": round(elapsed, 2),
        "error": error,
        "f1": round(f1, 4),
        "em": em,
        "judge_correct": judge_correct,
    }

    # Save result immediately
    _save_qa_result(workspace, qa_index, result)

    # Write trace
    _write_trace(workspace, qa_index, result, rt, sample_id)

    return result


def _purge_qa_session(rt, qa_key: str) -> None:
    """Remove any existing QA session data."""
    sm = rt.core.session_manager
    sm._cache.pop((DEFAULT_TENANT, qa_key), None)

    # Also clean DB if session exists
    workspace = getattr(sm, "_store_path", None)
    if workspace is None:
        try:
            from session.store import SessionStore
            if hasattr(sm, "_store") and hasattr(sm._store, "_db_path"):
                db_path = sm._store._db_path
            else:
                return
        except Exception:
            return
    else:
        db_path = workspace / "sessions.db"

    if db_path and db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("DELETE FROM messages WHERE session_key = ?", (qa_key,))
                conn.execute("DELETE FROM sessions WHERE key = ?", (qa_key,))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("purge qa session db: %s", exc)


def _extract_tool_chain(session_manager, qa_key: str) -> list[dict]:
    """Pull tool_chain from the last assistant message."""
    try:
        session_manager._cache.pop((DEFAULT_TENANT, qa_key), None)
        session = session_manager.get_or_create(DEFAULT_TENANT, qa_key)
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant" and msg.get("tool_chain"):
                return msg["tool_chain"]
    except Exception as exc:
        logger.debug("tool_chain extraction failed: %s", exc)
    return []


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score_results(results: list[dict]) -> dict:
    """Aggregate scores by category.
    
    Returns:
        {"overall": {...}, "by_type": {...}}
    """
    by_type: dict[str, list[dict]] = {}
    for r in results:
        qt = r.get("category_name") or "unknown"
        by_type.setdefault(qt, []).append(r)

    def _agg(items: list[dict]) -> dict:
        n = len(items)
        errors = sum(1 for r in items if r.get("error"))
        
        f1s = [r.get("f1", 0.0) for r in items if not r.get("error")]
        ems = [r.get("em", 0.0) for r in items if not r.get("error")]
        judged = [r for r in items if r.get("judge_correct") is not None and not r.get("error")]
        
        f1 = round(sum(f1s) / len(f1s), 4) if f1s else 0.0
        em = round(sum(ems) / len(ems), 4) if ems else 0.0
        judge_acc = round(sum(1 for r in judged if r["judge_correct"]) / len(judged), 4) if judged else None
        
        result = {"f1": f1, "em": em, "n": n, "errors": errors}
        if judge_acc is not None:
            result["judge_acc"] = judge_acc
        return result

    return {
        "overall": _agg(results),
        "by_type": {qt: _agg(items) for qt, items in sorted(by_type.items())},
    }


# ── Conversation runner ────────────────────────────────────────────────────────

async def _run_conversation(
    conversation,
    *,
    args,
    console: Console,
    progress,
    overall_task,
    worker_task,
    t_start: float,
) -> list[dict]:
    """Process one full LoCoMo conversation: ingest + all QAs."""
    from eval.longmemeval.runtime import close_runtime, create_runtime

    conv_workspace = args.workspace / conversation.sample_id
    conv_workspace.mkdir(parents=True, exist_ok=True)
    short_id = conversation.sample_id[:8]
    n_qa = len(conversation.qa_pairs)

    # Limit questions
    qa_pairs = conversation.qa_pairs
    if args.limit > 0:
        qa_pairs = qa_pairs[:args.limit]

    # Check resume: collect already-completed results
    completed_indices: set[int] = set()
    if args.resume_auto:
        for qa in qa_pairs:
            cached = _load_qa_result(conv_workspace, qa.qa_index)
            if cached is not None:
                completed_indices.add(qa.qa_index)

    # ── Ingest ────────────────────────────────────────────────────────────
    if not args.qa_only:
        should_ingest = True
        if args.resume_auto:
            state = _load_ingest_state(conv_workspace)
            should_ingest = not (state and state.get("completed") is True)

        if should_ingest:
            n_sessions = len(conversation.sessions)

            def _on_progress(done: int, total: int) -> None:
                progress.update(
                    worker_task,
                    description=f"[cyan]{short_id}[/]  ingest {done}/{total}",
                    completed=done,
                    total=total,
                )

            rt = await create_runtime(args.config, conv_workspace)
            try:
                progress.update(
                    worker_task,
                    description=f"[cyan]{short_id}[/]  ingest 0/{n_sessions}",
                    completed=0, total=n_sessions,
                )
                await _ingest_conversation(
                    rt, conversation,
                    workspace=conv_workspace,
                    force=not args.resume_auto,
                    on_progress=_on_progress,
                )
            finally:
                await close_runtime(rt)
        elif args.resume_auto:
            progress.update(
                worker_task,
                description=f"[cyan]{short_id}[/]  [yellow]ingest-cached[/]",
                completed=0, total=1,
            )

    if args.ingest_only:
        progress.update(overall_task, advance=1)
        return []

    # ── QA ─────────────────────────────────────────────────────────────────
    remaining = [qa for qa in qa_pairs if qa.qa_index not in completed_indices]
    if args.resume_auto and remaining:
        logger.info(
            "%s: %d/%d questions already done, %d remaining",
            conversation.sample_id,
            len(completed_indices), n_qa, len(remaining),
        )

    if not remaining:
        # All cached — load from disk
        progress.update(
            worker_task,
            description=f"[cyan]{short_id}[/]  [yellow]all-cached[/]",
            completed=0, total=1,
        )
        results = _load_all_qa_results(conv_workspace)
        progress.update(overall_task, advance=1)
        return results

    # Create runtime for QA
    rt = await create_runtime(args.config, conv_workspace)
    results: list[dict] = []

    try:
        for idx, qa in enumerate(remaining):
            qa_idx = qa.qa_index
            progress.update(
                worker_task,
                description=f"[cyan]{short_id}[/]  [yellow]qa {idx+1}/{len(remaining)}[/]",
                completed=0, total=1,
            )

            result = await _run_single_qa(
                rt, qa, conversation,
                workspace=conv_workspace,
                timeout_s=args.timeout,
                qa_index=qa_idx,
            )
            results.append(result)

            # Print result panel
            _print_qa_result(result, console, idx, len(remaining), short_id, t_start)

        # Also load any cached results
        if args.resume_auto:
            for qa in qa_pairs:
                if qa.qa_index in completed_indices:
                    cached = _load_qa_result(conv_workspace, qa.qa_index)
                    if cached:
                        results.append(cached)

    finally:
        await close_runtime(rt)

    # Sort by qa_index for consistent ordering
    results.sort(key=lambda r: r.get("qa_index", 0))
    progress.update(overall_task, advance=1)
    return results


def _print_qa_result(
    result: dict,
    console: Console,
    idx: int,
    total: int,
    short_id: str,
    t_start: float,
) -> None:
    """Print a rich panel for one QA result."""
    from eval.longmemeval.metrics import token_f1

    judged = result.get("judge_correct")
    f1 = result.get("f1", 0.0)
    elapsed_total = time.monotonic() - t_start
    eta_s = (elapsed_total / (idx + 1)) * (total - idx - 1)
    eta_str = f"{eta_s/3600:.1f}h" if eta_s > 3600 else f"{eta_s/60:.1f}m"

    pred_text = (result.get("predicted_answer") or "(empty)")[:120]
    gold_text = (result.get("gold_answer") or "")[:120]
    question_text = (result.get("question") or "")[:120]

    body = Text()
    body.append("  Q     ", style="dim")
    body.append(question_text + "\n")
    body.append("  pred  ", style="dim")
    pred_style = "bold green" if judged else ("bold red" if judged is False else "bold")
    body.append(pred_text + "\n", style=pred_style)
    body.append("  gold  ", style="dim")
    body.append(gold_text, style="green")
    if result.get("error"):
        body.append(f"\n  err   {result['error']}", style="red")

    title = (
        f"[dim][{idx+1:03d}/{total}][/]  [bold cyan]{short_id}[/]  "
        f"[dim]{result.get('category_name', '?')}[/]"
    )
    subtitle = (
        f"judge={_judge_str(judged)}  {_f1_str(f1)}  "
        f"[dim]{result['elapsed_s']:.0f}s  ETA {eta_str}[/]"
    )
    console.print(Panel(body, title=title, subtitle=subtitle, padding=(0, 1)))


# ── Main ───────────────────────────────────────────────────────────────────────

async def _run(args: argparse.Namespace) -> None:
    import sys

    from .dataset import load_locomo

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )

    # Load data
    if not args.data.exists():
        print(f"ERROR: data file not found: {args.data}")
        sys.exit(1)

    conversations = load_locomo(args.data)
    if not conversations:
        print("ERROR: no conversations loaded")
        sys.exit(1)

    # Filter to one conversation if specified
    if args.conversation_idx is not None:
        if args.conversation_idx >= len(conversations):
            print(f"ERROR: conversation-idx {args.conversation_idx} out of range (0-{len(conversations)-1})")
            sys.exit(1)
        conversations = [conversations[args.conversation_idx]]

    args.workspace.mkdir(parents=True, exist_ok=True)
    console = Console()

    total_qa = sum(len(c.qa_pairs) for c in conversations)
    console.print(
        Rule(f"[bold]LoCoMo QA[/]  {len(conversations)} conversations  {total_qa} questions  "
             f"workers={args.workers}")
    )

    all_results: list[dict] = []
    t_start = time.monotonic()

    progress = _make_progress(console)
    with progress:
        overall_task = progress.add_task("[bold]Overall[/]", total=len(conversations))
        worker_task = progress.add_task("[dim]idle[/]", total=1, completed=0)

        for conv in conversations:
            conv_results = await _run_conversation(
                conv,
                args=args,
                console=console,
                progress=progress,
                overall_task=overall_task,
                worker_task=worker_task,
                t_start=t_start,
            )
            all_results.extend(conv_results)

    if args.ingest_only:
        console.print("[green]Ingest-only complete.[/]")
        return

    # ── Final scores ───────────────────────────────────────────────────────
    elapsed = time.monotonic() - t_start
    scores = _score_results(all_results)
    ov = scores["overall"]

    judged = [r for r in all_results if r.get("judge_correct") is not None]
    judge_acc = sum(1 for r in judged if r["judge_correct"]) / len(judged) if judged else 0.0

    table = Table(
        title=f"Results  —  elapsed {elapsed/3600:.1f}h",
        show_header=True, header_style="bold", min_width=70,
    )
    table.add_column("Category", style="cyan", min_width=20)
    table.add_column("judge", justify="right")
    table.add_column("F1", justify="right")
    table.add_column("EM", justify="right")
    table.add_column("n", justify="right")
    table.add_column("errors", justify="right")

    table.add_row(
        "[bold]Overall[/]",
        f"[bold]{judge_acc:.1%}[/]",
        f"[bold]{ov['f1']:.4f}[/]",
        f"[bold]{ov['em']:.4f}[/]",
        str(ov["n"]),
        str(ov["errors"]),
        end_section=True,
    )
    for cat, s in sorted(scores["by_type"].items()):
        cat_judged = [
            r for r in all_results
            if r.get("category_name") == cat and r.get("judge_correct") is not None
        ]
        cat_acc = sum(1 for r in cat_judged if r["judge_correct"]) / len(cat_judged) if cat_judged else 0.0
        table.add_row(
            cat,
            f"{cat_acc:.1%}",
            f"{s['f1']:.4f}",
            f"{s['em']:.4f}",
            str(s["n"]),
            str(s.get("errors", 0)),
        )

    console.print(table)

    # ── Save ───────────────────────────────────────────────────────────────
    output = args.output
    if output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        output = results_dir / f"{ts}.json"

    payload = {
        "timestamp": datetime.now().isoformat(),
        "data": str(args.data),
        "workspace": str(args.workspace),
        "workers": args.workers,
        "scores": scores,
        "judge_acc": judge_acc,
        "results": all_results,
    }
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    console.print(f"\n  Saved → [bold]{output}[/]")

    # Print workspace summary
    console.print(f"\n  Per-question results: [bold]{args.workspace / '(conv)' / _QA_RESULTS_DIR}[/]")
    console.print(f"  Per-question traces:  [bold]{args.workspace / '(conv)' / 'traces'}[/]")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
