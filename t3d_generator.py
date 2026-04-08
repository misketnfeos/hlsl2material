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
                    value = str(int(float(value)) + offset_x)
                except (ValueError, TypeError):
                    pass
            elif key == 'MaterialExpressionEditorY':
                try:
                    value = str(int(float(value)) + offset_y)
                except (ValueError, TypeError):
                    pass
            # 对于 Code 属性，进行转义处理（从 python 字符串转为 T3D 转义格式）
            elif key == 'Code':
                # value 可能包含真实的换行符（来自 _unescape_t3d_string），需要转义为 \r\n
                if isinstance(value, str) and value.startswith('"') and value.endswith('"'):
                    code_inner = value[1:-1]
                    # 转义代码: 双反斜杠，转义双引号，转义换行为 \r\n
                    code_escaped = code_inner.replace('\\', '\\\\').replace('"', '\\"').replace('\r\n', '\\r\\n').replace('\r', '').replace('\n', '\\r\\n')
                    value = f'"{code_escaped}"'
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
    
    # Custom Node 图没有 output_node 时，用 target='ue4' 并自动检测布局模式
    is_custom_node_mode = (
        graph.output_node is None
        and any(n.ue_class == 'MaterialExpressionCustom' for n in graph.nodes)
    )
    if is_custom_node_mode:
        # 将 Custom Node 临时设为 output_node，使 compute_layout 能正确分层
        for n in graph.nodes:
            if n.ue_class == 'MaterialExpressionCustom':
                graph.output_node = n
                break
    
    positions = compute_layout(graph, target='ue4')
    
    if is_custom_node_mode:
        graph.output_node = None  # 恢复
    
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
    
    # 构建引用重映射表：old_name → new_name（用于修复属性中的内部引用）
    # 格式: OldGraphName.OldExprName → NewGraphName.NewExprName
    ref_rename_map = {}  # old_ref_str → new_ref_str
    for node in graph.nodes:
        old_graph = getattr(node, '_t3d_graph_name', '')
        old_expr = getattr(node, '_t3d_expr_name', '')
        if old_graph and old_expr:
            new_graph = node_names[node.id]
            new_expr = expr_names[node.id]
            # 替换完整引用: "OldGraph.OldExpr" → "NewGraph.NewExpr"
            ref_rename_map[f'{old_graph}.{old_expr}'] = f'{new_graph}.{new_expr}'
            # 也替换不带引号的 expression 引用
            ref_rename_map[old_expr] = new_expr
            ref_rename_map[old_graph] = new_graph
    
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
            node_by_id, ref_rename_map
        )
        node_texts.append(text)
    
    return '\r\n'.join(node_texts)


def _gen_material_node_t3d(
    node, idx: int, x: int, y: int,
    node_names: dict, expr_names: dict,
    node_guids: dict, pin_guids: dict,
    node_by_id: dict, ref_rename_map: dict = None
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
    _write_expression_properties(parts, node, x, y, ref_rename_map)
    
    parts.append(f'   End Object')
    
    # 4. MaterialExpression 引用
    parts.append(f'   MaterialExpression={ue_class}\'"{expr_name}"\'')
    
    # 5. 位置
    parts.append(f'   NodePosX={x}')
    parts.append(f'   NodePosY={y}')
    
    # 6. NodeGuid
    parts.append(f'   NodeGuid={node_guids[node.id]}')
    
    # 6.5. bCanRenameNode（Parameter 节点需要此标志，否则 UE 显示名称时会带引号）
    _RENAMEABLE_CLASSES = {
        'MaterialExpressionScalarParameter',
        'MaterialExpressionVectorParameter',
        'MaterialExpressionTextureObjectParameter',
        'MaterialExpressionTextureSampleParameter2D',
        'MaterialExpressionStaticBoolParameter',
        'MaterialExpressionStaticSwitchParameter',
    }
    if ue_class in _RENAMEABLE_CLASSES:
        parts.append(f'   bCanRenameNode=True')
    
    # 7. Pin
    # IMPORTANT: Use node.input_names order when available, because UE4 matches
    # Inputs(N) to the Nth CustomProperties Pin by index. If we use
    # node.inputs.keys() (dict insertion order from extract_inputs, which is
    # sorted alphabetically), the pin order may differ from the Inputs()
    # property order, causing wrong connections (e.g. Time→UV, UV→Time).
    if node.input_names:
        # Use the canonical order from input_names, then append any extra
        # keys from node.inputs that aren't already listed
        input_names = list(node.input_names)
        if node.inputs:
            for k in node.inputs:
                if k not in input_names:
                    input_names.append(k)
    else:
        input_names = list(node.inputs.keys()) if node.inputs else []
    
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


def _apply_ref_rename(value: str, ref_rename_map: dict) -> str:
    """在属性值字符串中替换旧的节点/表达式引用为新名称
    
    处理形如: Expression=MaterialExpressionFunctionInput'"OldGraph.OldExpr"'
    替换为:   Expression=MaterialExpressionFunctionInput'"NewGraph.NewExpr"'
    """
    if not ref_rename_map:
        return value
    result = value
    # 按长度降序排序，优先替换长的（避免短名称误匹配）
    for old_ref, new_ref in sorted(ref_rename_map.items(), key=lambda x: -len(x[0])):
        result = result.replace(old_ref, new_ref)
    return result


def _write_expression_properties(parts: list, node, x: int, y: int, ref_rename_map: dict = None):
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
            code_escaped = code.replace('\\', '\\\\').replace('"', '\\"').replace('\r\n', '\\r\\n').replace('\r', '').replace('\n', '\\r\\n')
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
        # AdditionalOutputs（额外输出 pin）
        if 'AdditionalOutputs' in props:
            for idx, out in enumerate(props['AdditionalOutputs']):
                out_name = out.get('OutputName', f'Output{idx}')
                out_type = out.get('OutputType', '')
                if out_type:
                    parts.append(f'      AdditionalOutputs({idx})=(OutputName="{out_name}",OutputType={out_type})')
                else:
                    parts.append(f'      AdditionalOutputs({idx})=(OutputName="{out_name}")')
        # bShowOutputNameOnPin
        if props.get('bShowOutputNameOnPin'):
            parts.append(f'      bShowOutputNameOnPin=True')
        # Outputs（所有输出 pin 定义）
        if 'Outputs' in props:
            for idx, out in enumerate(props['Outputs']):
                out_name = out.get('OutputName', 'return')
                parts.append(f'      Outputs({idx})=(OutputName="{out_name}")')
    
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
    
    elif ue_class == 'MaterialExpressionMaterialFunctionCall':
        # MaterialFunction 引用（资产路径）
        if 'MaterialFunction' in props:
            parts.append(f'      MaterialFunction={props["MaterialFunction"]}')
        # FunctionInputs 数组
        if 'FunctionInputs' in props:
            fi_list = props['FunctionInputs']
            for idx, fi in enumerate(fi_list):
                # 每个元素是原始 T3D 字符串或 dict
                if isinstance(fi, str):
                    parts.append(f'      FunctionInputs({idx})={_apply_ref_rename(fi, ref_rename_map)}')
                elif isinstance(fi, dict):
                    fi_parts = []
                    for fk, fv in fi.items():
                        fi_parts.append(f'{fk}={_apply_ref_rename(str(fv), ref_rename_map)}')
                    parts.append(f'      FunctionInputs({idx})=({",".join(fi_parts)})')
        # FunctionOutputs 数组
        if 'FunctionOutputs' in props:
            fo_list = props['FunctionOutputs']
            for idx, fo in enumerate(fo_list):
                if isinstance(fo, str):
                    parts.append(f'      FunctionOutputs({idx})={_apply_ref_rename(fo, ref_rename_map)}')
                elif isinstance(fo, dict):
                    fo_parts = []
                    for fk, fv in fo.items():
                        fo_parts.append(f'{fk}={_apply_ref_rename(str(fv), ref_rename_map)}')
                    parts.append(f'      FunctionOutputs({idx})=({",".join(fo_parts)})')
    
    # 通用：输出所有未被处理的属性
    handled = {'R', 'G', 'B', 'A', 'value', 'Constant', 'ParameterName', 'DefaultValue',
               'CoordinateIndex', 'UTiling', 'VTiling', 'Texture', 'Code', 'Description',
               'OutputType', 'ClampMode', 'MinDefault', 'MaxDefault', 'ConstAlpha',
               'EqualsThreshold', 'SpeedX', 'SpeedY', 'Inputs', 'Group',
               'MaterialExpressionEditorX', 'MaterialExpressionEditorY',
               'MaterialExpressionGuid', 'Material',
               'AdditionalOutputs', 'bShowOutputNameOnPin', 'Outputs',
               'MaterialFunction', 'FunctionInputs', 'FunctionOutputs'}
    for key, value in props.items():
        if key not in handled:
            val_str = _apply_ref_rename(str(value), ref_rename_map) if ref_rename_map else str(value)
            parts.append(f'      {key}={val_str}')


def generate_t3d_from_custom_hlsl(
    hlsl_code: str,
    input_names: list = None,
    output_type: str = 'CMOT_Float3',
    description: str = 'Custom HLSL',
    start_x: int = 0,
    start_y: int = 0,
) -> str:
    """将原始 HLSL 代码直接包装成 MaterialExpressionCustom T3D 格式
    
    Args:
        hlsl_code: 原始 HLSL 代码
        input_names: 输入变量名列表
        output_type: 输出类型 (CMOT_Float1/2/3/4)
        description: 节点描述
        start_x: 节点初始 X 坐标
        start_y: 节点初始 Y 坐标
    
    Returns:
        可直接粘贴到 UE4 的 T3D 文本
    """
    from node_mapper import MaterialNode, MaterialGraph
    
    # 1. Create single MaterialNode (Code stored raw; escaping happens in _write_expression_properties)
    node = MaterialNode(
        ue_class='MaterialExpressionCustom',
        display_name='Custom',
        pos_x=start_x,
        pos_y=start_y,
    )
    node.properties = {
        'Code': hlsl_code,
        'Description': description,
        'OutputType': output_type,
    }
    
    # Add input specifications if provided
    if input_names:
        node.properties['Inputs'] = [{'InputName': name} for name in input_names]
        node.input_names = input_names
    
    # 3. Create MaterialGraph with single node, set as output_node for layout
    graph = MaterialGraph()
    graph.nodes.append(node)
    graph.output_node = node  # Custom Node 作为布局的"输出端"，参数节点排在其左侧
    
    # 4. Auto-create input nodes (builtin variables + parameters) and connect to Custom Node
    from auto_input_generator import auto_create_inputs_for_graph
    auto_create_inputs_for_graph(graph, hlsl_code, custom_node_x=start_x, custom_node_y=start_y)
    
    # 5. Generate T3D
    return generate_t3d_from_material_graph(graph)
