#!/usr/bin/env python3
"""Phase 2-4 完整重构执行脚本

执行步骤:
1. 测试文件合并和精简
2. Candidate schema重构
3. 架构组件提取
4. 全面测试验证
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# 设置工作目录
REPO_ROOT = Path("/datafs/users/wujxy/agent-sci/GEPA-AI-researcher")
os.chdir(REPO_ROOT)

def run_tests():
    """运行所有测试"""
    print("=" * 80)
    print("运行测试验证")
    print("=" * 80)
    result = subprocess.run(
        ["python3", "-m", "pytest", "tests/", "-x", "-q"],
        capture_output=True,
        text=True
    )
    print(result.stdout)
    if result.returncode != 0:
        print("测试失败:", result.stderr)
        return False
    return True

def main():
    print("🚀 开始Phase 2-4重构执行")
    print(f"📁 工作目录: {REPO_ROOT}")

    # 验证当前状态
    print("\n📊 当前测试状态:")
    if run_tests():
        print("✅ 所有测试通过，可以开始重构")
    else:
        print("❌ 当前测试未通过，请先修复问题")
        sys.exit(1)

    print("\n🎯 重构准备完成，准备执行以下步骤:")
    print("1. 🧪 测试文件合并和精简")
    print("2. 📝 Candidate schema重构")
    print("3. 🏗️ 架构组件提取")
    print("4. ✅ 全面测试验证")

if __name__ == "__main__":
    main()