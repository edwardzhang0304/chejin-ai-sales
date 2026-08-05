from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


YEAR_MONTH_RE = re.compile(r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")


class VehicleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    series: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    public_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    first_registration: str | None = Field(default=None, max_length=7)
    mileage_km: int | None = Field(default=None, ge=0, le=10_000_000)
    exterior_color: str | None = Field(default=None, max_length=64)
    interior_color: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=128)
    customer_description: str | None = Field(default=None, max_length=5000)
    vin: str | None = Field(default=None, max_length=64)
    plate_number: str | None = Field(default=None, max_length=32)
    purchase_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    internal_notes: str | None = Field(default=None, max_length=5000)

    @field_validator(
        "display_name",
        "brand",
        "series",
        "model",
        "first_registration",
        "exterior_color",
        "interior_color",
        "location",
        "customer_description",
        "vin",
        "plate_number",
        "internal_notes",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("first_registration")
    @classmethod
    def validate_year_month(cls, value: str | None) -> str | None:
        if value and not YEAR_MONTH_RE.fullmatch(value):
            raise ValueError("首次上牌日期必须为 YYYY-MM")
        return value


class VehicleCreate(VehicleFields):
    display_name: str = Field(min_length=1, max_length=200)


class VehicleUpdate(VehicleFields):
    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个待修改字段")
        if "display_name" in self.model_fields_set and not self.display_name:
            raise ValueError("车辆展示名称不能为空")
        return self


class VehicleImageOrderRequest(BaseModel):
    image_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("image_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("图片 ID 不得重复")
        return value
