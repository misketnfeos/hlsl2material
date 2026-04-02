"""
============================================================
 ue4_codegen.py
 MaterialGraph → UE4 Editor Python 脚本
============================================================

生成可以在 UE4 4.24 Editor 中运行的 Python 脚本，
自动创建材质、添加节点、设置属性、连接引脚。

使用方法：
  1. 用本工具生成 .py 脚本
  2. 在 UE4 编辑器中：
     - Window → Developer Tools → Output Log
     - 底部切换为 "Python"
     - 执行: exec(open(r"生成的脚本路径").read())
     或：
     - Edit → Editor Preferences → Python → 启用 Python Editor Script
     - 在内容浏览器中右键 → Run Python Script

生成的脚本使用以下 UE4 Python API (4.24+)：
  - unreal.AssetToolsHelpers.get_asset_tools()
  - asset_tools.create_asset()
  - unreal.MaterialEditingLibrary.create_material_expression()
  - unreal.MaterialEditingLibrary.connect_material_expressions()
  - unreal.MaterialEditingLibrary.connect_material_property()
  - unreal.MaterialEditingLibrary.recompile_material()
============================================================
"""

from typing import Dict, List, Optional, Set
from node_mapper import MaterialNode, MaterialGraph


# ═══════════════════════════════════════════════════════════
# UE4 节点类名映射（Python API 中的类名）
# ═══════════════════════════════════════════════════════════

# 有些节点在 Python API 中的类名和 C++ 不完全一致
UE_CLASS_MAP = {
    'MaterialExpressionAdd':                'unreal.MaterialExpressionAdd',
    'MaterialExpressionSubtract':           'unreal.MaterialExpressionSubtract',
    'MaterialExpressionMultiply':           'unreal.MaterialExpressionMultiply',
    'MaterialExpressionDivide':             'unreal.MaterialExpressionDivide',
    'MaterialExpressionFmod':               'unreal.MaterialExpressionFmod',
    'MaterialExpressionPower':              'unreal.MaterialExpressionPower',
    'MaterialExpressionSquareRoot':         'unreal.MaterialExpressionSquareRoot',
    'MaterialExpressionAbs':                'unreal.MaterialExpressionAbs',
    'MaterialExpressionSign':               'unreal.MaterialExpressionSign',
    'MaterialExpressionFloor':              'unreal.MaterialExpressionFloor',
    'MaterialExpressionCeil':               'unreal.MaterialExpressionCeil',
    'MaterialExpressionRound':              'unreal.MaterialExpressionRound',
    'MaterialExpressionFrac':               'unreal.MaterialExpressionFrac',
    'MaterialExpressionMin':                'unreal.MaterialExpressionMin',
    'MaterialExpressionMax':                'unreal.MaterialExpressionMax',
    'MaterialExpressionOneMinus':           'unreal.MaterialExpressionOneMinus',
    'MaterialExpressionTruncate':           'unreal.MaterialExpressionTruncate',
    'MaterialExpressionLinearInterpolate':  'unreal.MaterialExpressionLinearInterpolate',
    # SmoothStep / Step — SmoothStep 使用 MaterialFunctionCall，Step 用 If 模拟
    'MaterialExpressionMaterialFunctionCall': 'unreal.MaterialExpressionMaterialFunctionCall',
    'MaterialExpressionClamp':              'unreal.MaterialExpressionClamp',
    'MaterialExpressionSaturate':           'unreal.MaterialExpressionSaturate',
    'MaterialExpressionSine':               'unreal.MaterialExpressionSine',
    'MaterialExpressionCosine':             'unreal.MaterialExpressionCosine',
    'MaterialExpressionTangent':            'unreal.MaterialExpressionTangent',
    'MaterialExpressionArcsine':            'unreal.MaterialExpressionArcsine',
    'MaterialExpressionArccosine':          'unreal.MaterialExpressionArccosine',
    'MaterialExpressionArctangent':         'unreal.MaterialExpressionArctangent',
    'MaterialExpressionArctangent2':        'unreal.MaterialExpressionArctangent2',
    'MaterialExpressionDotProduct':         'unreal.MaterialExpressionDotProduct',
    'MaterialExpressionCrossProduct':       'unreal.MaterialExpressionCrossProduct',
    'MaterialExpressionNormalize':          'unreal.MaterialExpressionNormalize',
    'MaterialExpressionComponentMask':      'unreal.MaterialExpressionComponentMask',
    'MaterialExpressionAppendVector':       'unreal.MaterialExpressionAppendVector',
    # VectorLength — 在当前引擎中不存在，由 node_mapper 回退为 Distance(x, 0)
    # 'MaterialExpressionVectorLength':     不存在
    'MaterialExpressionDistance':            'unreal.MaterialExpressionDistance',
    'MaterialExpressionTransform':          'unreal.MaterialExpressionTransform',
    'MaterialExpressionTransformPosition':  'unreal.MaterialExpressionTransformPosition',
    'MaterialExpressionTextureSample':      'unreal.MaterialExpressionTextureSample',
    'MaterialExpressionTextureObject':      'unreal.MaterialExpressionTextureObject',
    'MaterialExpressionTextureObjectParameter': 'unreal.MaterialExpressionTextureObjectParameter',
    'MaterialExpressionTextureSampleParameter2D': 'unreal.MaterialExpressionTextureSampleParameter2D',
    'MaterialExpressionIf':                 'unreal.MaterialExpressionIf',
    'MaterialExpressionStaticSwitch':       'unreal.MaterialExpressionStaticSwitch',
    'MaterialExpressionCustom':             'unreal.MaterialExpressionCustom',
    'MaterialExpressionConstant':           'unreal.MaterialExpressionConstant',
    'MaterialExpressionConstant2Vector':    'unreal.MaterialExpressionConstant2Vector',
    'MaterialExpressionConstant3Vector':    'unreal.MaterialExpressionConstant3Vector',
    'MaterialExpressionConstant4Vector':    'unreal.MaterialExpressionConstant4Vector',
    'MaterialExpressionConstantBiasScale':  'unreal.MaterialExpressionConstantBiasScale',
    'MaterialExpressionFunctionInput':      'unreal.MaterialExpressionFunctionInput',
    'MaterialExpressionFunctionOutput':     'unreal.MaterialExpressionFunctionOutput',
    'MaterialExpressionScalarParameter':    'unreal.MaterialExpressionScalarParameter',
    'MaterialExpressionVectorParameter':    'unreal.MaterialExpressionVectorParameter',
    'MaterialExpressionStaticBoolParameter':'unreal.MaterialExpressionStaticBoolParameter',
    'MaterialExpressionStaticSwitchParameter': 'unreal.MaterialExpressionStaticSwitchParameter',
    # Exponential / Exponential2 — 在当前引擎中不存在
    # 'MaterialExpressionExponential':      不存在
    # 'MaterialExpressionExponential2':     不存在
    'MaterialExpressionLogarithm10':        'unreal.MaterialExpressionLogarithm10',
    'MaterialExpressionLogarithm2':         'unreal.MaterialExpressionLogarithm2',
    'MaterialExpressionDDX':                'unreal.MaterialExpressionDDX',
    'MaterialExpressionDDY':                'unreal.MaterialExpressionDDY',
    'MaterialExpressionFresnel':            'unreal.MaterialExpressionFresnel',
    'MaterialExpressionDesaturation':       'unreal.MaterialExpressionDesaturation',
    'MaterialExpressionBlackBody':          'unreal.MaterialExpressionBlackBody',
    'MaterialExpressionNoise':              'unreal.MaterialExpressionNoise',
    'MaterialExpressionDepthFade':          'unreal.MaterialExpressionDepthFade',
    'MaterialExpressionSceneColor':         'unreal.MaterialExpressionSceneColor',
    'MaterialExpressionSceneTexture':       'unreal.MaterialExpressionSceneTexture',
    'MaterialExpressionSphereMask':         'unreal.MaterialExpressionSphereMask',
    'MaterialExpressionPanner':             'unreal.MaterialExpressionPanner',
    'MaterialExpressionRotator':            'unreal.MaterialExpressionRotator',
    'MaterialExpressionBumpOffset':         'unreal.MaterialExpressionBumpOffset',
    'MaterialExpressionAntialiasedTextureMask': 'unreal.MaterialExpressionAntialiasedTextureMask',
    'MaterialExpressionDistanceFieldGradient':  'unreal.MaterialExpressionDistanceFieldGradient',
    'MaterialExpressionDistanceToNearestSurface': 'unreal.MaterialExpressionDistanceToNearestSurface',
    'MaterialExpressionDynamicParameter':   'unreal.MaterialExpressionDynamicParameter',
    'MaterialExpressionChannelMaskParameter': 'unreal.MaterialExpressionChannelMaskParameter',
    'MaterialExpressionFeatureLevelSwitch':  'unreal.MaterialExpressionFeatureLevelSwitch',
    'MaterialExpressionQualitySwitch':       'unreal.MaterialExpressionQualitySwitch',
    'MaterialExpressionShadingPathSwitch':   'unreal.MaterialExpressionShadingPathSwitch',
    'MaterialExpressionMakeMaterialAttributes':  'unreal.MaterialExpressionMakeMaterialAttributes',
    'MaterialExpressionBreakMaterialAttributes': 'unreal.MaterialExpressionBreakMaterialAttributes',
    'MaterialExpressionGIReplace':           'unreal.MaterialExpressionGIReplace',
    'MaterialExpressionLightmassReplace':    'unreal.MaterialExpressionLightmassReplace',
    'MaterialExpressionAtmosphericFogColor': 'unreal.MaterialExpressionAtmosphericFogColor',
    'MaterialExpressionReflectionVector':    'unreal.MaterialExpressionReflectionVectorWS',
    # UE4 引擎内置变量节点（无输入端口的源节点）
    'MaterialExpressionCameraPositionWS':    'unreal.MaterialExpressionCameraPositionWS',
    'MaterialExpressionWorldPosition':       'unreal.MaterialExpressionWorldPosition',
    'MaterialExpressionPixelNormalWS':       'unreal.MaterialExpressionPixelNormalWS',
    'MaterialExpressionVertexNormalWS':      'unreal.MaterialExpressionVertexNormalWS',
    'MaterialExpressionCameraVectorWS':      'unreal.MaterialExpressionCameraVectorWS',
    'MaterialExpressionTextureCoordinate':   'unreal.MaterialExpressionTextureCoordinate',
    'MaterialExpressionTime':                'unreal.MaterialExpressionTime',
    'MaterialExpressionScreenPosition':      'unreal.MaterialExpressionScreenPosition',
    'MaterialExpressionViewSize':            'unreal.MaterialExpressionViewSize',
    'MaterialExpressionVertexColor':         'unreal.MaterialExpressionVertexColor',
    'MaterialExpressionReflectionVectorWS':  'unreal.MaterialExpressionReflectionVectorWS',
    'MaterialExpressionObjectPositionWS':    'unreal.MaterialExpressionObjectPositionWS',
    'MaterialExpressionActorPositionWS':     'unreal.MaterialExpressionActorPositionWS',
    'MaterialExpressionObjectRadius':        'unreal.MaterialExpressionObjectRadius',
    'MaterialExpressionObjectBounds':        'unreal.MaterialExpressionObjectBounds',
    'MaterialExpressionPixelDepth':          'unreal.MaterialExpressionPixelDepth',
    'MaterialExpressionSceneDepth':          'unreal.MaterialExpressionSceneDepth',
    'MaterialExpressionTwoSidedSign':        'unreal.MaterialExpressionTwoSidedSign',
    'MaterialExpressionVertexTangentWS':     'unreal.MaterialExpressionVertexTangentWS',
    'MaterialExpressionLightVector':         'unreal.MaterialExpressionLightVector',
    'MaterialExpressionPreSkinnedPosition':  'unreal.MaterialExpressionPreSkinnedPosition',
    'MaterialExpressionPreSkinnedNormal':    'unreal.MaterialExpressionPreSkinnedNormal',
    'MaterialExpressionParticleColor':       'unreal.MaterialExpressionParticleColor',
    'MaterialExpressionParticlePositionWS':  'unreal.MaterialExpressionParticlePositionWS',
    'MaterialExpressionParticleRadius':      'unreal.MaterialExpressionParticleRadius',
    'MaterialExpressionParticleRelativeTime':'unreal.MaterialExpressionParticleRelativeTime',
    'MaterialExpressionParticleDirection':   'unreal.MaterialExpressionParticleDirection',
    'MaterialExpressionParticleSpeed':       'unreal.MaterialExpressionParticleSpeed',
    'MaterialExpressionParticleSize':        'unreal.MaterialExpressionParticleSize',
    'MaterialExpressionCollectionParameter': 'unreal.MaterialExpressionCollectionParameter',
    'MaterialExpressionEyeAdaptation':       'unreal.MaterialExpressionEyeAdaptation',
    'MaterialExpressionPerInstanceFadeAmount': 'unreal.MaterialExpressionPerInstanceFadeAmount',
    'MaterialExpressionPerInstanceRandom':   'unreal.MaterialExpressionPerInstanceRandom',
}

# 输入端口名 → UE4 实际输入端口属性名
# UE4 的 connect_material_expressions(src, src_output_name, dst, dst_input_name)
# 其中 dst_input_name 是 UE4 节点类中实际的 FExpressionInput 属性名
# 注意：空字符串 "" 代表默认/唯一输入端口

INPUT_PORT_NAME_MAP = {
    # ═══════════════════════════════════════════════════
    # 以下端口名来自 dump_material_expressions.py 的连线探测结果
    # 探测日期: 2026-03-18, 引擎: UE4 DFM 分支
    # ═══════════════════════════════════════════════════

    # ── 双输入节点 (A/B) — JSON 已验证 ──
    'MaterialExpressionAdd':            {'A': 'A', 'B': 'B'},
    'MaterialExpressionSubtract':       {'A': 'A', 'B': 'B'},
    'MaterialExpressionMultiply':       {'A': 'A', 'B': 'B'},
    'MaterialExpressionDivide':         {'A': 'A', 'B': 'B'},
    'MaterialExpressionFmod':           {'A': 'A', 'B': 'B'},
    'MaterialExpressionMin':            {'A': 'A', 'B': 'B'},
    'MaterialExpressionMax':            {'A': 'A', 'B': 'B'},
    'MaterialExpressionDotProduct':     {'A': 'A', 'B': 'B'},
    'MaterialExpressionCrossProduct':   {'A': 'A', 'B': 'B'},
    'MaterialExpressionDistance':       {'A': 'A', 'B': 'B'},
    'MaterialExpressionAppendVector':   {'A': 'A', 'B': 'B'},

    # ── Arctangent2 — JSON: [X, Y] ──
    'MaterialExpressionArctangent2':    {'Y': 'Y', 'X': 'X'},

    # ── Power — UE4 实际端口名是 "Base" 和 "Exp"（不是 "Exponent"！）
    # 通过用户手动验证的材质序列化数据确认：PinName="Exp"
    'MaterialExpressionPower':          {'Base': 'Base', 'Exponent': 'Exp'},

    # ── 单输入节点 — JSON: inputs=[]，说明端口名是空字符串 "" ──
    'MaterialExpressionAbs':            {'Input': ''},
    'MaterialExpressionSign':           {'Input': ''},
    'MaterialExpressionFloor':          {'Input': ''},
    'MaterialExpressionCeil':           {'Input': ''},
    'MaterialExpressionRound':          {'Input': ''},
    'MaterialExpressionFrac':           {'Input': ''},
    'MaterialExpressionSquareRoot':     {'Input': ''},
    'MaterialExpressionOneMinus':       {'Input': ''},
    'MaterialExpressionTruncate':       {'Input': ''},
    'MaterialExpressionSaturate':       {'Input': ''},
    'MaterialExpressionSine':           {'Input': ''},
    'MaterialExpressionCosine':         {'Input': ''},
    'MaterialExpressionTangent':        {'Input': ''},
    'MaterialExpressionArcsine':        {'Input': ''},
    'MaterialExpressionArccosine':      {'Input': ''},
    'MaterialExpressionArctangent':     {'Input': ''},
    'MaterialExpressionBlackBody':      {'Input': ''},
    'MaterialExpressionConstantBiasScale': {'Input': ''},
    'MaterialExpressionComponentMask':  {'Input': ''},

    # ── 单输入节点 — JSON: inputs=[X]，端口名是 "X" ──
    'MaterialExpressionLogarithm2':     {'Input': 'X'},
    'MaterialExpressionLogarithm10':    {'Input': 'X'},

    # ── Normalize — JSON: [VectorInput] ──
    'MaterialExpressionNormalize':      {'VectorInput': 'VectorInput'},

    # ── DDX/DDY — JSON: [Value] ──
    'MaterialExpressionDDX':            {'Value': 'Value'},
    'MaterialExpressionDDY':            {'Value': 'Value'},

    # ── Lerp — JSON: [A, B, Alpha] ──
    'MaterialExpressionLinearInterpolate': {'A': 'A', 'B': 'B', 'Alpha': 'Alpha'},

    # ── Clamp — JSON: [Min, Max]，主输入端口探测为空字符串 "" ──
    'MaterialExpressionClamp':          {'Input': '', 'Min': 'Min', 'Max': 'Max'},

    # ── If 节点 — JSON 只探测到 [A, B]
    # AGreaterThanB/AEqualsB/ALessThanB 是标准 UE4 端口名（候选列表已包含，已验证）
    'MaterialExpressionIf':             {'A': 'A', 'B': 'B', 'A > B': 'AGreaterThanB', 'A == B': 'AEqualsB', 'A < B': 'ALessThanB'},

    # ── StaticSwitch — JSON: [Value, True, False] ──
    'MaterialExpressionStaticSwitch':   {'Value': 'Value', 'True': 'True', 'False': 'False'},
    'MaterialExpressionStaticSwitchParameter': {'True': 'True', 'False': 'False'},

    # ── TextureSample — JSON: [UVs, Tex] ──
    # 修正：旧映射 UVs→Coordinates 是错误的，实际端口名就是 "UVs"
    'MaterialExpressionTextureSample':  {'UVs': 'UVs', 'Tex': 'Tex'},
    'MaterialExpressionTextureSampleParameter2D': {'UVs': 'UVs'},

    # ── Fresnel — JSON: [ExponentIn, BaseReflectFractionIn, Normal] ──
    'MaterialExpressionFresnel':        {'ExponentIn': 'ExponentIn', 'BaseReflectFractionIn': 'BaseReflectFractionIn', 'Normal': 'Normal'},

    # ── Desaturation — JSON: [Fraction] ──
    'MaterialExpressionDesaturation':   {'Input': '', 'Fraction': 'Fraction'},

    # ── Noise — JSON: [Position, FilterWidth] ──
    'MaterialExpressionNoise':          {'Position': 'Position', 'FilterWidth': 'FilterWidth'},

    # ── DepthFade — JSON: [FadeDistance, Opacity] ──
    'MaterialExpressionDepthFade':      {'FadeDistance': 'FadeDistance', 'Opacity': 'Opacity', 'InOpacity': 'Opacity'},

    # ── SceneColor/SceneDepth/SceneTexture — JSON: [UVs] ──
    'MaterialExpressionSceneColor':     {'UVs': 'UVs', 'Input': 'UVs'},
    'MaterialExpressionSceneDepth':     {'UVs': 'UVs', 'Input': 'UVs'},
    'MaterialExpressionSceneTexture':   {'UVs': 'UVs', 'Input': 'UVs'},

    # ── SphereMask — JSON: [A, B, Radius, Hardness] ──
    'MaterialExpressionSphereMask':     {'A': 'A', 'B': 'B', 'Radius': 'Radius', 'Hardness': 'Hardness'},

    # ── Panner — JSON: [Coordinate, Time, Speed] ──
    'MaterialExpressionPanner':         {'Coordinate': 'Coordinate', 'Time': 'Time', 'Speed': 'Speed'},

    # ── Rotator — JSON: [Coordinate, Time] ──
    'MaterialExpressionRotator':        {'Coordinate': 'Coordinate', 'Time': 'Time'},

    # ── BumpOffset — JSON: [Coordinate, Height, HeightRatioInput] ──
    'MaterialExpressionBumpOffset':     {'Coordinate': 'Coordinate', 'Height': 'Height', 'HeightRatioInput': 'HeightRatioInput'},

    # ── FunctionInput — JSON: [Preview] ──
    'MaterialExpressionFunctionInput':  {'Preview': 'Preview'},

    # ── FeatureLevelSwitch — JSON: [Default, ES2, ES3_1, SM5] ──
    'MaterialExpressionFeatureLevelSwitch': {'Default': 'Default', 'ES2': 'ES2', 'ES3_1': 'ES3_1', 'SM5': 'SM5'},

    # ── QualitySwitch — JSON: [Default, Low, High, Medium] ──
    'MaterialExpressionQualitySwitch':  {'Default': 'Default', 'Low': 'Low', 'High': 'High', 'Medium': 'Medium'},

    # ── ShadingPathSwitch — JSON: [Default] ──
    'MaterialExpressionShadingPathSwitch': {'Default': 'Default'},

    # ── MakeMaterialAttributes — JSON: [Normal, BaseColor, Metallic, ...] ──
    'MaterialExpressionMakeMaterialAttributes': {
        'Normal': 'Normal', 'BaseColor': 'BaseColor', 'Metallic': 'Metallic',
        'Specular': 'Specular', 'Roughness': 'Roughness', 'EmissiveColor': 'EmissiveColor',
        'Opacity': 'Opacity', 'OpacityMask': 'OpacityMask',
        'WorldPositionOffset': 'WorldPositionOffset', 'WorldDisplacement': 'WorldDisplacement',
        'TessellationMultiplier': 'TessellationMultiplier', 'SubsurfaceColor': 'SubsurfaceColor',
        'ClearCoat': 'ClearCoat', 'ClearCoatRoughness': 'ClearCoatRoughness',
        'AmbientOcclusion': 'AmbientOcclusion', 'Refraction': 'Refraction',
        'PixelDepthOffset': 'PixelDepthOffset',
    },

    # ── GIReplace — JSON: [Default, StaticIndirect, DynamicIndirect] ──
    'MaterialExpressionGIReplace':      {'Default': 'Default', 'StaticIndirect': 'StaticIndirect', 'DynamicIndirect': 'DynamicIndirect'},

    # ── LightmassReplace — JSON: [Realtime, Lightmass] ──
    'MaterialExpressionLightmassReplace': {'Realtime': 'Realtime', 'Lightmass': 'Lightmass'},

    # ── AtmosphericFogColor — JSON: [WorldPosition] ──
    'MaterialExpressionAtmosphericFogColor': {'WorldPosition': 'WorldPosition'},

    # ── DistanceField — JSON: [Position] ──
    'MaterialExpressionDistanceFieldGradient':      {'Position': 'Position'},
    'MaterialExpressionDistanceToNearestSurface':   {'Position': 'Position'},

    # ── AntialiasedTextureMask — JSON: [UVs] ──
    'MaterialExpressionAntialiasedTextureMask':     {'UVs': 'UVs'},

    # ── MaterialFunctionCall (SmoothStep 等) — 动态端口，由函数定义决定 ──
    # SmoothStep 的输入端口: Alpha(0), Min(1), Max(2) — 通过索引连接
    'MaterialExpressionMaterialFunctionCall': {},

    # ── TextureObjectParameter — 无输入端口（纯输出节点）──
    'MaterialExpressionTextureObjectParameter': {},

    # ── Custom Expression — 动态端口 ──
    'MaterialExpressionCustom': {},

    # ═══════════════════════════════════════════════════
    # 以下节点在当前引擎中不存在（枚举失败），使用回退方案
    # SmoothStep, Step, VectorLength, Negate, Exponential, Exponential2
    # 代码中使用 If 节点或 Multiply 模拟
    # ═══════════════════════════════════════════════════
}


# ═══════════════════════════════════════════════════════════
# 代码生成器
# ═══════════════════════════════════════════════════════════

class UE4CodeGen:
    """生成 UE4 Editor Python 脚本"""

    def __init__(self, graph: MaterialGraph):
        self.graph = graph
        self.lines: List[str] = []
        # node_id → Python 变量名
        self.var_names: Dict[int, str] = {}

    def generate(self, material_name: str = 'M_Generated',
                 material_path: str = '/Game/Materials',
                 connect_to_emissive: bool = True) -> str:
        """生成完整的 UE4 Python 脚本"""

        self.lines = []
        self._emit('"""')
        self._emit(f'自动生成的 UE4 材质脚本: {material_name}')
        self._emit(f'由 HLSL → Material Node 转换工具生成')
        self._emit('')
        self._emit('使用方法:')
        self._emit('  1. 在 UE4 编辑器 Output Log 底部切换为 Python')
        self._emit(f'  2. 执行: exec(open(r"<此文件路径>").read())')
        self._emit('"""')
        self._emit('')
        self._emit('import unreal')
        self._emit('')
        self._emit('')

        # 辅助函数
        self._emit('# ═══════════════════════════════════════════════════')
        self._emit('# 辅助函数')
        self._emit('# ═══════════════════════════════════════════════════')
        self._emit('')
        self._emit('def safe_connect(mat, src_node, src_out, dst_node, dst_in, alt_names=None):')
        self._emit('    """安全连接两个节点，支持多种连接策略（索引/名称回退）"""')
        self._emit('    # 策略 1: 直接用给定的 dst_in 连接')
        self._emit('    try:')
        self._emit('        result = unreal.MaterialEditingLibrary.connect_material_expressions(')
        self._emit('            src_node, src_out, dst_node, dst_in')
        self._emit('        )')
        self._emit('        if result:')
        self._emit('            return True')
        self._emit('    except Exception:')
        self._emit('        pass')
        self._emit('    # 策略 2: 尝试备选名称列表 (用于 MaterialFunctionCall 等动态端口节点)')
        self._emit('    if alt_names:')
        self._emit('        for alt in alt_names:')
        self._emit('            try:')
        self._emit('                result = unreal.MaterialEditingLibrary.connect_material_expressions(')
        self._emit('                    src_node, src_out, dst_node, alt')
        self._emit('                )')
        self._emit('                if result:')
        self._emit('                    return True')
        self._emit('            except Exception:')
        self._emit('                pass')
        self._emit('    unreal.log_warning(f"连接失败: -> {dst_in} (alt={alt_names})")')
        self._emit('    return False')
        self._emit('')
        self._emit('')

        # 主函数
        self._emit('def create_material():')
        self._emit(f'    """创建材质: {material_name}"""')
        self._emit('')
        self._emit('    # ── 创建材质资产 ──')
        self._emit('    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()')
        self._emit(f'    mat = asset_tools.create_asset(')
        self._emit(f'        "{material_name}",')
        self._emit(f'        "{material_path}",')
        self._emit(f'        unreal.Material,')
        self._emit(f'        unreal.MaterialFactoryNew()')
        self._emit(f'    )')
        self._emit('')
        self._emit('    if mat is None:')
        self._emit(f'        # 材质已存在，尝试加载')
        self._emit(f'        mat = unreal.load_asset("{material_path}/{material_name}")')
        self._emit('        if mat is None:')
        self._emit(f'            unreal.log_error("无法创建或加载材质: {material_name}")')
        self._emit('            return None')
        self._emit('')
        self._emit(f'    unreal.log("创建材质: {material_name}")')
        self._emit('')

        # 计算布局
        from graph_visualizer import compute_layout
        positions = compute_layout(self.graph)

        # 创建所有节点
        self._emit('    # ── 创建节点 ──')
        node_order = self._get_creation_order()
        for node in node_order:
            self._gen_create_node(node, positions)
        self._emit('')

        # 设置属性
        self._emit('    # ── 设置节点属性 ──')
        for node in node_order:
            self._gen_set_properties(node)
        self._emit('')

        # 连接节点
        self._emit('    # ── 连接节点 ──')
        for node in node_order:
            self._gen_connections(node)
        self._emit('')

        # 连接到材质输出
        if connect_to_emissive and self.graph.output_node:
            out_var = self._get_var_name(self.graph.output_node)
            self._emit('    # ── 连接到材质输出 (Emissive Color) ──')
            self._emit('    try:')
            self._emit(f'        unreal.MaterialEditingLibrary.connect_material_property(')
            self._emit(f'            {out_var}, "",')
            self._emit(f'            unreal.MaterialProperty.MP_EMISSIVE_COLOR')
            self._emit(f'        )')
            self._emit('    except Exception as e:')
            self._emit(f'        unreal.log_warning(f"连接到 Emissive 失败: {{e}}")')
            self._emit(f'        # 你可以手动在编辑器中连接输出节点')
            self._emit('')

        # 重编译
        self._emit('    # ── 重编译材质 ──')
        self._emit('    unreal.MaterialEditingLibrary.recompile_material(mat)')
        self._emit(f'    unreal.log("材质 {material_name} 创建完成！共 {len(self.graph.nodes)} 个节点")')
        self._emit('    return mat')
        self._emit('')

        # 警告注释
        if self.graph.warnings:
            self._emit('')
            self._emit('# ═══════════════════════════════════════════════════')
            self._emit('# ⚠️ 转换警告（请手动检查）')
            self._emit('# ═══════════════════════════════════════════════════')
            for w in self.graph.warnings:
                self._emit(f'# {w}')
            self._emit('')

        # 执行
        self._emit('')
        self._emit('# ═══════════════════════════════════════════════════')
        self._emit('# 执行')
        self._emit('# ═══════════════════════════════════════════════════')
        self._emit('if __name__ == "__main__":')
        self._emit('    create_material()')
        self._emit('')

        return '\n'.join(self.lines)

    def _emit(self, line: str):
        self.lines.append(line)

    def _get_var_name(self, node: MaterialNode) -> str:
        """获取节点的 Python 变量名"""
        if node.id not in self.var_names:
            # 清理名称
            base = node.display_name.replace(' ', '_').replace(':', '_')
            base = ''.join(c for c in base if c.isalnum() or c == '_')
            if not base or base[0].isdigit():
                base = 'node_' + base
            self.var_names[node.id] = f'{base}_{node.id}'
        return self.var_names[node.id]

    def _get_creation_order(self) -> List[MaterialNode]:
        """获取节点创建顺序（拓扑序，确保依赖先创建）"""
        visited: Set[int] = set()
        order: List[MaterialNode] = []
        node_by_id = {n.id: n for n in self.graph.nodes}

        def visit(node: MaterialNode):
            if node.id in visited:
                return
            visited.add(node.id)
            for iname, inode in node.inputs.items():
                if inode and inode.id in node_by_id:
                    visit(inode)
            order.append(node)

        # 从输出节点开始
        if self.graph.output_node:
            visit(self.graph.output_node)

        # 确保所有节点都被包含
        for node in self.graph.nodes:
            if node.id not in visited:
                visit(node)

        return order

    def _gen_create_node(self, node: MaterialNode, positions: Dict):
        """生成创建节点的代码"""
        var = self._get_var_name(node)
        x, y = positions.get(node.id, (0, 0))

        ue_class = UE_CLASS_MAP.get(node.ue_class, f'unreal.{node.ue_class}')

        # 特殊处理：旧的 FunctionInput 兼容（如果还有）
        if node.ue_class == 'MaterialExpressionFunctionInput':
            param_name = node.properties.get('InputName', 'Input')
            self._emit(f'    # 输入参数: {param_name}')
            self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
            self._emit(f'        mat, unreal.MaterialExpressionVectorParameter, {x}, {y}')
            self._emit(f'    )')
            self._emit(f'    {var}.set_editor_property("parameter_name", "{param_name}")')
            return

        # ScalarParameter（float 标量参数）
        if node.ue_class == 'MaterialExpressionScalarParameter':
            param_name = node.properties.get('ParameterName', 'Param')
            default_val = node.properties.get('DefaultValue', 0.0)
            self._emit(f'    # 标量参数: {param_name}')
            self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
            self._emit(f'        mat, unreal.MaterialExpressionScalarParameter, {x}, {y}')
            self._emit(f'    )')
            self._emit(f'    {var}.set_editor_property("parameter_name", "{param_name}")')
            self._emit(f'    {var}.set_editor_property("default_value", {default_val})')
            return

        # VectorParameter（向量参数）
        if node.ue_class == 'MaterialExpressionVectorParameter':
            param_name = node.properties.get('ParameterName', 'Param')
            self._emit(f'    # 向量参数: {param_name}')
            self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
            self._emit(f'        mat, unreal.MaterialExpressionVectorParameter, {x}, {y}')
            self._emit(f'    )')
            self._emit(f'    {var}.set_editor_property("parameter_name", "{param_name}")')
            return

        # TextureObjectParameter（纹理对象参数）
        if node.ue_class == 'MaterialExpressionTextureObjectParameter':
            param_name = node.properties.get('ParameterName', 'Texture')
            self._emit(f'    # 纹理参数: {param_name}')
            self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
            self._emit(f'        mat, unreal.MaterialExpressionTextureObjectParameter, {x}, {y}')
            self._emit(f'    )')
            self._emit(f'    {var}.set_editor_property("parameter_name", "{param_name}")')
            return

        # MaterialFunctionCall（引擎内置 MaterialFunction 如 SmoothStep）
        if node.ue_class == 'MaterialExpressionMaterialFunctionCall':
            func_path = node.properties.get('MaterialFunction', '')
            self._emit(f'    # MaterialFunction: {node.display_name}')
            self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
            self._emit(f'        mat, unreal.MaterialExpressionMaterialFunctionCall, {x}, {y}')
            self._emit(f'    )')
            if func_path:
                self._emit(f'    # 加载 MaterialFunction 并设置')
                self._emit(f'    _func_asset = unreal.load_asset("{func_path}")')
                self._emit(f'    if _func_asset:')
                self._emit(f'        {var}.set_editor_property("material_function", _func_asset)')
                self._emit(f'    else:')
                self._emit(f'        unreal.log_warning("无法加载 MaterialFunction: {func_path}")')
            return

        # 引擎内置节点（无参数，直接创建即可）
        self._emit(f'    # {node.display_name}')
        self._emit(f'    {var} = unreal.MaterialEditingLibrary.create_material_expression(')
        self._emit(f'        mat, {ue_class}, {x}, {y}')
        self._emit(f'    )')

    def _gen_set_properties(self, node: MaterialNode):
        """生成设置节点属性的代码"""
        var = self._get_var_name(node)

        if node.ue_class == 'MaterialExpressionConstant':
            val = node.properties.get('R', 0.0)
            self._emit(f'    {var}.set_editor_property("r", {val})')

        elif node.ue_class == 'MaterialExpressionConstant2Vector':
            r = node.properties.get('R', 0.0)
            g = node.properties.get('G', 0.0)
            self._emit(f'    {var}.set_editor_property("r", {r})')
            self._emit(f'    {var}.set_editor_property("g", {g})')

        elif node.ue_class == 'MaterialExpressionConstant3Vector':
            const = node.properties.get('Constant', {})
            r = const.get('R', 0.0)
            g = const.get('G', 0.0)
            b = const.get('B', 0.0)
            self._emit(f'    {var}.set_editor_property("constant", unreal.LinearColor({r}, {g}, {b}, 1.0))')

        elif node.ue_class == 'MaterialExpressionConstant4Vector':
            const = node.properties.get('Constant', {})
            r = const.get('R', 0.0)
            g = const.get('G', 0.0)
            b = const.get('B', 0.0)
            a = const.get('A', 1.0)
            self._emit(f'    {var}.set_editor_property("constant", unreal.LinearColor({r}, {g}, {b}, {a}))')

        elif node.ue_class == 'MaterialExpressionComponentMask':
            r = node.properties.get('R', False)
            g = node.properties.get('G', False)
            b = node.properties.get('B', False)
            a = node.properties.get('A', False)
            self._emit(f'    {var}.set_editor_property("r", {r})')
            self._emit(f'    {var}.set_editor_property("g", {g})')
            self._emit(f'    {var}.set_editor_property("b", {b})')
            self._emit(f'    {var}.set_editor_property("a", {a})')

        elif node.ue_class == 'MaterialExpressionCustom':
            code = node.properties.get('Code', '')
            if code:
                self._emit(f'    {var}.set_editor_property("code", """{code}""")')
            # 设置 Custom Node 的输入 Pins
            inputs_info = node.properties.get('Inputs', [])
            if inputs_info:
                self._emit(f'    # 配置 Custom Node 输入 Pins')
                self._emit(f'    _custom_inputs_{node.id} = []')
                for inp_item in inputs_info:
                    inp_name = inp_item.get('InputName', '')
                    self._emit(f'    _ci = unreal.CustomInput()')
                    self._emit(f'    _ci.input_name = "{inp_name}"')
                    self._emit(f'    _custom_inputs_{node.id}.append(_ci)')
                self._emit(f'    {var}.set_editor_property("inputs", _custom_inputs_{node.id})')
            # 设置描述（在编辑器中显示为节点标题）
            desc = node.properties.get('Description', '')
            if desc:
                self._emit(f'    {var}.set_editor_property("description", "{desc}")')

    def _gen_connections(self, node: MaterialNode):
        """生成连接节点的代码"""
        if not node.inputs:
            return

        var = self._get_var_name(node)
        port_map = INPUT_PORT_NAME_MAP.get(node.ue_class, {})

        for input_idx, (input_name, input_node) in enumerate(node.inputs.items()):
            if input_node is None:
                continue

            src_var = self._get_var_name(input_node)

            # 确定源节点输出端口名
            src_output_name = ''
            # TextureObjectParameter 默认输出名为空字符串

            # MaterialFunctionCall 的输入通过索引连接
            # UE4 connect_material_expressions 对 MaterialFunctionCall 的 dst_input_name:
            #   - 优先用索引号字符串 "0", "1", "2" 
            #   - 如果失败，回退到 FunctionInput 名称 "Alpha (S)", "Min (S)" 等
            if node.ue_class == 'MaterialExpressionMaterialFunctionCall':
                # 构建备选名称列表：索引、带后缀名、不带后缀名
                alt_names = [f'"{input_name} (S)"', f'"{input_name}"']
                self._emit(f'    safe_connect(mat, {src_var}, "{src_output_name}", {var}, "{input_idx}", [{", ".join(alt_names)}])  # {input_node.display_name} → {node.display_name}.{input_name}(idx={input_idx})')
                continue

            # 获取 UE4 实际的输入端口属性名
            if input_name in port_map:
                ue_input_name = port_map[input_name]
            else:
                # 回退：尝试直接使用 input_name 或空字符串
                ue_input_name = input_name

            self._emit(f'    safe_connect(mat, {src_var}, "{src_output_name}", {var}, "{ue_input_name}")  # {input_node.display_name} → {node.display_name}.{input_name}')


# ═══════════════════════════════════════════════════════════
# 便捷接口
# ═══════════════════════════════════════════════════════════

def generate_ue4_script(graph: MaterialGraph,
                        material_name: str = 'M_Generated',
                        material_path: str = '/Game/Materials',
                        output_file: str = None) -> str:
    """
    生成 UE4 Editor Python 脚本

    参数:
        graph: 材质节点图
        material_name: 材质资产名
        material_path: 材质保存路径
        output_file: 输出文件路径（可选）
    返回:
        生成的 Python 脚本字符串
    """
    codegen = UE4CodeGen(graph)
    script = codegen.generate(material_name, material_path)

    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(script)

    return script


def generate_auto_input_ue4_code(
    hlsl_code: str,
    custom_node_var: str = 'custom_node',
    mat_var: str = 'mat',
) -> str:
    """
    生成自动创建输入节点并连线到 Custom Node 的 UE4 Python 代码

    参数:
        hlsl_code: HLSL 代码
        custom_node_var: Custom Node 的 Python 变量名
        mat_var: 材质的 Python 变量名

    返回:
        UE4 Python 脚本代码段
    """
    from auto_input_generator import generate_input_nodes_ue4_code
    return generate_input_nodes_ue4_code(
        hlsl_code, custom_node_var, mat_var,
    )
