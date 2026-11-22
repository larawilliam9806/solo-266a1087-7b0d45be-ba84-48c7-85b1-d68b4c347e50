"""河段水质报告 · 浓度汇总 API。

业务规则
--------
- 只处理同一指标（analyte）、同一单位（unit）的读数。
- 样组按唯一编号登记后固定：提交读数总和、独立测量方差之和、
  各空白样的使用次数。每次测量只用一份空白样，
  因此空白样使用次数之和 = 测量总数。
- 空白样的扣减值与方差可另行登记、更正。
- 新汇总可凭样组号、已有汇总号或两者组合生成；重叠样组只计一次。
  引用旧汇总时展开为其样组集合，按**当前**空白样数据重算。
- 校正均值 = (读数总和 − Σ 空白扣减值 × 该空白总使用次数) / 测量总数
- 标准误   = sqrt(Σ 空白方差 × 该空白总使用次数² + 独立测量方差之和) / 测量总数
- 查询旧汇总时返回原结果与当时采用的空白样数据（快照）。
- 资料缺失（样组号 / 汇总号 / 空白样）时列出所缺编号，本次不生成汇总。
- 样组复原：旧汇总表还在、原样组记录丢失时，凭各表合计（读数总和、
  独立测量方差之和、各空白样总使用次数，未列空白样按零次计）联立方程，
  固定已登记样组的值，核对并复原缺失样组。一次提交同一指标、单位下
  最多 12 张表、合计最多 12 个样组；表内合计、参与核算的已登记值与
  复原值均限绝对值不超过一万亿的整数。等式有唯一解且符合原登记规则
  才整批补录，否则说明原因、本次补录全部撤销。核算期间普通登记照常
  完成，落账前按当时已有的登记作最终判定。
- 原件核验：查询复原样组可见所用表号与核验状态；原件找回后走原登记
  入口报送同号样组，内容一致标为已核，不一致报冲突并保留原记录。
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fractions import Fraction
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator

from .db import connect, init_db


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# 复原批次限制：最多 12 张表、合计最多 12 个样组
MAX_RESTORE_TABLES = 12
MAX_RESTORE_GROUPS = 12
# 一万亿：表内合计、参与核算的已登记值与复原值的绝对值上限
VALUE_LIMIT = 1_000_000_000_000


# ---------------------------------------------------------------- 请求模型

class GroupCreate(BaseModel):
    """样组登记（登记后固定，不可修改）。"""

    group_id: str = Field(min_length=1, description="样组唯一编号")
    analyte: str = Field(min_length=1, description="指标，如 lead")
    unit: str = Field(min_length=1, description="单位，如 ug/L")
    reading_sum: float = Field(description="读数总和")
    variance_sum: float = Field(ge=0, description="独立测量方差之和")
    blank_usages: dict[str, PositiveInt] = Field(
        min_length=1, description="各空白样的使用次数：{空白样编号: 次数}"
    )


class BlankCreate(BaseModel):
    """空白样登记。"""

    blank_id: str = Field(min_length=1, description="空白样唯一编号")
    deduction: float = Field(description="扣减值")
    variance: float = Field(ge=0, description="方差")


class BlankUpdate(BaseModel):
    """空白样更正（扣减值与方差整体替换）。"""

    deduction: float = Field(description="扣减值")
    variance: float = Field(ge=0, description="方差")


class SummaryRequest(BaseModel):
    """生成汇总：凭样组号、已有汇总号或两者组合。"""

    group_ids: list[str] = Field(default_factory=list, description="样组编号列表")
    summary_ids: list[int] = Field(default_factory=list, description="已有汇总号列表")

    @model_validator(mode="after")
    def _need_at_least_one_input(self) -> "SummaryRequest":
        if not self.group_ids and not self.summary_ids:
            raise ValueError("group_ids 与 summary_ids 至少提供一项")
        return self


TableTotalInt = Annotated[int, Field(ge=-VALUE_LIMIT, le=VALUE_LIMIT)]
TableCountInt = Annotated[int, Field(ge=0, le=VALUE_LIMIT)]


class RestorationTable(BaseModel):
    """一张旧汇总表的合计数据（未列的空白样按零次计）。"""

    table_id: str = Field(min_length=1, description="表号（旧汇总表编号）")
    group_ids: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1, description="表内样组号"
    )
    reading_total: TableTotalInt = Field(
        description="表内读数总和（整数，绝对值不超过一万亿）"
    )
    variance_total: TableCountInt = Field(
        description="表内独立测量方差之和（非负整数，不超过一万亿）"
    )
    blank_usages: dict[str, TableCountInt] = Field(
        default_factory=dict,
        description="各空白样的总使用次数；未列的空白样按零次计",
    )

    @field_validator("table_id", mode="before")
    @classmethod
    def _coerce_table_id(cls, v: Any) -> Any:
        # 旧汇总表号常为整数，统一按字符串保存
        if isinstance(v, int) and not isinstance(v, bool):
            return str(v)
        return v


class RestorationRequest(BaseModel):
    """样组复原：同一指标、单位下最多 12 张表、合计最多 12 个样组。"""

    analyte: str = Field(min_length=1, description="指标，如 lead")
    unit: str = Field(min_length=1, description="单位，如 ug/L")
    tables: list[RestorationTable] = Field(
        min_length=1, max_length=MAX_RESTORE_TABLES,
        description="旧汇总表合计，一次最多 12 张",
    )

    @model_validator(mode="after")
    def _check_batch_limits(self) -> "RestorationRequest":
        table_ids = [t.table_id for t in self.tables]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("表号在一次复原中不能重复")
        groups = {g for t in self.tables for g in t.group_ids}
        if len(groups) > MAX_RESTORE_GROUPS:
            raise ValueError(
                f"一次复原合计涉及样组数不能超过 {MAX_RESTORE_GROUPS} 个"
                f"（当前 {len(groups)} 个）"
            )
        return self


# ---------------------------------------------------------------- 应用初始化

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="浓度汇总 API",
    version="1.1.0",
    description="河段水质报告浓度汇总：样组登记、空白样登记/更正、汇总生成与查询、"
                "样组复原与原件核验。",
    lifespan=lifespan,
)


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]


# ---------------------------------------------------------------- 读取辅助

def group_payload(db: sqlite3.Connection, group_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM sample_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if row is None:
        return None
    usages = db.execute(
        "SELECT blank_id, usage_count FROM group_blank_usages "
        "WHERE group_id = ? ORDER BY blank_id",
        (group_id,),
    ).fetchall()
    restoration_row = db.execute(
        "SELECT * FROM restored_groups WHERE group_id = ?", (group_id,)
    ).fetchone()
    if restoration_row is None:
        restoration = None
    else:
        # 复原样组：所用表号与原件核验状态
        restoration = {
            "restoration_id": restoration_row["restoration_id"],
            "table_ids": [
                r["table_id"]
                for r in db.execute(
                    "SELECT table_id FROM restored_group_tables "
                    "WHERE group_id = ? ORDER BY table_id",
                    (group_id,),
                )
            ],
            "verification_status": restoration_row["verification_status"],
            "verified_at": restoration_row["verified_at"],
            "conflict": (
                json.loads(restoration_row["conflict"])
                if restoration_row["conflict"]
                else None
            ),
            "restored_at": restoration_row["created_at"],
        }
    return {
        "group_id": row["group_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "reading_sum": row["reading_sum"],
        "variance_sum": row["variance_sum"],
        "blank_usages": {r["blank_id"]: r["usage_count"] for r in usages},
        "measurement_count": sum(r["usage_count"] for r in usages),
        "restoration": restoration,
        "created_at": row["created_at"],
    }


def blank_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "blank_id": row["blank_id"],
        "deduction": row["deduction"],
        "variance": row["variance"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def summary_payload(db: sqlite3.Connection, summary_id: int) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM summaries WHERE summary_id = ?", (summary_id,)
    ).fetchone()
    if row is None:
        return None
    group_ids = [
        r["group_id"]
        for r in db.execute(
            "SELECT group_id FROM summary_groups WHERE summary_id = ? ORDER BY group_id",
            (summary_id,),
        )
    ]
    blanks = [
        {
            "blank_id": r["blank_id"],
            "usage_count": r["usage_count"],
            "deduction": r["deduction"],
            "variance": r["variance"],
        }
        for r in db.execute(
            "SELECT blank_id, usage_count, deduction, variance "
            "FROM summary_blanks WHERE summary_id = ? ORDER BY blank_id",
            (summary_id,),
        )
    ]
    return {
        "summary_id": row["summary_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "group_ids": group_ids,
        "source_group_ids": json.loads(row["source_group_ids"]),
        "source_summary_ids": json.loads(row["source_summary_ids"]),
        "total_measurements": row["total_measurements"],
        "reading_sum": row["reading_sum"],
        "variance_sum": row["variance_sum"],
        "blank_deduction_total": row["blank_deduction_total"],
        "corrected_mean": row["corrected_mean"],
        "standard_error": row["standard_error"],
        # 生成该汇总时采用的空白样数据（快照，不随后续更正改变）
        "blanks": blanks,
        "created_at": row["created_at"],
    }


def missing_data_error(**missing: list) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "error": "missing_data",
            "message": "资料缺失，本次不生成汇总。",
            "missing": {k: v for k, v in missing.items() if v},
        },
    )


# ---------------------------------------------------------------- 基础接口

@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict[str, str]:
    return {"service": "浓度汇总 API", "docs": "/docs", "health": "/health"}


# ---------------------------------------------------------------- 样组

@app.post("/groups", status_code=201, tags=["groups"],
          summary="登记样组（登记后固定；复原样组的原件核验同走此入口）")
def create_group(body: GroupCreate, db: Db) -> Any:
    exists = db.execute(
        "SELECT 1 FROM sample_groups WHERE group_id = ?", (body.group_id,)
    ).fetchone()
    if exists:
        restored = db.execute(
            "SELECT 1 FROM restored_groups WHERE group_id = ?", (body.group_id,)
        ).fetchone()
        if restored is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "group_exists",
                    "message": f"样组 {body.group_id} 已登记；样组登记后固定，不可修改。",
                },
            )
        # 复原样组：原件找回后报送同号样组，与复原记录核对
        return verify_restored_group(db, body)
    with db:
        db.execute(
            "INSERT INTO sample_groups "
            "(group_id, analyte, unit, reading_sum, variance_sum, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (body.group_id, body.analyte, body.unit,
             body.reading_sum, body.variance_sum, utcnow()),
        )
        db.executemany(
            "INSERT INTO group_blank_usages (group_id, blank_id, usage_count) "
            "VALUES (?, ?, ?)",
            [(body.group_id, b, c) for b, c in sorted(body.blank_usages.items())],
        )
    return group_payload(db, body.group_id)


def verify_restored_group(db: sqlite3.Connection, body: GroupCreate) -> JSONResponse:
    """原件核验：报送内容与复原记录一致则标为已核，不一致报冲突并保留原记录。"""
    current = group_payload(db, body.group_id)
    differences: list[dict[str, Any]] = []
    for field in ("analyte", "unit"):
        if getattr(body, field) != current[field]:
            differences.append({
                "field": field,
                "recorded": current[field],
                "submitted": getattr(body, field),
            })
    for field in ("reading_sum", "variance_sum"):
        if float(getattr(body, field)) != float(current[field]):
            differences.append({
                "field": field,
                "recorded": current[field],
                "submitted": getattr(body, field),
            })
    submitted_usages = dict(body.blank_usages)
    for b in sorted(set(current["blank_usages"]) | set(submitted_usages)):
        recorded = current["blank_usages"].get(b)
        submitted = submitted_usages.get(b)
        if recorded != submitted:
            differences.append({
                "field": f"blank_usages.{b}",
                "recorded": recorded,
                "submitted": submitted,
            })
    now = utcnow()
    if differences:
        conflict = {
            "reported_at": now,
            "submitted": {
                "analyte": body.analyte,
                "unit": body.unit,
                "reading_sum": body.reading_sum,
                "variance_sum": body.variance_sum,
                "blank_usages": submitted_usages,
            },
            "differences": differences,
        }
        with db:
            db.execute(
                "UPDATE restored_groups SET verification_status = 'conflict', "
                "verified_at = NULL, conflict = ? WHERE group_id = ?",
                (json.dumps(conflict), body.group_id),
            )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "restored_conflict",
                "message": f"报送内容与复原样组 {body.group_id} 的记录不一致，"
                           "保留原复原记录。",
                "differences": differences,
            },
        )
    with db:
        db.execute(
            "UPDATE restored_groups SET verification_status = 'verified', "
            "verified_at = ?, conflict = NULL WHERE group_id = ?",
            (now, body.group_id),
        )
    return JSONResponse(status_code=200, content=group_payload(db, body.group_id))


@app.get("/groups/{group_id}", tags=["groups"], summary="查询样组")
def get_group(group_id: str, db: Db) -> dict[str, Any]:
    payload = group_payload(db, group_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "group_not_found", "message": f"样组 {group_id} 不存在。"},
        )
    return payload


@app.get("/groups", tags=["groups"], summary="列出全部样组")
def list_groups(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["group_id"]
        for r in db.execute("SELECT group_id FROM sample_groups ORDER BY group_id")
    ]
    return [group_payload(db, g) for g in ids]


# ---------------------------------------------------------------- 空白样

@app.post("/blanks", status_code=201, tags=["blanks"], summary="登记空白样")
def create_blank(body: BlankCreate, db: Db) -> dict[str, Any]:
    exists = db.execute(
        "SELECT 1 FROM blanks WHERE blank_id = ?", (body.blank_id,)
    ).fetchone()
    if exists:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "blank_exists",
                "message": f"空白样 {body.blank_id} 已登记；如需更正请使用 PUT /blanks/{body.blank_id}。",
            },
        )
    now = utcnow()
    with db:
        db.execute(
            "INSERT INTO blanks (blank_id, deduction, variance, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (body.blank_id, body.deduction, body.variance, now, now),
        )
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (body.blank_id,)).fetchone()
    return blank_payload(row)


@app.put("/blanks/{blank_id}", tags=["blanks"], summary="更正空白样的扣减值与方差")
def update_blank(blank_id: str, body: BlankUpdate, db: Db) -> dict[str, Any]:
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "blank_not_found", "message": f"空白样 {blank_id} 尚未登记。"},
        )
    with db:
        db.execute(
            "UPDATE blanks SET deduction = ?, variance = ?, updated_at = ? "
            "WHERE blank_id = ?",
            (body.deduction, body.variance, utcnow(), blank_id),
        )
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    return blank_payload(row)


@app.get("/blanks/{blank_id}", tags=["blanks"], summary="查询空白样当前数据")
def get_blank(blank_id: str, db: Db) -> dict[str, Any]:
    row = db.execute("SELECT * FROM blanks WHERE blank_id = ?", (blank_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "blank_not_found", "message": f"空白样 {blank_id} 尚未登记。"},
        )
    return blank_payload(row)


@app.get("/blanks", tags=["blanks"], summary="列出全部空白样")
def list_blanks(db: Db) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM blanks ORDER BY blank_id").fetchall()
    return [blank_payload(r) for r in rows]


# ---------------------------------------------------------------- 汇总

@app.post("/summaries", status_code=201, tags=["summaries"], summary="生成新汇总")
def create_summary(body: SummaryRequest, db: Db) -> dict[str, Any]:
    """凭样组号、已有汇总号或两者组合生成汇总。

    - 重叠样组只计一次；
    - 引用旧汇总时展开为其样组集合，按**当前**空白样数据重算；
    - 资料缺失时返回所缺编号（404），本次不生成汇总；
    - 指标 / 单位不一致时返回 409。
    """
    # 1) 校验已有汇总号
    summary_ids = list(dict.fromkeys(body.summary_ids))
    missing_summary_ids = [
        s for s in summary_ids
        if not db.execute("SELECT 1 FROM summaries WHERE summary_id = ?", (s,)).fetchone()
    ]

    # 2) 校验直接给出的样组号
    direct_group_ids = list(dict.fromkeys(body.group_ids))
    missing_group_ids = [
        g for g in direct_group_ids
        if not db.execute("SELECT 1 FROM sample_groups WHERE group_id = ?", (g,)).fetchone()
    ]

    if missing_summary_ids or missing_group_ids:
        raise missing_data_error(
            summary_ids=missing_summary_ids, group_ids=missing_group_ids
        )

    # 3) 展开旧汇总为其样组集合，与直接样组取并集 —— 重叠样组只计一次
    group_ids: set[str] = set(direct_group_ids)
    for sid in summary_ids:
        rows = db.execute(
            "SELECT group_id FROM summary_groups WHERE summary_id = ?", (sid,)
        ).fetchall()
        group_ids.update(r["group_id"] for r in rows)

    groups = [group_payload(db, g) for g in sorted(group_ids)]

    # 4) 只处理同一指标、同一单位的读数
    combos: dict[tuple[str, str], list[str]] = {}
    for g in groups:
        combos.setdefault((g["analyte"], g["unit"]), []).append(g["group_id"])
    if len(combos) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "inconsistent_analyte_or_unit",
                "message": "只能汇总同一指标、同一单位的读数。",
                "combinations": [
                    {"analyte": a, "unit": u, "group_ids": ids}
                    for (a, u), ids in sorted(combos.items())
                ],
            },
        )
    (analyte, unit), _ = next(iter(combos.items()))

    # 5) 汇总各空白样的总使用次数
    usage: dict[str, int] = {}
    for g in groups:
        for b, c in g["blank_usages"].items():
            usage[b] = usage.get(b, 0) + c

    # 6) 取当前空白样资料；缺失则列出所缺编号，本次不生成汇总
    blanks: dict[str, sqlite3.Row] = {}
    missing_blank_ids: list[str] = []
    for b in sorted(usage):
        row = db.execute(
            "SELECT blank_id, deduction, variance FROM blanks WHERE blank_id = ?", (b,)
        ).fetchone()
        if row is None:
            missing_blank_ids.append(b)
        else:
            blanks[b] = row
    if missing_blank_ids:
        raise missing_data_error(blank_ids=missing_blank_ids)

    # 7) 计算
    reading_sum = math.fsum(g["reading_sum"] for g in groups)
    variance_sum = math.fsum(g["variance_sum"] for g in groups)
    total_measurements = sum(usage.values())  # 每次测量只用一份空白样
    if total_measurements <= 0:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_measurements", "message": "测量总数为零，无法汇总。"},
        )
    blank_deduction_total = math.fsum(
        blanks[b]["deduction"] * n for b, n in usage.items()
    )
    corrected_mean = (reading_sum - blank_deduction_total) / total_measurements
    total_variance = variance_sum + math.fsum(
        blanks[b]["variance"] * n * n for b, n in usage.items()
    )
    standard_error = math.sqrt(total_variance) / total_measurements

    # 8) 写入汇总及当时采用的空白样数据快照
    with db:
        cur = db.execute(
            "INSERT INTO summaries "
            "(analyte, unit, total_measurements, reading_sum, variance_sum, "
            " blank_deduction_total, corrected_mean, standard_error, "
            " source_group_ids, source_summary_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                analyte, unit, total_measurements, reading_sum, variance_sum,
                blank_deduction_total, corrected_mean, standard_error,
                json.dumps(direct_group_ids), json.dumps(summary_ids), utcnow(),
            ),
        )
        summary_id = cur.lastrowid
        db.executemany(
            "INSERT INTO summary_groups (summary_id, group_id) VALUES (?, ?)",
            [(summary_id, g) for g in sorted(group_ids)],
        )
        db.executemany(
            "INSERT INTO summary_blanks "
            "(summary_id, blank_id, usage_count, deduction, variance) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (summary_id, b, usage[b], blanks[b]["deduction"], blanks[b]["variance"])
                for b in sorted(usage)
            ],
        )
    return summary_payload(db, summary_id)


@app.get("/summaries/{summary_id}", tags=["summaries"], summary="查询旧汇总（原结果与当时空白样数据）")
def get_summary(summary_id: int, db: Db) -> dict[str, Any]:
    payload = summary_payload(db, summary_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "summary_not_found", "message": f"汇总 {summary_id} 不存在。"},
        )
    return payload


@app.get("/summaries", tags=["summaries"], summary="列出全部汇总")
def list_summaries(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["summary_id"]
        for r in db.execute("SELECT summary_id FROM summaries ORDER BY summary_id")
    ]
    return [summary_payload(db, s) for s in ids]


# ---------------------------------------------------------------- 样组复原

class TableSolver:
    """同一系数矩阵（表 × 缺失样组的 0/1 关联）下多组右端项的精确求解。

    高斯消元（分数运算）把系数矩阵化为行最简，同时记录行变换；
    对每个右端项施加同样变换后即可判定矛盾并读出唯一解。
    """

    def __init__(self, matrix: list[list[int]]) -> None:
        rows = len(matrix)
        cols = len(matrix[0])
        self.cols = cols
        # 增广 [A | I]：左侧做行最简，右侧记录行变换
        aug = [
            [Fraction(v) for v in row]
            + [Fraction(int(i == j)) for j in range(rows)]
            for i, row in enumerate(matrix)
        ]
        self.pivots: list[int] = []
        r = 0
        for c in range(cols):
            piv = next((i for i in range(r, rows) if aug[i][c] != 0), None)
            if piv is None:
                continue
            aug[r], aug[piv] = aug[piv], aug[r]
            pivot_value = aug[r][c]
            aug[r] = [v / pivot_value for v in aug[r]]
            for i in range(rows):
                if i != r and aug[i][c] != 0:
                    factor = aug[i][c]
                    aug[i] = [a - factor * b for a, b in zip(aug[i], aug[r])]
            self.pivots.append(c)
            r += 1
        self.rank = len(self.pivots)
        self._transform = [row[cols:] for row in aug]

    def solve(self, rhs: list[int]) -> list[Fraction] | None:
        """返回唯一解（长度为 cols）；右端项与系数矩阵矛盾时返回 None。"""
        transformed = [
            sum(coeff * Fraction(value) for coeff, value in zip(trow, rhs))
            for trow in self._transform
        ]
        if any(transformed[i] != 0 for i in range(self.rank, len(transformed))):
            return None
        solution = [Fraction(0)] * self.cols
        for i, c in enumerate(self.pivots):
            solution[c] = transformed[i]
        return solution


def determine_restoration(
    db: sqlite3.Connection, body: RestorationRequest
) -> dict[str, Any]:
    """按当前登记状态核算复原方案。

    失败抛 HTTPException（说明原因，不落任何账）；成功返回方案（仍不落账）。
    本函数先在事务外执行一次（核算期间普通登记可照常完成），
    落账前再在写事务内执行一次，按当时已有的登记作最终判定。
    """
    table_groups = {t.table_id: list(dict.fromkeys(t.group_ids)) for t in body.tables}
    involved = sorted({g for ids in table_groups.values() for g in ids})

    # 已登记样组的值固定，参与核算；其余为待复原的缺失样组
    registered: dict[str, dict[str, Any]] = {}
    for g in involved:
        payload = group_payload(db, g)
        if payload is not None:
            registered[g] = payload
    missing = [g for g in involved if g not in registered]
    if not missing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "nothing_to_restore",
                "message": "表内样组均已登记，没有需要复原的样组。",
                "group_ids": involved,
            },
        )

    # 一次复原只处理同一指标、同一单位
    mismatched = [
        {"group_id": g, "analyte": p["analyte"], "unit": p["unit"]}
        for g, p in registered.items()
        if p["analyte"] != body.analyte or p["unit"] != body.unit
    ]
    if mismatched:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "inconsistent_analyte_or_unit",
                "message": "表内已登记样组的指标/单位与本次复原不一致。",
                "expected": {"analyte": body.analyte, "unit": body.unit},
                "mismatched": mismatched,
            },
        )

    # 参与核算的已登记值须为绝对值不超过一万亿的整数
    invalid_registered: list[dict[str, Any]] = []
    for g, p in registered.items():
        for field in ("reading_sum", "variance_sum"):
            value = p[field]
            if not (float(value).is_integer() and abs(value) <= VALUE_LIMIT):
                invalid_registered.append(
                    {"group_id": g, "field": field, "value": value}
                )
        for blank_id, count in p["blank_usages"].items():
            if count > VALUE_LIMIT:
                invalid_registered.append({
                    "group_id": g,
                    "field": f"blank_usages.{blank_id}",
                    "value": count,
                })
    if invalid_registered:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "registered_values_invalid",
                "message": "参与核算的已登记值须为绝对值不超过一万亿的整数。",
                "values": invalid_registered,
            },
        )

    # 方程组：每行一张表、每列一个缺失样组，表含样组则系数为 1。
    # 读数总和、方差之和、各空白样使用次数共用该系数矩阵。
    solver = TableSolver(
        [
            [1 if g in table_groups[t.table_id] else 0 for g in missing]
            for t in body.tables
        ]
    )
    if solver.rank < len(missing):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "solution_not_unique",
                "message": "现有表格合计不足以唯一确定各缺失样组，解不唯一。",
                "missing_group_ids": missing,
            },
        )

    def solve_quantity(quantity: str, rhs: list[int]) -> list[Fraction]:
        solution = solver.solve(rhs)
        if solution is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "equations_inconsistent",
                    "message": "表格合计与已登记值互相矛盾，方程组无解。",
                    "quantity": quantity,
                },
            )
        return solution

    def registered_total(table: RestorationTable, field: str) -> int:
        return sum(
            int(registered[g][field])
            for g in table_groups[table.table_id]
            if g in registered
        )

    readings = solve_quantity(
        "reading_sum",
        [t.reading_total - registered_total(t, "reading_sum") for t in body.tables],
    )
    variances = solve_quantity(
        "variance_sum",
        [t.variance_total - registered_total(t, "variance_sum") for t in body.tables],
    )
    blank_ids = sorted(
        {b for t in body.tables for b in t.blank_usages}
        | {b for p in registered.values() for b in p["blank_usages"]}
    )
    usages: dict[str, list[Fraction]] = {}
    for b in blank_ids:
        rhs = [
            t.blank_usages.get(b, 0)  # 未列的空白样按零次计
            - sum(
                registered[g]["blank_usages"].get(b, 0)
                for g in table_groups[t.table_id]
                if g in registered
            )
            for t in body.tables
        ]
        usages[b] = solve_quantity(f"blank_usages.{b}", rhs)

    # 复原值须符合原登记规则：整数、绝对值不超过一万亿、方差非负、
    # 空白样使用次数为正整数且每组至少一份空白样
    problems: list[dict[str, Any]] = []

    def check_value(
        group_id: str, field: str, value: Fraction, negative_reason: str | None
    ) -> bool:
        if value.denominator != 1:
            problems.append({"group_id": group_id, "field": field,
                             "value": str(value), "reason": "复原值不是整数"})
            return False
        if abs(value) > VALUE_LIMIT:
            problems.append({"group_id": group_id, "field": field,
                             "value": str(value), "reason": "绝对值超过一万亿"})
            return False
        if negative_reason is not None and value < 0:
            problems.append({"group_id": group_id, "field": field,
                             "value": str(value), "reason": negative_reason})
            return False
        return True

    for i, g in enumerate(missing):
        check_value(g, "reading_sum", readings[i], None)
        check_value(g, "variance_sum", variances[i], "方差之和为负")
        usages_ok = True
        for b in blank_ids:
            if not check_value(g, f"blank_usages.{b}", usages[b][i], "使用次数为负"):
                usages_ok = False
        if usages_ok and all(usages[b][i] == 0 for b in blank_ids):
            problems.append({"group_id": g, "field": "blank_usages", "value": "{}",
                             "reason": "样组没有任何空白样使用记录"})
    if problems:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "solution_violates_rules",
                "message": "复原值不符合原登记规则（读数总和为整数；方差之和为非负整数；"
                           "空白样使用次数为正整数且至少一项；绝对值均不超过一万亿）。",
                "problems": problems,
            },
        )

    solutions = {
        g: {
            "reading_sum": int(readings[i]),
            "variance_sum": int(variances[i]),
            "blank_usages": {
                b: int(usages[b][i]) for b in blank_ids if usages[b][i] > 0
            },
        }
        for i, g in enumerate(missing)
    }
    return {"missing": missing, "solutions": solutions, "table_groups": table_groups}


def restoration_payload(
    db: sqlite3.Connection, restoration_id: int
) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM restorations WHERE restoration_id = ?", (restoration_id,)
    ).fetchone()
    if row is None:
        return None
    tables = [
        {
            "table_id": r["table_id"],
            "group_ids": json.loads(r["group_ids"]),
            "reading_total": r["reading_total"],
            "variance_total": r["variance_total"],
            "blank_usages": json.loads(r["blank_usages"]),
        }
        for r in db.execute(
            "SELECT * FROM restoration_tables WHERE restoration_id = ? "
            "ORDER BY table_id",
            (restoration_id,),
        )
    ]
    restored_ids = [
        r["group_id"]
        for r in db.execute(
            "SELECT group_id FROM restored_groups WHERE restoration_id = ? "
            "ORDER BY group_id",
            (restoration_id,),
        )
    ]
    return {
        "restoration_id": row["restoration_id"],
        "analyte": row["analyte"],
        "unit": row["unit"],
        "tables": tables,
        "restored_group_ids": restored_ids,
        "groups": [group_payload(db, g) for g in restored_ids],
        "created_at": row["created_at"],
    }


@app.post("/restorations", status_code=201, tags=["restorations"],
          summary="样组复原（凭旧汇总表合计整批补录缺失样组）")
def create_restoration(body: RestorationRequest, db: Db) -> dict[str, Any]:
    """旧汇总表还在、原样组记录丢失时，凭各表合计复原缺失样组。

    - 同一指标、单位下最多 12 张表、合计最多 12 个样组；
    - 表内合计、参与核算的已登记值与复原值均限绝对值不超过一万亿的整数；
    - 固定已登记样组的值，用各表合计核对缺失样组；
    - 等式有唯一解且符合原登记规则才整批补录，否则说明原因、全部撤销；
    - 核算期间普通登记可照常完成，落账前按当时已有的登记作最终判定。
    """
    # 阶段一：只读核算，不持写锁 —— 核算期间普通登记可照常完成
    determine_restoration(db, body)
    # 阶段二：写事务内按当时已有的登记作最终判定；整批补录或全部撤销
    db.execute("BEGIN IMMEDIATE")
    try:
        plan = determine_restoration(db, body)
        now = utcnow()
        cur = db.execute(
            "INSERT INTO restorations (analyte, unit, created_at) VALUES (?, ?, ?)",
            (body.analyte, body.unit, now),
        )
        restoration_id = cur.lastrowid
        for t in body.tables:
            db.execute(
                "INSERT INTO restoration_tables "
                "(restoration_id, table_id, group_ids, reading_total, "
                " variance_total, blank_usages) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    restoration_id, t.table_id,
                    json.dumps(plan["table_groups"][t.table_id]),
                    t.reading_total, t.variance_total,
                    json.dumps({b: c for b, c in sorted(t.blank_usages.items())}),
                ),
            )
        for g in plan["missing"]:
            solution = plan["solutions"][g]
            db.execute(
                "INSERT INTO sample_groups "
                "(group_id, analyte, unit, reading_sum, variance_sum, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (g, body.analyte, body.unit,
                 float(solution["reading_sum"]), float(solution["variance_sum"]),
                 now),
            )
            db.executemany(
                "INSERT INTO group_blank_usages (group_id, blank_id, usage_count) "
                "VALUES (?, ?, ?)",
                [(g, b, c) for b, c in sorted(solution["blank_usages"].items())],
            )
            db.execute(
                "INSERT INTO restored_groups "
                "(group_id, restoration_id, verification_status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (g, restoration_id, now),
            )
            db.executemany(
                "INSERT INTO restored_group_tables (group_id, table_id) VALUES (?, ?)",
                [
                    (g, t.table_id)
                    for t in body.tables
                    if g in plan["table_groups"][t.table_id]
                ],
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return restoration_payload(db, restoration_id)


@app.get("/restorations/{restoration_id}", tags=["restorations"], summary="查询复原批次")
def get_restoration(restoration_id: int, db: Db) -> dict[str, Any]:
    payload = restoration_payload(db, restoration_id)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "restoration_not_found",
                    "message": f"复原批次 {restoration_id} 不存在。"},
        )
    return payload


@app.get("/restorations", tags=["restorations"], summary="列出全部复原批次")
def list_restorations(db: Db) -> list[dict[str, Any]]:
    ids = [
        r["restoration_id"]
        for r in db.execute(
            "SELECT restoration_id FROM restorations ORDER BY restoration_id"
        )
    ]
    return [restoration_payload(db, i) for i in ids]
