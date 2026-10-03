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

// ---------------- 虚拟批次占用 ----------------

export interface OccupationSpec {
  scenario_name: string;
  batch_t_dry: number;
  candidates: { material_id: number; assay_version_id?: number | null }[];
  targets: Targets;
  hazard_limits_pct?: Record<string, number>;
  mode: string;
  cheap_material_id?: number | null;
  source_run_id?: number | null;
  source_solution_id?: number | null;
}

export interface CapacityGap {
  material_id: number;
  material_code: string;
  material_name: string;
  availability_t_wet: number | null;
  already_occupied_t_wet: number;
  remaining_t_wet: number | null;
  requested_t_wet: number;
  gap_t_wet: number;
}

export interface OccupationItem {
  material_id: number;
  material_code: string;
  material_name: string;
  assay_version: string;
  lab_report_no: string;
  share_pct_dry: number;
  mass_t_dry: number;
  mass_t_wet: number;
  water_t: number;
  moisture_pct: number;
  cost: number;
  availability_t_wet: number | null;
  already_occupied_t_wet: number;
  requested_t_wet: number;
  remaining_t_wet_after: number | null;
  ledger_version: number;
  conversion_trace: any;
}

export interface OccupationEvent {
  id: number;
  occupation_id: number | null;
  event_type: string;
  event_reason: string | null;
  idempotency_key: string | null;
  detail: any;
  created_at: string;
}

export interface Occupation {
  id: number;
  occupation_code: string;
  scenario_name: string;
  batch_t_dry: number;
  mode: string;
  status: 'occupied' | 'released' | 'expired' | string;
  source_kind: string;
  source_run_id: number | null;
  source_solution_id: number | null;
  replaces_occupation_id: number | null;
  total_cost: number | null;
  created_at: string;
  expires_at: string;
  released_at: string | null;
  items: OccupationItem[];
  events: OccupationEvent[];
  solution?: any;
}

export interface MaterialCapacity {
  material_id: number;
  material_code: string;
  material_name: string;
  moisture_pct: number;
  availability_t_wet: number | null;
  occupied_t_wet: number;
  remaining_t_wet: number | null;
  ledger_version: number;
  active_occupation_ids: number[];
  active_items: {
    occupation_id: number;
    mass_t_wet: number;
    share_pct_dry: number;
    occupation_code: string;
  }[];
}

export interface CapacityResponse {
  as_of: string;
  expired_released: number[];
  materials: MaterialCapacity[];
}

export interface OccupationPreview {
  feasible: boolean;
  fits: boolean;
  replace_occupation_id: number | null;
  solution: Solution | null;
  items: OccupationItem[];
  gaps: CapacityGap[];
  current_versions: Record<string, number>;
  diagnostic?: any;
  message?: string | null;
}

export interface OccupationConfirmResponse {
  occupation: Occupation;
  replay: boolean;
  replaced_occupation_id: number | null;
  versions: Record<string, number>;
}
