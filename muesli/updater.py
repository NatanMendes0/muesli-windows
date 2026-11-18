"""Per-user Windows updates: verify, unpack side by side, then switch after UI startup."""
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zipfile

import requests

REPO = 'https://github.com/NatanMendes0/muesli-windows'
API_BASE = 'https://api.github.com/repos/NatanMendes0/muesli-windows'
ASSET_HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
PREFIX = 'Muesli-windows-x64-'
VERSION = r'v(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})'
MAX_ARCHIVE = 1024 ** 3
MAX_EXPANDED = 3 * 1024 ** 3
MAX_ENTRIES = 15_000
TRIAL_ARG = '--muesli-update-trial'
ROLLBACK_ARG = '--muesli-update-rollback'
WINDOWS_DEVICES = re.compile(r'(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$)', re.I)
LOG = logging.getLogger(__name__)


class UpdateError(RuntimeError):
    pass


def version(tag):
    match = re.fullmatch(VERSION, tag) if isinstance(tag, str) else None
    if not match:
        raise UpdateError('Versão de atualização inválida.')
    return tuple(int(part) for part in match.groups())


def install_root():
    local = os.environ.get('LOCALAPPDATA', '')
    if sys.platform == 'win32' and getattr(sys, 'frozen', False) and local and Path(local).is_absolute():
        return Path(local).resolve() / 'Programs' / 'Muesli'
    return None


@dataclass(frozen=True)
class Asset:
    tag: str
    url: str
    sha256: str
    size: int


def verified_asset(release, tag):
    version(tag)
    if release.get('tag_name') != tag or release.get('draft') is not False or release.get('prerelease') is not False:
        raise UpdateError('A release não é uma versão estável do Muesli.')
    name = f'{PREFIX}{tag}.zip'
    url = f'{REPO}/releases/download/{tag}/{name}'
    for asset in release.get('assets', ()):
        if asset.get('name') != name or asset.get('browser_download_url') != url:
            continue
        digest, size = asset.get('digest'), asset.get('size')
        if (not isinstance(digest, str) or not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest)
                or type(size) is not int or not 0 < size <= MAX_ARCHIVE):
            break
        return Asset(tag, url, digest[7:].lower(), size)
    raise UpdateError('O GitHub não forneceu tamanho e SHA-256 válidos para o pacote oficial.')


def _response(url, *, stream=False):
    return requests.get(url, timeout=(10, 45), stream=stream, allow_redirects=False,
                        headers={'User-Agent': 'Muesli-Windows-Updater', 'Accept': 'application/vnd.github+json'})


def _approved_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname in ASSET_HOSTS and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        return False


def _download(asset, path, progress):
    url = asset.url
    for _ in range(5):
        if not _approved_url(url):
            raise UpdateError('O download foi redirecionado para um endereço não autorizado.')
        with _response(url, stream=True) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = response.headers.get('Location', '')
                continue
            if response.status_code != 200:
                raise UpdateError(f'O GitHub não entregou o pacote (HTTP {response.status_code}).')
            digest, received = hashlib.sha256(), 0
            with path.open('xb') as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    received += len(chunk)
                    if received > asset.size:
                        raise UpdateError('O pacote excedeu o tamanho publicado pelo GitHub.')
                    file.write(chunk)
                    digest.update(chunk)
                    progress(min(95, received * 95 // asset.size))
            if received != asset.size or digest.hexdigest() != asset.sha256:
                raise UpdateError('O tamanho ou SHA-256 do pacote não confere.')
            return
    raise UpdateError('O GitHub retornou redirecionamentos demais para o pacote.')


def _member_path(info):
    name = info.orig_filename.replace('\\', '/')
    parts = name[:-1].split('/') if name.endswith('/') else name.split('/')
    if any(part in ('', '.', '..') or part.rstrip(' .') != part
           or re.search(r'[<>:"|?*\x00-\x1f]', part)
           or WINDOWS_DEVICES.fullmatch(part.partition('.')[0].rstrip(' ')) for part in parts):
        raise UpdateError('O ZIP contém um caminho inválido para o Windows.')
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or info.external_attr & 0x400:  # reparse point
        raise UpdateError('O ZIP contém um vínculo ou arquivo especial.')
    return tuple(parts), name.endswith('/')


def extract_package(archive, destination, tag):
    version(tag)
    name = PREFIX + tag
    required = {name + '.exe', name + '.exe.config', '_internal/ui/index.html',
                'llama-runtime/llama-completion.exe', 'llama-runtime-vulkan/llama-completion.exe',
                'llama-runtime-vulkan/ggml-vulkan.dll'}
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        expanded = sum(info.file_size for info in entries)
        if len(entries) > MAX_ENTRIES or expanded > MAX_EXPANDED:
            raise UpdateError('O pacote excede o limite de arquivos ou de espaço extraído.')
        paths = {}
        actual_files = set()
        for info in entries:
            parts, is_dir = _member_path(info)
            if len(parts) == 1 and not is_dir and parts[0] not in required:
                raise UpdateError('O ZIP contém um arquivo inesperado no diretório principal.')
            key = tuple(part.casefold() for part in parts)
            if key in paths:
                raise UpdateError('O ZIP contém arquivos duplicados.')
            paths[key] = (parts, is_dir, info)
            if not is_dir:
                actual_files.add('/'.join(parts))
        if any(parent in paths and not paths[parent][1] for path in paths
               for parent in (path[:n] for n in range(1, len(path)))):
            raise UpdateError('O ZIP contém um conflito entre arquivo e diretório.')
        if not required.issubset(actual_files):
            raise UpdateError('O ZIP não contém todos os componentes do Muesli para Windows.')
        if shutil.disk_usage(destination.parent).free < expanded:
            raise UpdateError('Espaço insuficiente para extrair a atualização.')
        destination.mkdir()
        try:
            for parts, is_dir, info in paths.values():
                target = destination.joinpath(*parts)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as source, target.open('xb') as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            shutil.rmtree(destination, ignore_errors=True)
            raise


def stage_update(root, tag, current_version, progress=lambda _: None):
    if version(tag) <= version('v' + current_version):
        raise UpdateError('A atualização precisa ser mais recente que a versão em execução.')
    try:
        with _response(f'{API_BASE}/releases/tags/{tag}') as response:
            if response.status_code != 200:
                raise UpdateError(f'GitHub indisponível para verificar a atualização (HTTP {response.status_code}).')
            asset = verified_asset(response.json(), tag)
        root.mkdir(parents=True, exist_ok=True)
        versions = root / 'versions'
        versions.mkdir(exist_ok=True)
        if versions.resolve() != versions.absolute():
            raise UpdateError('A pasta de instalação aponta para outro local. A versão atual foi mantida.')
        if shutil.disk_usage(root).free < asset.size:
            raise UpdateError('Espaço insuficiente para baixar a atualização.')
        with tempfile.TemporaryDirectory(prefix='download-', dir=root) as folder:
            archive = Path(folder) / 'package.zip'
            _download(asset, archive, progress)
            extracted = Path(folder) / 'package'
            extract_package(archive, extracted, tag)
            staged = versions / (tag + '-' + uuid.uuid4().hex[:16])
            extracted.rename(staged)
        progress(100)
        return staged / (PREFIX + tag + '.exe')
    except requests.exceptions.SSLError as exc:
        raise UpdateError('O certificado HTTPS não pôde ser validado. A versão atual foi mantida.') from exc
    except (requests.RequestException, OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, UpdateError):
            raise
        LOG.warning('update preparation failed: %s', type(exc).__name__)
        raise UpdateError('Não foi possível baixar ou preparar a atualização. A versão atual foi mantida.') from exc


def _candidate(root, directory):
    match = re.fullmatch(f'({VERSION})-[0-9a-f]{{16}}', directory)
    if not match:
        return None
    tag = match[1]
    path = root / 'versions' / directory / (PREFIX + tag + '.exe')
    if path.is_file() and path.resolve() == path.absolute():
        return path, tag
    return None


def active_executable(root, current_version):
    try:
        directory = (root / 'active').read_text(encoding='ascii').strip()
        entry = _candidate(root, directory)
        if entry is not None and version(entry[1]) > version('v' + current_version):
            return entry[0]
    except (OSError, UnicodeError, UpdateError):
        pass
    return None


def spawn(executable, *args):
    env = {**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'}
    dll_directory = None
    if sys.platform == 'win32' and getattr(sys, 'frozen', False):
        import ctypes
        dll_directory = ctypes.windll.kernel32.SetDllDirectoryW
        dll_directory(None)
    try:
        return subprocess.Popen([str(executable), *args], cwd=executable.parent, env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        if dll_directory:
            dll_directory(getattr(sys, '_MEIPASS', None))


def valid_trial(token):
    return bool(re.fullmatch(r'[0-9a-f]{32}', token))


def mark_ready(root, executable, current_version, token):
    entry = _candidate(root, executable.parent.name)
    if not valid_trial(token) or entry != (executable, 'v' + current_version):
        return False
    (root / ('ready-' + token)).touch()
    return True


def trial_committed(root, executable):
    try:
        return (root / 'active').read_text(encoding='ascii').strip() == executable.parent.name
    except (OSError, UnicodeError):
        return False


def restart_into(root, candidate, current_executable, timeout: float = 45):
    entry = _candidate(root, candidate.parent.name)
    token = uuid.uuid4().hex
    ready = root / ('ready-' + token)
    pending = root / ('active-' + token + '.tmp')
    child = None
    try:
        if entry is None or entry[0] != candidate:
            raise OSError('Update candidate is no longer available')
        child = spawn(candidate, TRIAL_ARG, token)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                break
            if ready.is_file():
                pending.write_text(candidate.parent.name + '\n', encoding='ascii')
                pending.replace(root / 'active')
                return True
            time.sleep(.1)
    except OSError as exc:
        LOG.warning('update activation failed: %s', type(exc).__name__)
    finally:
        for path in (ready, pending):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if child is not None and child.poll() is None:
        try:
            child.terminate()
            child.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            LOG.warning('update trial did not shut down normally')
    try:
        spawn(current_executable, ROLLBACK_ARG)
    except OSError:
        LOG.warning('could not reopen previous executable')
    return False
