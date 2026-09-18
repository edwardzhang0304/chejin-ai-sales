/** phone_format_v1: formatting whitespace only; never repair/truncate digits. */
export const PHONE_PATTERN = /^1[3-9][0-9]{9}$/;

export function normalizePhoneInput(value: string): string {
  return value.replace(/[ \u3000\u00a0\t\r\n]/g, "");
}
