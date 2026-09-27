"""API 端到端测试：临时 SQLite 数据库 + FastAPI TestClient。"""
import os
import tempfile

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def main():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    pool = client.get("/api/pool").json()
    assert pool["parent_cidr"] == "10.10.0.0/16"
    assert pool["version"] == 1
    assert pool["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    print("initial pool:", pool["free_cidr_count"], "free CIDRs")

    # 参数校验：0 个请求
    r = client.post("/api/allocations/preview", json={"prefix_lengths": []})
    assert r.status_code == 422, r.text
    # 参数校验：9 个请求
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": [24] * 9}
    )
    assert r.status_code == 422
    # 非法前缀长度
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": [129]}
    )
    assert r.status_code == 422

    # 预演：3 个请求
    r = client.post(
        "/api/allocations/preview",
        json={"prefix_lengths": [24, 24, 23]},
    )
    assert r.status_code == 200, r.text
    plan = r.json()
    print("preview:", plan)
    assert plan["feasible"] is True
    assert plan["pool_version"] == 1
    assert len(plan["allocations"]) == 3
    v1_allocs = plan["allocations"]

    # 预演不改变状态
    assert client.get("/api/pool").json()["version"] == 1

    # 用过期版本提交 -> 409（先造版本 2：提交一个不相关小方案）
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": [28]}
    )
    small = r.json()
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": 1, "allocations": small["allocations"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pool_version"] == 2

    # 旧方案以旧版本提交 -> 409
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": 1, "allocations": v1_allocs},
    )
    assert r.status_code == 409, r.text
    print("stale version -> 409 ok")

    # 数据库中只有 small 被写入，没有部分分配
    pool2 = client.get("/api/pool").json()
    assert small["allocations"][0] in pool2["allocated_cidrs"]
    for cidr in v1_allocs:
        assert cidr not in pool2["allocated_cidrs"]

    # 版本正确但方案已被占用（与已分配块重叠）-> 409
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": 2, "allocations": small["allocations"]},
    )
    assert r.status_code == 409
    pool_after_failed = client.get("/api/pool").json()
    assert pool_after_failed["version"] == 2  # 冲突不自增版本
    print("unavailable plan -> 409 ok")

    # 重新预演并原子提交 3 个请求
    r = client.post(
        "/api/allocations/preview",
        json={"prefix_lengths": [24, 24, 23]},
    )
    plan3 = r.json()
    assert plan3["pool_version"] == 2
    r = client.post(
        "/api/allocations/commit",
        json={
            "pool_version": 2,
            "allocations": plan3["allocations"],
        },
    )
    assert r.status_code == 200, r.text
    committed = r.json()
    assert committed["status"] == "committed"
    assert committed["pool_version"] == 3
    print("committed:", committed["allocations"])
    pool3 = client.get("/api/pool").json()
    assert pool3["version"] == 3
    assert len(pool3["allocated_cidrs"]) == 4  # small + 3

    # 不可行请求：空间不足
    r = client.post(
        "/api/allocations/preview", json={"prefix_lengths": [8]}
    )
    body = r.json()
    assert body["feasible"] is False and body["allocations"] == []
    print("infeasible preview handled:", body["reason"])

    # 幂等性检查：重启应用（同一 DB 文件）版本与数据保留
    from app import main as main_mod

    with TestClient(app) as client2:
        p = client2.get("/api/pool").json()
        assert p["version"] == 3 and len(p["allocated_cidrs"]) == 4
    print("persistence across restart ok")

    print("ALL API TESTS PASSED")


if __name__ == "__main__":
    main()
