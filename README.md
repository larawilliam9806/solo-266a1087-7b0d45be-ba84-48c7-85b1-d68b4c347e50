# 浓度汇总 API

汇编河段水质报告用的浓度汇总服务。两份汇总可能收录同一组测量，不同样组也可能
共用空白样；本服务按样组登记原始读数汇总，按当前空白样数据生成校正结果，
并为每次汇总保存当时采用的空白样数据快照。旧汇总表还在而原样组记录丢失时，
可凭各表合计复原缺失样组，并在原件找回后核对。

技术栈：FastAPI + SQLite（Python 标准库 `sqlite3`，无额外 ORM），支持 Docker Compose 启动。

## 业务规则

- **只处理同一指标、同一单位的读数**：一次汇总涉及的所有样组必须指标
  （`analyte`）与单位（`unit`）一致，否则返回 409。
- **样组登记后固定**：按唯一编号登记，提交读数总和、独立测量方差之和、
  各空白样的使用次数；不可修改，重复登记返回 409。
- **每次测量只用一份空白样**：因此一个样组（或一次汇总）的空白样使用次数
  之和即测量总数。
- **空白样可另行登记、更正**：扣减值与方差通过 `POST /blanks` 登记、
  `PUT /blanks/{id}` 更正。样组登记时不要求空白样已存在。
- **汇总生成**：凭样组号、已有汇总号或两者组合生成新汇总；重叠样组只计一次。
  引用旧汇总时展开为其样组集合，按**当前**空白样数据重新计算。
- **查询旧汇总**：返回原结果与当时采用的空白样数据（快照），不随后续更正改变。
- **资料缺失**：样组号 / 汇总号 / 空白样任一缺失时，返回 404 并列出所缺编号，
  本次不生成汇总。
- **样组复原**：旧汇总表还在、原样组记录丢失时，凭各表合计联立方程复原缺失
  样组。一次提交同一指标、单位下最多 12 张表、合计最多 12 个样组；每张表填
  表号、所含样组号、读数总和、独立测量方差之和、各空白样的总使用次数（未列
  的空白样按零次计）。表内合计、参与核算的已登记值与复原值均限绝对值不超过
  一万亿的整数。固定已登记样组的值，用各表合计核对缺失样组；等式有唯一解且
  符合原登记规则才整批补录，否则说明原因、本次补录全部撤销。核算期间普通
  登记可照常完成，落账前按当时已有的登记作最终判定。
- **原件核验**：查询复原样组可见所用表号与核验状态
  （`pending` 待核 / `verified` 已核 / `conflict` 冲突）。原件找回后走原登记
  入口 `POST /groups` 报送同号样组：内容一致标为已核（200），不一致报冲突
  （409）并保留原复原记录。

## 计算公式

设汇总去重后收录若干样组，空白样 `b` 的总使用次数为 `n_b`（各样组使用次数之和），
测量总数 `N = Σ n_b`：

```
校正均值 = ( Σ 读数总和 − Σ_b 扣减值_b × n_b ) / N
总方差   = Σ_b 方差_b × n_b²  +  Σ 独立测量方差之和
标准误   = sqrt(总方差) / N
```

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，交互式文档见 `http://localhost:8000/docs`。
SQLite 数据保存在命名卷 `app-data`（容器内 `/data/app.db`）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # 默认数据库 ./data/app.db，可用 DB_PATH 覆盖
```

### 测试

```bash
pip install -r requirements-dev.txt
python3 tests/test_api.py              # 或 pytest tests/
python3 tests/test_restoration.py
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/groups` | 登记样组（登记后固定；重复编号 409；复原样组的原件核验同走此入口） |
| GET | `/groups/{id}` / `/groups` | 查询 / 列出样组（复原样组附所用表号与核验状态） |
| POST | `/blanks` | 登记空白样（重复编号 409） |
| PUT | `/blanks/{id}` | 更正空白样的扣减值与方差 |
| GET | `/blanks/{id}` / `/blanks` | 查询 / 列出空白样当前数据 |
| POST | `/summaries` | 生成新汇总（样组号、汇总号或两者组合） |
| GET | `/summaries/{id}` / `/summaries` | 查询旧汇总（原结果 + 当时空白样数据） |
| POST | `/restorations` | 样组复原（凭旧汇总表合计整批补录缺失样组） |
| GET | `/restorations/{id}` / `/restorations` | 查询 / 列出复原批次 |
| GET | `/health` | 健康检查 |

### 登记样组

```bash
curl -X POST localhost:8000/groups -H 'Content-Type: application/json' -d '{
  "group_id": "SG-1", "analyte": "cod", "unit": "mg/L",
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_usages": {"BLK-A": 10}
}'
```

### 登记 / 更正空白样

```bash
curl -X POST localhost:8000/blanks -H 'Content-Type: application/json' -d \
  '{"blank_id": "BLK-A", "deduction": 0.5, "variance": 0.01}'

curl -X PUT localhost:8000/blanks/BLK-A -H 'Content-Type: application/json' -d \
  '{"deduction": 0.8, "variance": 0.02}'
```

### 生成汇总

```bash
# 仅凭样组号
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"group_ids": ["SG-1", "SG-2"]}'

# 凭已有汇总号 + 新样组继续合并（按当前空白样数据重算，重叠样组只计一次）
curl -X POST localhost:8000/summaries -H 'Content-Type: application/json' -d \
  '{"summary_ids": [1], "group_ids": ["SG-3"]}'
```

响应示例：

```json
{
  "summary_id": 1,
  "analyte": "cod", "unit": "mg/L",
  "group_ids": ["SG-1"],
  "source_group_ids": ["SG-1"], "source_summary_ids": [],
  "total_measurements": 10,
  "reading_sum": 105.0, "variance_sum": 4.0,
  "blank_deduction_total": 5.0,
  "corrected_mean": 10.0,
  "standard_error": 0.223606797749979,
  "blanks": [
    {"blank_id": "BLK-A", "usage_count": 10, "deduction": 0.5, "variance": 0.01}
  ],
  "created_at": "2026-11-21T23:12:23+00:00"
}
```

其中 `blanks` 为生成该汇总时采用的空白样数据快照；之后更正空白样不影响
`GET /summaries/{id}` 的返回，但凭该汇总号继续合并时会按当前数据重算。

### 样组复原与原件核验

旧汇总表还在、原样组记录丢失时，把各表合计一次报送，复原缺失样组：

```bash
# SG-1 已登记（读数总和 100、方差之和 4、BLK-A 用 10 次），SG-2 记录丢失。
# 旧表 7 收录 {SG-1, SG-2}：读数合计 160、方差合计 10、BLK-A 共用 15 次。
curl -X POST localhost:8000/restorations -H 'Content-Type: application/json' -d '{
  "analyte": "cod", "unit": "mg/L",
  "tables": [
    {"table_id": 7, "group_ids": ["SG-1", "SG-2"],
     "reading_total": 160, "variance_total": 10,
     "blank_usages": {"BLK-A": 15}}
  ]
}'
```

固定已登记样组的值，按各表合计对联立方程求解：上例解出 SG-2 = 读数总和 60、
方差之和 6、BLK-A 用 5 次，整批补录。等式有唯一解且复原值符合原登记规则
（整数、绝对值不超过一万亿、方差非负、空白样使用次数为正整数且至少一项）
才落账；任一不满足则说明原因、本次补录全部撤销。核算期间普通登记可照常
完成，落账前按当时已有的登记作最终判定。

查询复原样组可见所用表号与原件核验状态：

```json
{"group_id": "SG-2", "reading_sum": 60.0, "variance_sum": 6.0,
 "blank_usages": {"BLK-A": 5},
 "restoration": {"restoration_id": 1, "table_ids": ["7"],
                 "verification_status": "pending",
                 "verified_at": null, "conflict": null,
                 "restored_at": "2026-11-22T03:15:03+00:00"}}
```

原件找回后走原登记入口报送同号样组：内容一致标为已核（200），不一致报冲突
（409，列出差异字段）并保留原复原记录。

```bash
curl -X POST localhost:8000/groups -H 'Content-Type: application/json' -d '{
  "group_id": "SG-2", "analyte": "cod", "unit": "mg/L",
  "reading_sum": 60, "variance_sum": 6, "blank_usages": {"BLK-A": 5}
}'
```

### 错误返回

- **404 资料缺失**（不生成汇总）：

```json
{"detail": {"error": "missing_data", "message": "资料缺失，本次不生成汇总。",
            "missing": {"group_ids": ["SG-9"], "blank_ids": ["BLK-X"]}}}
```

- **409 指标 / 单位不一致**：

```json
{"detail": {"error": "inconsistent_analyte_or_unit",
            "message": "只能汇总同一指标、同一单位的读数。",
            "combinations": [{"analyte": "lead", "unit": "ug/L", "group_ids": ["G1"]},
                             {"analyte": "lead", "unit": "mg/L", "group_ids": ["G3"]}]}}
```

- **409 重复登记**（样组 / 空白样编号已存在）；**422 入参校验失败**
  （如方差为负、空白样使用次数为空、汇总请求两项皆空、复原表数或样组数
  超限、表内合计不是一万亿以内的整数）。

- **409 复原失败**（本次补录全部撤销，不留下任何记录）：

| `error` | 原因 |
| --- | --- |
| `nothing_to_restore` | 表内样组均已登记，没有需要复原的样组 |
| `inconsistent_analyte_or_unit` | 表内已登记样组的指标/单位与本次复原不一致 |
| `registered_values_invalid` | 参与核算的已登记值不是绝对值不超过一万亿的整数 |
| `solution_not_unique` | 表格合计不足以唯一确定缺失样组（方程组欠定） |
| `equations_inconsistent` | 表格合计与已登记值互相矛盾（方程组无解） |
| `solution_violates_rules` | 复原值不符合原登记规则（附 `problems` 明细） |
| `restored_conflict` | 原件报送内容与复原记录不一致（附 `differences`，保留原记录） |

## 项目结构

```
app/
  main.py        FastAPI 应用与全部接口
  db.py          SQLite 连接与建表
tests/
  test_api.py    端到端测试（汇总业务规则）
  test_restoration.py  端到端测试（样组复原与原件核验）
Dockerfile
docker-compose.yml
requirements.txt
```

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |
