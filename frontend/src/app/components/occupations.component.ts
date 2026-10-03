import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api.service';
import {
  CapacityResponse, CapacityRow, Occupation, OccupationEvent,
  OccupationPreviewResponse, Targets,
} from '../models/models';

const WIDE: Targets = {
  SM: { min: 0, max: 10 }, IM: { min: 0, max: 10 }, KH: { min: 0, max: 2 },
};

/** 验收场景：宽率值窗口 + LS01/SS01/IR01，min_cost ⇒ LS 干基 60%/SS 40%；
 * B=1880 ⇒ SS01 湿料 800 t（打满 800 t 可用量），B=705 ⇒ 300 t。 */
interface ScenarioPreset {
  key: string; label: string; desc: string;
  batch: number; name: string;
}

@Component({
  selector: 'app-occupations',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './occupations.component.html',
})
export class OccupationsComponent implements OnInit {
  cap: CapacityResponse | null = null;
  occs: Occupation[] = [];
  events: OccupationEvent[] = [];
  detail: Occupation | null = null;
  busy = false;
  apiError: any = null;
  info: string | null = null;
  sweptNote = '';

  // 试算/预览输入
  scenario = '方案甲';
  batch = 1880;
  ttl = 3600;
  presetKey = 'A800';
  presets: ScenarioPreset[] = [
    { key: 'A800', label: '方案甲：占 800 t SS01', desc: 'B=1880，SS01 湿料 800 t，打满可用量', batch: 1880, name: '方案甲' },
    { key: 'B300', label: '方案乙：再申请 300 t', desc: 'B=705，SS01 湿料 300 t（被占满时应被拒）', batch: 705, name: '方案乙' },
    { key: 'SMALL', label: '较小可行方案：占 200 t', desc: 'B=470，SS01 湿料 200 t（用于替换甲）', batch: 470, name: '甲(缩小)' },
  ];

  preview: OccupationPreviewResponse | null = null;
  replaceTarget: Occupation | null = null;
  idemKey = '';
  showTraceFor: number | null = null;
  expandedMat: number | null = null;

  constructor(private api: ApiService) {}

  ngOnInit(): void { this.refreshAll(); }

  refreshAll(): void {
    this.api.capacity().subscribe(r => {
      this.cap = r;
      this.sweptNote = r.swept_expired > 0
        ? `已自动释放 ${r.swept_expired} 条过期占用` : '';
    });
    this.api.occupations().subscribe(os => { this.occs = os; });
    this.api.occupationEvents().subscribe(es => { this.events = es.slice(0, 100); });
    if (this.detail) {
      this.api.occupation(this.detail.id).subscribe(d => { this.detail = d; });
    }
  }

  applyPreset(k: string): void {
    const p = this.presets.find(x => x.key === k);
    if (p) { this.batch = p.batch; this.scenario = p.name; }
  }

  openDetail(o: Occupation): void {
    this.api.occupation(o.id).subscribe(d => { this.detail = d; });
  }

  openDetailById(id: number): void {
    const o = this.occs.find(x => x.id === id);
    if (o) { this.openDetail(o); }
  }

  confirmById(o: Occupation): void {
    this.apiError = null;
    this.api.confirmOccupation(o.id, { expected_version: o.version }).subscribe({
      next: () => { this.info = `${o.occ_code} 已确认占用`; this.refreshAll(); },
      error: e => this.onConflict(e),
    });
  }

  reqBody(mode: string, extra: Record<string, any> = {}) {
    return {
      scenario_name: this.scenario,
      batch_t_dry: this.batch,
      candidates: [1, 2, 5].map(id => ({ material_id: id })),
      targets: WIDE,
      hazard_limits_pct: {},
      mode,
      ttl_seconds: this.ttl,
      idempotency_key: this.idemKey || null,
      ...extra,
    };
  }

  previewPlan(): void {
    this.apiError = null; this.info = null; this.preview = null;
    this.busy = true;
    this.api.previewOccupation(this.reqBody('min_cost')).subscribe({
      next: r => {
        this.preview = r; this.busy = false;
        this.refreshAll();
        if (!r.feasible) {
          this.apiError = {
            error_code: 'INFEASIBLE',
            message: '方案无可行解（未建草稿、不占量），见冲突项诊断。',
          };
        }
      },
      error: e => {
        this.apiError = e.error ?? { message: e.message };
        this.busy = false;
        this.refreshAll();
      },
    });
  }

  confirmPreview(): void {
    if (this.preview && this.preview.occupation_id != null
        && this.preview.version != null) {
      this.confirm(this.preview, this.preview.version);
    }
  }

  confirm(p: OccupationPreviewResponse, version: number): void {
    if (p.occupation_id == null) { return; }
    this.busy = true; this.apiError = null;
    this.api.confirmOccupation(p.occupation_id, {
      expected_version: version,
      idempotency_key: this.idemKey || null,
    }).subscribe({
      next: o => {
        this.busy = false; this.preview = null;
        this.info = `已确认占用 ${o.occ_code}（版本 ${o.version}，未双扣量）`;
        this.refreshAll();
      },
      error: e => { this.busy = false; this.onConflict(e); },
    });
  }

  release(o: Occupation): void {
    this.apiError = null;
    this.api.releaseOccupation(o.id, {
      expected_version: o.version, note: '人工释放',
    }).subscribe({
      next: () => { this.info = `${o.occ_code} 已释放`; this.refreshAll(); },
      error: e => this.onConflict(e),
    });
  }

  startReplace(o: Occupation): void {
    this.replaceTarget = o;
    this.batch = 470; this.scenario = '甲(缩小)'; this.presetKey = 'SMALL';
    this.preview = null; this.apiError = null;
  }

  cancelReplace(): void { this.replaceTarget = null; }

  doReplace(): void {
    if (!this.replaceTarget) { return; }
    this.busy = true; this.apiError = null;
    this.api.replaceOccupation(this.replaceTarget.id,
      this.reqBody('min_cost', {
        expected_version: this.replaceTarget.version,
        replace_note: '替换为较小可行方案',
      }) as any).subscribe({
      next: r => {
        this.busy = false; this.replaceTarget = null; this.preview = null;
        this.info = `已原子替换：旧占用 ${r.replaced.occ_code} 留痕为已释放，`
          + `新占用 ${r.occupation.occ_code} 已生效`;
        this.refreshAll();
      },
      error: e => { this.busy = false; this.onConflict(e); },
    });
  }

  onConflict(e: any): void {
    const err = e.error ?? { message: e.message };
    this.apiError = err;
    this.refreshAll();
  }

  statusLabel(s: string): string {
    return ({ draft: '草稿', occupied: '已占用', released: '已释放',
      expired: '已过期' } as Record<string, string>)[s] ?? s;
  }

  statusCls(s: string): string {
    return ({ draft: 'badge', occupied: 'badge ok', released: 'badge warn',
      expired: 'badge err' } as Record<string, string>)[s] ?? 'badge';
  }

  eventLabel(t: string): string {
    return ({
      created: '创建草稿', confirmed: '确认占用',
      confirm_rejected: '确认被拒', released: '释放', expired: '过期释放',
      replaced: '被替换', replace_rejected: '替换被拒',
      missing_assay_rejected: '缺测拒绝', infeasible_rejected: '无解拒绝',
      release_rejected: '释放被拒',
    } as Record<string, string>)[t] ?? t;
  }

  eventCls(t: string): string {
    return t.includes('rejected') ? 'badge err'
      : (t === 'expired' ? 'badge warn' : 'badge ok');
  }

  totalWet(o: Occupation): number {
    return o.items.reduce((s, i) => s + i.mass_t_wet, 0);
  }

  presetDesc(): string {
    return this.presets.find(p => p.batch === this.batch)?.desc ?? '';
  }

  dash(v: number | null | undefined): string {
    return v == null ? '—' : String(v);
  }

  fillPct(m: CapacityRow): number {
    if (m.unlimited || m.availability_t_wet == null || m.availability_t_wet <= 0) {
      return 0;
    }
    return Math.min(100, 100 * m.occupied_t_wet / m.availability_t_wet);
  }
}
