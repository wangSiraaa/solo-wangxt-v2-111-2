export interface AssayVersion {
  id: number;
  version: string;
  lab_report_no: string;
  assayed_at: string;
  basis: 'dry' | 'wet';
  composition: Record<string, number>;
  measured_oxides: string[];
}

export interface Material {
  id: number;
  code: string;
  name: string;
  category: string;
  moisture_pct: number;
  cost_per_t_wet: number;
  availability_t_wet: number | null;
  min_share_pct: number;
  is_active: boolean;
  note?: string | null;
  assay_versions: AssayVersion[];
}

export interface Interval { min?: number | null; max?: number | null; }
export interface Targets { SM: Interval; IM: Interval; KH: Interval; }

export interface BlendRequest {
  scenario_name: string;
  batch_t_dry: number;
  candidates: { material_id: number; assay_version_id?: number | null }[];
  targets: Targets;
  hazard_limits_pct: Record<string, number>;
  modes: string[];
  cheap_material_id?: number | null;
  save?: boolean;
}

export interface ConversionStep {
  component: string;
  basis_in: string;
  value_in: number;
  formula: string;
  factor: number;
  basis_out: string;
  value_out: number;
}

export interface SolutionItem {
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  share_pct_dry: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  cost: number;
  conversion_trace: {
    material_code: string;
    material_name: string;
    moisture_pct: number;
    assay_basis: string;
    dry_factor: number;
    steps: ConversionStep[];
    mass_balance?: any;
  };
}

export interface Conflict {
  constraint: string;
  limit?: number;
  achieved?: number;
  normalized_gap: number;
}

export interface Solution {
  mode: string;
  mode_label: string;
  success: boolean;
  total_cost?: number;
  cost_per_t_dry?: number;
  indicators?: {
    SM: number; IM: number; KH: number;
    CaO: number; SiO2: number; Al2O3: number; Fe2O3: number;
    warnings: string[];
  };
  composition_dry_pct?: Record<string, number>;
  composition_wet_pct?: Record<string, number>;
  water_pct_in_wet_mix?: number;
  items: SolutionItem[];
  diagnostic?: {
    reason: string; message: string;
    conflicts: Conflict[];
    min_violation_objective?: number;
  };
}

export interface BlendResponse {
  run_id: number | null;
  run_code: string;
  status: string;
  solutions: Solution[];
}

export interface RunSummary {
  id: number; run_code: string; scenario_name: string;
  status: string; created_at: string; modes: string[];
}

export interface RunDetail {
  id: number; run_code: string; scenario_name: string;
  batch_t_dry: number; target: Targets; constraint_set: any;
  status: string; created_at: string;
  solutions: any[];
}

// ---- 虚拟批次占用 ----

export interface OccupationPreviewRequest {
  scenario_name: string;
  batch_t_dry: number;
  candidates: { material_id: number; assay_version_id?: number | null }[];
  targets: Targets;
  hazard_limits_pct: Record<string, number>;
  mode: string;
  cheap_material_id?: number | null;
  ttl_seconds: number;
  idempotency_key?: string | null;
}

export interface OccupationItem {
  material_id: number;
  material_code: string;
  material_name: string;
  assay_version_id: number;
  assay_version: string;
  lab_report_no: string;
  moisture_pct: number;
  dry_factor: number;
  share_pct_dry: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  cost: number;
  conversion_trace: any;
  available_t_wet_snapshot: number | null;
  occupied_before_t_wet: number | null;
  remaining_after_t_wet_snapshot: number | null;
}

export interface CapacityContributor {
  occupation_id: number;
  occ_code?: string;
  scenario_name?: string;
  mass_t_wet: number;
  occupied_at?: string | null;
  expires_at?: string | null;
}

export interface CapacityRow {
  material_id: number;
  material_code: string;
  material_name: string;
  availability_t_wet: number | null;
  occupied_t_wet: number;
  requested_t_wet?: number;
  remaining_t_wet: number | null;
  unlimited: boolean;
  would_fit?: boolean;
  effective_occupations: CapacityContributor[];
}

export interface CapacityResponse {
  swept_expired: number;
  materials: CapacityRow[];
}

export interface OccupationEvent {
  id: number;
  occupation_id: number | null;
  occ_code: string | null;
  event_type: string;
  created_at: string;
  idempotency_key: string | null;
  detail: any;
}

export interface Occupation {
  id: number;
  occ_code: string;
  scenario_name: string;
  status: 'draft' | 'occupied' | 'released' | 'expired' | string;
  version: number;
  batch_t_dry: number;
  mode: string;
  total_cost: number | null;
  run_id: number | null;
  solution_id: number | null;
  occupied_at: string | null;
  expires_at: string | null;
  released_at: string | null;
  replace_reason: string | null;
  created_at: string;
  items: OccupationItem[];
  events: OccupationEvent[];
}

export interface OccupationPreviewResponse {
  occupation_id: number | null;
  occ_code: string | null;
  status: string;
  feasible: boolean;
  mode: string;
  total_cost: number | null;
  items: OccupationItem[];
  capacity: CapacityRow[];
  indicators: any;
  diagnostic?: any;
  ttl_seconds: number | null;
  version?: number | null;
}

export interface OccupationActionRequest {
  expected_version: number;
  idempotency_key?: string | null;
  note?: string | null;
}

export interface OccupationReplaceRequest extends OccupationPreviewRequest {
  expected_version: number;
  replace_note?: string | null;
}
