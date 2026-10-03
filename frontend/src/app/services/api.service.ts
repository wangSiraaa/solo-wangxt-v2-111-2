import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import {
  BlendRequest, BlendResponse, CapacityResponse, Material, Occupation,
  OccupationConfirmResponse, OccupationEvent, OccupationPreview,
  OccupationSpec, RunDetail, RunSummary,
} from '../models/models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private base = '/api';

  constructor(private http: HttpClient) {}

  materials(activeOnly = false): Observable<Material[]> {
    return this.http.get<Material[]>(`${this.base}/materials`, {
      params: activeOnly ? { active_only: true } : {},
    });
  }

  blend(req: BlendRequest): Observable<BlendResponse> {
    return this.http.post<BlendResponse>(`${this.base}/blend`, req);
  }

  evaluate(picks: { material_id: number; assay_version_id?: number | null }[],
           shares: number[], scenarioName: string): Observable<any> {
    return this.http.post(`${this.base}/evaluate`, {
      scenario_name: scenarioName, picks, shares_pct_dry: shares,
    });
  }

  runs(): Observable<RunSummary[]> {
    return this.http.get<RunSummary[]>(`${this.base}/runs`);
  }

  run(id: number): Observable<RunDetail> {
    return this.http.get<RunDetail>(`${this.base}/runs/${id}`);
  }

  // ---------- 虚拟批次占用 ----------

  capacity(): Observable<CapacityResponse> {
    return this.http.get<CapacityResponse>(`${this.base}/capacity`);
  }

  previewOccupation(spec: OccupationSpec, replaceId: number | null,
                    expectedVersions: Record<string, number>):
      Observable<OccupationPreview> {
    return this.http.post<OccupationPreview>(
      `${this.base}/occupations/preview`,
      { spec, replace_occupation_id: replaceId, expected_versions: expectedVersions });
  }

  confirmOccupation(body: {
    spec: OccupationSpec; replace_occupation_id: number | null;
    expected_versions: Record<string, number>;
    idempotency_key: string; ttl_minutes: number;
  }): Observable<OccupationConfirmResponse> {
    return this.http.post<OccupationConfirmResponse>(
      `${this.base}/occupations/confirm`, body);
  }

  releaseOccupation(id: number, expectedVersions: Record<string, number> = {}):
      Observable<OccupationConfirmResponse> {
    return this.http.post<OccupationConfirmResponse>(
      `${this.base}/occupations/${id}/release`, { expected_versions: expectedVersions });
  }

  occupations(status?: string): Observable<Occupation[]> {
    const params: Record<string, string> = status ? { status } : {};
    return this.http.get<Occupation[]>(`${this.base}/occupations`, { params });
  }

  occupation(id: number): Observable<Occupation> {
    return this.http.get<Occupation>(`${this.base}/occupations/${id}`);
  }

  occupationEvents(): Observable<OccupationEvent[]> {
    return this.http.get<OccupationEvent[]>(`${this.base}/occupation-events`);
  }
}
