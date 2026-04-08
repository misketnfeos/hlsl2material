"""
============================================================
 shadertoy_converter.py
 Shadertoy GLSL → UE4 Custom Node HLSL 转换器
============================================================

将 Shadertoy 的 GLSL 代码转换为 UE4 Material Custom Node 可用的 HLSL 代码。

功能：
  1. GLSL → HLSL 类型映射 (vec2→float2, mat3→float3x3, etc.)
  2. GLSL → HLSL 函数映射 (mix→lerp, fract→frac, mod→glsl_mod, texture→tex2D, etc.)
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
    'mod':           'glsl_mod',  # will be inlined later by _inline_glsl_mod()
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
    'iResolution':      {'ue4': 'float3(ViewSize.y, ViewSize.x, 1.0)',      'desc': 'Screen resolution as float3(width, height, 1.0). ViewSize.yx because UE4 ViewSize is (height, width)'},
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
            # Pre-collect matrix-returning function names from all helpers
            # so individual helper conversion can see cross-function matrix types
            self._known_matrix_funcs = set()
            matrix_types_glsl = {'mat2': 'float2x2', 'mat3': 'float3x3', 'mat4': 'float4x4'}
            for mtype_glsl, mtype_hlsl in matrix_types_glsl.items():
                for func in helpers:
                    for fm in re.finditer(r'\b(?:' + re.escape(mtype_glsl) + r'|' + re.escape(mtype_hlsl) + r')\s+(\w+)\s*\(', func):
                        fname = fm.group(1)
                        if fname not in ('if', 'for', 'while', 'return', 'define'):
                            self._known_matrix_funcs.add(fname)

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

            # Step 10: Zero-initialize uninitialized variables
            # GLSL auto-initializes locals to 0, HLSL does not.
            # e.g. "float i, s;" → "float i = 0, s = 0;"
            final_code = self._zero_init_uninitialized_vars(final_code)

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

        code = '\n'.join(processed)

        # Expand #define macros (function-like, expression, alias, multi-line)
        code = self._expand_define_macros(code)

        return code

    def _expand_define_macros(self, code: str) -> str:
        """Expand all #define macros by text substitution.

        Handles:
          1. Multi-line continuation (backslash-newline)
          2. Function-like macros: #define P(z) (vec3(...))
          3. Expression macros:    #define T (sin(iTime*.6)*64.)
          4. Alias macros:         #define N normalize
          5. Simple constant macros: #define PI 3.14159 → kept as static const

        Macros are expanded in definition order. Function-like and expression
        macros are inlined; simple numeric constants become static const.
        """
        # Step 1: Join backslash-continued lines
        code = re.sub(r'\\\s*\n\s*', ' ', code)

        # Step 2: Collect all #define directives
        lines = code.split('\n')
        macros = []  # list of (name, params_or_None, body, line_index)
        non_define_lines = []
        define_indices = set()

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Function-like macro: #define NAME(params) body
            m = re.match(r'#define\s+(\w+)\(([^)]*)\)\s*(.*)', stripped)
            if m:
                name = m.group(1)
                params = [p.strip() for p in m.group(2).split(',') if p.strip()]
                body = m.group(3).strip()
                macros.append((name, params, body, i))
                define_indices.add(i)
                continue

            # Object-like macro: #define NAME body
            m = re.match(r'#define\s+(\w+)\s+(.*)', stripped)
            if m:
                name = m.group(1)
                body = m.group(2).strip()
                macros.append((name, None, body, i))
                define_indices.add(i)
                continue

            # Bare #define NAME (no body) — skip
            m = re.match(r'#define\s+(\w+)\s*$', stripped)
            if m:
                define_indices.add(i)
                continue

        # Step 3: Build output lines (without #define lines)
        result_lines = []
        for i, line in enumerate(lines):
            if i not in define_indices:
                result_lines.append(line)

        result_code = '\n'.join(result_lines)

        # Step 4: Expand macros in the remaining code
        # Also expand macros within other macro bodies (forward references)
        # Process in definition order; do multiple passes for cross-references
        for _pass in range(3):  # max 3 passes for nested macro expansion
            changed = False
            for name, params, body, _ in macros:
                if params is not None:
                    # Function-like macro expansion
                    new_code = self._expand_func_macro(result_code, name, params, body)
                    if new_code != result_code:
                        result_code = new_code
                        changed = True
                else:
                    # Object-like macro: check if it's a simple constant
                    if self._is_simple_constant(body):
                        # Keep as static const — don't inline
                        continue
                    # Expression or alias macro — inline expand
                    # Use word boundary to avoid partial matches
                    pattern = re.compile(r'\b' + re.escape(name) + r'\b')
                    new_code = pattern.sub(body, result_code)
                    if new_code != result_code:
                        result_code = new_code
                        changed = True
            if not changed:
                break

        # Step 5: Re-insert simple constant macros as static const declarations
        const_lines = []
        for name, params, body, _ in macros:
            if params is None and self._is_simple_constant(body):
                const_type = self._infer_define_type(body)
                const_lines.append(f'static const {const_type} {name} = {body};')

        if const_lines:
            result_code = '\n'.join(const_lines) + '\n' + result_code

        return result_code

    def _is_simple_constant(self, value: str) -> bool:
        """Check if a #define value is a simple numeric constant.

        Returns True for: 3.14159, 10, -5, 2e2, 1.0f
        Returns False for: (sin(x)+1), normalize, vec3(1,0,0)
        """
        return bool(re.match(r'^-?(\d+\.?\d*|\d*\.\d+)([eE][+-]?\d+)?[fF]?$', value.strip()))

    def _expand_func_macro(self, code: str, name: str, params: list, body: str) -> str:
        """Expand a function-like macro in code.

        #define P(z) (vec3(cos((z)*.015)*16., ...))
        P(p.z) → (vec3(cos((p.z)*.015)*16., ...))
        """
        # Pattern: NAME followed by ( with balanced parens
        pattern = re.compile(r'\b' + re.escape(name) + r'\s*\(')
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

            args_str = code[paren_start + 1:paren_end]
            args = self._split_args_balanced(args_str)

            if len(args) != len(params):
                # Argument count mismatch — skip (might be a different function)
                offset = paren_end + 1
                continue

            # Substitute parameters in body
            expanded = body
            for param, arg in zip(params, args):
                # Replace parameter with argument, using word boundaries
                expanded = re.sub(r'\b' + re.escape(param) + r'\b', arg.strip(), expanded)

            code = code[:m.start()] + expanded + code[paren_end + 1:]
            offset = m.start() + len(expanded)

        return code

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

        # 2b. 内联 glsl_mod() 为表达式（不能在 Custom Node 内定义函数）
        result = self._inline_glsl_mod(result)

        # 3. 构造函数展开（如 vec3(1.0) → float3(1.0, 1.0, 1.0)）
        result = self._expand_constructors(result)

        # 4. 矩阵构造函数
        result = self._convert_matrix_constructors(result)

        # 5. texture 调用转换
        result = self._convert_texture_calls(result)

        # 6. 修复 GLSL 特有语法
        result = self._fix_glsl_syntax(result)

        # 7. 修复向量构造函数维度溢出
        # After macro expansion, constructors like float3(Z.z, 0, -Z) can appear
        # where -Z is a float3, making 5 components for a float3 target.
        # GLSL truncates silently; HLSL rejects this. Add swizzle to truncate.
        result = self._fix_constructor_dimension_overflow(result)

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

    def _inline_glsl_mod(self, code: str) -> str:
        """Inline glsl_mod(a, b) calls to ((a) - (b) * floor((a) / (b))).

        UE4 Custom Node code runs inside a function body, so we cannot define
        standalone helper functions.  Instead we expand each glsl_mod() call
        into the equivalent arithmetic expression.
        """
        pattern = re.compile(r'\bglsl_mod\s*\(')
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
            inner = code[paren_start + 1:paren_end]
            # Split into two args at top-level comma
            args = self._split_args_balanced(inner)
            if len(args) != 2:
                offset = paren_end + 1
                continue
            a = args[0].strip()
            b = args[1].strip()
            replacement = f'(({a}) - ({b}) * floor(({a}) / ({b})))'
            code = code[:m.start()] + replacement + code[paren_end + 1:]
            offset = m.start() + len(replacement)
        return code

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
                # Check if the argument is a scalar expression (not a known vector
                # variable or constructor). If so, expand by duplicating.
                # Scalar expressions: arithmetic like "ac * 0.02", function calls
                # returning scalar like "sin(x)", single scalar variables, etc.
                # NOT scalar: variable names that could be vectors (handled by
                # _convert_truncation_constructors for actual truncation)
                if self._is_scalar_expression(arg):
                    return f'{type_name}({", ".join([arg] * dim)})'
                # Not a scalar literal — leave as-is (truncation cast)
                return m.group(0)

            result = pattern.sub(_expand_match, code)
            code = result

        return code

    def _is_scalar_expression(self, expr: str) -> bool:
        """Check if an expression is likely a scalar (not a vector/matrix).

        Returns True for:
          - Arithmetic expressions: "ac * 0.02", "x + y", "a - b * c"
          - Single scalar variables: "x", "alpha"
          - Scalar function calls: "sin(x)", "abs(y)"
          - Numeric literals: "1.0", "42" (though these are handled elsewhere)

        Returns False for:
          - Vector constructors: "float2(...)", "float3(...)"
          - Expressions with swizzle: "v.xy", "pos.xyz"
          - Expressions that might be vectors (bare identifiers could be vectors,
            but if they contain arithmetic operators they're likely scalar math)
        """
        expr = expr.strip()
        if not expr:
            return False

        # If it contains a vector/matrix constructor, it's not scalar
        if re.search(r'\b(float[234]|int[234]|half[234]|float[234]x[234])\s*\(', expr):
            return False

        # If it ends with a swizzle of length > 1, it's a vector
        swizzle_match = re.search(r'\.[xyzwrgba]{2,}$', expr)
        if swizzle_match:
            return False

        # If it contains arithmetic operators (+, -, *, /), it's likely scalar math
        # (vector math would use the same operators but that's a rare case for
        # single-arg constructors in practice)
        if re.search(r'[+\-*/]', expr):
            return True

        # Single identifier without operators — could be scalar or vector.
        # We can't tell without type info, so be conservative and return False
        # to let _convert_truncation_constructors handle it.
        if re.match(r'^[a-zA-Z_]\w*$', expr):
            return False

        # Single-component swizzle: v.x, v.r — that's a scalar
        if re.search(r'\.[xyzwrgba]$', expr):
            return True

        # Function call without vector constructor (e.g. sin(x), abs(y))
        # These typically return scalar
        if re.match(r'^[a-zA-Z_]\w*\s*\(', expr):
            return True

        # Parenthesized expression — check inside
        if expr.startswith('(') and expr.endswith(')'):
            inner = expr[1:-1].strip()
            return self._is_scalar_expression(inner)

        return False

    def _convert_matrix_constructors(self, code: str) -> str:
        """Convert GLSL-style matrix constructors to HLSL row-major form.

        GLSL matrices are column-major. This method handles three cases:

        1. Scalar args (4/9/16 scalars): Transpose the fill order.
           GLSL mat2(a,b,c,d) fills col0=(a,b), col1=(c,d)
           → HLSL float2x2(float2(a,c), float2(b,d))

        2. Single vec4 arg for mat2: Extract components with transpose.
           GLSL mat2(v) fills col0=(v.x,v.y), col1=(v.z,v.w)
           → HLSL float2x2(float2(v.x,v.z), float2(v.y,v.w))

        3. N vector args for NxN matrix: GLSL treats them as columns.
           GLSL mat3(col0, col1, col2) → HLSL transpose(float3x3(col0, col1, col2))
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
        """Rewrite GLSL-style matrix constructors to HLSL row-major form.

        GLSL matrices are column-major: mat2(a,b,c,d) fills columns first:
            col0=(a,b), col1=(c,d) → matrix | a c |
                                             | b d |
        HLSL float2x2(row0, row1) is row-major:
            float2x2(float2(a,c), float2(b,d)) → matrix | a c |
                                                          | b d |

        So scalar args must be transposed: element at col C, row R in GLSL
        maps to args[C * dim + R], and HLSL row R needs args from each column.

        For vector args (N vectors of dim N), GLSL treats them as columns,
        so we wrap with transpose() to convert column-major → row-major.

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
                # Scalar-argument constructor — transpose from column-major to row-major.
                # GLSL fills column-major: args[0..dim-1] = col0, args[dim..2*dim-1] = col1, etc.
                # HLSL needs rows: row R = (args[0*dim+R], args[1*dim+R], ..., args[(dim-1)*dim+R])
                rows = []
                for r in range(dim):
                    row_args = [args[c * dim + r] for c in range(dim)]
                    rows.append(f'{row_type}({", ".join(row_args)})')
                new_constructor = f'{mtype}({", ".join(rows)})'
                code = code[:m.start()] + new_constructor + code[paren_end + 1:]
                offset = m.start() + len(new_constructor)
            elif len(args) == 1 and dim == 2 and not re.match(r'^[+-]?\d+\.?\d*[fF]?$', args[0].strip()):
                # Single vector arg for float2x2: mat2(vec4_expr)
                # GLSL fills column-major from the vec4 components:
                #   col0=(v.x, v.y), col1=(v.z, v.w)
                #   matrix: | v.x  v.z |
                #           | v.y  v.w |
                # HLSL rows: row0=(v.x, v.z), row1=(v.y, v.w)
                expr = args[0].strip()
                new_constructor = f'{mtype}(float2(({expr}).x, ({expr}).z), float2(({expr}).y, ({expr}).w))'
                code = code[:m.start()] + new_constructor + code[paren_end + 1:]
                offset = m.start() + len(new_constructor)
            elif len(args) == dim and dim >= 2:
                # N vector args for NxN matrix: GLSL treats these as N column vectors.
                # e.g. mat2(col0, col1), mat3(col0, col1, col2)
                # Wrap with transpose() to convert column-major → row-major.
                original = code[m.start():paren_end + 1]
                new_constructor = f'transpose({original})'
                code = code[:m.start()] + new_constructor + code[paren_end + 1:]
                offset = m.start() + len(new_constructor)
            else:
                # Single scalar (identity matrix) or other — skip
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

        # 修复 GLSL mod(a, b) → glsl_mod(a, b)（已在函数映射中处理）

        # 修复 GLSL 特有的类型转换
        # int(x) → (int)(x) — HLSL 也支持函数式转换，保留即可

        # 修复 GLSL 向量*矩阵乘法 → HLSL mul()
        result = self._convert_matrix_multiply(result)

        # 修复 GLSL 截断构造函数 → HLSL swizzle
        # e.g. float2(expr_returning_float3) → (expr_returning_float3).xy
        result = self._convert_truncation_constructors(result)

        return result

    def _convert_truncation_constructors(self, code: str) -> str:
        """Convert GLSL truncation constructors to HLSL swizzle.

        In GLSL, vec2(vec3_value) truncates to the first 2 components.
        In HLSL, float2(float3_value) is ILLEGAL — must use .xy swizzle.

        Converts:
            float2(<single_expr>) → (<single_expr>).xy   (when expr is not a scalar literal)
            float3(<single_expr>) → (<single_expr>).xyz   (when expr is not a scalar literal)

        Only applies when:
            - There is exactly 1 argument (no commas at depth 0)
            - The argument is NOT a numeric literal (those are handled by _expand_constructors)
            - The argument contains a function call or variable that likely returns a higher-dim vector
        """
        # Swizzle suffixes for truncation
        swizzle_map = {
            'float2': '.xy',
            'float3': '.xyz',
        }

        for target_type, swizzle in swizzle_map.items():
            code = self._rewrite_truncation(code, target_type, swizzle)

        return code

    def _rewrite_truncation(self, code: str, target_type: str, swizzle: str) -> str:
        """Rewrite float2(expr) → (expr).xy when expr is a single non-scalar argument.

        Uses balanced parenthesis matching and heuristics to detect truncation patterns.
        """
        pattern = re.compile(r'\b' + re.escape(target_type) + r'\s*\(')
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

            args_str = code[paren_start + 1:paren_end]
            args = self._split_args_balanced(args_str)

            if len(args) == 1:
                arg = args[0].strip()
                # Skip numeric literals — _expand_constructors handles those
                if re.match(r'^-?(\d+\.?\d*|\d*\.\d+)[fF]?$', arg):
                    offset = paren_end + 1
                    continue

                # Skip if the arg is already the same type (e.g. float2(float2(...)))
                # or a lower-dim type — those are valid
                # We only want to convert when the arg is likely a higher-dim vector
                # Heuristic: the arg contains a function call (has parens) or is a
                # variable/expression that is NOT a floatN constructor of same or lower dim
                is_same_or_lower_constructor = False
                for check_type in self._get_same_or_lower_types(target_type):
                    if re.match(r'\b' + re.escape(check_type) + r'\s*\(', arg):
                        is_same_or_lower_constructor = True
                        break

                if is_same_or_lower_constructor:
                    offset = paren_end + 1
                    continue

                # This looks like a truncation: float2(higher_dim_expr) → (higher_dim_expr).xy
                new_code = f'({arg}){swizzle}'
                code = code[:m.start()] + new_code + code[paren_end + 1:]
                offset = m.start() + len(new_code)
            else:
                # Multiple args — not a truncation, skip
                offset = paren_end + 1

        return code

    def _get_same_or_lower_types(self, target_type: str) -> list:
        """Return vector types that are same dimension or lower than target_type.

        For float2: [float2, int2, uint2] — constructing from these is valid
        For float3: [float2, float3, int2, int3, uint2, uint3] — valid
        """
        dim_map = {'float2': 2, 'float3': 3, 'float4': 4}
        target_dim = dim_map.get(target_type, 0)
        result = []
        for t, d in dim_map.items():
            if d <= target_dim:
                result.append(t)
        # Also add int/uint variants
        for prefix in ['int', 'uint', 'half']:
            for d in range(2, target_dim + 1):
                result.append(f'{prefix}{d}')
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
        # Exclude function declarations (where the name is followed by '(')
        matrix_vars = set()
        for mtype in matrix_types:
            decl_pattern = re.compile(r'\b' + re.escape(mtype) + r'\s+(\w+)\b')
            for m in decl_pattern.finditer(code):
                var_name = m.group(1)
                # Check if followed by '(' — that's a function declaration, not a variable
                after = code[m.end():].lstrip()
                if after.startswith('('):
                    continue
                matrix_vars.add(var_name)

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

        # Step 6: Handle `floatNxN(...) * expr` → `mul(floatNxN(...), expr)`
        # This covers mat*vec (matrix on left side), e.g. float2x2(...)*fragCoord
        for mtype in matrix_types:
            code = self._replace_mul_matrix_left(code, mtype)

        # Step 7: Handle `matrixVar * expr` → `mul(matrixVar, expr)`
        for mvar in matrix_vars:
            pattern = re.compile(
                r'\b(' + re.escape(mvar) + r')\s*\*\s*(\w+(?:\.\w+)*)\b'
            )
            code = pattern.sub(r'mul(\1, \2)', code)

        # Step 8: Collect function names that return matrix types
        # Also include pre-collected matrix functions from all helpers
        matrix_return_funcs = set(getattr(self, '_known_matrix_funcs', set()))
        for mtype in matrix_types:
            # Match function definitions: float2x2 funcName(
            func_def_pattern = re.compile(
                r'\b' + re.escape(mtype) + r'\s+(\w+)\s*\('
            )
            for fm in func_def_pattern.finditer(code):
                fname = fm.group(1)
                # Exclude keywords and type names
                if fname not in ('if', 'for', 'while', 'return', 'define',
                                 'float2x2', 'float3x3', 'float4x4'):
                    matrix_return_funcs.add(fname)

        # Step 9: Handle multiplication with functions returning matrix types
        for fname in matrix_return_funcs:
            code = self._replace_mul_matrix_func(code, fname)

        return code

    def _replace_mul_matrix_func(self, code: str, func_name: str) -> str:
        """Replace multiplication involving a function call that returns a matrix type.

        Handles:
          - expr *= funcName(...)        → expr = mul(expr, funcName(...))
          - expr *= X.funcName(...)      → expr = mul(expr, X.funcName(...))
          - expr * funcName(...)         → mul(expr, funcName(...))
          - expr * X.funcName(...)       → mul(expr, X.funcName(...))
          - funcName(...) * expr         → mul(funcName(...), expr)
          - X.funcName(...) * expr       → mul(X.funcName(...), expr)
        """
        # --- Part A: Handle `expr *= [prefix.]funcName(...)` ---
        mul_assign_pat = re.compile(
            r'(\b\w+(?:\.\w+)?)\s*\*=\s*(\w+\.)?(' + re.escape(func_name) + r')\s*\('
        )
        offset = 0
        while True:
            m = mul_assign_pat.search(code, offset)
            if not m:
                break
            lhs = m.group(1)
            prefix = m.group(2) or ''  # e.g. 'F.' or ''
            paren_start = m.end() - 1
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue
            func_call = prefix + func_name + code[paren_start:paren_end + 1]
            replacement = f'{lhs} = mul({lhs}, {func_call})'
            code = code[:m.start()] + replacement + code[paren_end + 1:]
            offset = m.start() + len(replacement)

        # --- Part B: Handle `expr * [prefix.]funcName(...)` (matrix on right) ---
        star_func_pat = re.compile(
            r'\*\s*(\w+\.)?(' + re.escape(func_name) + r')\s*\('
        )
        offset = 0
        while True:
            m = star_func_pat.search(code, offset)
            if not m:
                break
            star_pos = m.start()

            # Make sure it's not *=
            if star_pos + 1 < len(code) and code[star_pos + 1] == '=':
                offset = m.end()
                continue

            # Check if already inside a mul() call
            pre = code[:star_pos].rstrip()
            if pre.endswith('mul(') or pre.endswith('mul ('):
                offset = m.end()
                continue

            prefix = m.group(1) or ''
            paren_start = m.end() - 1
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue
            rhs = prefix + func_name + code[paren_start:paren_end + 1]

            # Scan backwards for the left operand
            i = star_pos - 1
            while i >= 0 and code[i] in ' \t\n\r':
                i -= 1
            if i < 0:
                offset = paren_end + 1
                continue

            if code[i] == ')':
                # Left operand ends with ) — scan for matching (
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
                    offset = paren_end + 1
                    continue
                k = j - 1
                while k >= 0 and code[k] in ' \t':
                    k -= 1
                while k >= 0 and (code[k].isalnum() or code[k] == '_' or code[k] == '.'):
                    k -= 1
                lhs_start = k + 1
                lhs = code[lhs_start:i + 1]
            else:
                ident_end = i + 1
                while i >= 0 and (code[i].isalnum() or code[i] == '_' or code[i] == '.'):
                    i -= 1
                lhs_start = i + 1
                lhs = code[lhs_start:ident_end]

            if not lhs.strip():
                offset = paren_end + 1
                continue

            replacement = f'mul({lhs.strip()}, {rhs})'
            code = code[:lhs_start] + replacement + code[paren_end + 1:]
            offset = lhs_start + len(replacement)

        # --- Part C: Handle `[prefix.]funcName(...) * expr` (matrix on left) ---
        func_left_pat = re.compile(
            r'(\w+\.)?\b(' + re.escape(func_name) + r')\s*\('
        )
        offset = 0
        while True:
            m = func_left_pat.search(code, offset)
            if not m:
                break
            prefix = m.group(1) or ''
            full_start = m.start(1) if m.group(1) else m.start(2)
            paren_start = m.end() - 1
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue

            # Check if there's a `*` after the closing paren
            j = paren_end + 1
            while j < len(code) and code[j] in ' \t\n\r':
                j += 1
            if j >= len(code) or code[j] != '*':
                offset = paren_end + 1
                continue
            # Make sure it's not *=
            if j + 1 < len(code) and code[j + 1] == '=':
                offset = paren_end + 1
                continue

            # Check if already inside mul()
            pre = code[:full_start].rstrip()
            if pre.endswith('mul(') or pre.endswith('mul ('):
                offset = paren_end + 1
                continue

            star_pos = j
            lhs = code[full_start:paren_end + 1]

            # Find the right operand after *
            k = star_pos + 1
            while k < len(code) and code[k] in ' \t\n\r':
                k += 1
            if k >= len(code):
                offset = paren_end + 1
                continue

            rhs_start = k
            if code[k].isalpha() or code[k] == '_':
                while k < len(code) and (code[k].isalnum() or code[k] == '_'):
                    k += 1
                # Check for constructor call or method call
                while k < len(code) and code[k] in ' \t':
                    k += 1
                if k < len(code) and code[k] == '(':
                    rhs_pe = self._find_balanced_parens(code, k)
                    if rhs_pe != -1:
                        k = rhs_pe + 1
                # Check for swizzle
                if k < len(code) and code[k] == '.':
                    k += 1
                    while k < len(code) and code[k].isalpha():
                        k += 1
                rhs = code[rhs_start:k].strip()
            else:
                offset = paren_end + 1
                continue

            if not rhs:
                offset = paren_end + 1
                continue

            replacement = f'mul({lhs}, {rhs})'
            code = code[:full_start] + replacement + code[k:]
            offset = full_start + len(replacement)

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
            # Skip whitespace (including newlines) before *
            i = lhs_end - 1
            while i >= 0 and code[i] in ' \t\n\r':
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

    def _replace_mul_matrix_left(self, code: str, mtype: str) -> str:
        """Replace `floatNxN(...) * expr` with `mul(floatNxN(...), expr)`.

        Handles matrix-on-left multiplication, e.g.:
          float2x2(a,b,c,d) * fragCoord  →  mul(float2x2(a,b,c,d), fragCoord)
        """
        # Find all occurrences of `<mtype>(`
        mtype_pattern = re.compile(r'\b(' + re.escape(mtype) + r')\s*\(')
        offset = 0
        while True:
            m = mtype_pattern.search(code, offset)
            if not m:
                break
            type_start = m.start(1)
            paren_start = m.end() - 1
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue

            # Check if there's a `*` after the closing paren (skip whitespace)
            j = paren_end + 1
            while j < len(code) and code[j] in ' \t\n\r':
                j += 1
            if j >= len(code) or code[j] != '*':
                offset = paren_end + 1
                continue
            # Make sure it's not *= 
            if j + 1 < len(code) and code[j + 1] == '=':
                offset = paren_end + 1
                continue

            # Check if already inside a mul() call
            pre = code[:type_start].rstrip()
            if pre.endswith('mul(') or pre.endswith('mul ('):
                offset = paren_end + 1
                continue

            star_pos = j

            # Find the right operand after *
            k = star_pos + 1
            while k < len(code) and code[k] in ' \t\n\r':
                k += 1
            if k >= len(code):
                offset = paren_end + 1
                continue

            # Right operand could be an identifier (possibly with swizzle)
            # or a constructor call like float3(...)
            if code[k] == '(' or (code[k].isalpha() or code[k] == '_'):
                # Collect identifier
                rhs_start = k
                while k < len(code) and (code[k].isalnum() or code[k] == '_'):
                    k += 1
                # Check for constructor call: ident(
                while k < len(code) and code[k] in ' \t':
                    k += 1
                if k < len(code) and code[k] == '(':
                    rhs_paren_end = self._find_balanced_parens(code, k)
                    if rhs_paren_end == -1:
                        offset = paren_end + 1
                        continue
                    k = rhs_paren_end + 1
                # Check for swizzle after identifier or constructor
                if k < len(code) and code[k] == '.':
                    k += 1
                    while k < len(code) and code[k].isalpha():
                        k += 1
                rhs = code[rhs_start:k].strip()
            else:
                offset = paren_end + 1
                continue

            if not rhs:
                offset = paren_end + 1
                continue

            lhs = code[type_start:paren_end + 1]
            replacement = f'mul({lhs}, {rhs})'
            code = code[:type_start] + replacement + code[k:]
            offset = type_start + len(replacement)
        return code

    # ── 向量构造函数维度溢出修复 ──

    def _fix_constructor_dimension_overflow(self, code: str) -> str:
        """Fix vector constructor dimension overflow after macro expansion.

        In GLSL, vec3(a, b, vec3_val) silently takes components in order and
        truncates. In HLSL, float3(a, b, float3_val) is a compile error.

        This method:
        1. Collects declared variable types from the code
        2. For each floatN(...) constructor, estimates component dimensions
        3. If total components > N, truncates the last argument with a swizzle

        Example: float3(Z.z, 0, -Z) where Z is float3
                 → float3(Z.z, 0, (-Z).x)
        """
        # Step 1: Collect variable type declarations
        var_types = self._collect_variable_types(code)

        # Step 2: Process each vector constructor
        for hlsl_type, target_dim in TYPE_DIMENSIONS.items():
            code = self._fix_overflow_for_type(code, hlsl_type, target_dim, var_types)

        return code

    def _collect_variable_types(self, code: str) -> Dict[str, int]:
        """Collect variable names and their vector dimensions from declarations.

        Scans for patterns like:
            float3 Z = ...;
            float2 uv;
            float3 N, V;
            float3 p = expr, ro=p, Z = normalize(...);  (chain declaration)
            float3x3 mat;  (skipped — not a vector)

        Returns dict mapping variable name → dimension (e.g. {'Z': 3, 'uv': 2})
        """
        var_types: Dict[str, int] = {}

        for hlsl_type, dim in TYPE_DIMENSIONS.items():
            # Find where type keyword starts a declaration, then parse forward
            # with balanced-paren awareness to handle initializers containing
            # commas (e.g. float3(a,b,c)).
            starter = re.compile(
                r'\b' + re.escape(hlsl_type) + r'\s+([a-zA-Z_]\w*)'
            )
            for m in starter.finditer(code):
                # Scan forward from the start of the first variable name
                # to find the full declaration up to the semicolon,
                # respecting nested parentheses.
                start = m.start(1)
                depth = 0
                end = start
                for i in range(start, len(code)):
                    ch = code[i]
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                    elif ch == ';' and depth == 0:
                        end = i
                        break
                else:
                    # No semicolon found; take rest of code
                    end = len(code)

                decl_str = code[start:end]

                # Split by commas at top level (respecting parentheses)
                parts = self._split_args_balanced(decl_str)
                for part in parts:
                    part = part.strip()
                    # Extract the variable name (before any = sign)
                    name_match = re.match(r'([a-zA-Z_]\w*)', part)
                    if name_match:
                        var_types[name_match.group(1)] = dim

        # Also detect function parameters: (float3 N, float3 V, ...)
        param_pattern = re.compile(
            r'\b(float[234]|int[234])\s+([a-zA-Z_]\w*)\s*[,)]'
        )
        for m in param_pattern.finditer(code):
            type_name = m.group(1)
            var_name = m.group(2)
            dim = TYPE_DIMENSIONS.get(type_name)
            if dim is not None:
                var_types[var_name] = dim

        return var_types

    def _estimate_arg_dimension(self, arg: str, var_types: Dict[str, int]) -> int:
        """Estimate the dimension (number of scalar components) of an expression.

        Returns:
            Positive int for known dimension, 0 for unknown.
        """
        arg = arg.strip()

        # Unwrap outer parentheses: (-Z) → -Z
        while arg.startswith('(') and arg.endswith(')'):
            inner = arg[1:-1]
            # Make sure these parens are balanced (not e.g. "(a)+(b)")
            if self._find_balanced_parens(arg, 0) == len(arg) - 1:
                arg = inner.strip()
            else:
                break

        # Unary negation: -expr → same dimension as expr
        if arg.startswith('-') or arg.startswith('+'):
            inner = arg[1:].strip()
            # Only recurse if the inner part is a simple token or wrapped expr
            if re.match(r'^[a-zA-Z_]\w*(?:\.[xyzwrgba]+)?$', inner) or inner.startswith('('):
                return self._estimate_arg_dimension(inner, var_types)

        # Numeric literal → 1 component
        if re.match(r'^-?(?:\d+\.?\d*|\d*\.\d+)(?:[eE][+-]?\d+)?[fF]?$', arg):
            return 1

        # Swizzle access: expr.xyz → len('xyz')
        swizzle_match = re.match(r'^(.+)\.([xyzwrgba]+)$', arg)
        if swizzle_match:
            swizzle = swizzle_match.group(2)
            # Validate it's a real swizzle (all chars from same set)
            if all(c in 'xyzw' for c in swizzle) or all(c in 'rgba' for c in swizzle):
                return len(swizzle)

        # floatN(...) constructor → N components
        ctor_match = re.match(r'^(float[234]|int[234]|half[234])\s*\(', arg)
        if ctor_match:
            type_name = ctor_match.group(1)
            dim = TYPE_DIMENSIONS.get(type_name)
            if dim is not None:
                return dim

        # Bare identifier: look up in var_types
        if re.match(r'^[a-zA-Z_]\w*$', arg):
            return var_types.get(arg, 0)

        # Unknown expression
        return 0

    def _fix_overflow_for_type(self, code: str, hlsl_type: str, target_dim: int,
                               var_types: Dict[str, int]) -> str:
        """Fix dimension overflow for a specific vector constructor type."""
        swizzle_suffixes = {1: '.x', 2: '.xy', 3: '.xyz'}

        pattern = re.compile(r'\b' + re.escape(hlsl_type) + r'\s*\(')
        offset = 0

        while True:
            m = pattern.search(code, offset)
            if not m:
                break

            paren_start = m.end() - 1
            paren_end = self._find_balanced_parens(code, paren_start)
            if paren_end == -1:
                offset = m.end()
                continue

            args_str = code[paren_start + 1:paren_end]
            args = self._split_args_balanced(args_str)

            if len(args) < 2:
                # Single arg or empty — not our case
                offset = paren_end + 1
                continue

            # Estimate dimension for each argument
            dims = [self._estimate_arg_dimension(a, var_types) for a in args]

            # Calculate total known dimension
            # If any dimension is 0 (unknown), we treat it as 1 (scalar heuristic)
            # but only if the argument count == target_dim (suggesting all-scalar intent)
            total = 0
            has_unknown = False
            for d in dims:
                if d == 0:
                    has_unknown = True
                    total += 1  # assume scalar
                else:
                    total += d

            if total <= target_dim:
                # No overflow
                offset = paren_end + 1
                continue

            # Overflow detected — truncate from the last argument backwards
            new_args = list(args)
            remaining = target_dim
            truncated = False

            for i in range(len(new_args)):
                d = dims[i] if dims[i] > 0 else 1
                if remaining <= 0:
                    # This arg and everything after is excess — remove
                    new_args = new_args[:i]
                    truncated = True
                    break
                elif d > remaining:
                    # Need to truncate this argument
                    needed = remaining
                    arg = new_args[i].strip()
                    swizzle = swizzle_suffixes.get(needed)
                    if swizzle:
                        # Wrap complex expressions in parens before adding swizzle
                        if self._needs_parens_for_swizzle(arg):
                            new_args[i] = f'({arg}){swizzle}'
                        else:
                            new_args[i] = f'{arg}{swizzle}'
                    # Remove any subsequent arguments
                    new_args = new_args[:i + 1]
                    truncated = True
                    break
                else:
                    remaining -= d

            if truncated:
                new_args_str = ', '.join(new_args)
                new_constructor = f'{hlsl_type}({new_args_str})'
                code = code[:m.start()] + new_constructor + code[paren_end + 1:]
                offset = m.start() + len(new_constructor)
            else:
                offset = paren_end + 1

        return code

    def _needs_parens_for_swizzle(self, arg: str) -> bool:
        """Check if an expression needs wrapping in parens before adding a swizzle.

        Simple identifiers and already-parenthesized expressions don't need it.
        Negated expressions, binary operations, etc. do need wrapping.
        """
        arg = arg.strip()
        # Simple identifier: Z, myVar
        if re.match(r'^[a-zA-Z_]\w*$', arg):
            return False
        # Already has swizzle: Z.xyz
        if re.match(r'^[a-zA-Z_]\w*\.[xyzwrgba]+$', arg):
            return False
        # Already wrapped in parens
        if arg.startswith('(') and self._find_balanced_parens(arg, 0) == len(arg) - 1:
            return False
        # Function call: foo(...)
        if re.match(r'^[a-zA-Z_]\w*\s*\(', arg):
            paren_pos = arg.index('(')
            end = self._find_balanced_parens(arg, paren_pos)
            if end == len(arg) - 1:
                return False
        # Everything else needs parens
        return True

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

    def _is_fragcoord_mutated(self, code: str) -> bool:
        """检测 fragCoord 是否被当作可变局部变量使用（被赋值或修改）

        检测模式:
            fragCoord = ...          (直接赋值)
            fragCoord += / -= / *= / /= ...  (复合赋值)
            fragCoord.x = ...        (分量赋值)
            fragCoord.xy += ...      (swizzle 复合赋值)
        """
        lines = code.split('\n')
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith('//'):
                continue
            code_part = line.split('//')[0]
            # 匹配 fragCoord (可选 swizzle) 后跟赋值运算符
            # 但排除 == 和 != 比较
            if re.search(
                r'\bfragCoord\b(?:\.\w+)?\s*(?<![=!<>])(?:\+|-|\*|/|%|&|\||\^|<<|>>)?=(?!=)',
                code_part
            ):
                return True
        return False

    def _handle_fragcoord(self, code: str) -> str:
        """处理 fragCoord / iResolution 的 UV 坐标转换

        Shadertoy 中常见模式:
            vec2 uv = fragCoord / iResolution.xy;
        转换为 UE4:
            float2 uv = UV;  // UE4 Custom Node 的 UV 坐标已经是 0~1 范围

        当 fragCoord 被用作可变局部变量时（被重新赋值），
        注入 float2 fragCoord = (UV * ViewSize); 声明而非全局替换。
        """
        result = code

        # 模式1: uv = fragCoord / iResolution.xy
        # 或: uv = fragCoord.xy / iResolution.xy
        # 注意: iResolution 可能已被 _map_shadertoy_uniforms 替换为 float3(ViewSize.y, ViewSize.x, 1.0)
        uv_pattern = re.compile(
            r'(\w+)\s*=\s*fragCoord(?:\.xy)?\s*/\s*(?:float3\(ViewSize\.y,\s*ViewSize\.x,\s*1\.0\)|float3\(ViewSize,\s*1\.0\)|ViewSize(?:\.yx)?|iResolution)(?:\.xy)?\s*;'
        )
        match = uv_pattern.search(result)
        if match:
            uv_var = match.group(1)
            result = uv_pattern.sub(f'{uv_var} = UV; // fragCoord/iResolution → UV (0~1)', result)
            self.warnings.append('fragCoord/iResolution 模式替换为 UV 输入。请将 UV 作为 Custom Node 的输入参数连接 TextureCoordinate 节点。')

        # 检查是否还有 fragCoord 残留（排除注释）
        has_fragcoord = False
        for line in result.split('\n'):
            stripped = line.lstrip()
            if stripped.startswith('//'):
                continue
            code_part = line.split('//')[0]
            if re.search(r'\bfragCoord\b', code_part):
                has_fragcoord = True
                break

        if not has_fragcoord:
            return result

        # 模式2/3: 直接使用 fragCoord（未除以 iResolution）
        if self._is_fragcoord_mutated(result):
            # 模式2: fragCoord 被当作可变局部变量使用
            # 注入局部变量声明，保留所有 fragCoord 引用不变
            result = 'float2 fragCoord = (UV * ViewSize.yx);\n' + result
            self.warnings.append(
                'fragCoord 被用作可变局部变量，已注入 float2 fragCoord = (UV * ViewSize.yx) 声明。'
                '请确保 UV 和 ViewSize 已作为 Custom Node 的输入。'
            )
        else:
            # 模式3: fragCoord 仅被读取，直接替换
            lines = result.split('\n')
            replaced = False
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith('//'):
                    continue
                code_part = line.split('//')[0]
                if re.search(r'\bfragCoord\b', code_part):
                    lines[i] = re.sub(r'\bfragCoord\b', '(UV * ViewSize.yx)', line)
                    replaced = True
            if replaced:
                result = '\n'.join(lines)
                self.warnings.append('fragCoord 替换为 (UV * ViewSize.yx)。请确保 UV 和 ViewSize 已作为 Custom Node 的输入。')

        return result

    def _handle_fragcolor(self, code: str) -> str:
        """将 fragColor 赋值转换为 return 语句，支持复合赋值。

        Handles:
        - Simple: fragColor = expr;
        - Compound: fragColor += expr;  fragColor *= expr;  fragColor -= expr;
        - fragColor used in expressions on the right side

        Strategy:
        1. First simple assignment `fragColor = expr;` → `float4 fragColor = expr;` (declare + init)
        2. All other assignments/compound ops remain as-is
        3. Last line: if `fragColor = expr;` → `return expr;`; otherwise append `return fragColor;`
        """
        result = code

        # Pattern for simple assignments: fragColor = expr;  (not +=, -=, *=, /=)
        simple_assign_pat = re.compile(r'\bfragColor\s*(?<![+\-*/])=\s*(?!=)([^;]+);')
        # Pattern for ALL fragColor assignments (simple + compound)
        any_assign_pat = re.compile(r'\bfragColor\s*[+\-*/]?=\s*(?!=)([^;]+);')

        simple_assignments = list(simple_assign_pat.finditer(result))
        all_assignments = list(any_assign_pat.finditer(result))

        if not all_assignments:
            # No fragColor assignments at all
            has_return = bool(re.search(r'\breturn\b', result))
            if not has_return:
                self.warnings.append(
                    '未找到 fragColor 赋值或 return 语句。'
                    '请确保 Custom Node 代码最后有 return 语句。'
                )
            return result

        # Determine if we need an explicit declaration at the top.
        # If compound assignments appear before the first simple assignment,
        # fragColor must already exist, so we need a zero-init declaration.
        first_any = all_assignments[0]
        need_top_decl = False
        if not simple_assignments:
            # No simple assignments at all — need declaration
            need_top_decl = True
        elif simple_assignments[0].start() > first_any.start():
            # A compound assignment appears before the first simple assignment
            need_top_decl = True

        # Collect replacements as (start, end, new_text), applied in reverse order
        replacements = []

        # Step 1: Handle the first simple assignment
        if simple_assignments and not need_top_decl:
            first = simple_assignments[0]
            first_expr = first.group(1).strip()
            replacements.append((first.start(), first.end(), f'float4 fragColor = {first_expr};'))

        # Step 2: Handle the last assignment — convert to return if it's simple
        last = all_assignments[-1]
        last_expr = last.group(1).strip()

        last_is_simple = any(
            m.start() == last.start() and m.end() == last.end()
            for m in simple_assignments
        )

        if last_is_simple:
            replacements.append((last.start(), last.end(), f'return {last_expr};'))

        # Deduplicate replacements at the same start position (keep last added)
        seen_starts = {}
        for r in replacements:
            seen_starts[r[0]] = r
        replacements = list(seen_starts.values())
        replacements.sort(key=lambda r: r[0], reverse=True)

        for start, end, new_text in replacements:
            result = result[:start] + new_text + result[end:]

        # Step 3: Insert top declaration if needed
        if need_top_decl:
            result = 'float4 fragColor = float4(0,0,0,0);\n' + result

        # Step 4: If the last assignment was compound, append return fragColor
        if not last_is_simple:
            result = result.rstrip() + '\nreturn fragColor;'

        return result

    # ── 变量零初始化 ──

    def _zero_init_uninitialized_vars(self, code: str) -> str:
        """Zero-initialize uninitialized variable declarations.

        GLSL auto-initializes local variables to 0, but HLSL does not.
        This causes UE4 compile errors like:
            error X4000: variable 'i' used without having been completely initialized

        Transforms:
            float i, s;          → float i = 0, s = 0;
            float3 p;            → float3 p = float3(0,0,0);
            float4 ref;          → float4 ref = float4(0,0,0,0);
            float i, s = 0.6;   → float i = 0, s = 0.6;  (only uninit ones)
        Does NOT touch:
            float x = 1.0;      (already initialized)
            const float h = 0.005; (already initialized)
        """
        # HLSL numeric types and their zero-initializer
        _ZERO_INIT = {
            'float':    '0',
            'float2':   'float2(0,0)',
            'float3':   'float3(0,0,0)',
            'float4':   'float4(0,0,0,0)',
            'half':     '0',
            'half2':    'half2(0,0)',
            'half3':    'half3(0,0,0)',
            'half4':    'half4(0,0,0,0)',
            'int':      '0',
            'int2':     'int2(0,0)',
            'int3':     'int3(0,0,0)',
            'int4':     'int4(0,0,0,0)',
            'uint':     '0',
            'float2x2': 'float2x2(0,0,0,0)',
            'float3x3': 'float3x3(0,0,0,0,0,0,0,0,0)',
            'float4x4': 'float4x4(0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0)',
        }
        type_pattern = '|'.join(re.escape(t) for t in sorted(_ZERO_INIT.keys(), key=len, reverse=True))

        lines = code.split('\n')
        result_lines = []
        inside_struct = False
        struct_brace_depth = 0
        self._struct_saw_brace = False
        for line in lines:
            stripped = line.strip()
            # Track struct bodies — members must not get zero-initialized
            if re.match(r'^struct\s+\w+', stripped):
                inside_struct = True
                struct_brace_depth = 0
            if inside_struct:
                struct_brace_depth += stripped.count('{') - stripped.count('}')
                # Exit struct only when we've seen at least one '{' (depth went >0)
                # and then depth returns to 0 (closing '};')
                if struct_brace_depth <= 0 and self._struct_saw_brace:
                    inside_struct = False
                    self._struct_saw_brace = False
                if '{' in stripped:
                    self._struct_saw_brace = True
                result_lines.append(line)
                continue
            # Skip comments, empty lines, preprocessor
            if stripped.startswith('//') or stripped.startswith('#') or not stripped:
                result_lines.append(line)
                continue

            # Match variable declarations: [const] [static] type var [= expr] [, var2 [= expr2]]... ;
            # We only want to process lines that are simple variable declarations
            # (not inside struct, not function signatures, not for-loop headers)
            m = re.match(
                r'^(\s*)'                                    # leading whitespace
                r'(?:static\s+)?(?:const\s+)?'              # optional qualifiers
                r'(' + type_pattern + r')'                   # type name
                r'\s+'                                       # space
                r'(\w[\w\s,=+\-*/().;]*;)\s*$',             # declarators + semicolon
                line
            )
            if not m:
                result_lines.append(line)
                continue

            indent = m.group(1)
            type_name = m.group(2)
            decl_part = m.group(3)  # e.g. "i, s;" or "i, s = 0.6;" or "x = 1.0;"

            # Check if the line has 'const' — const vars must be initialized, skip
            if re.match(r'\s*(?:static\s+)?const\s+', line):
                result_lines.append(line)
                continue

            # Parse the declarators (split by comma, respecting parentheses)
            # Remove trailing semicolon
            decl_body = decl_part.rstrip(';').strip()
            declarators = self._split_declarators(decl_body)

            zero = _ZERO_INIT.get(type_name, '0')
            new_declarators = []
            any_changed = False
            for decl in declarators:
                decl = decl.strip()
                if '=' in decl:
                    # Already has initializer, keep as-is
                    new_declarators.append(decl)
                else:
                    # No initializer — add zero init
                    var_name = decl.strip()
                    if re.match(r'^[a-zA-Z_]\w*$', var_name):
                        new_declarators.append(f'{var_name} = {zero}')
                        any_changed = True
                    else:
                        new_declarators.append(decl)

            if any_changed:
                # Check if original line had 'static' prefix
                prefix = ''
                if re.match(r'\s*static\s+', line):
                    prefix = 'static '
                new_line = f'{indent}{prefix}{type_name} {", ".join(new_declarators)};'
                result_lines.append(new_line)
            else:
                result_lines.append(line)

        return '\n'.join(result_lines)

    def _split_declarators(self, decl_body: str) -> List[str]:
        """Split comma-separated declarators, respecting parentheses.

        e.g. "i, s = max(a, b), d" → ["i", "s = max(a, b)", "d"]
        """
        parts = []
        depth = 0
        current = []
        for ch in decl_body:
            if ch == '(' or ch == '[':
                depth += 1
                current.append(ch)
            elif ch == ')' or ch == ']':
                depth -= 1
                current.append(ch)
            elif ch == ',' and depth == 0:
                parts.append(''.join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append(''.join(current))
        return parts

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

            # Detect user-defined global variables referenced inside struct functions
            global_var_members = self._detect_global_vars_in_helpers(global_lines, func_bodies)

            parts.append(f'// 1. Struct definition with all helper functions')
            parts.append(f'struct {struct_name}')
            parts.append('{')

            # Add member variables for external inputs (Time, ViewSize, etc.)
            if external_members:
                for var_name, var_type in external_members:
                    parts.append(f'    {var_type} {var_name};')

            # Add member variables for user-defined globals used in struct functions
            if global_var_members:
                for var_name, var_type, _init in global_var_members:
                    parts.append(f'    {var_type} {var_name};')

            if external_members or global_var_members:
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

            # Assign global variable values to struct members
            if global_var_members:
                for var_name, _var_type, _init in global_var_members:
                    parts.append(f'{instance_name}.{var_name} = {var_name};')

            parts.append('')

            # 3. Rewrite main_code: replace direct function calls with F.func()
            # Build a list of function names from the helpers
            func_names = self._extract_func_names(func_bodies)
            main_code = self._rewrite_calls_to_struct(main_code, func_names, instance_name)

            # 3b. After rewriting to F.func(), convert matrix-returning function
            # multiplications to mul() calls. This must happen after struct rewriting
            # because _convert_matrix_multiply (which runs during GLSL→HLSL conversion)
            # only sees the main_code separately from the helper definitions.
            matrix_return_funcs = self._extract_matrix_return_funcs(func_bodies)
            for fname in matrix_return_funcs:
                main_code = self._replace_mul_matrix_func(main_code, fname)

            # 4. Rewrite main_code: replace global variable references with F.<varname>
            # so that main code and struct functions share the same state
            if global_var_members:
                global_var_names = [name for name, _t, _i in global_var_members]
                main_code = self._rewrite_global_vars_to_struct(
                    main_code, global_var_names, instance_name
                )

        # 5. Main logic code
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

    def _detect_global_vars_in_helpers(self, global_lines: List[str],
                                       func_bodies: List[str]) -> List[Tuple[str, str, Optional[str]]]:
        """Detect user-defined global variables that are referenced inside struct functions.

        When helper functions are wrapped in a struct, they lose access to global
        variables declared outside the struct. This method finds those globals so
        they can be added as struct members.

        Args:
            global_lines: List of global code blocks (variable declarations, consts, etc.)
            func_bodies: List of function code blocks that will go inside the struct

        Returns:
            List of (var_name, var_type, init_expr_or_None) tuples for globals
            that are referenced in struct function bodies.
        """
        # Known HLSL types to match in global variable declarations
        hlsl_types = (
            'float', 'float2', 'float3', 'float4',
            'half', 'half2', 'half3', 'half4',
            'int', 'int2', 'int3', 'int4',
            'uint', 'uint2', 'uint3', 'uint4',
            'bool',
            'float2x2', 'float3x3', 'float4x4',
        )
        type_pattern = '|'.join(re.escape(t) for t in sorted(hlsl_types, key=len, reverse=True))

        # Pattern: [static] type varname [= init_expr] ;
        # Capture: type, varname, optional init_expr
        decl_pattern = re.compile(
            r'^\s*(?:static\s+)?'
            r'(' + type_pattern + r')'
            r'\s+(\w+)'
            r'(?:\s*=\s*([^;]+))?'
            r'\s*;',
            re.MULTILINE
        )

        global_text = '\n'.join(global_lines)
        func_text = '\n'.join(func_bodies)

        results = []
        for m in decl_pattern.finditer(global_text):
            var_type = m.group(1)
            var_name = m.group(2)
            init_expr = m.group(3).strip() if m.group(3) else None

            # Skip 'static const' declarations — these are accessible inside struct
            line_start = global_text.rfind('\n', 0, m.start()) + 1
            line_prefix = global_text[line_start:m.start()].strip()
            full_line_prefix = (line_prefix + ' ' + global_text[m.start():m.end()].split(var_type)[0]).strip()
            if 'const' in global_text[line_start:m.start() + len(var_type) + 1]:
                continue

            # Check if this variable is referenced in the struct function bodies
            if re.search(r'\b' + re.escape(var_name) + r'\b', func_text):
                results.append((var_name, var_type, init_expr))

        return results

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

    def _extract_matrix_return_funcs(self, func_bodies: List[str]) -> List[str]:
        """Extract function names that return a matrix type from helper code blocks."""
        matrix_types = {'float2x2', 'float3x3', 'float4x4'}
        names = []
        for mtype in matrix_types:
            func_pattern = re.compile(
                r'\b' + re.escape(mtype) + r'\s+(\w+)\s*\('
            )
            for body in func_bodies:
                for m in func_pattern.finditer(body):
                    fname = m.group(1)
                    if fname not in ('if', 'for', 'while', 'return', 'define',
                                     'float2x2', 'float3x3', 'float4x4'):
                        names.append(fname)
        return list(dict.fromkeys(names))

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

    def _rewrite_global_vars_to_struct(self, code: str, var_names: List[str],
                                        instance_name: str) -> str:
        """Rewrite global variable references in main code to use struct instance.

        e.g. lights → F.lights

        This ensures main code and struct functions share the same variable state.
        Skips occurrences that are part of the variable's own declaration line.
        """
        result = code
        for name in var_names:
            # Replace bare references but not ones already prefixed with F. or
            # that are part of a type declaration (e.g. "float4 lights = ...")
            # Use negative lookbehind for '.' to avoid double-prefixing
            pattern = re.compile(
                r'(?<!\.)\b' + re.escape(name) + r'\b'
            )
            result = pattern.sub(f'{instance_name}.{name}', result)
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
