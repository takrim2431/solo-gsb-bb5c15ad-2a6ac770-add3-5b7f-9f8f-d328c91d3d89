"""CIDR 批量分配的核心搜索算法。

问题定义
========
地址池由一个父网段 *parent* 与若干保留子网 *reserved* 组成；剩余空间
（父网段减去全部保留子网）始终可表示为一组互不相交、对齐到网络边界的
规范 CIDR（free blocks）。

一次预演接收 m 个前缀长度请求 p_0 ... p_{m-1}（按请求顺序），需要为每个
请求选择一个**对齐子网**（网络地址在其 /p_i 边界上），所有子网互不重叠且
完全落在剩余空间中。

目标（字典序优化）
------------------
1. 最小化分配完成后剩余空闲空间的规范 CIDR 数量（分片数）；
2. 若并列，取 *按请求顺序排列的网络地址序列* 字典序最小者。

算法
====
把地址空间看作一棵二进制前缀树。在树节点上做子集 DP，状态为请求集合
（m <= 8，每节点至多 256 个状态）。

节点代价统一定义为“本子树在分配后剩余的规范空闲 CIDR 数量”：

* 保留（blocked）子树：0；
* 空的干净（clean，完全空闲）子树：1（它本身就是一个规范空闲 CIDR）；
* 唯一请求恰终止于该干净节点：0（整块被占满）；
* 其余干净/混合（mixed，部分保留）节点：枚举请求在左右半子树间的划分，
  代价为两个孩子代价之和。

记忆化
------
* 干净子树的子问题只依赖 (block_plen, 请求前缀多重集)，与所在地址无关；
* 混合子树的子问题依赖 (plen, 节点内空闲 CIDR 边界几何, 请求前缀多重集)，
  与节点绝对地址无关。

内部请求用“按 (前缀长度, 原请求下标) 排序后的规范位掩码”表示，因此等前缀
请求之间完全对称，所有几何同构的节点共享同一份缓存。根节点处再把规范位
还原为原始请求下标（同前缀按下标顺序配对地址最小的槽位）。
"""

from __future__ import annotations

import bisect
import ipaddress
from dataclasses import dataclass

IPNetwork = ipaddress._BaseNetwork  # type: ignore[attr-defined]


class InfeasibleError(Exception):
    """当前剩余空间无法满足全部前缀请求。"""


# ---------------------------------------------------------------------------
# 区间 / CIDR 工具函数
# ---------------------------------------------------------------------------

def _network_bounds(network: IPNetwork) -> tuple[int, int]:
    return int(network.network_address), int(network.broadcast_address)


def _collapse_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并相交或相邻的整数区间。"""
    if not ranges:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract_ranges(
    parent: tuple[int, int], blocked: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """从父区间中减去被阻塞区间，返回剩余整数区间。"""
    p_start, p_end = parent
    remaining: list[tuple[int, int]] = []
    cursor = p_start
    for raw_start, raw_end in sorted(blocked):
        # 完全落在游标之前（已处理）或父区间之后：跳过/结束。
        if raw_end < cursor:
            continue
        if raw_start > p_end:
            break
        start = max(raw_start, cursor)
        end = min(raw_end, p_end)
        if start > cursor:
            remaining.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
        if cursor > p_end:
            break
    if cursor <= p_end:
        remaining.append((cursor, p_end))
    return remaining


def _ranges_to_cidrs(
    width: int, ranges: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """把整数区间切成对齐的规范 CIDR，返回 [(start, prefixlen), ...]。"""
    cidrs: list[tuple[int, int]] = []
    for start, end in ranges:
        cursor = start
        while cursor <= end:
            size = end - cursor + 1
            # 由当前地址对齐边界允许的最大块：最低有效位对应的 2 的幂。
            alignment = cursor & -cursor
            block_size = size if alignment == 0 else min(alignment, size)
            # 向下取整到 2 的幂。
            block_size = 1 << (block_size.bit_length() - 1)
            plen = width - (block_size.bit_length() - 1)
            cidrs.append((cursor, plen))
            cursor += block_size
    return cidrs


# ---------------------------------------------------------------------------
# 求解结果
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationPlan:
    networks: tuple[IPNetwork, ...]
    free_cidrs_after: tuple[IPNetwork, ...]


# ---------------------------------------------------------------------------
# 求解器
# ---------------------------------------------------------------------------

class CidrAllocationSolver:
    """在单个地址池（父网段 + 保留子网）内求解批量分配。"""

    def __init__(
        self,
        parent: IPNetwork,
        reserved: list[IPNetwork],
        prefixes: tuple[int, ...],
    ) -> None:
        self.parent = parent
        self.width = parent.max_prefixlen
        self.root_start = int(parent.network_address)
        self.root_q = parent.prefixlen
        self.prefixes = tuple(prefixes)
        self.m = len(prefixes)

        blocked_ranges = _collapse_ranges(
            [_network_bounds(net) for net in reserved]
        )
        self._blocked = blocked_ranges
        self._blocked_starts = [r[0] for r in blocked_ranges]
        self._blocked_ends = [r[1] for r in blocked_ranges]

        free_ranges = _subtract_ranges(
            _network_bounds(parent), blocked_ranges
        )
        self.free_cidrs = [
            ipaddress.ip_network((start, plen), strict=True)
            for start, plen in _ranges_to_cidrs(self.width, free_ranges)
        ]
        self._free_starts = [int(n.network_address) for n in self.free_cidrs]
        self._free_ends = [int(n.broadcast_address) for n in self.free_cidrs]
        # 规范请求位：位 i 对应第 i 小的 (前缀长度, 原下标)。等前缀请求
        # 因此在 DP 内部对称；字典序键通过 _lex_order 按原请求顺序还原。
        order = sorted(range(self.m), key=lambda i: (self.prefixes[i], i))
        self._canon_prefix = tuple(self.prefixes[i] for i in order)
        self._canon_to_orig = tuple(order)
        self._orig_to_canon = [0] * self.m
        for c, o in enumerate(order):
            self._orig_to_canon[o] = c
        # 字典序比较时遍历的规范位顺序：按原始请求下标 0..m-1。
        self._lex_order = tuple(self._orig_to_canon[i] for i in range(self.m))

        # 热路径：每个规范 mask 的位列表与终止于各层的位掩码。
        self._ids_of_mask = [
            tuple(i for i in range(self.m) if mask & (1 << i))
            for mask in range(1 << self.m)
        ]
        self._end_at: dict[int, int] = {}
        for c, p in enumerate(self._canon_prefix):
            self._end_at[p] = self._end_at.get(p, 0) | (1 << c)

        # 等前缀组（规范位连续）：用于对称剪枝——同一组中分配到左子树的
        # 总是组内最小的若干位，组内交换不改变代价。
        self._prefix_groups: list[tuple[int, int]] = []
        i = 0
        while i < self.m:
            j = i
            while j + 1 < self.m and self._canon_prefix[j + 1] == self._canon_prefix[i]:
                j += 1
            self._prefix_groups.append((i, j))  # [i, j]
            i = j + 1

        # 每个 mask 的“规范子掩码”：只保留每个等前缀组内取最低若干位的划分，
        # 即左右划分在组内交换意义下的唯一代表。
        self._canonical_submasks = self._build_canonical_submasks()

        # 干净子树缓存：(block_plen, mask) -> (cost, {规范 id: 相对槽位})。
        self._clean_cache: dict[
            tuple[int, int], tuple[int, dict[int, int]]
        ] = {}
        # 混合子树缓存：(plen, geometry, mask)。
        self._node_cache: dict[
            tuple[int, tuple, int], tuple[int, dict[int, int]]
        ] = {}

    # -- 节点分类与几何 ----------------------------------------------------

    def _build_canonical_submasks(self) -> list[tuple[int, ...]]:
        """对每个 mask 生成左右划分的唯一代表集合（等前缀组内对称）。

        组内规范位升序排列；左子树若取 k 个，必取组内最小的 k 个。
        """
        result: list[tuple[int, ...]] = [()] * (1 << self.m)
        for mask in range(1 << self.m):
            group_options: list[tuple[int, ...]] = []
            for a, b in self._prefix_groups:
                present = [
                    i for i in range(a, b + 1) if mask & (1 << i)
                ]
                options: list[int] = []
                running = 0
                for k in range(len(present) + 1):
                    if k > 0:
                        running |= 1 << present[k - 1]
                    options.append(running)
                group_options.append(tuple(options))
            submasks: list[int] = [0]
            for options in group_options:
                submasks = [
                    s | opt for s in submasks for opt in options
                ]
            result[mask] = tuple(submasks)
        return result

    def _classify(self, start: int, plen: int) -> str:
        """返回节点相对保留区域的类型：blocked / clean / mixed。"""
        end = start + (1 << (self.width - plen)) - 1
        lo = bisect.bisect_right(self._blocked_ends, start - 1)
        hi = bisect.bisect_right(self._blocked_starts, end)
        if lo >= hi:
            return "clean"
        # 任一与节点重叠的保留区间完整覆盖该节点，即为 blocked。
        for j in range(lo, hi):
            if self._blocked_starts[j] <= start and end <= self._blocked_ends[j]:
                return "blocked"
        return "mixed"

    def _geometry(self, start: int, plen: int) -> tuple:
        """节点内空闲 CIDR 的规范化几何：(起点槽位偏移, 前缀长度) 序列。

        只记录完全落在节点内部的规范空闲 CIDR；按节点起点换算成
        以该空闲块为单位的偏移。两个几何相同的同层节点结构同构。
        """
        end = start + (1 << (self.width - plen)) - 1
        lo = bisect.bisect_left(self._free_ends, start)
        hi = bisect.bisect_right(self._free_starts, end)
        geo = []
        for j in range(lo, hi):
            fs = self._free_starts[j]
            fp = self.free_cidrs[j].prefixlen
            offset = (fs - start) >> (self.width - fp)
            geo.append((offset, fp))
        return tuple(geo)

    # -- 干净子树（完全空闲块） -------------------------------------------

    def _clean(self, block_plen: int, mask: int):
        """干净 /block_plen 子树内求解（规范 mask）。

        代价 = 本子树分配后剩余的规范空闲 CIDR 数量；槽位号相对于本节点
        起点（以各请求自身 /p 为单位）。
        """
        if mask == 0:
            return 1, {}
        key = (block_plen, mask)
        cached = self._clean_cache.get(key)
        if cached is not None:
            return cached

        ids = self._ids_of_mask[mask]

        # 唯一请求恰在本节点结束：整块被占用。
        if len(ids) == 1 and self._canon_prefix[ids[0]] == block_plen:
            result = (0, {ids[0]: 0})
            self._clean_cache[key] = result
            return result

        end_here = self._end_at.get(block_plen, 0) & mask
        best = None
        for sub in self._canonical_submasks[mask]:
            if end_here == 0:
                try:
                    cl, sl = self._clean(block_plen + 1, sub)
                    cr, sr = self._clean(block_plen + 1, mask ^ sub)
                except InfeasibleError:
                    continue
                slots = dict(sl)
                for i in ids:
                    if i in sr:
                        slots[i] = sr[i] + (
                            1 << (self._canon_prefix[i] - block_plen - 1)
                        )
                cost = cl + cr
                # 字典序必须按原始请求顺序比较地址。
                lex = tuple(
                    slots[c] << (self.width - self._canon_prefix[c])
                    for c in self._lex_order
                    if c in ids
                )
                if best is None or (cost, lex) < (best[0], best[1]):
                    best = (cost, lex, slots)

        if best is None:
            raise InfeasibleError("no aligned subnet fits in free space")
        result = (best[0], best[2])
        self._clean_cache[key] = result
        return result

    # -- 通用树节点 --------------------------------------------------------

    def _solve_node(self, start: int, plen: int, mask: int):
        """规范 mask 下返回 (cost, {规范 id: 相对槽位})。"""
        kind = self._classify(start, plen)
        if kind == "blocked":
            if mask != 0:
                raise InfeasibleError("request overlaps reserved network")
            return 0, {}
        if kind == "clean":
            return self._clean(plen, mask)
        if plen == self.width:
            # 单地址混合节点不可能出现（保留已与父网段对齐），防御性处理。
            return 0, {}
        if mask == 0:
            # 空混合子树：统计完全落在本节点内的规范空闲 CIDR 数量。
            geo = self._geometry(start, plen)
            return len(geo), {}

        geo = self._geometry(start, plen)
        key = (plen, geo, mask)
        cached = self._node_cache.get(key)
        if cached is not None:
            return cached

        half = 1 << (self.width - plen - 1)
        ids = self._ids_of_mask[mask]
        # 混合节点本身含保留区域，prefixlen <= plen 的请求无法放置。
        blocked_levels = any(
            self._canon_prefix[i] <= plen for i in ids
        )

        def split_cost(sub: int):
            if blocked_levels:
                return None
            try:
                cl, sl = self._solve_node(start, plen + 1, sub)
                cr, sr = self._solve_node(
                    start + half, plen + 1, mask ^ sub
                )
            except InfeasibleError:
                return None
            slots = dict(sl)
            for i in ids:
                if i in sr:
                    slots[i] = sr[i] + (
                        1 << (self._canon_prefix[i] - plen - 1)
                    )
            cost = cl + cr
            lex = tuple(
                start + slots[c] * (1 << (self.width - self._canon_prefix[c]))
                for c in self._lex_order
                if c in ids
            )
            return cost, lex, slots

        best = None
        for sub in self._canonical_submasks[mask]:
            cand = split_cost(sub)
            if cand is not None and (
                best is None or (cand[0], cand[1]) < (best[0], best[1])
            ):
                best = cand

        if best is None:
            raise InfeasibleError("no aligned subnet fits in free space")
        result = (best[0], best[2])
        self._node_cache[key] = result
        return result

    # -- 对外入口 ----------------------------------------------------------

    def solve(self) -> AllocationPlan:
        for p in self.prefixes:
            if p < self.root_q or p > self.width:
                raise InfeasibleError(f"prefix length {p} out of pool range")

        full_mask = (1 << self.m) - 1
        cost, canon_slots = self._solve_node(
            self.root_start, self.root_q, full_mask
        )

        # 规范位 -> 绝对地址。等前缀请求的规范位按下标顺序对应地址升序。
        canon_addr: dict[int, int] = {}
        for c, k in canon_slots.items():
            p = self._canon_prefix[c]
            canon_addr[c] = self.root_start + k * (1 << (self.width - p))

        networks: list[IPNetwork] = []
        for orig_i in range(self.m):
            c = self._orig_to_canon[orig_i]
            networks.append(
                ipaddress.ip_network(
                    (canon_addr[c], self.prefixes[orig_i]), strict=True
                )
            )

        free_after = self._free_after(networks)
        assert len(free_after) == cost, (
            f"free CIDR count mismatch: {len(free_after)} != {cost}"
        )
        return AllocationPlan(tuple(networks), tuple(free_after))

    def _free_after(
        self, allocated: list[IPNetwork]
    ) -> list[IPNetwork]:
        """从父网段中减去保留与分配子网，重新规范化（用于结果校验/展示）。"""
        blocked = list(self._blocked)
        blocked.extend(_network_bounds(net) for net in allocated)
        remaining = _subtract_ranges(
            _network_bounds(self.parent), _collapse_ranges(blocked)
        )
        return [
            ipaddress.ip_network((s, p), strict=True)
            for s, p in _ranges_to_cidrs(self.width, remaining)
        ]


def plan_allocation(
    parent: IPNetwork,
    reserved: list[IPNetwork],
    prefixes: tuple[int, ...],
) -> AllocationPlan:
    return CidrAllocationSolver(parent, reserved, prefixes).solve()
