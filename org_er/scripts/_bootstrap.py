"""脚本公共引导：把项目根目录加入 sys.path（兼容 python -I 运行）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
