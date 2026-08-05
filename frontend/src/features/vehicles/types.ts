export type VehicleListingStatus = "listed" | "unlisted";
export type VehicleListingFilter = "all" | VehicleListingStatus;

export type VehicleImage = {
  id: string;
  url: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  sort_order: number;
  is_main: boolean;
  created_at: string;
};

export type VehicleItem = {
  vehicle_code: string;
  display_name: string;
  brand: string | null;
  series: string | null;
  model: string | null;
  public_price: number | string | null;
  first_registration: string | null;
  mileage_km: number | null;
  exterior_color: string | null;
  interior_color: string | null;
  location: string | null;
  customer_description: string | null;
  vin: string | null;
  plate_number: string | null;
  purchase_price: number | string | null;
  internal_notes: string | null;
  listing_status: VehicleListingStatus;
  images: VehicleImage[];
  main_image: VehicleImage | null;
  created_at: string;
  updated_at: string;
};

export type VehicleEditableFields = Pick<
  VehicleItem,
  | "display_name"
  | "brand"
  | "series"
  | "model"
  | "public_price"
  | "first_registration"
  | "mileage_km"
  | "exterior_color"
  | "interior_color"
  | "location"
  | "customer_description"
  | "vin"
  | "plate_number"
  | "purchase_price"
  | "internal_notes"
>;

export type VehicleListResult = {
  items: VehicleItem[];
  page: number;
  page_size: number;
  total: number;
};

export type VehicleUploadResultItem = {
  filename: string;
  ok: boolean;
  duplicated?: boolean;
  image?: VehicleImage;
  error_code?: string;
};

export type VehicleUploadResult = {
  items: VehicleUploadResultItem[];
  succeeded: number;
  failed: number;
};

export type VehicleImportRow = {
  row_number: number;
  vehicle_code: string;
  action: "create" | "update";
  data: Partial<VehicleEditableFields>;
  errors: string[];
};

export type VehicleImportPreview = {
  preview_id: string;
  status: "pending" | "confirmed" | "expired";
  expires_at: string;
  total_rows: number;
  create_count: number;
  update_count: number;
  error_count: number;
  can_confirm: boolean;
  rows: VehicleImportRow[];
};

export type VehicleImportResult = VehicleImportPreview & {
  duplicated: boolean;
  confirmed_at?: string;
  imported_count?: number;
};
