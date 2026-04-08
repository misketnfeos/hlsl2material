from t3d_generator import generate_t3d_from_custom_hlsl

hlsl_code = """
float2 uv = UV;
float rainAmount = saturate(FlowAmount);
return float3(rainAmount, 0, 0);
"""

t3d = generate_t3d_from_custom_hlsl(hlsl_code)

with open("test_output.t3d", "w", encoding="utf-8") as f:
    f.write(t3d)

print("Written to test_output.t3d")