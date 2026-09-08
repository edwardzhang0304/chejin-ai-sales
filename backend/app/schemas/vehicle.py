from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


YEAR_MONTH_RE = re.compile(r"^(19|20)\d{2}-(0[1-9]|1[0-2])$")
ENERGY_TYPES = {"fuel": "燃油", "hybrid": "新能源混动", "range_extended": "新能源增程", "electric": "新能源纯电"}
SERIES_OPTIONS = ("国产新能源", "合资新能源", "国产车", "德系车", "日系车", "美系车", "韩系车", "法系车", "其他")
DRIVE_TYPES = {"front_wheel_drive": "前驱", "rear_wheel_drive": "后驱", "four_wheel_drive": "四驱"}
BATTERY_DECIMAL_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


class VehicleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)
    brand: str | None = Field(default=None, max_length=100)
    series: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)
    energy_type: str | None = None
    displacement: str | None = Field(default=None, max_length=100)
    battery_capacity_kwh: str | None = Field(default=None, max_length=100)
    drive_type: str | None = None
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

    @field_validator("series", "energy_type", "drive_type")
    @classmethod
    def validate_selection(cls, value, info):
        options = {"series": SERIES_OPTIONS, "energy_type": ENERGY_TYPES, "drive_type": DRIVE_TYPES}
        if value is not None and value not in options[info.field_name]:
            raise ValueError("请选择有效选项；清空请提交 null")
        return value

    @field_validator("displacement", "battery_capacity_kwh", mode="before")
    @classmethod
    def blank_to_null(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("battery_capacity_kwh")
    @classmethod
    def validate_battery_capacity(cls, value):
        if value is not None and (not BATTERY_DECIMAL_RE.fullmatch(value) or Decimal(value) <= 0):
            raise ValueError("电池包容量必须是大于 0 的十进制数字文本，不含单位或科学计数法")
        return value

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
