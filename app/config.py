"""应用配置（全部可通过环境变量覆盖）。"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field


def _parse_cidrs(raw: str | None) -> list[ipaddress._BaseNetwork]:
    if not raw or not raw.strip():
        return []
    networks: list[ipaddress._BaseNetwork] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=True))
    return networks


@dataclass(frozen=True)
class Settings:
    db_path: str = field(
        default_factory=lambda: os.getenv("DB_PATH", "/data/cidr_pool.db")
    )
    parent_cidr: str = field(
        default_factory=lambda: os.getenv("PARENT_CIDR", "10.0.0.0/8")
    )
    reserved_cidrs_raw: str = field(
        default_factory=lambda: os.getenv("RESERVED_CIDRS", "")
    )

    @property
    def parent(self) -> ipaddress._BaseNetwork:
        return ipaddress.ip_network(self.parent_cidr, strict=True)

    @property
    def reserved(self) -> list[ipaddress._BaseNetwork]:
        networks = _parse_cidrs(self.reserved_cidrs_raw)
        parent = self.parent
        for net in networks:
            if net.version != parent.version or not net.subnet_of(parent):
                raise ValueError(
                    f"reserved CIDR {net} is not within parent {parent}"
                )
        return networks


settings = Settings()
