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

The service also offers a second, independent tool for ECMP groups
identified by an action in the forwarding table: given the group's hash
buckets (current next-hop, bytes and active connections in one sampling
window) and the retained next-hops with their required bucket counts and
byte budgets, it returns a deployable bucket-to-next-hop mapping that
honours every count and budget while minimizing the active connections
that have to move away from their current next-hop.  Uplink maintenance:
remove one next-hop, rearrange the buckets before it leaves service.
"""

from __future__ import annotations

from ipaddress import IPv4Network, ip_network
from typing import Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .compressor import DROP, compress, verify
from .ecmp import (
    MAX_BUCKETS,
    MAX_RETAINED,
    Bucket,
    RetainedTarget,
    rearrange_buckets,
)

APP_VERSION = "1.1.0"
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

# 8-bucket ECMP group "ecmp-core" spread over NH1/NH2/NH3/NH4.  NH2's uplink
# is going into maintenance, so its 2 buckets must move to the retained
# next-hops.  NH1 and NH3 must each end up with 3 buckets, NH4 with 2.
# Optimal: NH2's heavy bucket 3 goes to NH3 (which already has 2 of its own),
# NH2's light bucket 4 joins NH4's existing bucket 7, and every bucket that
# is already on a retained next-hop stays put.  Only buckets 3 and 4 move,
# migrating 2100 + 700 = 2800 active connections.
EXAMPLE_ECMP_REARRANGE = {
    "summary": "摘除上联：ECMP 组 8 个哈希桶重排到保留下一跳",
    "description": (
        "组 ecmp-core 原有 8 个哈希桶分布在 NH1/NH2/NH3/NH4；NH2 即将检修，"
        "其桶必须全部改走保留下一跳。保留目标为 NH1 3 桶、NH3 3 桶、NH4 2 桶，"
        "各自给出同一采样窗口的字节预算。求解器在满足桶数与预算的前提下，"
        "让改走其他下一跳的活跃连接总数最少：只需迁移 NH2 的两个桶，共 2800 条连接。"
        "若预算过紧则只返回 feasible=false。"
    ),
    "value": {
        "group": "ecmp-core",
        "removed_nexthop": "NH2",
        "buckets": [
            {"bucket": 0, "current_nexthop": "NH1", "bytes_in_window": 4_200_000_000, "active_connections": 1200},
            {"bucket": 1, "current_nexthop": "NH1", "bytes_in_window": 3_800_000_000, "active_connections": 900},
            {"bucket": 2, "current_nexthop": "NH1", "bytes_in_window": 4_600_000_000, "active_connections": 1500},
            {"bucket": 3, "current_nexthop": "NH2", "bytes_in_window": 5_000_000_000, "active_connections": 2100},
            {"bucket": 4, "current_nexthop": "NH2", "bytes_in_window": 2_400_000_000, "active_connections": 700},
            {"bucket": 5, "current_nexthop": "NH3", "bytes_in_window": 3_100_000_000, "active_connections": 800},
            {"bucket": 6, "current_nexthop": "NH3", "bytes_in_window": 2_900_000_000, "active_connections": 650},
            {"bucket": 7, "current_nexthop": "NH4", "bytes_in_window": 1_800_000_000, "active_connections": 400},
        ],
        "retained": [
            {"nexthop": "NH1", "bucket_count": 3, "byte_budget": 15_000_000_000},
            {"nexthop": "NH3", "bucket_count": 3, "byte_budget": 14_000_000_000},
            {"nexthop": "NH4", "bucket_count": 2, "byte_budget": 5_000_000_000},
        ],
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


class EcmpBucket(BaseModel):
    """One hash bucket of an ECMP group in the current sampling window."""

    bucket: int = Field(ge=0, examples=[3], description="Bucket number.")
    current_nexthop: str = Field(
        min_length=1, max_length=64, examples=["NH2"],
        description="Next-hop this bucket currently hashes to.",
    )
    bytes_in_window: int = Field(
        ge=0, examples=[5_000_000_000],
        description="Bytes carried by this bucket in the sampling window.",
    )
    active_connections: int = Field(
        ge=0, examples=[2100],
        description="Active connections carried by this bucket in the same window.",
    )

    @field_validator("current_nexthop")
    @classmethod
    def _check_hop(cls, v: str) -> str:
        h = v.strip()
        if not h:
            raise ValueError("current_nexthop must be non-empty")
        return h


class EcmpRetained(BaseModel):
    """A next-hop that stays in service, with its bucket and byte targets."""

    nexthop: str = Field(min_length=1, max_length=64, examples=["NH1"])
    bucket_count: int = Field(
        ge=0, examples=[3],
        description="Exact number of buckets this next-hop must carry.",
    )
    byte_budget: int = Field(
        ge=0, examples=[15_000_000_000],
        description="Upper bound on the bytes assigned to it in the same window.",
    )

    @field_validator("nexthop")
    @classmethod
    def _check_hop(cls, v: str) -> str:
        h = v.strip()
        if not h:
            raise ValueError("nexthop must be non-empty")
        return h


class EcmpRearrangeRequest(BaseModel):
    group: str = Field(
        min_length=1, max_length=64, examples=["ecmp-core"],
        description="ECMP group name (the action identifying it in the FIB).",
    )
    removed_nexthop: str = Field(
        min_length=1, max_length=64, examples=["NH2"],
        description="Next-hop being taken out of service; all its buckets must move.",
    )
    buckets: List[EcmpBucket] = Field(
        max_length=MAX_BUCKETS,
        description=f"Up to {MAX_BUCKETS} buckets with distinct numbers.",
    )
    retained: List[EcmpRetained] = Field(
        max_length=MAX_RETAINED,
        description=(
            f"Up to {MAX_RETAINED} retained next-hops with the exact bucket "
            "count and byte budget each must receive."
        ),
    )

    @field_validator("group", "removed_nexthop")
    @classmethod
    def _check_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("must be non-empty")
        return s

    @model_validator(mode="after")
    def _coherent(self):
        numbers = [b.bucket for b in self.buckets]
        if len(set(numbers)) != len(numbers):
            raise ValueError("bucket numbers must be distinct")
        hops = [r.nexthop for r in self.retained]
        if len(set(hops)) != len(hops):
            raise ValueError("retained next-hops must be distinct")
        if self.removed_nexthop in set(hops):
            raise ValueError("removed_nexthop cannot also be a retained next-hop")
        return self


class EcmpRearrangeResponse(BaseModel):
    feasible: bool = Field(
        description="Whether every count and byte-budget constraint can be met."
    )
    group: str
    removed_nexthop: str
    assignment: Optional[Dict[int, str]] = Field(
        default=None,
        description=(
            "Bucket number -> retained next-hop, ready to deploy; null when "
            "infeasible."
        ),
    )
    migrated_connections: Optional[int] = Field(
        default=None,
        description=(
            "Active connections whose bucket changes next-hop; null when "
            "infeasible.  Minimized over all feasible assignments."
        ),
    )


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FIB Compressor",
    version=APP_VERSION,
    description=(
        "Two tools for an edge-router change window. "
        "(1) Compress an IPv4 forwarding table for a smaller-capacity router: "
        "the compressed table is entry-minimal and equivalent under "
        "longest-prefix matching — distinguishing drop from no-match — both "
        "before and after a next-hop failure drill. "
        "(2) Rearrange the hash buckets of an ECMP group before one uplink "
        "next-hop is removed: exact bucket counts and byte budgets per "
        "retained next-hop, with the fewest active connections moved. "
        "Try the ready-made examples below."
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
            "POST /ecmp/rearrange": "bucket-to-next-hop mapping for a draining uplink",
            "GET /docs": "interactive demo",
        },
        "limits": {
            "max_routes": MAX_ROUTES,
            "max_ecmp_buckets": MAX_BUCKETS,
            "max_retained_nexthops": MAX_RETAINED,
        },
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


@app.post(
    "/ecmp/rearrange",
    response_model=EcmpRearrangeResponse,
    summary="Rearrange ECMP buckets around a draining next-hop",
)
def ecmp_rearrange_endpoint(
    req: EcmpRearrangeRequest = Body(
        openapi_examples={"remove_uplink": EXAMPLE_ECMP_REARRANGE}
    )
):
    buckets = [
        Bucket(
            bucket=b.bucket,
            current_nexthop=b.current_nexthop,
            bytes_in_window=b.bytes_in_window,
            active_connections=b.active_connections,
        )
        for b in req.buckets
    ]
    targets = [
        RetainedTarget(
            nexthop=r.nexthop,
            bucket_count=r.bucket_count,
            byte_budget=r.byte_budget,
        )
        for r in req.retained
    ]
    result = rearrange_buckets(buckets, req.removed_nexthop, targets)
    return EcmpRearrangeResponse(
        feasible=result.feasible,
        group=req.group,
        removed_nexthop=req.removed_nexthop,
        assignment=result.bucket_to_nexthop,
        migrated_connections=result.migrated_connections,
    )
