import { Component } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MaterialsComponent } from './components/materials.component';
import { BlendComponent } from './components/blend.component';
import { OccupationsComponent } from './components/occupations.component';
import { HistoryComponent } from './components/history.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule, MaterialsComponent, BlendComponent,
            OccupationsComponent, HistoryComponent],
  templateUrl: './app.component.html',
  styleUrl: './app.component.css',
})
export class AppComponent {
  tab: 'materials' | 'blend' | 'occupations' | 'history' = 'blend';
}
