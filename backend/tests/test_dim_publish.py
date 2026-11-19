"""维度发布校验与主键发布快照业务测试。

覆盖：
- 发布校验结果落库，主键为空/类型相斥时维度不能进入已发布，也不多出发布事件
- 主键发布快照固化维度编码、属性字段与主键类型
- 补发失败时保留上一份已确认快照，已发布属性仍按原主键类型可用
- 维度属性登记按快照判定关联键与可用列，审核迟到的属性不自动进入旧快照
"""
from __future__ import annotations

import pytest

from app.store import Store
from app.services.dim_definition import DimDefinitionService
from app.services.dim_attr import DimAttrService
from app.services import pk_snapshot


@pytest.fixture
def store() -> Store:
    """每个测试复用同一个存储单例，但重置为初始示例数据。"""
    from app.store import store as shared_store
    shared_store.reset()
    return shared_store


@pytest.fixture
def dims(store: Store) -> DimDefinitionService:
    return DimDefinitionService()


@pytest.fixture
def attrs(store: Store) -> DimAttrService:
    return DimAttrService()


def publish_events(store: Store) -> list[dict]:
    return store.channel.topic_snapshot("dim.published")


def create_dimension(dims: DimDefinitionService, code: str, pk: str) -> dict:
    entry, missing = dims.create_entry({
        "维度编码": code,
        "维度名称": f"{code} 名称",
        "主键定义": pk,
        "SCD类型": "SCD类型2",
        "粒度说明": "客户级别",
        "数据来源": "crm",
        "刷新频率": "每日",
    })
    assert not missing, missing
    assert entry is not None
    return entry


def create_attribute(attrs: DimAttrService, dim_code: str, name: str, data_type: str) -> dict:
    entry, missing = attrs.create_entry({
        "属性名称": name,
        "所属维度": dim_code,
        "数据类型": data_type,
        "字段来源": f"crm.{name}",
        "是否派生": "否",
        "历史追踪": "否",
        "默认值": "",
    })
    assert not missing, missing
    assert entry is not None
    return entry


def find_row(rows: list[dict], entry_id: int) -> dict:
    return next(row for row in rows if row["id"] == entry_id)


# ======================================================================
# Section A: 发布校验落库与状态约束
# ======================================================================


class TestPublishValidation:
    def test_conflicting_key_attribute_keeps_status_and_no_publish_event(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """整数主键 + 字符型关联键属性：状态不进入已发布，也不多出发布事件。"""
        dim = create_dimension(dims, "DIM_CUSTOMER_CP", "customer_id INT")
        code_attr = create_attribute(attrs, "DIM_CUSTOMER_CP", "customer_code", "VARCHAR(32)")
        attrs.run_action(code_attr["id"], "确认属性")

        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is None
        assert "相斥" in message
        row = store.find("dim_definition", dim["id"])
        assert row["status"] == "草稿"
        assert row["发布校验结果"]["ok"] is False
        assert row["发布校验结果"]["键类型冲突"] is True
        assert row["发布校验"] == row["发布校验结果"]["message"]
        assert publish_events(store) == []

    def test_empty_primary_key_cannot_publish(
        self, store: Store, dims: DimDefinitionService,
    ) -> None:
        """主键为空：发布校验落库为失败，维度不能进入已发布。"""
        dim = create_dimension(dims, "DIM_EMPTY_PK", "placeholder_id INT")
        # 模拟历史数据或外部同步把主键定义清空
        store.find("dim_definition", dim["id"])["主键定义"] = ""
        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is None
        row = store.find("dim_definition", dim["id"])
        assert row["status"] == "草稿"
        codes = [issue["code"] for issue in row["发布校验结果"]["issues"]]
        assert "empty_primary_key" in codes
        assert publish_events(store) == []

    def test_primary_key_without_type_cannot_publish(
        self, store: Store, dims: DimDefinitionService,
    ) -> None:
        """主键只写列名不写类型：按主键规则无法判定键类型，发布被拦截。"""
        dim = create_dimension(dims, "DIM_NO_TYPE", "customer_id")
        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is None
        row = store.find("dim_definition", dim["id"])
        assert row["status"] == "草稿"
        assert "unknown_primary_key_type" in [
            issue["code"] for issue in row["发布校验结果"]["issues"]
        ]
        assert "类型" in message

    def test_publish_persists_evaluation_and_snapshot(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """校验通过：状态进入已发布，主键发布快照固化编码、属性与主键类型。"""
        dim = create_dimension(dims, "DIM_CUSTOMER_OK", "customer_id BIGINT")
        pk_attr = create_attribute(attrs, "DIM_CUSTOMER_OK", "customer_id", "BIGINT")
        name_attr = create_attribute(attrs, "DIM_CUSTOMER_OK", "customer_name", "VARCHAR(128)")
        pending = create_attribute(attrs, "DIM_CUSTOMER_OK", "customer_alias", "VARCHAR(64)")
        attrs.run_action(pk_attr["id"], "确认属性")
        attrs.run_action(name_attr["id"], "确认属性")

        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is not None
        assert entry["status"] == "已发布"
        assert len(publish_events(store)) == 1
        assert "主键发布快照" in message

        snap = pk_snapshot.latest_publish_snapshot(store, "DIM_CUSTOMER_OK")
        assert snap is not None
        data = snap["data"]
        assert data["维度编码"] == "DIM_CUSTOMER_OK"
        assert data["主键类型"] == "BIGINT"
        assert data["主键类型族"] == "INTEGER"
        assert data["属性字段"] == ["customer_id", "customer_name"]
        # 待审属性不进入快照
        assert pending["属性名称"] not in data["属性字段"]

        row = find_row(store.rows("dim_definition"), dim["id"])
        assert row["发布校验结果"]["ok"] is True
        assert row["主键发布快照"] == snap["snap_id"]

    def test_dimension_list_exposes_key_type_and_conflict(
        self, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """维度列表展示键类型、发布校验与键类型冲突判定。"""
        dim = create_dimension(dims, "DIM_LIST_CASE", "customer_id INT")
        attr = create_attribute(attrs, "DIM_LIST_CASE", "customer_code", "VARCHAR(32)")
        attrs.run_action(attr["id"], "确认属性")
        dims.run_action(dim["id"], "发布维度")

        rows, _ = dims.list_entries()
        row = find_row(rows, dim["id"])
        assert row["键类型"] == "INT"
        assert row["键类型族"] == "INTEGER"
        assert row["键类型冲突"] == "是"
        assert "相斥" in row["发布校验"]
        assert row["发布校验通过"] == "否"

    def test_action_status_transitions(self, store: Store, dims: DimDefinitionService) -> None:
        """提交建模 → 发布维度 → 停用维度 的状态流转。"""
        dim = create_dimension(dims, "DIM_FLOW", "id INT")
        dims.run_action(dim["id"], "提交建模")
        assert store.find("dim_definition", dim["id"])["status"] == "已建模"

        dims.run_action(dim["id"], "发布维度")
        assert store.find("dim_definition", dim["id"])["status"] == "已发布"

        dims.run_action(dim["id"], "停用维度")
        assert store.find("dim_definition", dim["id"])["status"] == "已停用"


# ======================================================================
# Section B: 补发与快照保留
# ======================================================================


class TestRepublish:
    def test_republish_failure_keeps_previous_snapshot(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """补发遇到键类型冲突：旧快照保留，已发布属性仍按原主键类型可用。"""
        dim = create_dimension(dims, "DIM_REPUB", "customer_id INT")
        key_attr = create_attribute(attrs, "DIM_REPUB", "customer_id", "INT")
        name_attr = create_attribute(attrs, "DIM_REPUB", "customer_name", "VARCHAR(128)")
        for attr in (key_attr, name_attr):
            attrs.run_action(attr["id"], "确认属性")

        dims.run_action(dim["id"], "发布维度")
        first_snap = pk_snapshot.latest_publish_snapshot(store, "DIM_REPUB")
        assert first_snap is not None
        events_before = len(publish_events(store))

        # 已发布属性被改成字符型后再补发，触发主键类型相斥
        store.find("dim_attr", key_attr["id"])["数据类型"] = "VARCHAR(64)"
        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is None
        assert "补发未执行" in message
        row = store.find("dim_definition", dim["id"])
        assert row["status"] == "已发布"
        assert row["发布校验结果"]["ok"] is False
        assert row["发布校验结果"]["键类型冲突"] is True
        assert row["维度版本"] == first_snap["data"]["维度版本"]
        assert row["发布校验结果"]["保留快照"] == first_snap["snap_id"]

        # 没有新的发布/补发事件，旧快照仍是最近一次已确认快照
        assert len(publish_events(store)) == events_before
        assert pk_snapshot.latest_publish_snapshot(store, "DIM_REPUB")["snap_id"] == first_snap["snap_id"]

        rows, _ = attrs.list_entries()
        name_row = find_row(rows, name_attr["id"])
        assert name_row["维度版本"] == first_snap["data"]["维度版本"]
        assert name_row["键类型"] == "INT"
        assert name_row["可用列文本"] == "customer_id、customer_name"
        assert name_row["快照一致"] == "是"

    def test_successful_republish_creates_next_version(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """补发成功：生成下一版快照，不重复投递首次发布事件。"""
        dim = create_dimension(dims, "DIM_REPUB_OK", "customer_id INT")
        key_attr = create_attribute(attrs, "DIM_REPUB_OK", "customer_id", "INT")
        attrs.run_action(key_attr["id"], "确认属性")

        dims.run_action(dim["id"], "发布维度")
        first = pk_snapshot.latest_publish_snapshot(store, "DIM_REPUB_OK")
        assert first is not None

        late_attr = create_attribute(attrs, "DIM_REPUB_OK", "customer_level", "VARCHAR(32)")
        attrs.run_action(late_attr["id"], "确认属性")

        entry, message = dims.run_action(dim["id"], "发布维度")

        assert entry is not None
        assert "补发" in message
        second = pk_snapshot.latest_publish_snapshot(store, "DIM_REPUB_OK")
        assert second is not None and second["snap_id"] != first["snap_id"]
        assert second["data"]["维度版本"] != first["data"]["维度版本"]
        assert "customer_level" in second["data"]["属性字段"]
        assert len(publish_events(store)) == 1
        assert len(store.channel.topic_snapshot("dim.republished")) == 1


# ======================================================================
# Section C: 维度属性登记读取主键发布快照
# ======================================================================


class TestAttributeSnapshot:
    def test_attribute_reads_snapshot_for_join_key(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """属性登记按快照判定客户编码可作为关联键，并指出维度版本与键类型。"""
        dim = create_dimension(dims, "DIM_ATTR_OK", "customer_code VARCHAR(32)")
        cookie_attr = create_attribute(attrs, "DIM_ATTR_OK", "customer_code", "VARCHAR(32)")
        attrs.run_action(cookie_attr["id"], "确认属性")
        dims.run_action(dim["id"], "发布维度")

        rows, _ = attrs.list_entries()
        row = find_row(rows, cookie_attr["id"])
        assert row["关联键可用"] == "是"
        assert row["维度版本"] == "v1"
        assert row["键类型"] == "VARCHAR"
        assert "v1" in row["校验明细"]
        assert "VARCHAR" in row["校验明细"]
        assert "主键规则" in row["校验明细"]

    def test_attribute_reports_type_mismatch_against_snapshot(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """未发布维度无快照时，属性按待定处理且明细说明尚无快照。"""
        dim = create_dimension(dims, "DIM_ATTR_NOSNAP", "customer_id INT")
        attr = create_attribute(attrs, "DIM_ATTR_NOSNAP", "customer_code", "VARCHAR(32)")
        attrs.run_action(attr["id"], "确认属性")

        rows, _ = attrs.list_entries()
        row = find_row(rows, attr["id"])
        assert row["关联键可用"] == "待定"
        assert row["维度版本"] == ""
        assert "尚未发布主键发布快照" in row["校验明细"]

    def test_late_review_not_in_old_snapshot(
        self, store: Store, dims: DimDefinitionService, attrs: DimAttrService,
    ) -> None:
        """客户别名审核结果迟到：不自动进入旧快照，可用列保持与已确认快照一致。"""
        dim = create_dimension(dims, "DIM_ALIAS", "customer_id INT")
        key_attr = create_attribute(attrs, "DIM_ALIAS", "customer_id", "INT")
        attrs.run_action(key_attr["id"], "确认属性")
        dims.run_action(dim["id"], "发布维度")

        alias = create_attribute(attrs, "DIM_ALIAS", "customer_alias", "VARCHAR(64)")
        attrs.run_action(alias["id"], "确认属性")

        rows, _ = attrs.list_entries()
        row = find_row(rows, alias["id"])
        assert row["审核迟到"] == "是"
        assert row["快照一致"] == "否"
        assert row["快照类型"] == "—"
        assert row["可用列文本"] == "customer_id"
        assert "重新发布" in row["校验明细"]

    def test_type_helpers_cover_decimal_and_varchar(self) -> None:
        """类型族判定覆盖带精度声明的小数型与字符型。"""
        assert pk_snapshot.type_family("DECIMAL(18,2)") == "DECIMAL"
        assert pk_snapshot.type_family("varchar(64)") == "STRING"
        assert pk_snapshot.type_family("BIGINT") == "INTEGER"
        parsed = pk_snapshot.parse_primary_key("customer_id DECIMAL(18,2)")
        assert parsed["column"] == "customer_id"
        assert parsed["type"] == "DECIMAL"
        assert parsed["family"] == "DECIMAL"
