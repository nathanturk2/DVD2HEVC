"""Build the same complete runtime resources used by source checkouts."""
from pathlib import Path
import shutil
import os
from wheel.bdist_wheel import bdist_wheel as _bdist_wheel
from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

PACKAGE = 'dvd2hevc_app'
class build_py(_build_py):
    def run(self):
        super().run()
        root = Path(__file__).parent
        destination = Path(self.build_lib) / PACKAGE / "resources"
        directories = ['assets', 'docs', 'tools', 'native', 'patches']
        for directory in directories:
            source = root / directory
            if source.is_dir():
                shutil.copytree(source, destination / directory, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "target", "*.pyc", "*.obj", "*.log", "*.exe", "*.dll", "*.zip"))
        for name in ['README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'dvd2hevc.py']:
            source = root / name
            if source.is_file():
                destination.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination / name)
        if os.name == 'nt':
            binary=root/'tools/hadris-udf/bin/hadris-udf.exe'
            if binary.is_file():
                target=destination/'tools/hadris-udf/bin/hadris-udf.exe'
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(binary,target)
class bdist_wheel(_bdist_wheel):
    def get_tag(self):
        if os.name == 'nt': return 'py3','none','win_amd64'
        return super().get_tag()
setup(cmdclass={"build_py": build_py, "bdist_wheel": bdist_wheel})
