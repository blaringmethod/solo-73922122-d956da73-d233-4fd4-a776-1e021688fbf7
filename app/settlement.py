"""结算流水批的整批校验与按理赔净支付汇总（纯内存、无 I/O）。

语义（与需求逐条对应）
======================
1. 交易按发生顺序排列：支付含唯一交易 ID、理赔 ID 和非负整数金额；
   全额冲正另含原交易 ID，不携带金额。
2. 冲正只能引用**本批先前**出现、**尚未被冲正**、且**同一理赔 ID**
   的支付交易。
3. 任一条交易 ID（含冲正自身的 transaction_id）与其它交易重复，
   或任一冲正不合法，**整批拒绝**，不产生任何部分核对结论。
4. 校验通过后按理赔 ID 汇总净支付：被冲正的支付全额剔除（净额不
   计入），其余支付金额累加；同一理赔内净额恒为非负。
5. 本模块不访问数据库、不缓存任何地区理赔全集，只处理本次提交的
   交易列表，输出 ``claim_id -> 净支付`` 供快照核对使用。
"""
from __future__ import annotations

from dataclasses import dataclass

from .schemas import PaymentTransaction, ReversalTransaction, SettlementTransaction


class SettlementValidationError(Exception):
    """结算批自身不合法（重复 ID / 非法冲正 / 超条数等），整批拒绝。"""

    def __init__(self, code: str, message: str, issues: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # 收集所有违规点，便于调用方一次性修正；即便有多个问题也整批拒绝。
        self.issues: list[str] = issues or [message]


@dataclass(frozen=True)
class NetPayment:
    claim_id: int
    net_amount: int


def validate_and_aggregate(
    transactions: list[SettlementTransaction],
    max_transactions: int,
) -> dict[int, int]:
    """校验整批交易并返回 ``{claim_id: 净支付金额}``。

    校验失败抛 :class:`SettlementValidationError`（HTTP 422），
    调用方不得据此返回任何部分核对结论。
    """
    if len(transactions) > max_transactions:
        raise SettlementValidationError(
            "too_many_transactions",
            f"交易条数 {len(transactions)} 超过上限 {max_transactions}，整批拒绝",
        )

    issues: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()

    # 支付交易索引：transaction_id -> 支付对象（仅登记本批先前出现的支付）
    payments: dict[str, PaymentTransaction] = {}
    reversed_payment_ids: set[str] = set()

    for index, txn in enumerate(transactions):
        position = index + 1

        # ---- 交易 ID 唯一性（支付与冲正共享同一 ID 空间）----
        if txn.transaction_id in seen_ids:
            if txn.transaction_id not in duplicate_ids:
                duplicate_ids.add(txn.transaction_id)
                issues.append(
                    f"第 {position} 条交易 ID {txn.transaction_id!r} 在本批重复"
                )
        else:
            seen_ids.add(txn.transaction_id)

        # ---- 冲正的引用合法性 ----
        if isinstance(txn, ReversalTransaction):
            original = payments.get(txn.original_transaction_id)
            if original is None:
                issues.append(
                    f"第 {position} 条冲正 {txn.transaction_id!r} 引用的原交易 "
                    f"{txn.original_transaction_id!r} 在本批先前支付中不存在"
                )
                continue
            if original.transaction_id in reversed_payment_ids:
                issues.append(
                    f"第 {position} 条冲正 {txn.transaction_id!r} 引用的原交易 "
                    f"{txn.original_transaction_id!r} 已被先前冲正，不可重复冲正"
                )
                continue
            if original.claim_id != txn.claim_id:
                issues.append(
                    f"第 {position} 条冲正 {txn.transaction_id!r} 的理赔 "
                    f"{txn.claim_id} 与原交易 {txn.original_transaction_id!r} "
                    f"所属理赔 {original.claim_id} 不一致，只允许同理赔冲正"
                )
                continue
            reversed_payment_ids.add(original.transaction_id)
        else:
            # 支付登记到索引，供后续冲正按发生顺序前向引用
            payments[txn.transaction_id] = txn

    if duplicate_ids:
        raise SettlementValidationError(
            "duplicate_transaction_id",
            "交易 ID 存在重复，整批拒绝且不返回部分核对结论",
            issues=issues,
        )
    if issues:
        raise SettlementValidationError(
            "invalid_reversal",
            "存在非法冲正（引用不存在/已冲正/跨理赔），整批拒绝且不返回部分核对结论",
            issues=issues,
        )

    # ---- 全部合法后再汇总净支付：被冲正的支付全额剔除 ----
    net_by_claim: dict[int, int] = {}
    for txn in transactions:
        if isinstance(txn, PaymentTransaction):
            if txn.transaction_id in reversed_payment_ids:
                continue
            net_by_claim[txn.claim_id] = (
                net_by_claim.get(txn.claim_id, 0) + txn.amount
            )
        # 冲正不产生金额行：全额冲正的效果就是剔除原支付
    return net_by_claim
