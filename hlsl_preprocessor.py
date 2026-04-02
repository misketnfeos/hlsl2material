"""
============================================================
 hlsl_preprocessor.py
 HLSL 预处理器：struct 内联展开 + 函数提取 + 复杂代码拆分
============================================================

处理 Custom 节点中复杂的 HLSL 代码，将其转化为
hlsl_parser 能够理解的简化形式。

主要功能：
  1. 提取 struct 中的成员函数定义
  2. 将成员方法调用 (F.Method(args)) 内联展开
  3. 含 for/while/复杂if 的函数 → 保留为 CustomExpression 子片段
  4. 简单函数 → 内联展开到调用点
  5. 处理多层函数嵌套调用

设计思路：
  - 纯文本级别的预处理（正则 + 字符串操作）
  - 不依赖 hlsl_parser 的 AST（因为 parser 不支持 struct）
  - 输出是扁平化的 HLSL 代码，可以被 hlsl_parser 解析

============================================================
"""

import re
from typing import Dict, List, Tuple, Optional, Any, Set


# ═══════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════

class HLSLFunction:
    """表示一个 HLSL 函数（从 struct 或顶层提取）"""
    def __init__(self, name: str, return_type: str, params: List[Tuple[str, str]],
                 body: str, is_complex: bool = False):
        self.name = name                  # 函数名
        self.return_type = return_type    # 返回类型
        self.params = params              # [(type, name), ...]
        self.body = body                  # 函数体原始代码
        self.is_complex = is_complex      # 是否含不可内联的结构 (for/while 等)
        self.dependencies: Set[str] = set()  # 依赖的其他函数名


class PreprocessResult:
    """预处理结果"""
    def __init__(self):
        self.main_code: str = ''              # 可以被 hlsl_parser 解析的主代码
        self.custom_fragments: List[Dict[str, Any]] = []  # 需要保留为 CustomExpression 的片段
        self.warnings: List[str] = []
        self.functions: Dict[str, HLSLFunction] = {}  # 提取出的所有函数


# ═══════════════════════════════════════════════════════════
# 复杂度检测
# ═══════════════════════════════════════════════════════════

# 不可内联到材质节点的 HLSL 结构
_COMPLEX_PATTERNS = [
    re.compile(r'\bfor\s*\('),       # for 循环
    re.compile(r'\bwhile\s*\('),     # while 循环
    re.compile(r'\bdo\s*\{'),        # do-while 循环
    re.compile(r'\bswitch\s*\('),    # switch
]

# if(...) return xxx; 这种提前返回模式 — 也标记为复杂
_EARLY_RETURN_PATTERN = re.compile(r'\bif\s*\(.*?\)\s*return\b')


def _is_complex_body(body: str) -> bool:
    """检测函数体是否包含不可内联的复杂结构
    
    以下情况标记为复杂（不可安全内联，需走 CustomExpression 路径）：
    - 含 for/while/do-while/switch 循环/分支结构
    - 多次 return
    - if(...) return 提前返回模式
    - 超过 3 个局部变量声明（内联时变量重命名容易出错）
    - 含有复合赋值或非声明的再赋值语句（x *= ..., x = ...）
    """
    for pat in _COMPLEX_PATTERNS:
        if pat.search(body):
            return True
    # 多次 return 也算复杂（不是简单的单 return 函数）
    return_count = len(re.findall(r'\breturn\b', body))
    if return_count > 1:
        return True
    # if(...) return 模式
    if _EARLY_RETURN_PATTERN.search(body):
        return True
    # 超过 3 个局部变量声明 → 内联不安全
    # 匹配 type varName = ... 形式的声明语句
    _HLSL_TYPE_PAT = r'(?:float[234]?|half[234]?|int[234]?|uint[234]?|bool|double)\s+\w+'
    var_decls = re.findall(_HLSL_TYPE_PAT, body)
    if len(var_decls) > 3:
        return True
    # 含有复合赋值 (+=, -=, *=, /=) 或非声明的再赋值 (x = ...)
    # 这些在内联时很难正确处理变量作用域
    lines = body.strip().split('\n')
    reassignment_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('//') or stripped.startswith('return'):
            continue
        # 复合赋值
        if re.search(r'\w+\s*[+\-*/]=\s*', stripped):
            reassignment_count += 1
        # 非声明的纯赋值 (不是 type var = ...)
        elif re.match(r'(\w+(?:\.\w+)?)\s*=\s*', stripped):
            # 检查是否是变量声明（前面有类型名）
            if not re.match(r'(?:float[234]?|half[234]?|int[234]?|uint[234]?|bool|double|void)\s', stripped):
                reassignment_count += 1
    if reassignment_count > 1:
        return True
    return False


# ═══════════════════════════════════════════════════════════
# 括号匹配工具
# ═══════════════════════════════════════════════════════════

def _find_matching_brace(code: str, start: int) -> int:
    """从 start 位置的 '{' 开始，找到匹配的 '}'
    
    返回 '}' 的位置索引，如果找不到返回 -1。
    跳过字符串和注释中的大括号。
    """
    if start >= len(code) or code[start] != '{':
        return -1
    
    depth = 0
    i = start
    in_string = False
    string_char = ''
    in_line_comment = False
    in_block_comment = False
    
    while i < len(code):
        ch = code[i]
        next_ch = code[i + 1] if i + 1 < len(code) else ''
        
        # 行注释
        if not in_string and not in_block_comment and ch == '/' and next_ch == '/':
            in_line_comment = True
            i += 2
            continue
        if in_line_comment:
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue
        
        # 块注释
        if not in_string and not in_line_comment and ch == '/' and next_ch == '*':
            in_block_comment = True
            i += 2
            continue
        if in_block_comment:
            if ch == '*' and next_ch == '/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        
        # 字符串
        if not in_string and ch in '"\'':
            in_string = True
            string_char = ch
            i += 1
            continue
        if in_string:
            if ch == '\\':
                i += 2  # 跳过转义
                continue
            if ch == string_char:
                in_string = False
            i += 1
            continue
        
        # 大括号匹配
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i
        
        i += 1
    
    return -1


def _find_matching_paren(code: str, start: int) -> int:
    """从 start 位置的 '(' 开始，找到匹配的 ')'"""
    if start >= len(code) or code[start] != '(':
        return -1
    
    depth = 0
    i = start
    
    while i < len(code):
        ch = code[i]
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        elif ch == '/' and i + 1 < len(code):
            if code[i + 1] == '/':
                # 跳过行注释
                nl = code.find('\n', i)
                i = nl if nl != -1 else len(code)
                continue
            elif code[i + 1] == '*':
                end = code.find('*/', i + 2)
                i = end + 2 if end != -1 else len(code)
                continue
        i += 1
    
    return -1


# ═══════════════════════════════════════════════════════════
# Struct 提取
# ═══════════════════════════════════════════════════════════

# 匹配 struct Name { ... };
_STRUCT_HEADER_RE = re.compile(
    r'\bstruct\s+(\w+)\s*\{',
    re.DOTALL
)

# 匹配函数定义: returnType funcName(params) { body }
_FUNC_DEF_RE = re.compile(
    r'(\w[\w\d]*)\s+'         # 返回类型
    r'(\w[\w\d]*)\s*'         # 函数名
    r'\(([^)]*)\)\s*\{',      # 参数列表
    re.DOTALL
)

# 匹配 StructName varName; 实例化
_INSTANCE_RE_TEMPLATE = r'\b{struct_name}\s+(\w+)\s*;'

# 匹配方法调用 varName.methodName(args)
_METHOD_CALL_RE_TEMPLATE = r'\b{var_name}\.(\w+)\s*\('


def _parse_params(param_str: str) -> List[Tuple[str, str]]:
    """解析函数参数列表字符串 → [(type, name), ...]"""
    params = []
    param_str = param_str.strip()
    if not param_str:
        return params
    
    for part in param_str.split(','):
        part = part.strip()
        if not part:
            continue
        # "float3 p" → ("float3", "p")
        tokens = part.split()
        if len(tokens) >= 2:
            ptype = ' '.join(tokens[:-1])
            pname = tokens[-1]
            params.append((ptype, pname))
        elif len(tokens) == 1:
            params.append(('float', tokens[0]))
    
    return params


def _extract_struct_functions(code: str) -> Tuple[str, Dict[str, HLSLFunction], Dict[str, str], List[str]]:
    """从代码中提取 struct 定义的成员函数
    
    返回:
        (cleaned_code, functions, instance_map, warnings)
        - cleaned_code: 移除了 struct 定义和实例化后的代码
        - functions: {func_name: HLSLFunction}  （使用 StructName_FuncName 格式）
        - instance_map: {var_name: struct_name}  实例变量映射
        - warnings: 警告信息列表
    """
    functions = {}
    instance_map = {}
    warnings = []
    cleaned = code
    
    # 找到所有 struct 定义
    structs_to_remove = []
    # 记录每个 struct 中的短名 → 长名映射，用于后续替换函数体内的互相调用
    struct_short_to_long: Dict[str, Dict[str, str]] = {}  # {struct_name: {short: long}}
    
    for m in _STRUCT_HEADER_RE.finditer(code):
        struct_name = m.group(1)
        brace_start = m.end() - 1  # '{' 的位置
        brace_end = _find_matching_brace(code, brace_start)
        
        if brace_end == -1:
            warnings.append(f'struct {struct_name} 未找到匹配的闭合大括号')
            continue
        
        # struct 体内容
        struct_body = code[brace_start + 1: brace_end]
        
        # 查找 struct 后面的分号（可能有空白）
        after_brace = code[brace_end + 1:].lstrip()
        semicolon_offset = brace_end + 1 + (len(code[brace_end + 1:]) - len(after_brace))
        if semicolon_offset < len(code) and code[semicolon_offset] == ';':
            struct_end = semicolon_offset + 1
        else:
            struct_end = brace_end + 1
        
        structs_to_remove.append((m.start(), struct_end))
        
        # 从 struct body 中提取函数
        short_to_long = {}
        _extract_functions_from_body(struct_body, struct_name, functions, warnings, short_to_long)
        struct_short_to_long[struct_name] = short_to_long
    
    # ── 关键：替换函数体内的互相调用 (短名→长名) ──
    # 例如 DropLayer2 函数体中的 N13(...) → RainFuncs_N13(...)
    for struct_name, name_map in struct_short_to_long.items():
        for full_name, func in functions.items():
            if not full_name.startswith(struct_name + '_'):
                continue
            # 在此函数体中替换所有同 struct 的短名调用
            for short_name, long_name in name_map.items():
                if short_name == full_name.split('_', 1)[1]:
                    continue  # 跳过自身（避免递归问题）
                func.body = re.sub(
                    r'\b' + re.escape(short_name) + r'\s*\(',
                    long_name + '(',
                    func.body
                )
    
    # 从后往前移除 struct 定义（避免索引错位）
    for start, end in reversed(structs_to_remove):
        cleaned = cleaned[:start] + cleaned[end:]
    
    # 从 functions 中收集所有 struct 名
    struct_names = set()
    for fname, func in functions.items():
        parts = fname.split('_', 1)
        if len(parts) == 2:
            struct_names.add(parts[0])
    
    for sname in struct_names:
        inst_re = re.compile(_INSTANCE_RE_TEMPLATE.format(struct_name=sname))
        for m in inst_re.finditer(cleaned):
            var_name = m.group(1)
            instance_map[var_name] = sname
        # 移除实例化语句
        cleaned = inst_re.sub('', cleaned)
    
    return cleaned, functions, instance_map, warnings


def _extract_functions_from_body(body: str, struct_name: str,
                                  functions: Dict[str, HLSLFunction],
                                  warnings: List[str],
                                  short_to_long: Dict[str, str] = None):
    """从 struct body 中提取所有函数定义
    
    short_to_long: 如果提供，记录短函数名 → 完整函数名的映射
    """
    pos = 0
    while pos < len(body):
        m = _FUNC_DEF_RE.search(body, pos)
        if not m:
            break
        
        ret_type = m.group(1)
        func_name = m.group(2)
        params_str = m.group(3)
        
        # 跳过看起来不是函数定义的匹配（比如变量声明）
        # 返回类型必须是 HLSL 类型
        valid_types = {'void', 'float', 'float2', 'float3', 'float4',
                       'half', 'half2', 'half3', 'half4',
                       'int', 'int2', 'int3', 'int4', 'bool', 'uint'}
        if ret_type not in valid_types:
            pos = m.end()
            continue
        
        # 找到函数体
        brace_start = body.index('{', m.start() + len(m.group(0)) - 1)
        brace_end = _find_matching_brace(body, brace_start)
        
        if brace_end == -1:
            warnings.append(f'函数 {struct_name}.{func_name} 未找到匹配的闭合大括号')
            pos = m.end()
            continue
        
        func_body = body[brace_start + 1: brace_end].strip()
        params = _parse_params(params_str)
        is_complex = _is_complex_body(func_body)
        
        full_name = f'{struct_name}_{func_name}'
        func = HLSLFunction(
            name=full_name,
            return_type=ret_type,
            params=params,
            body=func_body,
            is_complex=is_complex,
        )
        functions[full_name] = func
        
        # 记录短名→长名映射
        if short_to_long is not None:
            short_to_long[func_name] = full_name
        
        if is_complex:
            warnings.append(
                f'函数 {struct_name}.{func_name}() 含有复杂结构(循环/多return)，'
                f'将保留为 CustomExpression'
            )
        
        pos = brace_end + 1


# ═══════════════════════════════════════════════════════════
# 也提取顶层自定义函数（不在 struct 中的）
# ═══════════════════════════════════════════════════════════

_TOPLEVEL_FUNC_RE = re.compile(
    r'^(\w[\w\d]*)\s+'         # 返回类型 (行首)
    r'(\w[\w\d]*)\s*'          # 函数名
    r'\(([^)]*)\)\s*\{',       # 参数列表
    re.MULTILINE
)

# 需要排除的关键字（不是函数定义）
_NOT_FUNC_KEYWORDS = {'if', 'for', 'while', 'do', 'switch', 'return', 'else', 'struct', 'class'}

# HLSL 类型关键字
_HLSL_TYPES = {'void', 'float', 'float2', 'float3', 'float4',
               'half', 'half2', 'half3', 'half4',
               'int', 'int2', 'int3', 'int4', 'bool', 'uint',
               'uint2', 'uint3', 'uint4', 'double'}


def _extract_toplevel_functions(code: str) -> Tuple[str, Dict[str, HLSLFunction], List[str]]:
    """提取顶层函数定义（不在 struct 中的）
    
    返回:
        (cleaned_code, functions, warnings)
    """
    functions = {}
    warnings = []
    regions_to_remove = []
    
    pos = 0
    while pos < len(code):
        m = _FUNC_DEF_RE.search(code, pos)
        if not m:
            break
        
        ret_type = m.group(1)
        func_name = m.group(2)
        params_str = m.group(3)
        
        # 检查返回类型是否合法
        if ret_type not in _HLSL_TYPES or func_name in _NOT_FUNC_KEYWORDS:
            pos = m.end()
            continue
        
        # 检查函数名不是 HLSL 类型（避免误匹配构造函数调用）
        if func_name in _HLSL_TYPES:
            pos = m.end()
            continue
        
        # 找到函数体
        brace_pos = m.end() - 1
        brace_end = _find_matching_brace(code, brace_pos)
        
        if brace_end == -1:
            pos = m.end()
            continue
        
        func_body = code[brace_pos + 1: brace_end].strip()
        params = _parse_params(params_str)
        is_complex = _is_complex_body(func_body)
        
        func = HLSLFunction(
            name=func_name,
            return_type=ret_type,
            params=params,
            body=func_body,
            is_complex=is_complex,
        )
        functions[func_name] = func
        regions_to_remove.append((m.start(), brace_end + 1))
        
        pos = brace_end + 1
    
    # 从后往前移除函数定义
    cleaned = code
    for start, end in reversed(regions_to_remove):
        cleaned = cleaned[:start] + cleaned[end:]
    
    return cleaned, functions, warnings


# ═══════════════════════════════════════════════════════════
# 方法调用替换 (F.Method → 函数调用)
# ═══════════════════════════════════════════════════════════

def _replace_method_calls(code: str, instance_map: Dict[str, str],
                           functions: Dict[str, HLSLFunction]) -> str:
    """将 obj.method(args) 调用替换为扁平函数调用 StructName_method(args)
    
    例如: F.Drops(uv, t, ...) → RainFuncs_Drops(uv, t, ...)
    """
    for var_name, struct_name in instance_map.items():
        # 构造正则：var_name.methodName(
        pattern = re.compile(
            r'\b' + re.escape(var_name) + r'\.(\w+)\s*\(',
        )
        
        while True:
            m = pattern.search(code)
            if not m:
                break
            
            method_name = m.group(1)
            full_name = f'{struct_name}_{method_name}'
            
            # 替换 "F.Method(" → "RainFuncs_Method("
            replacement = f'{full_name}('
            code = code[:m.start()] + replacement + code[m.end():]
    
    return code


# ═══════════════════════════════════════════════════════════
# 函数内联展开
# ═══════════════════════════════════════════════════════════

def _find_call_in_code(code: str, func_name: str) -> Optional[Tuple[int, int, str]]:
    """在代码中查找函数调用，返回 (start, end, args_string)
    
    start: 函数调用起始位置（函数名开头）
    end: 右括号后的位置
    args_string: 括号内的参数字符串
    """
    pattern = re.compile(r'\b' + re.escape(func_name) + r'\s*\(')
    m = pattern.search(code)
    if not m:
        return None
    
    paren_start = code.index('(', m.start())
    paren_end = _find_matching_paren(code, paren_start)
    if paren_end == -1:
        return None
    
    args_str = code[paren_start + 1: paren_end]
    return (m.start(), paren_end + 1, args_str)


def _split_args(args_str: str) -> List[str]:
    """拆分函数参数，正确处理嵌套括号和逗号"""
    args = []
    depth = 0
    current = []
    
    for ch in args_str:
        if ch == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
        else:
            if ch in '([{':
                depth += 1
            elif ch in ')]}':
                depth -= 1
            current.append(ch)
    
    remainder = ''.join(current).strip()
    if remainder:
        args.append(remainder)
    
    return args


def _inline_simple_function(code: str, func: HLSLFunction, call_start: int,
                             call_end: int, args_str: str,
                             inline_counter: Dict[str, int]) -> str:
    """内联展开一个简单函数调用
    
    将 funcName(arg1, arg2) 替换为内联后的代码块。
    
    策略：
    - 为每次内联生成唯一的临时变量前缀（避免命名冲突）
    - 将参数赋值给临时变量
    - 将函数体中的 return xxx; 替换为最终表达式
    - 将结果包装在一个临时变量赋值中
    """
    # 生成唯一前缀
    counter = inline_counter.get(func.name, 0)
    inline_counter[func.name] = counter + 1
    prefix = f'_il_{func.name}_{counter}_'
    
    args = _split_args(args_str)
    body = func.body
    
    # 参数替换：将形参名替换为实参表达式
    # 注意要用 word boundary 避免误替换
    # 关键：按参数名长度降序排序，先替换长的避免短名误替换
    # 同时排除 HLSL 类型关键字，防止 "t" 替换了 "float" 中的 "t"
    _HLSL_KEYWORDS = {'float', 'float2', 'float3', 'float4', 'half', 'half2', 'half3', 'half4',
                      'int', 'int2', 'int3', 'int4', 'uint', 'uint2', 'uint3', 'uint4',
                      'bool', 'void', 'double', 'return', 'if', 'else', 'for', 'while',
                      'do', 'switch', 'case', 'break', 'continue', 'struct', 'true', 'false',
                      'sin', 'cos', 'tan', 'abs', 'frac', 'floor', 'ceil', 'saturate',
                      'length', 'normalize', 'dot', 'cross', 'pow', 'sqrt', 'min', 'max',
                      'clamp', 'lerp', 'step', 'smoothstep', 'exp', 'exp2', 'log', 'log2',
                      'sign', 'fmod', 'atan2', 'reflect', 'refract'}
    
    # 排序：参数名长度降序（先替换长的）
    sorted_params = sorted(enumerate(func.params), key=lambda x: -len(x[1][1]))
    for i, (ptype, pname) in sorted_params:
        if i < len(args) and pname not in _HLSL_KEYWORDS and len(pname) > 1:
            arg_expr = args[i].strip()
            # 使用 word boundary 替换，但额外确认不是类型名的一部分
            body = re.sub(r'\b' + re.escape(pname) + r'\b', arg_expr, body)
        elif i < len(args) and (pname in _HLSL_KEYWORDS or len(pname) <= 1):
            # 短名或关键字冲突：用更安全的替换策略
            # 只在 "独立位置" 替换（前后不是字母数字下划线，且不在类型名内部）
            arg_expr = args[i].strip()
            # 使用 word boundary 但排除前面紧跟着字母的情况（如 "float" 中的 "t"）
            body = re.sub(r'(?<![a-zA-Z\d_])' + re.escape(pname) + r'(?![a-zA-Z\d_])', arg_expr, body)
    
    # 提取 return 表达式
    # 简单函数应该只有一个 return
    return_match = re.search(r'\breturn\s+(.+?)\s*;', body, re.DOTALL)
    
    if return_match:
        # 有 return 语句
        return_expr = return_match.group(1).strip()
        
        # 收集 return 之前的所有语句（临时变量声明等）
        before_return = body[:return_match.start()].strip()
        
        if before_return:
            # 有临时变量 → 需要将其重命名并展开为语句块
            # 第一遍：收集所有局部变量名
            lines = before_return.split('\n')
            local_vars = []
            
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped or line_stripped.startswith('//'):
                    continue
                # 检测变量声明: type varName = ...;  或  type varName;
                var_decl = re.match(r'(?:float[234]?|half[234]?|int[234]?|uint[234]?|bool|double)\s+(\w[\w\d]*)', line_stripped)
                if var_decl:
                    old_var = var_decl.group(1)
                    new_var = prefix + old_var
                    local_vars.append((old_var, new_var))
            
            # 第二遍：在所有行中统一替换所有局部变量（按名称长度降序）
            local_vars_sorted = sorted(local_vars, key=lambda x: -len(x[0]))
            renamed_lines = []
            for line in lines:
                line = line.strip()
                if not line or line.startswith('//'):
                    if line:
                        renamed_lines.append(line)
                    continue
                for old_var, new_var in local_vars_sorted:
                    line = re.sub(r'\b' + re.escape(old_var) + r'\b', new_var, line)
                renamed_lines.append(line)
            
            # 在 return_expr 中也替换局部变量名
            for old_var, new_var in local_vars_sorted:
                return_expr = re.sub(r'\b' + re.escape(old_var) + r'\b', new_var, return_expr)
            
            # 构造内联代码块
            result_var = prefix + 'result'
            
            # 在调用位置之前插入展开的语句
            pre_code = '\n'.join(renamed_lines)
            pre_code += f'\n{func.return_type} {result_var} = {return_expr};'
            
            # 找到包含此调用的语句的开头
            stmt_start = code.rfind('\n', 0, call_start)
            if stmt_start == -1:
                stmt_start = 0
            else:
                stmt_start += 1
            
            # 替换：在语句前插入展开代码，调用位置替换为结果变量名
            new_code = (
                code[:stmt_start] +
                pre_code + '\n' +
                code[stmt_start:call_start] +
                result_var +
                code[call_end:]
            )
            return new_code
        else:
            # 没有临时变量，直接将调用替换为 return 表达式
            # 用括号包裹以保证优先级
            replacement = f'({return_expr})'
            return code[:call_start] + replacement + code[call_end:]
    else:
        # 没有 return 语句（void 函数或者整个body就是一个表达式）
        # 尝试把整个 body 作为表达式
        body_stripped = body.strip().rstrip(';').strip()
        if body_stripped:
            return code[:call_start] + f'({body_stripped})' + code[call_end:]
        else:
            return code[:call_start] + '0' + code[call_end:]


# ═══════════════════════════════════════════════════════════
# 复杂函数 → CustomExpression 节点
# ═══════════════════════════════════════════════════════════

def _build_custom_expression_for_func(func: HLSLFunction,
                                       all_functions: Dict[str, HLSLFunction]) -> Dict[str, Any]:
    """为复杂函数构建 CustomExpression 描述
    
    生成一个自包含的 HLSL 代码片段，包含该函数依赖的所有其他函数定义，
    以及该函数自身的代码。
    
    返回:
        {
            'name': 函数名,
            'code': 完整的 HLSL 代码（含依赖函数定义）,
            'inputs': [(type, name), ...],  # 输入参数
            'return_type': 返回类型,
        }
    """
    # 收集依赖链
    deps = _collect_dependencies(func.name, all_functions)
    
    # 构建代码：先是依赖函数的定义，最后是主函数体
    code_parts = []
    
    for dep_name in deps:
        dep_func = all_functions.get(dep_name)
        if dep_func:
            params_str = ', '.join(f'{t} {n}' for t, n in dep_func.params)
            code_parts.append(
                f'{dep_func.return_type} {dep_name}({params_str}) {{\n'
                f'    {dep_func.body}\n'
                f'}}\n'
            )
    
    # 主函数的代码直接使用函数体（入参通过 CustomExpression 的 Input pin 传入）
    # 将参数名替换回原始名称（因为 CustomExpression pin 名就是参数名）
    main_body = func.body
    
    # 完整代码 = 依赖函数定义 + 主函数体（包裹在 return 中）
    deps_code = '\n'.join(code_parts)
    
    # 检查主函数体是否有 return
    if not re.search(r'\breturn\b', main_body):
        main_body = f'return {main_body.strip().rstrip(";")};'
    
    full_code = deps_code + '\n' + main_body if deps_code else main_body
    
    return {
        'name': func.name,
        'code': full_code.strip(),
        'inputs': func.params,
        'return_type': func.return_type,
    }


def _collect_dependencies(func_name: str, all_functions: Dict[str, HLSLFunction],
                           visited: Set[str] = None) -> List[str]:
    """递归收集函数的依赖链（拓扑排序）"""
    if visited is None:
        visited = set()
    
    if func_name in visited:
        return []
    visited.add(func_name)
    
    func = all_functions.get(func_name)
    if not func:
        return []
    
    result = []
    
    # 查找函数体中调用的其他自定义函数
    for other_name in all_functions:
        if other_name == func_name:
            continue
        if re.search(r'\b' + re.escape(other_name) + r'\s*\(', func.body):
            func.dependencies.add(other_name)
            # 递归收集依赖
            sub_deps = _collect_dependencies(other_name, all_functions, visited)
            for d in sub_deps:
                if d not in result:
                    result.append(d)
            if other_name not in result:
                result.append(other_name)
    
    return result


def _replace_complex_call_with_custom(
    code: str, func_name: str, func: HLSLFunction,
    all_functions: Dict[str, HLSLFunction],
    custom_fragments: List[Dict[str, Any]],
    warnings: List[str],
    replaced_funcs: Set[str],
) -> str:
    """将复杂函数调用替换为 CustomExpression 节点调用标记
    
    使用特殊标记 __CUSTOM_N__(args) 来标记这里是一个 CustomExpression 调用。
    后续 node_mapper 遇到 __CUSTOM_N__ 时会创建对应的 CustomExpression 节点。
    """
    if func_name not in replaced_funcs:
        # 第一次遇到，构建 CustomExpression 描述
        custom_info = _build_custom_expression_for_func(func, all_functions)
        custom_fragments.append(custom_info)
        replaced_funcs.add(func_name)
    
    # 找到 custom_fragments 中此函数的索引
    custom_idx = None
    for i, cf in enumerate(custom_fragments):
        if cf['name'] == func_name:
            custom_idx = i
            break
    
    if custom_idx is None:
        return code
    
    # 将所有 func_name(args) 调用替换为 __CUSTOM_{idx}__(args)
    # 这样 hlsl_parser 会将其视为一个普通函数调用
    marker_name = f'__CUSTOM_{custom_idx}__'
    pattern = re.compile(r'\b' + re.escape(func_name) + r'\s*\(')
    
    while True:
        m = pattern.search(code)
        if not m:
            break
        # 替换函数名部分
        paren_pos = code.index('(', m.start())
        code = code[:m.start()] + marker_name + code[paren_pos:]
    
    return code


# ═══════════════════════════════════════════════════════════
# 分析函数复杂度（含依赖传递）
# ═══════════════════════════════════════════════════════════

def _propagate_complexity(functions: Dict[str, HLSLFunction]):
    """传播复杂度标记：如果函数A调用了复杂函数B，则A也标记为复杂"""
    changed = True
    while changed:
        changed = False
        for fname, func in functions.items():
            if func.is_complex:
                continue
            # 检查是否调用了任何复杂函数
            for other_name, other_func in functions.items():
                if other_name == fname:
                    continue
                if not other_func.is_complex:
                    continue
                if re.search(r'\b' + re.escape(other_name) + r'\s*\(', func.body):
                    func.is_complex = True
                    changed = True
                    break


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def preprocess_hlsl(code: str, input_names: List[str] = None) -> PreprocessResult:
    """预处理复杂 HLSL 代码
    
    完整流程：
    1. 清理换行符
    2. 提取 struct 成员函数
    3. 提取顶层自定义函数
    4. 替换方法调用 (F.Method → StructName_Method)
    5. 分析函数复杂度（含依赖传递）
    6. 复杂函数 → CustomExpression
    7. 简单函数 → 内联展开
    8. 处理 return 语句
    
    参数:
        code: 原始 HLSL 代码
        input_names: Custom 节点的输入 pin 名称列表
    返回:
        PreprocessResult
    """
    result = PreprocessResult()
    
    if input_names is None:
        input_names = []
    
    # 1. 清理
    code = code.replace('\r\n', '\n').replace('\r', '\n')
    
    # 快速判断：如果没有 struct 且没有自定义函数，直接返回
    has_struct = 'struct ' in code
    # 检查是否有自定义函数定义（简单启发式）
    has_func_def = bool(_FUNC_DEF_RE.search(code)) and has_struct
    
    if not has_struct and not has_func_def:
        # 简单 HLSL，直接走原来的预处理逻辑
        result.main_code = _simple_preprocess(code, input_names)
        return result
    
    # 2. 提取 struct 成员函数
    code, struct_funcs, instance_map, warnings = _extract_struct_functions(code)
    result.warnings.extend(warnings)
    result.functions.update(struct_funcs)
    
    # 3. 替换方法调用 → 扁平函数调用
    code = _replace_method_calls(code, instance_map, struct_funcs)
    
    # 4. 提取顶层自定义函数
    code, toplevel_funcs, tl_warnings = _extract_toplevel_functions(code)
    result.warnings.extend(tl_warnings)
    result.functions.update(toplevel_funcs)
    
    all_functions = dict(result.functions)
    
    # 5. 分析复杂度传播
    _propagate_complexity(all_functions)
    
    # 6. 复杂函数 → CustomExpression 标记
    replaced_funcs: Set[str] = set()
    
    for fname, func in all_functions.items():
        if func.is_complex:
            code = _replace_complex_call_with_custom(
                code, fname, func, all_functions,
                result.custom_fragments, result.warnings, replaced_funcs
            )
    
    # 7. 简单函数 → 内联展开（迭代直到没有更多调用）
    inline_counter: Dict[str, int] = {}
    max_iterations = 50  # 防止无限循环
    
    for iteration in range(max_iterations):
        found_call = False
        for fname, func in all_functions.items():
            if func.is_complex:
                continue
            
            call_info = _find_call_in_code(code, fname)
            if call_info:
                call_start, call_end, args_str = call_info
                code = _inline_simple_function(
                    code, func, call_start, call_end, args_str, inline_counter
                )
                found_call = True
                break  # 每次替换一个后重新搜索（索引可能变化）
        
        if not found_call:
            break
    
    # 8. 最终清理
    code = _final_cleanup(code, input_names)
    
    result.main_code = code
    return result


def _simple_preprocess(code: str, input_names: List[str]) -> str:
    """简单的预处理（无 struct/函数定义的情况）
    
    与原 _preprocess_custom_hlsl 逻辑一致。
    """
    lines = code.strip().split('\n')
    processed_lines = []
    has_return = False
    
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('return '):
            has_return = True
        processed_lines.append(line)
    
    if not has_return and processed_lines:
        last = processed_lines[-1].strip()
        if last and not last.endswith(';'):
            processed_lines[-1] = f'return {last};'
        elif last.endswith(';') and '=' not in last and not last.startswith('//'):
            processed_lines[-1] = f'return {last[:-1]};'
    
    return '\n'.join(processed_lines)


def _final_cleanup(code: str, input_names: List[str]) -> str:
    """最终代码清理"""
    # 移除多余的空行
    lines = code.split('\n')
    cleaned = []
    prev_empty = False
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if prev_empty:
                continue
            prev_empty = True
        else:
            prev_empty = False
        # 跳过注释行（保留代码行中的行内注释）
        if stripped.startswith('//'):
            # 保留注释，但不影响解析
            cleaned.append(line)
            continue
        cleaned.append(line)
    
    code = '\n'.join(cleaned).strip()
    
    # 确保有 return 语句
    if not re.search(r'\breturn\b', code):
        lines = code.split('\n')
        if lines:
            last = lines[-1].strip()
            if last and not last.endswith(';'):
                lines[-1] = f'return {last};'
            elif last.endswith(';') and '=' not in last and not last.startswith('//'):
                lines[-1] = f'return {last[:-1]};'
        code = '\n'.join(lines)
    
    return code
