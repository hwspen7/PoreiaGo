"""Bundle application resources for editable builds and installable wheels."""
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py

PROJECT_ROOT = Path(__file__).resolve().parent


class BuildWithResources(build_py):
    def run(self):
        super().run()
        destination = Path(self.build_lib) / "poreiago_resources"
        destination.mkdir(parents=True, exist_ok=True)
        for directory in ("config", "benchmarks", "plugins"):
            source = PROJECT_ROOT / directory
            if source.is_dir():
                shutil.copytree(source, destination / directory, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
        shutil.copy2(PROJECT_ROOT / "pyproject.toml", destination / "pyproject.toml")
        # No frontend build is shipped unless its dist folder already exists.
        # The frontend can also be served independently by Vite/a static host.
        frontend_dist = PROJECT_ROOT / "web" / "dist"
        if frontend_dist.is_dir():
            shutil.copytree(frontend_dist, destination / "web" / "dist", dirs_exist_ok=True)


setup(cmdclass={"build_py": BuildWithResources})
