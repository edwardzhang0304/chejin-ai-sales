// Display choices mirror the vehicle API contract; persistence validation remains server-owned.
export const energyTypes = { fuel: "燃油", hybrid: "新能源混动", range_extended: "新能源增程", electric: "新能源纯电" };
export const seriesOptions = ["国产新能源", "合资新能源", "国产车", "德系车", "日系车", "美系车", "韩系车", "法系车", "其他"];
export const driveTypes = { front_wheel_drive: "前驱", rear_wheel_drive: "后驱", four_wheel_drive: "四驱" };

export function batteryCapacityError(value: string): string | null {
  const text = value.trim();
  if (!text) return null;
  // Never convert this exact decimal text into a JS number.
  return text.length <= 100 && /^[0-9]+(?:\.[0-9]+)?$/.test(text) && /[1-9]/.test(text)
    ? null : "电池包容量必须是大于 0 的十进制数字，最多 100 字符，不含单位或科学计数法。";
}

export function vehicleOptionLabel(options: Record<string, string>, value: string | null): string {
  return value ? options[value] || "未填写" : "未填写";
}
