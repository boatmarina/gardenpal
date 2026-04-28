from setuptools import setup, find_packages

setup(
    name="gardenpal",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "gardenpal=gardenpal.cli:main",
            "gardenpal-web=gardenpal.web:run",
        ],
    },
    install_requires=[
        "Flask",
        "requests",
    ],
    python_requires=">=3.8",
)
