"""端到端自测：覆盖分页、快照隔离、名额、关闭、到期、连接丢失与重启语义，
以及结算核对（POST /settlement-checks）的冲正、差异分类与关闭/到期交错。

运行方式（本地已起好 PostgreSQL 15，见 README）：
    DATABASE_URL=postgresql://actuary:actuary@127.0.0.1:55432/claims \
    SESSION_TTL_SECONDS=2 ENABLE_DEBUG_HOOKS=true \
    python -m unittest tests.test_api -v
"""
from __future__ import annotations

import os
import threading
import time
import unittest

import httpx
import psycopg
import uvicorn

# 本地测试用默认值
os.environ.setdefault(
    "DATABASE_URL", "postgresql://actuary:actuary@127.0.0.1:55432/claims"
)
os.environ.setdefault("SESSION_TTL_SECONDS", "2")
os.environ.setdefault("ENABLE_DEBUG_HOOKS", "true")
os.environ.setdefault("SWEEPER_INTERVAL_SECONDS", "0.1")

from app.config import settings  # noqa: E402
from app.main import app, manager  # noqa: E402
from app.session_manager import SessionError  # noqa: E402

SEED_SQL = os.path.join(os.path.dirname(__file__), "..", "db", "init", "02_seed.sql")


def reset_seed_data() -> None:
    """每个用例前重置演示数据，保证改/删类测试可重复运行。"""
    with open(SEED_SQL, encoding="utf-8") as fh:
        sql = fh.read()
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        conn.execute(sql)


TTL = settings.session_ttl_seconds


class ApiTestBase(unittest.TestCase):
    # 整个套件共享一个 uvicorn 实例与一个 manager 单例，
    # 避免多实例争抢同一端口导致请求串到旧进程。
    _server: "uvicorn.Server | None" = None
    _server_thread: threading.Thread | None = None
    client: httpx.Client

    @classmethod
    def setUpClass(cls):
        if ApiTestBase._server is None:
            config = uvicorn.Config(
                "app.main:app", host="127.0.0.1", port=18080,
                log_level="warning",
            )
            ApiTestBase._server = uvicorn.Server(config)
            ApiTestBase._server_thread = threading.Thread(
                target=ApiTestBase._server.run,
                name="test-uvicorn", daemon=True,
            )
            ApiTestBase._server_thread.start()
            for _ in range(100):
                try:
                    httpx.get("http://127.0.0.1:18080/health", timeout=1)
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            # 全套件以干净的种子数据开始
            reset_seed_data()
        cls.client = httpx.Client(
            base_url="http://127.0.0.1:18080", timeout=15
        )

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        # 每个用例前清掉所有未终结会话，保证相互独立
        for s in manager.list_sessions():
            if s.status in ("open", "closing"):
                try:
                    manager.close(s.session_id)
                except SessionError:
                    pass
        # 写入类用例会改数据，统一在用例内自行安排快照时机，
        # 不在全局 setUp 中重置（否则会清掉同用例刚提交的写入）。

    @classmethod
    def _reset_seed(cls):
        reset_seed_data()

    def open(self, region="华北"):
        r = self.client.post("/sessions", json={"region": region})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def close(self, sid):
        return self.client.post(f"/sessions/{sid}/close")

    def page(self, sid, page=1, page_size=100, headers=None):
        return self.client.get(
            f"/sessions/{sid}/claims",
            params={"page": page, "page_size": page_size},
            headers=headers or {},
        )

    def check_settlement(self, sid, transactions, headers=None):
        return self.client.post(
            "/settlement-checks",
            json={"session_id": sid, "transactions": transactions},
            headers=headers or {},
        )

    def all_snapshot_rows(self, sid):
        """翻完会话全部页，返回快照内理赔行（仅测试侧取数用）。"""
        rows, page_no = [], 1
        while True:
            body = self.page(sid, page=page_no).json()
            rows.extend(body["rows"])
            if not body["has_more"]:
                return rows
            page_no += 1

    @staticmethod
    def pay(tid, claim_id, amount):
        return {
            "type": "payment",
            "transaction_id": tid,
            "claim_id": claim_id,
            "amount": amount,
        }

    @staticmethod
    def reverse(tid, claim_id, original_tid):
        return {
            "type": "reversal",
            "transaction_id": tid,
            "claim_id": claim_id,
            "original_transaction_id": original_tid,
        }


class TestPagination(ApiTestBase):
    def test_pages_ordered_and_capped(self):
        s = self.open()
        r1 = self.page(s["session_id"])
        self.assertEqual(r1.status_code, 200)
        body = r1.json()
        self.assertEqual(len(body["rows"]), 100)
        self.assertTrue(body["has_more"])
        ids = [r["claim_id"] for r in body["rows"]]
        self.assertEqual(ids, sorted(ids))
        self.assertTrue(all(r["region"] == "华北" for r in body["rows"]))
        self.assertTrue(all(isinstance(r["amount"], int) for r in body["rows"]))

        r2 = self.page(s["session_id"], page=2)
        self.assertEqual(len(r2.json()["rows"]), 20)
        self.assertFalse(r2.json()["has_more"])
        self.assertGreater(
            r2.json()["rows"][0]["claim_id"], body["rows"][-1]["claim_id"]
        )

        r3 = self.page(s["session_id"], page=3)
        self.assertEqual(r3.json()["rows"], [])
        self.assertFalse(r3.json()["has_more"])

    def test_page_size_forced_to_cap(self):
        s = self.open()
        r = self.page(s["session_id"], page_size=999)
        self.assertEqual(r.json()["page_size"], 100)
        self.assertEqual(len(r.json()["rows"]), 100)

    def test_invalid_pagination_params(self):
        s = self.open()
        self.assertEqual(self.client.get(
            f"/sessions/{s['session_id']}/claims", params={"page": 0}
        ).status_code, 422)
        self.assertEqual(self.client.get(
            f"/sessions/{s['session_id']}/claims", params={"page_size": 0}
        ).status_code, 422)

    def test_repeat_page_same_snapshot(self):
        s = self.open()
        b1 = self.page(s["session_id"]).json()
        b2 = self.page(s["session_id"]).json()
        self.assertEqual(
            [r["claim_id"] for r in b1["rows"]],
            [r["claim_id"] for r in b2["rows"]],
        )
        self.assertEqual(b1["snapshot_time"], b2["snapshot_time"])


class TestSnapshotIsolation(ApiTestBase):
    def test_source_writes_invisible_inside_session(self):
        self._reset_seed()  # 本用例需要干净的种子数据
        s = self.open("华北")
        before = self.page(s["session_id"]).json()
        before_ids = {r["claim_id"] for r in before["rows"]}

        # 源端：新增保单+理赔、改金额、删理赔，均独立提交
        pol = self.client.post("/admin/policies", json={"region": "华北"})
        self.assertEqual(pol.status_code, 201)
        nc = self.client.post(
            "/admin/claims",
            json={"policy_id": pol.json()["id"], "amount": 777},
        )
        self.assertEqual(nc.status_code, 201)
        first_id = before["rows"][0]["claim_id"]
        second_id = before["rows"][1]["claim_id"]
        self.assertEqual(self.client.patch(
            f"/admin/claims/{first_id}", params={"amount": 1}
        ).status_code, 200)
        self.assertEqual(self.client.delete(
            f"/admin/claims/{second_id}"
        ).status_code, 204)

        after = self.page(s["session_id"]).json()
        after_ids = {r["claim_id"] for r in after["rows"]}
        self.assertEqual(before_ids, after_ids)
        self.assertNotIn(nc.json()["id"], after_ids)
        self.assertNotEqual(after["rows"][0]["amount"], 1)
        self.assertIn(second_id, after_ids)

        # 新会话能看到源端最新提交（翻完全部页核对）
        s2 = self.open("华北")
        fresh_ids = set()
        page_no = 1
        while True:
            body = self.page(s2["session_id"], page=page_no).json()
            fresh_ids.update(r["claim_id"] for r in body["rows"])
            if not body["has_more"]:
                break
            page_no += 1
        self.assertIn(nc.json()["id"], fresh_ids)
        self.assertNotIn(second_id, fresh_ids)
        self.close(s2["session_id"])

    def test_snapshot_covers_both_tables(self):
        # 地区来自 policies 表、金额来自 claims 表：JOIN 结果同样固定
        s = self.open("华东")
        b = self.page(s["session_id"]).json()
        self.assertEqual(len(b["rows"]), 100)
        self.assertTrue(all(r["region"] == "华东" for r in b["rows"]))


class TestCapacity(ApiTestBase):
    def test_max_two_sessions_and_release(self):
        s1 = self.open("华东")
        s2 = self.open("华南")
        r = self.client.post("/sessions", json={"region": "华北"})
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["code"], "capacity_reached")

        # 满额不影响既有会话
        self.assertEqual(self.page(s1["session_id"], page_size=5).status_code, 200)

        self.assertEqual(self.close(s2["session_id"]).json()["status"], "closed")
        s3 = self.open("华北")  # 名额归还后可再开
        self.assertEqual(s3["status"], "open")
        # 另一个会话不受影响
        self.assertEqual(self.page(s1["session_id"], page_size=5).status_code, 200)
        self.close(s1["session_id"])
        self.close(s3["session_id"])

    def test_unknown_session_404(self):
        r = self.client.get("/sessions/nonexistent/claims")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.client.get("/sessions/nonexistent").status_code, 404)


class TestCloseSemantics(ApiTestBase):
    def test_close_rejects_later_pages_but_keeps_status_queryable(self):
        s = self.open()
        self.close(s["session_id"])
        r = self.page(s["session_id"])
        self.assertEqual(r.status_code, 409)
        self.assertNotIn("rows", r.json())
        info = self.client.get(f"/sessions/{s['session_id']}").json()
        self.assertEqual(info["status"], "closed")

    def test_slow_page_then_close(self):
        s = self.open("华东")
        sid = s["session_id"]
        box = {}

        def slow():
            box["r"] = self.client.get(
                f"/sessions/{sid}/claims",
                params={"page": 1, "page_size": 10},
                headers={"X-Debug-Sleep-Seconds": "3"},
                timeout=15,
            )

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.8)
        r = self.close(sid)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "closed")
        t.join()
        self.assertIn(box["r"].status_code, (409, 410))
        self.assertNotIn("rows", box["r"].json())

        # 关闭后名额已归还，可开新会话
        s2 = self.open("华东")
        self.assertEqual(s2["status"], "open")
        self.close(s2["session_id"])

    def test_close_idempotent_and_new_session_unaffected(self):
        s1 = self.open("华东")
        s2 = self.open("华南")
        self.close(s1["session_id"])
        again = self.close(s1["session_id"])
        self.assertEqual(again.json()["status"], "closed")
        # 另一个会话照常工作
        self.assertEqual(self.page(s2["session_id"], page_size=5).status_code, 200)
        self.close(s2["session_id"])


class TestExpiry(ApiTestBase):
    def test_expiry_rejects_pages_and_releases_slot(self):
        s = self.open("华南")
        sid = s["session_id"]
        time.sleep(TTL + 0.6)
        r = self.page(sid)
        self.assertIn(r.status_code, (409, 410))
        self.assertNotIn("rows", r.json())
        info = self.client.get(f"/sessions/{sid}").json()
        self.assertEqual(info["status"], "expired")
        # 名额已释放：能连开两个新会话
        a = self.open("华东")
        b = self.open("华北")
        self.assertEqual(a["status"], "open")
        self.assertEqual(b["status"], "open")
        self.close(a["session_id"])
        self.close(b["session_id"])

    def test_pages_before_expiry_still_work(self):
        s = self.open("华南")
        r = self.page(s["session_id"], page_size=10)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["rows"]), 10)


class TestConnectionLossAndRestart(ApiTestBase):
    """连接丢失（终止会话后端）与服务重启语义。

    本环境禁止 kill 系统调用，故用 pg_terminate_backend 从 SQL 层
    断开会话持有的后端连接，对客户端而言与网络/进程断开等价。
    """

    def _terminate_session_backend(self, sid: str) -> None:
        sess = manager.get_status(sid)
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            # 按客户端后端 PID 精准终止该会话独占的连接
            admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (sess.conn.info.backend_pid,),
            )
        # 给客户端一点时间感知 TCP 断开
        deadline = time.time() + 5
        while time.time() < deadline:
            if sess.conn.closed:
                return
            time.sleep(0.1)

    def test_connection_loss_invalidates_session_no_snapshot_swap(self):
        s = self.open("华东")
        sid = s["session_id"]
        # 从 SQL 层断开会话持有的后端连接 -> 快照事务随之消失
        self._terminate_session_backend(sid)
        r = self.page(sid)
        # 旧会话直接失败，绝不换连接/换快照续取；
        # 409（清理线程先标记终态）或 503（取页时发现连接断裂）均合规
        self.assertIn(r.status_code, (409, 503))
        self.assertNotIn("rows", r.json())
        # 旧 id 无法续取（失效会话已移出注册表或已为终态）
        info = self.client.get(f"/sessions/{sid}")
        if info.status_code == 200:
            self.assertIn(info.json()["status"], ("closed", "expired"))
            self.assertEqual(self.page(sid).status_code, 409)
        else:
            self.assertEqual(info.status_code, 404)
        # 新会话在池恢复后的连接上正常开启
        s2 = self.open("华东")
        body = self.page(s2["session_id"], page_size=5)
        self.assertEqual(body.status_code, 200)
        self.assertEqual(len(body.json()["rows"]), 5)
        self.close(s2["session_id"])

    @staticmethod
    def _restart_uvicorn():
        """完整重启共享 uvicorn：lifespan 负责 manager.stop()/start()。"""
        ApiTestBase._server.should_exit = True
        ApiTestBase._server_thread.join(timeout=10)
        config = uvicorn.Config(
            "app.main:app", host="127.0.0.1", port=18080, log_level="warning",
        )
        ApiTestBase._server = uvicorn.Server(config)
        ApiTestBase._server_thread = threading.Thread(
            target=ApiTestBase._server.run,
            name="test-uvicorn-restart", daemon=True,
        )
        ApiTestBase._server_thread.start()
        for _ in range(100):
            try:
                if httpx.get(
                    "http://127.0.0.1:18080/health", timeout=1
                ).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise RuntimeError("uvicorn 重启后健康检查失败")

    def test_service_restart_drops_in_memory_sessions(self):
        s = self.open("华南")
        sid = s["session_id"]

        # 模拟 API 服务重启（新进程语义：内存注册表为空、池与 sweeper 重建）
        self._restart_uvicorn()

        # 旧会话 id 在新进程中不存在，不得换快照续取
        r = self.client.get(f"/sessions/{sid}")
        self.assertEqual(r.status_code, 404)
        r2 = self.page(sid)
        self.assertEqual(r2.status_code, 404)
        s2 = self.open("华南")
        self.assertEqual(s2["status"], "open")
        self.close(s2["session_id"])


class TestSettlementCheck(ApiTestBase):
    """POST /settlement-checks：净支付汇总与四类差异分类。"""

    def test_full_cover_marks_warehouse_ready(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        txns = [
            self.pay(f"t{r['claim_id']}", r["claim_id"], r["amount"])
            for r in rows
        ]
        r = self.check_settlement(sid, txns)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["snapshot_claim_count"], 40)
        self.assertEqual(body["settled_claim_count"], 40)
        self.assertEqual(body["transaction_count"], 40)
        self.assertEqual(body["total_difference_count"], 0)
        for key in (
            "unsettled_count", "underpaid_count",
            "overpaid_count", "orphan_count",
        ):
            self.assertEqual(body[key], 0)
        self.assertEqual(body["differences"], [])
        self.assertFalse(body["differences_truncated"])
        self.assertTrue(body["ready_for_warehouse"])
        self.close(sid)

    def test_four_difference_types_classified(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        under, over, unsettled = rows[0], rows[1], rows[2]
        txns = [
            # 少付 100
            self.pay("p1", under["claim_id"], under["amount"] - 100),
            # 超付 100
            self.pay("p2", over["claim_id"], over["amount"] + 100),
            # 支付后被全额冲正 -> 净额 0 -> 未结算
            self.pay("p3", unsettled["claim_id"], unsettled["amount"]),
            self.reverse("rv3", unsettled["claim_id"], "p3"),
            # 快照中不存在的理赔 -> 无源
            self.pay("p4", 999_000_111, 500),
        ]
        r = self.check_settlement(sid, txns)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["underpaid_count"], 1)
        self.assertEqual(body["overpaid_count"], 1)
        # 40 笔快照理赔中只有前 2 笔保持有效净支付，第 3 笔被全额冲正
        self.assertEqual(body["unsettled_count"], 38)
        self.assertEqual(body["orphan_count"], 1)
        self.assertEqual(body["total_difference_count"], 41)
        self.assertFalse(body["ready_for_warehouse"])
        # 差异 41 条 > 上限 30：明细被截断，无源理赔 ID 最大排在后面，
        # 但其计数不受截断影响（计数在 SQL LIMIT 之前由窗口函数统计）
        self.assertTrue(body["differences_truncated"])
        self.assertEqual(len(body["differences"]), 30)

        by_id = {d["claim_id"]: d for d in body["differences"]}
        self.assertEqual(by_id[under["claim_id"]]["difference_type"], "underpaid")
        self.assertEqual(
            by_id[under["claim_id"]]["difference_amount"], -100
        )
        self.assertEqual(by_id[over["claim_id"]]["difference_type"], "overpaid")
        self.assertEqual(by_id[over["claim_id"]]["difference_amount"], 100)
        d_unsettled = by_id[unsettled["claim_id"]]
        self.assertEqual(d_unsettled["difference_type"], "unsettled")
        self.assertEqual(d_unsettled["net_paid_amount"], 0)
        self.assertEqual(
            d_unsettled["difference_amount"], -unsettled["amount"]
        )

        # 单独核对无源理赔：先对全部 40 笔快照理赔足额支付，再加一笔快照
        # 中不存在的支付 -> 差异总数恰为 1，不会被前 30 条截断
        cover = [
            self.pay(f"c{r['claim_id']}", r["claim_id"], r["amount"])
            for r in rows
        ]
        r_orphan = self.check_settlement(
            sid, cover + [self.pay("p4", 999_000_111, 500)]
        )
        obody = r_orphan.json()
        self.assertEqual(obody["orphan_count"], 1)
        self.assertEqual(obody["total_difference_count"], 1)
        self.assertFalse(obody["differences_truncated"])
        d_orphan = obody["differences"][0]
        self.assertEqual(d_orphan["claim_id"], 999_000_111)
        self.assertEqual(d_orphan["difference_type"], "orphan")
        self.assertIsNone(d_orphan["snapshot_amount"])
        self.assertEqual(d_orphan["net_paid_amount"], 500)
        self.assertEqual(d_orphan["difference_amount"], 500)

        # 返回明细按理赔 ID 升序
        ids = [d["claim_id"] for d in body["differences"]]
        self.assertEqual(ids, sorted(ids))
        self.close(sid)

    def test_diff_limit_30_sorted_and_counts_cover_all(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        # 只付 5 笔，其余 35 笔全部未结算；差异总数 35 > 30
        txns = [
            self.pay(f"p{r['claim_id']}", r["claim_id"], r["amount"])
            for r in rows[:5]
        ]
        r = self.check_settlement(sid, txns)
        body = r.json()
        self.assertEqual(body["total_difference_count"], 35)
        self.assertEqual(body["unsettled_count"], 35)
        self.assertEqual(len(body["differences"]), 30)
        self.assertTrue(body["differences_truncated"])
        ids = [d["claim_id"] for d in body["differences"]]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids, [r["claim_id"] for r in rows[5:35]])
        self.close(sid)

    def test_empty_batch_leaves_all_unsettled(self):
        s = self.open("华南")
        sid = s["session_id"]
        r = self.check_settlement(sid, [])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["transaction_count"], 0)
        self.assertEqual(body["settled_claim_count"], 0)
        self.assertEqual(body["unsettled_count"], 40)
        self.assertEqual(body["total_difference_count"], 40)
        self.assertFalse(body["ready_for_warehouse"])
        self.assertTrue(body["differences_truncated"])
        self.assertEqual(len(body["differences"]), 30)
        self.close(sid)

    def test_duplicate_transaction_id_rejects_whole_batch(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        c1, c2 = rows[0]["claim_id"], rows[1]["claim_id"]
        r = self.check_settlement(
            sid,
            [self.pay("dup", c1, 10), self.pay("dup", c2, 20)],
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "duplicate_transaction_id")
        # 整批拒绝：不返回任何核对结论字段
        self.assertNotIn("differences", r.json())
        self.assertNotIn("ready_for_warehouse", r.json())
        self.close(sid)

    def test_invalid_reversals_reject_whole_batch(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        c1, c2 = rows[0]["claim_id"], rows[1]["claim_id"]

        # 引用不存在的原交易
        r = self.check_settlement(
            sid, [self.reverse("r1", c1, "ghost")]
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "invalid_reversal")
        self.assertNotIn("differences", r.json())

        # 跨理赔冲正
        r = self.check_settlement(
            sid,
            [
                self.pay("p1", c1, 10),
                self.reverse("r2", c2, "p1"),
            ],
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "invalid_reversal")

        # 重复冲正同一笔支付
        r = self.check_settlement(
            sid,
            [
                self.pay("p1", c1, 10),
                self.reverse("r2", c1, "p1"),
                self.reverse("r3", c1, "p1"),
            ],
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "invalid_reversal")
        self.close(sid)

    def test_reversal_must_reference_earlier_payment(self):
        s = self.open("华南")
        sid = s["session_id"]
        c = self.all_snapshot_rows(sid)[0]["claim_id"]
        # 冲正出现在原支付之前：引用不到 -> 非法
        r = self.check_settlement(
            sid,
            [
                self.reverse("r1", c, "p1"),
                self.pay("p1", c, 10),
            ],
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "invalid_reversal")
        self.close(sid)

    def test_batch_over_200_rejected(self):
        s = self.open("华南")
        sid = s["session_id"]
        c = self.all_snapshot_rows(sid)[0]["claim_id"]
        txns = [
            self.pay(f"t{i:04d}", c, 1) for i in range(201)
        ]
        r = self.check_settlement(sid, txns)
        # 请求体层 max_length=200 直接 422，整批拒绝且无核对结论
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("differences", r.json())
        self.assertNotIn("ready_for_warehouse", r.json())
        self.close(sid)

    def test_unknown_and_closed_session_checks_fail(self):
        # 未知会话 -> 404
        r = self.check_settlement("no-such-session", [])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["code"], "session_not_found")

        s = self.open("华南")
        sid = s["session_id"]
        self.close(sid)
        r = self.check_settlement(sid, [])
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "session_not_open")
        self.assertNotIn("differences", r.json())

    def test_check_interleaved_with_close_fails_without_conclusion(self):
        s = self.open("华东")
        sid = s["session_id"]
        c = self.all_snapshot_rows(sid)[0]
        payload = {
            "session_id": sid,
            "transactions": [self.pay("p1", c["claim_id"], c["amount"])],
        }
        box = {}

        def slow():
            box["r"] = self.client.post(
                "/settlement-checks",
                json=payload,
                headers={"X-Debug-Sleep-Seconds": "3"},
                timeout=15,
            )

        t = threading.Thread(target=slow)
        t.start()
        time.sleep(0.8)
        closed = self.close(sid)
        self.assertEqual(closed.status_code, 200)
        t.join()
        r = box["r"]
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "session_not_open")
        self.assertNotIn("differences", r.json())

    def test_check_interleaved_with_expiry_fails(self):
        s = self.open("华南")
        sid = s["session_id"]
        c = self.all_snapshot_rows(sid)[0]
        # 调试睡眠跨过 TTL（TTL=2s）：核对期间到期，必须失败不续算
        r = self.client.post(
            "/settlement-checks",
            json={
                "session_id": sid,
                "transactions": [
                    self.pay("p1", c["claim_id"], c["amount"])
                ],
            },
            headers={"X-Debug-Sleep-Seconds": str(TTL + 1.5)},
            timeout=15,
        )
        self.assertIn(r.status_code, (409, 410))
        self.assertNotIn("differences", r.json())
        info = self.client.get(f"/sessions/{sid}").json()
        self.assertEqual(info["status"], "expired")

    def test_source_corrections_do_not_change_check_basis(self):
        self._reset_seed()
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        first = rows[0]
        # 会话开启后源端改金额、删理赔、补录新理赔并提交
        self.assertEqual(
            self.client.patch(
                f"/admin/claims/{first['claim_id']}",
                params={"amount": first["amount"] + 999},
            ).status_code,
            200,
        )
        second = rows[1]
        self.assertEqual(
            self.client.delete(
                f"/admin/claims/{second['claim_id']}"
            ).status_code,
            204,
        )
        pol = self.client.post(
            "/admin/policies", json={"region": "华南"}
        ).json()
        new_claim = self.client.post(
            "/admin/claims",
            json={"policy_id": pol["id"], "amount": 888},
        ).json()

        # 仍以旧快照为核对依据：按快照金额足额支付 -> 全部吻合
        txns = [
            self.pay(f"t{r['claim_id']}", r["claim_id"], r["amount"])
            for r in rows
        ]
        body = self.check_settlement(sid, txns).json()
        self.assertTrue(body["ready_for_warehouse"])
        self.assertEqual(body["snapshot_claim_count"], 40)

        # 源端新理赔在快照中不存在：对其支付会被判为无源
        r = self.check_settlement(
            sid, [self.pay("tn", new_claim["id"], 888)]
        )
        self.assertEqual(r.json()["orphan_count"], 1)
        self.close(sid)

    def test_no_region_claim_set_cached_in_process_memory(self):
        s = self.open("华南")
        sid = s["session_id"]
        rows = self.all_snapshot_rows(sid)
        # 先制造一次核对
        self.assertEqual(
            self.check_settlement(
                sid,
                [self.pay("p1", rows[0]["claim_id"], rows[0]["amount"])],
            ).status_code,
            200,
        )
        # 进程内除会话注册对象外，不得出现以理赔 id 为键的地区全集缓存：
        # 注册表中的会话对象不允许挂任何核对结果/全集字段。
        for sess in manager.list_sessions():
            self.assertFalse(
                hasattr(sess, "settlement_cache"),
                "会话对象上不得缓存地区理赔全集/核对结果",
            )
        # 再次核对结果与首次一致（现场 SQL，而非内存缓存）
        r1 = self.check_settlement(
            sid,
            [self.pay("p1", rows[0]["claim_id"], rows[0]["amount"])],
        ).json()
        r2 = self.check_settlement(
            sid,
            [self.pay("p1", rows[0]["claim_id"], rows[0]["amount"])],
        ).json()
        self.assertEqual(
            r1["total_difference_count"], r2["total_difference_count"]
        )
        self.close(sid)

    def test_connection_loss_during_check_fails_no_snapshot_swap(self):
        s = self.open("华东")
        sid = s["session_id"]
        sess = manager.get_status(sid)
        with psycopg.connect(settings.database_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(%s)",
                (sess.conn.info.backend_pid,),
            )
        deadline = time.time() + 5
        while time.time() < deadline:
            if sess.conn.closed:
                break
            time.sleep(0.1)
        r = self.check_settlement(
            sid, [self.pay("p1", 1, 100)]
        )
        self.assertIn(r.status_code, (409, 503))
        self.assertNotIn("differences", r.json())
        # 新会话仍可正常核对
        s2 = self.open("华东")
        sid2 = s2["session_id"]
        fresh = self.all_snapshot_rows(sid2)
        body = self.check_settlement(
            sid2,
            [
                self.pay(
                    f"t{r['claim_id']}", r["claim_id"], r["amount"]
                )
                for r in fresh
            ],
        ).json()
        self.assertTrue(body["ready_for_warehouse"])
        self.close(sid2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
