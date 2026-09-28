"""历史版本快照接口的端到端测试。

直接运行：``python3 -m tests.test_versions``（或 pytest 收集）。

覆盖：
* 首次启动即存在版本 1 基线快照，内容与当前池状态一致；
* 分配提交 / 部分回收 / 整批迁移 / 保留区替换成功后，各自写事务内
  生成新版本快照，内容与操作响应中的状态一致；
* 失败操作（409 冲突）不生成任何版本——缺失版本查询返回 404；
* 未知版本返回 404（含 0、负数），非整数版本号返回 422；
* 旧快照只追加、不更新：后续多轮写入后，每个旧版本返回的父网段 /
  保留 / 已分配 / 空闲 CIDR 与该版本生成时逐字节一致（含保留区替换
  后旧版本仍保留旧保留区）；
* 旧库**首次升级**：手工构造只有旧表结构的数据库（版本已自增过），
  Database 初始化时给当前版本补存基线，更早的版本仍为 404，之后的
  成功写入继续追加新版本且基线不变；
* 重启（同一 DB 文件重新初始化）后历史快照仍然可查。
"""
from __future__ import annotations

import ipaddress
import os
import sqlite3
import tempfile

PARENT = "10.10.0.0/16"
RESERVED = "10.10.0.0/20,10.10.255.0/24"

os.environ["PARENT_CIDR"] = PARENT
os.environ["RESERVED_CIDRS"] = RESERVED
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_versions.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


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


def _version(version):
    return client.get(f"/api/pool/versions/{version}")


def _overlaps(cidr, cidrs):
    net = ipaddress.ip_network(cidr)
    return any(net.overlaps(ipaddress.ip_network(c)) for c in cidrs)


def main():
    # ---- 基线：版本 1 在首次启动时即可查询，与当前池状态完全一致 ----
    current = client.get("/api/pool").json()
    assert current["version"] == 1
    r = _version(1)
    assert r.status_code == 200, r.text
    v1 = r.json()
    assert v1 == current
    assert v1["parent_cidr"] == PARENT
    assert v1["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    assert v1["allocated_cidrs"] == []
    assert v1["free_cidr_count"] == len(v1["free_cidrs"])
    snapshots = {1: v1}
    print("baseline snapshot for current version ok")

    # ---- 未知版本 404（含 0 与负数）；非整数版本号 422 ----
    for unknown in (0, 2, 999, -2):
        r = _version(unknown)
        assert r.status_code == 404, (unknown, r.status_code, r.text)
        assert "unknown pool version" in r.json()["detail"]
    r = client.get("/api/pool/versions/abc")
    assert r.status_code == 422, r.text
    print("unknown version -> 404, non-integer -> 422 ok")

    # ---- 成功提交 -> v2 快照；失败提交不生成版本 ----
    plan = _preview([24])
    assert plan["pool_version"] == 1
    a1 = plan["allocations"]
    committed = _commit(1, a1)
    assert committed["pool_version"] == 2
    r = _version(2)
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2
    assert v2["allocated_cidrs"] == a1
    assert v2["reserved_cidrs"] == snapshots[1]["reserved_cidrs"]
    assert a1[0] not in v2["free_cidrs"]
    assert v2["free_cidr_count"] == len(v2["free_cidrs"])
    snapshots[2] = v2

    # 过期版本提交同一方案 -> 409，不产生 v3。
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": 1, "allocations": a1},
    )
    assert r.status_code == 409, r.text
    assert _version(3).status_code == 404
    # 当前版本仍是 2。
    assert client.get("/api/pool").json()["version"] == 2
    print("commit snapshot + failed commit creates no version ok")

    # ---- 部分回收 -> v3 快照（回收整块后占用集回到空）----
    r = client.post(
        "/api/allocations/reclaim",
        json={"pool_version": 2, "cidr": a1[0]},
    )
    assert r.status_code == 200, r.text
    reclaimed = r.json()
    assert reclaimed["pool_version"] == 3
    r = _version(3)
    assert r.status_code == 200, r.text
    v3 = r.json()
    assert v3["allocated_cidrs"] == []
    assert v3["allocated_cidrs"] == reclaimed["allocated_cidrs"]
    assert v3["free_cidrs"] == reclaimed["free_cidrs"]
    # 内容上 v3 与 v1 的池状态相同（同一父网段/保留/空占用）。
    assert v3["parent_cidr"] == v1["parent_cidr"]
    assert v3["reserved_cidrs"] == v1["reserved_cidrs"]
    assert v3["free_cidrs"] == v1["free_cidrs"]
    snapshots[3] = v3

    # 失败回收（空池上回收）-> 409，不产生 v4。
    r = client.post(
        "/api/allocations/reclaim",
        json={"pool_version": 3, "cidr": a1[0]},
    )
    assert r.status_code == 409, r.text
    assert _version(4).status_code == 404
    print("reclaim snapshot + failed reclaim creates no version ok")

    # ---- 再次提交 -> v4 ----
    plan = _preview([24])
    assert plan["pool_version"] == 3
    a2 = plan["allocations"]
    committed = _commit(3, a2)
    assert committed["pool_version"] == 4
    v4 = _version(4).json()
    assert v4["allocated_cidrs"] == a2
    assert v4["free_cidrs"] == committed["free_cidrs"]
    snapshots[4] = v4

    # ---- 整批迁移（原地迁移 from == to）-> v5 快照，占用集不变 ----
    r = client.post(
        "/api/allocations/relocate",
        json={
            "pool_version": 4,
            "moves": [{"from_cidr": a2[0], "to_cidr": a2[0]}],
        },
    )
    assert r.status_code == 200, r.text
    relocated = r.json()
    assert relocated["pool_version"] == 5
    v5 = _version(5).json()
    assert v5["allocated_cidrs"] == a2
    assert v5["free_cidrs"] == relocated["free_cidrs"]
    snapshots[5] = v5

    # 冲突迁移（来源含未分配地址）-> 409，不产生 v6。
    r = client.post(
        "/api/allocations/relocate",
        json={
            "pool_version": 5,
            "moves": [
                {"from_cidr": "10.10.252.0/24", "to_cidr": "10.10.253.0/24"}
            ],
        },
    )
    assert r.status_code == 409, r.text
    assert _version(6).status_code == 404
    print("relocate snapshot + failed relocate creates no version ok")

    # ---- 保留区替换 -> v6 快照：新版本采用新保留区，旧版本保留旧保留区 ----
    # 选一个不与当前占用相交的空闲 /24 作为新保留区。
    new_reserved = next(
        c
        for c in ("10.10.200.0/24", "10.10.50.0/24", "10.10.99.0/24")
        if not _overlaps(c, a2)
    )
    r = client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": 5, "desired_cidrs": [new_reserved]},
    )
    assert r.status_code == 200, r.text
    replaced = r.json()
    assert replaced["pool_version"] == 6
    assert replaced["reserved_cidrs"] == [new_reserved]
    v6 = _version(6).json()
    assert v6["reserved_cidrs"] == [new_reserved]
    assert v6["allocated_cidrs"] == a2
    assert v6["free_cidrs"] == replaced["free_cidrs"]
    snapshots[6] = v6

    # 冲突替换（新保留区压在已分配地址上）-> 409，不产生 v7。
    r = client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": 6, "desired_cidrs": a2},
    )
    assert r.status_code == 409, r.text
    assert _version(7).status_code == 404
    assert client.get("/api/pool").json()["version"] == 6
    print("replace snapshot + failed replace creates no version ok")

    # ---- 旧快照不随后续写入变化：逐版本与生成时的响应逐字段比较 ----
    for version, old in snapshots.items():
        got = _version(version).json()
        assert got == old, (
            f"snapshot of version {version} changed after later writes:\n"
            f"then={old}\nnow ={got}"
        )
    # 特别地：保留区替换后，v1 仍返回初始保留区与初始空闲 CIDR。
    assert _version(1).json()["reserved_cidrs"] == [
        "10.10.0.0/20",
        "10.10.255.0/24",
    ]
    print("old snapshots remain immutable after later writes ok")

    # ---- 重启（同一 DB 文件重新初始化）后历史快照仍可查 ----
    Database(
        Settings(
            db_path=os.environ["DB_PATH"],
            parent_cidr=PARENT,
            reserved_cidrs_raw=RESERVED,
        )
    )
    for version, old in snapshots.items():
        got = _version(version).json()
        assert got == old, f"snapshot {version} lost after restart"
    print("snapshots survive restart ok")

    # ---- 旧库首次升级：只给当前版本补基线，更早版本仍未知 ----
    legacy_path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    with sqlite3.connect(legacy_path) as raw:
        raw.executescript(
            """
            CREATE TABLE pool_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                parent_cidr TEXT NOT NULL,
                version    INTEGER NOT NULL
            );
            CREATE TABLE subnets (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                cidr       TEXT NOT NULL UNIQUE,
                kind       TEXT NOT NULL CHECK (kind IN ('reserved', 'allocated')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            """
        )
        raw.execute(
            "INSERT INTO pool_meta (id, parent_cidr, version) "
            "VALUES (1, '10.10.0.0/16', 3)"
        )
        raw.executemany(
            "INSERT INTO subnets (cidr, kind) VALUES (?, ?)",
            [
                ("10.10.0.0/20", "reserved"),
                ("10.10.255.0/24", "reserved"),
                ("10.10.16.0/24", "allocated"),
            ],
        )
        raw.commit()

    legacy = Database(
        Settings(
            db_path=legacy_path,
            parent_cidr=PARENT,
            reserved_cidrs_raw=RESERVED,
        )
    )
    # 首次升级：仅当前版本 v3 有基线，v1/v2/v4 未知。
    assert legacy.get_state_version(1) is None
    assert legacy.get_state_version(2) is None
    assert legacy.get_state_version(4) is None
    baseline = legacy.get_state_version(3)
    assert baseline is not None
    live = legacy.get_state()
    assert live.version == 3
    assert str(baseline.parent) == str(live.parent) == PARENT
    assert tuple(str(n) for n in baseline.reserved) == (
        "10.10.0.0/20",
        "10.10.255.0/24",
    )
    assert tuple(str(n) for n in baseline.allocated) == ("10.10.16.0/24",)

    # 升级后的成功写入继续在同一事务追加新版本快照，基线保持不变。
    after = legacy.commit_plan(
        3, (ipaddress.ip_network("10.10.32.0/24", strict=True),)
    )
    assert after.version == 4
    v4_legacy = legacy.get_state_version(4)
    assert v4_legacy is not None
    assert tuple(str(n) for n in v4_legacy.allocated) == (
        "10.10.16.0/24",
        "10.10.32.0/24",
    )
    assert tuple(
        str(n) for n in legacy.get_state_version(3).allocated
    ) == ("10.10.16.0/24",)
    # 再次初始化幂等：不会重复补存或报错。
    Database(
        Settings(
            db_path=legacy_path,
            parent_cidr=PARENT,
            reserved_cidrs_raw=RESERVED,
        )
    )
    assert legacy.get_state_version(3) is not None
    print("legacy DB first-upgrade baseline backfill ok")

    print("ALL VERSION TESTS PASSED")


if __name__ == "__main__":
    main()
