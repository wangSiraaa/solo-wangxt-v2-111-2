import { HttpClient } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';
import {
  BlendRequest, BlendResponse, CapacityResponse, Material,
  Occupation, OccupationActionRequest, OccupationEvent,
  OccupationPreviewRequest, OccupationPreviewResponse,
  OccupationReplaceRequest, RunDetail, RunSummary,
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

  // ---- 虚拟批次占用 ----

  capacity(): Observable<CapacityResponse> {
    return this.http.get<CapacityResponse>(`${this.base}/occupations/capacity`);
  }

  occupations(status?: string): Observable<Occupation[]> {
    return this.http.get<Occupation[]>(`${this.base}/occupations`, {
      params: status ? { status } : {},
    });
  }

  occupation(id: number): Observable<Occupation> {
    return this.http.get<Occupation>(`${this.base}/occupations/${id}`);
  }

  occupationEvents(id?: number): Observable<OccupationEvent[]> {
    return this.http.get<OccupationEvent[]>(`${this.base}/occupations/events`, {
      params: id != null ? { occupation_id: id } : {},
    });
  }

  previewOccupation(req: OccupationPreviewRequest):
    Observable<OccupationPreviewResponse> {
    return this.http.post<OccupationPreviewResponse>(
      `${this.base}/occupations/preview`, req);
  }

  confirmOccupation(id: number, body: OccupationActionRequest):
    Observable<Occupation> {
    return this.http.post<Occupation>(
      `${this.base}/occupations/${id}/confirm`, body);
  }

  releaseOccupation(id: number, body: OccupationActionRequest):
    Observable<Occupation> {
    return this.http.post<Occupation>(
      `${this.base}/occupations/${id}/release`, body);
  }

  replaceOccupation(oldId: number, req: OccupationReplaceRequest):
    Observable<{ replaced: Occupation; occupation: Occupation }> {
    return this.http.post<{ replaced: Occupation; occupation: Occupation }>(
      `${this.base}/occupations/${oldId}/replace`, req);
  }
}
