"""
============================================================
 custom_converter.py
 Custom 节点 → 原生材质节点 转换器
============================================================

从 T3D 解析结果中找到 MaterialExpressionCustom 节点，
提取其内部 HLSL 代码和输入 pin，然后：
  1. 用 hlsl_parser 解析 HLSL 代码
  2. 用 node_mapper 将 HLSL 转换为原生 UE4 材质节点
  3. 将 Custom 节点替换为等效的原生节点子图
  4. 保留其他非 Custom 节点不变
  5. 重新连线，确保原来连到 Custom 的输入/输出正确映射

用法：
  from custom_converter import convert_custom_nodes
  result = convert_custom_nodes(t3d_text)
  # result = { 'graph': {...}, 't3d_output': '...', ... }
============================================================
"""

import re
import copy
from typing import Dict, List, Optional, Tuple, Any

from t3d_parser import (
    T3DParser, T3DParseResult, T3DGraphNode, T3DInnerObject, T3DPin,
    parse_t3d_clipboard, parse_t3d_to_result, t3d_to_graph_data,
)
from hlsl_parser import parse_hlsl
from hlsl_preprocessor import preprocess_hlsl, PreprocessResult
from node_mapper import (
    hlsl_to_material_graph, MaterialGraph, MaterialNode,
    NodeMapper, FUNCTION_MAP, BINARY_OP_MAP,
    ENGINE_BUILTIN_VARS,
)
from t3d_generator import generate_t3d_from_material_graph


# ═══════════════════════════════════════════════════════════
# Custom 节点 HLSL 提取
# ═══════════════════════════════════════════════════════════

def _extract_custom_info(node: T3DGraphNode) -> Optional[Dict[str, Any]]:
    """从 T3D 节点提取 Custom 节点的 HLSL 代码和输入信息
    
    返回:
      {
        'code': 'float3 result = ...;\\nreturn result;',
        'description': 'BackLight',
        'inputs': [
          {'name': 'LightDirection', 'pin_id': '...', 'linked_node': '...', 'linked_pin': '...'},
          ...
        ],
        'output_pin_id': '...',
        'output_linked_to': [('NodeName', 'PinGuid'), ...],
        'node_name': 'MaterialGraphNode_X',
        'pos_x': 320,
        'pos_y': 240,
      }
    """
    if not node.material_expression:
        return None
    
    expr = node.material_expression
    class_name = expr.class_name
    
    if class_name != 'MaterialExpressionCustom':
        return None
    
    # 提取 Code
    code = expr.properties.get('Code', '')
    if not code:
        return None
    
    # Code 属性通常被引号包裹，且内部换行用 \n 表示
    code = _unescape_t3d_string(code)
    
    # 提取 Description
    description = expr.properties.get('Description', '').strip('"')
    
    # 提取输入 pin 和输出 pin
    input_pins = []
    output_pin = None
    
    for pin in node.pins:
        if pin.direction == 'EGPD_Input' and not pin.is_hidden:
            input_pins.append({
                'name': pin.pin_name,
                'pin_id': pin.pin_id,
                'linked_to': pin.linked_to,  # [(NodeName, PinGuid), ...]
            })
        elif pin.direction == 'EGPD_Output' and not pin.is_hidden:
            if output_pin is None:  # 取第一个输出 pin
                output_pin = {
                    'pin_id': pin.pin_id,
                    'linked_to': pin.linked_to,
                }
    
    return {
        'code': code,
        'description': description,
        'inputs': input_pins,
        'output': output_pin,
        'node_name': node.name,
        'pos_x': node.node_pos_x,
        'pos_y': node.node_pos_y,
    }


def _unescape_t3d_string(s: str) -> str:
    """反转义 T3D 属性字符串
    
    T3D 中 Code 属性使用的转义序列：
      \\n  → 换行
      \\r  → 回车（通常与 \\n 配合为 \\r\\n）
      \\t  → tab
      \\"  → 引号
      \\\\ → 反斜杠
    
    注意：T3D 文本中这些是字面两个字符（反斜杠+字母），
    Python 读入后会是 \\n, \\r 等两字符序列。
    """
    s = s.strip('"')
    # 先处理 \\\\（双反斜杠），用占位符暂存，避免干扰后续替换
    PLACEHOLDER = '\x00BACKSLASH\x00'
    s = s.replace('\\\\', PLACEHOLDER)
    # 转义序列替换
    s = s.replace('\\r\\n', '\n')  # Windows 风格换行，先处理组合
    s = s.replace('\\r', '\r')
    s = s.replace('\\n', '\n')
    s = s.replace('\\t', '\t')
    s = s.replace('\\"', '"')
    # 还原真正的反斜杠
    s = s.replace(PLACEHOLDER, '\\')
    return s


# ═══════════════════════════════════════════════════════════
# Custom HLSL 预处理
# ═══════════════════════════════════════════════════════════

def _preprocess_custom_hlsl(code: str, input_names: List[str]) -> str:
    """预处理 Custom 节点的 HLSL 代码
    
    Custom 节点的 HLSL 与普通 HLSL 有几个区别：
    1. 输入变量直接使用 pin 名称（如 LightDirection），不需要声明
    2. 可能没有 return 语句（最后一行就是输出）
    3. 可能用了 UE4 特有的函数（如 GetAtmosphericLightDirection 等）
    """
    # 清理残留的 \r 和行末反斜杠续行符
    code = code.replace('\r\n', '\n').replace('\r', '\n')
    
    lines = code.strip().split('\n')
    processed_lines = []
    
    has_return = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('return '):
            has_return = True
        processed_lines.append(line)
    
    # 如果没有 return，看最后一行是否是表达式，自动添加 return
    if not has_return and processed_lines:
        last = processed_lines[-1].strip()
        # 如果最后一行不是赋值也不是空行
        if last and not last.endswith(';'):
            processed_lines[-1] = f'return {last};'
        elif last.endswith(';') and '=' not in last and not last.startswith('//'):
            # 单独的表达式语句
            processed_lines[-1] = f'return {last[:-1]};'
    
    return '\n'.join(processed_lines)


# ═══════════════════════════════════════════════════════════
# 核心转换逻辑
# ═══════════════════════════════════════════════════════════

def convert_custom_in_t3d(t3d_text: str) -> Dict[str, Any]:
    """将 T3D 文本中的 Custom 节点转换为原生材质节点
    
    流程：
    1. 解析 T3D → 找到所有 Custom 节点
    2. 对每个 Custom 节点：提取 HLSL → 解析 → 转换为 MaterialGraph
    3. 将转换出的原生节点替换 Custom 节点
    4. 保留所有非 Custom 节点
    5. 重新生成 T3D 剪贴板文本
    
    返回:
      {
        'graph': { ... },        # 前端可视化用的图数据
        't3d_output': '...',     # 可粘贴回 UE4 的 T3D 文本
        'custom_count': int,     # 找到的 Custom 节点数
        'converted_count': int,  # 成功转换的 Custom 节点数
        'total_new_nodes': int,  # 转换后新增的原生节点数
        'warnings': [...],
        'error': '...',          # 如果有错误
      }
    """
    warnings = []
    
    # 1. 解析 T3D
    parser = T3DParser()
    parse_result = parser.parse(t3d_text)
    
    if parse_result.errors:
        return {
            'error': '\n'.join(parse_result.errors),
            'warnings': parse_result.warnings,
        }
    
    warnings.extend(parse_result.warnings)
    
    # 2. 找到所有 Custom 节点
    custom_infos = []
    custom_node_names = set()
    non_custom_nodes = []
    
    for node in parse_result.nodes:
        info = _extract_custom_info(node)
        if info:
            custom_infos.append(info)
            custom_node_names.add(node.name)
        else:
            non_custom_nodes.append(node)
    
    if not custom_infos:
        # 没有 Custom 节点，原样返回
        graph_data = t3d_to_graph_data(parse_result)
        return {
            'graph': graph_data,
            't3d_output': t3d_text,
            'custom_count': 0,
            'converted_count': 0,
            'total_new_nodes': 0,
            'warnings': warnings + ['未找到 Custom 节点，无需转换'],
        }
    
    # 3. 转换每个 Custom 节点
    all_converted_graphs = []  # [(custom_info, MaterialGraph), ...]
    converted_count = 0
    total_new_nodes = 0
    
    for info in custom_infos:
        try:
            code = info['code']
            input_names = [inp['name'] for inp in info['inputs']]
            
            # 解析 HLSL → 材质节点图
            # hlsl_to_material_graph 内部自动处理：
            #   - struct 提取 + 函数内联展开
            #   - 复杂函数（含 for/while/多 return）拆分为 CustomExpression
            #   - HLSL 解析 → AST → 材质节点映射
            graph = hlsl_to_material_graph(code)
            
            all_converted_graphs.append((info, graph))
            converted_count += 1
            total_new_nodes += len(graph.nodes)
            
            desc = info.get('description', 'Custom')
            warnings.append(
                f'✓ Custom 节点 "{desc}" → {len(graph.nodes)} 个原生节点'
            )
            
            if graph.warnings:
                for w in graph.warnings:
                    warnings.append(f'  ⚠ {desc}: {w}')
                    
        except Exception as e:
            desc = info.get('description', 'Custom')
            warnings.append(f'✗ Custom 节点 "{desc}" 转换失败: {str(e)}')
    
    if not all_converted_graphs:
        # 所有 Custom 节点都转换失败，返回原始
        graph_data = t3d_to_graph_data(parse_result)
        return {
            'graph': graph_data,
            't3d_output': t3d_text,
            'custom_count': len(custom_infos),
            'converted_count': 0,
            'total_new_nodes': 0,
            'warnings': warnings,
        }
    
    # 4. 合并所有转换结果为一个大 MaterialGraph
    merged_graph = _merge_conversion_results(
        parse_result, non_custom_nodes, all_converted_graphs, warnings
    )
    
    # 5. 生成 T3D 输出
    t3d_output = generate_t3d_from_material_graph(merged_graph)
    
    # 6. 生成前端图数据
    from graph_visualizer import compute_layout, NODE_COLORS, DEFAULT_COLOR
    graph_data = _graph_to_vis_data(merged_graph)
    
    return {
        'graph': graph_data,
        't3d_output': t3d_output,
        'custom_count': len(custom_infos),
        'converted_count': converted_count,
        'total_new_nodes': total_new_nodes,
        'warnings': warnings,
    }


def _merge_conversion_results(
    parse_result: T3DParseResult,
    non_custom_nodes: List[T3DGraphNode],
    converted: List[Tuple[Dict, MaterialGraph]],
    warnings: List[str],
) -> MaterialGraph:
    """将非 Custom 节点 + 转换出的原生节点合并为一个 MaterialGraph
    
    关键逻辑：
    - 非 Custom 的 T3D 节点 → 直接转为 MaterialNode
    - Custom 转换出的 MaterialGraph → 把其所有节点加入合并图
    - 重新映射连线：Custom 的输入 pin → 转换后子图的输入参数节点
    - 重新映射连线：Custom 的输出 pin → 转换后子图的 output_node
    """
    merged = MaterialGraph()
    merged._next_id = 0
    
    # ── 第一步：将非 Custom 的 T3D 节点转为 MaterialNode ──
    
    # 记录 T3D node name → MaterialNode 的映射
    t3d_name_to_mat_node: Dict[str, MaterialNode] = {}
    # 记录 T3D pin_id → MaterialNode 的映射（用于连线重建）
    t3d_pin_to_mat_node: Dict[str, MaterialNode] = {}
    
    for t3d_node in non_custom_nodes:
        mat_node = _t3d_node_to_material_node(t3d_node, merged)
        t3d_name_to_mat_node[t3d_node.name] = mat_node
        
        # 记录输出 pin → 这个节点
        for pin in t3d_node.pins:
            if pin.direction == 'EGPD_Output':
                t3d_pin_to_mat_node[pin.pin_id] = mat_node
    
    # ── 第二步：将 Custom 转换出的子图加入合并图 ──
    
    # 记录 Custom 节点名 → (输入映射, 输出节点) 的映射
    custom_replacement: Dict[str, Dict] = {}
    
    for info, sub_graph in converted:
        custom_name = info['node_name']
        pos_x = info['pos_x']
        pos_y = info['pos_y']
        
        # 重新编号子图中的所有节点
        id_offset = merged._next_id
        input_name_to_node: Dict[str, MaterialNode] = {}
        
        for sub_node in sub_graph.nodes:
            # 给新 ID
            sub_node.id = merged._next_id
            merged._next_id += 1
            
            # 偏移位置（以 Custom 节点位置为基准）
            sub_node.pos_x += pos_x
            sub_node.pos_y += pos_y
            
            merged.nodes.append(sub_node)
        
        # 记录输入参数映射：Custom 的输入 pin 名 → 子图中的输入节点
        for param_name, param_node in sub_graph.input_nodes.items():
            input_name_to_node[param_name] = param_node
        
        # 记录输出节点
        output_node = sub_graph.output_node
        
        custom_replacement[custom_name] = {
            'info': info,
            'input_map': input_name_to_node,  # pin_name → 子图输入节点
            'output_node': output_node,  # 子图输出节点
        }
        
        # 为输出 pin 建立映射
        if output_node and info.get('output'):
            out_pin_id = info['output']['pin_id']
            t3d_pin_to_mat_node[out_pin_id] = output_node
            t3d_name_to_mat_node[custom_name] = output_node
    
    # ── 第三步：重建连线 ──
    
    # 建立 pin_id → 所属 T3DGraphNode 的索引
    all_nodes = parse_result.nodes
    pin_to_t3d_node: Dict[str, T3DGraphNode] = {}
    for t3d_node in all_nodes:
        for pin in t3d_node.pins:
            pin_to_t3d_node[pin.pin_id] = t3d_node
    
    # 对每个非 Custom 节点，重建输入连线
    for t3d_node in non_custom_nodes:
        mat_node = t3d_name_to_mat_node.get(t3d_node.name)
        if not mat_node:
            continue
        
        for pin in t3d_node.pins:
            if pin.direction != 'EGPD_Input' or not pin.linked_to:
                continue
            
            for linked_node_name, linked_pin_id in pin.linked_to:
                # 找到源节点
                if linked_node_name in custom_replacement:
                    # 连接到 Custom 节点 → 替换为子图的 output_node
                    replacement = custom_replacement[linked_node_name]
                    source_node = replacement['output_node']
                else:
                    source_node = t3d_name_to_mat_node.get(linked_node_name)
                
                if source_node and mat_node:
                    pin_name = pin.pin_name or f'Input_{len(mat_node.inputs)}'
                    mat_node.inputs[pin_name] = source_node
    
    # 对每个 Custom 替换，重建输入连线
    for custom_name, replacement in custom_replacement.items():
        info = replacement['info']
        input_map = replacement['input_map']
        
        for inp in info['inputs']:
            pin_name = inp['name']
            linked_to = inp.get('linked_to', [])
            
            if not linked_to:
                continue
            
            # 找到子图中对应此输入名的节点
            target_node = input_map.get(pin_name)
            if not target_node:
                # 尝试忽略大小写匹配
                for k, v in input_map.items():
                    if k.lower() == pin_name.lower():
                        target_node = v
                        break
            
            if not target_node:
                warnings.append(
                    f'⚠ Custom 输入 "{pin_name}" 在转换结果中未找到对应节点'
                )
                continue
            
            # 找到源节点（连接到 Custom 输入的那个节点）
            for source_node_name, source_pin_id in linked_to:
                source_mat_node = t3d_name_to_mat_node.get(source_node_name)
                if source_mat_node:
                    # 把子图输入参数节点替换为实际的源节点
                    _replace_node_in_graph(merged, target_node, source_mat_node)
    
    # 设置 output_node（如果有的话取第一个 Custom 的输出）
    if converted:
        first_replacement = custom_replacement.get(converted[0][0]['node_name'])
        if first_replacement and first_replacement['output_node']:
            merged.output_node = first_replacement['output_node']
    
    merged.warnings.extend(warnings)
    
    return merged


def _replace_node_in_graph(
    graph: MaterialGraph,
    old_node: MaterialNode,
    new_node: MaterialNode,
):
    """在图中将所有引用 old_node 的地方替换为 new_node"""
    for node in graph.nodes:
        for key, val in list(node.inputs.items()):
            if val is old_node:
                node.inputs[key] = new_node
    
    # 从节点列表中移除 old_node
    if old_node in graph.nodes:
        graph.nodes.remove(old_node)
    
    # 从输入节点映射中更新
    for key, val in list(graph.input_nodes.items()):
        if val is old_node:
            graph.input_nodes[key] = new_node


def _t3d_node_to_material_node(t3d_node: T3DGraphNode, graph: MaterialGraph) -> MaterialNode:
    """将一个 T3D 节点转为 MaterialNode（保留原始属性）"""
    expr = t3d_node.material_expression
    if not expr:
        ue_class = 'MaterialExpressionComment'
        display_name = t3d_node.name
        properties = {}
    else:
        ue_class = expr.class_name
        display_name = _get_display_name_for_expr(expr)
        properties = dict(expr.properties)
    
    node = MaterialNode(
        id=graph._next_id,
        ue_class=ue_class,
        display_name=display_name,
        pos_x=t3d_node.node_pos_x,
        pos_y=t3d_node.node_pos_y,
        properties=properties,
    )
    graph._next_id += 1
    graph.nodes.append(node)
    
    return node


def _get_display_name_for_expr(expr: T3DInnerObject) -> str:
    """从 MaterialExpression 获取显示名称"""
    cls = expr.class_name
    props = expr.properties
    
    # 去掉前缀 MaterialExpression
    name = cls.replace('MaterialExpression', '')
    
    if cls == 'MaterialExpressionConstant' and 'R' in props:
        name = f'Const({props["R"]})'
    elif cls == 'MaterialExpressionScalarParameter' and 'ParameterName' in props:
        pname = props['ParameterName'].strip('"')
        name = f'Param: {pname}'
    elif cls == 'MaterialExpressionVectorParameter' and 'ParameterName' in props:
        pname = props['ParameterName'].strip('"')
        name = f'Vec Param: {pname}'
    elif cls == 'MaterialExpressionConstant3Vector':
        name = 'Color'
    elif cls == 'MaterialExpressionCustom':
        desc = props.get('Description', '').strip('"')
        name = f'Custom: {desc}' if desc else 'Custom'
    
    return name



# ═══════════════════════════════════════════════════════════
# CustomExpression 片段注册
# ═══════════════════════════════════════════════════════════

def _register_custom_fragments(graph: MaterialGraph, preprocess_result: PreprocessResult):
    """将预处理中拆出的复杂函数片段注册到 MaterialGraph 中
    
    这些复杂函数（含 for/while/多 return 等）在预处理阶段被标记为
    __CUSTOM_N__(args)，hlsl_parser 会把它们解析为普通函数调用。
    
    node_mapper 在遇到 __CUSTOM_N__ 时会创建 CustomExpression 节点，
    所以我们需要在 graph 上注册每个片段的代码和输入信息，
    让 node_mapper 能找到正确的 HLSL 代码。
    """
    for idx, fragment in enumerate(preprocess_result.custom_fragments):
        marker = f'__CUSTOM_{idx}__'
        graph.custom_expressions.append(
            f'{marker}:{fragment["name"]}:{fragment["code"]}'
        )


# ═══════════════════════════════════════════════════════════
# 图可视化数据生成
# ═══════════════════════════════════════════════════════════

# 类别颜色映射（和 graph_visualizer.py 一致）
_CATEGORY_COLORS = {
    'MaterialExpressionConstant': '#4a6741',
    'MaterialExpressionConstant3Vector': '#4a6741',
    'MaterialExpressionConstant4Vector': '#4a6741',
    'MaterialExpressionStaticBool': '#4a6741',
    'MaterialExpressionAdd': '#3d5a80',
    'MaterialExpressionSubtract': '#3d5a80',
    'MaterialExpressionMultiply': '#3d5a80',
    'MaterialExpressionDivide': '#3d5a80',
    'MaterialExpressionFmod': '#3d5a80',
    'MaterialExpressionAbs': '#3d5a80',
    'MaterialExpressionSign': '#3d5a80',
    'MaterialExpressionFloor': '#3d5a80',
    'MaterialExpressionCeil': '#3d5a80',
    'MaterialExpressionRound': '#3d5a80',
    'MaterialExpressionFrac': '#3d5a80',
    'MaterialExpressionMin': '#3d5a80',
    'MaterialExpressionMax': '#3d5a80',
    'MaterialExpressionSquareRoot': '#3d5a80',
    'MaterialExpressionOneMinus': '#3d5a80',
    'MaterialExpressionLinearInterpolate': '#6b4c8a',
    'MaterialExpressionSaturate': '#6b4c8a',
    'MaterialExpressionClamp': '#6b4c8a',
    'MaterialExpressionSine': '#4a7a8a',
    'MaterialExpressionCosine': '#4a7a8a',
    'MaterialExpressionTangent': '#4a7a8a',
    'MaterialExpressionArcsine': '#4a7a8a',
    'MaterialExpressionArccosine': '#4a7a8a',
    'MaterialExpressionDotProduct': '#7a5a3d',
    'MaterialExpressionCrossProduct': '#7a5a3d',
    'MaterialExpressionNormalize': '#7a5a3d',
    'MaterialExpressionComponentMask': '#7a5a3d',
    'MaterialExpressionAppendVector': '#7a5a3d',
    'MaterialExpressionTransform': '#7a5a3d',
    'MaterialExpressionPower': '#3d5a80',
    'MaterialExpressionTextureSample': '#8a3d3d',
    'MaterialExpressionTextureSampleParameter2D': '#8a3d3d',
    'MaterialExpressionTextureCoordinate': '#8a5a3d',
    'MaterialExpressionTextureObject': '#8a5a3d',
    'MaterialExpressionTextureObjectParameter': '#8a5a3d',
    'MaterialExpressionScalarParameter': '#8a7a3d',
    'MaterialExpressionVectorParameter': '#8a7a3d',
    'MaterialExpressionStaticBoolParameter': '#8a7a3d',
    'MaterialExpressionStaticSwitchParameter': '#8a7a3d',
    'MaterialExpressionIf': '#8a4a6a',
    'MaterialExpressionCustom': '#5a5a5a',
}
_DEFAULT_COLOR = '#455a64'


def _graph_to_vis_data(graph: MaterialGraph) -> Dict[str, Any]:
    """将 MaterialGraph 转为前端可视化用的 JSON 数据
    
    输出格式必须与 web_server.py 的 graph_to_json() 和
    t3d_parser.py 的 t3d_to_graph_data() 保持一致，
    这样前端 renderGraph() 才能正确显示节点名称和引脚。
    
    前端期望的节点字段：
      id, display_name, ue_class, x, y, color,
      inputs: [{name, source_id}],  -- 已连接的输入列表
      input_names: [str],            -- 所有输入 pin 名称
      properties, is_output, is_input, is_builtin, source_line
    """
    from graph_visualizer import compute_layout, NODE_COLORS, DEFAULT_COLOR
    
    # 计算布局
    positions = compute_layout(graph)
    node_by_id = {n.id: n for n in graph.nodes}
    
    nodes_data = []
    for node in graph.nodes:
        x, y = positions.get(node.id, (node.pos_x or 50, node.pos_y or 50))
        color = NODE_COLORS.get(node.ue_class, DEFAULT_COLOR)
        
        # 已连接的输入列表（与 graph_to_json 格式一致）
        input_list = []
        for iname, inode in node.inputs.items():
            if inode:
                input_list.append({
                    'name': iname,
                    'source_id': inode.id,
                })
        
        # 属性字符串
        props_str = ''
        if node.properties:
            import json as _json
            props_str = _json.dumps(node.properties, indent=2, ensure_ascii=False)
        
        # 输入 pin 名称列表
        input_names = list(node.inputs.keys()) if node.inputs else node.input_names
        
        is_output = (graph.output_node and node.id == graph.output_node.id)
        is_input = node.ue_class in (
            'MaterialExpressionFunctionInput',
            'MaterialExpressionScalarParameter',
            'MaterialExpressionVectorParameter',
        )
        is_builtin = (
            node.ue_class.startswith('MaterialExpressionCamera') or
            node.ue_class.startswith('MaterialExpressionWorld') or
            node.ue_class.startswith('MaterialExpressionPixelNormal') or
            node.ue_class.startswith('MaterialExpressionVertexNormal') or
            (node.ue_class.startswith('MaterialExpressionTexture') and 'Coordinate' in node.ue_class) or
            node.ue_class == 'MaterialExpressionTime' or
            node.ue_class == 'MaterialExpressionScreenPosition' or
            node.ue_class == 'MaterialExpressionVertexColor' or
            node.ue_class.startswith('MaterialExpressionReflectionVector') or
            node.ue_class.startswith('MaterialExpressionObject') or
            node.ue_class.startswith('MaterialExpressionActor') or
            node.ue_class == 'MaterialExpressionPixelDepth' or
            node.ue_class == 'MaterialExpressionSceneDepth'
        )
        
        nodes_data.append({
            'id': node.id,
            'display_name': node.display_name,
            'ue_class': node.ue_class,
            'x': x, 'y': y,
            'color': color,
            'inputs': input_list,
            'input_names': input_names,
            'properties': props_str,
            'is_output': is_output,
            'is_input': is_input,
            'is_builtin': is_builtin,
            'source_line': node.source_line,
        })
    
    # 连线（与 graph_to_json 格式一致）
    connections = []
    for node in graph.nodes:
        for i, (iname, inode) in enumerate(node.inputs.items()):
            if inode and inode.id in node_by_id:
                connections.append({
                    'from_id': inode.id,
                    'to_id': node.id,
                    'to_pin_index': i,
                    'label': iname,
                })
    
    # 统计
    input_count = len(graph.input_nodes)
    
    return {
        'nodes': nodes_data,
        'connections': connections,
        'stats': {
            'node_count': len(nodes_data),
            'input_count': input_count,
            'connection_count': len(connections),
        },
        'warnings': graph.warnings,
        'custom_expressions': graph.custom_expressions,
    }


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def convert_custom_nodes(t3d_text: str) -> Dict[str, Any]:
    """一步完成：T3D 文本中的 Custom 节点 → 原生节点
    
    这是主入口函数。
    """
    return convert_custom_in_t3d(t3d_text)
