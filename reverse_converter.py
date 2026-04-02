"""
============================================================
 reverse_converter.py
 MaterialGraph → HLSL Custom Node 反向转换
============================================================

将 node_mapper 生成的 MaterialGraph 中间表示反向转换为
可读的 HLSL 代码，用于 UE4 Custom HLSL Node。

反向映射规则 (UE4 4.24)：
  ─────────────────────────────────────────────────
  UE4 MaterialExpression       →  HLSL 操作
  ─────────────────────────────────────────────────
  MaterialExpressionAdd        →  a + b
  MaterialExpressionSubtract   →  a - b
  MaterialExpressionMultiply   →  a * b
  MaterialExpressionDivide     →  a / b
  MaterialExpressionFmod       →  fmod(a, b)
  ─────────────────────────────────────────────────
  MaterialExpressionLinearInterpolate →  lerp(a, b, t)
  MaterialExpressionSaturate          →  saturate(x)
  MaterialExpressionClamp             →  clamp(x, min, max)
  MaterialExpressionDotProduct        →  dot(a, b)
  MaterialExpressionCrossProduct      →  cross(a, b)
  MaterialExpressionNormalize         →  normalize(x)
  MaterialExpressionPower             →  pow(base, exp)
  ... (see REVERSE_MAP below)
  ─────────────────────────────────────────────────
  MaterialExpressionComponentMask     →  .xyz / .rg 等
  MaterialExpressionAppendVector      →  float3(a, b) 构造
  ─────────────────────────────────────────────────
  MaterialExpressionConstant          →  数字常量
  MaterialExpressionConstant3Vector   →  float3(r, g, b)
  MaterialExpressionConstant4Vector   →  float4(r, g, b, a)
  ─────────────────────────────────────────────────
  MaterialExpressionIf                →  a > b ? c : d
  ─────────────────────────────────────────────────
  MaterialExpressionCustom            →  /* 保留为注释 */
  ─────────────────────────────────────────────────
============================================================
"""

from typing import List, Dict, Optional, Set, Tuple, Any
from node_mapper import MaterialNode, MaterialGraph


# ═══════════════════════════════════════════════════════════
# UE4 节点 → HLSL 反向映射表
# ═══════════════════════════════════════════════════════════

# 二元运算节点 → 中缀运算符
REVERSE_BINARY_OP = {
    'MaterialExpressionAdd':       '+',
    'MaterialExpressionSubtract':  '-',
    'MaterialExpressionMultiply':  '*',
    'MaterialExpressionDivide':    '/',
}

# 单参数函数节点
REVERSE_FUNC_UNARY = {
    'MaterialExpressionSaturate':     'saturate',
    'MaterialExpressionAbs':          'abs',
    'MaterialExpressionSign':         'sign',
    'MaterialExpressionFloor':        'floor',
    'MaterialExpressionCeil':         'ceil',
    'MaterialExpressionRound':        'round',
    'MaterialExpressionFrac':         'frac',
    'MaterialExpressionSquareRoot':   'sqrt',
    'MaterialExpressionSine':         'sin',
    'MaterialExpressionCosine':       'cos',
    'MaterialExpressionTangent':      'tan',
    'MaterialExpressionArcsine':      'asin',
    'MaterialExpressionArccosine':    'acos',
    'MaterialExpressionArctangent':   'atan',
    'MaterialExpressionNormalize':    'normalize',
    'MaterialExpressionOneMinus':     '1.0 - ',  # special: prefix expression
    'MaterialExpressionLogarithm10':  'log10',
    'MaterialExpressionLogarithm2':   'log2',
    'MaterialExpressionDDX':          'ddx',
    'MaterialExpressionDDY':          'ddy',
    'MaterialExpressionVectorLength': 'length',
}

# 双参数函数节点
REVERSE_FUNC_BINARY = {
    'MaterialExpressionFmod':         'fmod',
    'MaterialExpressionDotProduct':   'dot',
    'MaterialExpressionCrossProduct': 'cross',
    'MaterialExpressionPower':        'pow',
    'MaterialExpressionMin':          'min',
    'MaterialExpressionMax':          'max',
    'MaterialExpressionDistance':      'distance',
    'MaterialExpressionArctangent2':  'atan2',
}

# 三参数函数节点
REVERSE_FUNC_TERNARY = {
    'MaterialExpressionLinearInterpolate': 'lerp',
    'MaterialExpressionClamp':             'clamp',
}

# 引擎内置变量节点 → HLSL 变量名
REVERSE_BUILTIN_VARS = {
    'MaterialExpressionCameraPositionWS':    'CameraPosition',
    'MaterialExpressionWorldPosition':       'WorldPosition',
    'MaterialExpressionPixelNormalWS':       'Normal',
    'MaterialExpressionVertexNormalWS':      'VertexNormal',
    'MaterialExpressionCameraVectorWS':      'ViewDir',
    'MaterialExpressionTextureCoordinate':   'UV',
    'MaterialExpressionTime':                'Time',
    'MaterialExpressionScreenPosition':      'ScreenPosition',
    'MaterialExpressionVertexColor':         'VertexColor',
    'MaterialExpressionReflectionVectorWS':  'ReflectionVector',
    'MaterialExpressionObjectPositionWS':    'ObjectPosition',
    'MaterialExpressionActorPositionWS':     'ActorPosition',
    'MaterialExpressionPixelDepth':          'PixelDepth',
    'MaterialExpressionSceneDepth':          'SceneDepth',
}

# 不可逆节点类型（保留为注释）
IRREVERSIBLE_NODES = {
    'MaterialExpressionCustom',
    'MaterialExpressionMaterialFunctionCall',
}


# ═══════════════════════════════════════════════════════════
# 反向转换器
# ═══════════════════════════════════════════════════════════

class ReverseConverter:
    """将 MaterialGraph 反向转换为 HLSL 代码"""

    def __init__(self, graph: MaterialGraph):
        self.graph = graph
        # 已经生成过表达式的节点 → 变量名
        self._node_var_map: Dict[int, str] = {}
        # 节点被引用次数（用于决定是否需要提取为变量）
        self._ref_count: Dict[int, int] = {}
        # 生成的语句列表
        self._statements: List[str] = []
        # 变量计数器
        self._var_counter: int = 0
        # 警告
        self.warnings: List[str] = []
        # 输入参数集合（识别出的参数）
        self._input_params: Dict[str, str] = {}  # name → inferred_type

    def convert(self) -> str:
        """执行反向转换，返回 HLSL 代码字符串"""
        if not self.graph.output_node:
            self.warnings.append("MaterialGraph 没有输出节点")
            return "// 错误: MaterialGraph 没有输出节点\nreturn float3(0, 0, 0);"

        # 1. 计算引用次数（拓扑遍历）
        self._compute_ref_counts(self.graph.output_node)

        # 2. 生成表达式（从输出节点递归）
        result_expr = self._emit_node(self.graph.output_node)

        # 3. 组装最终代码
        lines = []

        # 注释头
        lines.append("// Auto-generated HLSL from MaterialGraph (reverse conversion)")

        # 输入参数声明（作为注释提示）
        if self._input_params:
            lines.append("// Input parameters:")
            for pname, ptype in sorted(self._input_params.items()):
                lines.append(f"//   {ptype} {pname}")
            lines.append("")

        # 变量声明和赋值语句
        for stmt in self._statements:
            lines.append(stmt)

        # return 语句
        lines.append(f"return {result_expr};")

        # 警告注释
        if self.warnings:
            lines.append("")
            lines.append("// === 转换警告 ===")
            for w in self.warnings:
                lines.append(f"// WARNING: {w}")

        return "\n".join(lines)

    def _compute_ref_counts(self, node: MaterialNode, visited: Set[int] = None):
        """计算每个节点被引用的次数"""
        if visited is None:
            visited = set()

        if node is None:
            return

        self._ref_count[node.id] = self._ref_count.get(node.id, 0) + 1

        if node.id in visited:
            return
        visited.add(node.id)

        for input_node in node.inputs.values():
            if input_node is not None:
                self._compute_ref_counts(input_node, visited)

    def _new_var(self, hint: str = "") -> str:
        """生成新的临时变量名"""
        self._var_counter += 1
        if hint:
            # 清理 hint 为合法变量名
            clean = ''.join(c if c.isalnum() or c == '_' else '_' for c in hint)
            clean = clean.strip('_')
            if clean and clean[0].isdigit():
                clean = 'v_' + clean
            if clean:
                return f"{clean}_{self._var_counter}"
        return f"t{self._var_counter}"

    def _needs_variable(self, node: MaterialNode) -> bool:
        """判断节点是否需要提取为中间变量（被多次引用）"""
        return self._ref_count.get(node.id, 0) > 1

    def _emit_node(self, node: MaterialNode) -> str:
        """递归生成节点的 HLSL 表达式，返回表达式字符串
        
        如果节点被多次引用，会提取为变量并记录在 _statements 中。
        """
        if node is None:
            return "0.0"

        # 已经生成过 → 直接返回变量名
        if node.id in self._node_var_map:
            return self._node_var_map[node.id]

        # 生成表达式
        expr = self._node_to_expr(node)

        # 如果被多次引用，提取为变量
        if self._needs_variable(node):
            var_name = self._new_var(self._var_hint(node))
            var_type = self._infer_type(node)
            self._statements.append(f"{var_type} {var_name} = {expr};")
            self._node_var_map[node.id] = var_name
            return var_name

        # 单次引用，直接返回表达式
        self._node_var_map[node.id] = expr
        return expr

    def _var_hint(self, node: MaterialNode) -> str:
        """从节点信息中生成变量名提示"""
        dn = node.display_name.lower()
        # 从一些常见模式提取有意义的名字
        if 'fresnel' in dn:
            return 'fresnel'
        if 'rim' in dn:
            return 'rim'
        if 'mask' in dn:
            return 'mask'
        if 'smoothstep' in dn.lower():
            return 'smooth'
        if 'negate' in dn:
            return 'neg'
        # 默认用 display_name 简化
        clean = ''.join(c if c.isalnum() else '_' for c in node.display_name)
        if len(clean) > 12:
            clean = clean[:12]
        return clean or 'tmp'

    def _node_to_expr(self, node: MaterialNode) -> str:
        """将单个节点转换为 HLSL 表达式"""
        ue = node.ue_class

        # ── 常量节点 ──
        if ue == 'MaterialExpressionConstant':
            return self._emit_constant(node)
        if ue == 'MaterialExpressionConstant2Vector':
            return self._emit_constant2(node)
        if ue == 'MaterialExpressionConstant3Vector':
            return self._emit_constant3(node)
        if ue == 'MaterialExpressionConstant4Vector':
            return self._emit_constant4(node)

        # ── 参数节点 ──
        if ue == 'MaterialExpressionScalarParameter':
            return self._emit_param(node, 'float')
        if ue == 'MaterialExpressionVectorParameter':
            return self._emit_param(node, 'float3')
        if ue == 'MaterialExpressionTextureObjectParameter':
            return self._emit_param(node, 'Texture2D')

        # ── 引擎内置变量 ──
        if ue in REVERSE_BUILTIN_VARS:
            return REVERSE_BUILTIN_VARS[ue]

        # ── 二元运算 ──
        if ue in REVERSE_BINARY_OP:
            return self._emit_binary_op(node, REVERSE_BINARY_OP[ue])

        # ── 单参数函数 ──
        if ue in REVERSE_FUNC_UNARY:
            return self._emit_unary_func(node, REVERSE_FUNC_UNARY[ue])

        # ── 双参数函数 ──
        if ue in REVERSE_FUNC_BINARY:
            return self._emit_binary_func(node, REVERSE_FUNC_BINARY[ue])

        # ── 三参数函数 ──
        if ue in REVERSE_FUNC_TERNARY:
            return self._emit_ternary_func(node, REVERSE_FUNC_TERNARY[ue])

        # ── ComponentMask (Swizzle) ──
        if ue == 'MaterialExpressionComponentMask':
            return self._emit_component_mask(node)

        # ── AppendVector (构造函数) ──
        if ue == 'MaterialExpressionAppendVector':
            return self._emit_append_vector(node)

        # ── If 节点 ──
        if ue == 'MaterialExpressionIf':
            return self._emit_if_node(node)

        # ── TextureSample ──
        if ue == 'MaterialExpressionTextureSample':
            return self._emit_texture_sample(node)

        # ── CustomExpression (不可逆) ──
        if ue == 'MaterialExpressionCustom':
            return self._emit_custom_expression(node)

        # ── 未知节点 ──
        self.warnings.append(
            f"未知节点类型 '{ue}' (display: {node.display_name})，使用占位表达式"
        )
        return f"/* UNKNOWN: {node.display_name} */ 0.0"

    # ── 常量 ──

    def _emit_constant(self, node: MaterialNode) -> str:
        val = node.properties.get('R', 0.0)
        return self._format_float(val)

    def _emit_constant2(self, node: MaterialNode) -> str:
        r = node.properties.get('R', 0.0)
        g = node.properties.get('G', 0.0)
        return f"float2({self._format_float(r)}, {self._format_float(g)})"

    def _emit_constant3(self, node: MaterialNode) -> str:
        c = node.properties.get('Constant', {})
        if isinstance(c, dict):
            r = c.get('R', 0.0)
            g = c.get('G', 0.0)
            b = c.get('B', 0.0)
        else:
            r, g, b = 0.0, 0.0, 0.0
        return f"float3({self._format_float(r)}, {self._format_float(g)}, {self._format_float(b)})"

    def _emit_constant4(self, node: MaterialNode) -> str:
        c = node.properties.get('Constant', {})
        if isinstance(c, dict):
            r = c.get('R', 0.0)
            g = c.get('G', 0.0)
            b = c.get('B', 0.0)
            a = c.get('A', 0.0)
        else:
            r, g, b, a = 0.0, 0.0, 0.0, 0.0
        return (f"float4({self._format_float(r)}, {self._format_float(g)}, "
                f"{self._format_float(b)}, {self._format_float(a)})")

    # ── 参数 ──

    def _emit_param(self, node: MaterialNode, param_type: str) -> str:
        name = node.properties.get('ParameterName', node.display_name)
        self._input_params[name] = param_type
        return name

    # ── 二元运算 ──

    def _emit_binary_op(self, node: MaterialNode, op: str) -> str:
        a_node = node.inputs.get('A')
        b_node = node.inputs.get('B')

        # 特殊情况：Multiply(-1, x) → -x
        if op == '*' and a_node and a_node.ue_class == 'MaterialExpressionConstant':
            val = a_node.properties.get('R', 0.0)
            if val == -1.0:
                b_expr = self._emit_node(b_node)
                return f"(-{self._wrap_if_needed(b_expr)})"

        a_expr = self._emit_node(a_node)
        b_expr = self._emit_node(b_node)

        return f"({a_expr} {op} {b_expr})"

    # ── 单参数函数 ──

    def _emit_unary_func(self, node: MaterialNode, func_name: str) -> str:
        # 找输入（尝试常见名称）
        input_node = (node.inputs.get('Input') or
                      node.inputs.get('VectorInput') or
                      node.inputs.get('Value') or
                      self._get_first_input(node))

        input_expr = self._emit_node(input_node)

        # OneMinus 特殊处理
        if func_name.startswith('1.0 - '):
            return f"(1.0 - {input_expr})"

        return f"{func_name}({input_expr})"

    # ── 双参数函数 ──

    def _emit_binary_func(self, node: MaterialNode, func_name: str) -> str:
        a_node = node.inputs.get('A') or node.inputs.get('Base')
        b_node = node.inputs.get('B') or node.inputs.get('Exponent') or node.inputs.get('Y')

        # pow 的特殊输入名称
        if func_name == 'pow':
            a_node = node.inputs.get('Base') or node.inputs.get('A')
            b_node = node.inputs.get('Exponent') or node.inputs.get('B')

        if func_name == 'atan2':
            a_node = node.inputs.get('Y') or node.inputs.get('A')
            b_node = node.inputs.get('X') or node.inputs.get('B')

        a_expr = self._emit_node(a_node)
        b_expr = self._emit_node(b_node)
        return f"{func_name}({a_expr}, {b_expr})"

    # ── 三参数函数 ──

    def _emit_ternary_func(self, node: MaterialNode, func_name: str) -> str:
        if func_name == 'lerp':
            a_node = node.inputs.get('A')
            b_node = node.inputs.get('B')
            c_node = node.inputs.get('Alpha')
        elif func_name == 'clamp':
            a_node = node.inputs.get('Input')
            b_node = node.inputs.get('Min')
            c_node = node.inputs.get('Max')
        else:
            a_node = self._get_input_by_index(node, 0)
            b_node = self._get_input_by_index(node, 1)
            c_node = self._get_input_by_index(node, 2)

        a_expr = self._emit_node(a_node)
        b_expr = self._emit_node(b_node)
        c_expr = self._emit_node(c_node)
        return f"{func_name}({a_expr}, {b_expr}, {c_expr})"

    # ── ComponentMask (Swizzle) ──

    def _emit_component_mask(self, node: MaterialNode) -> str:
        input_node = node.inputs.get('Input') or self._get_first_input(node)
        input_expr = self._emit_node(input_node)

        # 从 properties 中恢复 swizzle 分量
        r = node.properties.get('R', False)
        g = node.properties.get('G', False)
        b = node.properties.get('B', False)
        a = node.properties.get('A', False)

        components = ''
        if r:
            components += 'x'
        if g:
            components += 'y'
        if b:
            components += 'z'
        if a:
            components += 'w'

        if not components:
            components = 'x'  # 默认

        return f"{self._wrap_if_needed(input_expr)}.{components}"

    # ── AppendVector (构造函数) ──

    def _emit_append_vector(self, node: MaterialNode) -> str:
        a_node = node.inputs.get('A')
        b_node = node.inputs.get('B')

        a_expr = self._emit_node(a_node)
        b_expr = self._emit_node(b_node)

        # 尝试检测是否可以折叠为 float3/float4 构造函数
        # 如果 A 也是 AppendVector，可能是 float3(x, y, z) 的链式结构
        a_components = self._count_append_components(a_node)
        b_components = self._count_append_components(b_node)
        total = a_components + b_components

        # 展开为 floatN 构造
        a_parts = self._flatten_append(a_node)
        b_parts = self._flatten_append(b_node)
        all_parts = a_parts + b_parts

        if len(all_parts) == 2:
            return f"float2({all_parts[0]}, {all_parts[1]})"
        elif len(all_parts) == 3:
            return f"float3({all_parts[0]}, {all_parts[1]}, {all_parts[2]})"
        elif len(all_parts) == 4:
            return f"float4({all_parts[0]}, {all_parts[1]}, {all_parts[2]}, {all_parts[3]})"

        # 默认回退
        return f"float2({a_expr}, {b_expr})"

    def _count_append_components(self, node: MaterialNode) -> int:
        """计算 AppendVector 链的分量数"""
        if node is None:
            return 1
        if node.ue_class != 'MaterialExpressionAppendVector':
            return 1
        a = self._count_append_components(node.inputs.get('A'))
        b = self._count_append_components(node.inputs.get('B'))
        return a + b

    def _flatten_append(self, node: MaterialNode) -> List[str]:
        """展开 AppendVector 链为分量表达式列表"""
        if node is None:
            return ["0.0"]
        if node.ue_class != 'MaterialExpressionAppendVector':
            return [self._emit_node(node)]
        a_parts = self._flatten_append(node.inputs.get('A'))
        b_parts = self._flatten_append(node.inputs.get('B'))
        return a_parts + b_parts

    # ── If 节点 ──

    def _emit_if_node(self, node: MaterialNode) -> str:
        a_node = node.inputs.get('A')
        b_node = node.inputs.get('B')
        gt_node = node.inputs.get('A > B')
        eq_node = node.inputs.get('A == B')
        lt_node = node.inputs.get('A < B')

        a_expr = self._emit_node(a_node)
        b_expr = self._emit_node(b_node)
        gt_expr = self._emit_node(gt_node) if gt_node else "0.0"
        eq_expr = self._emit_node(eq_node) if eq_node else gt_expr
        lt_expr = self._emit_node(lt_node) if lt_node else "0.0"

        # 尝试检测运算符模式
        # 如果 A > B 和 A == B 给出同一结果，则是 >= 比较
        if gt_node and eq_node and gt_node.id == eq_node.id:
            return f"({a_expr} >= {b_expr} ? {gt_expr} : {lt_expr})"
        # 如果 A < B 和 A == B 给出同一结果，则是 <= 比较
        if lt_node and eq_node and lt_node.id == eq_node.id:
            return f"({a_expr} <= {b_expr} ? {gt_expr} : {lt_expr})"
        # 如果 A > B 和 A < B 给出同一结果且 != A == B，则是 == 比较
        if gt_node and lt_node and gt_node.id == lt_node.id and (not eq_node or eq_node.id != gt_node.id):
            return f"({a_expr} == {b_expr} ? {eq_expr} : {gt_expr})"
        # 简单 > 比较
        if gt_expr != lt_expr:
            return f"({a_expr} > {b_expr} ? {gt_expr} : {lt_expr})"

        return f"({a_expr} > {b_expr} ? {gt_expr} : {lt_expr})"

    # ── TextureSample ──

    def _emit_texture_sample(self, node: MaterialNode) -> str:
        tex_node = node.inputs.get('Tex')
        uv_node = node.inputs.get('UVs')

        tex_expr = self._emit_node(tex_node) if tex_node else "Texture"
        uv_expr = self._emit_node(uv_node) if uv_node else "UV"

        return f"tex2D({tex_expr}, {uv_expr})"

    # ── CustomExpression (不可逆) ──

    def _emit_custom_expression(self, node: MaterialNode) -> str:
        code = node.properties.get('Code', '')
        desc = node.properties.get('Description', node.display_name)

        # 收集输入
        input_exprs = []
        for iname in node.input_names:
            inode = node.inputs.get(iname)
            if inode:
                input_exprs.append(self._emit_node(inode))
            else:
                input_exprs.append(iname)

        # 检查 Code 中是否是简单的 return func(Input0); 模式
        code_stripped = code.strip()
        if code_stripped.startswith('return ') and code_stripped.endswith(';'):
            inner = code_stripped[7:-1].strip()
            # 替换 Input0, Input1 等为实际输入表达式
            result = inner
            for i, expr in enumerate(input_exprs):
                result = result.replace(f'Input{i}', expr)
            # 还原自定义输入名
            for i, iname in enumerate(node.input_names):
                if i < len(input_exprs):
                    result = result.replace(iname, input_exprs[i])
            return result

        # 复杂的 CustomExpression → 保留为注释
        self.warnings.append(
            f"CustomExpression '{desc}' 包含复杂代码，保留为注释"
        )
        args_str = ", ".join(input_exprs) if input_exprs else ""
        return f"/* CustomExpression: {desc}({args_str}) */"

    # ── 辅助方法 ──

    def _get_first_input(self, node: MaterialNode) -> Optional[MaterialNode]:
        """获取节点的第一个输入"""
        if node.input_names:
            return node.inputs.get(node.input_names[0])
        if node.inputs:
            return next(iter(node.inputs.values()), None)
        return None

    def _get_input_by_index(self, node: MaterialNode, index: int) -> Optional[MaterialNode]:
        """按索引获取输入"""
        if index < len(node.input_names):
            return node.inputs.get(node.input_names[index])
        return None

    def _format_float(self, value: float) -> str:
        """格式化浮点数"""
        if value == int(value) and abs(value) < 10000:
            return f"{int(value)}.0"
        # 移除尾零
        s = f"{value:.6f}".rstrip('0').rstrip('.')
        if '.' not in s:
            s += '.0'
        return s

    def _wrap_if_needed(self, expr: str) -> str:
        """如果表达式包含运算符，用括号包裹"""
        # 简单标识符、函数调用、数字不需要包裹
        if expr.startswith('(') and expr.endswith(')'):
            return expr
        # 检查是否是简单标识符
        if all(c.isalnum() or c == '_' for c in expr):
            return expr
        # 检查是否是函数调用
        if '(' in expr and expr.endswith(')') and not any(c in expr.split('(')[0] for c in '+-*/ '):
            return expr
        # 检查是否是带 swizzle 的简单访问
        if '.' in expr and not any(c in expr for c in '+-*/ '):
            return expr
        # 需要包裹
        if any(op in expr for op in [' + ', ' - ', ' * ', ' / ', ' ? ']):
            return f"({expr})"
        return expr

    def _infer_type(self, node: MaterialNode) -> str:
        """推断节点的 HLSL 类型"""
        ue = node.ue_class

        # 常量类型
        if ue == 'MaterialExpressionConstant':
            return 'float'
        if ue == 'MaterialExpressionConstant2Vector':
            return 'float2'
        if ue in ('MaterialExpressionConstant3Vector', 'MaterialExpressionConstant4Vector'):
            return 'float3' if ue.endswith('3Vector') else 'float4'

        # 标量参数
        if ue == 'MaterialExpressionScalarParameter':
            return 'float'

        # 向量参数
        if ue == 'MaterialExpressionVectorParameter':
            return 'float3'

        # 返回标量的函数
        scalar_results = {
            'MaterialExpressionDotProduct', 'MaterialExpressionDistance',
            'MaterialExpressionComponentMask',  # depends on mask, default to float
            'MaterialExpressionSaturate',
        }

        # ComponentMask 的类型取决于分量数
        if ue == 'MaterialExpressionComponentMask':
            r = node.properties.get('R', False)
            g = node.properties.get('G', False)
            b = node.properties.get('B', False)
            a = node.properties.get('A', False)
            count = sum(1 for x in [r, g, b, a] if x)
            if count == 1:
                return 'float'
            elif count == 2:
                return 'float2'
            elif count == 3:
                return 'float3'
            else:
                return 'float4'

        if ue == 'MaterialExpressionDotProduct':
            return 'float'
        if ue == 'MaterialExpressionDistance':
            return 'float'

        # AppendVector
        if ue == 'MaterialExpressionAppendVector':
            count = self._count_append_components(node)
            if count == 2:
                return 'float2'
            elif count == 3:
                return 'float3'
            elif count >= 4:
                return 'float4'

        # TextureSample → float4
        if ue == 'MaterialExpressionTextureSample':
            return 'float4'

        # 默认根据 display_name 猜测
        dn = node.display_name.lower()
        if any(kw in dn for kw in ['saturate', 'fresnel', 'dot', 'distance', 'length', 'scalar']):
            return 'float'

        # 默认 float3
        return 'float3'


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def material_graph_to_hlsl(graph: MaterialGraph) -> str:
    """
    一键将 MaterialGraph 反向转换为 HLSL 代码

    参数:
        graph: MaterialGraph 材质节点图
    返回:
        HLSL 代码字符串
    """
    converter = ReverseConverter(graph)
    return converter.convert()


def reverse_from_hlsl(hlsl_source: str) -> str:
    """
    双向转换测试：HLSL → MaterialGraph → HLSL

    参数:
        hlsl_source: 原始 HLSL 代码
    返回:
        反向转换后的 HLSL 代码
    """
    from node_mapper import hlsl_to_material_graph
    graph = hlsl_to_material_graph(hlsl_source)
    return material_graph_to_hlsl(graph)
