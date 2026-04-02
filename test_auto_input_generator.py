"""
============================================================
 test_auto_input_generator.py
 自动输入变量生成器的测试用例
============================================================
"""

import sys
import os
import unittest

# 确保模块可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auto_input_generator import (
    AutoInputGenerator, InputVarInfo,
    create_input_nodes_for_custom, generate_input_nodes_ue4_code,
    auto_create_inputs_for_graph,
)
from hlsl_parser import parse_hlsl
from node_mapper import MaterialGraph, hlsl_to_material_graph


class TestAutoInputExtraction(unittest.TestCase):
    """测试输入变量提取"""

    def test_basic_extraction(self):
        """基本变量提取：声明的变量不应成为输入"""
        code = """
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_names = {i.name for i in inputs}

        # viewDir, fresnel, result 是声明的局部变量，不应出现
        self.assertNotIn('viewDir', input_names)
        self.assertNotIn('fresnel', input_names)
        self.assertNotIn('result', input_names)

        # 这些是外部输入变量
        self.assertIn('FresnelPower', input_names)
        self.assertIn('BaseColor', input_names)
        self.assertIn('RimColor', input_names)

    def test_builtin_detection(self):
        """UE4 内置变量识别"""
        code = """
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
return fresnel;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        # CameraPosition, WorldPosition, Normal 是内置变量
        self.assertIn('CameraPosition', input_map)
        self.assertTrue(input_map['CameraPosition'].is_builtin)
        self.assertEqual(input_map['CameraPosition'].ue_class, 'MaterialExpressionCameraPositionWS')

        self.assertIn('WorldPosition', input_map)
        self.assertTrue(input_map['WorldPosition'].is_builtin)

        self.assertIn('Normal', input_map)
        self.assertTrue(input_map['Normal'].is_builtin)

        # FresnelPower 不是内置变量
        self.assertIn('FresnelPower', input_map)
        self.assertFalse(input_map['FresnelPower'].is_builtin)

    def test_texture_detection(self):
        """纹理参数识别"""
        code = """
float noise = tex2D(NoiseTex, UV).r;
float3 result = tex2D(MainTex, UV).rgb;
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        # NoiseTex 和 MainTex 是 tex2D 第一个参数，应被识别为纹理
        self.assertIn('NoiseTex', input_map)
        self.assertTrue(input_map['NoiseTex'].is_texture)
        self.assertEqual(input_map['NoiseTex'].param_type, 'texture')
        self.assertEqual(input_map['NoiseTex'].dimension, -1)

        self.assertIn('MainTex', input_map)
        self.assertTrue(input_map['MainTex'].is_texture)

    def test_texture_name_pattern(self):
        """通过名称模式识别纹理"""
        code = """
float3 color = DiffuseTexture;
return color;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        self.assertIn('DiffuseTexture', input_map)
        self.assertTrue(input_map['DiffuseTexture'].is_texture)


class TestTypeInference(unittest.TestCase):
    """测试类型推断"""

    def test_scalar_suffix(self):
        """标量后缀推断"""
        code = """
float rim = pow(1.0 - dot(Normal, ViewDir), RimPower);
float3 result = BaseColor * rim * RimIntensity;
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        # Power 和 Intensity 后缀应推断为标量
        self.assertEqual(input_map['RimPower'].param_type, 'scalar')
        self.assertEqual(input_map['RimPower'].dimension, 1)

        self.assertEqual(input_map['RimIntensity'].param_type, 'scalar')
        self.assertEqual(input_map['RimIntensity'].dimension, 1)

    def test_vector_pattern(self):
        """向量模式推断"""
        code = """
float3 result = lerp(BaseColor, RimColor, 0.5);
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        # Color 模式应推断为向量
        self.assertEqual(input_map['BaseColor'].param_type, 'vector')
        self.assertEqual(input_map['BaseColor'].dimension, 3)

        self.assertEqual(input_map['RimColor'].param_type, 'vector')
        self.assertEqual(input_map['RimColor'].dimension, 3)

    def test_dissolve_effect(self):
        """溶解效果的输入变量推断"""
        code = """
float noise = tex2D(NoiseTex, UV).r;
float edge = smoothstep(DissolveAmount - EdgeWidth, DissolveAmount, noise);
float edgeMask = smoothstep(DissolveAmount, DissolveAmount + EdgeWidth, noise);
float3 edgeColor = EdgeColor * (edge - edgeMask);
float3 result = BaseColor * edgeMask + edgeColor;
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_map = {i.name: i for i in inputs}

        # NoiseTex → texture
        self.assertTrue(input_map['NoiseTex'].is_texture)

        # UV → builtin
        self.assertTrue(input_map['UV'].is_builtin)

        # DissolveAmount → scalar (amount suffix)
        self.assertEqual(input_map['DissolveAmount'].param_type, 'scalar')

        # EdgeWidth → scalar (width suffix)
        self.assertEqual(input_map['EdgeWidth'].param_type, 'scalar')

        # EdgeColor → vector (color pattern)
        self.assertEqual(input_map['EdgeColor'].param_type, 'vector')

        # BaseColor → vector (color pattern)
        self.assertEqual(input_map['BaseColor'].param_type, 'vector')


class TestNodeCreation(unittest.TestCase):
    """测试节点创建和连线信息"""

    def test_create_input_nodes(self):
        """测试输入节点信息生成"""
        code = """
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
"""
        result = create_input_nodes_for_custom(code)

        self.assertIn('inputs', result)
        self.assertIn('nodes', result)
        self.assertIn('connections', result)
        self.assertIn('custom_node_inputs', result)

        # 应该有输入节点
        self.assertTrue(len(result['nodes']) > 0)

        # 连线数应与节点数匹配
        self.assertEqual(len(result['connections']), len(result['nodes']))

        # 检查自定义节点输入 Pin 名称
        self.assertTrue(len(result['custom_node_inputs']) > 0)

    def test_node_types(self):
        """测试节点类型正确性"""
        code = """
float3 result = lerp(BaseColor, RimColor, RimPower);
return result;
"""
        result = create_input_nodes_for_custom(code)
        node_map = {n['name']: n for n in result['nodes']}

        # BaseColor → VectorParameter
        self.assertEqual(node_map['BaseColor']['ue_class'], 'MaterialExpressionVectorParameter')

        # RimPower → ScalarParameter
        self.assertEqual(node_map['RimPower']['ue_class'], 'MaterialExpressionScalarParameter')

    def test_builtin_node_type(self):
        """测试内置变量节点类型"""
        code = """
float3 result = WorldPosition + Normal;
return result;
"""
        result = create_input_nodes_for_custom(code)
        node_map = {n['name']: n for n in result['nodes']}

        self.assertEqual(node_map['WorldPosition']['ue_class'], 'MaterialExpressionWorldPosition')
        self.assertEqual(node_map['Normal']['ue_class'], 'MaterialExpressionPixelNormalWS')

    def test_texture_node_type(self):
        """测试纹理节点类型"""
        code = """
float3 result = tex2D(DiffuseTex, UV).rgb;
return result;
"""
        result = create_input_nodes_for_custom(code)
        node_map = {n['name']: n for n in result['nodes']}

        self.assertEqual(node_map['DiffuseTex']['ue_class'], 'MaterialExpressionTextureObjectParameter')


class TestUE4CodeGeneration(unittest.TestCase):
    """测试 UE4 代码生成"""

    def test_generate_code(self):
        """测试生成的 UE4 Python 代码"""
        code = """
float3 result = lerp(BaseColor, RimColor, RimPower);
return result;
"""
        ue4_code = generate_input_nodes_ue4_code(code)

        # 应包含输入节点创建
        self.assertIn('自动创建输入节点', ue4_code)

        # 应包含参数创建代码
        self.assertIn('create_material_expression', ue4_code)

        # 应包含连线代码
        self.assertIn('safe_connect', ue4_code)

    def test_code_includes_pin_setup(self):
        """测试生成代码包含 Custom Node Pin 配置"""
        code = """
float fresnel = pow(1.0 - dot(Normal, ViewDir), FresnelPower);
return fresnel;
"""
        ue4_code = generate_input_nodes_ue4_code(code)

        # 应包含 Custom Node 输入 Pin 配置
        self.assertIn('custom_inputs', ue4_code)
        self.assertIn('input_name', ue4_code)


class TestMaterialGraphIntegration(unittest.TestCase):
    """测试与 MaterialGraph 的集成"""

    def test_auto_create_inputs_method(self):
        """测试 MaterialGraph.auto_create_inputs() 方法"""
        code = """
float3 result = lerp(BaseColor, RimColor, RimPower);
return result;
"""
        graph = hlsl_to_material_graph(code)
        original_node_count = len(graph.nodes)

        # 调用自动创建输入（可能已有部分节点）
        graph.auto_create_inputs(code)

        # 节点数应不少于原来
        self.assertGreaterEqual(len(graph.nodes), original_node_count)

    def test_auto_create_inputs_for_graph(self):
        """测试 auto_create_inputs_for_graph 函数"""
        code = """
float3 viewDir = normalize(CameraPosition - WorldPosition);
float rim = pow(1.0 - saturate(dot(Normal, viewDir)), RimPower);
return rim;
"""
        graph = MaterialGraph()
        auto_create_inputs_for_graph(graph, code)

        # 应创建了输入节点
        self.assertTrue(len(graph.input_nodes) > 0)

        # 检查特定节点存在
        input_names = set(graph.input_nodes.keys())
        self.assertIn('RimPower', input_names)

    def test_no_duplicate_nodes(self):
        """测试不会创建重复节点"""
        code = """
float3 result = lerp(BaseColor, RimColor, RimPower);
return result;
"""
        graph = hlsl_to_material_graph(code)

        # 记录已有的 input 节点
        existing_inputs = set(graph.input_nodes.keys())

        # 再次调用，不应创建重复
        graph.auto_create_inputs(code)

        # 每个名字只应出现一次
        for name in existing_inputs:
            count = sum(1 for n in graph.nodes
                        if n.display_name == name
                        and n.ue_class in (
                            'MaterialExpressionScalarParameter',
                            'MaterialExpressionVectorParameter',
                            'MaterialExpressionTextureObjectParameter',
                        ))
            self.assertLessEqual(count, 1, f"Duplicate node for {name}")


class TestEdgeCases(unittest.TestCase):
    """测试边界情况"""

    def test_empty_code(self):
        """空代码"""
        code = "return 0;"
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        # 空代码不应有外部输入
        self.assertEqual(len(inputs), 0)

    def test_all_declared(self):
        """所有变量都已声明"""
        code = """
float x = 1.0;
float y = 2.0;
float result = x + y;
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        # 所有变量都是局部的
        self.assertEqual(len(inputs), 0)

    def test_complex_expressions(self):
        """复杂表达式中的变量提取"""
        code = """
float2 distortion = tex2D(DistortionTex, UV + Time * 0.1).rg;
distortion = distortion * 2.0 - 1.0;
float2 distortedUV = UV + distortion * DistortionStrength;
float3 result = tex2D(MainTex, distortedUV).rgb;
return result;
"""
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(hlsl_code=code)
        input_names = {i.name for i in inputs}

        # 应识别出纹理和标量参数
        self.assertIn('DistortionTex', input_names)
        self.assertIn('MainTex', input_names)
        self.assertIn('DistortionStrength', input_names)

        # UV 和 Time 是内置变量
        self.assertIn('UV', input_names)
        self.assertIn('Time', input_names)

    def test_ast_input(self):
        """通过 AST 输入"""
        code = """
float3 result = BaseColor * Brightness;
return result;
"""
        ast = parse_hlsl(code)
        gen = AutoInputGenerator()
        inputs = gen.extract_inputs(ast=ast)
        input_names = {i.name for i in inputs}

        self.assertIn('BaseColor', input_names)
        self.assertIn('Brightness', input_names)


if __name__ == '__main__':
    unittest.main()
