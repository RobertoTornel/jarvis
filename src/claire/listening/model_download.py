"""Prepare local Whisper and streaming-zipformer files before engine start."""

from __future__ import annotations

from claire.debug import debug_log


def prepare_mlx_model(repo: str) -> str:
    """Use the Hub cache and native byte progress, without its file-count bar."""
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    class FileCounter(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

    print("📥 Checking Whisper model files (first run may download a large model)...", flush=True)
    path = snapshot_download(repo_id=repo, tqdm_class=FileCounter)
    debug_log(f"Whisper model files available: {path}", "voice")
    print("🎤 Loading Whisper into memory and warming up speech recognition...", flush=True)
    return path


class ModelDownloadError(RuntimeError):
    """A model preparation failure with a stable recovery category."""

    def __init__(self, category: str, detail: str):
        self.category = category
        super().__init__(f"Whisper model download {category}: {detail}")


def _complete_faster_whisper_model(path: str) -> bool:
    """Require local weights, configuration and tokenisation before loading."""
    from pathlib import Path

    root = Path(path)
    required = ('model.bin', 'config.json', 'tokenizer.json')
    try:
        return (all((root / name).is_file() and (root / name).stat().st_size > 0
                    for name in required)
                and any(file.is_file() and file.stat().st_size > 0
                        for file in root.glob('vocabulary.*')))
    except OSError:
        return False


def _download_error_category(error: Exception) -> str:
    """Retain rate-limit status through the Hub's cache fallback exceptions."""
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(getattr(current, 'response', None), 'status_code', None) == 429:
            return 'rate_limit'
        current = current.__cause__ or current.__context__
    return 'download'


def _download_worker(connection, model_name: str) -> None:
    """Prepare Hub files in a spawn child without starting audio or the daemon."""
    try:
        from faster_whisper.utils import download_model

        path = download_model(model_name)
        if not _complete_faster_whisper_model(path):
            raise ModelDownloadError('incomplete_download', 'required model files are missing')
        connection.send(('ready', path, ''))
    except Exception as error:
        category = (error.category if isinstance(error, ModelDownloadError)
                    else _download_error_category(error))
        detail = 'HTTP 429' if category == 'rate_limit' else type(error).__name__
        connection.send(('error', category, detail))
    finally:
        connection.close()


def _run_download_worker(model_name: str, timeout_sec: float = 300) -> str:
    """Bound child execution and reap it before exposing its result."""
    import multiprocessing

    context = multiprocessing.get_context('spawn')
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_download_worker, args=(sender, model_name))
    started = False
    try:
        process.start()
        started = True
        sender.close()
        if not receiver.poll(timeout_sec):
            raise ModelDownloadError('timeout', 'download exceeded its time limit')
        try:
            result = receiver.recv()
        except EOFError:
            raise ModelDownloadError('worker_exit', 'download process exited without a result') from None
        process.join(timeout=1)
        if process.is_alive():
            raise ModelDownloadError('worker_exit', 'download process did not stop after its result')
        if process.exitcode != 0:
            raise ModelDownloadError('worker_exit', f'download process exited ({process.exitcode})')
        if not isinstance(result, tuple) or len(result) != 3:
            raise ModelDownloadError('worker_exit', 'invalid download result')
        status, value, detail = result
        if status == 'error':
            raise ModelDownloadError(value, detail)
        if status != 'ready' or not isinstance(value, str) or not _complete_faster_whisper_model(value):
            raise ModelDownloadError('incomplete', 'required model files are missing')
        return value
    except (OSError, RuntimeError) as error:
        if isinstance(error, ModelDownloadError):
            raise
        raise ModelDownloadError('worker_start', type(error).__name__) from error
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if not process.is_alive():
                process.close()


#: Official k2-fsa release archive for the always-on streaming zipformer. The
#: default ``streaming_stt_model_dir`` points at ~/.local/share/claire/models/
#: sherpa-onnx-streaming-zipformer-en-20M, so a fresh machine only has to call
#: :func:`ensure_sherpa_streaming_model` with that directory.
SHERPA_STREAMING_MODEL_NAME = 'sherpa-onnx-streaming-zipformer-en-20M-2023-02-17'
SHERPA_STREAMING_URL = (
    'https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
    f'{SHERPA_STREAMING_MODEL_NAME}.tar.bz2'
)
#: Recovery advice printed verbatim in the failure message, so nobody has to
#: read this file to fix a broken install.
SHERPA_STREAMING_FIX = (
    'fetch the .tar.bz2 from the URL above, extract it, and place tokens.txt plus '
    'the encoder/decoder/joiner .onnx files in the target directory'
)

#: Component prefixes every streaming zipformer bundle must supply.
_SHERPA_COMPONENTS = ('encoder', 'decoder', 'joiner')


class SherpaModelDownloadError(RuntimeError):
    """A streaming zipformer acquisition failure naming URL, target and fix."""

    def __init__(self, category: str, detail: str, *,
                 url: str = SHERPA_STREAMING_URL, target_dir: str | Path = '') -> None:
        self.category = category
        self.detail = detail
        self.url = url
        self.target_dir = str(target_dir)
        super().__init__(
            f'sherpa streaming model download {category}: {detail} '
            f'(url {url}; target directory {self.target_dir}). '
            f'To recover by hand: {SHERPA_STREAMING_FIX}.'
        )


def _sherpa_file_present(path: Path) -> bool:
    """A component counts only when it is a non-empty regular file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _looks_like_sherpa_bundle(path: Path) -> bool:
    """Require tokens.txt plus an encoder/decoder/joiner .onnx set."""
    if not path.is_dir():
        return False
    if not _sherpa_file_present(path / 'tokens.txt'):
        return False
    return all(
        any(_sherpa_file_present(candidate) for candidate in path.glob(f'{kind}*.onnx'))
        for kind in _SHERPA_COMPONENTS
    )


def _find_sherpa_bundle(root: Path) -> Path | None:
    """Return a complete bundle at, or one level below, root, else ``None``.

    Searching one level down keeps the result stable whether the caller points
    at the model directory itself or at its parent: the official archive unpacks
    into a named subdirectory, while a hand-copied install often holds the files
    directly.
    """
    try:
        children = sorted((item for item in root.iterdir() if item.is_dir()),
                          key=lambda item: item.name)
    except OSError:
        children = []
    for candidate in (root, *children):
        if _looks_like_sherpa_bundle(candidate):
            return candidate
    return None


def _report_sherpa_progress(received: int, total: int) -> None:
    """Native byte progress, matching the Whisper download's tone."""
    mebibyte = 1024 * 1024
    if total:
        print(f'   … {received / mebibyte:.1f}/{total / mebibyte:.1f} MiB '
              f'({received * 100 // total}%)', flush=True)
    else:
        print(f'   … {received / mebibyte:.1f} MiB', flush=True)


def _sherpa_download_category(error: BaseException) -> str:
    """Keep rate-limit status visible through urllib's exception wrapping."""
    if getattr(error, 'code', None) == 429:
        return 'rate_limit'
    return _download_error_category(error)


def _download_bundle(url: str, destination: Path, timeout_sec: float = 600.0) -> None:
    """Stream a release archive to destination with byte progress and a bound.

    The socket timeout bounds each read; ``timeout_sec`` bounds the whole
    transfer, so a server that trickles bytes forever cannot hang the listener.
    """
    import time
    import urllib.request

    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f'📥 Downloading the streaming zipformer bundle from {url} ...', flush=True)
    debug_log(f'Streaming zipformer bundle download started -> {destination}', 'voice')
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=30) as response, open(destination, 'wb') as sink:
        total = int(response.headers.get('Content-Length') or 0)
        received = 0
        last_report = started
        while True:
            if time.monotonic() - started > timeout_sec:
                raise SherpaModelDownloadError(
                    'timeout',
                    f'download exceeded its {timeout_sec:.0f}s time limit',
                    target_dir=destination.parent,
                )
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            sink.write(chunk)
            received += len(chunk)
            if time.monotonic() - last_report >= 5:
                last_report = time.monotonic()
                _report_sherpa_progress(received, total)
    if total and received != total:
        raise SherpaModelDownloadError(
            'truncated_download',
            f'received {received} of {total} bytes',
            target_dir=destination.parent,
        )


def _extract_bundle(archive: Path, destination: Path) -> None:
    """Extract a .tar.bz2 release archive, refusing unsafe member paths."""
    import tarfile

    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:bz2') as bundle:
        try:
            bundle.extractall(destination, filter='data')
        except TypeError:  # Python without the tar extraction filter
            bundle.extractall(destination)


def _publish_bundle(bundle: Path, root: Path) -> Path:
    """Move a verified bundle out of staging, replacing an incomplete namesake."""
    import shutil

    destination = root / SHERPA_STREAMING_MODEL_NAME
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.move(str(bundle), str(destination))
    return destination


def ensure_sherpa_streaming_model(target_dir: str | Path, timeout_sec: float = 600.0) -> Path:
    """Return a directory holding the streaming zipformer bundle.

    An intact local bundle is returned untouched and no network call is made.
    Otherwise the official k2-fsa archive is downloaded into a staging directory
    inside ``target_dir`` and extracted there; the bundle is published into
    ``target_dir`` only after every required file is present, then re-verified in
    place. A partial extraction therefore raises
    :class:`SherpaModelDownloadError` instead of being reported as success.
    """
    import shutil
    import tempfile
    from pathlib import Path

    cleaned = str(target_dir or '').strip()
    if not cleaned:
        raise SherpaModelDownloadError(
            'missing_target',
            'no target directory was given for the streaming zipformer bundle',
            target_dir='<empty target_dir>',
        )
    root = Path(cleaned).expanduser()

    existing = _find_sherpa_bundle(root)
    if existing is not None:
        debug_log(f'Streaming zipformer bundle already present: {existing}', 'voice')
        return existing

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.sherpa-download-', dir=root))
    try:
        try:
            archive = staging / f'{SHERPA_STREAMING_MODEL_NAME}.tar.bz2'
            _download_bundle(SHERPA_STREAMING_URL, archive, timeout_sec)
            unpacked = staging / 'unpacked'
            _extract_bundle(archive, unpacked)
        except SherpaModelDownloadError as error:
            # Rebuild against the caller's directory: internal seams may have
            # reported the staging path instead.
            raise SherpaModelDownloadError(
                error.category, error.detail, target_dir=root
            ) from error
        except Exception as error:
            raise SherpaModelDownloadError(
                _sherpa_download_category(error), type(error).__name__, target_dir=root
            ) from error

        bundle = _find_sherpa_bundle(unpacked)
        if bundle is None:
            raise SherpaModelDownloadError(
                'incomplete_download',
                'the archive was extracted but tokens.txt plus an '
                'encoder/decoder/joiner .onnx set are not all present',
                target_dir=root,
            )
        published = _publish_bundle(bundle, root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    verified = _find_sherpa_bundle(root)
    if verified is None or verified != published:
        raise SherpaModelDownloadError(
            'incomplete_download',
            'the published files did not verify as a complete bundle',
            target_dir=root,
        )
    debug_log(f'Streaming zipformer bundle prepared: {verified}', 'voice')
    print(f'✅ Streaming zipformer model ready at {verified}', flush=True)
    return verified


def prepare_faster_whisper_model(model_name: str) -> str:
    """Return complete local files, isolating every required network download."""
    from pathlib import Path
    from faster_whisper.utils import download_model

    if Path(model_name).is_dir():
        if _complete_faster_whisper_model(model_name):
            return model_name
        raise ModelDownloadError('incomplete', 'local model directory is missing required files')
    try:
        path = download_model(model_name, local_files_only=True)
        if _complete_faster_whisper_model(path):
            debug_log('Whisper model files available in local cache', 'voice')
            return path
    except Exception as error:
        debug_log(f'Whisper local cache unavailable: {type(error).__name__}', 'voice')
    print('  📥 Checking Whisper model files (first run may download a large model)...', flush=True)
    debug_log('Starting isolated Whisper model download', 'voice')
    try:
        path = _run_download_worker(model_name)
    except ModelDownloadError as error:
        debug_log(f'Whisper download failed: {error.category}', 'voice')
        raise
    debug_log('Whisper model files prepared by isolated download', 'voice')
    return path
