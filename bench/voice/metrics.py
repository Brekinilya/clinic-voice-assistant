"""Helpers for measurements emitted by the voice load probe."""


def memory_mib(value: str) -> float:
    """Parse Docker's MemUsage value (for example, 40.44MiB) into MiB."""
    units = {"B": 1 / (1024 * 1024), "KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "TiB": 1024 * 1024}
    for suffix, factor in sorted(units.items(), key=lambda item: len(item[0]), reverse=True):
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * factor
    raise ValueError(f"unsupported Docker memory unit: {value}")
