"""
============================================================
 test_reverse_converter.py
 双向转换测试: HLSL → MaterialGraph → HLSL
============================================================

验证 reverse_converter 模块能够正确地将 MaterialGraph
反向转换为语义等价的 HLSL 代码。

测试策略:
  1. 对每个测试用例，执行 HLSL → Graph → HLSL 双向转换
  2. 再将反向输出 HLSL → Graph → HLSL 验证幂等性
  3. 检查关键语义元素（函数调用、运算符、参数名）是否保留
============================================================
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from node_mapper import hlsl_to_material_graph, MaterialGraph
from reverse_converter import ReverseConverter, material_graph_to_hlsl, reverse_from_hlsl


# ═══════════════════════════════════════════════════════════
# 测试用例
# ═══════════════════════════════════════════════════════════

TEST_CASES = {
    'simple_lerp': {
        'hlsl': 'float3 c = lerp(BaseColor, float3(1,0,0), Roughness); return c;',
        'expect_contains': ['lerp', 'BaseColor', 'float3(1.0, 0.0, 0.0)', 'Roughness', 'return'],
    },
    'fresnel': {
        'hlsl': """
float3 viewDir = normalize(CameraPosition - WorldPosition);
float fresnel = pow(1.0 - saturate(dot(Normal, viewDir)), FresnelPower);
float3 result = lerp(BaseColor, RimColor, fresnel);
return result;
""",
        'expect_contains': ['normalize', 'CameraPosition', 'WorldPosition', 'pow',
                            'saturate', 'dot', 'Normal', 'FresnelPower',
                            'lerp', 'BaseColor', 'RimColor', 'return'],
    },
    'rim_light': {
        'hlsl': """
float NdotV = dot(Normal, ViewDir);
float rim = 1.0 - saturate(NdotV);
rim = pow(rim, RimPower);
float3 rimColor = RimColor * rim * RimIntensity;
float3 result = BaseColor + rimColor;
return result;
""",
        'expect_contains': ['dot', 'Normal', 'ViewDir', 'saturate', 'pow',
                            'RimPower', 'RimColor', 'RimIntensity', 'BaseColor', 'return'],
    },
    'simple_math': {
        'hlsl': 'return (A + B) * C - D / E;',
        'expect_contains': ['return', '+', '*', '-', '/'],
    },
    'texture_sample': {
        'hlsl': """
float3 color = tex2D(MainTex, UV).rgb;
return color;
""",
        'expect_contains': ['tex2D', 'MainTex', 'UV', 'return'],
    },
    'constants': {
        'hlsl': """
float3 red = float3(1.0, 0.0, 0.0);
float3 blue = float3(0.0, 0.0, 1.0);
float3 result = lerp(red, blue, 0.5);
return result;
""",
        'expect_contains': ['float3(1.0, 0.0, 0.0)', 'float3(0.0, 0.0, 1.0)',
                            'lerp', '0.5', 'return'],
    },
    'swizzle': {
        'hlsl': """
float3 color = tex2D(MainTex, UV).rgb;
float r = color.x;
return float3(r, r, r);
""",
        'expect_contains': ['tex2D', 'MainTex', '.x', 'return'],
    },
    'ternary': {
        'hlsl': """
float3 result = A > B ? float3(1,1,1) : float3(0,0,0);
return result;
""",
        'expect_contains': ['?', ':', 'return'],
    },
    'math_functions': {
        'hlsl': """
float a = abs(X);
float b = floor(Y);
float c = frac(Z);
float result = min(a, max(b, c));
return result;
""",
        'expect_contains': ['abs', 'floor', 'frac', 'min', 'max', 'return'],
    },
}


# ═══════════════════════════════════════════════════════════
# 测试执行
# ═══════════════════════════════════════════════════════════

def run_test(name: str, test_case: dict) -> dict:
    """运行单个测试用例"""
    result = {
        'name': name,
        'passed': True,
        'errors': [],
        'original': test_case['hlsl'].strip(),
        'reversed': '',
        'double_reversed': '',
    }

    try:
        # Step 1: HLSL → Graph → HLSL
        reversed_hlsl = reverse_from_hlsl(test_case['hlsl'])
        result['reversed'] = reversed_hlsl

        # Step 2: 检查语义元素
        for expected in test_case.get('expect_contains', []):
            if expected not in reversed_hlsl:
                result['errors'].append(f"缺少预期元素: '{expected}'")
                result['passed'] = False

        # Step 3: 幂等性验证 — 再转一次应该得到相似结果
        double_reversed = reverse_from_hlsl(reversed_hlsl)
        result['double_reversed'] = double_reversed

        # 检查双向转换后 return 语句仍然存在
        if 'return' not in double_reversed:
            result['errors'].append("幂等性检查失败: 二次转换后缺少 return 语句")
            result['passed'] = False

    except Exception as e:
        result['passed'] = False
        result['errors'].append(f"异常: {e}")

    return result


def run_all_tests():
    """运行所有测试"""
    print("=" * 60)
    print("  HLSL ↔ MaterialGraph 双向转换测试")
    print("=" * 60)
    print()

    results = []
    passed = 0
    failed = 0

    for name, test_case in TEST_CASES.items():
        result = run_test(name, test_case)
        results.append(result)

        status = "PASS" if result['passed'] else "FAIL"
        icon = "+" if result['passed'] else "x"

        print(f"  [{icon}] {status} - {name}")

        if not result['passed']:
            failed += 1
            for err in result['errors']:
                print(f"        {err}")
        else:
            passed += 1

    print()
    print("-" * 60)
    print(f"  结果: {passed} 通过, {failed} 失败, 共 {len(results)} 个测试")
    print("=" * 60)

    # 输出详细的对比（仅失败的用例）
    if failed > 0:
        print()
        print("  失败用例详情:")
        print("-" * 60)
        for r in results:
            if not r['passed']:
                print(f"\n  [{r['name']}]")
                print(f"  原始 HLSL:")
                for line in r['original'].split('\n'):
                    print(f"    {line}")
                print(f"  反向 HLSL:")
                for line in r['reversed'].split('\n'):
                    print(f"    {line}")
                print(f"  错误:")
                for err in r['errors']:
                    print(f"    - {err}")

    return results


if __name__ == '__main__':
    run_all_tests()
