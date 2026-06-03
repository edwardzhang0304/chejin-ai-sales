export type SalesItem = {
  id: string;
  sales_name: string;
  phone: string | null;
  wechat: string | null;
  feishu_user_id: string | null;
  enabled: boolean;
  sort_order: number | null;
  remark: string | null;
  lead_count: number;
};

export type SalesUpsertPayload = {
  sales_name: string;
  phone?: string | null;
  wechat?: string | null;
  feishu_user_id?: string | null;
  enabled: boolean;
  sort_order?: number | null;
  remark?: string | null;
};
