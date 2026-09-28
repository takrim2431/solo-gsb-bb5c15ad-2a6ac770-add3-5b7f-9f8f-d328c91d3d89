"""历史版本查询接口的端到端测试。

直接运行：``python3 -m tests.test_versions``（或 pytest 收集）。

覆盖：
* 全新建库即存在版本 1 基线，与当前池状态完全一致；
* 提交 / 回收 / 迁移 / 保留区替换成功后，新版本快照在同一写事务内
  生成（内容等于当时 ``GET /api/pool`` 的结果）；
* 失败操作（409/422）不生成版本：版本号出现空洞时查询空洞得到 404；
* 旧快照不可变：后续写入只改变当前池，历史版本的父网段 / 保留 /
  已分配 / 空闲 CIDR 保持首次落库时的内容；
* 未知版本返回 404；
* 老库首次升级：仅有旧表（pool_meta/subnets）、当前版本不为 1 时，
  升级后只为**当前**版本补存基线，更早的版本仍为 404。
"""
import ipaddress
import os
import sqlite3
import tempfile

_tmp = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp, "test_versions.db")
os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
os.environ["DB_PATH"] = DB_PATH

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def _get(version):
    return client.get(f"/api/pool/versions/{version}")


def _pool():
    return client.get("/api/pool").json()


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


def _reclaim(version, cidr):
    return client.post(
        "/api/allocations/reclaim",
        json={"pool_version": version, "cidr": cidr},
    )


def _replace(version, cidrs):
    return client.post(
        "/api/pool/reservations/replace",
        json={"pool_version": version, "desired_cidrs": cidrs},
    )


def _relocate(version, moves):
    return client.post(
        "/api/allocations/relocate",
        json={"pool_version": version, "moves": moves},
    )


def _snapshot(version):
    """返回版本快照响应体，断言存在（200）。"""
    r = _get(version)
    assert r.status_code == 200, r.text
    return r.json()


def _assert_snapshot_equals_pool(version):
    snap = _snapshot(version)
    pool = _pool()
    if pool["version"] == version:
        # 仅当查询的恰为当前版本时才要求与当前池完全一致。
        assert snap == pool, (snap, pool)
    return snap


def main():
    # ---- 基线：版本 1 在首次建库时即存在，与当前池一致 ----
    pool1 = _pool()
    assert pool1["version"] == 1
    snap1 = _snapshot(1)
    assert snap1 == pool1
    assert snap1["parent_cidr"] == "10.10.0.0/16"
    assert snap1["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    assert snap1["allocated_cidrs"] == []
    assert snap1["free_cidr_count"] == len(snap1["free_cidrs"])
    print("baseline snapshot v1 ok:", snap1["free_cidr_count"], "free CIDRs")

    # 未知版本 -> 404（未来版本 / 0 / 负数均为“无此版本”）。
    assert _get(2).status_code == 404
    assert _get(999).status_code == 404
    assert _get(0).status_code == 404
    assert client.get("/api/pool/versions/-1").status_code == 404
    # 非整数路径段无法解析为版本号 -> 422（路由校验）。
    assert client.get("/api/pool/versions/abc").status_code == 422
    print("unknown version -> 404 ok")

    # ---- 一次成功提交：v1 -> v2，快照在同一事务内生成 ----
    plan = _preview([24, 24])
    a1, a2 = plan["allocations"]
    committed = _commit(1, [a1, a2])
    assert committed["pool_version"] == 2
    pool2 = _pool()
    assert pool2["version"] == 2
    snap2 = _snapshot(2)
    assert snap2 == pool2
    assert snap2["allocated_cidrs"] == [a1, a2]
    assert snap2["reserved_cidrs"] == snap1["reserved_cidrs"]
    # 旧基线不随本次写入改变。
    assert _snapshot(1) == snap1
    print("commit snapshot v2 ok:", snap2["allocated_cidrs"])

    # ---- 失败提交（409，方案已占用）不生成版本：v3 缺失 -> 404 ----
    r = client.post(
        "/api/allocations/commit",
        json={"pool_version": 2, "allocations": [a1]},  # a1 已被占用
    )
    assert r.status_code == 409
    assert _pool()["version"] == 2
    assert _get(3).status_code == 404
    print("failed commit creates no version ok")

    # ---- 成功回收：v2 -> v3 ----
    r = _reclaim(2, a1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 3
    snap3 = _snapshot(3)
    assert snap3 == _pool()
    assert a1 not in snap3["allocated_cidrs"]
    a1_net = ipaddress.ip_network(a1)
    assert any(
        a1_net.subnet_of(ipaddress.ip_network(f)) for f in snap3["free_cidrs"]
    )
    # v2 快照仍记录 a1 为已分配（不可变）。
    assert a1 in snap2["allocated_cidrs"]
    print("reclaim snapshot v3 ok")

    # ---- 失败回收（409，目标未被已分配完整覆盖）不生成版本 ----
    r = _reclaim(3, a1)  # a1 此刻空闲
    assert r.status_code == 409
    assert _get(4).status_code == 404
    print("failed reclaim creates no version ok")

    # ---- 整批迁移：v3 -> v4（把 a2 原地迁到另一空闲 /24）----
    # a2 此时仍已分配；找一个当前空闲的 /24 作为目标。
    free24 = next(
        f for f in _pool()["free_cidrs"] if f.endswith("/24")
    )
    r = _relocate(3, [{"from_cidr": a2, "to_cidr": free24}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 4
    snap4 = _snapshot(4)
    assert snap4 == _pool()
    assert snap4["allocated_cidrs"] == [free24]
    # v3 快照仍记录迁移前的占用。
    assert a2 in snap3["allocated_cidrs"]
    assert free24 not in snap3["allocated_cidrs"]
    print("relocate snapshot v4 ok:", snap4["allocated_cidrs"])

    # ---- 失败迁移（409，来源未被已分配覆盖）不生成版本 ----
    r = _relocate(4, [{"from_cidr": a2, "to_cidr": a1}])
    assert r.status_code == 409, r.text
    assert _get(5).status_code == 404
    print("failed relocate creates no version ok")

    # ---- 保留区替换：v4 -> v5（相邻两段规范化合并）----
    r = _replace(
        4, ["10.10.100.0/24", "10.10.101.0/24"]
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 5
    assert body["reserved_cidrs"] == ["10.10.100.0/23"], body
    snap5 = _snapshot(5)
    assert snap5 == _pool()
    assert snap5["reserved_cidrs"] == ["10.10.100.0/23"]
    assert snap5["allocated_cidrs"] == [free24]
    # v4 快照仍保留旧保留区。
    assert snap4["reserved_cidrs"] == ["10.10.0.0/20", "10.10.255.0/24"]
    print("replace snapshot v5 ok:", snap5["reserved_cidrs"])

    # ---- 失败替换（409，与已分配地址相交）不生成版本 ----
    r = _replace(5, [free24])
    assert r.status_code == 409, r.text
    assert _get(6).status_code == 404
    # 422（越出父网段）同样不生成版本。
    r = _replace(5, ["10.11.0.0/24"])
    assert r.status_code == 422, r.text
    assert _get(6).status_code == 404
    print("failed replace creates no version ok")

    # ---- 不可变性总检查：所有历史快照保持首次落库内容 ----
    expected = {
        1: {
            "reserved_cidrs": ["10.10.0.0/20", "10.10.255.0/24"],
            "allocated_cidrs": [],
        },
        2: {
            "reserved_cidrs": ["10.10.0.0/20", "10.10.255.0/24"],
            "allocated_cidrs": [a1, a2],
        },
        3: {
            "reserved_cidrs": ["10.10.0.0/20", "10.10.255.0/24"],
            "allocated_cidrs": [a2],
        },
        4: {
            "reserved_cidrs": ["10.10.0.0/20", "10.10.255.0/24"],
            "allocated_cidrs": [free24],
        },
        5: {
            "reserved_cidrs": ["10.10.100.0/23"],
            "allocated_cidrs": [free24],
        },
    }
    for version, want in expected.items():
        snap = _snapshot(version)
        assert snap["version"] == version
        assert snap["parent_cidr"] == "10.10.0.0/16"
        assert snap["reserved_cidrs"] == want["reserved_cidrs"], (
            version, snap
        )
        assert snap["allocated_cidrs"] == want["allocated_cidrs"], (
            version, snap
        )
        # 空闲 CIDR 与快照状态自洽：数量一致、不与占用/保留重叠、
        # 与父网段相减后重新规范化结果一致。
        parent = ipaddress.ip_network(snap["parent_cidr"])
        blocked = [
            ipaddress.ip_network(c)
            for c in snap["reserved_cidrs"] + snap["allocated_cidrs"]
        ]
        from app.allocator import (
            _collapse_ranges,
            _network_bounds,
            _ranges_to_cidrs,
            _subtract_ranges,
        )

        remaining = _subtract_ranges(
            _network_bounds(parent),
            _collapse_ranges([_network_bounds(n) for n in blocked]),
        )
        recomputed = [
            str(ipaddress.ip_network((s, p), strict=True))
            for s, p in _ranges_to_cidrs(parent.max_prefixlen, remaining)
        ]
        assert snap["free_cidrs"] == recomputed, version
        assert snap["free_cidr_count"] == len(recomputed)
    print("all historical snapshots immutable and self-consistent ok")

    # ---- 重启（同一 DB 文件）：历史快照持久保留 ----
    with TestClient(app) as client2:
        r = client2.get("/api/pool/versions/1")
        assert r.status_code == 200
        assert r.json() == snap1
        assert client2.get("/api/pool/versions/5").json() == snap5
        assert client2.get("/api/pool/versions/6").status_code == 404
    print("snapshots persist across restart ok")

    _legacy_upgrade_test()
    print("ALL VERSION TESTS PASSED")


def _legacy_upgrade_test():
    """老库首次升级：旧表存在、当前版本为 3，仅补存 v3 基线。"""
    legacy_path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = sqlite3.connect(legacy_path)
    conn.executescript(
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
            created_at TEXT NOT NULL DEFAULT
                (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        INSERT INTO pool_meta (id, parent_cidr, version)
            VALUES (1, '10.9.0.0/16', 3);
        INSERT INTO subnets (cidr, kind) VALUES
            ('10.9.0.0/20', 'reserved'),
            ('10.9.1.0/24', 'allocated');
        """
    )
    conn.commit()
    conn.close()

    from app.config import Settings
    from app.db import Database

    db = Database(
        Settings(
            db_path=legacy_path,
            parent_cidr="10.9.0.0/16",
            reserved_cidrs_raw="10.9.0.0/20",
        )
    )

    # 只为当前版本 v3 补存基线；更早的 v1/v2 无快照。
    assert db.get_version(1) is None
    assert db.get_version(2) is None
    state3 = db.get_version(3)
    assert state3 is not None
    assert state3.version == 3
    assert str(state3.parent) == "10.9.0.0/16"
    assert [str(n) for n in state3.reserved] == ["10.9.0.0/20"]
    assert [str(n) for n in state3.allocated] == ["10.9.1.0/24"]
    assert db.get_state().version == 3  # 当前池版本不被升级改写

    # 再次初始化幂等：不重复插入基线（PRIMARY KEY 冲突会报错），
    # 且后续成功写入照常生成新版本快照。
    db.initialize()
    new_state = db.commit_plan(
        3,
        (ipaddress.ip_network("10.9.16.0/24"),),
    )
    assert new_state.version == 4
    snap4 = db.get_version(4)
    assert snap4 is not None
    assert [str(n) for n in snap4.allocated] == [
        "10.9.1.0/24",
        "10.9.16.0/24",
    ]
    # v3 基线内容不被后续写入改变。
    assert [str(n) for n in db.get_version(3).allocated] == ["10.9.1.0/24"]
    print("legacy upgrade baseline backfill ok")


if __name__ == "__main__":
    main()
