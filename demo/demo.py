#!/usr/bin/env python3
"""慢取页与关键语义端到端演示（仅依赖标准库）。

前置：docker compose up -d 后运行  python3 demo/demo.py

演示内容
========
1. 分页与重复取页：华北 120 条理赔 -> 两页，claim_id 升序、每页 <=100；
   重复取第 1 页与首次结果逐行一致（同一快照）。
2. 快照隔离：会话开启后通过源端接口补录/修改/删除数据并提交，
   会话内取页结果不变。
3. 名额控制：最多 2 个会话，第 3 个开启请求被 429 拒绝；
   关闭一个后名额归还，可再开；另一个会话不受影响。
4. 慢取页 + 及时关闭：用 X-Debug-Sleep-Seconds 头让取页变慢，
   在它响应前发起关闭，关闭返回后再取页 -> 409 且无明细。
5. 到期失效（可选，较慢）：设置 DEMO_TTL_WAIT=1 时，
   等待会话到期后取页 -> 410 且无明细。
6. 结算核对 POST /settlement-checks：足额覆盖标记可入仓；冲正剔除、
   四类差异分类、前 30 条截断与计数、重复 ID/非法冲正整批拒绝、
   核对进行中关闭受理必须失败且不返回结论。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
TTL_WAIT = os.environ.get("DEMO_TTL_WAIT", "0") == "1"
PASS, FAIL = "✅", "❌"
results: list[tuple[bool, str]] = []


def check(cond: bool, msg: str) -> None:
    results.append((cond, msg))
    print(f"  {PASS if cond else FAIL} {msg}")
    if not cond:
        check.failed = True


check.failed = False


def call(method: str, path: str, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def title(n: int, text: str) -> None:
    print(f"\n=== 场景 {n}：{text} ===")


def main() -> int:
    print(f"目标服务: {BASE}")
    status, meta = call("GET", "/meta")
    assert status == 200, f"服务不可用: {status}"
    print(f"服务参数: {meta}")

    # 清理可能存在的旧会话，保证演示可重复运行
    for s in call("GET", "/sessions")[1]:
        if s["status"] in ("open", "closing"):
            call("POST", f"/sessions/{s['session_id']}/close")

    # ------------------------------------------------------------------
    title(1, "分页 + 重复取页（同一快照、claim_id 升序、每页至多 100 行）")
    _, s1 = call("POST", "/sessions", {"region": "华北"})
    sid1 = s1["session_id"]
    check(s1["status"] == "open", f"开启会话 {sid1[:8]}… 状态 open，TTL={s1['ttl_seconds']}s")

    st, p1 = call("GET", f"/sessions/{sid1}/claims?page=1&page_size=100")
    check(st == 200 and len(p1["rows"]) == 100, "第 1 页返回 100 行")
    check(p1["has_more"] is True, "has_more=true（还有下一页）")
    ids1 = [r["claim_id"] for r in p1["rows"]]
    check(ids1 == sorted(ids1), "claim_id 严格按升序排列")
    check(all(r["region"] == "华北" for r in p1["rows"]), "每行地区均为华北")
    check(all(isinstance(r["amount"], int) for r in p1["rows"]), "金额均为整数")

    st, p1b = call("GET", f"/sessions/{sid1}/claims?page=1&page_size=100")
    check([r["claim_id"] for r in p1b["rows"]] == ids1, "重复取第 1 页结果与首次完全一致")
    check(p1b["snapshot_time"] == p1["snapshot_time"], "两次取页 snapshot_time 相同")

    st, p2 = call("GET", f"/sessions/{sid1}/claims?page=2&page_size=100")
    check(st == 200 and len(p2["rows"]) == 20, "第 2 页返回剩余 20 行")
    check(p2["has_more"] is False, "has_more=false（已到末页）")
    check(p2["rows"][0]["claim_id"] > p1["rows"][-1]["claim_id"], "第 2 页首行 id 大于第 1 页末行 id")

    st, p3 = call("GET", f"/sessions/{sid1}/claims?page=3&page_size=100")
    check(st == 200 and p3["rows"] == [] and p3["has_more"] is False, "超出末页返回空页")

    st, p_overflow = call("GET", f"/sessions/{sid1}/claims?page=1&page_size=999")
    check(st == 200 and p_overflow["page_size"] == 100, "page_size=999 被服务端截断为 100")

    # ------------------------------------------------------------------
    title(2, "快照隔离：源端增改删照常提交，会话内数据不变")
    _, pol = call("POST", "/admin/policies", {"region": "华北"})
    _, new_claim = call("POST", "/admin/claims", {"policy_id": pol["id"], "amount": 55500})
    check(new_claim["amount"] == 55500, f"源端补录理赔 id={new_claim['id']} 已提交")
    # 记录原始金额，用与之不同的新值修改，保证演示可重复运行
    original_amount = p1["rows"][0]["amount"]
    changed_amount = (original_amount + 12345) % 500000
    call("PATCH", f"/admin/claims/{ids1[0]}?amount={changed_amount}")
    call("DELETE", f"/admin/claims/{ids1[1]}")
    check(True, f"源端修改 id={ids1[0]} 金额、删除 id={ids1[1]} 均已提交")

    st, p1c = call("GET", f"/sessions/{sid1}/claims?page=1&page_size=100")
    check([r["claim_id"] for r in p1c["rows"]] == ids1, "快照会话第 1 页仍为 100 行且 id 序列不变")
    check(p1c["rows"][0]["amount"] == original_amount, "被修改的金额在快照中保持原值")
    check(new_claim["id"] not in [r["claim_id"] for r in p1c["rows"]], "新补录理赔在快照中不可见")

    # ------------------------------------------------------------------
    title(3, "名额控制：最多 2 个会话，满额 429，关闭后归还且互不影响")
    _, s2 = call("POST", "/sessions", {"region": "华东"})
    sid2 = s2["session_id"]
    st, err = call("POST", "/sessions", {"region": "华南"})
    check(st == 429 and err["code"] == "capacity_reached", f"第 3 个会话被拒绝：HTTP {st} {err['detail']}")

    st, info1 = call("GET", f"/sessions/{sid1}")
    check(st == 200 and info1["status"] == "open", "已满额时既有会话 1 仍可查询且保持 open")

    st, closed = call("POST", f"/sessions/{sid2}/close")
    check(st == 200 and closed["status"] == "closed", f"关闭会话 2 -> {closed['status']}")

    st, s3 = call("POST", "/sessions", {"region": "华南"})
    sid3 = s3["session_id"]
    check(st == 201, "名额归还后新会话开启成功")

    st, p1d = call("GET", f"/sessions/{sid1}/claims?page=1&page_size=5")
    check(st == 200 and len(p1d["rows"]) == 5, "会话 1 不受会话 2 关闭影响，仍可正常取页")
    call("POST", f"/sessions/{sid3}/close")

    # ------------------------------------------------------------------
    title(4, "慢取页演示：关闭及时受理，受理后未开始响应的取页失败且无明细")
    _, s4 = call("POST", "/sessions", {"region": "华东"})
    sid4 = s4["session_id"]
    box = {}

    def slow_page():
        box["t0"] = time.time()
        # 服务端收到请求后先睡眠 4 秒再执行 SQL
        box["result"] = call(
            "GET", f"/sessions/{sid4}/claims?page=1&page_size=10",
            headers={"X-Debug-Sleep-Seconds": "4"}, timeout=15,
        )
        box["t1"] = time.time()

    t = threading.Thread(target=slow_page)
    t.start()
    time.sleep(1)  # 确保慢取页已到达服务端但尚未开始执行 SQL
    t_close0 = time.time()
    st, info = call("POST", f"/sessions/{sid4}/close")
    t_close1 = time.time()
    check(st == 200 and info["status"] == "closed",
          f"关闭请求在 {t_close1 - t_close0:.2f}s 内受理完成，状态=closed")
    t.join()
    st_slow, body_slow = box["result"]
    print(f"    慢取页最终返回: HTTP {st_slow}，耗时 {box['t1'] - box['t0']:.2f}s，"
          f"响应={json.dumps(body_slow, ensure_ascii=False)[:120]}")
    check(st_slow in (409, 410), "慢取页在关闭后失败（409/410），未返回明细")
    check(isinstance(body_slow, dict) and "rows" not in body_slow, "失败响应不含任何理赔明细行")

    st, after = call("GET", f"/sessions/{sid4}/claims?page=1")
    check(st == 409 and "rows" not in after, f"关闭后再次取页被拒绝 HTTP {st}，且无明细")

    st, info = call("GET", f"/sessions/{sid4}")
    check(st == 200 and info["status"] == "closed", "会话状态可查，终态=closed")

    _, s5 = call("POST", "/sessions", {"region": "华东"})
    check(s5["status"] == "open", "关闭并释放名额后可立即开启新会话")
    call("POST", f"/sessions/{s5['session_id']}/close")

    # ------------------------------------------------------------------
    if TTL_WAIT:
        title(5, "到期失效：60 秒后会话自动到期，取页 410 且无明细")
        _, s6 = call("POST", "/sessions", {"region": "华南"})
        sid6 = s6["session_id"]
        print("    等待 62 秒让会话到期 …")
        time.sleep(62)
        st, expired = call("GET", f"/sessions/{sid6}/claims?page=1")
        check(st in (410, 409), f"到期后取页被拒绝 HTTP {st}，且无明细")
        check("rows" not in expired, "到期失败响应不含理赔明细行")
        st, info = call("GET", f"/sessions/{sid6}")
        check(st == 200 and info["status"] == "expired", "会话状态可查，终态=expired")
        st, s7 = call("POST", "/sessions", {"region": "华南"})
        check(st == 201 and s7["status"] == "open", "到期释放名额后可开启新会话")
        call("POST", f"/sessions/{s7['session_id']}/close")

    # ------------------------------------------------------------------
    title(6, "结算核对 POST /settlement-checks（华南 40 条快照，逐笔核对净支付）")
    _, sc = call("POST", "/sessions", {"region": "华南"})
    scid = sc["session_id"]

    # 翻完快照全部页，拿到核对基准（仅演示侧取数，服务端不缓存全集）
    snapshot, page_no = [], 1
    while True:
        st, b = call("GET", f"/sessions/{scid}/claims?page={page_no}&page_size=100")
        snapshot.extend(b["rows"])
        if not b["has_more"]:
            break
        page_no += 1
    check(len(snapshot) == 40, f"取齐华南快照 {len(snapshot)} 条作为核对基准")

    def pay(tid, cid, amount):
        return {"type": "payment", "transaction_id": tid,
                "claim_id": cid, "amount": amount}

    def rev(tid, cid, original):
        return {"type": "reversal", "transaction_id": tid,
                "claim_id": cid, "original_transaction_id": original}

    # 6.1 全部足额覆盖 -> 可入仓
    full = [pay(f"P{r['claim_id']}", r["claim_id"], r["amount"]) for r in snapshot]
    st, ok = call("POST", "/settlement-checks",
                  {"session_id": scid, "transactions": full})
    check(st == 200 and ok["ready_for_warehouse"] is True,
          f"足额覆盖 40 笔 -> ready_for_warehouse=true（差异 {ok['total_difference_count']}）")
    check(ok["snapshot_claim_count"] == 40 and ok["transaction_count"] == 40,
          "回执含快照理赔笔数 40 与受理交易笔数 40")

    # 6.2 冲正剔除 + 四类差异：少付/超付/未结算（冲正后净额 0）/无源
    under, over, unsettled = snapshot[0], snapshot[1], snapshot[2]
    mixed = [
        pay("M1", under["claim_id"], under["amount"] - 100),       # 少付 100
        pay("M2", over["claim_id"], over["amount"] + 100),         # 超付 100
        pay("M3", unsettled["claim_id"], unsettled["amount"]),
        rev("M3R", unsettled["claim_id"], "M3"),                   # 全额冲正 -> 未结算
        pay("M4", 999000111, 500),                                 # 无源理赔
    ]
    st, diff = call("POST", "/settlement-checks",
                    {"session_id": scid, "transactions": mixed})
    check(st == 200 and diff["ready_for_warehouse"] is False, "存在差异 -> 不可入仓")
    check(diff["underpaid_count"] == 1 and diff["overpaid_count"] == 1,
          f"少付 {diff['underpaid_count']} 笔、超付 {diff['overpaid_count']} 笔")
    check(diff["unsettled_count"] == 38, f"冲正后未结算 {diff['unsettled_count']} 笔（40-2 笔有效支付）")
    check(diff["orphan_count"] == 1, f"无源理赔 {diff['orphan_count']} 笔")
    check(diff["total_difference_count"] == 41 and diff["differences_truncated"] is True,
          f"差异总笔数 {diff['total_difference_count']}（计数不随前 30 条截断），截断标记=true")
    check(len(diff["differences"]) == 30, "差异明细只返回按理赔 ID 升序的前 30 条")
    ids = [d["claim_id"] for d in diff["differences"]]
    check(ids == sorted(ids), "返回的差异明细按理赔 ID 升序")

    # 6.3 重复交易 ID：整批 422 拒绝，无任何核对结论
    st, rej1 = call("POST", "/settlement-checks", {
        "session_id": scid,
        "transactions": [pay("DUP", snapshot[0]["claim_id"], 1),
                         pay("DUP", snapshot[1]["claim_id"], 2)],
    })
    check(st == 422 and rej1["code"] == "duplicate_transaction_id",
          f"重复 ID 整批拒绝 HTTP {st} code={rej1.get('code')}")
    check("differences" not in rej1 and "ready_for_warehouse" not in rej1,
          "拒绝响应不含部分核对结论")

    # 6.4 非法冲正（跨理赔/引用不存在/重复冲正）：整批 422 拒绝
    c1, c2 = snapshot[0]["claim_id"], snapshot[1]["claim_id"]
    st, rej2 = call("POST", "/settlement-checks", {
        "session_id": scid,
        "transactions": [pay("X1", c1, 10), rev("X2", c2, "X1")],
    })
    check(st == 422 and rej2["code"] == "invalid_reversal",
          f"跨理赔冲正整批拒绝 HTTP {st} code={rej2.get('code')}")
    st, rej3 = call("POST", "/settlement-checks", {
        "session_id": scid,
        "transactions": [rev("Y1", c1, "ghost")],
    })
    check(st == 422 and rej3["code"] == "invalid_reversal",
          "引用本批不存在的原交易 -> invalid_reversal")
    st, rej4 = call("POST", "/settlement-checks", {
        "session_id": scid,
        "transactions": [pay("Z1", c1, 10), rev("Z2", c1, "Z1"),
                         rev("Z3", c1, "Z1")],
    })
    check(st == 422 and rej4["code"] == "invalid_reversal",
          "对同一支付重复冲正 -> invalid_reversal")

    # 6.5 空批：全部快照理赔判为未结算，不可入仓
    st, empty = call("POST", "/settlement-checks",
                     {"session_id": scid, "transactions": []})
    check(st == 200 and empty["unsettled_count"] == 40
          and empty["ready_for_warehouse"] is False,
          "空批核对：40 笔全部未结算，不可入仓")

    # 6.6 核对进行中关闭受理：慢核对 + 关闭交错，核对失败且无结论
    box = {}

    def slow_check():
        box["r"] = call(
            "POST", "/settlement-checks",
            {"session_id": scid, "transactions": [pay("S1", c1, snapshot[0]["amount"])]},
            headers={"X-Debug-Sleep-Seconds": "3"}, timeout=15,
        )

    t = threading.Thread(target=slow_check)
    t.start()
    time.sleep(1)
    st, info = call("POST", f"/sessions/{scid}/close")
    check(st == 200 and info["status"] == "closed", "核对期间关闭被及时受理，状态=closed")
    t.join()
    st_slow, body_slow = box["r"]
    check(st_slow in (409, 410),
          f"在途核对在关闭后失败 HTTP {st_slow}，不换快照续算")
    check("differences" not in body_slow, "失败的核对响应不含任何差异结论")
    st, after = call("POST", "/settlement-checks",
                     {"session_id": scid, "transactions": []})
    check(st in (409, 410), f"关闭后再次核对同样被拒绝 HTTP {st}")

    # ------------------------------------------------------------------
    # 收尾
    call("POST", f"/sessions/{sid1}/close")

    print("\n" + "=" * 60)
    failed = [m for ok, m in results if not ok]
    print(f"共 {len(results)} 项检查，通过 {len(results) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        for m in failed:
            print(f"  {FAIL} {m}")
        return 1
    print("全部通过 🎉")
    print("\n提示：交互式文档位于 /docs（Swagger UI），原始 OpenAPI 位于 /openapi.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
