"""Draven 12-agent swarm — real multi-agent orchestration, not a simulation.

Every agent action executes through the real typed tool registry
(``app/draven_tools.py``) and the configured LLM provider
(stub/anthropic/openai-compatible). Nothing is faked:

* each agent has a typed definition (id, name, role, system prompt,
  least-privilege tool subset, max delegation depth);
* the orchestrator decomposes a goal into subtasks, fans out independent
  subtasks concurrently (``asyncio.gather``), runs dependent levels
  sequentially, and synthesizes a result;
* high-risk tools still return ``approval_required`` when called by an
  agent — approval gating is inherited, never bypassed;
* every tool call writes an audit row to ``draven_tool_runs`` attributed
  with ``agent_id`` / ``swarm_run_id``;
* every run writes an append-only event feed to ``draven_swarm_events``
  (the frontend polls this — investors see real activity, never a
  fabricated feed);
* when the LLM provider is unconfigured (stub), agents run their real
  tools and degrade honestly: generation steps are marked
  ``degraded`` with a plain-language note instead of invented output.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.orm import Session

from forge_db.models import (
    DravenSwarmEvent,
    DravenSwarmRun,
    DravenSwarmRunStatus,
    User,
)

from app.draven_tools import TOOLS, execute_tool

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

AGENT_TIMEOUT_S = 60.0      # generous per-agent budget (stage-robust)
RUN_TIMEOUT_S = 300.0       # overall run ceiling
MAX_AGENTS = 12


@dataclass
class AgentDef:
    id: str
    name: str
    role: str
    system_prompt: str
    allowed_tools: list[str]
    max_depth: int = 1

    def __post_init__(self) -> None:
        unknown = [t for t in self.allowed_tools if t not in TOOLS]
        if unknown:
            raise ValueError(
                f"agent {self.id} references unknown tools: {unknown}"
            )


def _mk(
    agent_id: str,
    name: str,
    role: str,
    system_prompt: str,
    allowed_tools: list[str],
    max_depth: int = 1,
) -> AgentDef:
    return AgentDef(
        id=agent_id, name=name, role=role,
        system_prompt=system_prompt, allowed_tools=allowed_tools,
        max_depth=max_depth,
    )


AGENTS: dict[str, AgentDef] = {}

for _def in [
    _mk(
        "supervisor",
        "Supervisor",
        "Orchestrator. Decomposes goals into subtasks, assigns agents, "
        "and synthesizes the final result. Never touches write tools.",
        "You are the Supervisor, the orchestrator of the Draven agent "
        "swarm inside ForgeOS. Break the user's goal into concrete "
        "subtasks, assign each to the best-qualified agent, and "
        "synthesize their verified results into one coherent answer. "
        "Only report what agents actually found — never invent outcomes.",
        [
            "draven.business_summary",
            "draven.campaigns_status",
            "draven.analytics_summary",
            "draven.autopilot_status",
            "draven.approvals_pending",
        ],
    ),
    _mk(
        "researcher",
        "Researcher",
        "Market intelligence. Finds product opportunities and demand "
        "signals with evidence provenance.",
        "You are the Researcher. Use the market-intelligence tools to "
        "find real product opportunities. Every claim must carry its "
        "evidence: connectors used and evidence gaps. If no live data "
        "source is configured, say so plainly instead of guessing.",
        [
            "market.research_start",
            "market.research_status",
            "market.top_opportunities",
        ],
    ),
    _mk(
        "brand_guardian",
        "Brand Guardian",
        "Protects the brand voice. Reviews queued content against the "
        "brand kit and flags off-voice material.",
        "You are the Brand Guardian. Your job is voice consistency: "
        "review assets awaiting approval and flag anything that does not "
        "sound like the brand. You do not rewrite copy — you judge it.",
        [
            "draven.business_summary",
            "draven.assets_pending_review",
            "draven.approvals_pending",
        ],
    ),
    _mk(
        "copywriter",
        "Copywriter",
        "Drafts on-brand marketing copy via the configured LLM provider. "
        "Honest degradation when no provider is configured.",
        "You are the Copywriter. Draft marketing copy in the brand's "
        "voice from the brief you are given. Keep it tight, specific, "
        "and channel-appropriate. Never present unreviewed drafts as "
        "approved — they always go through the approval queue.",
        [
            "draven.business_summary",
            "draven.assets_pending_review",
        ],
    ),
    _mk(
        "channel_adapter",
        "Channel Adapter",
        "Adapts approved copy for email / SMS / social formats and "
        "consent constraints.",
        "You are the Channel Adapter. Take a message and reshape it for "
        "each channel: email (subject + body), SMS (under 160 chars, "
        "consent-aware), social (hook-first). Respect quiet hours and "
        "consent — a message that cannot legally send is a failure.",
        ["draven.business_summary"],
    ),
    _mk(
        "scheduler",
        "Scheduler",
        "Plans send timing against quiet hours, daily caps, and the "
        "weekly content plan.",
        "You are the Scheduler. Plan when things go out: respect quiet "
        "hours, the daily send cap, and the current weekly content plan. "
        "Propose a concrete schedule — day, channel, slot — with the "
        "reasoning for each choice.",
        [
            "draven.autopilot_status",
            "draven.campaigns_status",
        ],
    ),
    _mk(
        "analyst",
        "Analyst",
        "Reads performance data. Reports what happened with the same "
        "math the UI shows.",
        "You are the Analyst. Read the analytics and report what the "
        "numbers actually say: sent, opened, clicked, converted, rates, "
        "spend. Compare against prior periods when data exists. No "
        "vanity metrics, no invented trends.",
        [
            "draven.analytics_summary",
            "draven.campaigns_status",
        ],
    ),
    _mk(
        "optimizer",
        "Optimizer",
        "Turns evidence into recommendations: what to double down on, "
        "what to stop.",
        "You are the Optimizer. Turn evidence into decisions: which "
        "channels, assets, and segments deserve more budget, and which "
        "should be paused. Every recommendation must cite the evidence "
        "that supports it. Uncertain calls are labeled uncertain.",
        [
            "draven.analytics_summary",
            "draven.autopilot_status",
        ],
    ),
    _mk(
        "compliance",
        "Compliance",
        "Guardrails. Checks affiliate disclosures, consent, and policy "
        "before anything ships.",
        "You are the Compliance officer. Nothing ships on your watch "
        "without its guardrails: affiliate disclosures on affiliate "
        "content, consent for email/SMS, no unapproved assets in "
        "campaigns. Flag violations precisely; never wave them through.",
        [
            "draven.assets_pending_review",
            "draven.approvals_pending",
        ],
    ),
    _mk(
        "outreach",
        "Outreach",
        "Lead follow-up. Qualifies leads and stages follow-ups for "
        "approval — never sends on its own.",
        "You are the Outreach agent. Qualify leads with the deterministic "
        "engine and stage follow-ups for human approval. You never send "
        "anything yourself: every follow-up goes through the approval "
        "queue bound to its exact payload.",
        [
            "alpha.lead_intake",
            "alpha.qualify_lead",
            "alpha.run_status",
            "alpha.prepare_followup",
            "draven.contacts_search",
        ],
    ),
    _mk(
        "onboarder",
        "Onboarder",
        "New-client onboarding. Gathers business context and kicks off "
        "the Card 0 interview path.",
        "You are the Onboarder. Welcome new clients: summarize what you "
        "know about their business, check for existing contacts and "
        "leads, and prepare the ground for their brand-kit interview. "
        "Make the first session feel effortless.",
        [
            "draven.business_summary",
            "draven.contacts_search",
            "alpha.lead_intake",
        ],
    ),
    _mk(
        "asset_reviewer",
        "Asset Reviewer",
        "Works the review queue. Summarizes pending assets and proposes "
        "approvals — the human still decides.",
        "You are the Asset Reviewer. Work through the review queue: "
        "summarize each pending asset, note anything questionable, and "
        "propose approvals. Proposing is not approving — every approval "
        "still needs the human's explicit token.",
        [
            "draven.approvals_pending",
            "draven.assets_pending_review",
            "draven.asset_approve",
        ],
    ),
]:
    if _def.id in AGENTS:
        raise ValueError(f"duplicate agent id: {_def.id}")
    AGENTS[_def.id] = _def

assert len(AGENTS) == MAX_AGENTS, f"expected {MAX_AGENTS} agents, got {len(AGENTS)}"


def agent_definitions() -> list[dict[str, Any]]:
    """Public agent cards (no secrets, no prompt internals beyond role)."""
    return [
        {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "allowed_tools": a.allowed_tools,
            "max_depth": a.max_depth,
        }
        for a in AGENTS.values()
    ]


# ---------------------------------------------------------------------------
# Demo scenario
# ---------------------------------------------------------------------------

DEMO_GOAL = (
    "Plan and draft this week's campaign: research what's trending, "
    "check what worked last month, review the brand voice and the "
    "approval queue, then produce a 7-day campaign plan with channel mix "
    "and timing."
)
DEMO_CONTEXT = {"demo": True, "audience": "investors"}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class Subtask:
    agent_id: str
    task: str
    tools: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    agent_id: str
    status: str  # ok | degraded | error | approval_needed
    summary: str
    tools_used: list[dict[str, Any]] = field(default_factory=list)
    approvals_needed: list[dict[str, Any]] = field(default_factory=list)
    degraded: bool = False
    duration_ms: int = 0


def _heuristic_plan(goal: str) -> list[Subtask]:
    """Deterministic decomposition (stub provider or LLM fallback).

    Level 0 gathers evidence concurrently; level 1 reasons over it;
    the supervisor synthesizes last. The plan is honest about what each
    agent can actually do with the tools it holds.
    """
    g = goal.lower()
    plan: list[Subtask] = []

    def add(agent_id: str, task: str,
            tools: list[tuple[str, dict[str, Any]]] | None = None,
            depends_on: list[str] | None = None) -> None:
        plan.append(Subtask(
            agent_id=agent_id, task=task,
            tools=tools or [], depends_on=depends_on or [],
        ))

    level0 = ["researcher", "analyst", "brand_guardian", "scheduler"]
    level1 = ["optimizer", "copywriter", "channel_adapter", "compliance",
              "asset_reviewer"]

    if any(k in g for k in ("campaign", "plan", "week", "content", "marketing")):
        add("researcher",
            "Surface the top product opportunities with evidence provenance.",
            tools=[("market.top_opportunities", {"limit": 5})])
        add("analyst",
            "Report last-30-day performance: what worked, what didn't.",
            tools=[("draven.analytics_summary", {})])
        add("brand_guardian",
            "Check the approval queue for off-voice assets.",
            tools=[("draven.assets_pending_review", {})])
        add("scheduler",
            "Report autopilot settings, quiet hours, caps, and the current weekly plan.",
            tools=[("draven.autopilot_status", {})])
        add("optimizer",
            "Recommend channel mix and what to double down on, citing evidence.",
            tools=[("draven.analytics_summary", {})],
            depends_on=level0)
        add("copywriter",
            "Draft campaign themes for the week in the brand voice.",
            tools=[("draven.business_summary", {})],
            depends_on=["brand_guardian"])
        add("channel_adapter",
            "Sketch the channel adaptations for the week's themes.",
            tools=[("draven.business_summary", {})],
            depends_on=["copywriter"])
        add("compliance",
            "List the guardrails that apply to this week's content.",
            tools=[("draven.approvals_pending", {})],
            depends_on=["copywriter"])
        add("asset_reviewer",
            "Summarize the review queue and propose approvals where safe.",
            tools=[("draven.approvals_pending", {})],
            depends_on=["compliance"])
    elif any(k in g for k in ("lead", "outreach", "follow", "prospect", "client")):
        add("outreach",
            "Check the lead pipeline: latest runs and their states.",
            tools=[("alpha.run_status", {})])
        add("onboarder",
            "Summarize business context for onboarding new clients.",
            tools=[("draven.business_summary", {})])
        add("analyst",
            "Report recent engagement performance for outreach timing.",
            tools=[("draven.analytics_summary", {})],
            depends_on=["outreach"])
    else:
        # Generic goal: fan out the read agents, then synthesize.
        add("analyst", "Business performance snapshot.",
            tools=[("draven.analytics_summary", {})])
        add("scheduler", "Campaign and autopilot status.",
            tools=[("draven.campaigns_status", {})])
        add("asset_reviewer", "Approval queue status.",
            tools=[("draven.approvals_pending", {})])
        add("researcher", "Top opportunities snapshot.",
            tools=[("market.top_opportunities", {"limit": 3})])

    # Supervisor always synthesizes last.
    deps = [s.agent_id for s in plan]
    add("supervisor",
        f"Synthesize the swarm's findings into one coherent answer to: {goal[:160]}",
        tools=[("draven.business_summary", {})],
        depends_on=deps)
    return plan


async def _llm_plan(
    provider: Any, provider_name: str, goal: str, context: dict[str, Any],
    configured: bool = True,
) -> list[Subtask] | None:
    """Ask the configured LLM to decompose the goal. Returns None when the
    provider is unconfigured (or the stub) or the plan is unusable
    (caller falls back)."""
    if not configured or provider_name == "stub":
        return None
    agent_cards = "\n".join(
        f"- {a.id}: {a.role} (tools: {', '.join(a.allowed_tools)})"
        for a in AGENTS.values()
    )
    prompt = (
        "You are the Supervisor of a 12-agent marketing-OS swarm. "
        "Decompose this goal into subtasks, one per agent (agents may be "
        "reused; keep it under 10 subtasks). Mark independent subtasks "
        'with "depends_on": [] and dependent ones with the agent ids they '
        "need first.\n\n"
        f"GOAL: {goal}\nCONTEXT: {json.dumps(context)[:800]}\n\n"
        f"AGENTS:\n{agent_cards}\n\n"
        'Reply with ONLY a JSON array of {"agent_id","task","depends_on"} '
        "objects. No markdown, no commentary."
    )
    try:
        from forge_llm import GenerationRequest

        gen = await asyncio.wait_for(
            provider.generate(GenerationRequest(
                prompt=prompt, max_tokens=1200, temperature=0.3)),
            timeout=30,
        )
        raw = gen.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        items = json.loads(raw)
        plan: list[Subtask] = []
        for it in items[:10]:
            aid = str(it.get("agent_id", ""))
            if aid not in AGENTS:
                continue
            deps = [d for d in (it.get("depends_on") or []) if d in AGENTS]
            plan.append(Subtask(
                agent_id=aid, task=str(it.get("task", ""))[:500],
                depends_on=deps,
            ))
        if not plan:
            return None
        # Supervisor synthesis always last.
        deps = [s.agent_id for s in plan]
        plan.append(Subtask(
            agent_id="supervisor",
            task=f"Synthesize the swarm's findings into one coherent answer to: {goal[:160]}",
            depends_on=deps,
        ))
        return plan
    except Exception:
        return None


def _levels(plan: list[Subtask]) -> list[list[Subtask]]:
    """Topological levels: independent subtasks share a level (fan-out),
    dependents run in later levels. Cycle-safe: leftovers go last."""
    remaining = list(plan)
    done: set[str] = set()
    levels: list[list[Subtask]] = []
    while remaining:
        ready = [s for s in remaining
                 if all(d in done for d in s.depends_on)]
        if not ready:
            ready = remaining  # break cycles: run leftovers together
        levels.append(ready)
        for s in ready:
            done.add(s.agent_id)
        remaining = [s for s in remaining if s not in ready]
    return levels


class Orchestrator:
    """Runs one swarm run. Owns the event feed and agent attribution."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        user: User,
        provider: Any,
        provider_name: str,
        run_id: uuid.UUID,
        provider_configured: bool = True,
    ) -> None:
        self._sessions = session_factory
        self._user = user
        self._provider = provider
        self._provider_name = provider_name
        self._provider_configured = provider_configured
        self._run_id = run_id
        self._seq = 0
        self._lock = asyncio.Lock()

    # -- events ---------------------------------------------------------
    async def emit(
        self,
        agent_id: str | None,
        kind: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        async with self._lock:
            self._seq += 1
            seq = self._seq
        db = self._sessions()
        try:
            db.add(DravenSwarmEvent(
                swarm_run_id=self._run_id,
                business_id=self._user.business_id,
                seq=seq,
                agent_id=agent_id,
                kind=kind,
                message=message[:2000],
                data=data or {},
            ))
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

    def _set_phase(self, db: Session, phase: str) -> None:
        run = db.query(DravenSwarmRun).filter(
            DravenSwarmRun.id == self._run_id).first()
        if run is not None:
            run.current_phase = phase
            db.commit()

    # -- agent execution ------------------------------------------------
    async def _run_agent(self, sub: Subtask) -> AgentResult:
        started = time.perf_counter()
        agent = AGENTS[sub.agent_id]
        await self.emit(agent.id, "agent_start",
                        f"{agent.name} — {sub.task[:140]}")
        tools_used: list[dict[str, Any]] = []
        approvals: list[dict[str, Any]] = []
        notes: list[str] = []
        degraded = False
        status = "ok"

        async def _call_tool(
            tool_id: str, tool_input: dict[str, Any]
        ) -> dict[str, Any]:
            """One attributed tool call with timeout + never-raise."""
            try:
                return await asyncio.wait_for(
                    _execute_attributed(
                        tool_id, db, self._user, tool_input,
                        agent.id, self._run_id,
                    ),
                    timeout=30,
                )
            except asyncio.TimeoutError:
                return {
                    "tool": tool_id, "risk": "low", "status": "error",
                    "error": "tool timed out after 30s",
                    "duration_ms": 30000,
                }
            except Exception as exc:
                return {
                    "tool": tool_id, "risk": "low", "status": "error",
                    "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    "duration_ms": 0,
                }

        def _record(result: dict[str, Any]) -> None:
            tool_id = result.get("tool", "?")
            tools_used.append({
                "tool": tool_id, "status": result.get("status"),
                "duration_ms": result.get("duration_ms", 0),
            })
            if result.get("status") == "approval_required":
                ap = result.get("approval", {})
                approvals.append({
                    "tool": tool_id,
                    "description": ap.get("description", ""),
                })
                notes.append(
                    f"approval needed: {ap.get('description', '')[:160]}")

        async def _emit_tool(result: dict[str, Any]) -> None:
            await self.emit(
                agent.id, "tool_call",
                f"{agent.name} ran {result.get('tool')} → "
                f"{result.get('status')}",
                {"tool": result.get("tool"),
                 "status": result.get("status")},
            )

        db = self._sessions()
        try:
            for tool_id, tool_input in sub.tools:
                if tool_id not in agent.allowed_tools:
                    notes.append(
                        f"skipped {tool_id}: not in {agent.name}'s toolset")
                    continue
                result = await _call_tool(tool_id, tool_input)
                if result.get("status") == "error":
                    notes.append(
                        f"{tool_id} error: {str(result.get('error'))[:160]}")
                _record(result)
                await _emit_tool(result)

            # Role behavior: the asset reviewer proposes approval for the
            # first queued asset. This is approval-gated by construction —
            # it records an approval request, never an approval.
            if agent.id == "asset_reviewer" and not any(
                t["tool"] == "draven.asset_approve" for t in tools_used
            ):
                pending = await _call_tool(
                    "draven.approvals_pending", {})
                _record(pending)
                await _emit_tool(pending)
                assets = ((pending.get("output") or {}).get("assets") or [])
                if pending.get("status") == "ok" and assets:
                    first = assets[0]
                    prop = await _call_tool(
                        "draven.asset_approve",
                        {"asset_id": first.get("id")},
                    )
                    _record(prop)
                    await _emit_tool(prop)
        finally:
            db.close()

        # Reasoning: real LLM when configured, honest degradation otherwise.
        summary_parts: list[str] = []
        if not self._provider_configured or self._provider_name == "stub":
            degraded = True
            status = "degraded"
            summary_parts.append(
                "LLM provider not configured: ran real tools, "
                "skipped generative reasoning — no output was invented. "
                "Connect a provider (e.g. Gemini) in Settings → AI provider "
                "for full reasoning.")
        else:
            try:
                from forge_llm import GenerationRequest

                slim = [
                    {"tool": t["tool"], "status": t["status"]}
                    for t in tools_used
                ]
                gen = await asyncio.wait_for(
                    self._provider.generate(GenerationRequest(
                        prompt=(
                            f"Task: {sub.task}\n"
                            f"Tool outcomes: {json.dumps(slim)}\n"
                            f"Notes: {'; '.join(notes)[:600] or 'none'}\n\n"
                            "Summarize what you found in 2-3 sentences, "
                            "grounded only in the tool outcomes above."
                        ),
                        system_prompt=agent.system_prompt,
                        max_tokens=300, temperature=0.3,
                    )),
                    timeout=45,
                )
                summary_parts.append(gen.text.strip())
            except Exception as exc:
                degraded = True
                status = "degraded"
                summary_parts.append(
                    f"Reasoning unavailable ({type(exc).__name__}); "
                    "tool results stand on their own.")
        if notes:
            summary_parts.append("Notes: " + "; ".join(notes)[:800])
        if not tools_used and not notes:
            summary_parts.append(
                f"{agent.name} completed its task with no tool calls needed.")

        duration_ms = int((time.perf_counter() - started) * 1000)
        await self.emit(
            agent.id, "agent_done",
            f"{agent.name} done ({status}, {duration_ms}ms)",
            {"status": status, "duration_ms": duration_ms,
             "tools": len(tools_used)},
        )
        return AgentResult(
            agent_id=agent.id, status=status,
            summary=" ".join(summary_parts)[:2000],
            tools_used=tools_used, approvals_needed=approvals,
            degraded=degraded, duration_ms=duration_ms,
        )

    # -- run ------------------------------------------------------------
    async def run(self, goal: str, context: dict[str, Any]) -> None:
        db = self._sessions()
        try:
            run = db.query(DravenSwarmRun).filter(
                DravenSwarmRun.id == self._run_id).first()
            if run is None:
                return
            run.status = DravenSwarmRunStatus.running
            run.started_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()

        await self.emit(None, "run_start", f"Swarm run started: {goal[:160]}",
                        {"goal": goal[:500]})

        plan = await _llm_plan(self._provider, self._provider_name,
                               goal, context,
                               configured=self._provider_configured)
        if plan is None:
            plan = _heuristic_plan(goal)
            await self.emit(None, "note",
                            "Using deterministic task plan (provider "
                            "unconfigured or LLM planning unavailable) — "
                            "agents run real tools; generative steps degrade "
                            "honestly.")

        results: dict[str, dict[str, Any]] = {}
        failed = False
        try:
            for level in _levels(plan):
                db2 = self._sessions()
                try:
                    self._set_phase(
                        db2, f"level: {', '.join(s.agent_id for s in level)}")
                finally:
                    db2.close()
                outcomes = await asyncio.gather(
                    *(asyncio.wait_for(self._run_agent(s), AGENT_TIMEOUT_S)
                      for s in level),
                    return_exceptions=True,
                )
                for sub, outcome in zip(level, outcomes):
                    if isinstance(outcome, BaseException):
                        results[sub.agent_id] = {
                            "agent_id": sub.agent_id, "status": "error",
                            "summary": (
                                f"{AGENTS[sub.agent_id].name} failed: "
                                f"{type(outcome).__name__}"),
                            "tools_used": [], "approvals_needed": [],
                            "degraded": False, "duration_ms": 0,
                        }
                        await self.emit(
                            sub.agent_id, "error",
                            f"{AGENTS[sub.agent_id].name} failed: "
                            f"{type(outcome).__name__}")
                    else:
                        results[sub.agent_id] = {
                            "agent_id": outcome.agent_id,
                            "status": outcome.status,
                            "summary": outcome.summary,
                            "tools_used": outcome.tools_used,
                            "approvals_needed": outcome.approvals_needed,
                            "degraded": outcome.degraded,
                            "duration_ms": outcome.duration_ms,
                        }
        except Exception as exc:  # never leave a run hanging
            failed = True
            await self.emit(None, "error",
                            f"Run failed: {type(exc).__name__}: "
                            f"{str(exc)[:300]}")

        db = self._sessions()
        try:
            run = db.query(DravenSwarmRun).filter(
                DravenSwarmRun.id == self._run_id).first()
            if run is not None:
                run.agent_results = results
                summary = _synthesize(goal, results)
                run.result_summary = summary
                run.completed_at = datetime.now(timezone.utc)
                if failed:
                    run.status = DravenSwarmRunStatus.failed
                    run.error = "orchestrator exception (see events)"
                else:
                    run.status = DravenSwarmRunStatus.completed
                db.commit()
        finally:
            db.close()

        await self.emit(None, "run_done",
                        "Swarm run completed." if not failed
                        else "Swarm run failed.",
                        {"status": "failed" if failed else "completed",
                         "agents": len(results)})


async def _execute_attributed(
    tool_id: str,
    db: Session,
    user: User,
    raw_input: dict[str, Any],
    agent_id: str,
    run_id: uuid.UUID,
) -> dict[str, Any]:
    """execute_tool + swarm attribution (best-effort, never raises)."""
    result = await execute_tool(
        tool_id, db, user, raw_input,
        agent_id=agent_id, swarm_run_id=run_id,
    )
    return result


def _synthesize(goal: str, results: dict[str, dict[str, Any]]) -> str:
    """Deterministic synthesis from real agent results (no LLM needed)."""
    parts = [f"Swarm result for: {goal[:200]}"]
    order = ["supervisor", "researcher", "analyst", "optimizer",
             "brand_guardian", "copywriter", "channel_adapter", "scheduler",
             "compliance", "asset_reviewer", "outreach", "onboarder"]
    for aid in order:
        r = results.get(aid)
        if not r:
            continue
        name = AGENTS[aid].name
        flag = ""
        if r.get("degraded"):
            flag = " [degraded: stub provider, tools only]"
        elif r.get("status") == "error":
            flag = " [error]"
        parts.append(f"{name}{flag}: {r.get('summary', '')[:400]}")
    for aid, r in results.items():
        if aid not in order:
            parts.append(f"{aid}: {r.get('summary', '')[:400]}")
    return "\n".join(parts)[:6000]


async def run_swarm(
    session_factory: Callable[[], Session],
    user: User,
    provider: Any,
    provider_name: str,
    run_id: uuid.UUID,
    goal: str,
    context: dict[str, Any] | None = None,
    provider_configured: bool = True,
) -> None:
    """Entry point: execute one swarm run to completion (or honest failure)."""
    orch = Orchestrator(session_factory, user, provider, provider_name, run_id,
                        provider_configured=provider_configured)
    try:
        await asyncio.wait_for(orch.run(goal, context or {}), RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        await orch.emit(None, "error",
                        f"Run exceeded the {RUN_TIMEOUT_S:g}s ceiling — "
                        "marked failed rather than hanging.")
        db = session_factory()
        try:
            run = db.query(DravenSwarmRun).filter(
                DravenSwarmRun.id == run_id).first()
            if run is not None:
                run.status = DravenSwarmRunStatus.failed
                run.error = "run timeout"
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        await orch.emit(None, "error",
                        f"Run crashed: {type(exc).__name__}: {str(exc)[:300]}")
        db = session_factory()
        try:
            run = db.query(DravenSwarmRun).filter(
                DravenSwarmRun.id == run_id).first()
            if run is not None:
                run.status = DravenSwarmRunStatus.failed
                run.error = f"{type(exc).__name__}: {str(exc)[:300]}"
                run.completed_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()


def demo_goal() -> tuple[str, dict[str, Any]]:
    """The one-click investor demo scenario."""
    return DEMO_GOAL, dict(DEMO_CONTEXT)
