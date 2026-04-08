"""
============================================================
 shadertoy_converter.py
 Shadertoy GLSL → UE4 Custom Node HLSL 转换器
============================================================

将 Shadertoy 的 GLSL 代码转换为 UE4 Material Custom Node 可用的 HLSL 代码。

功能：
  1. GLSL → HLSL 类型映射 (vec2→float2, mat3→float3x3, etc.)
  2. GLSL → HLSL 函数映射 (mix→lerp, fract→frac, mod→fmod, texture→tex2D, etc.)
  3. 构造函数转换 (vec3(1.0)→float3(1.0,1.0,1.0))
  4. Shadertoy 内置变量映射 (iTime, iResolution, fragCoord, iMouse, iChannel)
  5. mainImage 函数体提取与转换
  6. 辅助函数处理
  7. 多种输入方法 (URL, 文件, 代码字符串)

用法：
  from shadertoy_converter import convert_shadertoy

  # 从代码字符串转换
  result = convert_shadertoy(glsl_code)

  # 从文件转换
  result = convert_shadertoy_file('shader.glsl')

  # 从 URL 转换
  result = convert_shadertoy_url('https://www.shadertoy.com/view/XsXXXX')
============================================================
"""

import re
import os
import sys
import textwrap
from typing import Dict, List, Optional, Tuple, Any


# ═══════════════════════════════════════════════════════════
# GLSL → HLSL 类型映射
# ═══════════════════════════════════════════════════════════

TYPE_MAP = {
    # 向量类型
    'vec2':   'float2',
    'vec3':   'float3',
    'vec4':   'float4',
    'ivec2':  'int2',
    'ivec3':  'int3',
    'ivec4':  'int4',
    'bvec2':  'bool2',
    'bvec3':  'bool3',
    'bvec4':  'bool4',
    'uvec2':  'uint2',
    'uvec3':  'uint3',
    'uvec4':  'uint4',
    # 矩阵类型
    'mat2':   'float2x2',
    'mat3':   'float3x3',
    'mat4':   'float4x4',
    'mat2x2': 'float2x2',
    'mat3x3': 'float3x3',
    'mat4x4': 'float4x4',
    # 采样器类型
    'sampler2D':   'Texture2D',
    'samplerCube': 'TextureCube',
    'sampler3D':   'Texture3D',
}

# 反向映射：HLSL 类型名 → 维度
TYPE_DIMENSIONS = {
    'float2': 2,
    'float3': 3,
    'float4': 4,
    'int2': 2,
    'int3': 3,
    'int4': 4,
}


# ═══════════════════════════════════════════════════════════
# GLSL → HLSL 函数映射
# ═══════════════════════════════════════════════════════════

FUNCTION_MAP = {
    'mix':           'lerp',
    'fract':         'frac',
    'mod':           'fmod',
    'texture':       'tex2D',
    'texture2D':     'tex2D',
    'textureLod':    'tex2Dlod',
    'texelFetch':    'tex2Dfetch',
    'textureGrad':   'tex2Dgrad',
    'dFdx':          'ddx',
    'dFdy':          'ddy',
    'inversesqrt':   'rsqrt',
    'atan':          'atan2',  # GLSL atan(y,x) → HLSL atan2(y,x)
    'lessThan':      'step',   # 近似，需注意语义差异
    'greaterThan':   'step',
    'equal':         'step',
}

# 不需要映射（GLSL/HLSL 同名）的函数
COMMON_FUNCTIONS = {
    'abs', 'sign', 'floor', 'ceil', 'round',
    'min', 'max', 'clamp', 'step', 'smoothstep',
    'length', 'distance', 'dot', 'cross', 'normalize', 'reflect', 'refract',
    'pow', 'exp', 'exp2', 'log', 'log2', 'sqrt',
    'sin', 'cos', 'tan', 'asin', 'acos',
    'radians', 'degrees',
    'saturate',  # HLSL 特有，但 Shadertoy 不用
}


# ═══════════════════════════════════════════════════════════
# Shadertoy 内置变量映射
# ═══════════════════════════════════════════════════════════

# Shadertoy uniform → UE4 对应方案
SHADERTOY_UNIFORMS = {
    'iTime':            {'ue4': 'Time',          'desc': 'Time node (seconds)'},
    'iGlobalTime':      {'ue4': 'Time',          'desc': 'Time node (legacy name)'},
    'iTimeDelta':       {'ue4': '0.016',         'desc': 'Frame delta time (~16ms), use a parameter for accuracy'},
    'iFrame':           {'ue4': '0',             'desc': 'Frame number (not directly available in UE4 material)'},
    'iResolution':      {'ue4': 'ViewSize',      'desc': 'Screen resolution as float3(width, height, 1.0)'},
    'iMouse':           {'ue4': 'float4(0,0,0,0)', 'desc': 'Mouse position (removed, use parameter if needed)'},
    'iDate':            {'ue4': 'float4(0,0,0,0)', 'desc': 'Date (not available in UE4 material)'},
    'iSampleRate':      {'ue4': '44100.0',        'desc': 'Audio sample rate (not relevant for UE4)'},
    'iChannelTime[0]':  {'ue4': 'Time',          'desc': 'Channel 0 playback time'},
    'iChannelTime[1]':  {'ue4': 'Time',          'desc': 'Channel 1 playback time'},
    'iChannelTime[2]':  {'ue4': 'Time',          'desc': 'Channel 2 playback time'},
    'iChannelTime[3]':  {'ue4': 'Time',          'desc': 'Channel 3 playback time'},
    'iChannelResolution[0]': {'ue4': 'float3(1024.0, 1024.0, 1.0)', 'desc': 'Channel 0 resolution'},
    'iChannelResolution[1]': {'ue4': 'float3(1024.0, 1024.0, 1.0)', 'desc': 'Channel 1 resolution'},
    'iChannelResolution[2]': {'ue4': 'float3(1024.0, 1024.0, 1.0)', 'desc': 'Channel 2 resolution'},
    'iChannelResolution[3]': {'ue4': 'float3(1024.0, 1024.0, 1.0)', 'desc': 'Channel 3 resolution'},
}

# UE4 input pin variables that need to be passed into struct as members
# Maps variable name → HLSL type (used when struct functions reference external inputs)
UE4_INPUT_VAR_TYPES = {
    'Time':      'float',
    'ViewSize':  'float2',
    'UV':        'float2',
}


# ═══════════════════════════════════════════════════════════
# 转换结果
# ═══════════════════════════════════════════════════════════

class ShadertoyConvertResult:
    """Shadertoy 转换结果"""

    def __init__(self):
        self.hlsl_code: str = ''           # 转换后的 HLSL 代码
        self.custom_node_code: str = ''    # 可直接粘贴到 UE4 Custom Node 的代码
        self.input_params: List[Dict[str, str]] = []  # Custom Node 需要的输入参数
        self.texture_inputs: List[Dict[str, str]] = []  # 需要的纹理输入
        self.warnings: List[str] = []      # 转换警告
        self.errors: List[str] = []        # 转换错误
        self.original_code: str = ''       # 原始 GLSL 代码
        self.helper_functions: List[str] = []  # 辅助函数列表
        self.has_multipass: bool = False   # 是否检测到多 pass

    @property
    def success(self) -> bool:
        return len(self.errors) == 0 and len(self.hlsl_code) > 0


# ═══════════════════════════════════════════════════════════
# 核心转换类
# ═══════════════════════════════════════════════════════════

class ShadertoyConverter:
    """Shadertoy GLSL → UE4 Custom Node HLSL 转换器"""

    def __init__(self):
        self.warnings: List[str] = []
        self.errors: List[str] = []
        self.helper_functions: List[str] = []
        self.used_uniforms: set = set()
        self.texture_channels: set = set()

    def convert(self, glsl_code: str) -> ShadertoyConvertResult:
        """主转换入口"""
        result = ShadertoyConvertResult()
        result.original_code = glsl_code

        self.warnings = []
        self.errors = []
        self.helper_functions = []
        self.used_uniforms = set()
        self.texture_channels = set()

        try:
            # Step 1: 预处理（移除版本声明、precision 等）
            code = self._preprocess(glsl_code)

            # Step 2: 检测多 pass
            result.has_multipass = self._detect_multipass(glsl_code)
            if result.has_multipass:
                self.warnings.append(
                    '检测到多 Pass (Buffer A/B/C/D) 引用。'
                    'UE4 Custom Node 不支持多 pass，需手动拆分为多个材质或使用 Render Target 链。'
                )

            # Step 3: 提取辅助函数和 mainImage
            helpers, main_body = self._extract_functions(code)

            if main_body is None:
                # 没有 mainImage，尝试直接转换整个代码
                self.warnings.append('未找到 mainImage 函数，尝试直接转换整个代码')
                main_body = code

            # Step 4: 转换辅助函数
            converted_helpers = []
            for func in helpers:
                converted = self._convert_glsl_to_hlsl(func)
                converted_helpers.append(converted)
                # 提取函数名
                func_name_match = re.search(r'\b(\w+)\s*\(', func)
                if func_name_match and func_name_match.group(1) not in ('define', 'if', 'for', 'while'):
                    self.helper_functions.append(func_name_match.group(1))
                else:
                    self.helper_functions.append('(globals)')

            # Step 5: 转换 mainImage 函数体
            converted_main = self._convert_glsl_to_hlsl(main_body)

            # Step 6: 处理 Shadertoy 内置变量
            converted_main = self._map_shadertoy_uniforms(converted_main)
            for i, h in enumerate(converted_helpers):
                converted_helpers[i] = self._map_shadertoy_uniforms(h)

            # Step 7: 处理 fragCoord → UV 转换
            converted_main = self._handle_fragcoord(converted_main)

            # Step 8: 处理 fragColor → return
            converted_main = self._handle_fragcolor(converted_main)

            # Step 9: 组装最终代码
            final_code = self._assemble_custom_node(converted_helpers, converted_main)

            result.hlsl_code = final_code
            result.custom_node_code = final_code
            result.helper_functions = self.helper_functions
            result.input_params = self._get_input_params()
            result.texture_inputs = self._get_texture_inputs()

        except Exception as e:
            self.errors.append(f'转换异常: {str(e)}')

        result.warnings = self.warnings[:]
        result.errors = self.errors[:]
        return result

    # ── 预处理 ──

    def _preprocess(self, code: str) -> str:
        """预处理 GLSL 代码"""
        lines = code.split('\n')
        processed = []
        for line in lines:
            stripped = line.strip()
            # 移除 #version
            if stripped.startswith('#version'):
                continue
            # 移除 precision
            if stripped.startswith('precision '):
                continue
            # 移除 #ifdef GL_ES ... #endif
            if stripped.startswith('#ifdef GL_ES'):
                continue
            if stripped == '#endif':
                continue
            # 保留 #define（可能有用户宏）
            processed.append(line)
        return '\n'.join(processed)

    def _detect_multipass(self, code: str) -> bool:
        """检测是否包含多 pass 引用"""
        multipass_patterns = [
            r'iChannel\d+',
            r'Buffer\s*[A-D]',
            r'bufferA|bufferB|bufferC|bufferD',
        ]
        for pattern in multipass_patterns:
            if re.search(pattern, code, re.IGNORECASE):
                return True
        return False

    # ── 函数提取 ──

    def _extract_functions(self, code: str) -> Tuple[List[str], Optional[str]]:
        """提取辅助函数和 mainImage 函数体

        Returns:
            (helper_functions, mainImage_body)
        """
        helpers = []
        main_body = None

        # 找到 mainImage 函数
        main_pattern = re.compile(
            r'void\s+mainImage\s*\(\s*out\s+vec4\s+(\w+)\s*,\s*in\s+vec2\s+(\w+)\s*\)\s*\{',
            re.MULTILINE
        )
        main_match = main_pattern.search(code)

        if main_match:
            frag_color_name = main_match.group(1)
            frag_coord_name = main_match.group(2)
            body_start = main_match.end()

            # 匹配到对应的右大括号
            body_end = self._find_matching_brace(code, body_start - 1)
            if body_end is not None:
                main_body = textwrap.dedent(code[body_start:body_end]).strip()
                # 在 main_body 中替换参数名为标准名
                if frag_color_name != 'fragColor':
                    main_body = re.sub(
                        r'\b' + re.escape(frag_color_name) + r'\b',
                        'fragColor',
                        main_body
                    )
                if frag_coord_name != 'fragCoord':
                    main_body = re.sub(
                        r'\b' + re.escape(frag_coord_name) + r'\b',
                        'fragCoord',
                        main_body
                    )

                # 提取 mainImage 之前的所有函数定义作为辅助函数
                before_main = code[:main_match.start()].strip()
                if before_main:
                    helpers = self._split_functions(before_main)
            else:
                self.errors.append('无法找到 mainImage 函数的闭合大括号')
        else:
            # 尝试查找 void main() 模式（旧版 GLSL）
            alt_pattern = re.compile(
                r'void\s+main\s*\(\s*(void)?\s*\)\s*\{',
                re.MULTILINE
            )
            alt_match = alt_pattern.search(code)
            if alt_match:
                body_start = alt_match.end()
                body_end = self._find_matching_brace(code, body_start - 1)
                if body_end is not None:
                    main_body = textwrap.dedent(code[body_start:body_end]).strip()
                    before_main = code[:alt_match.start()].strip()
                    if before_main:
                        helpers = self._split_functions(before_main)
                    self.warnings.append('找到 void main() 而非 mainImage()，使用旧版模式')

        return helpers, main_body

    def _find_matching_brace(self, code: str, open_pos: int) -> Optional[int]:
        """找到与给定位置 { 匹配的 } 的位置"""
        depth = 0
        i = open_pos
        while i < len(code):
            if code[i] == '{':
                depth += 1
            elif code[i] == '}':
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    def _split_functions(self, code: str) -> List[str]:
        """从代码块中分离出独立的函数定义

        识别策略：找到每个函数定义的开始（返回类型 + 函数名 + 参数列表 + {）
        """
        functions = []
        # 匹配函数定义模式
        # Exclude control flow keywords (if, else, for, while, switch, do) from being
        # mistakenly matched as function return types
        func_pattern = re.compile(
            r'(?:^|\n)\s*'
            r'(?!(?:if|else|for|while|switch|do|return|struct|class)\b)'
            r'([\w]+(?:\s+[\w]+)*)'
            r'\s+(\w+)\s*'
            r'\([^)]*\)\s*\{',
            re.MULTILINE
        )

        # 收集非函数的全局代码（#define、全局变量等）
        global_code_parts = []
        last_end = 0
        func_ranges = []

        for match in func_pattern.finditer(code):
            func_start = match.start()
            brace_pos = code.index('{', match.start())
            func_end = self._find_matching_brace(code, brace_pos)
            if func_end is not None:
                func_ranges.append((func_start, func_end + 1))
                # 两个函数之间的全局代码
                between = code[last_end:func_start].strip()
                if between:
                    global_code_parts.append(between)
                last_end = func_end + 1

        # 最后一段全局代码
        remaining = code[last_end:].strip()
        if remaining:
            global_code_parts.append(remaining)

        # 提取函数代码
        for start, end in func_ranges:
            functions.append(code[start:end].strip())

        # 如果有全局代码（#define、常量等），作为第一个"函数"前缀
        if global_code_parts:
            global_block = '\n'.join(global_code_parts)
            functions.insert(0, global_block)

        return functions

    # ── GLSL → HLSL 转换 ──

    def _convert_glsl_to_hlsl(self, code: str) -> str:
        """将 GLSL 代码转换为 HLSL"""
        result = code

        # 1. 类型映射
        result = self._map_types(result)

        # 2. 函数映射
        result = self._map_functions(result)

        # 3. 构造函数展开（如 vec3(1.0) → float3(1.0, 1.0, 1.0)）
        result = self._expand_constructors(result)

        # 4. 矩阵构造函数
        result = self._convert_matrix_constructors(result)

        # 5. texture 调用转换
        result = self._convert_texture_calls(result)

        # 6. 修复 GLSL 特有语法
        result = self._fix_glsl_syntax(result)

        return result

    def _map_types(self, code: str) -> str:
        """替换 GLSL 类型为 HLSL 类型"""
        result = code
        # 按类型名长度降序排序，避免部分匹配（如 vec4 先于 vec2）
        sorted_types = sorted(TYPE_MAP.items(), key=lambda x: len(x[0]), reverse=True)
        for glsl_type, hlsl_type in sorted_types:
            # 使用单词边界确保精确匹配
            result = re.sub(r'\b' + re.escape(glsl_type) + r'\b', hlsl_type, result)
        return result

    def _map_functions(self, code: str) -> str:
        """替换 GLSL 函数为 HLSL 等效函数"""
        result = code
        for glsl_func, hlsl_func in FUNCTION_MAP.items():
            # 匹配函数调用模式: funcname(
            result = re.sub(
                r'\b' + re.escape(glsl_func) + r'\s*\(',
                hlsl_func + '(',
                result
            )
        return result

    def _expand_constructors(self, code: str) -> str:
        """展开标量参数的向量构造函数

        vec3(1.0) → float3(1.0, 1.0, 1.0)
        vec4(1.0) → float4(1.0, 1.0, 1.0, 1.0)
        vec2(0.5) → float2(0.5, 0.5)

        但不展开非标量参数：
        float3(someVec4) 保持不变（这是截断/swizzle 操作）
        """
        # 已经在 _map_types 中把 vec3 替换为 float3 了
        # 需要处理 float3(单参数) → float3(x, x, x)
        # 但只对数字字面量进行展开，变量名保持不变
        for hlsl_type, dim in TYPE_DIMENSIONS.items():
            # 匹配 float3(<single_arg>) — 单参数且不含逗号
            pattern = re.compile(
                r'\b(' + re.escape(hlsl_type) + r')\s*\(([^,()]+)\)'
            )

            def _expand_match(m, dim=dim):
                type_name = m.group(1)
                arg = m.group(2).strip()
                # Only expand if the argument is a numeric literal (int or float)
                # e.g. 1.0, .5, 0, -3.14, 1., 0., etc.
                # Do NOT expand if it's a variable name like 'pieces', 'color', etc.
                if re.match(r'^-?(\d+\.?\d*|\d*\.\d+)[fF]?$', arg):
                    return f'{type_name}({", ".join([arg] * dim)})'
                # Not a scalar literal — leave as-is (truncation cast)
                return m.group(0)

            result = pattern.sub(_expand_match, code)
            code = result

        return code

    def _convert_matrix_constructors(self, code: str) -> str:
        """Convert GLSL-style matrix constructors to HLSL row-vector form.

        GLSL allows scalar arguments in matrix constructors:
            mat2(a, b, c, d)           → fills column-major
            mat3(a,b,c, d,e,f, g,h,i)  → fills column-major

        HLSL requires row vectors:
            float2x2(float2(a,b), float2(c,d))
            float3x3(float3(a,b,c), float3(d,e,f), float3(g,h,i))
            float4x4(float4(a,..,d), float4(e,..,h), float4(i,..,l), float4(m,..,p))

        Only rewrites when the argument count matches the scalar count
        (4 for 2x2, 9 for 3x3, 16 for 4x4). If the user already passes
        row vectors (e.g. float3x3(v1, v2, v3) with 3 args), leave as-is.
        """
        matrix_info = {
            'float2x2': {'dim': 2, 'scalar_count': 4,  'row_type': 'float2'},
            'float3x3': {'dim': 3, 'scalar_count': 9,  'row_type': 'float3'},
            'float4x4': {'dim': 4, 'scalar_count': 16, 'row_type': 'float4'},
        }

        for mtype, info in matrix_info.items():
            code = self._rewrite_matrix_constructor(code, mtype, info)

        return code

    def _split_args_balanced(self, args_str: str) -> list:
        """Split a comma-separated argument string respecting nested parentheses.

        e.g. "cos(rot), sin(rot), -sin(rot), cos(rot)"
             → ["cos(rot)", "sin(rot)", "-sin(rot)", "cos(rot)"]
        """
        args = []
        depth = 0
        current = []
        for ch in args_str:
            if ch == '(':
                depth += 1
                current.append(ch)
            elif ch == ')':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                args.append(''.join(current).strip())
                current = []
            else:
                current.append(ch)
        # last argument
        last = ''.join(current).strip()
        if last:
            args.append(last)
        return args

    def _rewrite_matrix_constructor(self, code: str, mtype: str, info: dict) -> str:
        """Rewrite all occurrences of mtype(scalar_args...) to mtype(rowN(...), ...).

        Uses balanced parenthesis matching to correctly handle nested expressions.
        """
        dim = info['dim']
        scalar_count = info['scalar_count']
        row_type = info['row_type']

        # Find pattern: float2x2( or float3x3( or float4x4(
        pattern = re.compile(r'\b' + re.escape(mtype) + r'\s*\(')
        offset = 0
        while True:
            m = pattern.search(code, offset)
            if not m:
                break
            paren_start = m.end() - 1  # position of '('
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue

            # Extract the arguments string between ( and )
            args_str = code[paren_start + 1:paren_end]
            args = self._split_args_balanced(args_str)

            if len(args) == scalar_count:
                # This is a scalar-argument constructor — rewrite to row vectors
                rows = []
                for r in range(dim):
                    row_args = args[r * dim:(r + 1) * dim]
                    rows.append(f'{row_type}({", ".join(row_args)})')
                new_constructor = f'{mtype}({", ".join(rows)})'
                code = code[:m.start()] + new_constructor + code[paren_end + 1:]
                offset = m.start() + len(new_constructor)
            else:
                # Not scalar args (already row vectors or single scalar) — skip
                offset = paren_end + 1

        return code

    def _convert_texture_calls(self, code: str) -> str:
        """转换纹理采样调用"""
        result = code

        # texture(iChannel0, uv) → tex2D(iChannel0_Tex, uv)
        # 记录使用的 channel
        for i in range(4):
            channel_name = f'iChannel{i}'
            if channel_name in result:
                self.texture_channels.add(i)
                # 将 iChannel0 替换为 Texture{i} 参数名
                result = re.sub(
                    r'\b' + re.escape(channel_name) + r'\b',
                    f'Texture{i}',
                    result
                )

        return result

    def _fix_glsl_syntax(self, code: str) -> str:
        """修复 GLSL 特有语法"""
        result = code

        # const 修饰符 — HLSL Custom Node 中可以保留
        # 但 HLSL 不支持 const 用于局部变量初始化
        # 在 Custom Node 中保留 const 是安全的

        # 修复 GLSL 的 out/inout 参数修饰符
        # HLSL 也支持 out/inout，所以可以保留

        # 修复 GLSL mod(a, b) → fmod(a, b)（已在函数映射中处理）

        # 修复 GLSL 特有的类型转换
        # int(x) → (int)(x) — HLSL 也支持函数式转换，保留即可

        # 修复 GLSL 向量*矩阵乘法 → HLSL mul()
        result = self._convert_matrix_multiply(result)

        return result

    def _convert_matrix_multiply(self, code: str) -> str:
        """Convert GLSL vector*matrix multiplication to HLSL mul() calls.

        In GLSL, `vec2 * mat2` is valid matrix multiplication.
        In HLSL, `float2 * float2x2` is NOT valid — must use mul(vector, matrix).

        Handles:
          1. expr * floatNxN(...)  → mul(expr, floatNxN(...))
          2. expr *= floatNxN(...) → expr = mul(expr, floatNxN(...))
          3. expr * matrixVar      → mul(expr, matrixVar)  (tracked via declarations)
          4. expr *= matrixVar     → expr = mul(expr, matrixVar)
        """
        matrix_types = {'float2x2', 'float3x3', 'float4x4'}

        # Step 1: Collect declared matrix variable names from the code
        matrix_vars = set()
        for mtype in matrix_types:
            decl_pattern = re.compile(r'\b' + re.escape(mtype) + r'\s+(\w+)\b')
            for m in decl_pattern.finditer(code):
                matrix_vars.add(m.group(1))

        # Step 2: Handle `expr *= floatNxN(...)` → `expr = mul(expr, floatNxN(...))`
        for mtype in matrix_types:
            code = self._replace_mul_assign_constructor(code, mtype)

        # Step 3: Handle `expr *= matrixVar` → `expr = mul(expr, matrixVar)`
        for mvar in matrix_vars:
            pattern = re.compile(
                r'(\b\w+(?:\.\w+)?)\s*\*=\s*(' + re.escape(mvar) + r')\b'
            )
            code = pattern.sub(r'\1 = mul(\1, \2)', code)

        # Step 4: Handle `expr * floatNxN(...)` → `mul(expr, floatNxN(...))`
        for mtype in matrix_types:
            code = self._replace_mul_constructor(code, mtype)

        # Step 5: Handle `expr * matrixVar` → `mul(expr, matrixVar)`
        for mvar in matrix_vars:
            pattern = re.compile(
                r'(\b\w+(?:\.\w+)*)\s*\*\s*(' + re.escape(mvar) + r')\b'
            )
            code = pattern.sub(r'mul(\1, \2)', code)

        return code

    def _find_balanced_parens(self, code: str, start: int) -> int:
        """Find the index of the closing ')' that balances the '(' at position start.
        Returns -1 if not found."""
        if start >= len(code) or code[start] != '(':
            return -1
        depth = 0
        for i in range(start, len(code)):
            if code[i] == '(':
                depth += 1
            elif code[i] == ')':
                depth -= 1
                if depth == 0:
                    return i
        return -1

    def _replace_mul_assign_constructor(self, code: str, mtype: str) -> str:
        """Replace `expr *= floatNxN(...)` with `expr = mul(expr, floatNxN(...))`
        using balanced parenthesis matching."""
        # Find pattern: <ident> *= <mtype>(
        pattern = re.compile(
            r'(\b\w+(?:\.\w+)?)\s*\*=\s*(' + re.escape(mtype) + r')\s*\('
        )
        offset = 0
        while True:
            m = pattern.search(code, offset)
            if not m:
                break
            lhs = m.group(1)
            type_name = m.group(2)
            paren_start = m.end() - 1  # position of '('
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue
            constructor = code[m.start(2):paren_end + 1]  # e.g. "float2x2(...)"
            replacement = f'{lhs} = mul({lhs}, {constructor})'
            code = code[:m.start()] + replacement + code[paren_end + 1:]
            offset = m.start() + len(replacement)
        return code

    def _replace_mul_constructor(self, code: str, mtype: str) -> str:
        """Replace `expr * floatNxN(...)` with `mul(expr, floatNxN(...))`
        using balanced parenthesis matching.

        Handles both simple variable left operands (e.g. `st * float2x2(...)`)
        and constructor call left operands (e.g. `float3(st, 1.) * float3x3(...)`).
        """
        # Find all occurrences of `* <mtype>(`
        star_mtype_pattern = re.compile(r'\*\s*(' + re.escape(mtype) + r')\s*\(')
        offset = 0
        while True:
            m = star_mtype_pattern.search(code, offset)
            if not m:
                break
            star_pos = m.start()  # position of '*'

            # Check it's not *= 
            if star_pos + 1 < len(code) and code[star_pos + 1] == '=':
                offset = m.end()
                continue

            # Find the right operand: floatNxN(...) with balanced parens
            rhs_type_start = m.start(1)
            rhs_paren_start = m.end() - 1  # position of '('
            rhs_paren_end = self._find_balanced_parens(code, rhs_paren_start)
            if rhs_paren_end == -1:
                offset = m.end()
                continue
            rhs = code[rhs_type_start:rhs_paren_end + 1]

            # Find the left operand by scanning backwards from star_pos
            lhs_end = star_pos
            # Skip whitespace before *
            i = lhs_end - 1
            while i >= 0 and code[i] in ' \t':
                i -= 1
            if i < 0:
                offset = rhs_paren_end + 1
                continue

            # Check if left operand ends with ')' — could be a constructor call
            if code[i] == ')':
                # Scan backwards for matching '('
                depth = 0
                j = i
                while j >= 0:
                    if code[j] == ')':
                        depth += 1
                    elif code[j] == '(':
                        depth -= 1
                        if depth == 0:
                            break
                    j -= 1
                if j < 0:
                    offset = rhs_paren_end + 1
                    continue
                # Now scan backwards from '(' to find the function/type name
                k = j - 1
                while k >= 0 and code[k] in ' \t':
                    k -= 1
                # Collect the identifier (e.g. float3, myFunc)
                ident_end = k + 1
                while k >= 0 and (code[k].isalnum() or code[k] == '_'):
                    k -= 1
                lhs_start = k + 1
                lhs = code[lhs_start:i + 1]
            else:
                # Simple identifier (possibly with swizzle like st.xy)
                ident_end = i + 1
                while i >= 0 and (code[i].isalnum() or code[i] == '_' or code[i] == '.'):
                    i -= 1
                lhs_start = i + 1
                lhs = code[lhs_start:ident_end]

            if not lhs.strip():
                offset = rhs_paren_end + 1
                continue

            replacement = f'mul({lhs.strip()}, {rhs})'
            code = code[:lhs_start] + replacement + code[rhs_paren_end + 1:]
            offset = lhs_start + len(replacement)
        return code

    # ── Shadertoy 内置变量处理 ──

    def _map_shadertoy_uniforms(self, code: str) -> str:
        """将 Shadertoy 内置变量映射为 UE4 对应方案"""
        result = code

        # 按名称长度降序处理，避免 iTime 匹配到 iTimeDelta 前缀
        sorted_uniforms = sorted(SHADERTOY_UNIFORMS.items(), key=lambda x: len(x[0]), reverse=True)

        for uniform_name, info in sorted_uniforms:
            if re.search(r'\b' + re.escape(uniform_name) + r'\b', result):
                self.used_uniforms.add(uniform_name)
                ue4_replacement = info['ue4']
                result = re.sub(
                    r'\b' + re.escape(uniform_name) + r'\b',
                    ue4_replacement,
                    result
                )
                self.warnings.append(
                    f'{uniform_name} → {ue4_replacement} ({info["desc"]})'
                )

        return result

    def _handle_fragcoord(self, code: str) -> str:
        """处理 fragCoord / iResolution 的 UV 坐标转换

        Shadertoy 中常见模式:
            vec2 uv = fragCoord / iResolution.xy;
        转换为 UE4:
            float2 uv = UV;  // UE4 Custom Node 的 UV 坐标已经是 0~1 范围
        """
        result = code

        # 模式1: uv = fragCoord / iResolution.xy
        # 或: uv = fragCoord.xy / iResolution.xy
        uv_pattern = re.compile(
            r'(\w+)\s*=\s*fragCoord(?:\.xy)?\s*/\s*(?:ViewSize|iResolution)(?:\.xy)?\s*;'
        )
        match = uv_pattern.search(result)
        if match:
            uv_var = match.group(1)
            result = uv_pattern.sub(f'{uv_var} = UV; // fragCoord/iResolution → UV (0~1)', result)
            self.warnings.append('fragCoord/iResolution 模式替换为 UV 输入。请将 UV 作为 Custom Node 的输入参数连接 TextureCoordinate 节点。')

        # 模式2: 直接使用 fragCoord（未除以 iResolution）
        # 用 word boundary 正则检测，排除注释中的出现
        if re.search(r'\bfragCoord\b', result):
            # 只替换非注释行中的 fragCoord
            lines = result.split('\n')
            replaced = False
            for i, line in enumerate(lines):
                # 跳过注释行
                stripped = line.lstrip()
                if stripped.startswith('//'):
                    continue
                # 移除行内注释后检测
                code_part = line.split('//')[0]
                if re.search(r'\bfragCoord\b', code_part):
                    lines[i] = re.sub(r'\bfragCoord\b', '(UV * ViewSize)', line)
                    replaced = True
            if replaced:
                result = '\n'.join(lines)
                self.warnings.append('fragCoord 替换为 (UV * ViewSize)。请确保 UV 和 ViewSize 已作为 Custom Node 的输入。')

        return result

    def _handle_fragcolor(self, code: str) -> str:
        """将 fragColor 赋值转换为 return 语句

        Shadertoy: fragColor = vec4(color, 1.0);
        UE4:       return float4(color, 1.0);
        """
        result = code

        # 使用正则找到所有 fragColor = <expr>; 并用 return <expr>; 替换最后一个
        frag_assignments = list(re.finditer(
            r'\bfragColor\s*=\s*([^;]+);',
            result
        ))

        if frag_assignments:
            # 最后一个赋值替换为 return
            last = frag_assignments[-1]
            expr = last.group(1).strip()
            result = result[:last.start()] + f'return {expr};' + result[last.end():]

            # 之前的赋值替换为临时变量（如果有多个）
            # 需要倒序处理以保持索引有效
            for match in reversed(frag_assignments[:-1]):
                result = (result[:match.start()] +
                          result[match.start():match.end()].replace('fragColor', '_fragColor_temp') +
                          result[match.end():])
                self.warnings.append(
                    f'中间 fragColor 赋值替换为 _fragColor_temp'
                )
        else:
            # 没有 fragColor 赋值，检查是否已经有 return
            has_return = bool(re.search(r'\breturn\b', result))
            if not has_return:
                self.warnings.append(
                    '未找到 fragColor 赋值或 return 语句。'
                    '请确保 Custom Node 代码最后有 return 语句。'
                )

        return result

    # ── 组装最终代码 ──

    def _assemble_custom_node(self, helpers: List[str], main_code: str) -> str:
        """组装最终的 UE4 Custom Node 代码（struct 封装模式）

        将辅助函数封装在 struct 中，通过实例调用。
        这是 UE4 Custom Node 中定义多个函数的标准方式。

        输出格式:
            // 1. struct definition with all helper functions
            struct ShaderFuncs {
                float helper1(...) { ... }
                float2 helper2(...) { ... }
            };
            ShaderFuncs F;

            // 2. main logic using F.helper1(), F.helper2()
            ...
            return result;
        """
        parts = []

        # Separate globals (#define, const) from actual functions
        global_lines = []
        func_bodies = []
        for h in helpers:
            if self._is_global_code(h):
                global_lines.append(h)
            else:
                func_bodies.append(h)

        # 1. Global declarations (#define → static const, const stays as-is)
        if global_lines:
            parts.append('// ── Global Declarations ──')
            for g in global_lines:
                converted_global = self._convert_defines_to_const(g)
                parts.append(converted_global)
            parts.append('')

        # 2. Struct encapsulation
        if func_bodies:
            struct_name = 'ShaderFuncs'
            instance_name = 'F'

            # Detect external input variables used inside struct functions
            # These need to be declared as struct members so they're accessible
            struct_code_combined = '\n'.join(func_bodies)
            external_members = self._detect_external_vars_in_struct(struct_code_combined)

            parts.append(f'// 1. Struct definition with all helper functions')
            parts.append(f'struct {struct_name}')
            parts.append('{')

            # Add member variables for external inputs (Time, ViewSize, etc.)
            if external_members:
                for var_name, var_type in external_members:
                    parts.append(f'    {var_type} {var_name};')
                parts.append('')

            for i, func in enumerate(func_bodies):
                # Indent each function body inside struct
                indented = self._indent_code(func, '    ')
                parts.append(indented)
                if i < len(func_bodies) - 1:
                    parts.append('')

            parts.append('};')
            parts.append('')
            parts.append(f'{struct_name} {instance_name};')

            # Assign external input values to struct members
            if external_members:
                for var_name, var_type in external_members:
                    parts.append(f'{instance_name}.{var_name} = {var_name};')

            parts.append('')

            # 3. Rewrite main_code: replace direct function calls with F.func()
            # Build a list of function names from the helpers
            func_names = self._extract_func_names(func_bodies)
            main_code = self._rewrite_calls_to_struct(main_code, func_names, instance_name)

        # 4. Main logic code
        parts.append('// 2. Main logic')
        parts.append(main_code)

        return '\n'.join(parts)

    def _detect_external_vars_in_struct(self, struct_code: str) -> list:
        """Detect external input variables (Time, ViewSize, etc.) used inside struct functions.

        These variables come from Custom Node input pins and are not accessible
        inside struct scope. They need to be declared as struct member variables.

        Returns:
            List of (var_name, hlsl_type) tuples for variables that need to be
            struct members.
        """
        members = []
        for var_name, var_type in UE4_INPUT_VAR_TYPES.items():
            # Check if this variable is referenced in the struct function bodies
            if re.search(r'\b' + re.escape(var_name) + r'\b', struct_code):
                members.append((var_name, var_type))
        return members

    def _is_global_code(self, code: str) -> bool:
        """Check if a code block is global declarations (#define, const, etc.)
        rather than a function definition."""
        stripped = code.strip()
        # If it contains a function body (has { } with a return type + name pattern), it's a function
        func_pattern = re.compile(
            r'^\s*(?:static\s+)?(?:inline\s+)?'
            r'(?:float[234]?|int[234]?|half[234]?|bool|void|float[234]x[234])'
            r'\s+\w+\s*\(',
            re.MULTILINE
        )
        if func_pattern.search(stripped):
            return False
        # Otherwise it's global code (#define, const, etc.)
        return True

    def _convert_defines_to_const(self, code: str) -> str:
        """Convert #define macros to static const declarations.

        #define PI 3.14159  →  static const float PI = 3.14159;
        #define SIZE 10     →  static const int SIZE = 10;

        This ensures the code can be parsed by hlsl_parser (which doesn't
        support preprocessor directives) and works correctly in UE4 Custom Nodes.
        """
        lines = code.split('\n')
        result_lines = []
        for line in lines:
            stripped = line.strip()
            # Match #define NAME VALUE (simple constant macros only)
            m = re.match(r'#define\s+(\w+)\s+(.+)', stripped)
            if m:
                name = m.group(1)
                value = m.group(2).strip()
                # Determine type from value
                const_type = self._infer_define_type(value)
                result_lines.append(f'static const {const_type} {name} = {value};')
            else:
                result_lines.append(line)
        return '\n'.join(result_lines)

    def _infer_define_type(self, value: str) -> str:
        """Infer the HLSL type for a #define value."""
        value = value.strip()
        # Check if it's an expression containing float operations
        if '.' in value or 'PI' in value or '/' in value:
            return 'float'
        # Check if it's a pure integer
        if re.match(r'^-?\d+$', value):
            return 'int'
        # Default to float for expressions
        return 'float'

    def _indent_code(self, code: str, indent: str) -> str:
        """Add indentation to each line of code."""
        lines = code.split('\n')
        return '\n'.join(indent + line if line.strip() else line for line in lines)

    def _extract_func_names(self, func_bodies: List[str]) -> List[str]:
        """Extract function names from function definition code blocks."""
        names = []
        func_pattern = re.compile(
            r'(?:float[234]?|int[234]?|half[234]?|bool|void|float[234]x[234])'
            r'\s+(\w+)\s*\('
        )
        for body in func_bodies:
            for m in func_pattern.finditer(body):
                name = m.group(1)
                if name not in ('if', 'for', 'while', 'return', 'define'):
                    names.append(name)
        return list(dict.fromkeys(names))  # deduplicate preserving order

    def _rewrite_calls_to_struct(self, code: str, func_names: List[str],
                                  instance_name: str) -> str:
        """Rewrite direct function calls to struct instance calls.

        e.g. square(st) → F.square(st)
             trail(st)  → F.trail(st)
        """
        result = code
        for name in func_names:
            # Match function call: name( but not already prefixed with F.
            # Use negative lookbehind to avoid double-prefixing
            pattern = re.compile(
                r'(?<!\.)\b' + re.escape(name) + r'\s*\('
            )
            result = pattern.sub(f'{instance_name}.{name}(', result)
        return result

    # ── 输入参数收集 ──

    def _get_input_params(self) -> List[Dict[str, str]]:
        """获取 Custom Node 需要的输入参数"""
        params = []

        # UV 坐标（几乎总是需要的）
        params.append({
            'name': 'UV',
            'type': 'float2',
            'desc': 'Texture Coordinate (connect TextureCoordinate node)',
        })

        # Time
        if 'iTime' in self.used_uniforms or 'iGlobalTime' in self.used_uniforms:
            params.append({
                'name': 'Time',
                'type': 'float',
                'desc': 'Time in seconds (connect Time node)',
            })

        # ViewSize
        if 'iResolution' in self.used_uniforms:
            params.append({
                'name': 'ViewSize',
                'type': 'float2',
                'desc': 'Screen resolution (connect ViewSize node or SceneTexture:SceneSize)',
            })

        return params

    def _get_texture_inputs(self) -> List[Dict[str, str]]:
        """获取需要的纹理输入"""
        textures = []
        for ch in sorted(self.texture_channels):
            textures.append({
                'name': f'Texture{ch}',
                'type': 'Texture2D',
                'desc': f'Texture for iChannel{ch} (connect TextureSample node)',
            })
        return textures


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def convert_shadertoy(glsl_code: str) -> ShadertoyConvertResult:
    """从 GLSL 代码字符串转换

    参数:
        glsl_code: Shadertoy GLSL 源代码
    返回:
        ShadertoyConvertResult
    """
    converter = ShadertoyConverter()
    return converter.convert(glsl_code)


def convert_shadertoy_file(file_path: str) -> ShadertoyConvertResult:
    """从 GLSL 文件转换

    参数:
        file_path: .glsl 文件路径
    返回:
        ShadertoyConvertResult
    """
    if not os.path.exists(file_path):
        result = ShadertoyConvertResult()
        result.errors.append(f'文件不存在: {file_path}')
        return result

    with open(file_path, 'r', encoding='utf-8') as f:
        glsl_code = f.read()

    return convert_shadertoy(glsl_code)


def print_result(result: ShadertoyConvertResult):
    """格式化输出转换结果"""
    print(f"\n{'═' * 60}")
    print('  Shadertoy → UE4 Custom Node 转换结果')
    print(f"{'═' * 60}")

    if result.errors:
        print(f'\n错误:')
        for err in result.errors:
            print(f'  X {err}')
        return

    if result.warnings:
        print(f'\n警告:')
        for warn in result.warnings:
            print(f'  ! {warn}')

    if result.input_params:
        print(f'\nCustom Node 输入参数:')
        for param in result.input_params:
            print(f'  - {param["name"]} ({param["type"]}): {param["desc"]}')

    if result.texture_inputs:
        print(f'\n纹理输入:')
        for tex in result.texture_inputs:
            print(f'  - {tex["name"]} ({tex["type"]}): {tex["desc"]}')

    if result.helper_functions:
        print(f'\n辅助函数: {", ".join(result.helper_functions)}')

    if result.has_multipass:
        print(f'\n注意: 检测到多 Pass 结构，请手动处理 Buffer 依赖。')

    print(f"\n{'─' * 60}")
    print('HLSL 代码 (可粘贴到 UE4 Custom Node):')
    print(f"{'─' * 60}")
    for i, line in enumerate(result.custom_node_code.split('\n'), 1):
        print(f'  {i:3d} | {line}')
    print(f"{'─' * 60}")

    print(f'\nUE4 使用步骤:')
    print(f'  1. 在材质编辑器中创建 Custom 节点')
    print(f'  2. 将以上 HLSL 代码粘贴到 Code 属性中')
    if result.input_params:
        print(f'  3. 添加以下输入:')
        for param in result.input_params:
            print(f'     - {param["name"]} ({param["type"]})')
    if result.texture_inputs:
        print(f'  4. 添加以下纹理输入:')
        for tex in result.texture_inputs:
            print(f'     - {tex["name"]}')
    print(f'  5. 连接输出到材质属性 (如 Emissive Color)')
    print()


# ═══════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════

def cli_main():
    """命令行入口，供 hlsl2material.py 调用"""
    import argparse

    parser = argparse.ArgumentParser(
        description='Shadertoy GLSL → UE4 Custom Node HLSL 转换器',
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('--glsl', '-g', metavar='FILE',
                             help='GLSL/Shadertoy 源文件路径')
    input_group.add_argument('--glsl-code', '-gc', metavar='CODE',
                             help='GLSL/Shadertoy 代码字符串')

    parser.add_argument('--output', '-o', metavar='FILE',
                        help='输出 HLSL 文件路径')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='安静模式，只输出代码')

    args = parser.parse_args()

    # 执行转换
    if args.glsl:
        result = convert_shadertoy_file(args.glsl)
    elif args.glsl_code:
        result = convert_shadertoy(args.glsl_code)

    # 输出结果
    if args.quiet:
        if result.success:
            print(result.custom_node_code)
        else:
            for err in result.errors:
                print(f'ERROR: {err}', file=sys.stderr)
            sys.exit(1)
    else:
        print_result(result)

    # 保存文件
    if args.output and result.success:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(result.custom_node_code)
        if not args.quiet:
            print(f'已保存到: {args.output}')

    return result


if __name__ == '__main__':
    cli_main()
