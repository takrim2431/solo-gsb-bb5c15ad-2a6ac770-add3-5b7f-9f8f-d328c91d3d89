"""并发交错回归测试：预演/池状态快照一致性与提交响应绑定。

背景
====
修复前存在两处“版本与内容错位”：

1. ``get_state`` 在两条独立 SELECT 中分别读取版本与子网（各自的隐式
   读事务）。并发提交若落在两条查询之间，会返回“旧版本 + 新占用集”，
   预演与池状态接口都会据此给出错位方案。
2. ``commit_plan`` 在写事务提交后**另开新连接**读取最终状态。若另一笔
   提交在重新读取前落库，提交响应中的 ``pool_version`` / ``free_cidrs``
   会变成后来那笔提交的结果。

本文件通过 ``Database`` 上的两个默认空操作钩子精确控制并发交错：

* ``_after_meta_select``：读事务内版本查询完成、子网查询开始之前；
* ``_after_commit``：写事务 COMMIT 完成之后。

直接运行：``python3 -m tests.test_concurrency``（或 pytest 收集）。
"""
from __future__ import annotations

import ipaddress
import os
import tempfile
import threading

try:
    import pytest
except ImportError:  # 允许在仅装运行时依赖时直接 python3 -m 运行
    class _PytestShim:
        @staticmethod
        def fixture(*args, **kwargs):
            if args and callable(args[0]):
                return args[0]

            def decorate(fn):
                return fn

            return decorate

    pytest = _PytestShim()  # type: ignore[assignment]

from app.allocator import CidrAllocationSolver
from app.config import Settings
from app.db import ConflictError, Database

PARENT = "10.0.0.0/16"
RESERVED = "10.0.255.0/24"


def _fresh_db() -> Database:
    """使用全新临时库构造 Database（初始版本 1，含 RESERVED）。"""
    db_path = os.path.join(tempfile.mkdtemp(), "pool.db")
    settings = Settings(
        db_path=db_path,
        parent_cidr=PARENT,
        reserved_cidrs_raw=RESERVED,
    )
    return Database(settings)


def _free_cidrs(state) -> list[str]:
    occupied = list(state.reserved) + list(state.allocated)
    return [
        str(n)
        for n in CidrAllocationSolver(state.parent, occupied, ()).free_cidrs
    ]


def _assert_state_internally_consistent(state) -> None:
    """版本、占用集、空闲网段必须自洽：allocated 必须真正被算作占用。"""
    free = _free_cidrs(state)
    for net in state.allocated:
        for raw in free:
            assert not net.overlaps(ipaddress.ip_network(raw)), (
                f"version {state.version} 的状态自相矛盾：已分配 {net} "
                f"仍出现在空闲网段 {raw} 中（旧版本配新占用集的错位读取）"
            )


def _is_free(cidr: str, free_cidrs: list[str]) -> bool:
    """cidr 完整落在某个规范空闲块内（空闲块是最大块，需按包含判断）。"""
    net = ipaddress.ip_network(cidr)
    return any(
        net.subnet_of(ipaddress.ip_network(free)) for free in free_cidrs
    )


def _overlaps_any(cidr: str, cidrs: list[str]) -> bool:
    net = ipaddress.ip_network(cidr)
    return any(net.overlaps(ipaddress.ip_network(c)) for c in cidrs)


@pytest.fixture
def db():
    return _fresh_db()


# ---------------------------------------------------------------------------
# 1. 读快照：版本与子网必须来自同一 SQLite 快照
# ---------------------------------------------------------------------------

def test_get_state_snapshot_does_not_tear_across_commit(db, monkeypatch):
    """读者读完版本后阻塞，写者完成提交后再读子网：结果必须整体为 v1。

    旧实现会返回 version=1 却包含写者新插入的子网（撕裂读取）。
    """
    new_subnet = "10.0.1.0/24"
    reader_ready = threading.Event()
    writer_done = threading.Event()
    errors: list[BaseException] = []

    def after_meta_select(conn):
        # 仅读者连接在读取版本后放行写者，避免拦到写者自己的查询。
        if threading.current_thread() is reader:
            reader_ready.set()
            assert writer_done.wait(timeout=10)

    monkeypatch.setattr(
        Database, "_after_meta_select", staticmethod(after_meta_select)
    )

    reader_result = {}

    def reader_fn():
        try:
            reader_result["state"] = db.get_state()
        except BaseException as exc:  # noqa: BLE001 - 回传线程内异常
            errors.append(exc)

    reader = threading.Thread(target=reader_fn)
    reader.start()
    assert reader_ready.wait(timeout=10)

    # 读者持有 v1 快照期间完成一笔提交（v1 -> v2）。
    committed = db.commit_plan(
        1, (ipaddress.ip_network(new_subnet, strict=True),)
    )
    assert committed.version == 2
    writer_done.set()
    reader.join(timeout=10)
    assert not reader.is_alive()
    assert errors == []

    state = reader_result["state"]
    # 快照必须整体属于 v1：版本号与占用集都不能混入 v2 的内容。
    assert state.version == 1
    assert state.allocated == ()
    assert new_subnet not in {str(n) for n in state.reserved}
    _assert_state_internally_consistent(state)

    # 快照之后再次读取即看到已提交的 v2。
    latest = db.get_state()
    assert latest.version == 2
    assert str(latest.allocated[0]) == new_subnet
    _assert_state_internally_consistent(latest)


def test_preview_and_pool_share_version_and_subnets(db, monkeypatch):
    """通过 HTTP 验证预演与池状态接口在同一快照内读取版本+子网。"""
    from fastapi.testclient import TestClient

    from app.main import app, get_db

    def override_get_db():
        return db

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        # 基线：v1 下预演一个 /24（最优解贴保留段，碎片最少）。
        before = client.post(
            "/api/allocations/preview", json={"prefix_lengths": [24]}
        ).json()
        assert before["pool_version"] == 1
        assert len(before["allocations"]) == 1
        v1_alloc = before["allocations"][0]

        reader_ready = threading.Event()
        writer_done = threading.Event()
        errors: list[BaseException] = []
        # FastAPI 同步端点运行在 portal 线程池中，无法按线程身份判别；
        # 用闸门开关让钩子只在目标请求（worker 的预演）内生效一次。
        armed = {"on": False}

        def after_meta_select(conn):
            if armed["on"]:
                armed["on"] = False
                reader_ready.set()
                assert writer_done.wait(timeout=10)

        monkeypatch.setattr(
            Database, "_after_meta_select", staticmethod(after_meta_select)
        )

        # 写者在读者读取版本之后占用 v1 的最优方案地址。
        results = {}

        def preview_worker():
            try:
                results["preview"] = client.post(
                    "/api/allocations/preview",
                    json={"prefix_lengths": [24]},
                ).json()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=preview_worker)
        armed["on"] = True
        worker.start()
        assert reader_ready.wait(timeout=10)
        db.commit_plan(1, (ipaddress.ip_network(v1_alloc),))
        writer_done.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []

        preview = results["preview"]
        # 预演基于 v1 快照：版本与方案都必须是 v1 的，
        # 不能用 v1 版本号搭配 v2 占用集给出另一个方案。
        assert preview["pool_version"] == 1
        assert preview["allocations"] == [v1_alloc]
        assert preview["free_cidrs_after"] == before["free_cidrs_after"]

        # ---- 第二段交错：池状态接口（此时库已在 v2）----
        # 读者读完版本 (v2) 后阻塞，写者再提交一笔 (v2 -> v3)。
        plan2 = client.post(
            "/api/allocations/preview", json={"prefix_lengths": [24]}
        ).json()
        assert plan2["pool_version"] == 2
        v2_alloc = plan2["allocations"][0]

        reader_ready.clear()
        writer_done.clear()
        results.clear()

        def pool_worker():
            try:
                results["pool"] = client.get("/api/pool").json()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=pool_worker)
        armed["on"] = True
        worker.start()
        assert reader_ready.wait(timeout=10)
        db.commit_plan(2, (ipaddress.ip_network(v2_alloc),))
        writer_done.set()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert errors == []

        pool = results["pool"]
        # 池状态必须整体为 v2：v1_alloc 已占用（与所有空闲块不重叠），
        # v2_alloc 仍完整落在某个空闲块内。
        assert pool["version"] == 2
        assert pool["allocated_cidrs"] == [v1_alloc]
        assert not _overlaps_any(v1_alloc, pool["free_cidrs"])
        assert _is_free(v2_alloc, pool["free_cidrs"])
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 2. 提交响应：版本与分配结果必须绑定本次写事务
# ---------------------------------------------------------------------------

def test_commit_response_is_bound_to_own_transaction(db, monkeypatch):
    """第一笔提交 COMMIT 后阻塞，第二笔提交先完成；首笔响应仍须是 v2。

    旧实现在提交后另开连接读取状态，首笔会错误地返回 v3 与第二笔的
    空闲网段（本次分配 10.0.0.0/24 却报告其已被占用）。
    """
    first_alloc = (ipaddress.ip_network("10.0.0.0/24"),)
    second_alloc = (ipaddress.ip_network("10.0.1.0/24"),)

    first_committed = threading.Event()
    second_turn = threading.Event()
    results = {}
    errors: list[BaseException] = []

    def after_commit(conn):
        if threading.current_thread() is first:
            first_committed.set()
            assert second_turn.wait(timeout=10)

    monkeypatch.setattr(
        Database, "_after_commit", staticmethod(after_commit)
    )

    def first_fn():
        try:
            results["first"] = db.commit_plan(1, first_alloc)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=first_fn)
    first.start()
    assert first_committed.wait(timeout=10)

    # 首笔已 COMMIT（v1 -> v2）但尚未组装响应；第二笔提交落库（v2 -> v3）。
    second_state = db.commit_plan(2, second_alloc)
    assert second_state.version == 3
    second_turn.set()
    first.join(timeout=10)
    assert not first.is_alive()
    assert errors == []

    state = results["first"]
    # 响应必须严格对应本次写事务：v2，仅含本次分配。
    assert state.version == 2
    assert tuple(str(n) for n in state.allocated) == ("10.0.0.0/24",)
    free = _free_cidrs(state)
    # 本次分配的网段不能出现在响应的空闲网段里；第二笔尚未发生，
    # 其占用的 10.0.1.0/24 此时仍应空闲。
    assert not _overlaps_any("10.0.0.0/24", free)
    assert _is_free("10.0.1.0/24", free)
    _assert_state_internally_consistent(state)


def test_commit_response_via_http_is_bound_to_own_transaction(
    db, monkeypatch
):
    """HTTP 层验证：并发提交后首笔响应的版本/空闲网段不被第二笔污染。"""
    from fastapi.testclient import TestClient

    from app.main import app, get_db

    def override_get_db():
        return db

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)

        first_ready = threading.Event()
        second_turn = threading.Event()
        results = {}
        errors: list[BaseException] = []
        # 端点运行于 portal 线程池：用闸门开关让钩子只拦首笔提交。
        armed = {"on": False}

        def after_commit(conn):
            if armed["on"]:
                armed["on"] = False
                first_ready.set()
                assert second_turn.wait(timeout=10)

        monkeypatch.setattr(
            Database, "_after_commit", staticmethod(after_commit)
        )

        def first_fn():
            try:
                # 重新预演得到当前 (v1) 最优方案并提交。
                plan = client.post(
                    "/api/allocations/preview",
                    json={"prefix_lengths": [24]},
                ).json()
                results["plan1"] = plan["allocations"]
                results["first"] = client.post(
                    "/api/allocations/commit",
                    json={
                        "pool_version": plan["pool_version"],
                        "allocations": plan["allocations"],
                    },
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        first = threading.Thread(target=first_fn)
        armed["on"] = True
        first.start()
        assert first_ready.wait(timeout=10)

        # 第二笔：预演时首笔已提交（v2），得到次优地址。
        plan2 = client.post(
            "/api/allocations/preview", json={"prefix_lengths": [24]}
        ).json()
        assert plan2["pool_version"] == 2
        assert plan2["allocations"] != results["plan1"]
        resp2 = client.post(
            "/api/allocations/commit",
            json={
                "pool_version": 2,
                "allocations": plan2["allocations"],
            },
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["pool_version"] == 3
        second_turn.set()
        first.join(timeout=10)
        assert not first.is_alive()
        assert errors == []

        body = results["first"].json()
        assert results["first"].status_code == 200, results["first"].text
        # 首笔响应绑定自己的写事务：v2 + 自己的分配与空闲网段。
        assert body["status"] == "committed"
        assert body["pool_version"] == 2
        assert body["allocations"] == results["plan1"]
        assert not _overlaps_any(results["plan1"][0], body["free_cidrs"])
        assert _is_free(plan2["allocations"][0], body["free_cidrs"])
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 3. 同版本并发提交：写锁互斥，恰有一笔成功（保留既有原子性保证）
# ---------------------------------------------------------------------------

def test_concurrent_commits_same_version_exactly_one_succeeds(db):
    """两个线程基于同一 v1 预演结果并发提交同一方案：一成功一 409。"""
    alloc = (ipaddress.ip_network("10.0.0.0/24"),)
    barrier = threading.Barrier(2)
    outcomes: dict[int, object] = {}
    lock = threading.Lock()

    def worker(idx):
        barrier.wait()
        try:
            state = db.commit_plan(1, alloc)
            outcome = ("ok", state.version)
        except ConflictError as exc:
            outcome = ("conflict", str(exc))
        with lock:
            outcomes[idx] = outcome

    t0 = threading.Thread(target=worker, args=(0,))
    t1 = threading.Thread(target=worker, args=(1,))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()

    summary = sorted(outcomes.values(), key=lambda x: x[0])
    assert [kind for kind, _ in summary] == ["conflict", "ok"]
    assert dict(summary)["ok"] == 2

    final = db.get_state()
    assert final.version == 2
    assert tuple(str(n) for n in final.allocated) == ("10.0.0.0/24",)
    _assert_state_internally_consistent(final)


def main() -> None:
    # 允许无 pytest 时直接运行：``python3 -m tests.test_concurrency``。
    tests = [
        test_get_state_snapshot_does_not_tear_across_commit,
        test_preview_and_pool_share_version_and_subnets,
        test_commit_response_is_bound_to_own_transaction,
        test_commit_response_via_http_is_bound_to_own_transaction,
        test_concurrent_commits_same_version_exactly_one_succeeds,
    ]
    for fn in tests:
        kwargs = {"db": _fresh_db()}
        if "monkeypatch" in fn.__code__.co_varnames:

            class _MP:
                def __init__(self):
                    self._saved = {}

                def setattr(self, obj, name, value):
                    # 通过 __dict__ 取原始描述符，保留 staticmethod 语义。
                    self._saved[(obj, name)] = obj.__dict__[name]
                    setattr(obj, name, value)

                def undo(self):
                    for (obj, name), value in self._saved.items():
                        setattr(obj, name, value)
                    self._saved.clear()

            mp = _MP()
            kwargs["monkeypatch"] = mp
        try:
            fn(**kwargs)
        finally:
            if "monkeypatch" in kwargs:
                kwargs["monkeypatch"].undo()
        print(f"PASS {fn.__name__}")
    print("ALL CONCURRENCY TESTS PASSED")


if __name__ == "__main__":
    main()
