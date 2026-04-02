"""
============================================================
 auto_input_generator.py
 自动为 Custom Node 创建输入变量节点并连线
============================================================

从 HLSL 代码或 AST 中提取外部输入变量，智能推断参数类型，
自动创建对应的参数节点并连接到 Custom Node 的输入引脚。

功能：
  1. 从 HLSL 代码/AST 提取外部输入变量
  2. 智能类型推断（标量/向量/纹理/内置变量）
  3. 自动创建 Custom Node 输入 Pin
  4. 自动生成参数节点并排列在 Custom Node 左侧
  5. 自动连接参数节点输出到 Custom Node 输入
  6. UE4 内置变量识别和映射

类型推断规则：
  - float/int/half → ScalarParameter
  - float2/float3/float4 → VectorParameter
  - tex2D/texture 参数 → TextureObject + TextureSample
  - 名称包含 Color/Position/Direction → 向量参数
  - 名称包含 Power/Intensity/Scale → 标量参数
  - UE4 内置变量 → 对应引擎节点
============================================================
"""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Any

from hlsl_parser import (
    parse_hlsl, HLSLProgram, ASTNode, VarDeclaration,
    ReturnStatement, ExpressionStatement, IfStatement,
    Identifier, BinaryOp, UnaryOp, FunctionCall,
    TypeConstructor, SwizzleAccess, ArrayAccess, TernaryOp, Assignment,
)
from node_mapper import (
    MaterialNode, MaterialGraph, ENGINE_BUILTIN_VARS,
    TYPE_DIMENSION_MAP, NodeMapper,
)


# ═══════════════════════════════════════════════════════════
# 输入变量信息
# ═══════════════════════════════════════════════════════════

@dataclass
class InputVarInfo:
    """输入变量的描述信息"""
    name: str                       # 变量名
    var_type: str = 'unknown'       # 推断的 HLSL 类型: float, float2, float3, float4, texture
    param_type: str = 'vector'      # 参数类型: scalar, vector, texture, builtin
    dimension: int = 3              # 维度: 1=标量, 2=vec2, 3=vec3, 4=vec4, -1=纹理
    default_value: Any = None       # 默认值
    group: str = ''                 # 参数分组名
    ue_class: str = ''              # UE4 节点类名 (内置变量时使用)
    display_name: str = ''          # 显示名称 (内置变量时使用)
    is_builtin: bool = False        # 是否是引擎内置变量
    is_texture: bool = False        # 是否是纹理参数
    used_in_tex2d: bool = False     # 是否作为 tex2D 的第一个参数使用


# ═══════════════════════════════════════════════════════════
# 标量参数名称模式
# ═══════════════════════════════════════════════════════════

SCALAR_SUFFIXES = [
    'power', 'intensity', 'strength', 'amount', 'width', 'height',
    'scale', 'speed', 'offset', 'bias', 'factor', 'ratio', 'threshold',
    'radius', 'size', 'alpha', 'opacity', 'metallic', 'roughness',
    'specular', 'exponent', 'weight', 'blend', 'value', 'time',
    'distance', 'depth', 'angle', 'frequency', 'amplitude',
    'min', 'max', 'step', 'tiling', 'density',
    'thickness', 'attenuation', 'falloff', 'hardness', 'softness',
    'contrast', 'saturation', 'brightness', 'gamma', 'exposure',
]

VECTOR_PATTERNS = [
    'color', 'colour', 'pos', 'position', 'dir', 'direction',
    'normal', 'tangent', 'bitangent', 'vec', 'rgb',
]

TEXTURE_PATTERNS = ['tex', 'texture', 'map', 'sampler']

# 参数分组规则：根据名称前缀/后缀推断分组
GROUP_PATTERNS = {
    'Base': ['basecolor', 'base_color', 'basecol', 'albedo'],
    'Lighting': ['rim', 'fresnel', 'specular', 'metallic', 'roughness'],
    'Effect': ['dissolve', 'distortion', 'edge', 'noise', 'glow', 'emission'],
    'Transform': ['uv', 'tiling', 'offset', 'scale', 'speed', 'panning'],
    'Texture': ['tex', 'texture', 'map', 'sampler'],
}


# ═══════════════════════════════════════════════════════════
# AutoInputGenerator
# ═══════════════════════════════════════════════════════════

class AutoInputGenerator:
    """
    自动输入变量生成器

    从 HLSL 代码中提取外部输入变量，推断类型，
    生成对应的 UE4 参数节点或内置变量节点。
    """

    def __init__(self):
        self.inputs: List[InputVarInfo] = []
        self._declared_vars: set = set()
        self._used_vars: set = set()
        self._tex2d_first_args: set = set()  # tex2D 第一个参数名
        self._var_type_hints: Dict[str, str] = {}  # 从声明中收集的类型信息

    def extract_inputs(self, hlsl_code: str = None, ast: HLSLProgram = None) -> List[InputVarInfo]:
        """
        从 HLSL 代码或 AST 提取所有外部输入变量

        参数:
            hlsl_code: HLSL 代码字符串 (二选一)
            ast: 已解析的 AST (二选一)

        返回:
            InputVarInfo 列表
        """
        if ast is None and hlsl_code is not None:
            ast = parse_hlsl(hlsl_code)
        elif ast is None and hlsl_code is None:
            raise ValueError("必须提供 hlsl_code 或 ast 参数")

        self.inputs = []
        self._declared_vars = set()
        self._used_vars = set()
        self._tex2d_first_args = set()
        self._var_type_hints = {}

        # 第一遍：收集声明和使用
        self._collect_declarations(ast.statements)
        self._collect_usage(ast.statements)

        # 第二遍：从 tex2D 调用中识别纹理参数
        self._collect_tex2d_params(ast.statements)

        # 确定外部输入：使用了但未声明的变量
        external_vars = self._used_vars - self._declared_vars

        # 为每个外部变量创建 InputVarInfo
        for var_name in sorted(external_vars):
            info = self._analyze_variable(var_name)
            self.inputs.append(info)

        return self.inputs

    def _collect_declarations(self, stmts: List[ASTNode]):
        """收集所有变量声明"""
        for stmt in stmts:
            if isinstance(stmt, VarDeclaration):
                self._declared_vars.add(stmt.var_name)
                self._var_type_hints[stmt.var_name] = stmt.type_name
            elif isinstance(stmt, IfStatement):
                self._collect_declarations(stmt.then_body)
                self._collect_declarations(stmt.else_body)

    def _collect_usage(self, stmts: List[ASTNode]):
        """收集所有变量使用"""
        for stmt in stmts:
            if isinstance(stmt, VarDeclaration):
                if stmt.initializer:
                    self._collect_expr_usage(stmt.initializer)
            elif isinstance(stmt, ReturnStatement):
                if stmt.value:
                    self._collect_expr_usage(stmt.value)
            elif isinstance(stmt, ExpressionStatement):
                self._collect_expr_usage(stmt.expression)
            elif isinstance(stmt, IfStatement):
                self._collect_expr_usage(stmt.condition)
                self._collect_usage(stmt.then_body)
                self._collect_usage(stmt.else_body)

    def _collect_expr_usage(self, expr: ASTNode):
        """递归收集表达式中的变量引用"""
        if expr is None:
            return
        if isinstance(expr, Identifier):
            self._used_vars.add(expr.name)
        elif isinstance(expr, BinaryOp):
            self._collect_expr_usage(expr.left)
            self._collect_expr_usage(expr.right)
        elif isinstance(expr, UnaryOp):
            self._collect_expr_usage(expr.operand)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                self._collect_expr_usage(arg)
        elif isinstance(expr, TypeConstructor):
            for arg in expr.args:
                self._collect_expr_usage(arg)
        elif isinstance(expr, SwizzleAccess):
            self._collect_expr_usage(expr.object)
        elif isinstance(expr, ArrayAccess):
            self._collect_expr_usage(expr.object)
            self._collect_expr_usage(expr.index)
        elif isinstance(expr, TernaryOp):
            self._collect_expr_usage(expr.condition)
            self._collect_expr_usage(expr.true_expr)
            self._collect_expr_usage(expr.false_expr)
        elif isinstance(expr, Assignment):
            self._collect_expr_usage(expr.target)
            self._collect_expr_usage(expr.value)

    def _collect_tex2d_params(self, stmts: List[ASTNode]):
        """收集 tex2D 调用中的第一个参数（纹理对象）"""
        for stmt in stmts:
            if isinstance(stmt, VarDeclaration) and stmt.initializer:
                self._find_tex2d_calls(stmt.initializer)
            elif isinstance(stmt, ReturnStatement) and stmt.value:
                self._find_tex2d_calls(stmt.value)
            elif isinstance(stmt, ExpressionStatement):
                self._find_tex2d_calls(stmt.expression)
            elif isinstance(stmt, IfStatement):
                self._find_tex2d_calls(stmt.condition)
                self._collect_tex2d_params(stmt.then_body)
                self._collect_tex2d_params(stmt.else_body)

    def _find_tex2d_calls(self, expr: ASTNode):
        """递归查找 tex2D 调用，标记第一个参数为纹理"""
        if expr is None:
            return
        if isinstance(expr, FunctionCall):
            if expr.name in ('tex2D', 'Texture2DSample', 'texture') and len(expr.args) >= 1:
                first_arg = expr.args[0]
                if isinstance(first_arg, Identifier):
                    self._tex2d_first_args.add(first_arg.name)
            for arg in expr.args:
                self._find_tex2d_calls(arg)
        elif isinstance(expr, BinaryOp):
            self._find_tex2d_calls(expr.left)
            self._find_tex2d_calls(expr.right)
        elif isinstance(expr, UnaryOp):
            self._find_tex2d_calls(expr.operand)
        elif isinstance(expr, TypeConstructor):
            for arg in expr.args:
                self._find_tex2d_calls(arg)
        elif isinstance(expr, SwizzleAccess):
            self._find_tex2d_calls(expr.object)
        elif isinstance(expr, ArrayAccess):
            self._find_tex2d_calls(expr.object)
            self._find_tex2d_calls(expr.index)
        elif isinstance(expr, TernaryOp):
            self._find_tex2d_calls(expr.condition)
            self._find_tex2d_calls(expr.true_expr)
            self._find_tex2d_calls(expr.false_expr)
        elif isinstance(expr, Assignment):
            self._find_tex2d_calls(expr.target)
            self._find_tex2d_calls(expr.value)

    def _analyze_variable(self, name: str) -> InputVarInfo:
        """分析一个外部变量，推断其类型和参数信息"""
        info = InputVarInfo(name=name)

        # 1. 检查是否是 UE4 内置变量
        if name in ENGINE_BUILTIN_VARS:
            ue_class, display_name, input_names = ENGINE_BUILTIN_VARS[name]
            info.param_type = 'builtin'
            info.is_builtin = True
            info.ue_class = ue_class
            info.display_name = display_name
            info.var_type = 'builtin'
            info.dimension = 3  # 大多数内置变量是 float3
            return info

        # 2. 检查是否是纹理参数（tex2D 第一个参数或名称匹配）
        if name in self._tex2d_first_args:
            info.param_type = 'texture'
            info.is_texture = True
            info.used_in_tex2d = True
            info.var_type = 'texture'
            info.dimension = -1
            info.group = self._guess_group(name)
            return info

        name_lower = name.lower()
        is_texture_name = any(pat in name_lower for pat in TEXTURE_PATTERNS)
        if is_texture_name:
            info.param_type = 'texture'
            info.is_texture = True
            info.var_type = 'texture'
            info.dimension = -1
            info.group = self._guess_group(name)
            return info

        # 3. 推断标量 vs 向量
        dimension = self._guess_dimension(name)
        info.dimension = dimension

        if dimension == 1:
            info.param_type = 'scalar'
            info.var_type = 'float'
            info.default_value = 0.0
        elif dimension == 2:
            info.param_type = 'vector'
            info.var_type = 'float2'
            info.default_value = {'R': 0, 'G': 0, 'B': 0, 'A': 1}
        elif dimension == 4:
            info.param_type = 'vector'
            info.var_type = 'float4'
            info.default_value = {'R': 0, 'G': 0, 'B': 0, 'A': 1}
        else:
            info.param_type = 'vector'
            info.var_type = 'float3'
            info.default_value = {'R': 0, 'G': 0, 'B': 0, 'A': 1}
            info.dimension = 3

        info.group = self._guess_group(name)
        return info

    def _guess_dimension(self, name: str) -> int:
        """根据名称推断参数维度"""
        name_lower = name.lower()

        # 标量模式
        for suffix in SCALAR_SUFFIXES:
            if name_lower.endswith(suffix) or name_lower == suffix:
                return 1

        # 向量模式
        for pat in VECTOR_PATTERNS:
            if pat in name_lower:
                return 3

        # UV 坐标
        if 'uv' in name_lower:
            return 2

        # 默认 float3
        return 3

    def _guess_group(self, name: str) -> str:
        """根据名称推断参数分组"""
        name_lower = name.lower()
        for group_name, patterns in GROUP_PATTERNS.items():
            for pat in patterns:
                if pat in name_lower:
                    return group_name
        return 'Parameters'


# ═══════════════════════════════════════════════════════════
# 节点创建和连线
# ═══════════════════════════════════════════════════════════

def create_input_nodes_for_custom(
    hlsl_code: str,
    custom_node_x: int = 0,
    custom_node_y: int = 0,
    node_spacing_x: int = 300,
    node_spacing_y: int = 150,
) -> Dict[str, Any]:
    """
    分析 HLSL 代码，生成输入节点信息（包括位置和连线）

    返回:
        {
            'inputs': List[InputVarInfo],
            'nodes': List[dict],       # 需要创建的节点描述
            'connections': List[dict],  # 需要创建的连线描述
            'custom_node_inputs': List[str],  # Custom Node 需要的输入 Pin 名称
        }
    """
    generator = AutoInputGenerator()
    inputs = generator.extract_inputs(hlsl_code=hlsl_code)

    nodes = []
    connections = []
    custom_node_inputs = []

    # 将输入按类型分组排列
    builtins = [i for i in inputs if i.is_builtin]
    textures = [i for i in inputs if i.is_texture]
    params = [i for i in inputs if not i.is_builtin and not i.is_texture]

    # 排列顺序：先参数，再纹理，最后内置变量
    ordered = params + textures + builtins

    for idx, inp in enumerate(ordered):
        # 计算节点位置（在 Custom Node 左侧）
        pos_x = custom_node_x - node_spacing_x
        pos_y = custom_node_y + idx * node_spacing_y

        if inp.is_builtin:
            # 内置变量节点
            node_info = {
                'name': inp.name,
                'ue_class': inp.ue_class,
                'display_name': inp.display_name,
                'pos_x': pos_x,
                'pos_y': pos_y,
                'param_type': 'builtin',
                'properties': {},
            }
        elif inp.is_texture:
            # 纹理参数节点
            node_info = {
                'name': inp.name,
                'ue_class': 'MaterialExpressionTextureObjectParameter',
                'display_name': inp.name,
                'pos_x': pos_x - node_spacing_x,  # 纹理对象再往左一级
                'pos_y': pos_y,
                'param_type': 'texture',
                'properties': {'ParameterName': inp.name},
            }
        elif inp.dimension == 1:
            # 标量参数节点
            node_info = {
                'name': inp.name,
                'ue_class': 'MaterialExpressionScalarParameter',
                'display_name': inp.name,
                'pos_x': pos_x,
                'pos_y': pos_y,
                'param_type': 'scalar',
                'properties': {
                    'ParameterName': inp.name,
                    'DefaultValue': inp.default_value or 0.0,
                },
            }
        else:
            # 向量参数节点
            node_info = {
                'name': inp.name,
                'ue_class': 'MaterialExpressionVectorParameter',
                'display_name': inp.name,
                'pos_x': pos_x,
                'pos_y': pos_y,
                'param_type': 'vector',
                'properties': {
                    'ParameterName': inp.name,
                    'DefaultValue': inp.default_value or {'R': 0, 'G': 0, 'B': 0, 'A': 1},
                },
            }

        if inp.group:
            node_info['group'] = inp.group

        nodes.append(node_info)

        # 如果不是内置变量，需要在 Custom Node 上创建输入 Pin
        # 内置变量也需要输入 Pin（Custom Node 通过 Input Pin 接收数据）
        custom_node_inputs.append(inp.name)

        # 连线信息
        connections.append({
            'source_node': inp.name,
            'source_output': '',  # 默认输出
            'target_input': inp.name,  # Custom Node 的输入 Pin 名称
        })

    return {
        'inputs': inputs,
        'nodes': nodes,
        'connections': connections,
        'custom_node_inputs': custom_node_inputs,
    }


def generate_input_nodes_ue4_code(
    hlsl_code: str,
    custom_node_var: str = 'custom_node',
    mat_var: str = 'mat',
    indent: str = '    ',
) -> str:
    """
    生成在 UE4 Python 环境中自动创建输入节点并连线的代码

    参数:
        hlsl_code: HLSL 代码
        custom_node_var: Custom Node 的 Python 变量名
        mat_var: 材质的 Python 变量名
        indent: 缩进字符串

    返回:
        UE4 Python 脚本代码字符串
    """
    result = create_input_nodes_for_custom(hlsl_code)
    lines = []

    lines.append(f'{indent}# ── 自动创建输入节点 ──')
    lines.append(f'{indent}# 输入变量: {[n["name"] for n in result["nodes"]]}')
    lines.append(f'{indent}')

    # 设置 Custom Node 的输入 Pin
    if result['custom_node_inputs']:
        lines.append(f'{indent}# 配置 Custom Node 输入 Pins')
        lines.append(f'{indent}_custom_inputs = []')
        for pin_name in result['custom_node_inputs']:
            lines.append(f'{indent}_inp = unreal.CustomInput()')
            lines.append(f'{indent}_inp.input_name = "{pin_name}"')
            lines.append(f'{indent}_custom_inputs.append(_inp)')
        lines.append(f'{indent}{custom_node_var}.set_editor_property("inputs", _custom_inputs)')
        lines.append(f'{indent}')

    # 创建各输入节点
    node_vars = {}
    for node_info in result['nodes']:
        name = node_info['name']
        var_name = _safe_var_name(name)
        node_vars[name] = var_name
        ue_class = node_info['ue_class']
        pos_x = node_info['pos_x']
        pos_y = node_info['pos_y']

        # 通用导入映射
        from ue4_codegen import UE_CLASS_MAP
        py_class = UE_CLASS_MAP.get(ue_class, f'unreal.{ue_class}')

        lines.append(f'{indent}# 输入: {name} ({node_info["param_type"]})')
        lines.append(f'{indent}{var_name} = unreal.MaterialEditingLibrary.create_material_expression(')
        lines.append(f'{indent}    {mat_var}, {py_class}, {pos_x}, {pos_y}')
        lines.append(f'{indent})')

        # 设置属性
        props = node_info.get('properties', {})
        if 'ParameterName' in props:
            lines.append(f'{indent}{var_name}.set_editor_property("parameter_name", "{props["ParameterName"]}")')
        if 'DefaultValue' in props:
            val = props['DefaultValue']
            if isinstance(val, (int, float)):
                lines.append(f'{indent}{var_name}.set_editor_property("default_value", {val})')
        lines.append(f'{indent}')

    # 连线
    if result['connections']:
        lines.append(f'{indent}# ── 连接输入节点到 Custom Node ──')
        for idx, conn in enumerate(result['connections']):
            src_name = conn['source_node']
            src_var = node_vars.get(src_name, '_unknown')
            pin_name = conn['target_input']
            lines.append(
                f'{indent}safe_connect({mat_var}, {src_var}, "", '
                f'{custom_node_var}, "{pin_name}")  # {src_name} → Custom.{pin_name}'
            )
        lines.append(f'{indent}')

    return '\n'.join(lines)


def _safe_var_name(name: str) -> str:
    """将变量名转换为安全的 Python 变量名"""
    safe = re.sub(r'[^a-zA-Z0-9_]', '_', name)
    if safe[0].isdigit():
        safe = 'v_' + safe
    return f'input_{safe}'


# ═══════════════════════════════════════════════════════════
# 集成到 MaterialGraph 的辅助函数
# ═══════════════════════════════════════════════════════════

def auto_create_inputs_for_graph(
    graph: MaterialGraph,
    hlsl_code: str,
    custom_node_x: int = 0,
    custom_node_y: int = 0,
) -> MaterialGraph:
    """
    为已有的 MaterialGraph 自动添加输入节点并连线到 Custom Node

    如果 graph 中已有 Custom Node，会创建对应的输入参数节点
    并将它们连接到 Custom Node 的输入引脚。

    参数:
        graph: 现有的材质节点图
        hlsl_code: HLSL 源代码
        custom_node_x: Custom Node 的 X 坐标
        custom_node_y: Custom Node 的 Y 坐标

    返回:
        更新后的 MaterialGraph
    """
    generator = AutoInputGenerator()
    inputs = generator.extract_inputs(hlsl_code=hlsl_code)

    next_id = graph._next_id

    # 查找 graph 中的 Custom Node（如果存在）
    custom_node = None
    for n in graph.nodes:
        if n.ue_class == 'MaterialExpressionCustom':
            custom_node = n
            break

    # 记录新创建的输入节点，用于后续连线
    new_input_nodes = []

    for idx, inp in enumerate(inputs):
        # 如果已有同名节点，跳过创建但仍记录用于连线
        if inp.name in graph.input_nodes:
            new_input_nodes.append((inp, graph.input_nodes[inp.name]))
            continue

        next_id += 1
        pos_x = custom_node_x - 300
        pos_y = custom_node_y + idx * 150

        if inp.is_builtin:
            node = MaterialNode(
                id=next_id,
                ue_class=inp.ue_class,
                display_name=inp.display_name,
                pos_x=pos_x,
                pos_y=pos_y,
            )
        elif inp.is_texture:
            node = MaterialNode(
                id=next_id,
                ue_class='MaterialExpressionTextureObjectParameter',
                display_name=inp.name,
                pos_x=pos_x,
                pos_y=pos_y,
                properties={'ParameterName': inp.name},
            )
        elif inp.dimension == 1:
            node = MaterialNode(
                id=next_id,
                ue_class='MaterialExpressionScalarParameter',
                display_name=inp.name,
                pos_x=pos_x,
                pos_y=pos_y,
                properties={
                    'ParameterName': inp.name,
                    'DefaultValue': inp.default_value or 0.0,
                },
            )
        else:
            node = MaterialNode(
                id=next_id,
                ue_class='MaterialExpressionVectorParameter',
                display_name=inp.name,
                pos_x=pos_x,
                pos_y=pos_y,
                properties={
                    'ParameterName': inp.name,
                    'DefaultValue': inp.default_value or {'R': 0, 'G': 0, 'B': 0, 'A': 1},
                },
            )

        graph.nodes.append(node)
        graph.input_nodes[inp.name] = node
        new_input_nodes.append((inp, node))

    graph._next_id = next_id

    # 将输入节点连线到 Custom Node
    if custom_node and new_input_nodes:
        # 收集所有需要添加到 Custom Node 的输入 pin 名称
        existing_input_names = set(custom_node.inputs.keys()) | set(custom_node.input_names)

        for inp, node in new_input_nodes:
            pin_name = inp.name
            if pin_name not in existing_input_names:
                custom_node.input_names.append(pin_name)
                existing_input_names.add(pin_name)
            # 建立连接：Custom Node 的输入 pin → 参数节点
            custom_node.inputs[pin_name] = node

        # 更新 Custom Node 的 Inputs 属性（T3D 生成需要）
        inputs_info = []
        for iname in custom_node.input_names:
            inputs_info.append({'InputName': iname})
        # 也包含之前 inputs dict 中已有的 key
        for iname in custom_node.inputs:
            if iname not in custom_node.input_names:
                inputs_info.append({'InputName': iname})
        if inputs_info:
            custom_node.properties['Inputs'] = inputs_info

    return graph
