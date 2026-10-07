# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for the Claire Desktop App.

Linux-only build: this fork targets Linux, so the Windows and macOS build
branches (and the Apple Silicon MLX speech collector they pulled in) have
been removed.
"""

from pathlib import Path

block_cipher = None

# Get the project root directory
project_root = Path('.').absolute()
src_path = project_root / 'src'

# Collect all necessary data files
# Note: Let PyInstaller's built-in hooks handle sounddevice, ctranslate2, and Qt WebEngine
# Manual collection can conflict with hooks and cause crashes
datas = [
    (str(src_path / 'desktop_app' / 'desktop_assets' / '*.png'), 'desktop_app/desktop_assets'),
]

# Collect Piper TTS data files (espeak-ng-data is required for phonemization)
try:
    import piper
    piper_path = Path(piper.__file__).parent
    # espeak-ng-data contains phoneme data needed for TTS
    espeak_data = piper_path / 'espeak-ng-data'
    if espeak_data.exists():
        datas.append((str(espeak_data), 'piper/espeak-ng-data'))
        print(f"Bundling Piper espeak-ng-data from {espeak_data}")
    # tashkeel contains Arabic diacritization data
    tashkeel_data = piper_path / 'tashkeel'
    if tashkeel_data.exists():
        datas.append((str(tashkeel_data), 'piper/tashkeel'))
        print(f"Bundling Piper tashkeel from {tashkeel_data}")
except ImportError:
    print("Warning: piper not installed, TTS may not work in bundle")

# Collect Qt plugins for system tray functionality
try:
    import PyQt6
    qt_path = Path(PyQt6.__file__).parent
    # Add Qt plugins for platform integration (needed for the system tray on Linux)
    # Only add directories that actually exist (e.g., 'styles' may not exist on Linux)
    qt_plugin_dirs = [
        ('platforms', 'PyQt6/Qt6/plugins/platforms'),
        ('styles', 'PyQt6/Qt6/plugins/styles'),
    ]
    for plugin_name, dest_path in qt_plugin_dirs:
        plugin_path = qt_path / 'Qt6' / 'plugins' / plugin_name
        if plugin_path.exists():
            datas.append((str(plugin_path), dest_path))
        else:
            print(f"Info: Qt plugin directory '{plugin_name}' not found, skipping")
except Exception as e:
    print(f"Warning: Could not collect Qt plugins: {e}")

# Note: Qt WebEngine resources are handled by PyInstaller's hook-PyQt6.QtWebEngineWidgets.py
# Manual collection can conflict with the hook and cause crashes

# Hidden imports that PyInstaller might miss
hiddenimports = [
    # Claire core modules
    'claire',
    'claire._version',
    'claire.daemon',
    'claire.config',
    'claire.debug',
    'claire.llm',
    'claire.main',
    # Desktop app modules
    'desktop_app',
    'desktop_app.app',
    'desktop_app.splash_screen',
    'desktop_app.setup_wizard',
    'desktop_app.updater',
    'desktop_app.update_dialog',
    'desktop_app.themes',
    'desktop_app.face_widget',
    'desktop_app.diary_dialog',
    'desktop_app.memory_viewer',
    # Listening modules
    'claire.listening',
    'claire.listening.echo_detection',
    'claire.listening.listener',
    'claire.listening.state_manager',
    'claire.listening.wake_detection',
    'claire.listening.transcript_buffer',
    'claire.listening.intent_judge',
    # Memory modules
    'claire.memory',
    'claire.memory.conversation',
    'claire.memory.db',
    # Output modules
    'claire.output',
    'claire.output.tts',
    'claire.output.tune_player',
    # Piper TTS (local neural TTS)
    'piper',
    'piper.voice',
    'piper.config',
    'piper.download',
    'piper.download_voices',
    'piper.phonemize_espeak',
    'piper.phoneme_ids',
    # ONNX Runtime (required by Piper for model inference)
    'onnxruntime',
    'onnxruntime.capi',
    'onnxruntime.capi._pybind_state',
    # Reply modules
    'claire.reply',
    'claire.reply.engine',
    'claire.reply.enrichment',
    # Tools modules
    'claire.tools',
    'claire.tools.base',
    'claire.tools.registry',
    'claire.tools.types',
    'claire.tools.builtin',
    'claire.tools.builtin.fetch_web_page',
    'claire.tools.builtin.local_files',
    'claire.tools.builtin.nutrition',
    'claire.tools.builtin.nutrition.delete_meal',
    'claire.tools.builtin.nutrition.fetch_meals',
    'claire.tools.builtin.nutrition.log_meal',
    'claire.tools.builtin.refresh_mcp_tools',
    'claire.tools.builtin.screenshot',
    'claire.tools.builtin.web_search',
    'claire.tools.external',
    'claire.tools.external.mcp_client',
    # Utils modules
    'claire.utils',
    'claire.utils.fast_vector_store',
    'claire.utils.fuzzy_search',
    'claire.utils.location',
    'claire.utils.redact',
    'claire.utils.vector_store',
    # PyQt6
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.sip',
    # PyQt6 WebEngine (for embedded memory viewer)
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebChannel',
    # Audio dependencies (critical for voice input)
    'sounddevice',
    '_sounddevice_data',
    '_sounddevice_data.portaudio-binaries',
    'webrtcvad',
    # Speech recognition (faster-whisper backend)
    'faster_whisper',
    'ctranslate2',
    'huggingface_hub',
    'huggingface_hub.file_download',
    'huggingface_hub.hf_api',
    'huggingface_hub.utils',
    'tokenizers',
    # Third-party dependencies
    'dotenv',
    'psutil',
    'requests',
    'numpy',
    'PIL',
    'PIL.Image',
    'rapidfuzz',
    'rapidfuzz.fuzz',
    'bs4',
    'lxml',
    'html2text',
    'faiss',
    'sqlite3',
    'json',
    'asyncio',
    'threading',
    'subprocess',
    'geoip2',
    'geoip2.database',
    'miniupnpc',
    # Linux resolves IANA zones from /usr/share/zoneinfo at runtime.
    'zoneinfo',
    # Flask for memory viewer
    'flask',
    'flask.json',
    'werkzeug',
    'werkzeug.serving',
    'werkzeug.routing',
    'werkzeug.utils',
    'werkzeug.datastructures',
    'werkzeug.wrappers',
    'werkzeug.exceptions',
    'jinja2',
    'markupsafe',
    'itsdangerous',
    'click',
    'blinker',
]

# Heavy packages kept out of the Linux bundle.
_excludes = [
    # Exclude heavy packages to keep bundle size reasonable
    'psycopg2',  # Not used and causes OpenSSL conflicts
    'torch',  # PyTorch is 1.5-2GB - chatterbox TTS is optional
    'torchaudio',
    'torchvision',
    'chatterbox',  # Optional TTS engine (uses PyTorch)
    'transformers',  # Heavy ML library (not needed, faster_whisper uses ctranslate2)
    'safetensors',
    'accelerate',
    'cv2',  # OpenCV - not needed for core functionality
    'opencv-python',
    'matplotlib',  # Not needed for core app
    'notebook',
    'jupyter',
    'IPython',
    'sklearn',
    'scikit-learn',
    'scipy',  # was only needed by the removed Apple Silicon speech backend
    # Note: Keep huggingface_hub - needed by faster_whisper for model downloads
]

a = Analysis(
    ['src/desktop_app/app.py'],
    pathex=[str(src_path)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['src/desktop_app/rthook_onnxruntime.py'],
    excludes=_excludes,
    cipher=block_cipher,
    noarchive=False,
)

# Filter out heavy binaries to reduce the Linux bundle size
# Note: Be careful not to exclude libs needed by numpy/faster-whisper
excluded_binary_patterns = [
    'torch', 'libtorch', 'libcaffe2',  # PyTorch (~1.5GB)
    'torchaudio', 'torchvision',
    'cv2', 'opencv', 'libopencv',  # OpenCV (~500MB)
    'sklearn', 'scikit',  # scikit-learn
    'transformers',  # Heavy ML library
    'chatterbox',
    'matplotlib',
    # Note: Keep huggingface_hub (needed by faster_whisper for model downloads)
    # Note: Keep libopenblas (needed by numpy) and libfreetype (needed by av/ffmpeg)
]

filtered_binaries = []
for binary in a.binaries:
    name = binary[0].lower()
    binary_path = str(binary[1]).lower() if len(binary) > 1 else ''

    # Pattern-based exclusions (heavy libraries)
    should_exclude = False
    for pattern in excluded_binary_patterns:
        if pattern in name or pattern in binary_path:
            print(f"Excluding heavy binary: {binary[0]}")
            should_exclude = True
            break

    if not should_exclude:
        filtered_binaries.append(binary)

a.binaries = filtered_binaries

# Linux: one console-less windowed bundle directory at dist/Claire/.
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Claire',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Claire',
)
