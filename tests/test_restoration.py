"""样组复原 API 的端到端测试。

可用 pytest 运行，也可直接执行：python3 tests/test_restoration.py
覆盖：唯一解复原、欠定/矛盾/复原值违规的整批撤销、表数与样组数上限、
表内合计整数上限、已登记值整数核对、指标/单位一致性、未列空白样按零次计、
原件核验（已核/冲突/保留原记录）、复原样组参与汇总、复原批次查询。
"""
import os
import sys
import tempfile

# 必须在导入应用之前指定独立数据库
_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.remove(_db_path)
os.environ["DB_PATH"] = _db_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.db as app_db  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def check(cond, msg):
    assert cond, msg


def run():
    # 与同进程的其他测试文件隔离：强制使用本文件自己的数据库
    app_db.DB_PATH = _db_path
    app_db.init_db()

    # ---- 准备：空白样与已登记样组 ----
    client.post("/blanks", json={"blank_id": "BA", "deduction": 0.5, "variance": 0.01})
    client.post("/blanks", json={"blank_id": "BB", "deduction": 1.0, "variance": 0.02})
    r = client.post("/groups", json={"group_id": "R1", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 100, "variance_sum": 4,
                                     "blank_usages": {"BA": 10}})
    check(r.status_code == 201, f"register R1: {r.text}")
    # 已登记值不是整数（普通登记允许小数）
    r = client.post("/groups", json={"group_id": "R2", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 1.5, "variance_sum": 0.5,
                                     "blank_usages": {"BA": 2}})
    check(r.status_code == 201, "register R2 (non-integer values)")
    # 不同指标
    r = client.post("/groups", json={"group_id": "R3", "analyte": "lead", "unit": "ug/L",
                                     "reading_sum": 7, "variance_sum": 1,
                                     "blank_usages": {"BA": 1}})
    check(r.status_code == 201, "register R3 (other analyte)")
    # 已登记值超出一万亿
    r = client.post("/groups", json={"group_id": "R4", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 2_000_000_000_000, "variance_sum": 0,
                                     "blank_usages": {"BA": 1}})
    check(r.status_code == 201, "register R4 (value over one trillion)")
    # 空白样使用次数超一万亿
    r = client.post("/groups", json={"group_id": "R5", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 5, "variance_sum": 1,
                                     "blank_usages": {"BA": 10_000_000_000_000}})
    check(r.status_code == 201, "register R5 (usage over one trillion)")

    # ---- 成功复原：T-1={R1,M1}、T-2={M1,M2}，唯一解 ----
    # M1: 读数 160-100=60，方差 10-4=6，BA 15-10=5、BB 3；M2: 读数 110-60=50，
    # 方差 11-6=5，BA 8-5=3、BB 8-3=5
    body = {
        "analyte": "cod", "unit": "mg/L",
        "tables": [
            {"table_id": "T-1", "group_ids": ["R1", "M1"],
             "reading_total": 160, "variance_total": 10,
             "blank_usages": {"BA": 15, "BB": 3}},
            {"table_id": "T-2", "group_ids": ["M1", "M2"],
             "reading_total": 110, "variance_total": 11,
             "blank_usages": {"BA": 8, "BB": 8}},
        ],
    }
    r = client.post("/restorations", json=body)
    check(r.status_code == 201, f"restore M1/M2: {r.text}")
    rest = r.json()
    check(rest["restored_group_ids"] == ["M1", "M2"], "restored ids")
    check(rest["analyte"] == "cod" and rest["unit"] == "mg/L", "restoration analyte/unit")
    check([t["table_id"] for t in rest["tables"]] == ["T-1", "T-2"], "tables recorded")
    groups = {g["group_id"]: g for g in rest["groups"]}
    m1, m2 = groups["M1"], groups["M2"]
    check(m1["reading_sum"] == 60 and m1["variance_sum"] == 6, "M1 solved values")
    check(m1["blank_usages"] == {"BA": 5, "BB": 3}, "M1 usages")
    check(m1["measurement_count"] == 8, "M1 measurement_count")
    check(m2["reading_sum"] == 50 and m2["variance_sum"] == 5, "M2 solved values")
    check(m2["blank_usages"] == {"BA": 3, "BB": 5}, "M2 usages")
    check(m1["restoration"]["verification_status"] == "pending", "M1 pending")
    check(m1["restoration"]["table_ids"] == ["T-1", "T-2"], "M1 table ids")
    check(m2["restoration"]["table_ids"] == ["T-2"], "M2 table ids")

    # ---- 查询复原样组：显示所用表号和原件核验状态 ----
    g = client.get("/groups/M1").json()
    check(g["restoration"]["restoration_id"] == rest["restoration_id"], "M1 batch id")
    check(g["restoration"]["verification_status"] == "pending", "M1 status in query")
    listed = {g["group_id"]: g for g in client.get("/groups").json()}
    check(listed["M1"]["restoration"]["table_ids"] == ["T-1", "T-2"],
          "list shows restoration info")
    check(listed["R1"]["restoration"] is None, "normal group has no restoration info")

    # ---- 原件核验：内容一致标为已核 ----
    r = client.post("/groups", json={"group_id": "M1", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 60, "variance_sum": 6,
                                     "blank_usages": {"BA": 5, "BB": 3}})
    check(r.status_code == 200, f"verify M1: {r.text}")
    check(r.json()["restoration"]["verification_status"] == "verified", "M1 verified")
    check(r.json()["restoration"]["verified_at"] is not None, "M1 verified_at")

    # ---- 原件核验：不一致报冲突、保留原记录 ----
    r = client.post("/groups", json={"group_id": "M2", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 51, "variance_sum": 5,
                                     "blank_usages": {"BA": 3, "BB": 5}})
    check(r.status_code == 409, "M2 mismatch -> 409")
    detail = r.json()["detail"]
    check(detail["error"] == "restored_conflict", "conflict error code")
    check(any(d["field"] == "reading_sum" and d["recorded"] == 50 and d["submitted"] == 51
              for d in detail["differences"]), "conflict difference listed")
    g = client.get("/groups/M2").json()
    check(g["reading_sum"] == 50 and g["variance_sum"] == 5, "conflict keeps original")
    check(g["restoration"]["verification_status"] == "conflict", "M2 conflict status")
    check(g["restoration"]["conflict"]["differences"], "conflict detail stored")
    # 之后报送一致内容仍可标为已核
    r = client.post("/groups", json={"group_id": "M2", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 50, "variance_sum": 5,
                                     "blank_usages": {"BA": 3, "BB": 5}})
    check(r.status_code == 200, "verify M2 after conflict")
    check(r.json()["restoration"]["verification_status"] == "verified", "M2 verified")
    check(r.json()["restoration"]["conflict"] is None, "conflict cleared after verify")

    # ---- 普通样组重复登记仍是 409 group_exists ----
    r = client.post("/groups", json={"group_id": "R1", "analyte": "cod", "unit": "mg/L",
                                     "reading_sum": 100, "variance_sum": 4,
                                     "blank_usages": {"BA": 10}})
    check(r.status_code == 409 and r.json()["detail"]["error"] == "group_exists",
          "normal duplicate stays group_exists")

    # ---- 复原样组可正常参与汇总 ----
    r = client.post("/summaries", json={"group_ids": ["M1", "M2"]})
    check(r.status_code == 201, f"summary with restored groups: {r.text}")
    check(r.json()["total_measurements"] == 16, "summary N over restored groups")
    check(r.json()["reading_sum"] == 110, "summary reading_sum over restored groups")

    # ---- 失败情形：说明原因，本次补录全部撤销 ----
    before_groups = len(client.get("/groups").json())
    before_rests = len(client.get("/restorations").json())

    # 欠定：一张表两个缺失样组，解不唯一
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "U1", "group_ids": ["U1", "U2"], "reading_total": 10,
         "variance_total": 2, "blank_usages": {"BA": 4}}]})
    check(r.status_code == 409, f"underdetermined -> 409: {r.text}")
    check(r.json()["detail"]["error"] == "solution_not_unique", "underdetermined code")

    # 矛盾：同一缺失样组两个不同合计
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "C1", "group_ids": ["C1"], "reading_total": 100,
         "variance_total": 1, "blank_usages": {"BA": 1}},
        {"table_id": "C2", "group_ids": ["C1"], "reading_total": 200,
         "variance_total": 1, "blank_usages": {"BA": 1}}]})
    check(r.status_code == 409, "inconsistent -> 409")
    check(r.json()["detail"]["error"] == "equations_inconsistent", "inconsistent code")
    check(r.json()["detail"]["quantity"] == "reading_sum", "inconsistent quantity")

    # 复原值不是整数：解为 1/2, 1/2, 3/2
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "F1", "group_ids": ["F1", "F2"], "reading_total": 1,
         "variance_total": 0, "blank_usages": {"BA": 2}},
        {"table_id": "F2", "group_ids": ["F2", "F3"], "reading_total": 2,
         "variance_total": 0, "blank_usages": {"BA": 2}},
        {"table_id": "F3", "group_ids": ["F1", "F3"], "reading_total": 2,
         "variance_total": 0, "blank_usages": {"BA": 2}}]})
    check(r.status_code == 409, "fractional solution -> 409")
    detail = r.json()["detail"]
    check(detail["error"] == "solution_violates_rules", "fractional code")
    check(any(p["field"] == "reading_sum" and "整数" in p["reason"]
              for p in detail["problems"]), "fractional reading reported")

    # 复原方差为负：表内方差合计小于已登记样组的方差
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "V1", "group_ids": ["R1", "V1"], "reading_total": 200,
         "variance_total": 2, "blank_usages": {"BA": 12}}]})
    check(r.status_code == 409, "negative variance -> 409")
    detail = r.json()["detail"]
    check(detail["error"] == "solution_violates_rules", "negative variance code")
    check(any(p["field"] == "variance_sum" for p in detail["problems"]),
          "negative variance reported")

    # 复原样组没有任何空白样使用记录
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "W1", "group_ids": ["R1", "W1"], "reading_total": 150,
         "variance_total": 9, "blank_usages": {"BA": 10}}]})
    check(r.status_code == 409, "no blank usage -> 409")
    check(any(p["field"] == "blank_usages" for p in r.json()["detail"]["problems"]),
          "empty usages reported")

    # 未列的空白样按零次计：与已登记用量矛盾（BA 未列 -> 总次数 0，但 R1 用了 10 次）
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "Q1", "group_ids": ["R1", "Q1"], "reading_total": 150,
         "variance_total": 9, "blank_usages": {"BB": 3}}]})
    check(r.status_code == 409, "unlisted blank counts as zero -> 409")
    check(any(p["field"] == "blank_usages.BA" for p in r.json()["detail"]["problems"]),
          "negative BA usage reported")

    # 参与核算的已登记值不是整数 / 超出一万亿
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "N1", "group_ids": ["R2", "N1"], "reading_total": 10,
         "variance_total": 2, "blank_usages": {"BA": 4}}]})
    check(r.status_code == 409, "non-integer registered -> 409")
    check(r.json()["detail"]["error"] == "registered_values_invalid",
          "non-integer registered code")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "N2", "group_ids": ["R4", "N2"], "reading_total": 1_000_000_000_000,
         "variance_total": 1, "blank_usages": {"BA": 2}}]})
    check(r.status_code == 409, "over-limit registered -> 409")
    check(r.json()["detail"]["error"] == "registered_values_invalid",
          "over-limit registered code")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "N3", "group_ids": ["R5", "N3"], "reading_total": 10,
         "variance_total": 2, "blank_usages": {"BA": 5}}]})
    check(r.status_code == 409, "over-limit registered usage -> 409")
    check(r.json()["detail"]["error"] == "registered_values_invalid",
          "over-limit usage code")

    # 表内已登记样组的指标/单位与本次复原不一致
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "N4", "group_ids": ["R3", "N4"], "reading_total": 10,
         "variance_total": 2, "blank_usages": {"BA": 2}}]})
    check(r.status_code == 409, "analyte mismatch -> 409")
    check(r.json()["detail"]["error"] == "inconsistent_analyte_or_unit",
          "analyte mismatch code")

    # 表内样组均已登记，没有需要复原的样组
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "N5", "group_ids": ["R1"], "reading_total": 100,
         "variance_total": 4, "blank_usages": {"BA": 10}}]})
    check(r.status_code == 409, "nothing to restore -> 409")
    check(r.json()["detail"]["error"] == "nothing_to_restore", "nothing_to_restore code")

    # 全部撤销：上述失败不留下任何样组或复原批次
    check(len(client.get("/groups").json()) == before_groups,
          "failed restorations add no groups")
    check(len(client.get("/restorations").json()) == before_rests,
          "failed restorations add no batches")
    check(client.get("/groups/U1").status_code == 404, "rolled-back group absent")

    # ---- 入参校验 ----
    # 超过 12 张表
    tables = [{"table_id": f"T{i}", "group_ids": [f"G{i}"], "reading_total": 1,
               "variance_total": 0, "blank_usages": {"BA": 1}} for i in range(13)]
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L",
                                           "tables": tables})
    check(r.status_code == 422, "13 tables -> 422")
    # 合计超过 12 个样组
    tables = [
        {"table_id": "A", "group_ids": [f"G{i}" for i in range(6)],
         "reading_total": 6, "variance_total": 0, "blank_usages": {"BA": 6}},
        {"table_id": "B", "group_ids": [f"G{i}" for i in range(6, 13)],
         "reading_total": 7, "variance_total": 0, "blank_usages": {"BA": 7}},
    ]
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L",
                                           "tables": tables})
    check(r.status_code == 422, "13 groups -> 422")
    # 表内合计超一万亿 / 负方差合计 / 负使用次数 / 表号重复 / 空表清单
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "X", "group_ids": ["X1"], "reading_total": 1_000_000_000_001,
         "variance_total": 0, "blank_usages": {}}]})
    check(r.status_code == 422, "total over one trillion -> 422")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "X", "group_ids": ["X1"], "reading_total": 1,
         "variance_total": -1, "blank_usages": {}}]})
    check(r.status_code == 422, "negative variance total -> 422")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "X", "group_ids": ["X1"], "reading_total": 1,
         "variance_total": 0, "blank_usages": {"BA": -1}}]})
    check(r.status_code == 422, "negative usage -> 422")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "X", "group_ids": ["X1"], "reading_total": 1,
         "variance_total": 0, "blank_usages": {"BA": 1}},
        {"table_id": "X", "group_ids": ["X2"], "reading_total": 1,
         "variance_total": 0, "blank_usages": {"BA": 1}}]})
    check(r.status_code == 422, "duplicate table id -> 422")
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L",
                                           "tables": []})
    check(r.status_code == 422, "no tables -> 422")

    # ---- 边界：恰好 12 张表、12 个样组；表号可用整数；合计可取一万亿 ----
    tables = [{"table_id": i + 1, "group_ids": [f"D{i + 1}"],
               "reading_total": (i + 1) * 10, "variance_total": i,
               "blank_usages": {"BA": 1}} for i in range(12)]
    tables[0]["reading_total"] = 1_000_000_000_000  # 一万亿整，含边界
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L",
                                           "tables": tables})
    check(r.status_code == 201, f"12 tables x 12 groups: {r.text}")
    check(len(r.json()["restored_group_ids"]) == 12, "12 groups restored")
    d1 = client.get("/groups/D1").json()
    check(d1["reading_sum"] == 1_000_000_000_000, "boundary value restored")
    check(d1["restoration"]["table_ids"] == ["1"], "int table id coerced to string")

    # ---- 已复原样组即已登记：后续复原按其固定值核算 ----
    # M1 固定为 读数 60 / 方差 6 / BA 5 / BB 3 -> E1: 100 / 4 / BA 10（BB 为零不计）
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "T-9", "group_ids": ["M1", "E1"], "reading_total": 160,
         "variance_total": 10, "blank_usages": {"BA": 15, "BB": 3}}]})
    check(r.status_code == 201, f"restore with fixed restored group: {r.text}")
    e1 = client.get("/groups/E1").json()
    check(e1["reading_sum"] == 100 and e1["variance_sum"] == 4, "E1 solved values")
    check(e1["blank_usages"] == {"BA": 10}, "E1 zero-usage blank dropped")
    check(e1["restoration"]["table_ids"] == ["T-9"], "E1 table id")

    # 固定值与表内合计矛盾：E2 由 T-10 解出 100，与 T-11 的 101 冲突
    r = client.post("/restorations", json={"analyte": "cod", "unit": "mg/L", "tables": [
        {"table_id": "T-10", "group_ids": ["M1", "E2"], "reading_total": 160,
         "variance_total": 10, "blank_usages": {"BA": 15, "BB": 3}},
        {"table_id": "T-11", "group_ids": ["E2"], "reading_total": 101,
         "variance_total": 4, "blank_usages": {"BA": 10}}]})
    check(r.status_code == 409, "fixed values vs totals -> 409")
    check(r.json()["detail"]["error"] == "equations_inconsistent",
          "fixed values checked against totals")
    check(client.get("/groups/E2").status_code == 404, "E2 rolled back")

    # ---- 复原批次查询 ----
    rests = client.get("/restorations").json()
    check(len(rests) == 3, "three successful restorations")
    first = client.get(f"/restorations/{rests[0]['restoration_id']}")
    check(first.status_code == 200, "get restoration")
    check(first.json()["restored_group_ids"] == ["M1", "M2"], "batch restored ids")
    check(first.json()["tables"][0]["table_id"] == "T-1", "batch keeps table data")
    check(first.json()["tables"][0]["blank_usages"] == {"BA": 15, "BB": 3},
          "batch keeps table usages")
    check(client.get("/restorations/999").status_code == 404,
          "missing restoration -> 404")

    print("ALL RESTORATION TESTS PASSED")


def test_api():
    run()


if __name__ == "__main__":
    run()
