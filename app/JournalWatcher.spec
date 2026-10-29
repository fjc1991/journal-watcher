# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for Journal Watcher.app (run by build_app.sh; not meant to be called by hand).
import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SKILL = os.path.join(ROOT, ".agents", "skills", "ais-journal-watcher")
SCRIPTS = os.path.join(SKILL, "scripts")
DASH = os.path.join(ROOT, "dashboard")
VERSION = open(os.path.join(SPECPATH, "VERSION"), encoding="utf-8").read().strip()
SIGN = os.environ.get("JW_SIGN_IDENTITY") or None                # a "Developer ID Application: …" identity signs for real (build_app.sh); without it the app is signed ad hoc afterwards
DEFAULTS = os.path.join(SPECPATH, "build", "defaults")        # made by build_app.sh: the settings a new data home starts from

# every module of the project except the tests
mods = sorted(os.path.splitext(f)[0] for f in os.listdir(SCRIPTS) if f.endswith(".py") and not f.startswith(("test_", "real_smoke")))
hidden = mods + ["server", "Quartz", "AppKit", "nacl", "nacl.signing", "nacl.exceptions", "nacl.bindings", "nacl._sodium", "_cffi_backend"]      # nacl: the update feed's signature (scripts/updater.py imports it only when it checks one); _cffi_backend: the compiled module that PyNaCl's libsodium binding imports from C, which PyInstaller cannot see (without it the app says "No module named '_cffi_backend'" and cannot check a signature: the first release dry run found it)
datas, binaries = [], []
for pkg in ("playwright", "fastembed", "onnxruntime", "docx", "pymupdf", "certifi", "anthropic", "tokenizers", "huggingface_hub"):
    try:
        d, b, h = collect_all(pkg)
    except Exception as e:                                    # a package that is not installed is simply left out
        print("collect_all(%s) skipped: %s" % (pkg, e))
        continue
    datas += d; binaries += b; hidden += h

datas += [(os.path.join(DASH, "index.html"), "dashboard"), (os.path.join(DASH, "static"), os.path.join("dashboard", "static")), (DEFAULTS, "defaults")]

a = Analysis(
    [os.path.join(SPECPATH, "jw_app.py")],
    pathex=[SCRIPTS, DASH],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "jupyter", "notebook", "pytest", "sphinx", "pandas", "scipy", "PIL.ImageTk"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Journal Watcher", console=False, disable_windowed_traceback=False, argv_emulation=False, target_arch=None, codesign_identity=SIGN, entitlements_file=(os.path.join(SPECPATH, "entitlements.plist") if SIGN else None))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Journal Watcher")
app = BUNDLE(
    coll,
    name="Journal Watcher.app",
    icon=os.path.join(SPECPATH, "build", "JournalWatcher.icns"),
    codesign_identity=SIGN,
    entitlements_file=(os.path.join(SPECPATH, "entitlements.plist") if SIGN else None),
    bundle_identifier="local.journalwatcher.app",
    info_plist={
        "CFBundleName": "Journal Watcher",
        "CFBundleDisplayName": "Journal Watcher",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSMicrophoneUsageDescription": "Journal Watcher can read your papers aloud and take dictated notes.",
        "NSHumanReadableCopyright": "Your own copy of Journal Watcher.",
    },
)
