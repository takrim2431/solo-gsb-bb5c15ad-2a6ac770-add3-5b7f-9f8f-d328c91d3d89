"""小规模暴力对照：穷举全部可行对齐子网组合，验证 DP 的两个优化目标。"""
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
    free_cidrs = list(
        ipaddress.ip_network((s, p)) for s, p in _ranges_to_cidrs(width, free_ranges)
    )

    candidates = []
    for p in prefixes:
        cands = []
        # 父网段内所有对齐 /p
        start = int(parent.network_address)
        end = int(parent.broadcast_address)
        step = 1 << (width - p)
        a = start
        while a + step - 1 <= end:
            net = ipaddress.ip_network((a, p))
            # 完全落在剩余空间内
            lo, hi = int(net.network_address), int(net.broadcast_address)
            inside = any(
                int(f.network_address) <= lo and hi <= int(f.broadcast_address)
                for f in free_cidrs
            )
            if inside:
                cands.append(net)
            a += step
        candidates.append(cands)

    best = None
    for combo in itertools.product(*candidates):
        # 互不重叠
        if any(
            int(a.network_address) <= int(b.broadcast_address)
            and int(b.network_address) <= int(a.broadcast_address)
            for a, b in itertools.combinations(combo, 2)
        ):
            continue
        # 计算分配后剩余规范 CIDR 数
        blk = list(blocked) + [_network_bounds(n) for n in combo]
        rem = _subtract_ranges(_network_bounds(parent), _collapse_ranges(blk))
        after = _ranges_to_cidrs(width, rem)
        key = (len(after), tuple(int(n.network_address) for n in combo))
        if best is None or key < best[0]:
            best = (key, combo, after)
    if best is None:
        return None
    return best


def random_pool(rng):
    width = 32
    q = rng.randint(26, 28)
    base = rng.randrange(0, 2**32 - 2 ** (32 - q), 2 ** (32 - q))
    parent = ipaddress.ip_network((base, q))
    reserved = []
    # 在父网段内挑几个随机对齐保留子网
    used = []
    for _ in range(rng.randint(0, 4)):
        rp = rng.randint(q + 1, 30)
        start = int(parent.network_address)
        end = int(parent.broadcast_address)
        step = 1 << (32 - rp)
        a = start + rng.randrange((end - start + 1) // step) * step
        net = ipaddress.ip_network((a, rp))
        if any(net.overlaps(r) for r in reserved):
            continue
        reserved.append(net)
    return parent, reserved


def main():
    rng = random.Random(20260920)
    total = 0
    for trial in range(4000):
        parent, reserved = random_pool(rng)
        m = rng.randint(1, 4)
        q = parent.prefixlen
        prefixes = tuple(rng.randint(q, 30) for _ in range(m))
        try:
            plan = CidrAllocationSolver(parent, reserved, prefixes).solve()
            dp_key = (
                len(plan.free_cidrs_after),
                tuple(int(n.network_address) for n in plan.networks),
            )
            dp_feasible = True
        except InfeasibleError:
            dp_key = None
            dp_feasible = False

        bf = brute_force(parent, reserved, prefixes)
        total += 1
        if bf is None:
            assert not dp_feasible, (parent, reserved, prefixes, dp_key)
        else:
            assert dp_feasible, (parent, reserved, prefixes, "DP infeasible but BF ok")
            bf_key = bf[0]
            assert dp_key == bf_key, (
                parent,
                reserved,
                prefixes,
                "DP", dp_key,
                "BF", bf_key,
            )
            # 校验子网合法
            seen = []
            for net, p in zip(plan.networks, prefixes):
                assert net.prefixlen == p
                assert net.subnet_of(parent)
                assert not any(net.overlaps(r) for r in reserved)
                assert not any(net.overlaps(s) for s in seen)
                seen.append(net)
    print(f"OK: {total} random cases match brute force")


if __name__ == "__main__":
    main()
