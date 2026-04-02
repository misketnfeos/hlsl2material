"""
============================================================
 t3d_generator.py
 UE4 材质节点 T3D 剪贴板文本生成器
============================================================

从 T3D 解析结果或内部数据结构，生成可以直接粘贴到
UE4 材质编辑器的 T3D 格式文本。

用法：
  1. 从 T3D 解析结果生成（原样导出）：
     text = generate_t3d_from_parse_result(parse_result)
     
  2. 从 HLSL 转换结果生成 T3D：
     text = generate_t3d_from_material_graph(graph)
============================================================
"""

import uuid
import re
from typing import Dict, List, Optional, Tuple
from t3d_parser import T3DParseResult, T3DGraphNode, T3DPin, T3DInnerObject


# ═══════════════════════════════════════════════════════════
# 值格式化工具
# ═══════════════════════════════════════════════════════════

def _fmt_float(value) -> str:
    """将数值格式化为 T3D 浮点数格式"""
    if isinstance(value, str):
        return value
    try:
        return f'{float(value):.6f}'
    except (ValueError, TypeError):
        return str(value)


def _fmt_color(value) -> str:
    """将颜色值格式化为 T3D 颜色格式 (R=0.000000,G=0.000000,B=0.000000,A=1.000000)
    
    支持:
    - dict: {'R': 0, 'G': 0, 'B': 0, 'A': 1}
    - str: 原样返回（已经是 T3D 格式）
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        r = _fmt_float(value.get('R', 0))
        g = _fmt_float(value.get('G', 0))
        b = _fmt_float(value.get('B', 0))
        a = _fmt_float(value.get('A', 0))
        return f'(R={r},G={g},B={b},A={a})'
    return str(value)


# ═══════════════════════════════════════════════════════════
# UUID 生成工具
# ═══════════════════════════════════════════════════════════

# 使用固定命名空间确保相同输入生成相同 UUID
_NAMESPACE = uuid.uuid1()


def _make_uuid(name: str) -> str:
    """生成确定性 UUID（UE4 格式：32 位大写十六进制，无连字符）"""
    u = uuid.uuid3(_NAMESPACE, name)
    return u.hex.upper()


def _make_guid(name: str) -> str:
    """生成 FGuid 格式的确定性 GUID"""
    return _make_uuid(name)


# ═══════════════════════════════════════════════════════════
# 从 T3D 解析结果生成（保留原始格式）
# ═══════════════════════════════════════════════════════════

def generate_t3d_from_parse_result(
    result: T3DParseResult,
    offset_x: int = 0,
    offset_y: int = 0,
) -> str:
    """从 T3D 解析结果重新生成 T3D 文本
    
    这主要用于"原样导出"场景，保留所有原始属性。
    支持对所有节点施加位置偏移。
    """
    lines = []
    
    for node in result.nodes:
        lines.append(_gen_node_t3d(node, offset_x, offset_y))
    
    return '\r\n'.join(lines)


def _gen_node_t3d(node: T3DGraphNode, offset_x: int = 0, offset_y: int = 0) -> str:
    """生成单个节点的 T3D 文本"""
    parts = []
    
    # 1. Begin Object - 外层 MaterialGraphNode
    begin_line = f'Begin Object'
    if node.class_path:
        begin_line += f' Class={node.class_path}'
    begin_line += f' Name="{node.name}"'
    if node.archetype:
        begin_line += f' Archetype={node.archetype}'
    parts.append(begin_line)
    
    # 2. 嵌套的 MaterialExpression
    if node.material_expression:
        me = node.material_expression
        
        # 声明块（有 Class）
        inner_begin = f'   Begin Object'
        if me.class_path:
            inner_begin += f' Class={me.class_path}'
        inner_begin += f' Name="{me.name}"'
        if me.archetype:
            inner_begin += f' Archetype={me.archetype}'
        parts.append(inner_begin)
        parts.append('   End Object')
        
        # 定义块（有属性）
        parts.append(f'   Begin Object Name="{me.name}"')
        for key, value in me.properties.items():
            # 处理位置偏移
            if key == 'MaterialExpressionEditorX':
                try:
                    value = str(int(value) + offset_x)
                except ValueError:
                    pass
            elif key == 'MaterialExpressionEditorY':
                try:
                    value = str(int(value) + offset_y)
                except ValueError:
                    pass
            parts.append(f'      {key}={value}')
        parts.append('   End Object')
    
    # 3. MaterialExpression 引用
    if node.material_expression_ref:
        parts.append(f'   MaterialExpression={node.material_expression_ref}')
    elif node.material_expression:
        me = node.material_expression
        parts.append(f'   MaterialExpression={me.class_name}\'"{me.name}"\'')
    
    # 4. 位置
    parts.append(f'   NodePosX={node.node_pos_x + offset_x}')
    parts.append(f'   NodePosY={node.node_pos_y + offset_y}')
    
    # 5. NodeGuid
    if node.node_guid:
        parts.append(f'   NodeGuid={node.node_guid}')
    
    # 6. 其他属性
    for key, value in node.properties.items():
        if key not in ('NodePosX', 'NodePosY', 'NodeGuid', 'MaterialExpression'):
            parts.append(f'   {key}={value}')
    
    # 7. CustomProperties Pin
    for pin in node.pins:
        pin_str = _gen_pin_t3d(pin)
        parts.append(f'   CustomProperties Pin ({pin_str})')
    
    # 8. End Object
    parts.append('End Object')
    
    return '\r\n'.join(parts)


def _gen_pin_t3d(pin: T3DPin) -> str:
    """生成单个 Pin 的 T3D 属性字符串"""
    props = []
    
    # PinId（必须）
    props.append(f'PinId={pin.pin_id}')
    
    # PinName
    if pin.pin_name:
        props.append(f'PinName="{pin.pin_name}"')
    
    # PinFriendlyName
    if pin.pin_friendly_name:
        props.append(f'PinFriendlyName="{pin.pin_friendly_name}"')
    
    # Direction（只在非默认时输出）
    if pin.direction == 'EGPD_Output':
        props.append(f'Direction="EGPD_Output"')
    
    # PinType 子属性
    if pin.pin_type_category:
        props.append(f'PinType.PinCategory="{pin.pin_type_category}"')
    else:
        props.append(f'PinType.PinCategory=""')
    
    # 从 raw_props 中输出 PinType 的其他属性
    for key in sorted(pin.raw_props.keys()):
        if key.startswith('PinType.'):
            props.append(f'{key}={pin.raw_props[key]}')
    
    # DefaultValue
    if pin.default_value:
        props.append(f'DefaultValue="{pin.default_value}"')
    
    # AutogeneratedDefaultValue
    if pin.autogenerated_default_value:
        props.append(f'AutogeneratedDefaultValue="{pin.autogenerated_default_value}"')
    
    # LinkedTo
    if pin.linked_to:
        links = ','.join(f'{node} {pid}' for node, pid in pin.linked_to)
        props.append(f'LinkedTo=({links},)')
    
    # PersistentGuid
    props.append(f'PersistentGuid={pin.persistent_guid}')
    
    # Boolean 属性
    props.append(f'bHidden={"True" if pin.is_hidden else "False"}')
    props.append(f'bNotConnectable={"True" if pin.is_not_connectable else "False"}')
    
    # 其他 raw boolean props
    for key in ('bDefaultValueIsReadOnly', 'bDefaultValueIsIgnored', 'bAdvancedView', 'bOrphanedPin'):
        if key in pin.raw_props:
            props.append(f'{key}={pin.raw_props[key]}')
        else:
            props.append(f'{key}=False')
    
    # 其他未识别的 raw 属性
    skip_keys = {'PinType.PinCategory', 'bDefaultValueIsReadOnly', 'bDefaultValueIsIgnored',
                 'bAdvancedView', 'bOrphanedPin'}
    for key, value in pin.raw_props.items():
        if key.startswith('PinType.') or key in skip_keys:
            continue
        props.append(f'{key}={value}')
    
    return ','.join(props)


# ═══════════════════════════════════════════════════════════
# 从 MaterialGraph（HLSL 转换结果）生成 T3D
# ═══════════════════════════════════════════════════════════

# UE 类名 → 完整类路径
_CLASS_PATHS = {
    'MaterialExpressionAdd': '/Script/Engine.MaterialExpressionAdd',
    'MaterialExpressionSubtract': '/Script/Engine.MaterialExpressionSubtract',
    'MaterialExpressionMultiply': '/Script/Engine.MaterialExpressionMultiply',
    'MaterialExpressionDivide': '/Script/Engine.MaterialExpressionDivide',
    'MaterialExpressionPower': '/Script/Engine.MaterialExpressionPower',
    'MaterialExpressionAbs': '/Script/Engine.MaterialExpressionAbs',
    'MaterialExpressionCeil': '/Script/Engine.MaterialExpressionCeil',
    'MaterialExpressionFloor': '/Script/Engine.MaterialExpressionFloor',
    'MaterialExpressionFrac': '/Script/Engine.MaterialExpressionFrac',
    'MaterialExpressionFmod': '/Script/Engine.MaterialExpressionFmod',
    'MaterialExpressionMin': '/Script/Engine.MaterialExpressionMin',
    'MaterialExpressionMax': '/Script/Engine.MaterialExpressionMax',
    'MaterialExpressionClamp': '/Script/Engine.MaterialExpressionClamp',
    'MaterialExpressionSaturate': '/Script/Engine.MaterialExpressionSaturate',
    'MaterialExpressionOneMinus': '/Script/Engine.MaterialExpressionOneMinus',
    'MaterialExpressionSign': '/Script/Engine.MaterialExpressionSign',
    'MaterialExpressionTruncate': '/Script/Engine.MaterialExpressionTruncate',
    'MaterialExpressionRound': '/Script/Engine.MaterialExpressionRound',
    'MaterialExpressionSquareRoot': '/Script/Engine.MaterialExpressionSquareRoot',
    'MaterialExpressionSine': '/Script/Engine.MaterialExpressionSine',
    'MaterialExpressionCosine': '/Script/Engine.MaterialExpressionCosine',
    'MaterialExpressionTangent': '/Script/Engine.MaterialExpressionTangent',
    'MaterialExpressionArcsine': '/Script/Engine.MaterialExpressionArcsine',
    'MaterialExpressionArcsineFast': '/Script/Engine.MaterialExpressionArcsineFast',
    'MaterialExpressionArccosine': '/Script/Engine.MaterialExpressionArccosine',
    'MaterialExpressionArccosineFast': '/Script/Engine.MaterialExpressionArccosineFast',
    'MaterialExpressionArctangent': '/Script/Engine.MaterialExpressionArctangent',
    'MaterialExpressionArctangentFast': '/Script/Engine.MaterialExpressionArctangentFast',
    'MaterialExpressionArctangent2': '/Script/Engine.MaterialExpressionArctangent2',
    'MaterialExpressionArctangent2Fast': '/Script/Engine.MaterialExpressionArctangent2Fast',
    'MaterialExpressionDotProduct': '/Script/Engine.MaterialExpressionDotProduct',
    'MaterialExpressionCrossProduct': '/Script/Engine.MaterialExpressionCrossProduct',
    'MaterialExpressionDistance': '/Script/Engine.MaterialExpressionDistance',
    'MaterialExpressionNormalize': '/Script/Engine.MaterialExpressionNormalize',
    'MaterialExpressionLinearInterpolate': '/Script/Engine.MaterialExpressionLinearInterpolate',
    'MaterialExpressionIf': '/Script/Engine.MaterialExpressionIf',
    'MaterialExpressionConstant': '/Script/Engine.MaterialExpressionConstant',
    'MaterialExpressionConstant2Vector': '/Script/Engine.MaterialExpressionConstant2Vector',
    'MaterialExpressionConstant3Vector': '/Script/Engine.MaterialExpressionConstant3Vector',
    'MaterialExpressionConstant4Vector': '/Script/Engine.MaterialExpressionConstant4Vector',
    'MaterialExpressionAppendVector': '/Script/Engine.MaterialExpressionAppendVector',
    'MaterialExpressionComponentMask': '/Script/Engine.MaterialExpressionComponentMask',
    'MaterialExpressionStaticSwitch': '/Script/Engine.MaterialExpressionStaticSwitch',
    'MaterialExpressionStaticBool': '/Script/Engine.MaterialExpressionStaticBool',
    'MaterialExpressionScalarParameter': '/Script/Engine.MaterialExpressionScalarParameter',
    'MaterialExpressionVectorParameter': '/Script/Engine.MaterialExpressionVectorParameter',
    'MaterialExpressionStaticBoolParameter': '/Script/Engine.MaterialExpressionStaticBoolParameter',
    'MaterialExpressionStaticSwitchParameter': '/Script/Engine.MaterialExpressionStaticSwitchParameter',
    'MaterialExpressionTextureSample': '/Script/Engine.MaterialExpressionTextureSample',
    'MaterialExpressionTextureSampleParameter2D': '/Script/Engine.MaterialExpressionTextureSampleParameter2D',
    'MaterialExpressionTextureCoordinate': '/Script/Engine.MaterialExpressionTextureCoordinate',
    'MaterialExpressionTextureObject': '/Script/Engine.MaterialExpressionTextureObject',
    'MaterialExpressionTextureObjectParameter': '/Script/Engine.MaterialExpressionTextureObjectParameter',
    'MaterialExpressionTime': '/Script/Engine.MaterialExpressionTime',
    'MaterialExpressionCameraPositionWS': '/Script/Engine.MaterialExpressionCameraPositionWS',
    'MaterialExpressionCameraVectorWS': '/Script/Engine.MaterialExpressionCameraVectorWS',
    'MaterialExpressionWorldPosition': '/Script/Engine.MaterialExpressionWorldPosition',
    'MaterialExpressionObjectPositionWS': '/Script/Engine.MaterialExpressionObjectPositionWS',
    'MaterialExpressionPixelNormalWS': '/Script/Engine.MaterialExpressionPixelNormalWS',
    'MaterialExpressionVertexNormalWS': '/Script/Engine.MaterialExpressionVertexNormalWS',
    'MaterialExpressionVertexTangentWS': '/Script/Engine.MaterialExpressionVertexTangentWS',
    'MaterialExpressionVertexColor': '/Script/Engine.MaterialExpressionVertexColor',
    'MaterialExpressionScreenPosition': '/Script/Engine.MaterialExpressionScreenPosition',
    'MaterialExpressionPixelDepth': '/Script/Engine.MaterialExpressionPixelDepth',
    'MaterialExpressionSceneDepth': '/Script/Engine.MaterialExpressionSceneDepth',
    'MaterialExpressionReflectionVectorWS': '/Script/Engine.MaterialExpressionReflectionVectorWS',
    'MaterialExpressionPanner': '/Script/Engine.MaterialExpressionPanner',
    'MaterialExpressionRotator': '/Script/Engine.MaterialExpressionRotator',
    'MaterialExpressionTransform': '/Script/Engine.MaterialExpressionTransform',
    'MaterialExpressionTransformPosition': '/Script/Engine.MaterialExpressionTransformPosition',
    'MaterialExpressionCustom': '/Script/Engine.MaterialExpressionCustom',
    'MaterialExpressionFunctionInput': '/Script/Engine.MaterialExpressionFunctionInput',
    'MaterialExpressionFunctionOutput': '/Script/Engine.MaterialExpressionFunctionOutput',
    'MaterialExpressionMaterialFunctionCall': '/Script/Engine.MaterialExpressionMaterialFunctionCall',
    'MaterialExpressionReroute': '/Script/Engine.MaterialExpressionReroute',
    'MaterialExpressionComment': '/Script/Engine.MaterialExpressionComment',
    'MaterialExpressionSphereMask': '/Script/Engine.MaterialExpressionSphereMask',
    'MaterialExpressionStep': '/Script/Engine.MaterialExpressionStep',
    'MaterialExpressionSmoothStep': '/Script/Engine.MaterialExpressionSmoothStep',
    'MaterialExpressionLength': '/Script/Engine.MaterialExpressionLength',
    'MaterialExpressionDDX': '/Script/Engine.MaterialExpressionDDX',
    'MaterialExpressionDDY': '/Script/Engine.MaterialExpressionDDY',
    'MaterialExpressionDesaturation': '/Script/Engine.MaterialExpressionDesaturation',
    'MaterialExpressionFresnel': '/Script/Engine.MaterialExpressionFresnel',
    'MaterialExpressionBumpOffset': '/Script/Engine.MaterialExpressionBumpOffset',
    'MaterialExpressionDepthFade': '/Script/Engine.MaterialExpressionDepthFade',
    'MaterialExpressionTwoSidedSign': '/Script/Engine.MaterialExpressionTwoSidedSign',
    'MaterialExpressionBlackBody': '/Script/Engine.MaterialExpressionBlackBody',
}


def _get_class_path(class_name: str) -> str:
    """获取 UE 类的完整路径"""
    if class_name in _CLASS_PATHS:
        return _CLASS_PATHS[class_name]
    # 默认尝试构造
    return f'/Script/Engine.{class_name}'


def _get_default_archetype(class_path: str) -> str:
    """获取默认 Archetype"""
    class_name = class_path.rsplit('.', 1)[-1] if '.' in class_path else class_path
    return f"{class_path}'/Script/Engine.Default__{class_name}'"


def generate_t3d_from_material_graph(graph, start_x: int = 0, start_y: int = 0) -> str:
    """从 node_mapper.MaterialGraph 生成 T3D 剪贴板文本
    
    这实现了 HLSL → 节点图 → T3D 文本 的最后一步。
    """
    from graph_visualizer import compute_layout
    
    positions = compute_layout(graph)
    node_by_id = {n.id: n for n in graph.nodes}
    
    # 为每个节点分配 T3D 名称和 UUID
    node_names = {}    # node.id -> MaterialGraphNode_N
    expr_names = {}    # node.id -> MaterialExpressionXxx_N
    node_guids = {}    # node.id -> GUID
    pin_guids = {}     # (node.id, pin_name) -> GUID
    
    for idx, node in enumerate(graph.nodes):
        node_names[node.id] = f'MaterialGraphNode_{idx}'
        expr_names[node.id] = f'{node.ue_class}_{idx}'
        node_guids[node.id] = _make_guid(f'node_{node.id}')
    
    # 为每个节点的每个 pin 分配 GUID
    for node in graph.nodes:
        # 输入 pin
        input_names = list(node.inputs.keys()) if node.inputs else node.input_names
        for pin_name in input_names:
            pin_guids[(node.id, pin_name)] = _make_guid(f'pin_in_{node.id}_{pin_name}')
        # 输出 pin
        pin_guids[(node.id, 'Output')] = _make_guid(f'pin_out_{node.id}')
    
    # 生成每个节点
    node_texts = []
    for idx, node in enumerate(graph.nodes):
        x, y = positions.get(node.id, (50, 50))
        x += start_x
        y += start_y
        
        text = _gen_material_node_t3d(
            node, idx, x, y,
            node_names, expr_names, node_guids, pin_guids,
            node_by_id
        )
        node_texts.append(text)
    
    return '\r\n'.join(node_texts)


def _gen_material_node_t3d(
    node, idx: int, x: int, y: int,
    node_names: dict, expr_names: dict,
    node_guids: dict, pin_guids: dict,
    node_by_id: dict
) -> str:
    """生成单个 MaterialNode 的 T3D 文本"""
    graph_name = node_names[node.id]
    expr_name = expr_names[node.id]
    ue_class = node.ue_class
    class_path = _get_class_path(ue_class)
    graph_class = '/Script/UnrealEd.MaterialGraphNode'
    
    parts = []
    
    # 1. Begin Object（外层 MaterialGraphNode）
    parts.append(f'Begin Object Class={graph_class} Name="{graph_name}"')
    
    # 2. 内层 MaterialExpression - 声明
    parts.append(f'   Begin Object Class={class_path} Name="{expr_name}"')
    parts.append(f'   End Object')
    
    # 3. 内层 MaterialExpression - 定义
    parts.append(f'   Begin Object Name="{expr_name}"')
    
    # 输出属性
    _write_expression_properties(parts, node, x, y)
    
    parts.append(f'   End Object')
    
    # 4. MaterialExpression 引用
    parts.append(f'   MaterialExpression={ue_class}\'"{expr_name}"\'')
    
    # 5. 位置
    parts.append(f'   NodePosX={x}')
    parts.append(f'   NodePosY={y}')
    
    # 6. NodeGuid
    parts.append(f'   NodeGuid={node_guids[node.id]}')
    
    # 7. Pin
    input_names = list(node.inputs.keys()) if node.inputs else node.input_names
    
    # 输入 pin
    for pin_name in input_names:
        source = node.inputs.get(pin_name) if node.inputs else None
        linked_str = ''
        if source and source.id in node_names:
            target_graph = node_names[source.id]
            target_pin_id = pin_guids.get((source.id, 'Output'), _make_guid(f'pin_out_{source.id}'))
            linked_str = f',LinkedTo=({target_graph} {target_pin_id},)'
        
        pin_id = pin_guids.get((node.id, pin_name), _make_guid(f'pin_in_{node.id}_{pin_name}'))
        pin_line = (
            f'   CustomProperties Pin ('
            f'PinId={pin_id},'
            f'PinName="{pin_name}",'
            f'PinType.PinCategory="",'
            f'PinType.PinSubCategoryObject=None,'
            f'PinType.PinSubCategoryMemberReference=(),'
            f'PinType.PinValueType=(),'
            f'PinType.ContainerType=None,'
            f'PinType.bIsReference=False,'
            f'PinType.bIsConst=False,'
            f'PinType.bIsWeakPointer=False'
            f'{linked_str},'
            f'PersistentGuid=00000000000000000000000000000000,'
            f'bHidden=False,bNotConnectable=False,'
            f'bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,'
            f'bAdvancedView=False,bOrphanedPin=False,)'
        )
        parts.append(pin_line)
    
    # 输出 pin
    # 收集所有连接到此节点的目标
    output_linked = []
    for other_node in node_by_id.values():
        for iname, inode in (other_node.inputs.items() if other_node.inputs else []):
            if inode and inode.id == node.id:
                target_graph = node_names.get(other_node.id)
                target_pin_id = pin_guids.get((other_node.id, iname), '')
                if target_graph and target_pin_id:
                    output_linked.append(f'{target_graph} {target_pin_id}')
    
    linked_str = ''
    if output_linked:
        linked_str = f',LinkedTo=({",".join(output_linked)},)'
    
    out_pin_id = pin_guids.get((node.id, 'Output'), _make_guid(f'pin_out_{node.id}'))
    out_pin_line = (
        f'   CustomProperties Pin ('
        f'PinId={out_pin_id},'
        f'PinName="Output",'
        f'PinFriendlyName=" ",'
        f'Direction="EGPD_Output",'
        f'PinType.PinCategory="",'
        f'PinType.PinSubCategoryObject=None,'
        f'PinType.PinSubCategoryMemberReference=(),'
        f'PinType.PinValueType=(),'
        f'PinType.ContainerType=None,'
        f'PinType.bIsReference=False,'
        f'PinType.bIsConst=False,'
        f'PinType.bIsWeakPointer=False'
        f'{linked_str},'
        f'PersistentGuid=00000000000000000000000000000000,'
        f'bHidden=False,bNotConnectable=False,'
        f'bDefaultValueIsReadOnly=False,bDefaultValueIsIgnored=False,'
        f'bAdvancedView=False,bOrphanedPin=False,)'
    )
    parts.append(out_pin_line)
    
    # 8. End Object
    parts.append('End Object')
    
    return '\r\n'.join(parts)


def _write_expression_properties(parts: list, node, x: int, y: int):
    """输出 MaterialExpression 的特定属性"""
    ue_class = node.ue_class
    props = node.properties or {}
    
    # 通用位置属性
    parts.append(f'      MaterialExpressionEditorX={x}')
    parts.append(f'      MaterialExpressionEditorY={y}')
    parts.append(f'      MaterialExpressionGuid={_make_guid(f"expr_{node.id}")}')
    
    # 根据不同的表达式类型输出特定属性
    if ue_class == 'MaterialExpressionConstant':
        if 'R' in props:
            parts.append(f'      R={_fmt_float(props["R"])}')
        elif 'value' in props:
            parts.append(f'      R={_fmt_float(props["value"])}')
    
    elif ue_class == 'MaterialExpressionConstant3Vector':
        if 'Constant' in props:
            parts.append(f'      Constant={_fmt_color(props["Constant"])}')
        elif all(k in props for k in ('R', 'G', 'B')):
            r, g, b = props.get('R', 0), props.get('G', 0), props.get('B', 0)
            a = props.get('A', 0)
            parts.append(f'      Constant=(R={_fmt_float(r)},G={_fmt_float(g)},B={_fmt_float(b)},A={_fmt_float(a)})')
    
    elif ue_class == 'MaterialExpressionConstant4Vector':
        if 'Constant' in props:
            parts.append(f'      Constant={_fmt_color(props["Constant"])}')
    
    elif ue_class == 'MaterialExpressionScalarParameter':
        if 'ParameterName' in props:
            parts.append(f'      ParameterName="{props["ParameterName"]}"')
        if 'DefaultValue' in props:
            parts.append(f'      DefaultValue={_fmt_float(props["DefaultValue"])}')
    
    elif ue_class == 'MaterialExpressionVectorParameter':
        if 'ParameterName' in props:
            parts.append(f'      ParameterName="{props["ParameterName"]}"')
        if 'DefaultValue' in props:
            parts.append(f'      DefaultValue={_fmt_color(props["DefaultValue"])}')
    
    elif ue_class == 'MaterialExpressionTextureCoordinate':
        if 'CoordinateIndex' in props:
            parts.append(f'      CoordinateIndex={props["CoordinateIndex"]}')
        if 'UTiling' in props:
            parts.append(f'      UTiling={props["UTiling"]}')
        if 'VTiling' in props:
            parts.append(f'      VTiling={props["VTiling"]}')
    
    elif ue_class == 'MaterialExpressionTextureSample':
        if 'Texture' in props:
            parts.append(f'      Texture={props["Texture"]}')
    
    elif ue_class in ('MaterialExpressionTextureObjectParameter', 'MaterialExpressionTextureObject'):
        if 'ParameterName' in props:
            parts.append(f'      ParameterName="{props["ParameterName"]}"')
        if 'Texture' in props:
            parts.append(f'      Texture={props["Texture"]}')
        if 'Group' in props:
            parts.append(f'      Group="{props["Group"]}"')
    
    elif ue_class == 'MaterialExpressionStaticBoolParameter':
        if 'ParameterName' in props:
            parts.append(f'      ParameterName="{props["ParameterName"]}"')
        if 'DefaultValue' in props:
            parts.append(f'      DefaultValue={props["DefaultValue"]}')
    
    elif ue_class == 'MaterialExpressionCustom':
        if 'Code' in props:
            # Custom 节点的 HLSL 代码
            # UE4 T3D 格式要求：换行符用 \n 转义，双引号用 \" 转义
            code = props['Code']
            code_escaped = code.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
            parts.append(f'      Code="{code_escaped}"')
        if 'Description' in props:
            desc = props['Description'].replace('"', '\\"')
            parts.append(f'      Description="{desc}"')
        if 'OutputType' in props:
            parts.append(f'      OutputType={props["OutputType"]}')
        # CustomExpression 的输入参数定义
        # UE4 T3D 格式中 Inputs 数组定义告诉引擎有多少个输入 pin
        if 'Inputs' in props:
            inputs_arr = props['Inputs']
            parts.append(f'      Inputs({len(inputs_arr)})')
            for idx, inp in enumerate(inputs_arr):
                inp_name = inp.get('InputName', f'Input{idx}')
                parts.append(f'      Inputs({idx})=(InputName="{inp_name}")')
        elif node.input_names:
            # 从 node.input_names 自动生成 Inputs 数组
            parts.append(f'      Inputs({len(node.input_names)})')
            for idx, iname in enumerate(node.input_names):
                parts.append(f'      Inputs({idx})=(InputName="{iname}")')
    
    elif ue_class == 'MaterialExpressionComponentMask':
        for comp in ('R', 'G', 'B', 'A'):
            if comp in props:
                parts.append(f'      {comp}={props[comp]}')
    
    elif ue_class == 'MaterialExpressionClamp':
        if 'ClampMode' in props:
            parts.append(f'      ClampMode={props["ClampMode"]}')
        if 'MinDefault' in props:
            parts.append(f'      MinDefault={props["MinDefault"]}')
        if 'MaxDefault' in props:
            parts.append(f'      MaxDefault={props["MaxDefault"]}')
    
    elif ue_class == 'MaterialExpressionLinearInterpolate':
        if 'ConstAlpha' in props:
            parts.append(f'      ConstAlpha={props["ConstAlpha"]}')
    
    elif ue_class == 'MaterialExpressionIf':
        if 'EqualsThreshold' in props:
            parts.append(f'      EqualsThreshold={props["EqualsThreshold"]}')
    
    elif ue_class == 'MaterialExpressionPanner':
        if 'SpeedX' in props:
            parts.append(f'      SpeedX={props["SpeedX"]}')
        if 'SpeedY' in props:
            parts.append(f'      SpeedY={props["SpeedY"]}')
    
    # 通用：输出所有未被处理的属性
    handled = {'R', 'G', 'B', 'A', 'value', 'Constant', 'ParameterName', 'DefaultValue',
               'CoordinateIndex', 'UTiling', 'VTiling', 'Texture', 'Code', 'Description',
               'OutputType', 'ClampMode', 'MinDefault', 'MaxDefault', 'ConstAlpha',
               'EqualsThreshold', 'SpeedX', 'SpeedY', 'Inputs', 'Group'}
    for key, value in props.items():
        if key not in handled:
            parts.append(f'      {key}={value}')
