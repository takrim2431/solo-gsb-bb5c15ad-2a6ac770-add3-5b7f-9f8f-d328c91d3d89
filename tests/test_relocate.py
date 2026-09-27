"""已分配地址整批同时迁移接口的端到端测试。

直接运行：``python3 -m tests.test_relocate``（或 pytest 收集）。

造数：在池尾空闲块 10.10.254.0/24 的起点依次提交 /25、/26、/27、
/28、/28，恰好铺满该 /24（与 test_reclaim 相同）：

    10.10.254.0/25   = 0..127
    10.10.254.128/26 = 128..191
    10.10.254.192/27 = 192..223
    10.10.254.224/28 = 224..239
    10.10.254.240/28 = 240..255

覆盖：
* 非法输入 422：空列表/超过 8 组、CIDR 无法解析或非规范（主机位
  非零）、两端地址族或前缀长度不一致、来源之间重叠、目标之间重叠、
  来源/目标越出父网段或地址族与父网段不一致；
* 冲突 409：版本过期、来源未被已分配地址完整覆盖、目标触及保留区、
  目标触及未迁移的已分配地址（整批语义：只释放来源覆盖的地址）；
* 409 不改动地址池（版本、已分配集、空闲 CIDR 全部保持不变）；
* 成功迁移：单组迁移后剩余占用合并重写为最少规范 CIDR
  （/26+/27+/28+/28 -> 单个 /25）；
* 整批同时迁移：目标可落在本批来源（含其它组来源）释放的地址上，
  包括两组目标互换、目标跨两个来源的释放区拼接、from == to；
* 迁移后 GET /api/pool 与后续预演立即反映：来源地址变空闲可再次
  预演/提交；
* IPv6 池的迁移（直接走 Database 层）；
* 同版本并发迁移：写锁互斥，恰有一个成功。
"""
import ipaddress
import os
import tempfile
import threading

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_relocate.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import ConflictError, Database, RelocateInputError  # noqa: E402
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


def _relocate(version, moves):
    return client.post(
        "/api/allocations/relocate",
        json={"pool_version": version, "moves": moves},
    )


def _pool():
    return client.get("/api/pool").json()


def _cidr_num_key(cidr: str) -> int:
    return int(ipaddress.ip_network(cidr, strict=False).network_address)


def _seed_tail_block():
    """在 10.10.254.0/24 铺满 /25 /26 /27 /28 /28，返回 (版本, CIDR 列表)。"""
    pieces = []
    version = _pool()["version"]
    for plen in (25, 26, 27, 28, 28):
        got = _preview([plen])["allocations"][0]
        _commit(version, [got])
        version += 1
        pieces.append(got)
    assert pieces == [
        "10.10.254.0/25",
        "10.10.254.128/26",
        "10.10.254.192/27",
        "10.10.254.224/28",
        "10.10.254.240/28",
    ], pieces
    return version, pieces


def main():
    assert _pool()["version"] == 1

    # =====================================================================
    # 一、非法输入 -> 422（不触库）
    # =====================================================================
    version, _ = _seed_tail_block()
    assert version == 6

    bad_payloads = [
        {"pool_version": 6, "moves": []},  # 0 组
        {  # 9 组
            "pool_version": 6,
            "moves": [
                {"from_cidr": "10.10.254.0/28", "to_cidr": f"10.10.253.{i}/28"}
                for i in range(0, 36, 4)
            ][:9],
        },
        # CIDR 无法解析
        {"pool_version": 6,
         "moves": [{"from_cidr": "not-a-cidr", "to_cidr": "10.10.253.0/25"}]},
        # 非规范写法（主机位非零）
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.10.254.1/25", "to_cidr": "10.10.253.0/25"}]},
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.1/25"}]},
        # 地址族不一致
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.10.254.0/25", "to_cidr": "2001:db8::/121"}]},
        # 前缀长度不一致
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.0/26"}]},
        # 来源之间重叠（/26 落在 /25 内）
        {"pool_version": 6, "moves": [
            {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.0/25"},
            {"from_cidr": "10.10.254.0/26", "to_cidr": "10.10.252.0/26"},
        ]},
        # 目标之间重叠
        {"pool_version": 6, "moves": [
            {"from_cidr": "10.10.254.0/26", "to_cidr": "10.10.253.0/25"},
            {"from_cidr": "10.10.254.64/26", "to_cidr": "10.10.253.0/26"},
        ]},
        # 来源/目标越出父网段、地址族与父网段不一致（IPv6 对 IPv4 池）
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.11.0.0/24", "to_cidr": "10.10.253.0/24"}]},
        {"pool_version": 6,
         "moves": [{"from_cidr": "10.10.254.0/25", "to_cidr": "10.11.0.0/25"}]},
        {"pool_version": 6,
         "moves": [{"from_cidr": "2001:db8::/121", "to_cidr": "2001:db8::100/121"}]},
        # pool_version 非法
        {"pool_version": 0,
         "moves": [{"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.0/25"}]},
    ]
    for payload in bad_payloads:
        r = client.post("/api/allocations/relocate", json=payload)
        assert r.status_code == 422, (payload, r.status_code, r.text)
    assert _pool()["version"] == 6
    print("invalid payloads -> 422, pool untouched")

    # =====================================================================
    # 二、状态冲突 -> 409，池与版本不变
    # =====================================================================
    before = _pool()

    # 版本过期
    r = _relocate(1, [
        {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.0/25"}
    ])
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]

    # 来源未被已分配地址完整覆盖：10.10.252.0/24 完全空闲
    r = _relocate(6, [
        {"from_cidr": "10.10.252.0/24", "to_cidr": "10.10.253.0/24"}
    ])
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]

    # 来源只覆盖了部分已分配地址：10.10.254.0/23 跨越整段已分配的
    # 254.0/24 与保留的 255.0/24，保留地址不属于已分配地址，故未被
    # 当前已分配地址完整覆盖。
    r = _relocate(6, [
        {"from_cidr": "10.10.254.0/23", "to_cidr": "10.10.250.0/23"}
    ])
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]

    # 目标触及保留区
    r = _relocate(6, [
        {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.0.0/25"}
    ])
    assert r.status_code == 409, r.text
    assert "reserved" in r.json()["detail"]

    # 目标触及未迁移的已分配地址：
    # 仅释放 0..63，目标 64..127 仍是未迁移的已分配地址 -> 409
    r = _relocate(6, [
        {"from_cidr": "10.10.254.0/26", "to_cidr": "10.10.254.64/26"}
    ])
    assert r.status_code == 409, r.text
    assert "not being migrated" in r.json()["detail"]

    # 多组中任一组冲突即整体 409（第二组目标撞上未迁移占用 224..239）
    r = _relocate(6, [
        {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.0/25"},
        {"from_cidr": "10.10.254.192/27", "to_cidr": "10.10.254.224/27"},
    ])
    assert r.status_code == 409, r.text

    after = _pool()
    assert after == before
    print("state conflicts -> 409, pool and version unchanged")

    # =====================================================================
    # 三、单组迁移成功：剩余占用重写合并为最少 CIDR
    # =====================================================================
    # 迁走 0..127(/25)：剩余 128..255 连续，/26+/27+/28+/28 重写
    # 为单个 10.10.254.128/25。
    r = _relocate(6, [
        {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.128/25"}
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "relocated"
    assert body["pool_version"] == 7
    assert body["moves"] == [
        {"from_cidr": "10.10.254.0/25", "to_cidr": "10.10.253.128/25"}
    ]
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.253.128/25",
        "10.10.254.128/25",
    ], body["allocated_cidrs"]
    print("single relocate rewrites leftover into minimal CIDRs ok")

    pool7 = _pool()
    assert pool7["version"] == 7
    assert pool7["allocated_cidrs"] == body["allocated_cidrs"] or \
        sorted(pool7["allocated_cidrs"], key=_cidr_num_key) == \
        sorted(body["allocated_cidrs"], key=_cidr_num_key)
    assert pool7["free_cidrs"] == body["free_cidrs"]
    # 来源地址立即变空闲（10.10.254.0/25 出现在某空闲块内）
    freed = ipaddress.ip_network("10.10.254.0/25")
    assert any(
        freed.subnet_of(ipaddress.ip_network(f)) for f in pool7["free_cidrs"]
    )
    # 后续预演可立即选中释放出的地址：253.0/25 与 254.0/25 同为整块
    # /25 空闲块，优化器取地址序列字典序最小者 253.0/25。
    plan = _preview([25])
    assert plan["pool_version"] == 7
    assert plan["allocations"] == ["10.10.253.0/25"], plan
    print("freed source reusable in preview ok")

    # =====================================================================
    # 四、整批同时迁移：目标互换（目标落在别组来源上）
    # =====================================================================
    # 当前占用：10.10.253.128/25 与 10.10.254.128/25。
    # 取 128..191 与 192..255 两个 /26（都完整落在 10.10.254.128/25 内），
    # 目标互换：任一目标都覆盖了一组“尚未释放”的来源地址——单组串行
    # 必然冲突，整批同时迁移必须成功。
    r = _relocate(7, [
        {"from_cidr": "10.10.254.128/26", "to_cidr": "10.10.254.192/26"},
        {"from_cidr": "10.10.254.192/26", "to_cidr": "10.10.254.128/26"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 8
    # 两组 /26 互换后 128..255 仍整段占用，覆盖范围与互换前相同，
    # 重写为最少规范 CIDR 后就是单个 10.10.254.128/25。
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.253.128/25",
        "10.10.254.128/25",
    ], body["allocated_cidrs"]
    print("batch swap targets onto other sources ok")

    # =====================================================================
    # 五、链式整批迁移：A 的目标恰好落在 B 的来源释放区
    # =====================================================================
    # 当前占用：10.10.253.128/25 与 10.10.254.128/25(= 128..255)。
    # 来源 A=/27(128..159)、来源 B=/27(160..191)；目标 A=160..191
    # （即 B 的来源，单组串行时是未迁移占用必然 409），目标 B 落在
    # 普通空闲块 10.10.252.0/27。
    # 先验证单组迁移确实冲突：
    r = _relocate(8, [
        {"from_cidr": "10.10.254.128/27", "to_cidr": "10.10.254.160/27"}
    ])
    assert r.status_code == 409, r.text
    assert "not being migrated" in r.json()["detail"]
    # 整批同时迁移则成功：
    r = _relocate(8, [
        {"from_cidr": "10.10.254.128/27", "to_cidr": "10.10.254.160/27"},
        {"from_cidr": "10.10.254.160/27", "to_cidr": "10.10.252.0/27"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 9
    # 160..191 与未迁移的 192..255 相邻，合并重写为 160/27 + 192/26。
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.252.0/27",
        "10.10.253.128/25",
        "10.10.254.160/27",
        "10.10.254.192/26",
    ], body["allocated_cidrs"]
    print("chain move: target reuses another source released range ok")

    # =====================================================================
    # 六、from == to（原地迁移）：允许，占用集不变、版本自增
    # =====================================================================
    r = _relocate(9, [
        {"from_cidr": "10.10.254.160/27", "to_cidr": "10.10.254.160/27"}
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 10
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.252.0/27",
        "10.10.253.128/25",
        "10.10.254.160/27",
        "10.10.254.192/26",
    ], body["allocated_cidrs"]
    print("in-place move (from == to) ok")

    # =====================================================================
    # 七、同版本并发迁移：写锁互斥，恰有一个成功
    # =====================================================================
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(idx, dst):
        barrier.wait()
        results[idx] = _relocate(10, [
            {"from_cidr": "10.10.254.160/27", "to_cidr": dst}
        ])

    t0 = threading.Thread(target=worker, args=(0, "10.10.251.0/27"))
    t1 = threading.Thread(target=worker, args=(1, "10.10.250.0/27"))
    t0.start()
    t1.start()
    t0.join(timeout=30)
    t1.join(timeout=30)
    assert not t0.is_alive() and not t1.is_alive()
    codes = sorted(r.status_code for r in results.values())
    assert codes == [200, 409], codes
    assert _pool()["version"] == 11  # 只自增一次
    winner = next(r for r in results.values() if r.status_code == 200)
    assert winner.json()["pool_version"] == 11
    print("concurrent relocate: exactly one succeeds ok")

    # =====================================================================
    # 八、来源只覆盖单条分配记录的一部分：剩余部分重写为更小 CIDR
    # =====================================================================
    # v11 时 10.10.254.192/26(=192..255) 仍是一整段分配记录。取其一半
    # 224..255(/27) 迁到此刻空闲的 10.10.254.160/27（并发迁移的来源
    # 刚释放），剩余 192..223 重写为单独的 10.10.254.192/27。
    winner_target = winner.json()["moves"][0]["to_cidr"]
    r = _relocate(11, [
        {"from_cidr": "10.10.254.224/27", "to_cidr": "10.10.254.160/27"}
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 12
    expected = {
        "10.10.252.0/27",
        "10.10.253.128/25",
        "10.10.254.160/27",
        "10.10.254.192/27",
        winner_target,
    }
    assert set(body["allocated_cidrs"]) == expected, body["allocated_cidrs"]
    print("partial move within one allocation record rewrites leftover ok")

    # =====================================================================
    # 九、IPv6 池（直接走 Database 层）
    # =====================================================================
    v6_db = Database(Settings(
        db_path=os.path.join(tempfile.mkdtemp(), "v6.db"),
        parent_cidr="2001:db8::/48",
        reserved_cidrs_raw="2001:db8::/64",
    ))
    v6_db.commit_plan(1, (ipaddress.ip_network("2001:db8:0:1::/64"),))
    state = v6_db.relocate_allocations(
        2,
        ((ipaddress.ip_network("2001:db8:0:1::/64"),
          ipaddress.ip_network("2001:db8:0:2::/64")),),
    )
    assert state.version == 3
    assert tuple(str(n) for n in state.allocated) == ("2001:db8:0:2::/64",)

    # v6 冲突：目标触及保留区
    try:
        v6_db.relocate_allocations(
            3,
            ((ipaddress.ip_network("2001:db8:0:2::/64"),
              ipaddress.ip_network("2001:db8::/64")),),
        )
        raise AssertionError("expected ConflictError")
    except ConflictError:
        pass
    assert v6_db.get_state().version == 3

    # v6 非法输入：来源越出父网段 -> RelocateInputError（HTTP 422）
    try:
        v6_db.relocate_allocations(
            3,
            ((ipaddress.ip_network("2001:db9::/64"),
              ipaddress.ip_network("2001:db8:0:3::/64")),),
        )
        raise AssertionError("expected RelocateInputError")
    except RelocateInputError:
        pass
    assert v6_db.get_state().version == 3
    print("IPv6 relocate + conflict + input error ok")

    print("ALL RELOCATE TESTS PASSED")


if __name__ == "__main__":
    main()
