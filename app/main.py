"""FIB compressor service.

Edge-router hardware migration helper: squeeze the current IPv4 forwarding
table into a new device's entry budget.  Given up to 128 routes with
distinct prefixes, a device capacity, and a next-hop to drill-fail, the
service returns the smallest equivalent table (new prefixes may be used,
actions are reused from the original table).  Equivalence means identical
longest-prefix-match results for every destination address — with "drop"
and "no-match" kept distinct — both before and after withdrawing the
failed next-hop from old and new tables.  When the minimum does not fit
the capacity, only the minimum required entry count is reported.
"""

from __future__ import annotations

from ipaddress import IPv4Network, ip_network
from typing import List, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .compressor import DROP, compress, verify

APP_VERSION = "1.0.0"
MAX_ROUTES = 128

# ---------------------------------------------------------------------------
# demo payloads (shown in /docs)
# ---------------------------------------------------------------------------

DEMO_ROUTES = [
    {"prefix": "10.0.0.0/8", "action": "NH1"},
    {"prefix": "10.1.0.0/16", "action": "NH2"},
    {"prefix": "10.2.0.0/16", "action": "NH1"},
    {"prefix": "10.3.0.0/16", "action": "drop"},
    {"prefix": "192.168.0.0/16", "action": "NH2"},
]

EXAMPLE_CAPACITY_OK = {
    "summary": "容量足够：5 条压到 4 条，故障切换保持等价",
    "description": (
        "10.2.0.0/16 与默认的 10.0.0.0/8 同为 NH1，被合并吸收；"
        "撤掉 NH2 后 10.1.0.0/16 回落到 NH1、192.168.0.0/16 变为未匹配，"
        "新旧表两种场景下逐地址一致。最少需要 4 条，容量 4 刚好装下。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 4,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_CAPACITY_EXCEEDED = {
    "summary": "容量不够：只回报最低所需条数",
    "description": (
        "同一张表演练失效 NH2，但新设备只能装 3 条。等效表最少需要 4 条，"
        "超过容量，因此只返回 min_entries=4，不下发转发表。"
    ),
    "value": {
        "routes": DEMO_ROUTES,
        "capacity": 3,
        "failed_nexthop": "NH2",
    },
}

EXAMPLE_FAILOVER_LADDER = {
    "summary": "故障回落链：被撤下一跳后回落到更短前缀",
    "description": (
        "10.0.0.0/8→NH1、10.1.0.0/16→NH2、10.1.1.0/24→NH3，演练失效 NH2。"
        "撤掉 NH2 后 10.1.0.0/16 内（除 10.1.1.0/24）须回落到 NH1。"
        "压缩结果保持正常与故障两种场景逐地址等价。"
    ),
    "value": {
        "routes": [
            {"prefix": "10.0.0.0/8", "action": "NH1"},
            {"prefix": "10.1.0.0/16", "action": "NH2"},
            {"prefix": "10.1.1.0/24", "action": "NH3"},
        ],
        "capacity": 8,
        "failed_nexthop": "NH2",
    },
}


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

class Route(BaseModel):
    """One forwarding entry: a CIDR prefix and its action.

    ``action`` is a next-hop name, or the reserved word ``drop`` to discard
    matching traffic.  Addresses that match no entry are "no-match", which
    is distinct from ``drop``.
    """

    prefix: str = Field(examples=["10.0.0.0/8"])
    action: str = Field(min_length=1, max_length=64, examples=["NH1"])

    @field_validator("prefix")
    @classmethod
    def _check_prefix(cls, v: str) -> str:
        try:
            net = ip_network(v.strip(), strict=False)
        except ValueError as exc:
            raise ValueError(f"invalid IPv4 CIDR prefix: {v!r}") from exc
        if not isinstance(net, IPv4Network):
            raise ValueError(f"not an IPv4 prefix: {v!r}")
        return str(net)

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        a = v.strip()
        if not a:
            raise ValueError("action must be a non-empty next-hop name or 'drop'")
        return a


class CompressRequest(BaseModel):
    routes: List[Route] = Field(
        max_length=MAX_ROUTES,
        description=f"Up to {MAX_ROUTES} routes with distinct prefixes.",
    )
    capacity: int = Field(ge=0, description="Entry budget of the new device.")
    failed_nexthop: str = Field(
        min_length=1,
        max_length=64,
        description="Next-hop withdrawn in the failure drill.",
    )

    @field_validator("failed_nexthop")
    @classmethod
    def _check_failed(cls, v: str) -> str:
        f = v.strip()
        if not f:
            raise ValueError("failed_nexthop must be non-empty")
        return f

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        prefixes = [r.prefix for r in self.routes]
        if len(set(prefixes)) != len(prefixes):
            raise ValueError("route prefixes must be distinct")
        return self


class CompressResponse(BaseModel):
    status: str = Field(description="'ok' or 'capacity_exceeded'.")
    fits: bool = Field(description="Whether the minimal table fits the capacity.")
    min_entries: int = Field(description="Minimal number of entries of any equivalent table.")
    capacity: int
    failed_nexthop: str
    table: Optional[List[Route]] = Field(
        description="The minimal equivalent table; null when over capacity."
    )
    verified: Optional[bool] = Field(
        description=(
            "Server-side re-check that old and new tables agree on every "
            "address in both the normal and the failure scenario."
        )
    )


class VerifyRequest(BaseModel):
    old_routes: List[Route] = Field(max_length=4096)
    new_routes: List[Route] = Field(max_length=4096)
    failed_nexthop: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _distinct_prefixes(self):
        for name, rs in (("old_routes", self.old_routes), ("new_routes", self.new_routes)):
            prefixes = [r.prefix for r in rs]
            if len(set(prefixes)) != len(prefixes):
                raise ValueError(f"{name}: prefixes must be distinct")
        return self


class VerifyResponse(BaseModel):
    equivalent: bool
    normal_equivalent: bool
    failover_equivalent: bool
    checked_points: int = Field(
        description="Number of elementary address intervals compared."
    )
    first_mismatch: Optional[dict] = None


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FIB Compressor",
    version=APP_VERSION,
    description=(
        "Compress an IPv4 forwarding table for a smaller-capacity edge router. "
        "The compressed table is entry-minimal and equivalent under "
        "longest-prefix matching — distinguishing drop from no-match — both "
        "before and after a next-hop failure drill. "
        "Try the ready-made examples below: one where the capacity suffices "
        "and one where only the minimum required count is reported."
    ),
)


@app.get("/", summary="Service info")
def root():
    return {
        "service": "fib-compressor",
        "version": APP_VERSION,
        "endpoints": {
            "POST /compress": "minimal equivalent table (or minimum count when over capacity)",
            "POST /verify": "check two tables for normal + failover equivalence",
            "GET /docs": "interactive demo",
        },
        "limits": {"max_routes": MAX_ROUTES},
        "reserved_action": DROP,
    }


@app.post("/compress", response_model=CompressResponse, summary="Compress a table")
def compress_endpoint(
    req: CompressRequest = Body(
        openapi_examples={
            "capacity_ok": EXAMPLE_CAPACITY_OK,
            "capacity_exceeded": EXAMPLE_CAPACITY_EXCEEDED,
            "failover_ladder": EXAMPLE_FAILOVER_LADDER,
        }
    )
):
    routes = [(IPv4Network(r.prefix), r.action) for r in req.routes]
    min_entries, table = compress(routes, req.failed_nexthop, req.capacity)

    if table is None:
        return CompressResponse(
            status="capacity_exceeded",
            fits=False,
            min_entries=min_entries,
            capacity=req.capacity,
            failed_nexthop=req.failed_nexthop,
            table=None,
            verified=None,
        )

    new_routes = [(IPv4Network(p), a) for p, a in table]
    ok, detail = verify(routes, new_routes, req.failed_nexthop)
    if not ok:  # pragma: no cover - indicates an algorithmic bug
        raise HTTPException(
            status_code=500,
            detail=f"internal verification failed: {detail['first_mismatch']}",
        )

    return CompressResponse(
        status="ok",
        fits=True,
        min_entries=min_entries,
        capacity=req.capacity,
        failed_nexthop=req.failed_nexthop,
        table=[Route(prefix=p, action=a) for p, a in table],
        verified=True,
    )


@app.post("/verify", response_model=VerifyResponse, summary="Check equivalence")
def verify_endpoint(req: VerifyRequest):
    old = [(IPv4Network(r.prefix), r.action) for r in req.old_routes]
    new = [(IPv4Network(r.prefix), r.action) for r in req.new_routes]
    ok, detail = verify(old, new, req.failed_nexthop)
    return VerifyResponse(equivalent=ok, **detail)
