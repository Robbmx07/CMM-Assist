# PyInstaller spec for a standalone, single-file CMM-Gen CLI executable.
#
# Packages the CLI only (parse-cad / extract-gdt / generate) -- the
# Streamlit visualizer is deliberately excluded. Streamlit doesn't bundle
# reliably into a single-file PyInstaller build (it depends on its own
# static assets and a subprocess-based server model), so `cmm-gen.exe
# visualize` prints a message pointing back to the source install instead
# of trying and silently failing.
#
# Build (run from the REPO ROOT, not this directory, on the target OS --
# see .github/workflows/build-windows-exe.yml for the Windows CI build):
#   pip install -r requirements.txt pyinstaller
#   pyinstaller packaging/cmm-gen.spec
# Output: dist/cmm-gen(.exe)
#
# cadquery/OCP wrap compiled OpenCascade libraries and don't get fully
# picked up by PyInstaller's default static import analysis, so this spec
# explicitly collect_all()s the packages known to need it (native
# extensions, data files) rather than relying on hidden-import guessing.

import os

from PyInstaller.utils.hooks import collect_all

# SPECPATH is injected by PyInstaller into the spec file's exec globals as
# the absolute directory containing this spec file -- used instead of "."
# or ".." so the build works regardless of the CWD it's invoked from.
REPO_ROOT = os.path.dirname(SPECPATH)  # noqa: F821

datas = [(os.path.join(REPO_ROOT, "config"), "config")]
binaries = []
hiddenimports = [
    "cmm_gen.cad_parser",
    "cmm_gen.gdt_extractor",
    "cmm_gen.kinematics_validator",
    "cmm_gen.pcdmis_generator",
    "cmm_gen.logging_config",
    "cmm_gen.models",
]

for pkg in (
    "OCP",
    "cadquery",
    "anthropic",
    "yaml",
    "PIL",
    "pdf2image",
    "rich",
    "typer",
    "loguru",
    "pydantic",
):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    [os.path.join(REPO_ROOT, "cmm_gen", "cli.py")],
    pathex=[REPO_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["streamlit", "plotly", "cmm_gen.visualizer"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="cmm-gen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
