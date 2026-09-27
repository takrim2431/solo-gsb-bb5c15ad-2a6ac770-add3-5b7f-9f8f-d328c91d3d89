"""高密度、相邻/嵌套保留子网下的暴力对照测试。

专门覆盖保留区间相互紧邻时节点分类（blocked/clean/mixed）的边界情形。
"""
import ipaddress
import itertools
import random

from app.allocator import (
    CidrAllocationSolver,
    InfeasibleError,
    _collapse_ranges,
    _network_bounds,
    _ranges_to_cidrs,
    _subtract_ranges,
)


def brute_force(parent, reserved, prefixes):
    width = parent.max_prefixlen
    blocked = _collapse_ranges([_network_bounds(n) for n in reserved])
    free_ranges = _subtract_ranges(_network_bounds(parent), blocked)
    free_cidrs = [
        ipaddress.ip_network((s, p)) for s, p in _ranges_to_cidrs(width, free_ranges)
    ]
    candidates = []
    for p in prefixes:
        cands = []
        step = 1 << (width - p)
        a = int(parent.network_address)
        end = int(parent.broadcast_address)
        while a + step - 1 <= end:
            net = ipaddress.ip_network((a, p))
            lo, hi = int(net.network_address), int(net.broadcast_address)
            if any(
                int(f.network_address) <= lo and hi <= int(f.broadcast_address)
                for f in free_cidrs
            ):
                cands.append(net)
            a += step
        candidates.append(cands)

    best = None
    for combo in itertools.product(*candidates):
        if any(
            int(a.network_address) <= int(b.broadcast_address)
            and int(b.network_address) <= int(a.broadcast_address)
            for a, b in itertools.combinations(combo, 2)
        ):
            continue
        blk = blocked + [_network_bounds(n) for n in combo]
        after = _ranges_to_cidrs(
            width, _subtract_ranges(_network_bounds(parent), _collapse_ranges(blk))
        )
        key = (len(after), tuple(int(n.network_address) for n in combo))
        if best is None or key < best:
            best = key
    return best


def carve_pool(rng):
    """通过递归切分生成稠密的相邻/嵌套保留块集合。"""
    width = 32
    q = rng.randint(25, 27)
    base = rng.randrange(0, 2**32 - 2 ** (32 - q), 2 ** (32 - q))
    parent = ipaddress.ip_network((base, q))

    reserved: list = []
    leaves = [parent]
    # 随机把若干叶子再切成两半，每一半以一定概率被保留
    for _ in range(rng.randint(1, 12)):
        if not leaves:
            break
        idx = rng.randrange(len(leaves))
        leaf = leaves.pop(idx)
        if leaf.prefixlen >= 30:
            leaves.append(leaf)
            continue
        left, right = list(leaf.subnets())
        for half in (left, right):
            roll = rng.random()
            if roll < 0.45:
                reserved.append(half)  # 整块保留（常与邻居紧邻）
            elif roll < 0.7:
                leaves.append(half)    # 继续细分
            else:
                pass                   # 整块空闲
    # 去重并偶尔加入一个嵌套的更小保留块
    uniq = []
    for net in reserved:
        if not any(net.subnet_of(u) for u in uniq if u != net):
            uniq = [u for u in uniq if not u.subnet_of(net)] + [net]
    return parent, uniq


def main():
    rng = random.Random(20260921)
    for trial in range(5000):
        parent, reserved = carve_pool(rng)
        q = parent.prefixlen
        m = rng.randint(1, 5)
        prefixes = tuple(rng.randint(q, 30) for _ in range(m))
        bf = brute_force(parent, reserved, prefixes)
        try:
            plan = CidrAllocationSolver(parent, reserved, prefixes).solve()
            dp = (
                len(plan.free_cidrs_after),
                tuple(int(n.network_address) for n in plan.networks),
            )
            feasible = True
        except InfeasibleError:
            feasible = False
        if bf is None:
            assert not feasible, (parent, reserved, prefixes)
        else:
            assert feasible, (parent, reserved, prefixes)
            assert dp == bf, (parent, reserved, prefixes, dp, bf)
    print("OK: 5000 dense/adjacent reservation cases match brute force")


if __name__ == "__main__":
    main()
