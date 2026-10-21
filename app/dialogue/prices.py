"""Локальный справочник платных услуг, отдельный от сценарных фикстур."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.scheduling.matching import profile_key


class PaidPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    service: str = Field(min_length=1, max_length=160)
    price: int = Field(gt=0)


class PaidPriceCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prices: list[PaidPrice] = Field(default_factory=list, max_length=500)


def load_paid_prices(path: Path | None) -> dict[str, int]:
    if path is None:
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        catalog = PaidPriceCatalog.model_validate(document)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ValueError("не удалось загрузить локальный справочник цен") from error
    prices: dict[str, int] = {}
    for item in catalog.prices:
        key = profile_key(item.service)
        if key in prices:
            raise ValueError("в справочнике цен повторяется услуга")
        prices[key] = item.price
    return prices
