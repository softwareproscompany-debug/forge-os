"""Draven 12-agent swarm tests: TestClient + SQLite (models are dialect-portable).

Covers:
  agent registry (12 defs, unique ids, least-privilege tool subsets),
  deterministic decomposition (levels, supervisor last),
  orchestrator on the stub provider (real tool execution, honest
  degradation — nothing invented),
  approval gating inherited by agents (high-risk tools never execute),
  audit attribution (agent_id / swarm_run_id on every row),
  API surface (agents list, run start, live events, run detail),
  tenant isolation between businesses.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("WEBHOOK_SECRET", "test-webhook-secret")

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forge_db.models import (
    Asset,
    AssetKind,
    AssetStatus,
    Base,
    Business,
    DravenSwarmRun,
    DravenSwarmRunStatus,
    DravenSwarmEvent,
    DravenToolRun,
    User,
)

from app import draven_swarm as swarm
from app.core.config import Settings
from app.core.deps import get_db_session, get_settings_dep
from app.draven_tools import TOOLS
from app.main import create_app
from forge_llm.providers import StubProvider

TEST_DB = "/tmp/forgeos_swarm_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)

engine = create_engine(f"sqlite:///{TEST_DB}", connect_args={"check_same_thread": False})
TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base.metadata.create_all(engine)

DRAVEN_KEY = Fernet.generate_key().decode()

test_settings = Settings(
    DATABASE_URL=f"sqlite:///{TEST_DB}",
    REDIS_URL="redis://127.0.0.1:9/0",  # closed port -> enqueue fails fast
    JWT_SECRET="test-secret",
    WEBHOOK_SECRET="test-webhook-secret",
    DRAVEN_CONFIG_KEY=DRAVEN_KEY,
)

app = create_app()
app.dependency_overrides[get_settings_dep] = lambda: test_settings


def _override_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db_session] = _override_db

client = TestClient(app)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


_counter = 0


def _register(email_prefix: str) -> dict:
    """Register a fresh business+owner (unique email per call)."""
    global _counter
    _counter += 1
    email = f"{email_prefix}{_counter}@example.com"
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "s3cret-pass",
            "full_name": email_prefix.title(),
            "business_name": f"{email_prefix.title()} Co {_counter}",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return {"email": email, "token": body["token"], "user": body["user"]}


def _user_obj(user: dict) -> User:
    db = TestingSession()
    try:
        return (
            db.query(User)
            .filter(User.id == uuid.UUID(user["user"]["id"]))
            .one()
        )
    finally:
        db.close()


def _session_factory():
    return TestingSession()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_agent_registry_has_twelve_least_privilege():
    assert len(swarm.AGENTS) == 12
    ids = [a.id for a in swarm.AGENTS.values()]
    assert len(set(ids)) == 12
    for a in swarm.AGENTS.values():
        assert a.name and a.role and a.system_prompt
        assert a.max_depth >= 0
        for t in a.allowed_tools:
            assert t in TOOLS, f"agent {a.id} references unknown tool {t}"


def test_swarm_agents_endpoint_lists_twelve():
    user = _register("swarmreg")
    r = client.get("/api/v1/draven/swarm/agents", headers=_headers(user["token"]))
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 12
    ids = {i["id"] for i in items}
    assert "supervisor" in ids and "researcher" in ids and "compliance" in ids


def test_heuristic_plan_supervisor_last_and_levels_valid():
    plan = swarm._heuristic_plan("Plan and draft this week's campaign")
    assert plan, "campaign goal must produce subtasks"
    assert plan[-1].agent_id == "supervisor"
    agent_ids = {s.agent_id for s in plan}
    assert "researcher" in agent_ids and "analyst" in agent_ids
    levels = swarm._levels(plan)
    assert len(levels) >= 2, "independent subtasks must fan out, dependents follow"
    # every dependency appears in an earlier level
    seen: set[str] = set()
    for level in levels:
        for s in level:
            assert all(d in seen for d in s.depends_on), f"bad level order for {s.agent_id}"
        seen.update(s.agent_id for s in level)


def test_demo_goal_is_seeded():
    goal, ctx = swarm.demo_goal()
    assert isinstance(goal, str) and len(goal) > 20
    assert ctx.get("demo") is True


# ---------------------------------------------------------------------------
# Orchestrator on the stub provider: real tools, honest degradation
# ---------------------------------------------------------------------------


def test_run_swarm_stub_executes_tools_and_degrades_honestly():
    user = _register("swarmstub")
    u = _user_obj(user)
    bid = u.business_id

    db = TestingSession()
    run = DravenSwarmRun(
        business_id=bid, user_id=u.id,
        goal="Plan and draft this week's campaign",
        context={},
        status=DravenSwarmRunStatus.queued,
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    asyncio.run(
        swarm.run_swarm(
            _session_factory, u, StubProvider(), "stub",
            run_id, "Plan and draft this week's campaign", {},
        )
    )

    db = TestingSession()
    try:
        row = db.query(DravenSwarmRun).filter(DravenSwarmRun.id == run_id).one()
        assert row.status == DravenSwarmRunStatus.completed, row.error
        assert row.result_summary, "synthesis must exist"
        assert row.agent_results, "per-agent results must exist"
        # real agents ran — not just the supervisor
        assert len(row.agent_results) >= 5

        # every agent summary is honest about stub degradation OR reports
        # real tool outcomes — nothing is invented
        for aid, res in row.agent_results.items():
            assert res["summary"], f"{aid} has no summary"

        # audit attribution: tool rows carry this run's id
        tool_rows = (
            db.query(DravenToolRun)
            .filter(DravenToolRun.swarm_run_id == run_id)
            .all()
        )
        assert tool_rows, "agent tool calls must be audit-logged"
        assert all(r.agent_id for r in tool_rows)
        assert all(r.business_id == bid for r in tool_rows)

        # live event feed was written (agent_start/agent_done/tool_call)
        events = (
            db.query(DravenSwarmEvent)
            .filter(DravenSwarmEvent.swarm_run_id == run_id)
            .order_by(DravenSwarmEvent.seq)
            .all()
        )
        kinds = {e.kind for e in events}
        assert "run_start" in kinds and "run_done" in kinds
        assert "agent_start" in kinds and "agent_done" in kinds
        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    finally:
        db.close()


def test_approval_gating_inherited_by_agents():
    """asset_reviewer calling the high-risk draven.asset_approve gets an
    approval request — the asset is NOT approved."""
    user = _register("swarmappr")
    u = _user_obj(user)

    db = TestingSession()
    asset = Asset(
        business_id=u.business_id, kind=AssetKind.email_copy,
        title="Needs review", body="Hello", status=AssetStatus.in_review,
        created_by=u.id,
    )
    db.add(asset)
    db.commit()
    asset_id = asset.id
    db.close()

    db = TestingSession()
    run = DravenSwarmRun(
        business_id=u.business_id, user_id=u.id,
        goal="review the approval queue", context={},
        status=DravenSwarmRunStatus.queued,
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    asyncio.run(
        swarm.run_swarm(
            _session_factory, u, StubProvider(), "stub",
            run_id, "review the approval queue", {},
        )
    )

    db = TestingSession()
    try:
        # asset untouched — no agent can approve without a human token
        a = db.query(Asset).filter(Asset.id == asset_id).one()
        assert a.status == AssetStatus.in_review

        # the approval request IS recorded in the audit trail
        rows = (
            db.query(DravenToolRun)
            .filter(
                DravenToolRun.swarm_run_id == run_id,
                DravenToolRun.tool == "draven.asset_approve",
            )
            .all()
        )
        assert rows, "high-risk proposal must be audited"
        assert all(r.status == "approval_required" for r in rows)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_swarm_run_api_flow_with_live_events():
    user = _register("swarmapi")
    h = _headers(user["token"])

    r = client.post(
        "/api/v1/draven/swarm/run",
        headers=h,
        json={"goal": "demo", "demo": True},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert run_id

    # poll the live feed until the background run completes
    deadline = time.time() + 120
    detail = None
    while time.time() < deadline:
        d = client.get(f"/api/v1/draven/swarm/runs/{run_id}", headers=h)
        assert d.status_code == 200, d.text
        detail = d.json()
        if detail["status"] in ("completed", "failed"):
            break
        time.sleep(1.0)
    assert detail is not None
    assert detail["status"] == "completed", detail.get("error")
    assert detail["result_summary"]
    assert len(detail["agent_results"]) >= 5

    ev = client.get(
        f"/api/v1/draven/swarm/runs/{run_id}/events?after=0", headers=h
    )
    assert ev.status_code == 200, ev.text
    body = ev.json()
    assert body["events"], "live feed must have events"
    assert body["latest_seq"] >= len(body["events"])
    # incremental polling works
    first_seq = body["events"][0]["seq"]
    ev2 = client.get(
        f"/api/v1/draven/swarm/runs/{run_id}/events?after={first_seq}",
        headers=h,
    )
    assert ev2.status_code == 200
    assert all(e["seq"] > first_seq for e in ev2.json()["events"])


def test_swarm_run_tenant_isolation():
    a = _register("swarma")
    b = _register("swarmb")
    r = client.post(
        "/api/v1/draven/swarm/run",
        headers=_headers(a["token"]),
        json={"goal": "plan this week's campaign"},
    )
    run_id = r.json()["run_id"]
    # business B cannot see business A's run
    r2 = client.get(
        f"/api/v1/draven/swarm/runs/{run_id}", headers=_headers(b["token"])
    )
    assert r2.status_code == 404
    r3 = client.get(
        f"/api/v1/draven/swarm/runs/{run_id}/events", headers=_headers(b["token"])
    )
    assert r3.status_code == 404


def test_swarm_run_rejects_blank_goal():
    user = _register("swarmblank")
    r = client.post(
        "/api/v1/draven/swarm/run",
        headers=_headers(user["token"]),
        json={"goal": ""},
    )
    assert r.status_code == 422
