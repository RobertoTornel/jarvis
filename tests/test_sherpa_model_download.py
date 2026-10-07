"""Claire can acquire the streaming zipformer bundle on a fresh machine.

Policy under test:

* a complete local bundle is returned untouched and never touches the network;
* a missing/corrupt bundle triggers exactly one download of the official
  k2-fsa release for ``sherpa-onnx-streaming-zipformer-en-20M-2023-02-17``;
* any failure raises a *named* error carrying the URL, the target directory and
  the by-hand fix — never a silent empty success;
* a partial extraction is detected as failure, so a half-unpacked archive can
  never be mistaken for a usable model.

The download seam is always monkeypatched: no test here opens a socket. The
single ``integration`` test reads the real bundle already installed on this
machine and only asserts the fast path.
"""

import io
import tarfile
import urllib.request
from pathlib import Path

import pytest

from claire.listening import model_download

REAL_SHERPA_BUNDLE = Path(
    '/home/mana/.local/share/claire/models/sherpa-onnx-streaming-zipformer-en-20M'
)
needs_real_bundle = pytest.mark.skipif(
    not (REAL_SHERPA_BUNDLE / 'tokens.txt').is_file(),
    reason=f'real streaming bundle absent at {REAL_SHERPA_BUNDLE}',
)

BUNDLE_NAME = model_download.SHERPA_STREAMING_MODEL_NAME
EXPECTED_URL = model_download.SHERPA_STREAMING_URL


# ---------------------------------------------------------------------------
# Fixtures and helpers (all local: builds files, never a socket)
# ---------------------------------------------------------------------------


def _complete_files():
    """The smallest file set a streaming zipformer install must supply."""
    return {
        'tokens.txt': b'<blk> 0\n<unk> 1\n',
        'encoder-epoch-99-avg-1.onnx': b'encoder-bytes',
        'decoder-epoch-99-avg-1.int8.onnx': b'decoder-bytes',
        'joiner-epoch-99-avg-1.onnx': b'joiner-bytes',
    }


def _write_bundle(root, files):
    root.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (root / name).write_bytes(payload)
    return root


def _make_bundle(root):
    return _write_bundle(root, _complete_files())


def _forbidden_downloader(calls):
    """A downloader that must never run; records the attempt if it does."""

    def download(url, destination, timeout_sec=600.0):
        calls.append((url, destination))
        raise AssertionError('the downloader must not be called for a local bundle')

    return download


class _FakeResponse:
    """Context manager shaped like the urllib response the downloader reads."""

    def __init__(self, payload, declared_total=None):
        self._payload = payload
        self._offset = 0
        if declared_total is None:
            self.headers = {'Content-Length': str(len(payload))}
        else:
            self.headers = {'Content-Length': str(declared_total)}

    def read(self, size=-1):
        chunk = self._payload[self._offset:self._offset + size] if size > 0 else self._payload
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _EndlessResponse(_FakeResponse):
    """A server that trickles bytes forever, to exercise the total timeout."""

    def read(self, size=-1):
        return b'x' * 1024


def _install_urlopen(monkeypatch, response, seen=None):
    """Patch ``urllib.request.urlopen`` with an in-memory stand-in."""

    def fake_urlopen(url, timeout=None):
        if seen is not None:
            seen['url'] = url
            seen['timeout'] = timeout
        return response

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)


def _archive_bytes(bundle_name, files, prefix=''):
    """Build a real .tar.bz2 in memory, mirroring the release archive layout."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:bz2') as tar:
        for name, payload in files.items():
            member = f'{prefix}{bundle_name}/{name}' if bundle_name else name
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _install_download(monkeypatch, calls):
    """Stub the two seams so the download path runs without a socket."""

    def download(url, destination, timeout_sec=600.0):
        calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'fake-archive')

    def extract(archive, destination):
        _make_bundle(destination / BUNDLE_NAME)

    monkeypatch.setattr(model_download, '_download_bundle', download)
    monkeypatch.setattr(model_download, '_extract_bundle', extract)


# ---------------------------------------------------------------------------
# Fast path: a complete bundle is returned without network
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_complete_bundle_is_returned_without_network(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path / 'models')
    calls = []
    monkeypatch.setattr(model_download, '_download_bundle', _forbidden_downloader(calls))

    assert model_download.ensure_sherpa_streaming_model(bundle) == bundle
    assert calls == []


@pytest.mark.unit
def test_extracted_subdirectory_bundle_is_reused_without_network(tmp_path, monkeypatch):
    """The official archive unpacks into a named subdirectory; re-runs must not re-download."""
    models = _make_bundle(tmp_path / 'models' / BUNDLE_NAME)
    calls = []
    monkeypatch.setattr(model_download, '_download_bundle', _forbidden_downloader(calls))

    assert model_download.ensure_sherpa_streaming_model(models.parent) == models
    assert calls == []


@pytest.mark.unit
def test_expanduser_path_is_resolved(tmp_path, monkeypatch):
    bundle = _make_bundle(tmp_path / 'models')
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(model_download, '_download_bundle',
                        _forbidden_downloader([]))

    assert model_download.ensure_sherpa_streaming_model('~/models') == bundle


# ---------------------------------------------------------------------------
# Missing or corrupt bundle: the download path runs once
# ---------------------------------------------------------------------------


def _corrupt_layout(root, variant):
    if variant == 'absent':
        return
    if variant == 'tokens_only':
        root.mkdir(parents=True, exist_ok=True)
        (root / 'tokens.txt').write_bytes(b'tokens')
    elif variant == 'onnx_only':
        root.mkdir(parents=True, exist_ok=True)
        for name in ('encoder-a.onnx', 'decoder-a.onnx', 'joiner-a.onnx'):
            (root / name).write_bytes(b'x')
    elif variant == 'zero_byte_tokens':
        _make_bundle(root)
        (root / 'tokens.txt').write_bytes(b'')
    elif variant == 'incomplete_subdir':
        _write_bundle(root / BUNDLE_NAME, {'tokens.txt': b'tokens'})


@pytest.mark.unit
@pytest.mark.parametrize('variant', [
    'absent', 'tokens_only', 'onnx_only', 'zero_byte_tokens', 'incomplete_subdir',
])
def test_missing_or_corrupt_bundle_triggers_download(tmp_path, monkeypatch, variant):
    target = tmp_path / 'models'
    _corrupt_layout(target, variant)
    calls = []
    _install_download(monkeypatch, calls)

    result = model_download.ensure_sherpa_streaming_model(target)

    assert calls == [EXPECTED_URL]
    assert result == target / BUNDLE_NAME
    assert (result / 'tokens.txt').is_file()
    assert (result / 'joiner-epoch-99-avg-1.onnx').is_file()


@pytest.mark.unit
def test_real_archive_is_downloaded_extracted_and_published(tmp_path, monkeypatch):
    """Exercises the genuine downloader/extractor against an in-memory archive."""
    target = tmp_path / 'models'
    archive = _archive_bytes(BUNDLE_NAME, _complete_files())
    seen = {}
    _install_urlopen(monkeypatch, _FakeResponse(archive), seen)

    result = model_download.ensure_sherpa_streaming_model(target)

    assert seen['url'] == EXPECTED_URL
    assert seen['timeout'] == 30, 'each socket read must be bounded'
    assert result == target / BUNDLE_NAME
    assert (result / 'tokens.txt').read_bytes() == _complete_files()['tokens.txt']
    assert (result / 'decoder-epoch-99-avg-1.int8.onnx').is_file()
    assert not list(target.glob('.sherpa-download-*')), 'staging must be cleaned up'


@pytest.mark.unit
def test_flat_archive_layout_is_published_under_the_release_name(tmp_path, monkeypatch):
    target = tmp_path / 'models'
    archive = _archive_bytes('', _complete_files())
    _install_urlopen(monkeypatch, _FakeResponse(archive))

    result = model_download.ensure_sherpa_streaming_model(target)

    assert result == target / BUNDLE_NAME
    assert (result / 'tokens.txt').is_file()
    assert not list(target.glob('.sherpa-download-*'))


# ---------------------------------------------------------------------------
# Failures are named, specific and never silent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failed_download_raises_error_naming_url_and_target(tmp_path, monkeypatch):
    target = tmp_path / 'models'

    def failing(url, destination, timeout_sec=600.0):
        raise OSError('connection reset by peer')

    monkeypatch.setattr(model_download, '_download_bundle', failing)

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model(target)

    message = str(caught.value)
    assert EXPECTED_URL in message
    assert str(target) in message
    assert 'by hand' in message
    assert caught.value.url == EXPECTED_URL
    assert caught.value.target_dir == str(target)
    assert caught.value.category == 'download'
    assert not (target / BUNDLE_NAME).exists()
    assert not list(target.glob('.sherpa-download-*'))


@pytest.mark.unit
def test_rate_limited_download_keeps_its_own_category(tmp_path, monkeypatch):
    target = tmp_path / 'models'
    error = urllib.request.HTTPError(EXPECTED_URL, 429, 'Too Many Requests', {}, None)
    monkeypatch.setattr(model_download, '_download_bundle',
                        lambda *a, **k: (_ for _ in ()).throw(error))

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model(target)

    assert caught.value.category == 'rate_limit'
    assert EXPECTED_URL in str(caught.value)


@pytest.mark.unit
def test_truncated_download_is_detected(tmp_path, monkeypatch):
    target = tmp_path / 'models'
    payload = _archive_bytes(BUNDLE_NAME, _complete_files())
    response = _FakeResponse(payload[:len(payload) // 2], declared_total=len(payload))
    _install_urlopen(monkeypatch, response)

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model(target)

    assert caught.value.category == 'truncated_download'
    assert str(target) in str(caught.value)
    assert not (target / BUNDLE_NAME).exists()


@pytest.mark.unit
def test_endless_download_hits_the_total_timeout(tmp_path, monkeypatch):
    target = tmp_path / 'models'
    _install_urlopen(monkeypatch, _EndlessResponse(b''))

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model(target, timeout_sec=0.0)

    assert caught.value.category == 'timeout'
    assert str(target) in str(caught.value)
    assert EXPECTED_URL in str(caught.value)


@pytest.mark.unit
def test_empty_target_directory_is_rejected_without_network(monkeypatch):
    calls = []
    monkeypatch.setattr(model_download, '_download_bundle', _forbidden_downloader(calls))

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model('')

    assert caught.value.category == 'missing_target'
    assert 'empty target_dir' in str(caught.value)
    assert calls == []


# ---------------------------------------------------------------------------
# A partial extraction must never be reported as success
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize('remove,empty', [
    ('tokens.txt', None),
    ('encoder-epoch-99-avg-1.onnx', None),
    ('joiner-epoch-99-avg-1.onnx', None),
    (None, 'encoder-epoch-99-avg-1.onnx'),
])
def test_partial_extraction_is_detected_as_failure(tmp_path, monkeypatch, remove, empty):
    target = tmp_path / 'models'
    files = _complete_files()
    if remove is not None:
        del files[remove]
    if empty is not None:
        files[empty] = b''

    def download(url, destination, timeout_sec=600.0):
        destination.write_bytes(b'fake-archive')

    def partial_extract(archive, destination):
        _write_bundle(destination / BUNDLE_NAME, files)

    monkeypatch.setattr(model_download, '_download_bundle', download)
    monkeypatch.setattr(model_download, '_extract_bundle', partial_extract)

    with pytest.raises(model_download.SherpaModelDownloadError) as caught:
        model_download.ensure_sherpa_streaming_model(target)

    assert caught.value.category == 'incomplete_download'
    assert EXPECTED_URL in str(caught.value)
    assert str(target) in str(caught.value)
    assert not (target / BUNDLE_NAME).exists(), 'partial bundle must not be published'
    assert not list(target.glob('.sherpa-download-*')), 'staging must be cleaned up'


# ---------------------------------------------------------------------------
# Integration: the real bundle on this machine is the fast path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@needs_real_bundle
def test_real_bundle_is_detected_as_already_present(monkeypatch):
    calls = []
    monkeypatch.setattr(model_download, '_download_bundle', _forbidden_downloader(calls))

    result = model_download.ensure_sherpa_streaming_model(REAL_SHERPA_BUNDLE)

    assert result == REAL_SHERPA_BUNDLE
    assert model_download._looks_like_sherpa_bundle(result)
    assert calls == [], 'an installed bundle must not trigger a download'


@pytest.mark.integration
@needs_real_bundle
def test_real_bundle_is_found_from_its_parent_directory(monkeypatch):
    calls = []
    monkeypatch.setattr(model_download, '_download_bundle', _forbidden_downloader(calls))

    result = model_download.ensure_sherpa_streaming_model(REAL_SHERPA_BUNDLE.parent)

    assert result == REAL_SHERPA_BUNDLE
    assert calls == []
