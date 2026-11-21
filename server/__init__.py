"""白底工作台本地服务。"""

from pathlib import Path
import sys


PYLIBS = Path(__file__).resolve().parents[1] / 'pylibs'
sys.path.insert(0, str(PYLIBS))
