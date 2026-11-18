"""HTTP 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from .config import settings


class SessionStatus(str, Enum):
    OPEN = "open"          # 可正常取页
    CLOSING = "closing"    # 关闭已受理，正在等待在途取页结束
    CLOSED = "closed"      # 事务已回滚、连接已归还、名额已释放
    EXPIRED = "expired"    # 到期后由清理线程转入的终态（语义同 closed）


class OpenSessionRequest(BaseModel):
    region: str = Field(..., min_length=1, max_length=64, description="保单地区")


class SessionInfo(BaseModel):
    session_id: str
    region: str
    status: SessionStatus
    opened_at: datetime
    expires_at: datetime
    last_page_at: datetime | None = None
    pages_served: int
    ttl_seconds: int


class ClaimRow(BaseModel):
    claim_id: int
    policy_id: int
    region: str
    amount: int


class ClaimPage(BaseModel):
    session_id: str
    region: str
    page: PositiveInt
    page_size: int
    has_more: bool
    snapshot_time: datetime
    rows: list[ClaimRow]


class PolicyIn(BaseModel):
    region: str = Field(..., min_length=1, max_length=64)


class PolicyOut(BaseModel):
    id: int
    region: str


class ClaimIn(BaseModel):
    policy_id: int
    amount: int = Field(..., ge=0, description="整数理赔金额，单位分")


class ClaimOut(BaseModel):
    id: int
    policy_id: int
    amount: int


class Message(BaseModel):
    detail: str


# ----------------------------------------------------------------------
# 结算核对（POST /settlement-checks）
# ----------------------------------------------------------------------
class PaymentTransaction(BaseModel):
    """支付交易：必须含唯一交易 ID、理赔 ID 与非负整数金额。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["payment"] = "payment"
    transaction_id: str = Field(..., min_length=1, max_length=64)
    claim_id: int = Field(..., ge=1)
    amount: int = Field(..., ge=0, description="非负整数金额，单位：分")


class ReversalTransaction(BaseModel):
    """全额冲正：另含原交易 ID，且不携带金额（全额）。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["reversal"] = "reversal"
    transaction_id: str = Field(..., min_length=1, max_length=64)
    claim_id: int = Field(..., ge=1)
    original_transaction_id: str = Field(..., min_length=1, max_length=64)


SettlementTransaction = Annotated[
    Union[PaymentTransaction, ReversalTransaction],
    Field(discriminator="type"),
]


class SettlementCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(..., min_length=1, max_length=64)
    # 按发生顺序排列；至多 200 条，允许空批（等价于该批无任何净支付）
    transactions: list[SettlementTransaction] = Field(
        default_factory=list,
        max_length=settings.settlement_max_transactions,
    )


class SettlementDifferenceType(str, Enum):
    # 快照里存在、但本批净支付为 0（未发生有效支付，或支付已被全部冲正）
    UNSETTLED = "unsettled"
    # 净支付金额小于快照金额
    UNDERPAID = "underpaid"
    # 净支付金额大于快照金额
    OVERPAID = "overpaid"
    # 本批出现净支付、但快照中不存在该理赔（含非本会话地区的理赔）
    ORPHAN = "orphan"


class SettlementDifference(BaseModel):
    claim_id: int
    difference_type: SettlementDifferenceType
    snapshot_amount: int | None = Field(
        ..., description="快照金额；无源理赔（orphan）时为 null"
    )
    net_paid_amount: int = Field(..., description="本批按理赔汇总的净支付金额")
    difference_amount: int = Field(
        ...,
        description="净支付 - 快照金额（未结算为负值，无源理赔为净支付本身）",
    )


class SettlementCheckResult(BaseModel):
    session_id: str
    region: str
    snapshot_time: datetime
    transaction_count: int = Field(..., description="受理的交易总条数（含冲正）")
    settled_claim_count: int = Field(..., description="本批出现净支付的理赔笔数")
    snapshot_claim_count: int = Field(
        ..., description="会话快照中该地区的理赔总笔数"
    )
    # 四类差异的总笔数（统计全部差异，不随前 30 条截断）
    unsettled_count: int
    underpaid_count: int
    overpaid_count: int
    orphan_count: int
    total_difference_count: int
    # 按理赔 ID 升序的前 30 条差异
    differences: list[SettlementDifference]
    differences_truncated: bool = Field(
        ..., description="差异总数是否超过返回上限而被截断"
    )
    ready_for_warehouse: bool = Field(
        ..., description="全部吻合（无任何差异）才为 true"
    )
