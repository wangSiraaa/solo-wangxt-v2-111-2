# 离线原料配比试算台（虚构工艺边界 · 工艺研发用）

> ⚠️ **边界声明**：本应用使用的原料名称、化验单数值、成本、可用量与率值窗口均为**虚构演示数据**，
> 仅用于工艺研发离线比较“原料成本 ↔ 生料化学指标”的取舍。
> 应用不连接任何生产控制系统，**不向真实生产设备下发指令**。

## 技术栈

| 层 | 技术 | 职责 |
|---|---|---|
| 前端 | Angular 18（standalone 组件，纯 CSS 堆叠条） | 氧化物来源/配比比例展示、试算交互、方案对比、化验追溯、**湿基容量占用** |
| 后端 | FastAPI + Pydantic | REST API、干湿基换算、错误码、**跨批次容量账本/乐观并发**、静态托管 |
| 优化 | SciPy `linprog`（HiGHS） | 线性规划：成本最优 / 廉价料最大 / 率值居中 |
| 存储 | PostgreSQL 15 | 原料、**多版化验单**、试算批次、方案、逐原料换算留痕、**占用/占用事件/容量账本** |

## 计算口径

1. **先质量守恒合成，再算率值**（不把率值当输入去反推成分）。
   干基份额 x_i（Σx_i=1）：`合成干基% = Σ x_i × 原料干基%`。
2. 率值（分母为零即报错，见下）：
   - 硅率 `SM = SiO2 / (Al2O3 + Fe2O3)`
   - 铝率 `IM = Al2O3 / Fe2O3`
   - 石灰饱和系数 `KH = (CaO − 1.65·Al2O3 − 0.35·Fe2O3) / (2.8·SiO2)`
3. **干湿基**：化验按 `dry`（干基）或 `wet`（收到基）登记；
   含水率 w 时 `干基% = 湿基% /(1−w)`，自由水不并入 LOI；
   质量换算 `湿料t = 干料t /(1−w)`，成本按湿料吨价结算。
4. 碱当量 `Na2O + 0.658·K2O`。

### 硬性错误规则（不以零含量兜底）

- **缺测**：候选原料的必测组分（CaO/SiO2/Al2O3/Fe2O3，及被设上限的有害组分）
  不在 `measured_oxides` 中 → HTTP 422 `MISSING_ASSAY`，返回原料/化验版/单号/缺测项；
- **分母为零**：Fe2O₃、SiO₂ 等为 0 导致 IM/SM/KH 无定义 → HTTP 422 `ZERO_DENOMINATOR`；
- “已实测为 0”（演示料 QZ00）与“未测/缺测”（演示料 SP01）严格区分。

### 约束

- 原料**最低掺量**（干基 %，档案字段）、**湿基可用量**（换算成干基份额上限）；
- **有害组分干基上限**（Cl、碱当量等，可扩展）；
- 率值区间经线性化进入 LP（如 SM≤hi ⇔ `Σ(SiO2−hi(Al2O3+Fe2O3))x ≤ 0`）；
- **跨批次剩余容量**：确认虚拟占用时，以“湿基可用量 − 其它研发批次的有效占用
  （替换时先逻辑剔除被替换占用）”覆盖单次可用量上限，方案在 LP 阶段就不能超订。

### 虚拟批次占用（跨研发批次湿基容量账本）

多个虚构研发批次会争用同一批湿基原料，单次试算的可用量约束无法阻止跨批次超订，
因此在试算之上增加**虚拟批次占用**层：

- **生命周期**：可行方案可从草稿（预览，不落库）进入 **occupied / released / expired**；
  释放与过期只追加事件并改状态，**占用留痕与来源方案快照永久保留**；
- **逐原料快照**：每条占用的每个原料都保存干湿基换算、化验单来源、
  确认瞬间的湿基可用量 / 已有效占用 / 本笔申请 / 确认后剩余、账本版本；
- **容量不变量**：同一原料 `有效占用合计 + 新申请 ≤ 湿基可用量`，
  失败/缺测（`MISSING_ASSAY`）/无解（`NOT_FEASIBLE`）方案绝不留下部分占用；
- **原子替换**：替换旧占用时，在**同一事务**内验证“新方案 + 全部既存占用”成立后，
  才写入新占用并把旧占用标记为 `replaced`，绝不先释放旧量制造并发超订窗口；
- **乐观并发**：逐原料 `material_capacity_ledger` 账本带 `version`，
  确认在提交时以 `SELECT … FOR UPDATE` + 版本校验裁决——并发的两个申请只有一方成功，
  另一方得到可恢复的 `VERSION_CONFLICT`（附最新版本与容量缺口，刷新后可重试）；
- **幂等确认**：`idempotency_key` 唯一约束保证同一确认请求重试只回放、不双扣量；
- **过期**：每条占用带 `expires_at`，服务启动时与后台守护线程（60s）扫描释放；
  服务刷新/重启后过期占用被正确释放，原方案追溯（含率值/湿料量/换算）保持不变；
- **事件持久化**：`occupation_event` 只追加，记录 confirmed/released/expired/replaced
  /rejected 及冲突原因（容量缺口、版本冲突、缺测、无解）。

### 求解模式与无解诊断

- `min_cost`：最小元/吨干生料；
- `max_cheap`：两阶段 LP——先最大化指定廉价料份额，再锁定份额最小化成本打破平局；
- `balanced`：率值对区间中点的绝对偏差最小（线性化），轻微成本偏好做次序裁决；
- **求解失败**：对全部不等式做“最小违约松弛”模型，列出仍被突破的冲突约束、
  限值、最小违约解达到值与缺口；最低掺量之和 >100% 另有算术预检 `MIN_SHARE_OVERFLOW`。

## 目录

```
backend/
  app/
    main.py        FastAPI 路由 + 错误处理 + 虚拟批次占用 + SPA 托管
    chemistry.py   干湿基换算 / 质量守恒 / SM/IM/KH / 缺测与零分母异常
    optimizer.py   SciPy HiGHS LP、多模式、冲突诊断（含跨批次剩余容量上限）
    occupations.py 占用预览/确认/替换/释放、容量账本、行锁+版本号乐观并发、过期扫描
    models.py      SQLAlchemy：material / assay_version / blend_run / solution / item
                   + material_capacity_ledger / occupation / occupation_item / occupation_event
    crud.py        持久化与历史回看
    schemas.py     Pydantic 模型
    seed.py        虚构演示数据（含湿基化验单、缺测/零分母演示料、占用验收料）
  tests/           32 个 pytest（换算/守恒/报错/求解/API/追溯 + 虚拟占用验收）
  scripts/         pg_start / pg_stop / seed / serve
frontend/
  src/app/
    components/    materials / blend / occupations / solution-card / history / stack-bar
    services/api.service.ts
    models/models.ts
```

## 启动（本机用户态，无需 root/docker）

PostgreSQL 15 以 deb 解包方式安装在 `~/.local/pgsql`，数据目录 `~/.local/pgdata`，
端口 **55432**，库名 **rawmix**，连接串：
`postgresql+psycopg2://mixapp@127.0.0.1:55432/rawmix`（可用 `RAWMIX_DATABASE_URL` 覆盖）。

```bash
# 1) 启动数据库（首次自动 initdb + 建库）
backend/scripts/pg_start.sh
# 2) 写入虚构演示数据
backend/scripts/seed.sh
# 3) 启动 API + 已构建前端（http://127.0.0.1:8000）
backend/scripts/serve.sh
```

前端开发模式（热更新，代理 /api → :8000）：

```bash
cd frontend && ./dev.sh        # http://127.0.0.1:4200
# 生产构建（输出到 backend/static，由 FastAPI 托管）：
cd frontend && ./node_modules/.bin/ng build frontend
```

Python 依赖：`pip install -r backend/requirements.txt`（本机装于用户 site-packages）。

## 前端四个标签页

1. **原料与化验**：全部原料/多版化验单、干湿基标记、缺测红格、干基换算预览；
2. **配比试算与方案对比**：候选/化验版/率值窗口/有害上限/模式选择，四个快速场景：
   - 基准三方案对比（含水率差异：粉煤灰 18% 湿基化验单 → 采购湿料量与留痕）；
   - 廉价原料（页岩）致 IM/KH 超限 → 失败 + 冲突项；
   - 碱当量上限收紧（0.40%）→ 有害组分冲突与突破量；
   - 5000 t 大批量 → 湿基可用量与 KH 同时冲突；
   - **手工配比**错误演示：一键装入“100% 零铁石英（IM 分母为零）”和“缺测矿样（MISSING_ASSAY）”；
3. **虚拟批次占用**：每种原料的湿基可用 / 已占用 / 剩余 / 账本版本、关联占用展开；
   草稿→预览（逐原料确认前已占、确认后剩余快照与缺口）→确认（幂等键、TTL）；
   对已占用方案可**原子替换**（先验证新方案与全部占用成立）或释放；
   占用列表（occupied/released/expired、来源方案、替换链、逐原料湿料量）
   与全局事件流水（确认/释放/过期/替换/拒绝及冲突原因）；版本冲突时一键带新版本重试；
4. **历史追溯**：每个方案可追到批次号、原始化验版本/单号、原始 wet/dry 报送值、
   逐组分湿→干公式、干/湿料质量、水量与成本算式。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/materials?active_only=` | 原料与全部化验版本 |
| POST | `/api/blend` | 试算（多模式、约束、可入库） |
| POST | `/api/evaluate` | 手工份额合成 + 率值（错误演示） |
| GET | `/api/runs` `/api/runs/{id}` | 历史批次与完整追溯 |
| POST | `/api/occupations/preview` | 占用预览：按当前有效占用求解并核对容量，不落库 |
| POST | `/api/occupations/confirm` | 确认占用（幂等键、原子替换、乐观版本检查） |
| POST | `/api/occupations/{id}/release` | 人工释放（幂等） |
| GET | `/api/capacity` | 逐原料可用/有效占用/剩余/账本版本（先扫过期） |
| GET | `/api/occupations` `/api/occupations/{id}` | 占用列表（可按状态过滤）与明细追溯 |
| GET | `/api/occupation-events` | 占用事件流水（含被拒申请与冲突原因） |
| GET | `/api/health` | 健康检查（含 fictional-boundary 标记） |

错误响应体：`{ "error_code": "MISSING_ASSAY|ZERO_DENOMINATOR|...", "message": ..., "details": ... }`。

## 测试

```bash
cd backend && python3 -m pytest tests/ -q
# 32 passed（21 项原有核心/API 测试 + 11 项虚拟批次占用验收：
# 超订拒绝与缺口、原子替换、幂等、并发版本冲突、重启过期、缺测/无解不占量）
```
