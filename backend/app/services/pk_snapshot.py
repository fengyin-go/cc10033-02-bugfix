"""主键发布快照与键类型判定规则。

维度发布时把「维度编码 + 属性字段 + 主键类型」固化成一份主键发布快照：
- 维度定义列表读取快照展示键类型、发布校验与键类型冲突；
- 维度属性登记读取快照判断某个属性能否作为关联键；
- 补发失败时保留上一份已确认快照，已发布属性继续按原主键类型可用。

键类型规则（主键规则）：
1. 主键定义必须声明主键列与类型，缺失任一者不能发布；
2. 关联键属性（与主键列同名，或名称形如 xxx_id/xxx_key/xxx_code/xxx编码/xxx键）
   必须与主键类型同族：整数族、字符族、小数族、日期族、布尔族、二进制族、JSON 族；
3. 可用列与关联键属性以快照固化时的已确认属性为准，发布后迟到的审核结果
   不自动扩张当前快照，需重新发布后生效。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

FAMILY_LABELS = {
    "INTEGER": "整数型",
    "STRING": "字符型",
    "DECIMAL": "小数型",
    "DATE": "日期时间型",
    "BOOLEAN": "布尔型",
    "BINARY": "二进制型",
    "JSON": "JSON型",
}

_FAMILY_TYPES: dict[str, set[str]] = {
    "INTEGER": {
        "INT", "INTEGER", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT",
        "SERIAL", "BIGSERIAL", "INT2", "INT4", "INT8",
    },
    "STRING": {
        "VARCHAR", "VARCHAR2", "CHAR", "NCHAR", "NVARCHAR", "CHARACTER",
        "TEXT", "STRING", "CLOB", "BPCHAR", "LONGTEXT", "MEDIUMTEXT",
    },
    "DECIMAL": {"DECIMAL", "NUMERIC", "NUMBER", "FLOAT", "DOUBLE", "REAL", "MONEY"},
    "DATE": {"DATE", "DATETIME", "TIMESTAMP", "TIME", "TIMESTAMPTZ", "DATETIME2"},
    "BOOLEAN": {"BOOLEAN", "BOOL", "BIT"},
    "BINARY": {"BINARY", "VARBINARY", "BLOB", "BYTES", "BYTEA"},
    "JSON": {"JSON", "JSONB", "ARRAY", "STRUCT", "MAP"},
}

_TYPE_FAMILY = {
    type_name: family
    for family, type_names in _FAMILY_TYPES.items()
    for type_name in type_names
}

KEY_SUFFIXES = ("_id", "_key", "_code", "_sk", "_no")
KEY_MARKERS = ("编码", "编号", "代码", "键")

PUBLISH_ACTIONS = {"发布维度", "补发维度", "重新发布", "补发发布", "补发"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_type(raw: str | None) -> str:
    """把类型声明收敛成基础类型名，例如 VARCHAR(32) → VARCHAR。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(.*\)\s*$", "", text).strip()
    text = text.split(" ")[0].upper()
    for suffix in ("UNSIGNED", "SIGNED"):
        text = text.replace(suffix, "")
    return text.strip()


def type_family(raw: str | None) -> str:
    return _TYPE_FAMILY.get(normalize_type(raw), "")


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family, "未知类型")


def _split_columns(definition: str) -> list[str]:
    """按顶层逗号切分主键定义，保留 DECIMAL(18,2) 中的逗号。"""
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in definition:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    if "".join(buffer).strip():
        parts.append("".join(buffer))
    return parts


def parse_primary_key(definition: str | None) -> dict[str, Any]:
    """解析主键定义，返回主键列、类型、类型族及说明。"""
    raw = str(definition or "").strip()
    columns: list[dict[str, str]] = []
    for part in _split_columns(raw):
        part = part.strip()
        if not part:
            continue
        # 兼容 PRIMARY KEY (customer_id) 这类写法
        part = re.sub(r"^PRIMARY\s+KEY\s*", "", part, flags=re.IGNORECASE).strip()
        if part.startswith("(") and part.endswith(")"):
            part = part[1:-1].strip()
        if not part:
            continue
        match = re.match(r"^[`\"\[]?([^`\"\]\s]+)[`\"\]]?\s*(.*)$", part)
        if not match:
            continue
        column = match.group(1)
        type_raw = re.sub(r"\b(NOT\s+NULL|PRIMARY\s+KEY)\b", "", match.group(2),
                          flags=re.IGNORECASE).strip()
        if not column:
            continue
        columns.append({
            "column": column,
            "type_raw": type_raw,
            "type": normalize_type(type_raw),
            "family": type_family(type_raw),
        })

    first = columns[0] if columns else {
        "column": "", "type_raw": "", "type": "", "family": "",
    }
    return {
        "raw": raw,
        "column": first["column"],
        "type_raw": first["type_raw"],
        "type": first["type"],
        "family": first["family"],
        "columns": columns,
        "empty": not bool(raw) or not bool(first["column"]),
    }


def _normalize_name(name: Any) -> str:
    text = str(name or "").strip().lower()
    return re.sub(r"[\s`\"\[\]]", "", text)


def is_key_attribute(
    attribute_name: str,
    primary_key: dict[str, Any],
    explicit_flag: Any = None,
) -> bool:
    """判断属性是否属于关联键候选。"""
    if explicit_flag in (True, 1, "是", "true", "True", "Y"):
        return True
    name = _normalize_name(attribute_name)
    if not name:
        return False
    for column in primary_key.get("columns", []):
        if name == _normalize_name(column.get("column")):
            return True
    if any(name.endswith(suffix) for suffix in KEY_SUFFIXES):
        return True
    return any(marker in str(attribute_name) for marker in KEY_MARKERS)


def _matching_column(attribute_name: str, primary_key: dict[str, Any]) -> dict[str, str]:
    name = _normalize_name(attribute_name)
    for column in primary_key.get("columns", []):
        if name == _normalize_name(column.get("column")):
            return column
    return {
        "column": primary_key.get("column", ""),
        "type": primary_key.get("type", ""),
        "type_raw": primary_key.get("type_raw", ""),
        "family": primary_key.get("family", ""),
    }


def _primary_key_rule_text(primary_key: dict[str, Any]) -> str:
    column = primary_key.get("column") or "（未声明）"
    raw_type = primary_key.get("type_raw") or primary_key.get("type") or "（未声明）"
    return (
        f"主键列 {column} 声明为 {raw_type}"
        f"（键类型 {primary_key.get('type') or 'UNKNOWN'}，"
        f"{family_label(primary_key.get('family', ''))}）"
    )


def evaluate_publish(entry: dict[str, Any], attributes: list[dict[str, Any]]) -> dict[str, Any]:
    """执行发布校验：主键非空、主键类型可识别、已确认关联键属性与主键类型同族。

    只有「已确认」的属性才进入发布校验与快照；待审属性不会阻塞发布，
    审核迟到时也不会被算进当前快照。
    """
    primary_key = parse_primary_key(entry.get("主键定义"))
    issues: list[dict[str, Any]] = []
    key_type_conflict = False

    if primary_key["empty"]:
        issues.append({
            "code": "empty_primary_key",
            "message": "主键定义为空，缺失主键列；按主键规则，发布维度必须声明主键列及其类型",
        })
    elif not primary_key.get("family"):
        issues.append({
            "code": "unknown_primary_key_type",
            "message": (
                f"主键列 {primary_key.get('column')} 缺少可识别的类型声明"
                f"（{primary_key.get('type_raw') or '空'}），无法确定键类型；"
                "按主键规则，发布维度必须声明主键类型"
            ),
        })

    active_attributes = [
        attr for attr in attributes
        if str(attr.get("status", "")) != "已下线"
    ]
    confirmed_attributes = [
        attr for attr in active_attributes
        if str(attr.get("status", "")) == "已确认"
    ]
    key_attributes: list[str] = []
    compatible_key_attributes: list[str] = []
    attribute_records: list[dict[str, Any]] = []

    for attr in active_attributes:
        name = str(attr.get("属性名称", "")).strip()
        attr_type_raw = str(attr.get("数据类型", "")).strip()
        attr_family = type_family(attr_type_raw)
        status = str(attr.get("status", ""))
        explicit = attr.get("关联键", attr.get("是否关联键"))
        is_key = is_key_attribute(name, primary_key, explicit)
        if is_key:
            key_attributes.append(name)

        record = {
            "属性名称": name,
            "数据类型": attr_type_raw,
            "类型族": attr_family,
            "状态": status,
            "是否关联键": is_key,
            "entry_id": attr.get("id"),
        }
        attribute_records.append(record)

        # 只有已确认的属性参与主键类型校验
        if status != "已确认" or not is_key or primary_key["empty"] or not primary_key.get("family"):
            continue

        matched = _matching_column(name, primary_key)
        target_family = matched.get("family") or primary_key.get("family", "")
        record["主键类型族"] = target_family
        if not attr_family:
            issues.append({
                "code": "missing_attribute_type",
                "attribute": name,
                "message": (
                    f"关联键属性 {name} 未登记可识别的数据类型；"
                    f"按主键规则，关联键必须与主键 {target_family or '类型'} 同族"
                ),
            })
            continue
        if attr_family != target_family:
            key_type_conflict = True
            issues.append({
                "code": "key_type_conflict",
                "attribute": name,
                "attribute_type": attr_type_raw,
                "attribute_family": attr_family,
                "primary_key_column": matched.get("column"),
                "primary_key_type": matched.get("type"),
                "primary_key_family": target_family,
                "message": (
                    f"关联键属性 {name} 登记类型 {attr_type_raw}"
                    f"（{family_label(attr_family)}）与主键 {matched.get('column')} "
                    f"{matched.get('type') or ''}（{family_label(target_family)}）相斥；"
                    "按主键规则，关联键必须与主键同族"
                ),
            })
        else:
            compatible_key_attributes.append(name)

    confirmed_names = [
        str(attr.get("属性名称", "")).strip() for attr in confirmed_attributes
    ]
    available_columns: list[str] = []
    if primary_key.get("column"):
        available_columns.append(primary_key["column"])
    for name in confirmed_names:
        if name and name not in available_columns:
            available_columns.append(name)

    ok = not issues
    if ok:
        message = (
            f"发布校验通过：{_primary_key_rule_text(primary_key)}；"
            f"可用列 {len(available_columns)} 个，关联键属性 {len(compatible_key_attributes)} 个"
        )
    else:
        seen: set[tuple[str, str]] = set()
        parts: list[str] = []
        for issue in issues:
            signature = (issue.get("code", ""), str(issue.get("attribute", "")))
            if signature in seen:
                continue
            seen.add(signature)
            parts.append(issue["message"].rstrip("。"))
        message = "发布校验未通过：" + "；".join(parts) + "，维度不能进入已发布"

    return {
        "ok": ok,
        "checked_at": _utc_now(),
        "dim_code": str(entry.get("维度编码", "")),
        "entry_id": entry.get("id"),
        "主键定义": primary_key.get("raw", ""),
        "主键列": primary_key.get("column", ""),
        "主键类型": primary_key.get("type", ""),
        "主键类型原文": primary_key.get("type_raw", ""),
        "主键类型族": primary_key.get("family", ""),
        "主键规则": _primary_key_rule_text(primary_key),
        "键类型冲突": key_type_conflict,
        "issues": issues,
        "message": message,
        "attributes": attribute_records,
        "关联键属性": compatible_key_attributes,
        "可用列": available_columns,
        "primary_key": primary_key,
    }


def _snapshot_count(store: Any, dim_code: str) -> int:
    snapshots = _module_snapshots(store)
    return sum(
        1 for snap in snapshots
        if snap.get("data", {}).get("维度编码") == dim_code
        and snap.get("data", {}).get("发布校验") == "通过"
    )


def create_publish_snapshot(
    store: Any,
    entry: dict[str, Any],
    attributes: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """固化主键发布快照，返回快照ID、版本号与快照内容。

    快照只收录「已确认」的属性：属性字段、属性类型与可用列都对应发布时的
    已确认集合；之后才通过审核的属性不会自动出现在旧快照里。
    """
    primary_key = evaluation["primary_key"]
    version = f"v{_snapshot_count(store, evaluation['dim_code']) + 1}"

    confirmed_attributes = [
        attr for attr in attributes
        if str(attr.get("status", "")) == "已确认"
    ]
    attribute_fields = [
        str(attr.get("属性名称", "")).strip() for attr in confirmed_attributes
    ]
    attribute_types = {
        str(attr.get("属性名称", "")).strip(): str(attr.get("数据类型", "")).strip()
        for attr in confirmed_attributes
    }
    attribute_status = {
        str(attr.get("属性名称", "")).strip(): "已确认"
        for attr in confirmed_attributes
    }

    data = {
        "维度编码": evaluation["dim_code"],
        "维度名称": entry.get("维度名称", ""),
        "维度版本": version,
        "主键定义": primary_key.get("raw", ""),
        "主键列": primary_key.get("column", ""),
        "主键类型": primary_key.get("type", ""),
        "主键类型原文": primary_key.get("type_raw", ""),
        "主键类型族": primary_key.get("family", ""),
        "主键类型说明": evaluation["主键规则"],
        "属性字段": attribute_fields,
        "属性类型": attribute_types,
        "属性状态": attribute_status,
        "已确认属性": list(attribute_fields),
        "可用列": list(evaluation["可用列"]),
        "关联键属性": list(evaluation["关联键属性"]),
        "发布校验": "通过",
        "发布校验结果": {
            "ok": True,
            "checked_at": evaluation["checked_at"],
            "message": evaluation["message"],
        },
        "固化时间": _utc_now(),
        # 英文别名，方便其它模块按字段名读取
        "dim_code": evaluation["dim_code"],
        "attribute_fields": attribute_fields,
        "attribute_types": attribute_types,
        "primary_key_type": primary_key.get("type", ""),
        "primary_key_family": primary_key.get("family", ""),
        "version": version,
    }

    snap_id = store.snapshot.create_version_snapshot(
        "dim_definition", int(entry.get("id") or 0), data, version,
    )
    return snap_id, version, data


def _module_snapshots(store: Any) -> list[dict[str, Any]]:
    engine = store.snapshot
    if hasattr(engine, "list_module_snapshots"):
        return engine.list_module_snapshots("dim_definition")
    snapshots = getattr(engine, "_snapshots", {})
    return [snap for snap in snapshots.values() if snap.get("module") == "dim_definition"]


def latest_publish_snapshot(store: Any, dim_code: str) -> dict[str, Any] | None:
    """读取维度最近一次已确认的主键发布快照（补发失败不会覆盖它）。"""
    code = str(dim_code or "").strip()
    if not code:
        return None
    matches = [
        snap for snap in _module_snapshots(store)
        if snap.get("data", {}).get("维度编码") == code
        and snap.get("data", {}).get("发布校验") == "通过"
    ]
    return matches[-1] if matches else None


def _joined_columns(columns: list[str]) -> str:
    return "、".join(columns) if columns else "—"


def build_dimension_view(row: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """维度定义列表呈现：键类型、版本、键类型冲突与发布校验结果。"""
    primary_key = parse_primary_key(row.get("主键定义"))
    snapshot_data = snapshot.get("data", {}) if snapshot else {}
    result = row.get("发布校验结果") or {}
    conflict = result.get("键类型冲突")
    if conflict is None:
        conflict_text = "—"
    else:
        conflict_text = "是" if conflict else "否"

    version = ""
    if snapshot:
        version = snapshot_data.get("维度版本") or snapshot.get("version_label", "")
    key_type = snapshot_data.get("主键类型") or primary_key.get("type", "")
    key_family = snapshot_data.get("主键类型族") or primary_key.get("family", "")
    available = list(snapshot_data.get("可用列", [])) if snapshot else []

    return {
        "键类型": key_type,
        "键类型族": key_family,
        "键类型说明": family_label(key_family) if key_family else "未知类型",
        "维度版本": version,
        "主键发布快照": snapshot.get("snap_id", "") if snapshot else "",
        "键类型冲突": conflict_text,
        "发布校验": result.get("message") or row.get("发布校验") or "未校验",
        "发布校验通过": "是" if result.get("ok") else ("否" if result else "—"),
        "可用列": available,
        "可用列文本": _joined_columns(available),
        "关联键属性": list(snapshot_data.get("关联键属性", [])) if snapshot else list(
            result.get("关联键属性", [])
        ),
    }


def build_attribute_view(
    row: dict[str, Any],
    snapshot: dict[str, Any] | None,
    dimension: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """维度属性登记呈现：按已确认的主键发布快照判定类型与可用列。

    dimension 用于尚未发布成功时，把最近一次发布校验的失败原因一并写进明细，
    让属性登记入口能直接看到键类型冲突的判定依据。
    """
    dim_code = str(row.get("所属维度", "")).strip()
    name = str(row.get("属性名称", "")).strip()
    current_type = str(row.get("数据类型", "")).strip()
    status = str(row.get("status", ""))
    current_family = type_family(current_type)

    if snapshot is None:
        result = (dimension or {}).get("发布校验结果") or {}
        key_type = result.get("主键类型", "")
        key_family = result.get("主键类型族", "")
        if result and not result.get("ok"):
            issues = result.get("issues", [])
            reason = "；".join(
                str(issue.get("message", "")).rstrip("。") for issue in issues
            ) or str(result.get("message", "")).rstrip("。")
            detail = (
                f"维度 {dim_code or '（未填写）'} 尚未生成已确认的主键发布快照；"
                f"最近一次发布校验未通过（主键 {result.get('主键列', '')} "
                f"{result.get('主键类型') or '未声明'}，键类型 "
                f"{key_type or 'UNKNOWN'}/{family_label(key_family)}）：{reason}。"
                f"{name or '该属性'} 暂按待定处理，可用列为空。"
            )
        else:
            detail = (
                f"维度 {dim_code or '（未填写）'} 尚未发布主键发布快照，"
                f"无法判定 {name or '该属性'} 能否作为关联键。"
            )
        return {
            "维度版本": "",
            "键类型": key_type,
            "键类型族": key_family,
            "快照类型": "",
            "关联键可用": "待定",
            "can_join_key": False,
            "可用列": [],
            "可用列文本": "—",
            "快照一致": "—",
            "审核迟到": "否",
            "校验明细": detail,
        }

    data = snapshot.get("data", {})
    version = data.get("维度版本") or snapshot.get("version_label", "")
    key_type = data.get("主键类型", "")
    key_family = data.get("主键类型族", "")
    pk_column = data.get("主键列", "")
    pk_type_raw = data.get("主键类型原文") or key_type
    attribute_types: dict[str, str] = data.get("属性类型", {})
    available = list(data.get("可用列", []))
    key_attributes = list(data.get("关联键属性", []))

    in_snapshot = name in attribute_types
    snapshot_type = attribute_types.get(name, "")
    consistent = bool(in_snapshot) and (
        normalize_type(snapshot_type) == normalize_type(current_type)
    )

    can_join = False
    late = status == "已确认" and not in_snapshot
    if late:
        # 审核审批结果比发布动作迟到：当前已确认快照没有该列
        can_join = False
        judgement = "审核结果晚于主键发布快照，未包含在当前快照中，需重新发布后才能纳入可用列"
    elif status == "已下线":
        judgement = "属性已下线，不进入可用列"
    elif in_snapshot and name in available:
        if name in key_attributes:
            can_join = True
            judgement = "与主键同族，可作为关联键"
        elif current_family and current_family == key_family:
            judgement = "类型与主键同族，但不是关联键属性，仅作为可用列"
        else:
            judgement = "不是关联键属性，仅作为可用列"
    elif in_snapshot:
        judgement = "已进入快照但不在可用列中"
    else:
        judgement = "属性待审，未包含在当前快照中，可用列以已确认快照为准"

    detail = (
        f"读取维度 {dim_code} 主键发布快照 {version}"
        f"（主键列 {pk_column} 声明为 {pk_type_raw}，"
        f"键类型 {key_type or 'UNKNOWN'}/{family_label(key_family)}）；"
        f"按主键规则：关联键必须与主键同族。"
        f"属性 {name} 登记类型 {current_type or '未登记'}"
        f"（{family_label(current_family) if current_family else '类型未知'}），"
        f"{judgement}。"
    )

    can_join_text = (
        "是" if can_join
        else "待定" if late or status == "草稿"
        else "否"
    )
    return {
        "维度版本": version,
        "键类型": key_type,
        "键类型族": key_family,
        "快照类型": snapshot_type or "—",
        "关联键可用": can_join_text,
        "can_join_key": can_join,
        "可用列": available,
        "可用列文本": _joined_columns(available),
        "快照一致": "是" if consistent else "否",
        "审核迟到": "是" if late else "否",
        "校验明细": detail,
    }
