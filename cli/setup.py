from setuptools import setup

setup(
    name="vaultctl",
    version="0.1.0",
    py_modules=["vaultctl"],
    install_requires=[
        "typer==0.12.5",
        "httpx==0.27.2",
        "rich==13.8.1",
    ],
    entry_points={
        "console_scripts": [
            "vaultctl=vaultctl:app",
        ],
    },
)
