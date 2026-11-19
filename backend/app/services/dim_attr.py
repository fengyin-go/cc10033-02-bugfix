"""维度属性服务：管理维度表的属性字段定义、衍生字段逻辑和历史追踪策略。

增强能力：
- 消费 dim.defined 通道消息，关联属性到对应维度
- 派生字段计算：基于已有字段生成衍生属性值
- 历史追踪策略校验：确保SCD类型2关联的属性开启历史追踪
- 读取主键发布快照判定客户编码等关联键是否可用，并说明读取的维度版本与键类型
- 属性下线时通知下游模块
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.pk_snapshot import build_attribute_view, latest_publish_snapshot
from app.store import store

VALID_TYPES = {"VARCHAR", "INT", "BIGINT", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN", "TEXT", "JSON"}
DERIVED_PREFIX = "_derived_"



class DimAttrService(BaseModuleService):
    MODULE = "dim_attr"
    ENTITY = "属性字段"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["属性名称", "所属维度", "数据类型", "字段来源", "是否派生", "历史追踪", "默认值"]
    REQUIRED_FIELDS = ["属性名称", "所属维度", "数据类型"]
    STATUS_ORDER = ["草稿", "已确认", "已下线"]
    ACTION_RULES = {"确认属性": "已确认", "下线属性": "已下线"}
    NEGATIVE_ACTIONS = ["下线属性"]
    # 动作前置状态：不满足时不允许执行，避免状态错乱
    ACTION_REQUIRED_STATUS = {"确认属性": {"草稿"}, "下线属性": {"已确认"}}
    # 列表额外呈现的只读列：全部来自所属维度最近一次已确认的主键发布快照
    DERIVED_FIELDS = [
        "维度版本", "键类型", "键类型族", "快照类型", "关联键可用",
        "可用列文本", "快照一致", "审核迟到", "校验明细",
    ]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度属性：消费 dim.defined 通道，关联维度定义，校验数据类型。"""
        # 类型声明归一为大写；不把认不出的类型悄悄改成 VARCHAR，
        # 否则登记时的类型冲突会被吞掉，主键发布校验就失去依据
        data_type = str(values.get("数据类型", "")).strip().upper() or "VARCHAR"
        values["数据类型"] = data_type

        # 消费维度定义通道，获取维度信息
        dim_code = str(values.get("所属维度", "")).strip()
        dim_msgs = store.channel.consume_latest("dim.defined", "dim_attr")
        if dim_msgs and dim_msgs["payload"].get("dim_code") == dim_code:
            # 维度已定义，关联属性
            values["维度关联"] = dim_msgs["payload"]["dim_name"]

        # 检测是否派生字段
        source_field = str(values.get("字段来源", ""))
        if DERIVED_PREFIX in source_field:
            values["是否派生"] = "是"

        # 历史追踪策略校验：SCD类型2关联的维度属性必须开启历史追踪
        dim_rows = store.rows("dim_definition")
        for dim in dim_rows:
            if dim.get("维度编码") == dim_code and "SCD类型2" in str(dim.get("SCD类型", "")):
                values["历史追踪"] = "是"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 读取主键发布快照：登记时即给出该维度当前的键类型与可用列判定
        snapshot = latest_publish_snapshot(store, dim_code)
        view = build_attribute_view(entry, snapshot, _dimension_row(dim_code))
        entry["维度版本"] = view["维度版本"]
        entry["键类型"] = view["键类型"]
        entry["可用列"] = view["可用列"]
        entry["关联键可用"] = view["关联键可用"]
        entry["校验明细"] = view["校验明细"]

        # 发布属性定义到数据通道
        store.channel.publish("attr.defined", {
            "attr_name": entry["属性名称"],
            "dim_code": dim_code,
            "data_type": data_type,
            "is_derived": entry.get("是否派生") == "是",
            "history_tracking": entry.get("历史追踪") == "是",
            "source_field": source_field,
            "entry_id": entry["id"],
        })

        # 注册属性产物
        store.artifacts.register(
            "dim_attr", "dimension_attribute",
            f"attr_{entry['id']}",
            {"attr_name": entry["属性名称"], "dim_code": dim_code, "data_type": data_type},
            source_modules=["dim_definition", "dim_attr"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        dim_code = entry.get("所属维度", "")
        snapshot = latest_publish_snapshot(store, dim_code)
        view = build_attribute_view(entry, snapshot, _dimension_row(dim_code))
        if action == "确认属性":
            if entry.get("status") == "已下线":
                return None, f"属性 {entry.get('属性名称')} 已下线，不能重新确认"
            # 审核结果落库：确认后才进入后续发布的已确认属性集合
            entry["status"] = "已确认"
            entry["pending"] = False
            entry["abnormal"] = False
            message = f"{self.ENTITY}已{action}"

            # 确认属性后，重置关联维度定义状态
            _validate_dim_attr_integrity(dim_code)

            # 通知SCD处理器属性已确认
            store.channel.publish("attr.confirmed", {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "data_type": entry["数据类型"],
                "is_derived": entry.get("是否派生") == "是",
                "history_tracking": entry.get("历史追踪") == "是",
                "default_value": entry.get("默认值"),
                "entry_id": entry["id"],
                "dim_version": view["维度版本"],
                "key_type": view["键类型"],
                "join_key_available": view["can_join_key"],
            })
        elif action == "下线属性":
            entry["status"] = "已下线"
            entry["pending"] = True
            entry["abnormal"] = True
            message = f"{self.ENTITY}已{action}"
            # 通知下游模块属性已下线
            store.channel.publish("attr.decommissioned", {
                "attr_name": entry["属性名称"],
                "dim_code": dim_code,
                "entry_id": entry["id"],
            })
        else:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        entry["维度版本"] = view["维度版本"]
        entry["键类型"] = view["键类型"]
        entry["关联键可用"] = view["关联键可用"]
        entry["可用列"] = view["可用列"]
        entry["校验明细"] = view["校验明细"]
        return entry, message

    def list_entries(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(*args, **kwargs)
        return [self._decorate(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        return self._decorate(row) if row is not None else None

    @staticmethod
    def _decorate(row: dict[str, Any]) -> dict[str, Any]:
        snapshot = latest_publish_snapshot(store, row.get("所属维度", ""))
        view = build_attribute_view(row, snapshot, _dimension_row(row.get("所属维度", "")))
        decorated = dict(row)
        decorated.update({
            "维度版本": view["维度版本"],
            "键类型": view["键类型"],
            "键类型族": view["键类型族"],
            "快照类型": view["快照类型"],
            "关联键可用": view["关联键可用"],
            "可用列": view["可用列"],
            "可用列文本": view["可用列文本"],
            "快照一致": view["快照一致"],
            "审核迟到": view["审核迟到"],
            "校验明细": view["校验明细"],
        })
        return decorated

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 按维度聚合属性
        attr_by_dim: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            dim = row.get("所属维度", "未关联")
            attr_by_dim.setdefault(dim, []).append(row.get("属性名称", ""))
        base["attributes_by_dimension"] = attr_by_dim

        # 获取维度定义消费统计
        consumed_dims = store.channel.consume("dim.defined", "dim_attr")
        base["consumed_dim_definitions"] = len(consumed_dims)

        # 派生字段统计
        derived_count = sum(1 for row in store.rows(self.MODULE) if row.get("是否派生") == "是")
        base["derived_attributes"] = derived_count
        return base


def _dimension_row(dim_code: Any) -> dict[str, Any] | None:
    code = str(dim_code or "").strip()
    if not code:
        return None
    for row in store.rows("dim_definition"):
        if row.get("维度编码") == code:
            return row
    return None


def _validate_dim_attr_integrity(dim_code: str) -> None:
    """校验维度属性完整性，确保SCD类型2关联的属性全部开启历史追踪。"""
    dim_rows = store.rows("dim_definition")
    dim_scd = None
    for d in dim_rows:
        if d.get("维度编码") == dim_code:
            dim_scd = d.get("SCD类型", "")
            break

    attr_rows = store.rows("dim_attr")
    for attr in attr_rows:
        if attr.get("所属维度") == dim_code:
            if "SCD类型2" in str(dim_scd or ""):
                attr["历史追踪"] = "是"
            elif "SCD类型0" in str(dim_scd or "") or "SCD类型1" in str(dim_scd or ""):
                attr["历史追踪"] = "否"

    if dim_scd:
        store.channel.publish("attr.integrity_check", {
            "dim_code": dim_code,
            "scd_type": dim_scd,
            "checked_attrs": sum(1 for a in attr_rows if a.get("所属维度") == dim_code),
        })