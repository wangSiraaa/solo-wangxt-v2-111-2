import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';

export interface Seg { label: string; value: number; color: string; }

/** 氧化物来源/配料比例堆叠条（纯 CSS，无第三方图表） */
@Component({
  selector: 'app-stack-bar',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="legend" *ngIf="showLegend">
      <span class="chip" *ngFor="let s of segs">
        <span class="dot" [style.background]="s.color"></span>{{ s.label }}
      </span>
    </div>
    <div class="src-bar" [title]="title">
      <div class="src-seg" *ngFor="let s of segs"
           [style.width.%]="pct(s)"
           [style.background]="s.color"
           [title]="s.label + ': ' + s.value.toFixed(2)">
        <span *ngIf="pct(s) >= 6">{{ short(s.label) }} {{ pct(s).toFixed(1) }}%</span>
      </div>
    </div>
  `,
})
export class StackBarComponent {
  @Input() segs: Seg[] = [];
  @Input() title = '';
  @Input() showLegend = true;

  total(): number { return this.segs.reduce((a, s) => a + Math.max(0, s.value), 0) || 1; }
  pct(s: Seg): number { return 100 * Math.max(0, s.value) / this.total(); }
  short(l: string): string { return l.length > 8 ? l.slice(0, 7) + '…' : l; }
}

/** 物料配色（按 code 稳定取色） */
const PALETTE = [
  '#155e63', '#c77d4f', '#5b8ff9', '#61c3a6', '#8b6bb5',
  '#d4943a', '#4d9b94', '#b05d8c', '#7a8a99', '#9c7a3c',
];
export function colorOf(code: string, used: string[]): string {
  let i = used.indexOf(code);
  if (i < 0) { used.push(code); i = used.length - 1; }
  return PALETTE[i % PALETTE.length];
}
