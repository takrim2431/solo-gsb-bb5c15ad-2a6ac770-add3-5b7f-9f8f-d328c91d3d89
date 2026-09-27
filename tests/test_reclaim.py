"""已分配地址部分回收接口的端到端测试。

直接运行：``python3 -m tests.test_reclaim``（或 pytest 收集）。

造数：在池尾空闲块 10.10.254.0/24 的起点依次提交 /25、/26、/27、
/28、/28（优化目标使地址贴边紧凑排列），恰好铺满该 /24：

    10.10.254.0/25   = 0..127
    10.10.254.128/26 = 128..191
    10.10.254.192/27 = 192..223
    10.10.254.224/28 = 224..239
    10.10.254.240/28 = 240..255

覆盖：
* 目标跨越相邻多条分配记录（/27 覆盖两个相邻 /28）；
* 单条记录内部的部分回收（/26 落在 /25 记录内），重写为最少规范 CIDR；
* 扣除后不相邻的区间不被错误合并；扣除后相邻的多条记录被合并重写
  （/27 + /28 + /28 -> /26）；
* 冲突场景：版本过期、越出父网段、含未分配地址、含保留地址、已分配集
  为空、CIDR 写法非规范（422）；
* 冲突不改动地址池（版本、已分配集、空闲 CIDR 全部保持不变）；
* 回收后池状态查询与后续分配预演立即反映回收空间（同一 CIDR 可被
  再次预演/提交）。
"""
import ipaddress
import os
import tempfile

os.environ["PARENT_CIDR"] = "10.10.0.0/16"
os.environ["RESERVED_CIDRS"] = "10.10.0.0/20,10.10.255.0/24"
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reclaim.db")

from fastapi.testclient import TestClient  # noqa: E402

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


def _reclaim(version, cidr):
    return client.post(
        "/api/allocations/reclaim",
        json={"pool_version": version, "cidr": cidr},
    )


def _pool():
    return client.get("/api/pool").json()


def _cidr_num_key(cidr: str) -> int:
    return int(ipaddress.ip_network(cidr, strict=False).network_address)


def main():
    assert _pool()["version"] == 1

    # ---- 已分配集为空：回收 -> 409，池不变 ----
    r = _reclaim(1, "10.10.254.0/24")
    assert r.status_code == 409, r.text
    assert _pool()["version"] == 1
    print("empty pool reclaim -> 409 ok")

    # ---- 造数：/25 + /26 + /27 + /28 + /28 铺满 10.10.254.0/24 ----
    pieces = []
    version = 1
    for plen in (25, 26, 27, 28, 28):
        got = _preview([plen])["allocations"][0]
        _commit(version, [got])
        version += 1
        pieces.append(got)
    assert version == 6
    assert pieces == [
        "10.10.254.0/25",
        "10.10.254.128/26",
        "10.10.254.192/27",
        "10.10.254.224/28",
        "10.10.254.240/28",
    ], pieces
    print("seed allocations:", pieces)

    # ---- 非法 CIDR：主机位非零 / 无法解析 -> 422 ----
    r = _reclaim(6, "10.10.254.1/24")
    assert r.status_code == 422, r.text
    r = _reclaim(6, "not-a-cidr")
    assert r.status_code == 422, r.text
    print("non-canonical cidr -> 422 ok")

    # ---- 越出父网段 -> 409，版本/已分配集/空闲 CIDR 全部不变 ----
    before = _pool()
    r = _reclaim(6, "10.11.0.0/24")
    assert r.status_code == 409, r.text
    after = _pool()
    assert after["version"] == before["version"] == 6
    assert after["allocated_cidrs"] == before["allocated_cidrs"]
    assert after["free_cidrs"] == before["free_cidrs"]
    print("out-of-parent -> 409, pool untouched")

    # ---- 含未分配地址（10.10.252.0/24 完全空闲）-> 409 ----
    r = _reclaim(6, "10.10.252.0/24")
    assert r.status_code == 409, r.text
    assert "not fully covered" in r.json()["detail"]
    assert _pool()["version"] == 6
    print("contains unallocated -> 409 ok")

    # ---- 含保留地址 -> 409 ----
    r = _reclaim(6, "10.10.0.0/20")
    assert r.status_code == 409, r.text
    assert "reserved" in r.json()["detail"]

    # ---- 版本过期 -> 409，池不变 ----
    r = _reclaim(1, "10.10.254.0/25")
    assert r.status_code == 409, r.text
    assert "version changed" in r.json()["detail"]
    assert _pool()["version"] == 6
    print("stale version -> 409 ok")

    # ---- 跨相邻两条 /28 记录回收 10.10.254.224/27 ----
    # 剩余 0..223：/25(0..127) + /26(128..191) + /27(192..223)，
    # 扣除区间两侧不相邻（224..255 被整块取走），不发生错误合并。
    r = _reclaim(6, "10.10.254.224/27")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reclaimed"
    assert body["reclaimed_cidr"] == "10.10.254.224/27"
    assert body["pool_version"] == 7
    assert body["allocated_cidrs"] == [
        "10.10.254.0/25",
        "10.10.254.128/26",
        "10.10.254.192/27",
    ], body["allocated_cidrs"]
    print("reclaim spanning two adjacent records ok")

    # ---- 池状态查询立即反映：回收地址出现在空闲 CIDR 中 ----
    pool7 = _pool()
    assert pool7["version"] == 7
    assert pool7["allocated_cidrs"] == body["allocated_cidrs"]
    assert body["free_cidrs"] == pool7["free_cidrs"]
    assert "10.10.254.224/27" in pool7["free_cidrs"], pool7["free_cidrs"]

    # ---- 后续分配预演立即反映：/27 恰可在原回收位置再次落位 ----
    plan = _preview([27])
    assert plan["pool_version"] == 7
    assert plan["allocations"] == ["10.10.254.224/27"], plan
    committed = _commit(7, ["10.10.254.224/27"])
    assert committed["pool_version"] == 8
    print("reclaimed space reusable in preview + commit ok")

    # ---- 回收单条 /26：扣除后 /27 + /28 + /28 三段相邻，合并重写为 /26 ----
    # 状态：0..127(/25) 128..191(/26) 192..223(/27) 224..255(/27)。
    # 扣除 128..191 后，192..255 连续 -> 单个规范 /26。
    r = _reclaim(8, "10.10.254.128/26")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 9
    assert body["reclaimed_cidr"] == "10.10.254.128/26"
    assert body["allocated_cidrs"] == [
        "10.10.254.0/25",
        "10.10.254.192/26",
    ], body["allocated_cidrs"]
    print("rewrite merges adjacent records into minimal CIDRs ok")

    # ---- 单条记录内部的部分回收：/26 落在 /25 记录（0..127）内 ----
    # 扣除 0..63 后剩余 64..127 -> /26；与 192..255 不相邻。
    r = _reclaim(9, "10.10.254.0/26")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pool_version"] == 10
    # 库内按 cidr 文本排序，64 与 192 的文本序/数值序不一致，按集合比较。
    assert sorted(body["allocated_cidrs"], key=_cidr_num_key) == [
        "10.10.254.64/26",
        "10.10.254.192/26",
    ], body["allocated_cidrs"]
    print("partial reclaim within one record ok")

    # ---- 失败的回收不改写已有结果 ----
    before = _pool()
    r = _reclaim(10, "10.10.254.128/26")  # 128..191 此刻空闲
    assert r.status_code == 409
    assert _pool()["allocated_cidrs"] == before["allocated_cidrs"]
    assert _pool()["version"] == 10

    # ---- 抽干：逐条回收至已分配集为空 ----
    for cidr in ("10.10.254.64/26", "10.10.254.192/26"):
        cur = _pool()["version"]
        r = _reclaim(cur, cidr)
        assert r.status_code == 200, r.text
    final = _pool()
    assert final["version"] == 12
    assert final["allocated_cidrs"] == []
    print("pool drained ok")

    # 已分配为空后再回收 -> 409
    r = _reclaim(12, "10.10.254.192/26")
    assert r.status_code == 409
    assert _pool()["version"] == 12

    print("ALL RECLAIM TESTS PASSED")


if __name__ == "__main__":
    main()
