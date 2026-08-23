# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

hidden = []
for pkg in ("solders", "web3", "eth_account", "eth_abi", "eth_utils",
            "eth_keys", "eth_typing", "hexbytes", "nacl", "base58",
            "requests", "telegram", "certifi", "charset_normalizer"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        pass

hidden += [
    "sqlite3", "queue", "logging.handlers",
    "tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.messagebox",
    "execution", "safety_data", "venues", "evm_venue", "sniper_bot", "analyze",
]

datas = []
for pkg in ("certifi", "web3"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib", "pytest", "IPython", "notebook", "jupyter",
        "PIL", "numpy", "pandas", "scipy", "tornado", "zmq",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        "test", "unittest", "pydoc_data", "lib2to3",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TradingBot",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TradingBot")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TradingBot.app",
        icon=None,
        bundle_identifier="com.example.tradingbot",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleShortVersionString": "1.0.0",
        },
    )
