#!/bin/bash
# 录播合并脚本自检：语法、续行完整性、关键函数、定义顺序（仅检查顶层调用）
# 用法: ./check_rec_merge.sh [/path/to/rec_merge_unified.sh]
# 退出码: 0=通过, 1=失败

TARGET="${1:-/vol1/1000/biliup/data/rec/rec_merge_unified.sh}"
FAIL=0

echo "=== 自检: $TARGET ==="

[ -f "$TARGET" ] || { echo "❌ 文件不存在"; exit 1; }

# 1. 语法
if bash -n "$TARGET" 2>/dev/null; then
    echo "✅ 语法检查通过"
else
    echo "❌ 语法检查失败:"
    bash -n "$TARGET" 2>&1 | head -5
    FAIL=1
fi

# 2. 续行完整性 + 3. 顶层定义顺序
python3 - "$TARGET" <<'PYEOF'
import re
import sys

path = sys.argv[1]
with open(path, 'rb') as f:
    raw = f.read()
lines = raw.split(b'\n')
text_lines = [ln.decode('utf-8', 'replace') for ln in lines]

fail = 0

# --- 续行完整性 ---
problems = []
for i in range(len(lines) - 1):
    cur = lines[i].rstrip(b'\r')
    nxt = lines[i + 1].rstrip(b'\r')
    if not cur.endswith(b'\\'):
        continue
    st = nxt.strip()
    if not st:
        problems.append((i + 1, 'EMPTY'))
    elif st.startswith(b'#'):
        problems.append((i + 1, 'COMMENT'))

if problems:
    print(f"❌ 续行断裂 {len(problems)} 处:")
    for n, why in problems[:10]:
        print(f"   line {n}: next is {why}")
    fail = 1
else:
    print("✅ 续行完整性通过")

# --- 收集函数定义行号 ---
defs = {}
for i, ln in enumerate(text_lines):
    m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{', ln)
    if m:
        defs[m.group(1)] = i + 1

# --- 判断每个行是否处于函数体内 ---
def function_ranges():
    """返回 [(start_line, end_line)] 1-based，函数体范围"""
    ranges = []
    i = 0
    n = len(text_lines)
    while i < n:
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{', text_lines[i])
        if m:
            start = i + 1
            depth = 1
            j = i
            # 从定义行开始统计大括号深度
            while j < n:
                depth += text_lines[j].count('{') - text_lines[j].count('}')
                if j > i and depth <= 0:
                    break
                j += 1
            ranges.append((start, j + 1))
            i = j + 1
        else:
            i += 1
    return ranges


ranges = function_ranges()


def in_function(line_no):
    for a, b in ranges:
        if a <= line_no <= b:
            return True
    return False


# --- 顶层调用检查（只看函数体外的调用）---
bad = []
for name, defline in defs.items():
    # 前缀排除 . 和 / ，避免把文件名里 xx.log 误判成函数调用 log
    pat = re.compile(r'(?<![\w$%./-])' + re.escape(name) + r'(?=[\s"\']|$)')
    for i, ln in enumerate(text_lines):
        lineno = i + 1
        if lineno == defline:
            continue
        s = ln.lstrip()
        if s.startswith('#'):
            continue
        # 变量赋值、数组等不算调用
        if re.match(r'^\s*[A-Za-z_][A-Za-z0-9_]*=', ln):
            continue
        if not pat.search(ln):
            continue
        if in_function(lineno):
            continue  # 函数体内调用没问题（运行时已定义）
        if lineno < defline:
            bad.append((name, lineno, defline))
            break

if bad:
    print("❌ 顶层代码在函数定义前调用（会 command not found）:")
    for name, call, d in bad[:8]:
        print(f"   {name}: 调用 line {call} < 定义 line {d}")
    fail = 1
else:
    print("✅ 顶层定义顺序正确")

sys.exit(fail)
PYEOF
[ $? -ne 0 ] && FAIL=1

# 4. 关键函数存在
for fn in merge_group normalize_video safe_move to_ts reencode_fix; do
    if grep -qE "^${fn}\(\)" "$TARGET"; then
        echo "✅ 函数 $fn 存在"
    else
        echo "❌ 函数 $fn 缺失"
        FAIL=1
    fi
done

echo "=== 结果 ==="
if [ "$FAIL" -eq 0 ]; then
    echo "✅ 全部通过"
else
    echo "❌ 存在问题，请修复后再部署"
fi
exit "$FAIL"
