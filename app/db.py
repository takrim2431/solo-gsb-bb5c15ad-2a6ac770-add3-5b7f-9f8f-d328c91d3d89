"""SQLite 持久化层：池版本、保留子网、已分配子网与原子提交。

并发模型
========
* 数据库以 WAL 模式打开；
* 每个请求使用独立短连接（check_same_thread=False 仅为线程兼容性）；
* 读取（池状态 / 预演）在单条 ``BEGIN`` 读事务内读取版本与子网，
  整个查询基于同一 SQLite 快照，不会拼出“旧版本 + 新占用集”的
  错位状态；
* 提交接口在单条 ``BEGIN IMMEDIATE`` 事务内完成“版本检查 + 方案可用性
  复检 + 插入 + 版本自增”，任何一步失败即整体回滚，绝不部分分配；
  提交响应使用的版本与分配结果同样取自该写事务内部，不会混入其它
  并发提交之后的状态。
* 回收 / 整批迁移接口同样在单条 ``BEGIN IMMEDIATE`` 事务内完成“版本
  检查 + 覆盖/冲突复核 + 已分配集重写 + 版本自增”；迁移按整批同时
  迁移的语义释放全部来源、占用全部目标，目标可使用本批来源释放的
  地址，任何一步失败即整体回滚。

历史版本
========
* 每次成功的写事务（分配提交 / 部分回收 / 整批迁移 / 保留区替换）在
  **同一事务内**为自增后的新版本追加一份不可变快照（父网段、保留与
  已分配 CIDR）；失败的操作整体回滚，不会留下任何版本快照；
* 旧库**首次升级**（快照表尚不存在/为空）时，在初始化阶段给当前池
  版本补存一份基线快照，此后该版本及后续版本均可查询；
* 快照只追加、从不更新：旧版本的状态不随后续写入变化，查询时依据
  快照内容重新计算该版本下的空闲 CIDR。
"""
from __future__ import annotations

import ipaddress
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from .allocator import (
    _collapse_ranges,
    _network_bounds,
    _ranges_to_cidrs,
    _subtract_ranges,
)
from .config import Settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    parent_cidr TEXT NOT NULL,
    version    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subnets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cidr       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL CHECK (kind IN ('reserved', 'allocated')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- 历史版本快照：每次成功写事务为自增后的新版本追加一行 parent、一行
-- 保留/已分配 CIDR 集合；只追加、从不更新，旧版本不随后续写入变化。
CREATE TABLE IF NOT EXISTS pool_versions (
    version    INTEGER PRIMARY KEY,
    parent_cidr TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS pool_version_subnets (
    version INTEGER NOT NULL,
    cidr    TEXT NOT NULL,
    kind    TEXT NOT NULL CHECK (kind IN ('reserved', 'allocated')),
    PRIMARY KEY (version, cidr),
    FOREIGN KEY (version) REFERENCES pool_versions (version)
);

CREATE INDEX IF NOT EXISTS idx_pool_version_subnets_version
    ON pool_version_subnets (version);
"""


@dataclass(frozen=True)
class PoolState:
    parent: ipaddress._BaseNetwork
    version: int
    reserved: tuple[ipaddress._BaseNetwork, ...]
    allocated: tuple[ipaddress._BaseNetwork, ...]


class ConflictError(Exception):
    """提交冲突：池版本已变化，或方案在当前池状态下不再可用。"""


class ReservationInputError(Exception):
    """保留目标集合非法：越出父网段或地址族与父网段不一致（HTTP 422）。"""


class RelocateInputError(Exception):
    """迁移端点非法：来源/目标越出父网段或地址族与父网段不一致（HTTP 422）。"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


class Database:
    # --- 测试钩子（生产环境默认为空操作）--------------------------------
    # 并发回归测试通过替换这两个钩子来精确控制读写事务的交错时刻：
    # _after_meta_select: 读事务内版本查询完成、子网查询开始之前；
    # _after_commit:      写事务 COMMIT 完成之后（结果状态已在提交前
    #                     于同一事务内读取，不受交错影响）。
    _after_meta_select = staticmethod(lambda conn: None)
    _after_commit = staticmethod(lambda conn: None)

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._db_path = settings.db_path
        self._init_lock = threading.Lock()
        self.initialize()

    @contextmanager
    def _conn(self):
        conn = _connect(self._db_path)
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        """建表并在首次启动时写入父网段与保留子网（幂等）。

        旧库**首次升级**到带版本快照的结构时，``pool_versions`` 为空：
        给当前池版本补存一份基线快照（父网段 + 当前保留/已分配 CIDR），
        使当前版本及后续每个成功写入的版本都可查询。基线补存与首次建
        池分别在独立事务内完成；进程在中途崩溃后重启会重新补存，幂等。
        """
        import os

        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with self._init_lock, self._conn() as conn:
            conn.executescript(_SCHEMA)
            row = conn.execute(
                "SELECT parent_cidr FROM pool_meta WHERE id = 1"
            ).fetchone()
            if row is None:
                parent = self._settings.parent
                reserved = self._settings.reserved
                conn.execute(
                    "INSERT INTO pool_meta (id, parent_cidr, version) "
                    "VALUES (1, ?, 1)",
                    (str(parent),),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO subnets (cidr, kind) VALUES (?, 'reserved')",
                    [(str(net),) for net in reserved],
                )
                conn.commit()
                # 新建池：版本 1 即为基线版本，在同一初始化过程补存。
                conn.execute("BEGIN")
                state = self._load_state(conn)
                self._insert_snapshot(conn, state)
                conn.commit()
            elif not conn.execute(
                "SELECT 1 FROM pool_versions LIMIT 1"
            ).fetchone():
                # 旧库首次升级：快照表为空，给当前池版本补存基线。
                # _load_state 要求调用方已开启事务；显式 BEGIN 使版本与
                # 子网取自同一快照。
                conn.execute("BEGIN")
                state = self._load_state(conn)
                self._insert_snapshot(conn, state)
                conn.commit()

    # -- 读取 --------------------------------------------------------------

    @staticmethod
    def _load_state(
        conn: sqlite3.Connection, between_reads=None
    ) -> PoolState:
        """在**当前事务的快照**内读取版本与全部子网并组装 PoolState。

        调用方必须已开启事务（``BEGIN`` / ``BEGIN IMMEDIATE``），保证
        版本与子网来自同一快照、写入对自身连接可见。``between_reads``
        为测试钩子，在两次查询之间调用。
        """
        meta = conn.execute(
            "SELECT parent_cidr, version FROM pool_meta WHERE id = 1"
        ).fetchone()
        if between_reads is not None:
            between_reads(conn)
        rows = conn.execute(
            "SELECT cidr, kind FROM subnets ORDER BY cidr"
        ).fetchall()
        parent = ipaddress.ip_network(meta["parent_cidr"], strict=True)
        reserved = tuple(
            ipaddress.ip_network(r["cidr"], strict=True)
            for r in rows
            if r["kind"] == "reserved"
        )
        allocated = tuple(
            ipaddress.ip_network(r["cidr"], strict=True)
            for r in rows
            if r["kind"] == "allocated"
        )
        return PoolState(parent, meta["version"], reserved, allocated)

    @staticmethod
    def _insert_snapshot(conn: sqlite3.Connection, state: PoolState) -> None:
        """在**当前写事务内**为 ``state.version`` 追加一份不可变快照。

        快照只追加：每个版本恰好一行 ``pool_versions`` 及其保留/已分配
        CIDR 行。版本号主键冲突（同一版本重复快照）属于程序错误，直接
        让 SQLite 抛出，由外层事务回滚。
        """
        conn.execute(
            "INSERT INTO pool_versions (version, parent_cidr) VALUES (?, ?)",
            (state.version, str(state.parent)),
        )
        conn.executemany(
            "INSERT INTO pool_version_subnets (version, cidr, kind) "
            "VALUES (?, ?, ?)",
            [
                (state.version, str(net), "reserved")
                for net in state.reserved
            ]
            + [
                (state.version, str(net), "allocated")
                for net in state.allocated
            ],
        )

    def get_state(self) -> PoolState:
        """读取池状态。

        版本与子网在同一条读事务内获取：SQLite WAL 下 ``BEGIN`` 后
        事务固定在开启时的快照，两条查询之间即使有并发提交落库，也
        不会返回“旧版本配新占用集”的错位结果。
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            state = self._load_state(conn, self._after_meta_select)
            conn.commit()
        return state

    def get_state_version(self, version: int) -> PoolState | None:
        """读取**历史版本**的不可变快照；未知版本返回 ``None``。

        数据全部来自 ``pool_versions`` / ``pool_version_subnets`` 快照
        表，与在线池状态（``pool_meta`` / ``subnets``）无关：后续写入只
        追加新版本快照，从不修改旧版本行，因此旧快照不随后续写入变化。
        父网段、保留与已分配 CIDR 在同一条读事务的快照内读取。
        """
        with self._conn() as conn:
            conn.execute("BEGIN")
            meta = conn.execute(
                "SELECT parent_cidr FROM pool_versions WHERE version = ?",
                (version,),
            ).fetchone()
            if meta is None:
                conn.commit()
                return None
            rows = conn.execute(
                "SELECT cidr, kind FROM pool_version_subnets "
                "WHERE version = ? ORDER BY cidr",
                (version,),
            ).fetchall()
            conn.commit()
        parent = ipaddress.ip_network(meta["parent_cidr"], strict=True)
        reserved = tuple(
            ipaddress.ip_network(r["cidr"], strict=True)
            for r in rows
            if r["kind"] == "reserved"
        )
        allocated = tuple(
            ipaddress.ip_network(r["cidr"], strict=True)
            for r in rows
            if r["kind"] == "allocated"
        )
        return PoolState(parent, version, reserved, allocated)

    # -- 原子提交 ----------------------------------------------------------

    def commit_plan(
        self,
        expected_version: int,
        networks: tuple[ipaddress._BaseNetwork, ...],
    ) -> PoolState:
        """在版本未变且方案仍可用时原子落库，否则抛 ConflictError。

        返回的 PoolState 在**本次写事务内部**、插入与版本自增之后读取，
        因此响应中的版本、占用集严格对应本次提交；不会在提交后另开
        连接而读到其它并发提交的结果。
        """
        cidr_strs = [str(net) for net in networks]
        with self._conn() as conn:
            try:
                # 立即获取写锁，与并发提交互斥。
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT parent_cidr, version FROM pool_meta WHERE id = 1"
                ).fetchone()
                current_version = row["version"]
                if current_version != expected_version:
                    conn.rollback()
                    raise ConflictError(
                        f"pool version changed: expected {expected_version}, "
                        f"current {current_version}"
                    )

                existing = {
                    r["cidr"]
                    for r in conn.execute(
                        "SELECT cidr FROM subnets"
                    ).fetchall()
                }
                collisions = [c for c in cidr_strs if c in existing]
                if collisions:
                    conn.rollback()
                    raise ConflictError(
                        f"allocation no longer available, conflicts: {collisions}"
                    )

                # 复检方案内部互不重叠（预演结果天然满足，防御性校验）。
                _assert_disjoint(networks)
                # 复检方案与父网段及所有现存子网（保留 + 已分配）相容。
                parent = ipaddress.ip_network(row["parent_cidr"], strict=True)
                occupied = [
                    ipaddress.ip_network(c, strict=True) for c in existing
                ]
                for net in networks:
                    if net.version != parent.version or not net.subnet_of(parent):
                        conn.rollback()
                        raise ConflictError(
                            f"allocation {net} outside of pool {parent}"
                        )
                    if any(net.overlaps(other) for other in occupied):
                        conn.rollback()
                        raise ConflictError(
                            f"allocation {net} overlaps existing subnet"
                        )
                    occupied.append(net)

                conn.executemany(
                    "INSERT INTO subnets (cidr, kind) VALUES (?, 'allocated')",
                    [(c,) for c in cidr_strs],
                )
                conn.execute(
                    "UPDATE pool_meta SET version = version + 1 WHERE id = 1"
                )
                # 在同一写事务内读取提交后的状态：版本与占用集必然对应
                # 本次写入（其它提交被写锁挡在事务之外）。
                state = self._load_state(conn)
                # 与本次写入同一事务追加版本快照：提交失败（含快照写入
                # 失败）时整体回滚，失败操作不生成任何版本。
                self._insert_snapshot(conn, state)
                conn.commit()
                # 测试钩子：COMMIT 完成之后。修复前的实现会在此刻另开
                # 连接读取状态，另一笔提交可能恰好落库；修复后结果已在
                # 本事务内确定，钩子不再能造成版本/内容错位。
                self._after_commit(conn)
            except ConflictError:
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise ConflictError(f"commit failed: {exc}") from exc
        return state

    # -- 已分配地址的部分回收 ---------------------------------------------

    def reclaim_allocated(
        self,
        expected_version: int,
        target: ipaddress._BaseNetwork,
    ) -> tuple[PoolState, tuple[ipaddress._BaseNetwork, ...]]:
        """回收一个**完全由当前已分配地址覆盖**的规范 CIDR。

        目标 CIDR 可跨越相邻的多条分配记录。在单条 ``BEGIN IMMEDIATE``
        事务内完成版本检查、覆盖性复检、扣除、重写与版本自增：任何一步
        失败（版本过期 / 越出父网段 / 含保留或未分配地址）即整体回滚，
        地址池不发生任何改动。

        扣除后把全部仍被占用的地址（全局合并相邻区间）重写为**最少**的
        规范 CIDR 再落库。返回写事务内读取的新状态与重写后的已分配
        CIDR 序列。
        """
        with self._conn() as conn:
            try:
                # 立即获取写锁，与并发提交/回收互斥。
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT parent_cidr, version FROM pool_meta WHERE id = 1"
                ).fetchone()
                current_version = row["version"]
                if current_version != expected_version:
                    conn.rollback()
                    raise ConflictError(
                        f"pool version changed: expected {expected_version}, "
                        f"current {current_version}"
                    )

                parent = ipaddress.ip_network(row["parent_cidr"], strict=True)
                if target.version != parent.version or not target.subnet_of(
                    parent
                ):
                    conn.rollback()
                    raise ConflictError(
                        f"reclaim target {target} is outside of pool {parent}"
                    )

                rows = conn.execute(
                    "SELECT cidr, kind FROM subnets"
                ).fetchall()
                reserved = [
                    ipaddress.ip_network(r["cidr"], strict=True)
                    for r in rows
                    if r["kind"] == "reserved"
                ]
                allocated = [
                    ipaddress.ip_network(r["cidr"], strict=True)
                    for r in rows
                    if r["kind"] == "allocated"
                ]

                if any(target.overlaps(net) for net in reserved):
                    conn.rollback()
                    raise ConflictError(
                        f"reclaim target {target} contains reserved address"
                    )

                target_range = _network_bounds(target)
                allocated_ranges = _collapse_ranges(
                    [_network_bounds(net) for net in allocated]
                )
                # 目标区间必须被已分配区间完整覆盖：目标区间减去已分配
                # 合并集后必须为空（不允许含未分配地址）。
                uncovered = _subtract_ranges(target_range, allocated_ranges)
                if uncovered:
                    conn.rollback()
                    raise ConflictError(
                        f"reclaim target {target} is not fully covered by "
                        f"allocated addresses"
                    )

                # 从已分配合并区间中扣除目标区间；_ranges_to_cidrs 对每个
                # 连续区间贪心切出最大对齐块，块数即该区间的最少规范 CIDR
                # 数量；先 _collapse_ranges 合并扣除后再次相邻的区间。
                leftover_ranges: list[tuple[int, int]] = []
                t_start, t_end = target_range
                for a_start, a_end in allocated_ranges:
                    if a_end < t_start or a_start > t_end:
                        leftover_ranges.append((a_start, a_end))
                        continue
                    if a_start < t_start:
                        leftover_ranges.append((a_start, t_start - 1))
                    if a_end > t_end:
                        leftover_ranges.append((t_end + 1, a_end))
                leftover_ranges = _collapse_ranges(leftover_ranges)
                new_allocated = [
                    ipaddress.ip_network((s, p), strict=True)
                    for s, p in _ranges_to_cidrs(
                        target.max_prefixlen, leftover_ranges
                    )
                ]

                # 重写已分配子网：删除全部 allocated 行后写回规范化结果。
                conn.execute("DELETE FROM subnets WHERE kind = 'allocated'")
                conn.executemany(
                    "INSERT INTO subnets (cidr, kind) "
                    "VALUES (?, 'allocated')",
                    [(str(net),) for net in new_allocated],
                )
                conn.execute(
                    "UPDATE pool_meta SET version = version + 1 WHERE id = 1"
                )
                # 与提交相同：在同一写事务内读取回收后的状态。
                state = self._load_state(conn)
                # 与本次写入同一事务追加版本快照：提交失败（含快照写入
                # 失败）时整体回滚，失败操作不生成任何版本。
                self._insert_snapshot(conn, state)
                conn.commit()
                self._after_commit(conn)
            except ConflictError:
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise ConflictError(f"reclaim failed: {exc}") from exc
        return state, tuple(state.allocated)

    # -- 已分配地址的整批同时迁移 -----------------------------------------

    def relocate_allocations(
        self,
        expected_version: int,
        moves: tuple[
            tuple[ipaddress._BaseNetwork, ipaddress._BaseNetwork], ...
        ],
    ) -> PoolState:
        """把 1～8 段已分配地址**整批同时**迁移到新的目标 CIDR。

        ``moves`` 为 ``(来源 CIDR, 目标 CIDR)`` 序列。调用前由接口层保证
        （非法输入对应 HTTP 422）：两端均为规范 CIDR、同组两端地址族与
        前缀长度相同、各来源之间互不重叠、各目标之间互不重叠。迁移语义：

        * 每个来源必须被**当前**已分配地址完整覆盖（可跨越相邻多条记录，
          也可只覆盖某条记录的一部分）；
        * 目标必须落在父网段内，**可**使用本批来源释放的地址（包括来源
          与目标相同的原地迁移），但不得触及保留区或未被本批迁移的已
          分配地址；
        * 迁移结果 = （当前已分配地址 − 全部来源）∪ 全部目标，落库前
          全局合并相邻区间并重写为覆盖范围相同且数量最少的规范 CIDR。

        在单条 ``BEGIN IMMEDIATE`` 事务内完成：来源/目标的父网段与地址
        族校验（非法输入抛 ``RelocateInputError``，对应 HTTP 422）、版本
        检查、来源覆盖与目标冲突复核、重写与版本自增。任何一步失败
        （版本过期 / 来源覆盖不足 / 目标触及保留区或未迁移的已分配
        地址）即整体回滚，地址池与版本不发生任何改动。返回写事务内
        读取的新状态。
        """
        with self._conn() as conn:
            try:
                # 立即获取写锁，与并发提交/回收/替换/迁移互斥。
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT parent_cidr, version FROM pool_meta WHERE id = 1"
                ).fetchone()
                parent = ipaddress.ip_network(row["parent_cidr"], strict=True)

                # 输入校验：来源与目标都必须同族且完全落在父网段内
                # （非法输入，非状态冲突）。
                for src, dst in moves:
                    for net in (src, dst):
                        if net.version != parent.version or not net.subnet_of(
                            parent
                        ):
                            conn.rollback()
                            raise RelocateInputError(
                                f"relocate CIDR {net} is outside of pool "
                                f"{parent}"
                            )

                current_version = row["version"]
                if current_version != expected_version:
                    conn.rollback()
                    raise ConflictError(
                        f"pool version changed: expected {expected_version}, "
                        f"current {current_version}"
                    )

                rows = conn.execute(
                    "SELECT cidr, kind FROM subnets"
                ).fetchall()
                reserved = [
                    ipaddress.ip_network(r["cidr"], strict=True)
                    for r in rows
                    if r["kind"] == "reserved"
                ]
                allocated = [
                    ipaddress.ip_network(r["cidr"], strict=True)
                    for r in rows
                    if r["kind"] == "allocated"
                ]

                sources = [src for src, _ in moves]
                targets = [dst for _, dst in moves]

                allocated_ranges = _collapse_ranges(
                    [_network_bounds(net) for net in allocated]
                )

                # 覆盖性复核：每个来源减去已分配合并集后必须为空，即
                # 完全由当前已分配地址覆盖（不允许含未分配或保留地址）。
                for src in sources:
                    if _subtract_ranges(
                        _network_bounds(src), allocated_ranges
                    ):
                        conn.rollback()
                        raise ConflictError(
                            f"relocate source {src} is not fully covered by "
                            f"allocated addresses"
                        )

                # 迁移后仍被占用的已分配区间：已分配合并集扣除全部来源。
                source_ranges = _collapse_ranges(
                    [_network_bounds(net) for net in sources]
                )
                remaining_ranges: list[tuple[int, int]] = []
                for bounds in allocated_ranges:
                    remaining_ranges.extend(
                        _subtract_ranges(bounds, source_ranges)
                    )
                remaining_ranges = _collapse_ranges(remaining_ranges)

                # 目标冲突复核：不触及保留区、不触及未迁移的已分配
                # 地址；与本批来源释放的地址重叠是允许的。
                for dst in targets:
                    if any(dst.overlaps(net) for net in reserved):
                        conn.rollback()
                        raise ConflictError(
                            f"relocate target {dst} overlaps reserved address"
                        )
                    d_start, d_end = _network_bounds(dst)
                    if any(
                        d_start <= r_end and r_start <= d_end
                        for r_start, r_end in remaining_ranges
                    ):
                        conn.rollback()
                        raise ConflictError(
                            f"relocate target {dst} overlaps allocated "
                            f"address that is not being migrated"
                        )

                # 重写：剩余已分配区间与目标区间合并（相邻区间被
                # _collapse_ranges 合并），再贪心切出最大对齐块，块数即
                # 覆盖范围相同的最少规范 CIDR 数量。
                new_ranges = _collapse_ranges(
                    remaining_ranges
                    + [_network_bounds(net) for net in targets]
                )
                new_allocated = [
                    ipaddress.ip_network((s, p), strict=True)
                    for s, p in _ranges_to_cidrs(
                        parent.max_prefixlen, new_ranges
                    )
                ]

                # 重写已分配子网：删除全部 allocated 行后写回规范化结果。
                conn.execute("DELETE FROM subnets WHERE kind = 'allocated'")
                conn.executemany(
                    "INSERT INTO subnets (cidr, kind) "
                    "VALUES (?, 'allocated')",
                    [(str(net),) for net in new_allocated],
                )
                conn.execute(
                    "UPDATE pool_meta SET version = version + 1 WHERE id = 1"
                )
                # 与提交/回收/替换相同：在同一写事务内读取迁移后的状态。
                state = self._load_state(conn)
                # 与本次写入同一事务追加版本快照：提交失败（含快照写入
                # 失败）时整体回滚，失败操作不生成任何版本。
                self._insert_snapshot(conn, state)
                conn.commit()
                self._after_commit(conn)
            except (ConflictError, RelocateInputError):
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise ConflictError(f"relocate failed: {exc}") from exc
        return state

    # -- 保留子网的整体替换 -------------------------------------------------

    def replace_reservations(
        self,
        expected_version: int,
        desired: tuple[ipaddress._BaseNetwork, ...],
    ) -> PoolState:
        """把保留子网整体替换为 ``desired`` 覆盖范围的最少规范 CIDR 集合。

        ``desired`` 是保留地址的**完整目标集合**（而非增量）：允许为空
        （清空全部保留地址）、相互重叠或相邻；落库前先合并相交/相邻区间，
        再重写为覆盖范围相同且数量最少的规范 CIDR。

        在单条 ``BEGIN IMMEDIATE`` 事务内完成：父网段/地址族校验、版本
        核对、与已分配地址的冲突复核、删除并重写保留记录、版本自增。
        任何一步失败即整体回滚，池状态与版本均不变。返回写事务内读取的
        新状态。
        """
        with self._conn() as conn:
            try:
                # 立即获取写锁，与并发提交/回收/替换互斥。
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT parent_cidr, version FROM pool_meta WHERE id = 1"
                ).fetchone()
                parent = ipaddress.ip_network(row["parent_cidr"], strict=True)

                # 输入校验：同族且完全落在父网段内（非法输入，非状态冲突）。
                for net in desired:
                    if net.version != parent.version or not net.subnet_of(
                        parent
                    ):
                        conn.rollback()
                        raise ReservationInputError(
                            f"reserved CIDR {net} is outside of pool {parent}"
                        )

                current_version = row["version"]
                if current_version != expected_version:
                    conn.rollback()
                    raise ConflictError(
                        f"pool version changed: expected {expected_version}, "
                        f"current {current_version}"
                    )

                # 规范化：合并相交/相邻区间后，对每个连续区间贪心切出最大
                # 对齐块，块数即覆盖范围相同的最少规范 CIDR 数量；空列表
                # 得到空集合（清空保留区）。
                desired_ranges = _collapse_ranges(
                    [_network_bounds(net) for net in desired]
                )
                normalized = [
                    ipaddress.ip_network((s, p), strict=True)
                    for s, p in _ranges_to_cidrs(
                        parent.max_prefixlen, desired_ranges
                    )
                ]

                # 冲突复核：目标范围不得与任何已分配地址相交（与既有保留
                # 子网重叠无关紧要——它们将被整体替换）。
                allocated = [
                    ipaddress.ip_network(r["cidr"], strict=True)
                    for r in conn.execute(
                        "SELECT cidr FROM subnets WHERE kind = 'allocated'"
                    ).fetchall()
                ]
                collisions = [
                    str(net)
                    for net in normalized
                    if any(net.overlaps(other) for other in allocated)
                ]
                if collisions:
                    conn.rollback()
                    raise ConflictError(
                        f"desired reservations overlap allocated addresses: "
                        f"{collisions}"
                    )

                # 整体替换保留记录：删除全部 reserved 行后写回规范化结果。
                conn.execute("DELETE FROM subnets WHERE kind = 'reserved'")
                conn.executemany(
                    "INSERT INTO subnets (cidr, kind) VALUES (?, 'reserved')",
                    [(str(net),) for net in normalized],
                )
                conn.execute(
                    "UPDATE pool_meta SET version = version + 1 WHERE id = 1"
                )
                # 与提交/回收相同：在同一写事务内读取替换后的状态。
                state = self._load_state(conn)
                # 与本次写入同一事务追加版本快照：提交失败（含快照写入
                # 失败）时整体回滚，失败操作不生成任何版本。
                self._insert_snapshot(conn, state)
                conn.commit()
                self._after_commit(conn)
            except (ConflictError, ReservationInputError):
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise ConflictError(
                    f"replace reservations failed: {exc}"
                ) from exc
        return state


def _assert_disjoint(networks: tuple[ipaddress._BaseNetwork, ...]) -> None:
    ordered = sorted(networks, key=lambda n: int(n.network_address))
    for a, b in zip(ordered, ordered[1:]):
        if a.overlaps(b):
            raise ConflictError(f"plan contains overlapping subnets {a}, {b}")
