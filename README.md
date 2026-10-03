# 离线原料配比试算台（虚构工艺边界 · 工艺研发用）

> ⚠️ **边界声明**：本应用使用的原料名称、化验单数值、成本、可用量与率值窗口均为**虚构演示数据**，
> 仅用于工艺研发离线比较“原料成本 ↔ 生料化学指标”的取舍。
> 应用不连接任何生产控制系统，**不向真实生产设备下发指令**。

## 技术栈

| 层 | 技术 | 职责 |
|---|---|---|
| 前端 | Angular 18（standalone 组件，纯 CSS 堆叠条） | 氧化物来源/配比比例展示、试算交互、方案对比、化验追溯 |
| 后端 | FastAPI + Pydantic | REST API、干湿基换算、错误码、静态托管 |
| 优化 | SciPy `linprog`（HiGHS） | 线性规划：成本最优 / 廉价料最大 / 率值居中 |
| 存储 | PostgreSQL 15 | 原料、**多版化验单**、试算批次、方案、逐原料换算留痕、**虚拟批次占用与事件流** |

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
- 率值区间经线性化进入 LP（如 SM≤hi ⇔ `Σ(SiO2−hi(Al2O3+Fe2O3))x ≤ 0`）。

### 求解模式与无解诊断

- `min_cost`：最小元/吨干生料；
- `max_cheap`：两阶段 LP——先最大化指定廉价料份额，再锁定份额最小化成本打破平局；
- `balanced`：率值对区间中点的绝对偏差最小（线性化），轻微成本偏好做次序裁决；
- **求解失败**：对全部不等式做“最小违约松弛”模型，列出仍被突破的冲突约束、
  限值、最小违约解达到值与缺口；最低掺量之和 >100% 另有算术预检 `MIN_SHARE_OVERFLOW`。

### 虚拟批次占用（跨批次湿基防超订）

多个虚构研发批次争用同一批湿基原料时，单次试算的可用量约束无法阻止跨批次超订，
故在试算之上增加“虚拟批次占用”层：

- 方案生命周期：**草稿（预览，不占量）→ 已占用 → 已释放 / 已过期**；
  已占用方案可被新方案**原子替换**（旧占用留痕为已释放）。
- 不变式：对每种原料，所有「已占用」方案的湿料量之和 ≤ `availability_t_wet`
  （NULL 视为不限）。每条占用**逐原料**持久化干湿基换算、来源试算方案
  （run/solution）与确认时刻「可用/占用前/占用后剩余」快照。
- 确认/替换在一个事务内先 `SELECT … FOR UPDATE`（按原料 id 排序，防死锁）
  汇总全部有效占用再校验，使并发申请串行化；**替换必须先验证新方案与全部
  占用成立，再在同一提交内以新占旧——绝不先释放旧量暴露超订窗口**。
- 容量不足 → 409 `CAPACITY_CONFLICT`（逐原料点明可用/已占/剩余/申请/**缺口**，
  `recoverable=true`）；版本过期 → 409 `VERSION_CONFLICT`（带当前版本号，可恢复）；
  缺测 → 422 `MISSING_ASSAY`；新方案无解 → 422 `SOLUTION_INFEASIBLE`。
  失败、缺测、无解路径在任何占用写入之前拒绝，**绝不留下部分占用**，原因全部入事件表。
- 并发与幂等：占用带单调递增 `version`（乐观锁，确认/释放/替换须带期望值）；
  同一 `idempotency_key` 的确认/释放重试原样返回，**不双扣量**。
- 过期：占用带 `expires_at`（TTL）；启动钩子 + 容量查询/确认前都会清扫，
  过期占用转 `expired` 回收容量，**来源方案与事件追溯保持不变**。

## 目录

```
backend/
  app/
    main.py        FastAPI 路由 + 错误处理 + SPA 托管
    chemistry.py   干湿基换算 / 质量守恒 / SM/IM/KH / 缺测与零分母异常
    optimizer.py   SciPy HiGHS LP、多模式、冲突诊断
    models.py      SQLAlchemy：material / assay_version / blend_run / solution / item
                   / virtual_batch_occupation / occupation_item / occupation_event
    occupations.py 虚拟批次占用：预览/确认/释放/原子替换、行锁容量校验、
                   版本号与幂等键、TTL 过期清扫、事件持久化
    crud.py        持久化与历史回看
    schemas.py     Pydantic 模型
    seed.py        虚构演示数据（含湿基化验单、缺测/零分母演示料）
  tests/           37 个 pytest（换算/守恒/报错/求解/API/追溯 + 占用验收 16 个）
  scripts/         pg_start / pg_stop / seed / serve
frontend/
  src/app/
    components/    materials / blend / solution-card / history / stack-bar
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
3. **虚拟批次占用**：
   - 逐原料湿基「可用 / 已占用 / 剩余」进度条与有效占用来源（可点开追到占用单）；
   - 占用预览（草稿不占量，显示确认后剩余与“会超订”标记）、确认、释放、原子替换；
   - 验收场景一键装入：方案甲占 800 t SS01 / 方案乙再申 300 t / 缩小为 200 t；
   - 占用详情逐原料展示干湿基换算、可用量/占用前/占用后剩余快照与来源试算；
   - 全部占用事件（确认/容量冲突/版本冲突/释放/过期/替换/缺测/无解）留痕历史；
4. **历史追溯**：每个方案可追到批次号、原始化验版本/单号、原始 wet/dry 报送值、
   逐组分湿→干公式、干/湿料质量、水量与成本算式。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/materials?active_only=` | 原料与全部化验版本 |
| POST | `/api/blend` | 试算（多模式、约束、可入库） |
| POST | `/api/evaluate` | 手工份额合成 + 率值（错误演示） |
| GET | `/api/runs` `/api/runs/{id}` | 历史批次与完整追溯 |
| POST | `/api/occupations/preview` | 试算并建草稿（不占量）；无解返回 feasible=false；缺测 422 |
| POST | `/api/occupations/{id}/confirm` | 确认占用（体带 `expected_version`、可选 `idempotency_key`） |
| POST | `/api/occupations/{id}/release` | 释放/撤销（版本号 + 幂等） |
| POST | `/api/occupations/{id}/replace` | 原子替换：先验证再以新占旧（带旧版本号） |
| GET | `/api/occupations/capacity` | 逐原料可用/占用/剩余 + 构成明细（顺带过期清扫） |
| GET | `/api/occupations` `/api/occupations/{id}` | 占用列表（可按 status 过滤）/ 详情含事件 |
| GET | `/api/occupations/events` `/api/occupations/{id}/events` | 占用事件历史 |
| GET | `/api/health` | 健康检查（含 fictional-boundary 标记） |

错误响应体：`{ "error_code": "MISSING_ASSAY|ZERO_DENOMINATOR|...", "message": ..., "details": ... }`。

## 测试

```bash
cd backend && python3 -m pytest tests/ -q
# 37 passed（含占用验收：超订拒绝/原子替换/幂等/并发版本冲突/重启过期/缺测无解不占量）
```
