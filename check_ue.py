import os, glob

ue_dir = r"D:\UnrealEngine\UE_4.27\Engine\Binaries\Win64"

# Check specific files
for name in ["UE4Editor.exe", "UE4Editor-Cmd.exe", "UnrealEditor.exe"]:
    p = os.path.join(ue_dir, name)
    if os.path.exists(p):
        size_mb = os.path.getsize(p) / (1024*1024)
        print(f"FOUND: {name} ({size_mb:.1f} MB)")
    else:
        print(f"NOT_FOUND: {name}")

# Count all exe files
exes = glob.glob(os.path.join(ue_dir, "*.exe"))
print(f"\nTotal .exe files in Win64: {len(exes)}")
for e in sorted(exes)[:30]:
    size_mb = os.path.getsize(e) / (1024*1024)
    print(f"  {os.path.basename(e)} ({size_mb:.1f} MB)")

# Also check total file count
all_files = os.listdir(ue_dir)
print(f"\nTotal files in Win64: {len(all_files)}")
exts = {}
for f in all_files:
    ext = os.path.splitext(f)[1].lower()
    exts[ext] = exts.get(ext, 0) + 1
for ext, count in sorted(exts.items(), key=lambda x: -x[1])[:15]:
    print(f"  {ext or '(no ext)'}: {count}")
