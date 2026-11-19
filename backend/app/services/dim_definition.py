"""维度定义服务：管理维度表定义、主键设计、SCD类型配置。

增强能力：
- 创建维度时通过 DataChannel 发布维度定义，供 dim_attr 和 scd_handler 消费
- 维度编码重复校验与标准化转换
- SCD类型验证与默认值设定
- 发布维度：执行主键发布校验并固化「主键发布快照」，供属性登记与补发复用
- 发布校验未通过时不改状态、不发布维度事件，避免状态卡在已建模却多出发布事件
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.services.pk_snapshot import (
    build_dimension_view,
    create_publish_snapshot,
    evaluate_publish,
    latest_publish_snapshot,
    PUBLISH_ACTIONS,
)
from app.store import store


class DimDefinitionService(BaseModuleService):
    MODULE = "dim_definition"
    ENTITY = "维度表"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["维度编码", "维度名称", "主键定义", "SCD类型", "粒度说明", "数据来源", "刷新频率"]
    REQUIRED_FIELDS = ["维度编码", "维度名称", "主键定义"]
    STATUS_ORDER = ["草稿", "已建模", "已发布", "已停用"]
    ACTION_RULES = {"提交建模": "已建模", "发布维度": "已发布", "停用维度": "已停用"}
    NEGATIVE_ACTIONS = ["停用维度"]
    # 动作前置状态：不满足时不允许执行，避免状态错乱
    ACTION_REQUIRED_STATUS = {
        "提交建模": {"草稿"},
        "发布维度": {"草稿", "已建模", "已发布"},
        "停用维度": {"已发布"},
    }
    # 列表额外呈现的只读列：键类型与发布校验来自主键发布快照
    DERIVED_FIELDS = [
        "键类型", "键类型族", "键类型说明", "维度版本", "主键发布快照",
        "键类型冲突", "发布校验", "发布校验通过", "可用列文本", "关联键属性",
    ]

    VALID_SCD_TYPES = {"SCD类型0", "SCD类型1", "SCD类型2", "SCD类型3", "SCD类型4", "SCD类型6"}
    CODE_PATTERN = re.compile(r'^DIM_[A-Z0-9_]+$')

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建维度定义，校验编码格式和SCD类型合法性，发布到数据通道。"""
        # 标准化处理
        dim_code = str(values.get("维度编码", "")).strip().upper()
        if dim_code and not self.CODE_PATTERN.match(dim_code):
            dim_code = "DIM_" + re.sub(r'[^A-Z0-9_]', '', dim_code.upper())
            values["维度编码"] = dim_code

        scd_type = str(values.get("SCD类型", "SCD类型2")).strip()
        if scd_type not in self.VALID_SCD_TYPES:
            scd_type = "SCD类型2"
            values["SCD类型"] = scd_type

        # 编码唯一性检查
        existing = store.rows(self.MODULE)
        for row in existing:
            if row.get("维度编码") == dim_code:
                return None, [f"维度编码 {dim_code} 已存在"]

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布维度定义到数据通道，让 dim_attr 和 scd_handler 消费
        store.channel.publish("dim.defined", {
            "dim_code": entry["维度编码"],
            "dim_name": entry["维度名称"],
            "scd_type": entry["SCD类型"],
            "primary_key": entry["主键定义"],
            "entry_id": entry["id"],
        })

        # 注册数据产物
        store.artifacts.register(
            "dim_definition", "dim_definition",
            f"dim_{entry['id']}",
            {"dim_code": entry["维度编码"], "dim_name": entry["维度名称"], "scd_type": entry["SCD类型"]},
            source_modules=["dim_definition"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES and action not in PUBLISH_ACTIONS:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action in PUBLISH_ACTIONS:
            return self._publish_dimension(entry, action)

        required = self.ACTION_REQUIRED_STATUS.get(action)
        if required and entry.get("status") not in required:
            return None, (
                f"{self.ENTITY}当前状态为 {entry.get('status')}，"
                f"不能执行「{action}」"
            )

        if action == "提交建模":
            entry["status"] = self.ACTION_RULES[action]
            entry["pending"] = True
            entry["abnormal"] = False
            store.channel.publish("dim.modeled", {
                "dim_code": entry["维度编码"],
                "primary_key": entry["主键定义"],
                "entry_id": entry["id"],
            })
            return entry, f"{self.ENTITY}已{action}"

        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "停用维度":
            store.channel.publish("dim.deactivated", {
                "dim_code": entry["维度编码"],
                "entry_id": entry["id"],
            })
            # 过期相关 topic 上未消费的消息
            store.channel.expire_topic("dim.defined")
        return entry, message

    def _publish_dimension(
        self, entry: dict[str, Any], action: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """发布/补发维度：先落库校验结果，通过后才改状态并固化主键发布快照。"""
        dim_code = str(entry.get("维度编码", ""))
        existing = latest_publish_snapshot(store, dim_code)
        # 已发布维度再次发布即补发：沿用原版本线，不重复投递维度发布事件
        is_republish = entry.get("status") == "已发布" or existing is not None
        prefix = "补发" if is_republish else "发布"

        if entry.get("status") == "已停用":
            message = f"维度 {dim_code} 已停用，不能发布"
            entry["发布校验"] = message
            entry["发布校验结果"] = {
                "ok": False, "checked_at": None, "message": message,
                "键类型冲突": False, "issues": [{"code": "dim_deactivated", "message": message}],
                "dim_code": dim_code, "entry_id": entry.get("id"),
                "维度版本": existing.get("data", {}).get("维度版本", "") if existing else "",
            }
            return None, message

        attributes = [
            attr for attr in store.rows("dim_attr")
            if attr.get("所属维度") == dim_code
        ]
        evaluation = evaluate_publish(entry, attributes)

        # 校验结果先落库：无论通过与否，列表读取的都是这次动作的真实判定
        entry["发布校验"] = evaluation["message"]
        entry["发布校验结果"] = evaluation
        entry["发布校验时间"] = evaluation["checked_at"]
        entry["pending"] = not evaluation["ok"]

        if not evaluation["ok"]:
            # 状态保持不动，也不向通道发布维度发布事件
            entry["abnormal"] = True
            if existing:
                evaluation["保留快照"] = existing.get("snap_id", "")
                evaluation["维度版本"] = existing.get("data", {}).get("维度版本", "")
                evaluation["可用列"] = list(existing.get("data", {}).get("可用列", []))
                entry["可用列"] = evaluation["可用列"]
                entry["维度版本"] = evaluation["维度版本"]
            return None, evaluation["message"] + (
                f"；{prefix}未执行，现有已确认主键发布快照保持不变，"
                "已发布属性仍按原主键类型可用"
                if existing else f"；{prefix}未执行"
            )

        snap_id, version, snapshot_data = create_publish_snapshot(
            store, entry, attributes, evaluation)

        if not is_republish:
            entry["status"] = "已发布"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["主键发布快照"] = snap_id
        entry["维度版本"] = version
        entry["可用列"] = snapshot_data["可用列"]
        evaluation["快照ID"] = snap_id
        evaluation["维度版本"] = version

        if not is_republish:
            # 发布维度时，通知下游模块处理维度发布事件
            store.channel.publish("dim.published", {
                "dim_code": entry["维度编码"],
                "dim_name": entry["维度名称"],
                "scd_type": entry["SCD类型"],
                "primary_key": entry["主键定义"],
                "primary_key_type": snapshot_data["primary_key_type"],
                "primary_key_family": snapshot_data["primary_key_family"],
                "snapshot_id": snap_id,
                "snapshot_version": version,
                "granularity": entry["粒度说明"],
                "source": entry["数据来源"],
                "refresh_frequency": entry["刷新频率"],
                "entry_id": entry["id"],
            })
            # 为维度属性建立关联消费标记
            _auto_associate_attributes(entry["维度编码"], entry["id"])
            # 为SCD策略建立自动适配
            _auto_create_scd_strategy(entry)
        else:
            # 补发：固化新快照并投递补发事件，供下游重新消费
            store.channel.publish("dim.republished", {
                "dim_code": entry["维度编码"],
                "snapshot_id": snap_id,
                "snapshot_version": version,
                "primary_key": entry["主键定义"],
                "primary_key_type": snapshot_data["primary_key_type"],
                "primary_key_family": snapshot_data["primary_key_family"],
                "entry_id": entry["id"],
            })
            _auto_associate_attributes(entry["维度编码"], entry["id"])

        return entry, (
            f"{self.ENTITY}已{prefix}：{version} 主键发布快照已固化"
            f"（{evaluation['主键规则']}），可用列 {len(snapshot_data['可用列'])} 个"
        )
    def list_entries(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        rows, total = super().list_entries(*args, **kwargs)
        return [self._decorate(row) for row in rows], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        return self._decorate(row) if row is not None else None

    @staticmethod
    def _decorate(row: dict[str, Any]) -> dict[str, Any]:
        snapshot = latest_publish_snapshot(store, row.get("维度编码", ""))
        decorated = dict(row)
        decorated.update(build_dimension_view(row, snapshot))
        return decorated

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 增强快照：附加已发布维度关联的属性信息
        published_dims = []
        for row in store.rows(self.MODULE):
            if row.get("status") == "已发布":
                published_dims.append({
                    "dim_code": row.get("维度编码"),
                    "dim_name": row.get("维度名称"),
                    "scd_type": row.get("SCD类型"),
                })
        base["published_dims"] = published_dims
        base["channel_stats"] = store.channel.topic_stats().get("dim.defined", {})
        base["publish_snapshots"] = sum(
            1 for snap in store.snapshot.list_module_snapshots("dim_definition")
            if snap.get("data", {}).get("发布校验") == "通过"
        )
        return base


def _auto_associate_attributes(dim_code: str, dim_entry_id: int) -> None:
    """自动关联维度属性：为已存在的属性匹配到新发布的维度。"""
    attr_rows = store.rows("dim_attr")
    for attr in attr_rows:
        if attr.get("所属维度") == dim_code:
            # 发送属性关联事件
            store.channel.publish("attr.linked", {
                "dim_code": dim_code,
                "attr_name": attr.get("属性名称"),
                "data_type": attr.get("数据类型"),
                "attr_id": attr.get("id"),
                "source_dim_id": dim_entry_id,
            })


def _auto_create_scd_strategy(dim_def: dict[str, Any]) -> None:
    """根据维度定义的SCD类型自动生成SCD处理策略建议。"""
    scd_type = dim_def.get("SCD类型", "SCD类型2")
    dim_code = dim_def.get("维度编码", "")
    strategy_name = f"{dim_code}_{scd_type.replace('类型', '')}_auto"

    # 检查是否已有同名策略
    existing = store.rows("scd_handler")
    for row in existing:
        if row.get("策略名称") == strategy_name:
            return

    # 发布SCD策略建议到通道
    store.channel.publish("scd.strategy_suggested", {
        "dim_code": dim_code,
        "scd_type": scd_type,
        "strategy_name": strategy_name,
        "dim_name": dim_def.get("维度名称"),
        "primary_key": dim_def.get("主键定义"),
        "source_dim_id": dim_def.get("id"),
    })