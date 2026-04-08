"""
============================================================
 hlsl_parser.py
 HLSL 词法分析 & 语法解析 → AST（抽象语法树）
============================================================

支持的 HLSL 子集（Custom HLSL 常用）：
  - 类型：float, float2, float3, float4, half, half3, half4, int, bool
  - 运算符：+ - * / % > < >= <= == != && || ! ?:
  - 内置函数：lerp, saturate, clamp, dot, cross, normalize, pow,
              sin, cos, tan, asin, acos, atan, atan2,
              abs, sign, floor, ceil, round, frac, fmod,
              sqrt, rsqrt, exp, exp2, log, log2,
              min, max, step, smoothstep, length, distance,
              reflect, refract, ddx, ddy, tex2D, mul
  - 变量声明与赋值
  - Swizzle（.xyz, .rg, .w 等）
  - 构造函数（float3(...), float4(...)）
  - return 语句
  - 三元运算符 a ? b : c

不支持（保留为 CustomExpression）：
  - for / while / do 循环
  - 自定义函数定义
  - struct 定义
  - #define / #include 预处理器
  - switch / case
============================================================
"""

import re
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Any, Union


# ═══════════════════════════════════════════════════════════
# Token 定义
# ═══════════════════════════════════════════════════════════

class TokenType(Enum):
    # 字面量
    NUMBER = auto()
    IDENTIFIER = auto()

    # 类型关键字
    FLOAT = auto()
    FLOAT2 = auto()
    FLOAT3 = auto()
    FLOAT4 = auto()
    HALF = auto()
    HALF2 = auto()
    HALF3 = auto()
    HALF4 = auto()
    INT = auto()
    INT2 = auto()
    INT3 = auto()
    INT4 = auto()
    BOOL = auto()

    # 控制关键字
    RETURN = auto()
    IF = auto()
    ELSE = auto()
    FOR = auto()
    WHILE = auto()

    # 运算符
    PLUS = auto()       # +
    MINUS = auto()      # -
    STAR = auto()       # *
    SLASH = auto()       # /
    PERCENT = auto()    # %
    EQUALS = auto()     # =
    EQEQ = auto()      # ==
    NEQ = auto()        # !=
    LT = auto()         # <
    GT = auto()         # >
    LTE = auto()        # <=
    GTE = auto()        # >=
    AND = auto()        # &&
    OR = auto()         # ||
    NOT = auto()        # !
    QUESTION = auto()   # ?
    COLON = auto()      # :

    # 分隔符
    LPAREN = auto()     # (
    RPAREN = auto()     # )
    LBRACE = auto()     # {
    RBRACE = auto()     # }
    LBRACKET = auto()   # [
    RBRACKET = auto()   # ]
    COMMA = auto()      # ,
    SEMICOLON = auto()  # ;
    DOT = auto()        # .

    # 复合赋值
    PLUS_EQ = auto()    # +=
    MINUS_EQ = auto()   # -=
    STAR_EQ = auto()    # *=
    SLASH_EQ = auto()   # /=

    # 特殊
    EOF = auto()


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int


# 类型关键字映射
TYPE_KEYWORDS = {
    'float':  TokenType.FLOAT,
    'float2': TokenType.FLOAT2,
    'float3': TokenType.FLOAT3,
    'float4': TokenType.FLOAT4,
    'half':   TokenType.HALF,
    'half2':  TokenType.HALF2,
    'half3':  TokenType.HALF3,
    'half4':  TokenType.HALF4,
    'int':    TokenType.INT,
    'int2':   TokenType.INT2,
    'int3':   TokenType.INT3,
    'int4':   TokenType.INT4,
    'bool':   TokenType.BOOL,
}

CONTROL_KEYWORDS = {
    'return': TokenType.RETURN,
    'if':     TokenType.IF,
    'else':   TokenType.ELSE,
    'for':    TokenType.FOR,
    'while':  TokenType.WHILE,
    'struct': TokenType.IDENTIFIER,  # struct 作为特殊标识符处理
}

ALL_KEYWORDS = {**TYPE_KEYWORDS, **CONTROL_KEYWORDS}

# 所有类型 Token
TYPE_TOKENS = set(TYPE_KEYWORDS.values())


# ═══════════════════════════════════════════════════════════
# 词法分析器 (Lexer)
# ═══════════════════════════════════════════════════════════

class Lexer:
    """将 HLSL 源代码分割成 Token 序列"""

    def __init__(self, source: str):
        self.source = source
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []

    def error(self, msg: str):
        raise SyntaxError(f"词法错误 (行 {self.line}, 列 {self.col}): {msg}")

    def peek(self) -> str:
        if self.pos >= len(self.source):
            return '\0'
        return self.source[self.pos]

    def peek_next(self) -> str:
        if self.pos + 1 >= len(self.source):
            return '\0'
        return self.source[self.pos + 1]

    def advance(self) -> str:
        ch = self.source[self.pos]
        self.pos += 1
        if ch == '\n':
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def skip_whitespace(self):
        while self.pos < len(self.source) and self.source[self.pos] in ' \t\r\n':
            self.advance()

    def skip_line_comment(self):
        """跳过 // 注释"""
        while self.pos < len(self.source) and self.source[self.pos] != '\n':
            self.advance()

    def skip_block_comment(self):
        """跳过 /* */ 注释"""
        while self.pos < len(self.source) - 1:
            if self.source[self.pos] == '*' and self.source[self.pos + 1] == '/':
                self.advance()  # *
                self.advance()  # /
                return
            self.advance()
        self.error("未闭合的块注释 /*")

    def read_number(self) -> Token:
        start_line, start_col = self.line, self.col
        result = ''
        has_dot = False
        has_exp = False

        while self.pos < len(self.source):
            ch = self.source[self.pos]
            if ch.isdigit():
                result += self.advance()
            elif ch == '.' and not has_dot and not has_exp:
                has_dot = True
                result += self.advance()
            elif ch in 'eE' and not has_exp:
                has_exp = True
                result += self.advance()
                if self.pos < len(self.source) and self.source[self.pos] in '+-':
                    result += self.advance()
            elif ch in 'fFhH':
                # 后缀 f/h，跳过
                self.advance()
                break
            else:
                break

        return Token(TokenType.NUMBER, result, start_line, start_col)

    def read_identifier(self) -> Token:
        start_line, start_col = self.line, self.col
        result = ''
        while self.pos < len(self.source) and (self.source[self.pos].isalnum() or self.source[self.pos] == '_'):
            result += self.advance()

        # 检查是否是关键字
        if result in ALL_KEYWORDS:
            return Token(ALL_KEYWORDS[result], result, start_line, start_col)
        return Token(TokenType.IDENTIFIER, result, start_line, start_col)

    def tokenize(self) -> List[Token]:
        """执行词法分析，返回 Token 列表"""
        while self.pos < len(self.source):
            self.skip_whitespace()
            if self.pos >= len(self.source):
                break

            ch = self.peek()
            start_line, start_col = self.line, self.col

            # 注释
            if ch == '/' and self.peek_next() == '/':
                self.skip_line_comment()
                continue
            if ch == '/' and self.peek_next() == '*':
                self.advance()
                self.advance()
                self.skip_block_comment()
                continue

            # 数字
            if ch.isdigit() or (ch == '.' and self.peek_next().isdigit()):
                self.tokens.append(self.read_number())
                continue

            # 标识符/关键字
            if ch.isalpha() or ch == '_':
                self.tokens.append(self.read_identifier())
                continue

            # 双字符运算符
            two_char = ch + self.peek_next() if self.pos + 1 < len(self.source) else ''
            if two_char == '==':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.EQEQ, '==', start_line, start_col))
                continue
            if two_char == '!=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.NEQ, '!=', start_line, start_col))
                continue
            if two_char == '<=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.LTE, '<=', start_line, start_col))
                continue
            if two_char == '>=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.GTE, '>=', start_line, start_col))
                continue
            if two_char == '&&':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.AND, '&&', start_line, start_col))
                continue
            if two_char == '||':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.OR, '||', start_line, start_col))
                continue
            if two_char == '+=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.PLUS_EQ, '+=', start_line, start_col))
                continue
            if two_char == '-=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.MINUS_EQ, '-=', start_line, start_col))
                continue
            if two_char == '*=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.STAR_EQ, '*=', start_line, start_col))
                continue
            if two_char == '/=':
                self.advance(); self.advance()
                self.tokens.append(Token(TokenType.SLASH_EQ, '/=', start_line, start_col))
                continue

            # 单字符运算符/分隔符
            single_map = {
                '+': TokenType.PLUS, '-': TokenType.MINUS,
                '*': TokenType.STAR, '/': TokenType.SLASH,
                '%': TokenType.PERCENT,
                '=': TokenType.EQUALS,
                '<': TokenType.LT, '>': TokenType.GT,
                '!': TokenType.NOT,
                '?': TokenType.QUESTION, ':': TokenType.COLON,
                '(': TokenType.LPAREN, ')': TokenType.RPAREN,
                '{': TokenType.LBRACE, '}': TokenType.RBRACE,
                '[': TokenType.LBRACKET, ']': TokenType.RBRACKET,
                ',': TokenType.COMMA, ';': TokenType.SEMICOLON,
                '.': TokenType.DOT,
            }

            if ch in single_map:
                self.advance()
                self.tokens.append(Token(single_map[ch], ch, start_line, start_col))
                continue

            self.error(f"未识别的字符 '{ch}'")

        self.tokens.append(Token(TokenType.EOF, '', self.line, self.col))
        return self.tokens


# ═══════════════════════════════════════════════════════════
# AST 节点定义
# ═══════════════════════════════════════════════════════════

@dataclass
class ASTNode:
    """AST 基节点"""
    line: int = 0
    col: int = 0


# ── 表达式节点 ──

@dataclass
class NumberLiteral(ASTNode):
    """数字字面量: 1.0, 0.5, 255"""
    value: float = 0.0


@dataclass
class Identifier(ASTNode):
    """标识符: uv, color, time"""
    name: str = ''


@dataclass
class BinaryOp(ASTNode):
    """二元运算: a + b, x * y"""
    op: str = ''
    left: ASTNode = None
    right: ASTNode = None


@dataclass
class UnaryOp(ASTNode):
    """一元运算: -x, !flag"""
    op: str = ''
    operand: ASTNode = None


@dataclass
class FunctionCall(ASTNode):
    """函数调用: lerp(a, b, t), dot(n, l)"""
    name: str = ''
    args: List[ASTNode] = field(default_factory=list)


@dataclass
class TypeConstructor(ASTNode):
    """类型构造: float3(1, 0, 0), float4(color, 1.0)"""
    type_name: str = ''
    args: List[ASTNode] = field(default_factory=list)


@dataclass
class SwizzleAccess(ASTNode):
    """Swizzle 访问: color.rgb, uv.x, pos.xyzw"""
    object: ASTNode = None
    components: str = ''


@dataclass
class ArrayAccess(ASTNode):
    """数组访问: arr[i]"""
    object: ASTNode = None
    index: ASTNode = None


@dataclass
class TernaryOp(ASTNode):
    """三元运算: cond ? a : b"""
    condition: ASTNode = None
    true_expr: ASTNode = None
    false_expr: ASTNode = None


@dataclass
class Assignment(ASTNode):
    """赋值: x = expr 或 x += expr"""
    target: ASTNode = None
    op: str = '='          # =, +=, -=, *=, /=
    value: ASTNode = None


# ── 语句节点 ──

@dataclass
class VarDeclaration(ASTNode):
    """变量声明: float3 color = expr;"""
    type_name: str = ''
    var_name: str = ''
    initializer: Optional[ASTNode] = None


@dataclass
class ReturnStatement(ASTNode):
    """return expr;"""
    value: ASTNode = None


@dataclass
class ExpressionStatement(ASTNode):
    """表达式语句（赋值等）: x = expr;"""
    expression: ASTNode = None


@dataclass
class IfStatement(ASTNode):
    """if 语句（标记为不可完全转换，保留为参考）"""
    condition: ASTNode = None
    then_body: List[ASTNode] = field(default_factory=list)
    else_body: List[ASTNode] = field(default_factory=list)


@dataclass
class ForLoop(ASTNode):
    """for 循环（不可转换，标记为 CustomExpression）"""
    raw_code: str = ''


@dataclass
class Block(ASTNode):
    """语句块"""
    statements: List[ASTNode] = field(default_factory=list)


@dataclass
class HLSLProgram(ASTNode):
    """顶层程序: 一系列语句"""
    statements: List[ASTNode] = field(default_factory=list)
    # 从代码中识别出的输入参数
    inputs: List[str] = field(default_factory=list)
    # 原始源码（用于不可转换部分的回退）
    raw_source: str = ''


# ═══════════════════════════════════════════════════════════
# 语法解析器 (Parser)
# ═══════════════════════════════════════════════════════════

class Parser:
    """
    递归下降解析器
    将 Token 序列解析为 AST
    """

    def __init__(self, tokens: List[Token], source: str = ''):
        self.tokens = tokens
        self.pos = 0
        self.source = source
        # 记录遇到的不可转换结构
        self.warnings: List[str] = []

    def error(self, msg: str):
        tok = self.current()
        raise SyntaxError(f"解析错误 (行 {tok.line}, 列 {tok.col}): {msg}\n  当前 token: {tok.type.name} = '{tok.value}'")

    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset=0) -> Token:
        idx = self.pos + offset
        if idx >= len(self.tokens):
            return self.tokens[-1]  # EOF
        return self.tokens[idx]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return tok

    def expect(self, token_type: TokenType) -> Token:
        tok = self.current()
        if tok.type != token_type:
            self.error(f"期望 {token_type.name}，得到 {tok.type.name} ('{tok.value}')")
        return self.advance()

    def match(self, *token_types: TokenType) -> bool:
        return self.current().type in token_types

    def match_value(self, value: str) -> bool:
        return self.current().value == value

    # ── 顶层解析 ──

    def parse(self) -> HLSLProgram:
        """解析整个程序"""
        program = HLSLProgram(raw_source=self.source)
        program.statements = self.parse_statement_list()

        # 收集输入变量（在代码中使用但未声明的变量）
        declared = set()
        used = set()
        self._collect_vars(program.statements, declared, used)
        program.inputs = sorted(used - declared)

        return program

    def _collect_vars(self, stmts, declared: set, used: set):
        """递归收集已声明和已使用的变量"""
        for stmt in stmts:
            if isinstance(stmt, VarDeclaration):
                declared.add(stmt.var_name)
                if stmt.initializer:
                    self._collect_expr_vars(stmt.initializer, used)
            elif isinstance(stmt, ReturnStatement):
                if stmt.value:
                    self._collect_expr_vars(stmt.value, used)
            elif isinstance(stmt, ExpressionStatement):
                self._collect_expr_vars(stmt.expression, used)
            elif isinstance(stmt, IfStatement):
                self._collect_expr_vars(stmt.condition, used)
                self._collect_vars(stmt.then_body, declared, used)
                self._collect_vars(stmt.else_body, declared, used)

    def _collect_expr_vars(self, expr, used: set):
        """递归收集表达式中的变量引用"""
        if expr is None:
            return
        if isinstance(expr, Identifier):
            used.add(expr.name)
        elif isinstance(expr, BinaryOp):
            self._collect_expr_vars(expr.left, used)
            self._collect_expr_vars(expr.right, used)
        elif isinstance(expr, UnaryOp):
            self._collect_expr_vars(expr.operand, used)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                self._collect_expr_vars(arg, used)
        elif isinstance(expr, TypeConstructor):
            for arg in expr.args:
                self._collect_expr_vars(arg, used)
        elif isinstance(expr, SwizzleAccess):
            self._collect_expr_vars(expr.object, used)
        elif isinstance(expr, ArrayAccess):
            self._collect_expr_vars(expr.object, used)
            self._collect_expr_vars(expr.index, used)
        elif isinstance(expr, TernaryOp):
            self._collect_expr_vars(expr.condition, used)
            self._collect_expr_vars(expr.true_expr, used)
            self._collect_expr_vars(expr.false_expr, used)
        elif isinstance(expr, Assignment):
            self._collect_expr_vars(expr.target, used)
            self._collect_expr_vars(expr.value, used)

    # ── 语句解析 ──

    def parse_statement_list(self) -> List[ASTNode]:
        """解析语句列表，直到 EOF 或 }"""
        stmts = []
        while not self.match(TokenType.EOF, TokenType.RBRACE):
            stmt = self.parse_statement()
            if stmt is not None:
                stmts.append(stmt)
        return stmts

    def parse_statement(self) -> Optional[ASTNode]:
        """解析单条语句"""
        tok = self.current()

        # return 语句
        if tok.type == TokenType.RETURN:
            return self.parse_return()

        # for 循环（不可转换）
        if tok.type == TokenType.FOR:
            return self.parse_for_loop()

        # while 循环（不可转换）
        if tok.type == TokenType.WHILE:
            self.warnings.append(f"行 {tok.line}: while 循环无法转换为材质节点")
            return self.parse_for_loop()  # 复用不可转换逻辑

        # if 语句
        if tok.type == TokenType.IF:
            return self.parse_if_statement()

        # struct 定义（跳过整个 struct 块）
        if tok.type == TokenType.IDENTIFIER and tok.value == 'struct':
            return self.parse_struct_definition()

        # 类型声明
        if tok.type in TYPE_TOKENS:
            return self.parse_var_declaration()

        # IDENTIFIER IDENTIFIER ; 模式 → struct 类型变量声明（如 RainFuncs F;）
        # 解析为 VarDeclaration，使其被正确识别为已声明变量
        if (tok.type == TokenType.IDENTIFIER
                and self.peek(1).type == TokenType.IDENTIFIER
                and self.peek(2).type == TokenType.SEMICOLON):
            type_tok = self.advance()   # 跳过类型名（如 RainFuncs）
            name_tok = self.advance()   # 跳过变量名（如 F）
            self.advance()              # 跳过分号
            return VarDeclaration(
                line=type_tok.line, col=type_tok.col,
                type_name=type_tok.value,
                var_name=name_tok.value,
                initializer=None
            )

        # 表达式语句（赋值、函数调用等）
        return self.parse_expression_statement()

    def parse_return(self) -> ReturnStatement:
        tok = self.expect(TokenType.RETURN)
        if self.match(TokenType.SEMICOLON):
            self.advance()
            return ReturnStatement(line=tok.line, col=tok.col, value=None)
        expr = self.parse_expression()
        self.expect(TokenType.SEMICOLON)
        return ReturnStatement(line=tok.line, col=tok.col, value=expr)

    def parse_var_declaration(self) -> VarDeclaration:
        """解析变量声明: float3 color = expr;"""
        type_tok = self.advance()
        type_name = type_tok.value
        name_tok = self.expect(TokenType.IDENTIFIER)

        init = None
        if self.match(TokenType.EQUALS):
            self.advance()
            init = self.parse_expression()

        self.expect(TokenType.SEMICOLON)
        return VarDeclaration(
            line=type_tok.line, col=type_tok.col,
            type_name=type_name,
            var_name=name_tok.value,
            initializer=init
        )

    def parse_if_statement(self) -> IfStatement:
        """解析 if/else"""
        tok = self.expect(TokenType.IF)
        self.expect(TokenType.LPAREN)
        cond = self.parse_expression()
        self.expect(TokenType.RPAREN)

        then_body = self.parse_block_or_statement()

        else_body = []
        if self.match(TokenType.ELSE):
            self.advance()
            else_body = self.parse_block_or_statement()

        return IfStatement(
            line=tok.line, col=tok.col,
            condition=cond,
            then_body=then_body,
            else_body=else_body
        )

    def parse_block_or_statement(self) -> List[ASTNode]:
        """解析 { ... } 块或单条语句"""
        if self.match(TokenType.LBRACE):
            self.advance()
            stmts = self.parse_statement_list()
            self.expect(TokenType.RBRACE)
            return stmts
        else:
            stmt = self.parse_statement()
            return [stmt] if stmt else []

    def parse_struct_definition(self) -> Optional[ASTNode]:
        """跳过 struct 定义块: struct Name { ... };"""
        tok = self.advance()  # 跳过 'struct'
        # 跳过结构体名称（可选）
        if self.match(TokenType.IDENTIFIER):
            self.advance()
        # 跳过 { ... } 块
        if self.match(TokenType.LBRACE):
            self._skip_balanced(TokenType.LBRACE, TokenType.RBRACE)
        # 跳过可能的变量名和分号: struct Foo { } varName;
        if self.match(TokenType.IDENTIFIER):
            self.advance()
        if self.match(TokenType.SEMICOLON):
            self.advance()
        self.warnings.append(f"行 {tok.line}: struct 定义已跳过（不转换为材质节点）")
        return None

    def parse_for_loop(self) -> ForLoop:
        """for/while 循环 → 标记为不可转换，抓取原始代码"""
        tok = self.advance()  # for / while
        raw_start = tok.col
        # 跳过括号内容
        if self.match(TokenType.LPAREN):
            self._skip_balanced(TokenType.LPAREN, TokenType.RPAREN)
        # 跳过循环体
        if self.match(TokenType.LBRACE):
            self._skip_balanced(TokenType.LBRACE, TokenType.RBRACE)
        else:
            # 单条语句
            while not self.match(TokenType.SEMICOLON, TokenType.EOF):
                self.advance()
            if self.match(TokenType.SEMICOLON):
                self.advance()

        self.warnings.append(f"行 {tok.line}: {tok.value} 循环无法转换为材质节点，将保留为 CustomExpression")
        return ForLoop(line=tok.line, col=tok.col, raw_code=f"/* {tok.value} loop - 需要保留在 CustomExpression 中 */")

    def _skip_balanced(self, open_tok: TokenType, close_tok: TokenType):
        """跳过成对括号"""
        depth = 0
        while not self.match(TokenType.EOF):
            if self.match(open_tok):
                depth += 1
            elif self.match(close_tok):
                depth -= 1
                if depth == 0:
                    self.advance()
                    return
            self.advance()

    def parse_expression_statement(self) -> ExpressionStatement:
        """解析表达式语句"""
        tok = self.current()
        expr = self.parse_expression()
        self.expect(TokenType.SEMICOLON)
        return ExpressionStatement(line=tok.line, col=tok.col, expression=expr)

    # ── 表达式解析（优先级从低到高）──

    def parse_expression(self) -> ASTNode:
        """解析表达式（顶层入口）"""
        return self.parse_assignment_expr()

    def parse_assignment_expr(self) -> ASTNode:
        """赋值表达式: x = expr, x += expr, ..."""
        expr = self.parse_ternary()

        assign_ops = {
            TokenType.EQUALS: '=',
            TokenType.PLUS_EQ: '+=',
            TokenType.MINUS_EQ: '-=',
            TokenType.STAR_EQ: '*=',
            TokenType.SLASH_EQ: '/=',
        }

        if self.current().type in assign_ops:
            op = assign_ops[self.current().type]
            tok = self.advance()
            value = self.parse_expression()  # 右结合
            return Assignment(line=tok.line, col=tok.col, target=expr, op=op, value=value)

        return expr

    def parse_ternary(self) -> ASTNode:
        """三元运算: cond ? a : b"""
        expr = self.parse_or()
        if self.match(TokenType.QUESTION):
            tok = self.advance()
            true_expr = self.parse_expression()
            self.expect(TokenType.COLON)
            false_expr = self.parse_ternary()
            return TernaryOp(
                line=tok.line, col=tok.col,
                condition=expr,
                true_expr=true_expr,
                false_expr=false_expr
            )
        return expr

    def parse_or(self) -> ASTNode:
        """||"""
        left = self.parse_and()
        while self.match(TokenType.OR):
            tok = self.advance()
            right = self.parse_and()
            left = BinaryOp(line=tok.line, col=tok.col, op='||', left=left, right=right)
        return left

    def parse_and(self) -> ASTNode:
        """&&"""
        left = self.parse_comparison()
        while self.match(TokenType.AND):
            tok = self.advance()
            right = self.parse_comparison()
            left = BinaryOp(line=tok.line, col=tok.col, op='&&', left=left, right=right)
        return left

    def parse_comparison(self) -> ASTNode:
        """== != < > <= >="""
        left = self.parse_additive()
        cmp_ops = {
            TokenType.EQEQ: '==', TokenType.NEQ: '!=',
            TokenType.LT: '<', TokenType.GT: '>',
            TokenType.LTE: '<=', TokenType.GTE: '>=',
        }
        while self.current().type in cmp_ops:
            tok = self.advance()
            right = self.parse_additive()
            left = BinaryOp(line=tok.line, col=tok.col, op=cmp_ops[tok.type], left=left, right=right)
        return left

    def parse_additive(self) -> ASTNode:
        """+ -"""
        left = self.parse_multiplicative()
        while self.match(TokenType.PLUS, TokenType.MINUS):
            tok = self.advance()
            right = self.parse_multiplicative()
            left = BinaryOp(line=tok.line, col=tok.col, op=tok.value, left=left, right=right)
        return left

    def parse_multiplicative(self) -> ASTNode:
        """* / %"""
        left = self.parse_unary()
        while self.match(TokenType.STAR, TokenType.SLASH, TokenType.PERCENT):
            tok = self.advance()
            right = self.parse_unary()
            left = BinaryOp(line=tok.line, col=tok.col, op=tok.value, left=left, right=right)
        return left

    def parse_unary(self) -> ASTNode:
        """一元运算: -x, !flag"""
        if self.match(TokenType.MINUS):
            tok = self.advance()
            operand = self.parse_unary()
            return UnaryOp(line=tok.line, col=tok.col, op='-', operand=operand)
        if self.match(TokenType.NOT):
            tok = self.advance()
            operand = self.parse_unary()
            return UnaryOp(line=tok.line, col=tok.col, op='!', operand=operand)
        return self.parse_postfix()

    def parse_postfix(self) -> ASTNode:
        """后缀操作: .xyz, [i], (args), .method(args)"""
        expr = self.parse_primary()

        while True:
            if self.match(TokenType.DOT):
                self.advance()
                member = self.expect(TokenType.IDENTIFIER)
                # 如果后面跟着 (，则是成员方法调用（如 F.Drops(...)）
                if self.match(TokenType.LPAREN):
                    self.advance()
                    args = self.parse_arg_list()
                    self.expect(TokenType.RPAREN)
                    # 将方法调用表示为 FunctionCall，名称为 "obj.method"
                    expr = FunctionCall(
                        line=member.line, col=member.col,
                        name=f'_method_{member.value}',
                        args=[expr] + args
                    )
                # 判断是否是 swizzle（xyzw / rgba / stpq 的组合）
                elif self._is_swizzle(member.value):
                    expr = SwizzleAccess(
                        line=member.line, col=member.col,
                        object=expr, components=member.value
                    )
                else:
                    # 成员访问（struct 字段访问等）
                    expr = SwizzleAccess(
                        line=member.line, col=member.col,
                        object=expr, components=member.value
                    )
            elif self.match(TokenType.LBRACKET):
                tok = self.advance()
                index = self.parse_expression()
                self.expect(TokenType.RBRACKET)
                expr = ArrayAccess(line=tok.line, col=tok.col, object=expr, index=index)
            else:
                break

        return expr

    def _is_swizzle(self, s: str) -> bool:
        """检查是否是有效的 swizzle"""
        if len(s) == 0 or len(s) > 4:
            return False
        swizzle_sets = [
            set('xyzw'),
            set('rgba'),
            set('stpq'),
        ]
        for ss in swizzle_sets:
            if all(c in ss for c in s):
                return True
        return False

    def parse_primary(self) -> ASTNode:
        """基本表达式: 数字, 标识符, 函数调用, 括号, 类型构造"""
        tok = self.current()

        # 数字
        if tok.type == TokenType.NUMBER:
            self.advance()
            return NumberLiteral(line=tok.line, col=tok.col, value=float(tok.value))

        # 类型构造函数: float3(...)
        if tok.type in TYPE_TOKENS:
            type_name = tok.value
            self.advance()
            if self.match(TokenType.LPAREN):
                self.advance()
                args = self.parse_arg_list()
                self.expect(TokenType.RPAREN)
                return TypeConstructor(line=tok.line, col=tok.col, type_name=type_name, args=args)
            else:
                # 不带括号的类型名？可能是变量声明的一部分（不应该到这里）
                self.error(f"意外的类型名 '{type_name}'（非构造函数）")

        # 标识符或函数调用
        if tok.type == TokenType.IDENTIFIER:
            self.advance()
            if self.match(TokenType.LPAREN):
                # 函数调用
                self.advance()
                args = self.parse_arg_list()
                self.expect(TokenType.RPAREN)
                return FunctionCall(line=tok.line, col=tok.col, name=tok.value, args=args)
            else:
                return Identifier(line=tok.line, col=tok.col, name=tok.value)

        # 括号表达式
        if tok.type == TokenType.LPAREN:
            self.advance()
            expr = self.parse_expression()
            self.expect(TokenType.RPAREN)
            return expr

        self.error(f"意外的 token '{tok.value}'")

    def parse_arg_list(self) -> List[ASTNode]:
        """解析函数参数列表"""
        args = []
        if not self.match(TokenType.RPAREN):
            args.append(self.parse_expression())
            while self.match(TokenType.COMMA):
                self.advance()
                args.append(self.parse_expression())
        return args


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def parse_hlsl(source: str) -> HLSLProgram:
    """
    解析 HLSL 源代码，返回 AST

    参数:
        source: HLSL 代码字符串
    返回:
        HLSLProgram AST 节点
    """
    lexer = Lexer(source)
    tokens = lexer.tokenize()
    parser = Parser(tokens, source)
    program = parser.parse()
    program.raw_source = source
    return program


def dump_ast(node: ASTNode, indent: int = 0) -> str:
    """
    将 AST 转为可读字符串（用于调试）
    """
    pad = "  " * indent
    lines = []

    if isinstance(node, HLSLProgram):
        lines.append(f"{pad}Program (inputs: {node.inputs})")
        for stmt in node.statements:
            lines.append(dump_ast(stmt, indent + 1))
    elif isinstance(node, VarDeclaration):
        lines.append(f"{pad}VarDecl: {node.type_name} {node.var_name}")
        if node.initializer:
            lines.append(dump_ast(node.initializer, indent + 1))
    elif isinstance(node, ReturnStatement):
        lines.append(f"{pad}Return:")
        if node.value:
            lines.append(dump_ast(node.value, indent + 1))
    elif isinstance(node, ExpressionStatement):
        lines.append(f"{pad}ExprStmt:")
        lines.append(dump_ast(node.expression, indent + 1))
    elif isinstance(node, NumberLiteral):
        lines.append(f"{pad}Num({node.value})")
    elif isinstance(node, Identifier):
        lines.append(f"{pad}Id({node.name})")
    elif isinstance(node, BinaryOp):
        lines.append(f"{pad}BinOp({node.op})")
        lines.append(dump_ast(node.left, indent + 1))
        lines.append(dump_ast(node.right, indent + 1))
    elif isinstance(node, UnaryOp):
        lines.append(f"{pad}Unary({node.op})")
        lines.append(dump_ast(node.operand, indent + 1))
    elif isinstance(node, FunctionCall):
        lines.append(f"{pad}Call({node.name})")
        for arg in node.args:
            lines.append(dump_ast(arg, indent + 1))
    elif isinstance(node, TypeConstructor):
        lines.append(f"{pad}Construct({node.type_name})")
        for arg in node.args:
            lines.append(dump_ast(arg, indent + 1))
    elif isinstance(node, SwizzleAccess):
        lines.append(f"{pad}Swizzle(.{node.components})")
        lines.append(dump_ast(node.object, indent + 1))
    elif isinstance(node, ArrayAccess):
        lines.append(f"{pad}ArrayAccess")
        lines.append(dump_ast(node.object, indent + 1))
        lines.append(dump_ast(node.index, indent + 1))
    elif isinstance(node, TernaryOp):
        lines.append(f"{pad}Ternary(?:)")
        lines.append(dump_ast(node.condition, indent + 1))
        lines.append(dump_ast(node.true_expr, indent + 1))
        lines.append(dump_ast(node.false_expr, indent + 1))
    elif isinstance(node, Assignment):
        lines.append(f"{pad}Assign({node.op})")
        lines.append(dump_ast(node.target, indent + 1))
        lines.append(dump_ast(node.value, indent + 1))
    elif isinstance(node, IfStatement):
        lines.append(f"{pad}If:")
        lines.append(dump_ast(node.condition, indent + 1))
        lines.append(f"{pad}  Then:")
        for s in node.then_body:
            lines.append(dump_ast(s, indent + 2))
        if node.else_body:
            lines.append(f"{pad}  Else:")
            for s in node.else_body:
                lines.append(dump_ast(s, indent + 2))
    elif isinstance(node, ForLoop):
        lines.append(f"{pad}ForLoop(不可转换)")
    else:
        lines.append(f"{pad}{type(node).__name__}")

    return "\n".join(lines)
