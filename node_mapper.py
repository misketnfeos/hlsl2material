"""
============================================================
 node_mapper.py
 AST → UE4 材质节点中间表示（IR）
============================================================

将 hlsl_parser 生成的 AST 转换为 UE4 材质节点图的中间表示。
每个 HLSL 操作映射到对应的 MaterialExpression 节点类型。

映射规则 (UE4 4.24)：
  ─────────────────────────────────────────────────
  HLSL 操作           →  UE4 MaterialExpression
  ─────────────────────────────────────────────────
  a + b               →  MaterialExpressionAdd
  a - b               →  MaterialExpressionSubtract
  a * b               →  MaterialExpressionMultiply
  a / b               →  MaterialExpressionDivide
  a % b               →  MaterialExpressionFmod
  -x                  →  MaterialExpressionMultiply(-1, x)
  ─────────────────────────────────────────────────
  lerp(a, b, t)       →  MaterialExpressionLinearInterpolate
  saturate(x)         →  MaterialExpressionSaturate
  clamp(x, a, b)      →  MaterialExpressionClamp
  dot(a, b)           →  MaterialExpressionDotProduct
  cross(a, b)         →  MaterialExpressionCrossProduct
  normalize(x)        →  MaterialExpressionNormalize
  pow(a, b)           →  MaterialExpressionPower
  abs(x)              →  MaterialExpressionAbs
  sign(x)             →  MaterialExpressionSign
  floor(x)            →  MaterialExpressionFloor
  ceil(x)             →  MaterialExpressionCeil
  round(x)            →  MaterialExpressionRound
  frac(x)             →  MaterialExpressionFrac
  fmod(a, b)          →  MaterialExpressionFmod
  sqrt(x)             →  MaterialExpressionSquareRoot
  sin(x)              →  MaterialExpressionSine
  cos(x)              →  MaterialExpressionCosine
  min(a, b)           →  MaterialExpressionMin
  max(a, b)           →  MaterialExpressionMax
  step(a, b)          →  MaterialExpressionStep  (custom: 1-step → If节点)
  smoothstep(a, b, x) →  MaterialExpressionSmoothStep
  length(x)           →  MaterialExpressionVectorLength (Distance(x, 0))
  distance(a, b)      →  MaterialExpressionDistance
  reflect(i, n)       →  CustomExpression（UE4没有直接节点）
  tex2D(tex, uv)      →  MaterialExpressionTextureSample
  mul(a, b)           →  MaterialExpressionMultiply (或 Transform)
  ─────────────────────────────────────────────────
  float3(r, g, b)     →  MaterialExpressionAppendVector 链
  float4(r, g, b, a)  →  MaterialExpressionAppendVector 链
  float2(u, v)        →  MaterialExpressionAppendVector
  ─────────────────────────────────────────────────
  .xyz / .rgb         →  MaterialExpressionComponentMask
  .x / .r             →  MaterialExpressionComponentMask
  ─────────────────────────────────────────────────
  a > b ? c : d       →  MaterialExpressionIf
  ─────────────────────────────────────────────────
  数字常量 1.0        →  MaterialExpressionConstant
  数字常量 float3()   →  MaterialExpressionConstant3Vector
  数字常量 float4()   →  MaterialExpressionConstant4Vector
  ─────────────────────────────────────────────────
============================================================
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
from hlsl_parser import (
    ASTNode, HLSLProgram, NumberLiteral, Identifier, BinaryOp, UnaryOp,
    FunctionCall, TypeConstructor, SwizzleAccess, ArrayAccess, TernaryOp,
    Assignment, VarDeclaration, ReturnStatement, ExpressionStatement,
    IfStatement, ForLoop
)


# ═══════════════════════════════════════════════════════════
# 材质节点 IR（中间表示）
# ═══════════════════════════════════════════════════════════

@dataclass
class MaterialNode:
    """材质节点基类"""
    id: int = 0                        # 唯一节点 ID
    ue_class: str = ''                 # UE4 类名 (如 "MaterialExpressionAdd")
    display_name: str = ''             # 显示名称 (如 "Add")
    pos_x: int = 0                     # 节点图中的 X 坐标
    pos_y: int = 0                     # 节点图中的 Y 坐标
    properties: Dict[str, Any] = field(default_factory=dict)  # 额外属性

    # 输入连接
    inputs: Dict[str, Optional['MaterialNode']] = field(default_factory=dict)
    # 输入端口的名称列表（有序）
    input_names: List[str] = field(default_factory=list)
    # 输出端口名称（一般节点只有一个输出）
    output_name: str = 'Output'

    # 源码行号（用于调试）
    source_line: int = 0

    # 原始 T3D 名称（从 T3D 导入时保留，用于引用重映射）
    _t3d_graph_name: str = ''   # e.g. "MaterialGraphNode_9"
    _t3d_expr_name: str = ''    # e.g. "MaterialExpressionFunctionInput_2"


@dataclass
class MaterialGraph:
    """材质节点图"""
    nodes: List[MaterialNode] = field(default_factory=list)
    # 输出节点（连接到材质属性的最终节点）
    output_node: Optional[MaterialNode] = None
    # 输入参数节点
    input_nodes: Dict[str, MaterialNode] = field(default_factory=dict)
    # 不可转换的部分
    custom_expressions: List[str] = field(default_factory=list)
    # 警告信息
    warnings: List[str] = field(default_factory=list)
    # 变量→节点的映射（用于处理赋值和引用）
    _var_map: Dict[str, MaterialNode] = field(default_factory=dict)
    # 节点 ID 计数器
    _next_id: int = 0

    def auto_create_inputs(self, hlsl_code: str,
                           custom_node_x: int = 0,
                           custom_node_y: int = 0) -> 'MaterialGraph':
        """
        自动为材质图创建输入节点

        从 HLSL 代码中提取外部输入变量，创建对应的参数节点
        （ScalarParameter / VectorParameter / TextureObject / 引擎内置变量），
        并添加到 graph 中。

        参数:
            hlsl_code: HLSL 代码字符串
            custom_node_x: Custom Node 的 X 坐标（输入节点排列在其左侧）
            custom_node_y: Custom Node 的 Y 坐标

        返回:
            self（支持链式调用）
        """
        from auto_input_generator import auto_create_inputs_for_graph
        auto_create_inputs_for_graph(self, hlsl_code, custom_node_x, custom_node_y)
        return self

    def to_hlsl(self) -> str:
        """将材质节点图反向转换为 HLSL 代码
        
        使用 reverse_converter 模块遍历节点图拓扑，
        反向映射每个 MaterialExpression 到 HLSL 操作。
        
        返回:
            HLSL 代码字符串
        """
        from reverse_converter import ReverseConverter
        converter = ReverseConverter(self)
        return converter.convert()


# ═══════════════════════════════════════════════════════════
# HLSL 函数 → UE4 节点映射表
# ═══════════════════════════════════════════════════════════

# (ue_class, display_name, input_names)
FUNCTION_MAP = {
    # 插值/混合
    'lerp':       ('MaterialExpressionLinearInterpolate', 'Lerp',        ['A', 'B', 'Alpha']),
    # 钳制
    'saturate':   ('MaterialExpressionSaturate',          'Saturate',    ['Input']),
    'clamp':      ('MaterialExpressionClamp',             'Clamp',       ['Input', 'Min', 'Max']),
    # 向量运算
    'dot':        ('MaterialExpressionDotProduct',        'Dot',         ['A', 'B']),
    'cross':      ('MaterialExpressionCrossProduct',      'Cross',       ['A', 'B']),
    'normalize':  ('MaterialExpressionNormalize',         'Normalize',   ['VectorInput']),
    # 数学函数
    'pow':        ('MaterialExpressionPower',             'Power',       ['Base', 'Exponent']),
    'abs':        ('MaterialExpressionAbs',               'Abs',         ['Input']),
    'sign':       ('MaterialExpressionSign',              'Sign',        ['Input']),
    'floor':      ('MaterialExpressionFloor',             'Floor',       ['Input']),
    'ceil':       ('MaterialExpressionCeil',              'Ceil',        ['Input']),
    'round':      ('MaterialExpressionRound',             'Round',       ['Input']),
    'frac':       ('MaterialExpressionFrac',              'Frac',        ['Input']),
    'fmod':       ('MaterialExpressionFmod',              'Fmod',        ['A', 'B']),
    'sqrt':       ('MaterialExpressionSquareRoot',        'Sqrt',        ['Input']),
    'rsqrt':      ('MaterialExpressionSquareRoot',        '1/Sqrt',      ['Input']),  # 需要额外 1/x
    # exp/exp2 — 当前引擎中 MaterialExpressionExponential/Exponential2 不存在
    # 使用 CustomExpression 回退
    'exp':        ('MaterialExpressionCustom',            'exp(x)',      ['Input']),
    'exp2':       ('MaterialExpressionCustom',            'exp2(x)',     ['Input']),
    'log':        ('MaterialExpressionLogarithm10',       'Log10',       ['Input']),
    'log2':       ('MaterialExpressionLogarithm2',        'Log2',        ['Input']),
    # 三角函数（UE4 内部是 0~1 映射）
    'sin':        ('MaterialExpressionSine',              'Sine',        ['Input']),
    'cos':        ('MaterialExpressionCosine',            'Cosine',      ['Input']),
    'tan':        ('MaterialExpressionTangent',           'Tangent',     ['Input']),
    'asin':       ('MaterialExpressionArcsine',           'Arcsine',     ['Input']),
    'acos':       ('MaterialExpressionArccosine',         'Arccosine',   ['Input']),
    'atan':       ('MaterialExpressionArctangent',        'Arctangent',  ['Input']),
    'atan2':      ('MaterialExpressionArctangent2',       'Arctangent2', ['Y', 'X']),
    # Min/Max
    'min':        ('MaterialExpressionMin',               'Min',         ['A', 'B']),
    'max':        ('MaterialExpressionMax',               'Max',         ['A', 'B']),
    # Step / SmoothStep
    # SmoothStep: 在 _convert_function_call 中用原生节点组合实现 (Subtract/Divide/Saturate/Multiply)
    #   不使用 MaterialFunctionCall，因为 T3D 粘贴时 FunctionInputs 数量不匹配会导致引擎崩溃
    # Step: 使用 If 节点模拟 step(edge, x) = x >= edge ? 1 : 0
    'smoothstep': ('MaterialExpressionMultiply', 'SmoothStep', []),  # 占位，实际由特殊处理分支实现
    'step':       ('MaterialExpressionIf',                   'Step',       ['A', 'B', 'A > B', 'A == B', 'A < B']),
    # 向量长度/距离
    # VectorLength 在当前引擎不存在 → 回退为 Distance(x, 0)，在 _convert_function_call 中特殊处理
    'length':     ('MaterialExpressionDistance',           'Length→Distance', ['A', 'B']),
    'distance':   ('MaterialExpressionDistance',          'Distance',    ['A', 'B']),
    # 反射
    'reflect':    ('MaterialExpressionReflectionVector',  'Reflect',     ['Input', 'Normal']),
    'refract':    ('MaterialExpressionCustom',            'Refract(Custom)', ['Input']),
    # DDX/DDY
    'ddx':        ('MaterialExpressionDDX',               'DDX',         ['Value']),
    'ddy':        ('MaterialExpressionDDY',               'DDY',         ['Value']),
    # 纹理采样
    'tex2D':           ('MaterialExpressionTextureSample', 'TextureSample', ['UVs']),
    'Texture2DSample': ('MaterialExpressionTextureSample', 'TextureSample', ['UVs']),
    # 矩阵乘法
    'mul':        ('MaterialExpressionMultiply',          'Multiply',    ['A', 'B']),
}

# 需要特殊处理的 CustomExpression 函数 — 存储其 HLSL 代码模板
# UE4 CustomExpression 中用形参名（即 Input0, Input1 等）引用输入
CUSTOM_EXPR_CODE = {
    'exp':        'return exp(Input0);',
    'exp2':       'return exp2(Input0);',
}

# 引擎内置 MaterialFunction 路径映射
# 这些函数在引擎中有对应的 MaterialFunctionCall 节点
MATERIAL_FUNCTION_MAP = {
    'smoothstep': '/Engine/Functions/Engine_MaterialFunctions02/SmoothStep.SmoothStep',
}

# 二元运算符 → UE4 节点映射
BINARY_OP_MAP = {
    '+':  ('MaterialExpressionAdd',       'Add',      ['A', 'B']),
    '-':  ('MaterialExpressionSubtract',  'Subtract', ['A', 'B']),
    '*':  ('MaterialExpressionMultiply',  'Multiply', ['A', 'B']),
    '/':  ('MaterialExpressionDivide',    'Divide',   ['A', 'B']),
    '%':  ('MaterialExpressionFmod',      'Fmod',     ['A', 'B']),
}

# Swizzle 分量 → ComponentMask 属性
SWIZZLE_MAP = {
    'x': 'R', 'y': 'G', 'z': 'B', 'w': 'A',
    'r': 'R', 'g': 'G', 'b': 'B', 'a': 'A',
    's': 'R', 't': 'G', 'p': 'B', 'q': 'A',
}

# ═══════════════════════════════════════════════════════════
# UE4 引擎内置变量 → 原生节点映射
# ═══════════════════════════════════════════════════════════
# 这些变量在 HLSL 中常用，但在 UE4 材质编辑器中
# 应该用引擎自带的表达式节点，而不是创建新的参数节点。

ENGINE_BUILTIN_VARS = {
    # 相机/世界位置
    'CameraPosition':       ('MaterialExpressionCameraPositionWS',    'CameraPos (WS)',     []),
    'CameraWorldPosition':  ('MaterialExpressionCameraPositionWS',    'CameraPos (WS)',     []),
    'CamPos':               ('MaterialExpressionCameraPositionWS',    'CameraPos (WS)',     []),
    'WorldPosition':        ('MaterialExpressionWorldPosition',       'WorldPos',           []),
    'WorldPos':             ('MaterialExpressionWorldPosition',       'WorldPos',           []),
    'AbsoluteWorldPosition':('MaterialExpressionWorldPosition',       'WorldPos (Abs)',     []),
    # 法线
    'Normal':               ('MaterialExpressionPixelNormalWS',       'PixelNormal (WS)',   []),
    'WorldNormal':          ('MaterialExpressionPixelNormalWS',       'PixelNormal (WS)',   []),
    'PixelNormal':          ('MaterialExpressionPixelNormalWS',       'PixelNormal (WS)',   []),
    'VertexNormal':         ('MaterialExpressionVertexNormalWS',      'VertexNormal (WS)',  []),
    'VertexNormalWS':       ('MaterialExpressionVertexNormalWS',      'VertexNormal (WS)',  []),
    # 视线方向
    'ViewDir':              ('MaterialExpressionCameraVectorWS',      'CameraVector (WS)',  []),
    'CameraVector':         ('MaterialExpressionCameraVectorWS',      'CameraVector (WS)',  []),
    'CameraDir':            ('MaterialExpressionCameraVectorWS',      'CameraVector (WS)',  []),
    # UV 坐标
    'UV':                   ('MaterialExpressionTextureCoordinate',    'TexCoord',           []),
    'TexCoord':             ('MaterialExpressionTextureCoordinate',    'TexCoord',           []),
    'TexCoord0':            ('MaterialExpressionTextureCoordinate',    'TexCoord[0]',        []),
    # 时间
    'Time':                 ('MaterialExpressionTime',                'Time',               []),
    '_Time':                ('MaterialExpressionTime',                'Time',               []),
    # 屏幕位置
    'ScreenPosition':       ('MaterialExpressionScreenPosition',      'ScreenPos',          []),
    'ScreenUV':             ('MaterialExpressionScreenPosition',      'ScreenPos',          []),
    # 顶点颜色
    'VertexColor':          ('MaterialExpressionVertexColor',         'VertexColor',        []),
    # 反射向量
    'ReflectionVector':     ('MaterialExpressionReflectionVectorWS',  'ReflectionVec (WS)', []),
    # 物体位置
    'ObjectPosition':       ('MaterialExpressionObjectPositionWS',    'ObjectPos (WS)',     []),
    'ActorPosition':        ('MaterialExpressionActorPositionWS',     'ActorPos (WS)',      []),
    # 像素深度
    'PixelDepth':           ('MaterialExpressionPixelDepth',          'PixelDepth',         []),
    'SceneDepth':           ('MaterialExpressionSceneDepth',          'SceneDepth',         []),
}

# ═══════════════════════════════════════════════════════════
# HLSL 类型 → 参数维度推断
# ═══════════════════════════════════════════════════════════
# 根据变量声明的类型来决定用 ScalarParameter 还是 VectorParameter

TYPE_DIMENSION_MAP = {
    'float':  1,
    'half':   1,
    'int':    1,
    'bool':   1,
    'float2': 2,
    'half2':  2,
    'int2':   2,
    'float3': 3,
    'half3':  3,
    'int3':   3,
    'float4': 4,
    'half4':  4,
    'int4':   4,
}


# ═══════════════════════════════════════════════════════════
# AST → MaterialGraph 转换器
# ═══════════════════════════════════════════════════════════

class NodeMapper:
    """将 HLSL AST 转换为 UE4 材质节点图"""

    def __init__(self):
        self.graph = MaterialGraph()
        # 跟踪变量声明类型 → 用于推断参数维度
        self._var_types: Dict[str, int] = {}  # var_name → dimension (1/2/3/4)

    def _new_id(self) -> int:
        self.graph._next_id += 1
        return self.graph._next_id

    def _make_node(self, ue_class: str, display_name: str,
                   input_names: List[str] = None,
                   properties: Dict = None,
                   source_line: int = 0) -> MaterialNode:
        """创建一个新的材质节点"""
        node = MaterialNode(
            id=self._new_id(),
            ue_class=ue_class,
            display_name=display_name,
            input_names=input_names or [],
            properties=properties or {},
            source_line=source_line,
        )
        self.graph.nodes.append(node)
        return node

    def _make_constant(self, value: float, line: int = 0) -> MaterialNode:
        """创建常量节点"""
        node = self._make_node(
            'MaterialExpressionConstant', f'Const({value})',
            properties={'R': value},
            source_line=line,
        )
        return node

    def _make_constant3(self, r: float, g: float, b: float, line: int = 0) -> MaterialNode:
        """创建 Constant3Vector 节点"""
        node = self._make_node(
            'MaterialExpressionConstant3Vector', f'Const3({r},{g},{b})',
            properties={'Constant': {'R': r, 'G': g, 'B': b}},
            source_line=line,
        )
        return node

    def _make_constant4(self, r: float, g: float, b: float, a: float, line: int = 0) -> MaterialNode:
        """创建 Constant4Vector 节点"""
        node = self._make_node(
            'MaterialExpressionConstant4Vector', f'Const4({r},{g},{b},{a})',
            properties={'Constant': {'R': r, 'G': g, 'B': b, 'A': a}},
            source_line=line,
        )
        return node

    def _make_param(self, name: str, line: int = 0, dimension: int = 0) -> MaterialNode:
        """创建输入参数节点（根据维度自动选择 Scalar/Vector Parameter）
        
        dimension: 0=自动推断(默认 float3), 1=scalar, 2=vec2, 3=vec3, 4=vec4, -1=texture
        """
        if name in self.graph.input_nodes:
            return self.graph.input_nodes[name]

        # 先检查是否是引擎内置变量
        if name in ENGINE_BUILTIN_VARS:
            return self._make_builtin_var(name, line)

        # 检查是否是纹理参数
        if dimension == -1 or self._is_texture_param(name):
            return self._make_texture_param(name, line)

        # 根据维度选择参数类型
        if dimension <= 1 and dimension != 0:
            # 标量参数
            node = self._make_node(
                'MaterialExpressionScalarParameter', f'{name}',
                properties={'ParameterName': name, 'DefaultValue': 0.0},
                source_line=line,
            )
        else:
            # 向量参数（默认 float3/float4）
            node = self._make_node(
                'MaterialExpressionVectorParameter', f'{name}',
                properties={'ParameterName': name, 'DefaultValue': {'R': 0, 'G': 0, 'B': 0, 'A': 1}},
                source_line=line,
            )
        self.graph.input_nodes[name] = node
        return node

    def _is_texture_param(self, name: str) -> bool:
        """判断变量名是否表示纹理参数"""
        name_lower = name.lower()
        texture_patterns = ['tex', 'texture', 'map', 'sampler']
        for pat in texture_patterns:
            if pat in name_lower:
                return True
        return False

    def _make_texture_param(self, name: str, line: int = 0) -> MaterialNode:
        """创建纹理对象参数节点 (TextureObjectParameter)
        
        在 UE4 中，纹理采样的正确结构是：
        TextureObjectParameter (Tex pin) → TextureSample ← TextureCoordinate (UVs pin)
        
        这里只创建 TextureObjectParameter 节点本身，
        tex2D() 函数调用时会创建 TextureSample 并连接。
        """
        if name in self.graph.input_nodes:
            return self.graph.input_nodes[name]

        node = self._make_node(
            'MaterialExpressionTextureObjectParameter', f'{name}',
            properties={'ParameterName': name},
            source_line=line,
        )
        self.graph.input_nodes[name] = node
        return node

    def _make_builtin_var(self, name: str, line: int = 0) -> MaterialNode:
        """创建 UE4 引擎内置变量节点"""
        if name in self.graph.input_nodes:
            return self.graph.input_nodes[name]

        ue_class, display_name, input_names = ENGINE_BUILTIN_VARS[name]
        node = self._make_node(
            ue_class, display_name,
            input_names=input_names,
            source_line=line,
        )
        self.graph.input_nodes[name] = node
        return node

    # ── 主转换入口 ──

    def convert(self, program: HLSLProgram) -> MaterialGraph:
        """将 AST 转换为材质节点图"""
        self.graph.warnings.extend(
            getattr(program, '_warnings', [])
        )

        for stmt in program.statements:
            self._convert_statement(stmt)

        return self.graph

    def _convert_statement(self, stmt: ASTNode):
        """转换语句"""
        if isinstance(stmt, VarDeclaration):
            # 记录变量类型维度
            dim = TYPE_DIMENSION_MAP.get(stmt.type_name, 0)
            if dim:
                self._var_types[stmt.var_name] = dim
            if stmt.initializer:
                node = self._convert_expr(stmt.initializer)
                self.graph._var_map[stmt.var_name] = node
            else:
                # 无初始值的声明，创建占位
                node = self._make_constant(0.0, stmt.line)
                self.graph._var_map[stmt.var_name] = node

        elif isinstance(stmt, ReturnStatement):
            if stmt.value:
                node = self._convert_expr(stmt.value)
                self.graph.output_node = node

        elif isinstance(stmt, ExpressionStatement):
            if isinstance(stmt.expression, Assignment):
                self._convert_assignment(stmt.expression)
            else:
                self._convert_expr(stmt.expression)

        elif isinstance(stmt, IfStatement):
            self._convert_if_statement(stmt)

        elif isinstance(stmt, ForLoop):
            self.graph.custom_expressions.append(stmt.raw_code)
            self.graph.warnings.append(f"行 {stmt.line}: 循环结构保留为 CustomExpression")

    def _convert_assignment(self, assign: Assignment):
        """转换赋值表达式"""
        value_node = self._convert_expr(assign.value)

        if assign.op == '=':
            # 简单赋值
            if isinstance(assign.target, Identifier):
                self.graph._var_map[assign.target.name] = value_node
            elif isinstance(assign.target, SwizzleAccess):
                # swizzle 赋值比较复杂，这里简化处理
                if isinstance(assign.target.object, Identifier):
                    self.graph._var_map[assign.target.object.name] = value_node
                    self.graph.warnings.append(
                        f"行 {assign.line}: Swizzle 赋值 .{assign.target.components} 已简化处理"
                    )
        elif assign.op in ('+=', '-=', '*=', '/='):
            # 复合赋值: x += y → x = x + y
            op_char = assign.op[0]  # +, -, *, /
            if op_char in BINARY_OP_MAP:
                ue_class, display, inputs = BINARY_OP_MAP[op_char]
                target_node = self._convert_expr(assign.target)
                op_node = self._make_node(
                    ue_class, display, inputs,
                    source_line=assign.line,
                )
                op_node.inputs = {inputs[0]: target_node, inputs[1]: value_node}
                if isinstance(assign.target, Identifier):
                    self.graph._var_map[assign.target.name] = op_node

    def _convert_if_statement(self, stmt: IfStatement):
        """
        转换 if/else 为 If 节点
        if (a > b) { return c; } else { return d; }
        → MaterialExpressionIf(A>B, AGreaterThanB=c, ALessThanB=d)
        """
        # 简单情况：if/else 各有一条 return 语句
        if (len(stmt.then_body) == 1 and len(stmt.else_body) == 1 and
            isinstance(stmt.then_body[0], ReturnStatement) and
            isinstance(stmt.else_body[0], ReturnStatement)):

            cond_node = self._convert_expr(stmt.condition)
            then_node = self._convert_expr(stmt.then_body[0].value)
            else_node = self._convert_expr(stmt.else_body[0].value)

            # 尝试提取比较操作
            if isinstance(stmt.condition, BinaryOp) and stmt.condition.op in ('>', '<', '>=', '<=', '=='):
                a_node = self._convert_expr(stmt.condition.left)
                b_node = self._convert_expr(stmt.condition.right)

                if_node = self._make_node(
                    'MaterialExpressionIf', 'If',
                    ['A', 'B', 'A > B', 'A == B', 'A < B'],
                    source_line=stmt.line,
                )
                if stmt.condition.op in ('>', '>='):
                    if_node.inputs = {
                        'A': a_node, 'B': b_node,
                        'A > B': then_node, 'A == B': then_node, 'A < B': else_node,
                    }
                elif stmt.condition.op in ('<', '<='):
                    if_node.inputs = {
                        'A': a_node, 'B': b_node,
                        'A > B': else_node, 'A == B': else_node, 'A < B': then_node,
                    }
                elif stmt.condition.op == '==':
                    if_node.inputs = {
                        'A': a_node, 'B': b_node,
                        'A > B': else_node, 'A == B': then_node, 'A < B': else_node,
                    }

                self.graph.output_node = if_node
                return

        # 复杂 if：逐条处理
        self.graph.warnings.append(
            f"行 {stmt.line}: 复杂 if/else 结构已尽力转换"
        )
        for s in stmt.then_body:
            self._convert_statement(s)
        for s in stmt.else_body:
            self._convert_statement(s)

    # ── 表达式转换 ──

    def _convert_expr(self, expr: ASTNode) -> MaterialNode:
        """将 AST 表达式转换为材质节点，返回输出节点"""
        if isinstance(expr, NumberLiteral):
            return self._make_constant(expr.value, expr.line)

        elif isinstance(expr, Identifier):
            return self._convert_identifier(expr)

        elif isinstance(expr, BinaryOp):
            return self._convert_binary_op(expr)

        elif isinstance(expr, UnaryOp):
            return self._convert_unary_op(expr)

        elif isinstance(expr, FunctionCall):
            return self._convert_function_call(expr)

        elif isinstance(expr, TypeConstructor):
            return self._convert_type_constructor(expr)

        elif isinstance(expr, SwizzleAccess):
            return self._convert_swizzle(expr)

        elif isinstance(expr, ArrayAccess):
            return self._convert_array_access(expr)

        elif isinstance(expr, TernaryOp):
            return self._convert_ternary(expr)

        elif isinstance(expr, Assignment):
            self._convert_assignment(expr)
            if isinstance(expr.target, Identifier) and expr.target.name in self.graph._var_map:
                return self.graph._var_map[expr.target.name]
            return self._make_constant(0.0, expr.line)

        else:
            self.graph.warnings.append(f"未知表达式类型: {type(expr).__name__}")
            return self._make_constant(0.0)

    def _convert_identifier(self, ident: Identifier) -> MaterialNode:
        """转换标识符（变量引用、内置变量或输入参数）"""
        # 先查已声明的变量
        if ident.name in self.graph._var_map:
            return self.graph._var_map[ident.name]
        # 检查是否是 UE4 引擎内置变量
        if ident.name in ENGINE_BUILTIN_VARS:
            return self._make_builtin_var(ident.name, ident.line)
        # 检查是否是显式输入参数（来自 Custom Node 的输入 pin 名）
        if hasattr(self, '_explicit_inputs') and ident.name in self._explicit_inputs:
            return self._make_param(ident.name, ident.line, dimension=0)
        # 未声明 → 视为输入参数，尝试推断维度
        dimension = self._guess_param_dimension(ident.name)
        return self._make_param(ident.name, ident.line, dimension)

    def _guess_param_dimension(self, name: str) -> int:
        """根据变量名模式推断参数维度
        
        常见命名约定：
          - 以 Color/Colour 结尾 → float3/float4 (向量)
          - 以 Power/Intensity/Strength/Amount/Width/Scale/Speed/Offset/Bias 结尾 → float (标量)
          - 以 Tex/Texture 结尾 → 纹理（特殊处理）
          - 其它默认 float3
        """
        name_lower = name.lower()
        
        # 明确的标量名模式
        scalar_suffixes = [
            'power', 'intensity', 'strength', 'amount', 'width', 'height',
            'scale', 'speed', 'offset', 'bias', 'factor', 'ratio', 'threshold',
            'radius', 'size', 'alpha', 'opacity', 'metallic', 'roughness',
            'specular', 'exponent', 'weight', 'blend', 'value', 'time',
            'distance', 'depth', 'angle', 'frequency', 'amplitude',
            'min', 'max', 'step', 'tiling', 'density',
            'thickness', 'attenuation', 'falloff', 'hardness', 'softness',
            'contrast', 'saturation', 'brightness', 'gamma', 'exposure',
        ]
        for suffix in scalar_suffixes:
            if name_lower.endswith(suffix) or name_lower == suffix:
                return 1
        
        # 明确的向量名模式
        vector_patterns = ['color', 'colour', 'pos', 'position', 'dir', 'direction',
                           'normal', 'tangent', 'bitangent', 'vec', 'rgb']
        for pat in vector_patterns:
            if pat in name_lower:
                return 3
        
        # UV 坐标
        if 'uv' in name_lower:
            return 2
        
        # 默认按 float3
        return 3


    def _convert_binary_op(self, expr: BinaryOp) -> MaterialNode:
        """转换二元运算"""
        left_node = self._convert_expr(expr.left)
        right_node = self._convert_expr(expr.right)
        
        # 特殊处理：1.0 - x → MaterialExpressionOneMinus(x)
        if expr.op == '-':
            if isinstance(expr.left, NumberLiteral) and expr.left.value == 1.0:
                # 1.0 - expr → OneMinus
                node = self._make_node(
                    'MaterialExpressionOneMinus', 'OneMinus(1-x)',
                    ['Input'], source_line=expr.line,
                )
                node.inputs = {'Input': right_node}
                return node

        if expr.op in BINARY_OP_MAP:
            ue_class, display, inputs = BINARY_OP_MAP[expr.op]
            node = self._make_node(ue_class, display, inputs, source_line=expr.line)
            node.inputs = {inputs[0]: left_node, inputs[1]: right_node}
            return node

        # 比较运算符 → 用于 If 节点的条件
        if expr.op in ('>', '<', '>=', '<=', '==', '!='):
            # 比较运算本身不是一个独立节点
            # 在三元运算或 if 语句中会被上层处理
            # 这里作为后备，生成 If(A, B, 1, 0) 来模拟布尔值
            if_node = self._make_node(
                'MaterialExpressionIf', f'Compare({expr.op})',
                ['A', 'B', 'A > B', 'A == B', 'A < B'],
                source_line=expr.line,
            )
            one = self._make_constant(1.0, expr.line)
            zero = self._make_constant(0.0, expr.line)

            if expr.op == '>':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': one, 'A == B': zero, 'A < B': zero}
            elif expr.op == '>=':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': one, 'A == B': one, 'A < B': zero}
            elif expr.op == '<':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': zero, 'A == B': zero, 'A < B': one}
            elif expr.op == '<=':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': zero, 'A == B': one, 'A < B': one}
            elif expr.op == '==':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': zero, 'A == B': one, 'A < B': zero}
            elif expr.op == '!=':
                if_node.inputs = {'A': left_node, 'B': right_node, 'A > B': one, 'A == B': zero, 'A < B': one}
            return if_node

        # && / ||：用 Multiply / Max 模拟
        if expr.op == '&&':
            node = self._make_node('MaterialExpressionMultiply', 'AND(Mul)', ['A', 'B'], source_line=expr.line)
            node.inputs = {'A': left_node, 'B': right_node}
            return node
        if expr.op == '||':
            node = self._make_node('MaterialExpressionMax', 'OR(Max)', ['A', 'B'], source_line=expr.line)
            node.inputs = {'A': left_node, 'B': right_node}
            return node

        self.graph.warnings.append(f"行 {expr.line}: 未知运算符 '{expr.op}'")
        return self._make_constant(0.0, expr.line)

    def _convert_unary_op(self, expr: UnaryOp) -> MaterialNode:
        """转换一元运算"""
        operand_node = self._convert_expr(expr.operand)

        if expr.op == '-':
            # -x → Multiply(-1, x)
            neg_one = self._make_constant(-1.0, expr.line)
            node = self._make_node(
                'MaterialExpressionMultiply', 'Negate',
                ['A', 'B'], source_line=expr.line,
            )
            node.inputs = {'A': neg_one, 'B': operand_node}
            return node

        if expr.op == '!':
            # !x → 1 - x (对于 0/1 布尔值)
            one = self._make_constant(1.0, expr.line)
            node = self._make_node(
                'MaterialExpressionOneMinus', 'NOT(1-x)',
                ['Input'], source_line=expr.line,
            )
            node.inputs = {'Input': operand_node}
            return node

        return operand_node

    def _convert_function_call(self, expr: FunctionCall) -> MaterialNode:
        """转换函数调用"""
        func_name = expr.name

        # ── 预处理器标记的复杂函数 → CustomExpression ──
        # __CUSTOM_N__ 是预处理器为含有 for/while/多return 等复杂结构的
        # 函数生成的标记，需要创建完整的 CustomExpression 节点
        import re as _re
        custom_marker = _re.match(r'^__CUSTOM_(\d+)__$', func_name)
        if custom_marker:
            return self._convert_custom_marker_call(expr, int(custom_marker.group(1)))

        # ── 特殊处理: tex2D / Texture2DSample → TextureSample ──
        # 正确结构: TextureObjectParameter → TextureSample.Tex, UV → TextureSample.UVs
        if func_name in ('tex2D', 'Texture2DSample'):
            arg_nodes = [self._convert_expr(arg) for arg in expr.args]
            node = self._make_node(
                'MaterialExpressionTextureSample', 'TextureSample',
                ['UVs', 'Tex'],
                source_line=expr.line,
            )
            # 第一个参数是纹理对象 → 连接到 Tex pin
            # 第二个参数是 UV → 连接到 UVs pin
            if len(arg_nodes) >= 2:
                node.inputs['Tex'] = arg_nodes[0]   # 纹理对象 → Tex pin
                node.inputs['UVs'] = arg_nodes[1]   # UV 坐标 → UVs pin
            elif len(arg_nodes) == 1:
                node.inputs['Tex'] = arg_nodes[0]
            return node

        # ── 特殊处理: smoothstep → 用原生节点组合实现 ──
        # smoothstep(edge0, edge1, x) = t*t*(3-2*t), 其中 t = saturate((x-edge0)/(edge1-edge0))
        # 
        # 注意：不使用 MaterialExpressionMaterialFunctionCall，因为该节点类型
        # 在 T3D 粘贴时需要完整的 FunctionInputs/FunctionOutputs 数组，
        # 格式复杂且容易导致引擎断言失败崩溃：
        #   "Assertion failed: InputPins.Num() == ExpressionInputs.Num()"
        # 使用原生节点组合更可靠。
        if func_name == 'smoothstep':
            arg_nodes = [self._convert_expr(arg) for arg in expr.args]
            if len(arg_nodes) >= 3:
                edge0 = arg_nodes[0]
                edge1 = arg_nodes[1]
                x_val = arg_nodes[2]
            elif len(arg_nodes) == 2:
                edge0 = arg_nodes[0]
                edge1 = arg_nodes[1]
                x_val = self._make_constant(0.0, expr.line)
            else:
                edge0 = self._make_constant(0.0, expr.line)
                edge1 = self._make_constant(1.0, expr.line)
                x_val = arg_nodes[0] if arg_nodes else self._make_constant(0.0, expr.line)
            
            # t = saturate((x - edge0) / (edge1 - edge0))
            # node: x - edge0
            sub_x = self._make_node(
                'MaterialExpressionSubtract', 'SmoothStep_x-e0',
                ['A', 'B'], source_line=expr.line,
            )
            sub_x.inputs = {'A': x_val, 'B': edge0}
            
            # node: edge1 - edge0
            sub_range = self._make_node(
                'MaterialExpressionSubtract', 'SmoothStep_e1-e0',
                ['A', 'B'], source_line=expr.line,
            )
            sub_range.inputs = {'A': edge1, 'B': edge0}
            
            # node: (x - edge0) / (edge1 - edge0)
            div_node = self._make_node(
                'MaterialExpressionDivide', 'SmoothStep_div',
                ['A', 'B'], source_line=expr.line,
            )
            div_node.inputs = {'A': sub_x, 'B': sub_range}
            
            # node: t = saturate(...)
            sat_node = self._make_node(
                'MaterialExpressionSaturate', 'SmoothStep_t',
                ['Input'], source_line=expr.line,
            )
            sat_node.inputs = {'Input': div_node}
            
            # node: 3 - 2*t
            const_2 = self._make_constant(2.0, expr.line)
            const_3 = self._make_constant(3.0, expr.line)
            
            mul_2t = self._make_node(
                'MaterialExpressionMultiply', 'SmoothStep_2t',
                ['A', 'B'], source_line=expr.line,
            )
            mul_2t.inputs = {'A': const_2, 'B': sat_node}
            
            sub_3_2t = self._make_node(
                'MaterialExpressionSubtract', 'SmoothStep_3-2t',
                ['A', 'B'], source_line=expr.line,
            )
            sub_3_2t.inputs = {'A': const_3, 'B': mul_2t}
            
            # node: t * t
            mul_tt = self._make_node(
                'MaterialExpressionMultiply', 'SmoothStep_t*t',
                ['A', 'B'], source_line=expr.line,
            )
            mul_tt.inputs = {'A': sat_node, 'B': sat_node}
            
            # node: t*t * (3-2*t) = 最终结果
            result = self._make_node(
                'MaterialExpressionMultiply', 'SmoothStep',
                ['A', 'B'], source_line=expr.line,
            )
            result.inputs = {'A': mul_tt, 'B': sub_3_2t}
            
            return result

        # ── 特殊处理: step(edge, x) → If 节点模拟 ──
        # step(edge, x) = x >= edge ? 1 : 0
        if func_name == 'step':
            arg_nodes = [self._convert_expr(arg) for arg in expr.args]
            one = self._make_constant(1.0, expr.line)
            zero = self._make_constant(0.0, expr.line)
            if_node = self._make_node(
                'MaterialExpressionIf', 'Step(If)',
                ['A', 'B', 'A > B', 'A == B', 'A < B'],
                source_line=expr.line,
            )
            if len(arg_nodes) >= 2:
                # x >= edge → A=x, B=edge, A>B=1, A==B=1, A<B=0
                if_node.inputs = {
                    'A': arg_nodes[1],   # x
                    'B': arg_nodes[0],   # edge
                    'A > B': one,
                    'A == B': one,
                    'A < B': zero,
                }
            return if_node

        # 查映射表
        if func_name in FUNCTION_MAP:
            ue_class, display, input_names = FUNCTION_MAP[func_name]
            arg_nodes = [self._convert_expr(arg) for arg in expr.args]

            # ── 特殊处理: length(x) → Distance(x, Const(0)) ──
            if func_name == 'length':
                zero = self._make_constant(0.0, expr.line)
                node = self._make_node(ue_class, 'Length', ['A', 'B'], source_line=expr.line)
                if arg_nodes:
                    node.inputs = {'A': arg_nodes[0], 'B': zero}
                return node

            # ── CustomExpression 回退的函数（exp, exp2）──
            if func_name in CUSTOM_EXPR_CODE:
                code_template = CUSTOM_EXPR_CODE[func_name]
                custom_inputs = [f'Input{i}' for i in range(len(arg_nodes))]
                
                node = self._make_node(
                    'MaterialExpressionCustom', func_name,
                    custom_inputs,
                    properties={
                        'Code': code_template,
                        'Description': func_name,  # 设置描述=函数名，编辑器中显示为 "exp"
                    },
                    source_line=expr.line,
                )
                for i, iname in enumerate(custom_inputs):
                    if i < len(arg_nodes):
                        node.inputs[iname] = arg_nodes[i]
                return node

            node = self._make_node(ue_class, display, input_names, source_line=expr.line)

            # 连接输入
            for i, iname in enumerate(input_names):
                if i < len(arg_nodes):
                    node.inputs[iname] = arg_nodes[i]

            # rsqrt 特殊处理：1 / sqrt(x)
            if func_name == 'rsqrt':
                one = self._make_constant(1.0, expr.line)
                div_node = self._make_node(
                    'MaterialExpressionDivide', '1/Sqrt',
                    ['A', 'B'], source_line=expr.line,
                )
                div_node.inputs = {'A': one, 'B': node}
                return div_node

            return node

        # 未知函数 → CustomExpression
        self.graph.warnings.append(
            f"行 {expr.line}: 未知函数 '{func_name}'，将标记为 CustomExpression"
        )
        arg_nodes = [self._convert_expr(arg) for arg in expr.args]
        custom_inputs = [f'Input{i}' for i in range(len(arg_nodes))]
        # 生成有意义的 Code 模板：return func_name(Input0, Input1, ...);
        code_args = ', '.join(custom_inputs)
        code_template = f'return {func_name}({code_args});'
        node = self._make_node(
            'MaterialExpressionCustom', func_name,
            custom_inputs,
            properties={
                'Code': code_template,
                'Description': func_name,  # 在编辑器中显示函数名
            },
            source_line=expr.line,
        )
        for i, iname in enumerate(custom_inputs):
            if i < len(arg_nodes):
                node.inputs[iname] = arg_nodes[i]
        return node

    def _convert_type_constructor(self, expr: TypeConstructor) -> MaterialNode:
        """转换类型构造函数: float3(1,0,0)"""
        arg_nodes = [self._convert_expr(arg) for arg in expr.args]
        type_name = expr.type_name

        # 检测是否全部参数都是常量 → 直接创建 ConstantNVector
        all_const = all(isinstance(a, NumberLiteral) for a in expr.args)

        if type_name in ('float', 'half', 'int') and len(expr.args) == 1:
            return arg_nodes[0]

        if type_name in ('float2', 'half2', 'int2'):
            if all_const and len(expr.args) == 2:
                return self._make_node(
                    'MaterialExpressionConstant2Vector', f'Const2({expr.args[0].value},{expr.args[1].value})',
                    properties={'R': expr.args[0].value, 'G': expr.args[1].value},
                    source_line=expr.line,
                )
            if len(arg_nodes) == 2:
                append = self._make_node(
                    'MaterialExpressionAppendVector', 'Append',
                    ['A', 'B'], source_line=expr.line,
                )
                append.inputs = {'A': arg_nodes[0], 'B': arg_nodes[1]}
                return append
            elif len(arg_nodes) == 1:
                # float2(x) → float2(x, x) ？不太常见
                return arg_nodes[0]

        if type_name in ('float3', 'half3', 'int3'):
            if all_const and len(expr.args) == 3:
                return self._make_constant3(
                    expr.args[0].value, expr.args[1].value, expr.args[2].value,
                    expr.line,
                )
            if len(arg_nodes) == 3:
                # 需要两次 Append: Append(Append(r, g), b)
                append1 = self._make_node(
                    'MaterialExpressionAppendVector', 'Append',
                    ['A', 'B'], source_line=expr.line,
                )
                append1.inputs = {'A': arg_nodes[0], 'B': arg_nodes[1]}
                append2 = self._make_node(
                    'MaterialExpressionAppendVector', 'Append',
                    ['A', 'B'], source_line=expr.line,
                )
                append2.inputs = {'A': append1, 'B': arg_nodes[2]}
                return append2
            elif len(arg_nodes) == 1:
                return arg_nodes[0]

        if type_name in ('float4', 'half4', 'int4'):
            if all_const and len(expr.args) == 4:
                return self._make_constant4(
                    expr.args[0].value, expr.args[1].value,
                    expr.args[2].value, expr.args[3].value,
                    expr.line,
                )
            if len(arg_nodes) == 4:
                a1 = self._make_node('MaterialExpressionAppendVector', 'Append', ['A', 'B'], source_line=expr.line)
                a1.inputs = {'A': arg_nodes[0], 'B': arg_nodes[1]}
                a2 = self._make_node('MaterialExpressionAppendVector', 'Append', ['A', 'B'], source_line=expr.line)
                a2.inputs = {'A': a1, 'B': arg_nodes[2]}
                a3 = self._make_node('MaterialExpressionAppendVector', 'Append', ['A', 'B'], source_line=expr.line)
                a3.inputs = {'A': a2, 'B': arg_nodes[3]}
                return a3
            elif len(arg_nodes) == 2:
                # float4(float3_val, 1.0) → Append(float3, float)
                append = self._make_node(
                    'MaterialExpressionAppendVector', 'Append',
                    ['A', 'B'], source_line=expr.line,
                )
                append.inputs = {'A': arg_nodes[0], 'B': arg_nodes[1]}
                return append
            elif len(arg_nodes) == 1:
                return arg_nodes[0]

        # 后备
        if arg_nodes:
            return arg_nodes[0]
        return self._make_constant(0.0, expr.line)

    def _convert_swizzle(self, expr: SwizzleAccess) -> MaterialNode:
        """转换 Swizzle: color.rgb → ComponentMask"""
        obj_node = self._convert_expr(expr.object)
        components = expr.components

        # 判断是否需要 mask
        if components in ('xyzw', 'rgba', 'stpq'):
            return obj_node  # 全分量，无需 mask

        # ComponentMask
        r = any(c in 'xrs' for c in components)
        g = any(c in 'ygt' for c in components)
        b = any(c in 'zbp' for c in components)
        a = any(c in 'waq' for c in components)

        mask_node = self._make_node(
            'MaterialExpressionComponentMask', f'Mask(.{components})',
            ['Input'],
            properties={'R': r, 'G': g, 'B': b, 'A': a},
            source_line=expr.line,
        )
        mask_node.inputs = {'Input': obj_node}
        return mask_node

    def _convert_array_access(self, expr: ArrayAccess) -> MaterialNode:
        """转换数组访问 — 大多数情况不可直接转换"""
        self.graph.warnings.append(
            f"行 {expr.line}: 数组访问无法直接转换为材质节点"
        )
        return self._convert_expr(expr.object)

    def _convert_ternary(self, expr: TernaryOp) -> MaterialNode:
        """转换三元运算: a > b ? c : d → If 节点"""
        true_node = self._convert_expr(expr.true_expr)
        false_node = self._convert_expr(expr.false_expr)

        # 如果条件是比较运算，直接映射到 If 节点
        if isinstance(expr.condition, BinaryOp) and expr.condition.op in ('>', '<', '>=', '<=', '==', '!='):
            a_node = self._convert_expr(expr.condition.left)
            b_node = self._convert_expr(expr.condition.right)

            if_node = self._make_node(
                'MaterialExpressionIf', f'If({expr.condition.op})',
                ['A', 'B', 'A > B', 'A == B', 'A < B'],
                source_line=expr.line,
            )

            op = expr.condition.op
            if op == '>':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': true_node, 'A == B': false_node, 'A < B': false_node}
            elif op == '>=':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': true_node, 'A == B': true_node, 'A < B': false_node}
            elif op == '<':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': false_node, 'A == B': false_node, 'A < B': true_node}
            elif op == '<=':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': false_node, 'A == B': true_node, 'A < B': true_node}
            elif op == '==':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': false_node, 'A == B': true_node, 'A < B': false_node}
            elif op == '!=':
                if_node.inputs = {'A': a_node, 'B': b_node, 'A > B': true_node, 'A == B': false_node, 'A < B': true_node}
            return if_node

        # 非比较条件 → If(cond, 0, cond > 0, ..., ...)
        cond_node = self._convert_expr(expr.condition)
        zero = self._make_constant(0.0, expr.line)
        if_node = self._make_node(
            'MaterialExpressionIf', 'If(bool)',
            ['A', 'B', 'A > B', 'A == B', 'A < B'],
            source_line=expr.line,
        )
        if_node.inputs = {'A': cond_node, 'B': zero, 'A > B': true_node, 'A == B': false_node, 'A < B': false_node}
        return if_node

    def _convert_custom_marker_call(self, expr: FunctionCall, custom_idx: int) -> MaterialNode:
        """转换预处理器标记的复杂函数调用 → CustomExpression 节点
        
        __CUSTOM_N__(arg1, arg2, ...) 被转换为一个 MaterialExpressionCustom 节点，
        其 Code 属性包含完整的函数体（含依赖函数定义），
        输入参数通过 CustomExpression 的 Input pin 传入。
        
        函数信息存储在 graph.custom_expressions 中，格式：
            '__CUSTOM_N__:func_name:hlsl_code'
        完整信息在 self._custom_fragment_details[N] 中。
        """
        import re as _re
        
        # 从 graph.custom_expressions 中查找对应的代码片段
        marker = f'__CUSTOM_{custom_idx}__'
        custom_code = ''
        custom_name = f'CustomFunc_{custom_idx}'
        
        for entry in self.graph.custom_expressions:
            if entry.startswith(marker + ':'):
                parts = entry.split(':', 2)
                if len(parts) >= 3:
                    custom_name = parts[1]
                    custom_code = parts[2]
                break
        
        # 转换所有参数
        arg_nodes = [self._convert_expr(arg) for arg in expr.args]
        
        # 获取完整的 fragment 信息（含参数名列表）
        fragment_details = getattr(self, '_custom_fragment_details', {}).get(custom_idx)
        
        if fragment_details and 'inputs' in fragment_details:
            param_list = fragment_details['inputs']  # [(type, name), ...]
            # 用原始参数名作为输入 pin 名称
            custom_inputs = [pname for (ptype, pname) in param_list]
            
            # 在 HLSL 代码中，将原始参数名替换为对应的输入名
            # UE4 CustomExpression 中参数通过名称引用（而不是 Input0, Input1）
            # 但如果参数名和代码中的局部变量冲突，需要用 Input0 形式
            # 这里保持原始参数名，UE4 会根据 Inputs 数组匹配
            adjusted_code = custom_code
        else:
            # 没有 fragment_details，退回到默认 Input0, Input1 形式
            custom_inputs = [f'Input{i}' for i in range(len(arg_nodes))]
            adjusted_code = custom_code
        
        # 确保输入名数量不超过实参数量（补齐或截断）
        while len(custom_inputs) < len(arg_nodes):
            custom_inputs.append(f'Input{len(custom_inputs)}')
        custom_inputs = custom_inputs[:max(len(arg_nodes), len(custom_inputs))]
        
        # 构建 Inputs 属性（T3D 生成需要）
        inputs_info = [{'InputName': name} for name in custom_inputs]
        
        # 如果 custom_code 为空，发出警告
        if not adjusted_code.strip():
            self.graph.warnings.append(
                f"行 {expr.line}: CustomExpression '{custom_name}' 的 Code 为空，"
                f"可能是预处理器未能正确提取函数体"
            )
        
        # 创建 CustomExpression 节点
        node = self._make_node(
            'MaterialExpressionCustom', custom_name,
            custom_inputs,
            properties={
                'Code': adjusted_code,
                'Description': custom_name,
                'Inputs': inputs_info,
            },
            source_line=expr.line,
        )
        
        # 连接输入
        for i, iname in enumerate(custom_inputs):
            if i < len(arg_nodes):
                node.inputs[iname] = arg_nodes[i]
        
        return node


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def hlsl_to_material_graph(source: str, explicit_inputs: List[str] = None) -> MaterialGraph:
    """
    一键将 HLSL 源代码转为材质节点图
    
    自动检测是否包含 struct/自定义函数等复杂语法，
    如果有则先经过预处理器（hlsl_preprocessor）进行
    struct 提取、函数内联、复杂代码拆分，然后再解析。

    参数:
        source: HLSL 代码字符串
        explicit_inputs: 显式输入参数名列表（用于 Custom Node 转换）
    返回:
        MaterialGraph 材质节点图
    """
    from hlsl_preprocessor import preprocess_hlsl
    from hlsl_parser import parse_hlsl
    
    # 1. 预处理：struct 展开、函数内联、复杂代码拆分
    preprocess_result = preprocess_hlsl(source)
    
    # 2. 解析预处理后的代码
    program = parse_hlsl(preprocess_result.main_code)
    
    # 3. AST → 材质节点图
    mapper = NodeMapper()
    
    # 记录显式输入参数，用于在 _convert_identifier 中识别
    if explicit_inputs:
        mapper._explicit_inputs = set(explicit_inputs)
    else:
        mapper._explicit_inputs = set()
    
    # 注册预处理拆出的 CustomExpression 片段
    # 使用 _custom_fragment_details 保存完整信息（含参数列表）
    mapper._custom_fragment_details = {}
    for idx, fragment in enumerate(preprocess_result.custom_fragments):
        marker = f'__CUSTOM_{idx}__'
        mapper.graph.custom_expressions.append(
            f'{marker}:{fragment["name"]}:{fragment["code"]}'
        )
        mapper._custom_fragment_details[idx] = fragment
    
    graph = mapper.convert(program)
    
    # 传递预处理警告
    graph.warnings.extend(preprocess_result.warnings)
    
    return graph
