"""保留地址整体替换接口的端到端测试。

直接运行：``python3 -m tests.test_reservations_replace``（或 pytest 收集）。

造数：父网段 10.10.0.0/16，初始保留 10.10.0.0/20 与 10.10.255.0/24。

覆盖：
* 重叠 / 相邻 / 包含的网段被规范化为覆盖范围相同且数量最少的 CIDR；
* 空列表清空全部保留地址；
* 非法输入 422：非规范写法（主机位非零）、无法解析、越出父网段
  （含父网段的超网）、地址族与父网段不一致；
* 冲突 409：版本过期、目标范围与已分配地址相交（含完全包含某条分配
  记录、与分配记录相同）；
* 422 / 409 均不改动池状态与版本；
* 替换成功后 GET /api/pool 与后续预演/提交立即采用新保留区（新保留
  空间不可分配、被清空的保留空间立即可分配）；
* 同版本并发替换：写锁互斥，恰有一个成功。
"""
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reservations_replace.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)

INITIAL_RESERVED = ["10.10.0.0/20", "10.10.255.0/24"]


def _pool():
    return client.get("/api/pool").json()


def _replace(version, desired):
    return client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": desired},
    )


def _preview(prefixes):
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": prefixes}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True, body
    return body


def _commit(version, allocations):
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": version, "allocations": allocations},
    )
    assert r.status_code == 200, r.text
    return r.json()


def main():
    pool = _pool()
    assert pool["version"] == 1
    assert pool["reserved_cidrs"] == INITIAL_RESERVED

    # ---- 非法输入 -> 422，池状态与版本不变 ----
    for bad in (
        ["10.10.8.1/24"],   # 主机位非零，非规范写法
        ["not-a-cidr"],     # 无法解析
        ["10.11.0.0/24"],   # 越出父网段
        ["10.10.0.0/15"],   # 父网段的超网，同样越界
        ["2001:db8::/48"],  # 地址族与父网段不一致
        ["10.10.8.0/24", "10.11.0.0/24"],  # 任一元素非法即整体 422
    ):
        r = _replace(1, bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    pool = _pool()
    assert pool["version"] == 1
    assert pool["reserved_cidrs"] == INITIAL_RESERVED
    print("invalid input -> 422, pool untouched")

    # ---- 重叠 + 相邻 + 包含 -> 规范化为最少 CIDR ----
    # 10.10.128.128/25 被 10.10.128.0/24 包含；10.10.129.0/24 与
    # 10.10.128.0/24 相邻（合成 /23）；两个 /25 相邻（合成 /24）。
    desired = [
        "10.10.128.0/24",
        "10.10.128.128/25",
        "10.10.129.0/24",
        "10.10.130.0/25",
        "10.10.130.128/25",
    ]
    r = _replace(1, desired)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "replaced"
    assert body["pool_version"] == 2
    assert body["reserved_cidrs"] == ["10.10.128.0/23", "10.10.130.0/24"], body
    print("overlap/adjacent/contained normalized to minimal CIDRs ok")

    # ---- GET /api/pool 立即反映新保留区 ----
    pool = _pool()
    assert pool["version"] == 2
    assert pool["reserved_cidrs"] == body["reserved_cidrs"]
    assert pool["free_cidrs"] == body["free_cidrs"]
    assert "10.10.0.0/20" not in pool["reserved_cidrs"]
    assert "10.10.255.0/24" not in pool["reserved_cidrs"]

    # ---- 预演/提交立即采用新保留区：/24 最优落位 10.10.131.0/24 ----
    # （旧保留区下最优为 10.10.254.0/24；新保留区恰好留出 131.0/24 整块，
    # 分配后不产生碎片。）
    plan = _preview([24])
    assert plan["pool_version"] == 2
    assert plan["allocations"] == ["10.10.131.0/24"], plan
    committed = _commit(2, plan["allocations"])
    assert committed["pool_version"] == 3
    print("preview/commit use new reservations immediately ok")

    # ---- 目标范围与已分配地址相交 -> 409，池状态与版本不变 ----
    before = _pool()
    for conflict in (
        ["10.10.131.0/25"],   # 与已分配 10.10.131.0/24 相交
        ["10.10.130.0/23"],   # 完全包含已分配块
        ["10.10.131.0/24"],   # 与已分配块相同
        ["10.10.200.0/24", "10.10.131.128/25"],  # 任一元素相交即整体冲突
    ):
        r = _replace(3, list(conflict))
        assert r.status_code == 409, (conflict, r.status_code, r.text)
        assert "overlap" in r.json()["detail"]
    assert _pool() == before
    print("overlap with allocated -> 409, pool untouched")

    # ---- 版本过期 -> 409，池状态与版本不变 ----
    r = _replace(2, ["10.10.200.0/24"])
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    assert _pool() == before
    print("stale version -> 409, pool untouched")

    # ---- 空列表清空全部保留地址 ----
    r = _replace(3, [])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "replaced"
    assert body["reserved_cidrs"] == []
    assert body["pool_version"] == 4
    pool = _pool()
    assert pool["version"] == 4
    assert pool["reserved_cidrs"] == []
    assert pool["free_cidrs"] == body["free_cidrs"]
    print("empty list clears all reservations ok")

    # ---- 被清空的保留空间立即可分配 ----
    # 空闲块 128/129/130 三个相邻 /24（130 与已分配 131 相邻）：分配
    # 130.0/24 后 128+129 合并为 /23，碎片最少。
    plan = _preview([24])
    assert plan["pool_version"] == 4
    assert plan["allocations"] == ["10.10.130.0/24"], plan
    committed = _commit(4, plan["allocations"])
    assert committed["pool_version"] == 5
    assert _pool()["allocated_cidrs"] == ["10.10.130.0/24", "10.10.131.0/24"]
    print("cleared space reusable in preview + commit ok")

    # ---- 同版本并发替换：写锁互斥，恰有一个成功 ----
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx, desired_set):
        barrier.wait()
        results[idx] = _replace(5, desired_set)

    t0 = threading.Thread(target=worker, args=(0, ["10.10.200.0/24"]))
    t1 = threading.Thread(target=worker, args=(1, ["10.10.201.0/24"]))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    winner = next(r for r in results.values() if r.status_code == 200)
    pool = _pool()
    assert pool["version"] == 6  # 只自增一次
    assert pool["reserved_cidrs"] == winner.json()["reserved_cidrs"]
    assert pool["allocated_cidrs"] == ["10.10.130.0/24", "10.10.131.0/24"]
    print("concurrent replace: exactly one succeeds ok")

    print("ALL RESERVATIONS REPLACE TESTS PASSED")


if __name__ == "__main__":
    main()
