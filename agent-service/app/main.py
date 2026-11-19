"""
agent-service – main FastAPI application
"""
import logging
import os
import time
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .database import AsyncSessionLocal, get_db, init_db
from .models import Agent, Team, TeamMember
from .schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    CostInfo,
    DashboardOverview,
    RunRequest,
    RunResponse,
    TeamCreate,
    TeamMemberCreate,
    TeamMemberOut,
    TeamOut,
    TeamUpdate,
    UsageInfo,
)

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | agent-service | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

USAGE_SERVICE_URL = os.environ.get("USAGE_SERVICE_URL", "http://usage-service:8000")
COST_SERVICE_URL = os.environ.get("COST_SERVICE_URL", "http://cost-service:8000")

DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

# Shared httpx client
_http_client: Optional[httpx.AsyncClient] = None

ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CORS_ORIGINS",
        "http://localhost:8001,http://127.0.0.1:8001,http://localhost:8002,http://localhost:8003",
    ).split(",")
    if o.strip()
]

# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="agent-service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    request_id = request.headers.get("x-request-id", "-")
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "request | request_id=%s method=%s path=%s status=%d duration_ms=%.1f",
        request_id, request.method, request.url.path, response.status_code, duration_ms,
    )
    return response


SEED_TEAMS = [
    {"name": "Research", "description": "Literature search and paper synthesis", "daily_token_budget": 100_000},
    {"name": "Engineering", "description": "Software agents and load test pipelines", "daily_token_budget": 1_000_000},
    {"name": "Support", "description": "Customer ticket and FAQ automation", "daily_token_budget": 100_000},
    {"name": "Operations", "description": "Document and meeting summarization", "daily_token_budget": 150_000},
    {"name": "Data", "description": "Dataset analytics and reporting", "daily_token_budget": 150_000},
    {"name": "Legal", "description": "Contract review and compliance checks", "daily_token_budget": 100_000},
    {"name": "default", "description": "Default organizational team", "daily_token_budget": 500_000},
]

SEED_MEMBERS = {
    "Research": [("Research Lead", "research@company.local", "admin")],
    "Engineering": [
        ("Krish", "krish@company.local", "admin"),
        ("Platform Engineer", "eng@company.local", "developer"),
    ],
    "Support": [("Support Ops", "support@company.local", "developer")],
    "Operations": [("Ops Analyst", "ops@company.local", "developer")],
    "Data": [("Data Scientist", "data@company.local", "developer")],
    "Legal": [("Compliance Reviewer", "legal@company.local", "viewer")],
}


async def _ensure_team_exists(db: AsyncSession, team_name: str) -> None:
    if not team_name:
        return
    stmt = pg_insert(Team).values(name=team_name, daily_token_budget=500_000).on_conflict_do_nothing(index_elements=["name"])
    await db.execute(stmt)


async def _seed_teams_and_members(db: AsyncSession) -> None:
    await db.execute(pg_insert(Team).values(SEED_TEAMS).on_conflict_do_nothing(index_elements=["name"]))
    agent_teams = (await db.execute(select(Agent.owner_team).distinct())).scalars().all()
    for tname in agent_teams:
        await _ensure_team_exists(db, tname)
    member_count = (await db.execute(select(func.count(TeamMember.id)))).scalar_one()
    if member_count == 0:
        teams = {t.name: t.id for t in (await db.execute(select(Team))).scalars().all()}
        for tname, members in SEED_MEMBERS.items():
            tid = teams.get(tname)
            if tid:
                for mname, email, role in members:
                    db.add(TeamMember(team_id=tid, name=mname, email=email, role=role))
    await db.commit()


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(timeout=10.0)
    await init_db()
    async with AsyncSessionLocal() as session:
        await _seed_teams_and_members(session)
    logger.info("agent-service startup complete")


@app.on_event("shutdown")
async def shutdown():
    if _http_client:
        await _http_client.aclose()


def get_http_client() -> httpx.AsyncClient:
    return _http_client


# ── /agents CRUD ──────────────────────────────────────────────────────────────

@app.post("/agents", response_model=AgentOut, status_code=201)
async def create_agent(body: AgentCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(Agent).where(Agent.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Agent '{body.name}' already exists")
    await _ensure_team_exists(db, body.owner_team)
    agent = Agent(**body.model_dump())
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    logger.info("created agent id=%d name=%s", agent.id, agent.name)
    return agent


@app.get("/agents", response_model=List[AgentOut])
async def list_agents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Agent).order_by(Agent.id))
    return result.scalars().all()


@app.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return agent


@app.put("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: int, body: AgentUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    updates = body.model_dump(exclude_none=True)
    if "owner_team" in updates:
        await _ensure_team_exists(db, updates["owner_team"])
    for field, value in updates.items():
        setattr(agent, field, value)
    await db.commit()
    await db.refresh(agent)
    return agent


@app.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(agent_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    await db.delete(agent)
    await db.commit()


# ── /teams CRUD ───────────────────────────────────────────────────────────────

def _serialize_team(team: Team, agent_counts: dict) -> TeamOut:
    members = sorted(team.members, key=lambda m: m.id)
    return TeamOut(
        id=team.id,
        name=team.name,
        description=team.description,
        daily_token_budget=team.daily_token_budget,
        created_at=team.created_at,
        members=[TeamMemberOut.model_validate(m) for m in members],
        member_count=len(members),
        agent_count=agent_counts.get(team.name, 0),
    )


@app.get("/teams", response_model=List[TeamOut])
async def list_teams(db: AsyncSession = Depends(get_db)):
    teams = (await db.execute(select(Team).options(selectinload(Team.members)).order_by(Team.name))).scalars().all()
    counts_rows = (await db.execute(select(Agent.owner_team, func.count(Agent.id)).group_by(Agent.owner_team))).all()
    agent_counts = {row[0]: row[1] for row in counts_rows}
    return [_serialize_team(t, agent_counts) for t in teams]


@app.post("/teams", response_model=TeamOut, status_code=201)
async def create_team(body: TeamCreate, db: AsyncSession = Depends(get_db)):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Team name is required")
    existing = await db.execute(select(Team).where(Team.name == name))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Team '{name}' already exists")
    team = Team(name=name, description=body.description, daily_token_budget=body.daily_token_budget)
    db.add(team)
    await db.commit()
    result = await db.execute(select(Team).options(selectinload(Team.members)).where(Team.id == team.id))
    return _serialize_team(result.scalar_one(), {})


@app.put("/teams/{team_id}", response_model=TeamOut)
async def update_team(team_id: int, body: TeamUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Team).options(selectinload(Team.members)).where(Team.id == team_id))
    team = result.scalar_one_or_none()
    if not team:
        raise HTTPException(404, f"Team {team_id} not found")
    old_name = team.name
    updates = body.model_dump(exclude_none=True)
    if "name" in updates and updates["name"] != old_name:
        dup = (await db.execute(select(Team).where(Team.name == updates["name"]))).scalar_one_or_none()
        if dup:
            raise HTTPException(409, f"Team '{updates['name']}' already exists")
    for field, value in updates.items():
        setattr(team, field, value)
    if "name" in updates and updates["name"] != old_name:
        agents_res = (await db.execute(select(Agent).where(Agent.owner_team == old_name))).scalars().all()
        for ag in agents_res:
            ag.owner_team = updates["name"]
    await db.commit()
    refreshed = (await db.execute(select(Team).options(selectinload(Team.members)).where(Team.id == team_id))).scalar_one()
    ac = (await db.execute(select(func.count(Agent.id)).where(Agent.owner_team == refreshed.name))).scalar_one()
    return _serialize_team(refreshed, {refreshed.name: ac})


@app.delete("/teams/{team_id}", status_code=204)
async def delete_team(team_id: int, db: AsyncSession = Depends(get_db)):
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if not team:
        raise HTTPException(404, f"Team {team_id} not found")
    await db.delete(team)
    await db.commit()


@app.post("/teams/{team_id}/members", response_model=TeamMemberOut, status_code=201)
async def add_team_member(team_id: int, body: TeamMemberCreate, db: AsyncSession = Depends(get_db)):
    team = (await db.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if not team:
        raise HTTPException(404, f"Team {team_id} not found")
    if not body.name.strip():
        raise HTTPException(400, "Member name is required")
    member = TeamMember(team_id=team_id, name=body.name.strip(), email=body.email, role=body.role)
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


@app.delete("/teams/{team_id}/members/{member_id}", status_code=204)
async def delete_team_member(team_id: int, member_id: int, db: AsyncSession = Depends(get_db)):
    member = (
        await db.execute(select(TeamMember).where(TeamMember.id == member_id, TeamMember.team_id == team_id))
    ).scalar_one_or_none()
    if not member:
        raise HTTPException(404, f"Member {member_id} not found in team {team_id}")
    await db.delete(member)
    await db.commit()


# ── /agents/{id}/run ──────────────────────────────────────────────────────────

@app.post("/agents/{agent_id}/run", response_model=RunResponse)
async def run_agent(
    agent_id: int,
    body: RunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    t_total_start = time.perf_counter()

    # Lookup agent
    result = await db.execute(select(Agent).where(Agent.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    if agent.status == "INACTIVE":
        raise HTTPException(409, f"Agent '{agent.name}' is INACTIVE")

    # Enforce hard budget pre-flight when configured
    if getattr(agent, "budget_mode", "soft") == "hard":
        try:
            cr = await http_client.get(f"{COST_SERVICE_URL}/analytics/agents", timeout=3.0)
            if cr.status_code == 200:
                match = next((x for x in cr.json() if x.get("agent_id") == agent.id), None)
                tokens_today = (match or {}).get("tokens_today", 0)
                if tokens_today >= agent.daily_token_budget:
                    raise HTTPException(
                        429,
                        f"Hard daily budget cap reached for '{agent.name}' "
                        f"({tokens_today:,}/{agent.daily_token_budget:,} tokens today).",
                    )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("hard-budget check skipped (cost-service unreachable): %s", exc)

    # Generate request_id and propagate via header
    request_id = str(uuid.uuid4()).replace("-", "")
    headers = {"x-request-id": request_id, "Content-Type": "application/json"}

    logger.info(
        "run | request_id=%s agent_id=%d agent=%s model=%s source=%s",
        request_id, agent_id, agent.name, agent.model, body.source,
    )

    t_usage_start = time.perf_counter()

    # Call usage-service
    try:
        usage_resp = await http_client.post(
            f"{USAGE_SERVICE_URL}/usage/record",
            json={
                "agent_id": agent.id,
                "agent_name": agent.name,
                "model": agent.model,
                "daily_token_budget": agent.daily_token_budget,
                "prompt": body.prompt or "",
                "source": body.source,
                "simulate_failures": body.simulate_failures,
                "executed_by": body.executed_by,
            },
            headers=headers,
        )
        usage_chain_ms = (time.perf_counter() - t_usage_start) * 1000

        logger.info(
            "usage-call | request_id=%s status=%d duration_ms=%.1f target=usage-service",
            request_id, usage_resp.status_code, usage_chain_ms,
        )

        if usage_resp.status_code not in (200, 201):
            logger.error(
                "usage-service error status=%d body=%s request_id=%s",
                usage_resp.status_code, usage_resp.text[:200], request_id,
            )
            raise HTTPException(
                502,
                f"usage-service returned HTTP {usage_resp.status_code}",
            )

        ud = usage_resp.json()

    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(503, "usage-service timed out — downstream not reachable")
    except httpx.ConnectError:
        raise HTTPException(503, "usage-service connection refused — service may be down")
    except Exception as exc:
        logger.error("usage-service call failed: %s (request_id=%s)", exc, request_id)
        raise HTTPException(502, "usage-service unavailable")

    total_ms = (time.perf_counter() - t_total_start) * 1000

    usage_info = UsageInfo(
        usage_id=ud.get("usage_id"),
        input_tokens=ud.get("input_tokens", 0),
        output_tokens=ud.get("output_tokens", 0),
        total_tokens=ud.get("total_tokens", 0),
        latency_ms=ud.get("latency_ms", 0),
        status=ud.get("status", "success"),
        error_message=ud.get("error_message"),
        executed_by=ud.get("executed_by") or body.executed_by,
        prompt_preview=ud.get("prompt_preview"),
        response_preview=ud.get("response_preview"),
    )

    cost_info = CostInfo(
        cost_id=ud.get("cost_id"),
        cost_usd=str(ud.get("cost_usd", "0")),
        tokens_today=ud.get("tokens_today"),
        budget_used_pct=str(ud.get("budget_used_pct", "0")),
        alert=ud.get("alert"),
    )

    return RunResponse(
        request_id=request_id,
        trace=["agent-service", "usage-service", "cost-service"],
        agent=AgentOut.model_validate(agent),
        usage=usage_info,
        cost=cost_info,
        timings_ms={
            "total": round(total_ms, 1),
            "usage_chain": round(usage_chain_ms, 1),
        },
    )


# ── /dashboard/overview ───────────────────────────────────────────────────────

@app.get("/dashboard/overview")
async def dashboard_overview(
    source: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    # Agents from own DB
    agents_result = await db.execute(select(Agent).order_by(Agent.id))
    agents = agents_result.scalars().all()

    params = {"source": source} if source and source != "all" else {}
    cost_overview = {"total_requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "total_cost_usd": "0"}
    cost_models = []
    cost_agents = []
    alerts = []
    usage_summary = {}

    async def _get_json(url: str, default, query=None):
        try:
            r = await http_client.get(url, params=query, timeout=5.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return default

    cost_overview = await _get_json(f"{COST_SERVICE_URL}/analytics/overview", cost_overview, params)
    cost_models = await _get_json(f"{COST_SERVICE_URL}/analytics/models", [], params)
    cost_agents = await _get_json(f"{COST_SERVICE_URL}/analytics/agents", [], params)
    alerts = await _get_json(f"{COST_SERVICE_URL}/alerts?limit=20", [])
    usage_summary = await _get_json(f"{USAGE_SERVICE_URL}/usage/summary", {}, params)

    return {
        "agents": [AgentOut.model_validate(a).model_dump() for a in agents],
        "total_requests": cost_overview.get("total_requests", 0),
        "input_tokens": cost_overview.get("input_tokens", 0),
        "output_tokens": cost_overview.get("output_tokens", 0),
        "total_tokens": cost_overview.get("total_tokens", 0),
        "total_cost_usd": str(cost_overview.get("total_cost_usd", "0")),
        "failed_model_calls": usage_summary.get("failed_count", 0),
        "cost_per_model": cost_models,
        "tokens_per_agent": cost_agents,
        "recent_alerts": alerts,
    }


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)


# ── /health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agent-service"}


@app.get("/health/deps")
async def health_deps(
    db: AsyncSession = Depends(get_db),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    try:
        await db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        logger.warning("health/deps db error: %s", e)
        db_status = "error: unavailable"

    try:
        r = await http_client.get(f"{USAGE_SERVICE_URL}/health", timeout=3.0)
        usage_status = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        logger.warning("health/deps usage-service error: %s", e)
        usage_status = "error: unavailable"

    try:
        r = await http_client.get(f"{COST_SERVICE_URL}/health", timeout=3.0)
        cost_status = "ok" if r.status_code == 200 else f"http {r.status_code}"
    except Exception as e:
        logger.warning("health/deps cost-service error: %s", e)
        cost_status = "error: unavailable"

    return {
        "service": "agent-service",
        "database": db_status,
        "usage-service": usage_status,
        "cost-service": cost_status,
    }
