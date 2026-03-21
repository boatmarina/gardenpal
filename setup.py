from setuptools import setup, find_packages

setup(
    name="gardenpal",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "console_scripts": [
            "gardenpal=gardenpal.cli:main",
        ],
    },
    python_requires=">=3.8",
)
