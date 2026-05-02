from setuptools import setup

setup(
    name="runtimekit-cli",
    version="0.2.0",
    description="Generic CLI for scaffolding and managing modular runtime applications.",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    packages=["runtimekit_cli"],
    python_requires=">=3.9",
    entry_points={
        "console_scripts": [
            "runtimekit=runtimekit_cli.cli:main",
        ],
    },
)
