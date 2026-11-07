"""图片页前端（static/console/console-images.js）的回归检查：真正的检查写在 tests/js/console_images.test.js，
用 Node 自带的 node:test 和一个很小的假 DOM，不装依赖；这里只是让它跟后端测试一起跑（CI 机器自带 node）。
没有 node 的机器跳过。"""

from pathlib import Path
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
TESTS = Path(__file__).resolve().parent / "js"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_image_page_star_reads_and_selection_cap():
    result = subprocess.run([NODE, "--test", str(TESTS / "console_images.test.js")],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
