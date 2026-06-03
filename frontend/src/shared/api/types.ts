export type ApiEnvelope<T> = {
  code: string;
  message: string;
  data: T;
  trace_id?: string | null;
};

export type ApiErrorPayload = {
  code: string;
  message: string;
  data: unknown;
  status: number;
  traceId?: string | null;
};

export type PageResult<T> = {
  items: T[];
  page: number;
  page_size: number;
  total: number;
};
