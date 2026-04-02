import py_compile
import sys

files = ['web_server.py', 'ue4_executor.py', 'ue4_codegen.py', 'node_mapper.py', 'hlsl_parser.py']
ok = True
for f in files:
    try:
        py_compile.compile(f, doraise=True)
        print(f"  OK: {f}")
    except py_compile.PyCompileError as e:
        print(f"  FAIL: {f} -- {e}")
        ok = False

if ok:
    print("\nAll files compiled successfully!")
else:
    print("\nSome files have errors!")
    sys.exit(1)
