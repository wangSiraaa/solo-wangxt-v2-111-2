import { Component, OnInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api.service';
import {
  CapacityGap, CapacityResponse, Material, MaterialCapacity, Occupation,
  OccupationEvent, OccupationPreview, Targets,
} from '../models/models';

interface CandPick {
  material_id: number;
  assay_version_id: number | null;
  selected: boolean;
}

@Component({
  selector: 'app-occupations',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './occupations.component.html',
})
export class OccupationsComponent implements OnInit {
  materials: Material[] = [];
  capacity: CapacityResponse | null = null;
  occupations: Occupation[] = [];
  events: OccupationEvent[] = [];
  expandedMat = new Set<number>();

  // 申请表单
  picks: Record<number, CandPick> = {};
  batch = 1000;
  scenario = '虚拟研发批次（虚构边界）';
  mode = 'min_cost';
  ttlMinutes = 24 * 60;
  targets: Targets = {
    SM: { min: 2.4, max: 2.8 }, IM: { min: 1.4, max: 1.8 },
    KH: { min: 0.88, max: 0.94 },
  };

  busy = false;
  preview: OccupationPreview | null = null;
  replaceId: number | null = null;
  expectedVersions: Record<string, number> = {};
  idemKey = '';
  apiError: any = null;
  notice = '';

  constructor(private api: ApiService) {}

  ngOnInit(): void {
    this.api.materials(false).subscribe(ms => {
      this.materials = ms;
      for (const m of ms) {
        this.picks[m.id] = {
          material_id: m.id,
          assay_version_id: m.assay_versions[0]?.id ?? null,
          selected: [1, 2, 3, 4, 5].includes(m.id),
        };
      }
      this.refresh();
    });
  }

  refresh(): void {
    this.api.capacity().subscribe(cap => {
      this.capacity = cap;
      if (cap.expired_released?.length) {
        this.notice = `启动/刷新扫描：已自动释放过期占用 ${cap.expired_released.join(', ')}`;
      }
    });
    this.api.occupations().subscribe(os => { this.occupations = os; });
    this.api.occupationEvents().subscribe(es => { this.events = es; });
  }

  capOf(materialId: number): MaterialCapacity | undefined {
    return this.capacity?.materials.find(m => m.material_id === materialId);
  }

  toggleMat(id: number): void {
    if (this.expandedMat.has(id)) { this.expandedMat.delete(id); }
    else { this.expandedMat.add(id); }
  }

  selectedCandidates() {
    return Object.values(this.picks).filter(p => p.selected);
  }

  private buildSpec() {
    return {
      scenario_name: this.scenario,
      batch_t_dry: this.batch,
      candidates: this.selectedCandidates().map(p => ({
        material_id: p.material_id,
        assay_version_id: p.assay_version_id,
      })),
      targets: this.targets,
      hazard_limits_pct: {},
      mode: this.mode,
      cheap_material_id: null,
    };
  }

  static keyCounter = 0;
  private newKey(): string {
    OccupationsComponent.keyCounter += 1;
    const rnd = Math.random().toString(36).slice(2, 10);
    return `ui-${Date.now().toString(36)}-${OccupationsComponent.keyCounter}-${rnd}`;
  }

  doPreview(replaceId: number | null = null): void {
    this.apiError = null; this.preview = null; this.replaceId = replaceId;
    if (!this.selectedCandidates().length) {
      this.apiError = { message: '请至少勾选一种候选原料。' };
      return;
    }
    this.busy = true;
    this.api.previewOccupation(
      this.buildSpec(), replaceId, this.expectedVersions
    ).subscribe({
      next: pv => { this.preview = pv; this.busy = false; },
      error: e => { this.apiError = e.error ?? { message: e.message }; this.busy = false; },
    });
  }

  doConfirm(): void {
    this.apiError = null;
    const key = this.idemKey?.trim() || this.newKey();
    this.busy = true;
    this.api.confirmOccupation({
      spec: this.buildSpec(),
      replace_occupation_id: this.replaceId,
      expected_versions: this.preview?.current_versions ?? this.expectedVersions,
      idempotency_key: key,
      ttl_minutes: this.ttlMinutes,
    }).subscribe({
      next: r => {
        this.busy = false;
        this.notice = (r.replay ? '幂等重试：命中既有占用，未重复扣量。' :
          (this.replaceId ? `已原子替换占用 #${this.replaceId}。` : '占用确认成功。')) +
          ` 占用编号 ${r.occupation.occupation_code}`;
        this.preview = null; this.replaceId = null; this.idemKey = '';
        this.expectedVersions = {};
        this.refresh();
      },
      error: e => {
        this.busy = false;
        this.apiError = e.error ?? { message: e.message };
        // 版本冲突：把服务端返回的最新版本回填，供一键恢复重试
        const cur = e.error?.details?.current_versions;
        if (cur) { this.expectedVersions = cur; }
      },
    });
  }

  startReplace(o: Occupation): void {
    this.replaceId = o.id;
    this.expectedVersions = {};
    this.batch = o.batch_t_dry;
    this.mode = o.mode;
    this.scenario = `替换 ${o.occupation_code}：` + o.scenario_name;
    for (const k of Object.keys(this.picks)) { this.picks[+k].selected = false; }
    for (const it of o.items) {
      if (this.picks[it.material_id]) { this.picks[it.material_id].selected = true; }
    }
    this.notice = `准备原子替换旧占用 #${o.id}：新方案与全部占用验证通过后才会替代旧占用（不会先释放旧量）。`;
    this.preview = null;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  cancelReplace(): void {
    this.replaceId = null; this.preview = null; this.notice = '';
  }

  doRelease(o: Occupation): void {
    this.apiError = null;
    this.api.releaseOccupation(o.id).subscribe({
      next: r => {
        this.notice = (r.replay ? '该占用已是释放状态（回放）。' : `占用 ${o.occupation_code} 已释放。`);
        this.refresh();
      },
      error: e => { this.apiError = e.error ?? { message: e.message }; },
    });
  }

  retryAfterConflict(): void {
    // 用最新账本版本重新预览/确认
    this.doPreview(this.replaceId);
  }

  gapsText(gaps: CapacityGap[] | undefined): string {
    return (gaps ?? []).map(g =>
      `${g.material_code} 剩余 ${g.remaining_t_wet ?? '—'}t / 申请 ${g.requested_t_wet}t / 缺口 ${g.gap_t_wet}t`
    ).join('；');
  }

  fmt(n: number | null | undefined, digit = 1): string {
    return n == null ? '不限' : Number(n).toFixed(digit);
  }

  statusLabel(s: string): string {
    return { occupied: '已占用', released: '已释放', expired: '已过期' }[s] ?? s;
  }

  eventLabel(t: string): string {
    return {
      confirmed: '确认', released: '释放', expired: '过期',
      replaced: '替换', rejected: '拒绝',
    }[t] ?? t;
  }
}
