import os
import re
import shutil
import importlib
import importlib.metadata
import json
import platform
import sys
import traceback
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import streamlit as st
import yt_dlp

st.set_page_config(
    page_title="YouTube Downloader",
    page_icon="🎬",
    layout="wide",
)

MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
BGUTIL_VERSION = "2.0.0"
BGUTIL_DIR = Path.home() / ".cache" / "bgutil-ytdlp-pot-provider"
BGUTIL_SERVER = BGUTIL_DIR / "server"
BGUTIL_ARCHIVE = BGUTIL_DIR / f"bgutil-{BGUTIL_VERSION}.tar.gz"
BGUTIL_HTTP_HOST = "127.0.0.1"
BGUTIL_HTTP_PORT = 4416
BGUTIL_HTTP_URL = f"http://{BGUTIL_HTTP_HOST}:{BGUTIL_HTTP_PORT}"
BGUTIL_HTTP_LOG = BGUTIL_DIR / "bgutil-http-server.log"

# Resoluções apresentadas ao usuário. O app nunca mostra alturas "estranhas"
# como 2026p; ele trabalha com padrões comuns e limita o download a eles.
RESOLUCOES_PADRAO = [
    (2160, "2160p"),
    (1440, "1440p"),
    (1080, "1080p"),
    (720, "720p"),
    (480, "480p"),
    (360, "360p"),
    (240, "240p"),
    (144, "144p"),
]


def formatar_duracao(segundos: Optional[int]) -> str:
    if not segundos:
        return "N/A"
    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)
    return f"{horas:02d}:{minutos:02d}:{segundos:02d}" if horas else f"{minutos:02d}:{segundos:02d}"


def formatar_visualizacoes(valor: Optional[int]) -> str:
    if valor is None:
        return "N/A"
    if valor >= 1_000_000_000:
        return f"{valor / 1_000_000_000:.1f} bi"
    if valor >= 1_000_000:
        return f"{valor / 1_000_000:.1f} mi"
    if valor >= 1_000:
        return f"{valor / 1_000:.1f} mil"
    return str(valor)


def formatar_bytes(valor: Optional[float]) -> str:
    if valor is None:
        return "N/A"
    valor = float(valor)
    for unidade in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024:
            return f"{valor:.1f} {unidade}"
        valor /= 1024
    return f"{valor:.1f} PB"


def formatar_velocidade(valor: Optional[float]) -> str:
    return "N/A" if valor is None else f"{formatar_bytes(valor)}/s"


def formatar_eta(valor: Optional[int]) -> str:
    if valor is None:
        return "N/A"
    minutos, segundos = divmod(int(valor), 60)
    return f"{minutos}m {segundos}s" if minutos else f"{segundos}s"


def sanitizar_nome_arquivo(nome: str, limite: int = 180) -> str:
    nome = nome or "download"
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", nome)
    nome = re.sub(r"\s+", " ", nome).strip().rstrip(". ")
    return (nome or "download")[:limite]


def normalizar_url(url: str) -> str:
    url = url.strip()
    if "youtube.com/playlist" in url:
        raise ValueError("Playlists não são suportadas. Cole o link de um vídeo individual.")
    if "youtube.com/watch" in url and "&list=" in url:
        url = url.split("&list=", 1)[0]
    if "youtu.be/" in url and "?" in url:
        url = url.split("?", 1)[0]
    return url


def _extrair_provider() -> None:
    """Baixa e prepara o BgUtils para o servidor HTTP local."""
    BGUTIL_DIR.mkdir(parents=True, exist_ok=True)

    if not (BGUTIL_SERVER / "src" / "main.ts").exists():
        url = (
            "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/"
            f"archive/refs/tags/{BGUTIL_VERSION}.tar.gz"
        )
        urllib.request.urlretrieve(url, BGUTIL_ARCHIVE)

        temp_extract = BGUTIL_DIR / f"extract-{BGUTIL_VERSION}"
        if temp_extract.exists():
            shutil.rmtree(temp_extract)
        temp_extract.mkdir(parents=True, exist_ok=True)

        with tarfile.open(BGUTIL_ARCHIVE, "r:gz") as tar:
            tar.extractall(temp_extract, filter="data")

        extracted = temp_extract / f"bgutil-ytdlp-pot-provider-{BGUTIL_VERSION}" / "server"
        if not extracted.exists():
            raise RuntimeError("A estrutura do BgUtils foi baixada, mas a pasta server não foi encontrada.")

        if BGUTIL_SERVER.exists():
            shutil.rmtree(BGUTIL_SERVER)
        shutil.copytree(extracted, BGUTIL_SERVER)
        shutil.rmtree(temp_extract, ignore_errors=True)

    deno = shutil.which("deno")
    if not deno:
        raise RuntimeError("Deno não foi encontrado no servidor. Verifique o requirements.txt.")

    node_modules = BGUTIL_SERVER / "node_modules"
    if not node_modules.exists():
        resultado = subprocess.run(
            [
                deno,
                "install",
                "--allow-scripts=npm:canvas",
                "--frozen",
            ],
            cwd=BGUTIL_SERVER,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
        )
        if resultado.returncode != 0:
            raise RuntimeError(
                "Falha ao instalar as dependências do BgUtils.\n\n"
                + resultado.stdout[-5000:]
            )

    if not node_modules.is_dir():
        raise RuntimeError("O BgUtils foi baixado, mas node_modules não foi criado.")


def _ping_bgutil_http(timeout: float = 2.0) -> Dict[str, Any]:
    """Verifica se o servidor HTTP local do BgUtils está respondendo."""
    url = f"{BGUTIL_HTTP_URL}/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            corpo = response.read().decode("utf-8", "replace")
            try:
                dados = json.loads(corpo)
            except json.JSONDecodeError:
                dados = {"raw": corpo}
            return {"ok": True, "url": url, "status_code": response.status, "resposta": dados}
    except Exception as exc:
        return {
            "ok": False,
            "url": url,
            "status_code": None,
            "erro": f"{type(exc).__name__}: {exc}",
        }


def _tail_bgutil_log(limite: int = 12000) -> str:
    try:
        if BGUTIL_HTTP_LOG.is_file():
            return DiagnosticoLogger._redact(BGUTIL_HTTP_LOG.read_text(encoding="utf-8", errors="replace"))[-limite:]
    except Exception:
        pass
    return ""


def _iniciar_bgutil_http(deno: str) -> Dict[str, Any]:
    """Inicia uma única instância do servidor BgUtils HTTP em localhost:4416."""
    existente = _ping_bgutil_http(timeout=1.5)
    if existente.get("ok"):
        return {
            "status": "JA_ESTAVA_ATIVO",
            "url": BGUTIL_HTTP_URL,
            "pid": None,
            "ping": existente,
            "log": _tail_bgutil_log(),
        }

    BGUTIL_DIR.mkdir(parents=True, exist_ok=True)
    main_ts = BGUTIL_SERVER / "src" / "main.ts"
    node_modules = BGUTIL_SERVER / "node_modules"
    if not main_ts.is_file():
        raise RuntimeError(f"O servidor HTTP do BgUtils não foi encontrado: {main_ts}")

    log_handle = open(BGUTIL_HTTP_LOG, "a", encoding="utf-8")
    cmd = [
        deno,
        "run",
        "--no-prompt",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={node_modules.resolve()}",
        f"--allow-read={node_modules.resolve()}",
        f"--allow-read={BGUTIL_SERVER.resolve()}",
        f"--allow-write={BGUTIL_DIR.resolve()}",
        "--allow-sys",
        "../src/main.ts",
        "--host",
        BGUTIL_HTTP_HOST,
        "--port",
        str(BGUTIL_HTTP_PORT),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=node_modules,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=os.environ.copy(),
        )
    except Exception:
        log_handle.close()
        raise

    # O arquivo de log permanece aberto pelo processo filho. No processo
    # Streamlit fechamos apenas a referência Python; o descritor herdado segue
    # válido para o servidor.
    log_handle.close()

    prazo = time.time() + 25
    ultimo_ping = None
    while time.time() < prazo:
        if proc.poll() is not None:
            raise RuntimeError(
                "O servidor HTTP do BgUtils encerrou durante a inicialização.\n\n"
                + _tail_bgutil_log()
            )
        ultimo_ping = _ping_bgutil_http(timeout=1.5)
        if ultimo_ping.get("ok"):
            return {
                "status": "INICIADO",
                "url": BGUTIL_HTTP_URL,
                "pid": proc.pid,
                "ping": ultimo_ping,
                "log": _tail_bgutil_log(),
            }
        time.sleep(0.4)

    raise RuntimeError(
        "O servidor HTTP do BgUtils não respondeu em até 25 segundos.\n\n"
        + (_tail_bgutil_log() or str(ultimo_ping or "sem resposta ao /ping"))
    )


@st.cache_resource(show_spinner=False)
def preparar_ambiente() -> Dict[str, Any]:
    """Prepara Deno/BgUtils e mantém o servidor HTTP vivo na instância."""
    _extrair_provider()
    deno = shutil.which("deno")
    if not deno:
        raise RuntimeError("Deno não foi localizado. O pacote deno está instalado, mas o executável não está disponível.")

    teste = subprocess.run(
        [deno, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
    )
    if teste.returncode != 0:
        raise RuntimeError(
            "O executável Deno foi encontrado, mas não conseguiu iniciar.\n\n"
            + teste.stdout[-3000:]
        )

    servidor = _iniciar_bgutil_http(deno)
    script = BGUTIL_SERVER / "build" / "generate_once.js"
    return {
        "deno": deno,
        "script": str(script),
        "bgutil_http": servidor,
        "bgutil_http_url": BGUTIL_HTTP_URL,
    }


def _opcoes_provider() -> Dict[str, Dict[str, str]]:
    preparar_ambiente()
    # O modo HTTP é preferível aqui porque o próprio plugin conversa com o
    # servidor local. Isso elimina a etapa em que o modo script aparecia como
    # "external, unavailable" no diagnóstico anterior.
    return {
        "youtubepot-bgutilhttp": {
            "base_url": BGUTIL_HTTP_URL,
        }
    }


def _opcoes_js() -> Dict[str, Any]:
    ambiente = preparar_ambiente()
    deno = ambiente.get("deno") or shutil.which("deno")

    if not deno:
        raise RuntimeError(
            "Deno não foi localizado. O pacote deno está instalado, "
            "mas o executável não está disponível para o yt-dlp."
        )

    # Não basta Deno existir no PATH do processo. O yt-dlp aceita um caminho
    # explícito no formato equivalente a --js-runtimes deno:/caminho/deno.
    # Isso evita exatamente o estado 'script-deno ... unavailable'.
    return {
        "js_runtimes": {
            "deno": {"path": deno},
        },
        "remote_components": {"ejs": ["github"]},
        "extractor_args": {
            "youtube": {"player_client": ["mweb", "default"]},
        },
    }


class DiagnosticoLogger:
    """Captura logs completos do yt-dlp com redacao de segredos."""

    def __init__(self) -> None:
        self.linhas: list[str] = []

    @staticmethod
    def _redact(texto: str) -> str:
        patterns = [
            (r'(?i)(authorization\s*[:=]\s*)\S+', r'\1[OCULTO]'),
            (r'(?i)(cookie\s*[:=]\s*)\S+', r'\1[OCULTO]'),
            (r'(?i)(po.?token[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
            (r'(?i)(content[-_ ]binding[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
            (r'(?i)(visitor[-_ ]data[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
            (r'(?i)(data[-_ ]sync[-_ ]id[^=:\n]*[=:]\s*)[^\s,;]+', r'\1[OCULTO]'),
            (r'(?i)(proxy[^=:\n]*[=:]\s*)(https?://)?[^ \n]+@', r'\1[OCULTO]@'),
        ]
        for pattern, replacement in patterns:
            texto = re.sub(pattern, replacement, texto)
        if len(texto) > 5000:
            texto = texto[:5000] + " …[linha truncada]"
        return texto

    def _guardar(self, mensagem: Any) -> None:
        self.linhas.append(self._redact(str(mensagem)))

    def debug(self, msg: str) -> None:
        self._guardar(msg)

    def info(self, msg: str) -> None:
        self._guardar(msg)

    def warning(self, msg: str) -> None:
        self._guardar("WARNING: " + msg)

    def error(self, msg: str) -> None:
        self._guardar("ERROR: " + msg)


def _executar_comando(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 30) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return {
            "comando": " ".join(cmd),
            "returncode": proc.returncode,
            "timeout": False,
            "saida": DiagnosticoLogger._redact(proc.stdout or "")[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        saida = exc.stdout or ""
        if isinstance(saida, bytes):
            saida = saida.decode("utf-8", "replace")
        return {
            "comando": " ".join(cmd),
            "returncode": None,
            "timeout": True,
            "saida": DiagnosticoLogger._redact(saida)[-12000:],
        }
    except Exception as exc:
        return {
            "comando": " ".join(cmd),
            "returncode": None,
            "timeout": False,
            "saida": f"{type(exc).__name__}: {exc}",
        }


def _arquivos_relevantes(raiz: Path, limite: int = 300) -> list[Dict[str, Any]]:
    encontrados = []
    if not raiz.exists():
        return encontrados
    try:
        for item in sorted(raiz.rglob("*")):
            if len(encontrados) >= limite:
                break
            try:
                if item.is_file():
                    encontrados.append({
                        "arquivo": str(item.relative_to(raiz)),
                        "bytes": item.stat().st_size,
                        "ext": item.suffix,
                    })
            except OSError:
                continue
    except OSError:
        pass
    return encontrados


def _modulos_bgutil() -> Dict[str, Any]:
    resultado = {
        "distribuicoes": [],
        "modulos_encontrados": [],
        "erros_importacao": [],
    }

    try:
        for dist in importlib.metadata.distributions():
            nome = (dist.metadata.get("Name") or "").lower()
            if "bgutil" in nome:
                arquivos = [str(f) for f in (dist.files or [])
                            if "bgutil" in str(f).lower() or "ytdlp_plugins" in str(f).lower()]
                resultado["distribuicoes"].append({
                    "name": dist.metadata.get("Name"),
                    "version": dist.version,
                    "location": str(dist.locate_file("")),
                    "arquivos": arquivos[:300],
                })
    except Exception as exc:
        resultado["erros_importacao"].append(f"metadata: {type(exc).__name__}: {exc}")

    for nome in ("yt_dlp_plugins", "yt_dlp_plugins.extractor", "yt_dlp_plugins.extractor.youtube"):
        try:
            mod = importlib.import_module(nome)
            resultado["modulos_encontrados"].append({
                "modulo": nome,
                "arquivo": getattr(mod, "__file__", None),
                "caminho": [str(x) for x in getattr(mod, "__path__", [])],
            })
        except Exception as exc:
            resultado["erros_importacao"].append(
                f"import {nome}: {type(exc).__name__}: {exc}"
            )
    return resultado


def _variaveis_ambiente_relevantes() -> Dict[str, str]:
    nomes = [
        "HOME", "USER", "USERNAME", "PATH", "PYTHONPATH", "VIRTUAL_ENV",
        "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "YTDLP_NO_PLUGINS",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    ]
    out = {}
    for nome in nomes:
        valor = os.environ.get(nome)
        if valor is not None:
            if nome in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}:
                valor = re.sub(r'(?i)(https?://)([^/@]+)@', r'\1[OCULTO]@', valor)
            out[nome] = valor
    return out

def _resumo_diagnostico(info: Dict[str, Any], logger: DiagnosticoLogger, ambiente: Dict[str, str]) -> Dict[str, Any]:
    formatos = []
    for fmt in info.get("formats", []):
        formatos.append({
            "id": fmt.get("format_id"),
            "height": fmt.get("height"),
            "width": fmt.get("width"),
            "fps": fmt.get("fps"),
            "ext": fmt.get("ext"),
            "vcodec": fmt.get("vcodec"),
            "acodec": fmt.get("acodec"),
            "protocol": fmt.get("protocol"),
            "tbr": fmt.get("tbr"),
            "vbr": fmt.get("vbr"),
            "abr": fmt.get("abr"),
            "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
            "has_url": bool(fmt.get("url")),
            "format_note": fmt.get("format_note"),
        })

    try:
        plugin_dirs = [str(p) for p in yt_dlp.plugins.directories()]
    except Exception as exc:
        plugin_dirs = [f"ERRO AO CONSULTAR: {type(exc).__name__}: {exc}"]

    possiveis_raizes = [
        Path.home() / "yt-dlp-plugins",
        Path.home() / ".config" / "yt-dlp" / "plugins",
        Path.home() / ".local" / "share" / "yt-dlp" / "plugins",
        BGUTIL_DIR,
        BGUTIL_SERVER,
    ]
    possiveis_raizes.extend(Path(x) for x in plugin_dirs if x)

    filesystem = {}
    for raiz in possiveis_raizes:
        chave = str(raiz)
        if chave not in filesystem:
            filesystem[chave] = {
                "existe": raiz.exists(),
                "is_dir": raiz.is_dir(),
                "arquivos": _arquivos_relevantes(raiz),
            }

    comandos = {}
    comandos_def = {
        "python": [sys.executable, "--version"],
        "yt-dlp": [sys.executable, "-m", "yt_dlp", "--version"],
        "deno": [ambiente.get("deno") or "deno", "--version"],
        "node": ["node", "--version"],
        "npm": ["npm", "--version"],
        "npx": ["npx", "--version"],
        "ffmpeg": ["ffmpeg", "-version"],
        "ffprobe": ["ffprobe", "-version"],
    }
    for nome, cmd in comandos_def.items():
        comandos[nome] = _executar_comando(cmd, timeout=25)

    script = Path(ambiente.get("script", ""))
    if script.is_file() and ambiente.get("deno"):
        # O teste anterior executava o JS diretamente, sem as permissões que
        # o provider realmente usa. Isso produzia um falso diagnóstico
        # "Requires env access". Agora reproduzimos o modelo documentado para
        # Deno: env + net + ffi em node_modules + leitura de node_modules/cache.
        server_home = BGUTIL_SERVER.resolve()
        node_modules = (server_home / "node_modules").resolve()
        deno_base = [
            ambiente["deno"],
            "run",
            "--no-prompt",
            "--allow-env",
            "--allow-net",
            f"--allow-ffi={node_modules}",
            f"--allow-read={server_home}",
            "--allow-sys",
            str(script.resolve()),
        ]
        comandos["deno_generate_once_version"] = _executar_comando(
            deno_base + ["--version"],
            cwd=BGUTIL_SERVER,
            timeout=45,
        )
        comandos["deno_generate_once_verbose"] = _executar_comando(
            deno_base + ["--verbose"],
            cwd=BGUTIL_SERVER,
            timeout=45,
        )

        # Teste decisivo: pede ao próprio gerador um PO Token para o vídeo
        # analisado. O token é imediatamente redigido; só estado, binding e
        # expiração são mantidos no diagnóstico.
        comandos["deno_generate_once_pot"] = None

    return {
        "yt_dlp": getattr(yt_dlp.version, "__version__", "desconhecida"),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cwd": os.getcwd(),
        "home": str(Path.home()),
        "deno": ambiente.get("deno") or "não encontrado",
        "script": ambiente.get("script") or "não definido",
        "script_existe": script.is_file(),
        "script_bytes": script.stat().st_size if script.is_file() else None,
        "bgutil_dir": str(BGUTIL_DIR),
        "bgutil_server": str(BGUTIL_SERVER),
        "plugin_dirs": plugin_dirs,
        "plugin_modules": _modulos_bgutil(),
        "environment": _variaveis_ambiente_relevantes(),
        "filesystem": filesystem,
        "commands": comandos,
        "bgutil_http": {
            "url": BGUTIL_HTTP_URL,
            "ping": _ping_bgutil_http(timeout=2.0),
            "log_tail": _tail_bgutil_log(),
        },
        "logs_completos": logger.linhas[-500:],
        "formatos": formatos,
        "info_resumo": {
            "id": info.get("id"),
            "title": info.get("title"),
            "extractor": info.get("extractor"),
            "extractor_key": info.get("extractor_key"),
            "duration": info.get("duration"),
            "format_count": len(info.get("formats") or []),
        },
    }

class DiagnosticoErro(Exception):
    def __init__(self, diagnostico: Dict[str, Any]):
        self.diagnostico = diagnostico
        super().__init__(diagnostico.get("erro_extracao", "Falha no diagnóstico."))


def _testar_pot_direto(ambiente: Dict[str, str], video_id: str) -> Dict[str, Any]:
    """Testa o gerador BgUtils diretamente, sem depender do plugin yt-dlp."""
    deno = ambiente.get("deno")
    script = ambiente.get("script")
    if not deno or not script or not video_id:
        return {"status": "NAO_EXECUTADO", "motivo": "Deno, script ou video_id ausente"}

    script_path = Path(script).resolve()
    server_home = BGUTIL_SERVER.resolve()
    node_modules = (server_home / "node_modules").resolve()

    # O generate_once.ts usa a pasta de cache em ~/.cache/bgutil-ytdlp-pot-provider
    # (BGUTIL_DIR), não apenas server/. Ele também grava nessa pasta.
    # Portanto, leitura e escrita precisam abranger BGUTIL_DIR.
    cmd = [
        deno, "run", "--no-prompt",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={node_modules}",
        f"--allow-read={BGUTIL_DIR.resolve()}",
        f"--allow-write={BGUTIL_DIR.resolve()}",
        "--allow-sys",
        str(script_path),
        "--content-binding", video_id,
        "--bypass-cache",
    ]
    # Aqui precisamos do stdout bruto internamente para detectar o poToken.
    # A saída nunca é mostrada sem passar por _redact antes de entrar no
    # diagnóstico.
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(server_home),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            env=os.environ.copy(),
        )
        bruto = proc.stdout or ""
        resultado = {
            "returncode": proc.returncode,
            "timeout": False,
        }
    except subprocess.TimeoutExpired as exc:
        saida = exc.stdout or ""
        if isinstance(saida, bytes):
            saida = saida.decode("utf-8", "replace")
        bruto = saida
        resultado = {
            "returncode": None,
            "timeout": True,
        }
    except Exception as exc:
        bruto = ""
        resultado = {
            "returncode": None,
            "timeout": False,
            "erro": f"{type(exc).__name__}: {exc}",
        }

    texto = DiagnosticoLogger._redact(bruto)

    pot = None
    content_binding = None
    expires_at = None
    # O BgUtils pode escrever uma linha de diagnóstico do Deno antes do JSON.
    # Por isso json.loads(bruto) inteiro pode falhar mesmo quando o token foi
    # gerado corretamente. Procuramos o último objeto JSON válido com poToken.
    for linha in reversed(bruto.splitlines()):
        linha = linha.strip()
        if not linha or not linha.startswith("{"):
            continue
        try:
            dados = json.loads(linha)
        except Exception:
            continue
        if isinstance(dados, dict) and dados.get("poToken"):
            pot = dados.get("poToken")
            content_binding = dados.get("contentBinding")
            expires_at = dados.get("expiresAt")
            break

    return {
        "status": "PO_TOKEN_GERADO" if pot else "FALHA",
        "returncode": resultado.get("returncode"),
        "timeout": resultado.get("timeout"),
        "deno": deno,
        "script": str(script_path),
        "server_home": str(server_home),
        "script_existe": script_path.is_file(),
        "node_modules_existe": node_modules.is_dir(),
        "contentBinding": content_binding,
        "expiresAt": expires_at,
        "poToken_presente": bool(pot),
        "comando_redigido": f"{deno} run ... {script_path} --content-binding [OCULTO] --bypass-cache",
        "saida_redigida": texto[-12000:],
        "erro_execucao": resultado.get("erro"),
    }


def _testar_script_version(ambiente: Dict[str, str]) -> Dict[str, Any]:
    """Testa apenas a inicialização do generate_once.js no Deno."""
    deno = ambiente.get("deno")
    script = ambiente.get("script")
    if not deno or not script:
        return {"status": "NAO_EXECUTADO"}

    server_home = BGUTIL_SERVER.resolve()
    node_modules = (server_home / "node_modules").resolve()
    cmd = [
        deno, "run", "--no-prompt",
        "--allow-env", "--allow-net",
        f"--allow-ffi={node_modules}",
        f"--allow-read={BGUTIL_DIR.resolve()}",
        f"--allow-write={BGUTIL_DIR.resolve()}",
        "--allow-sys",
        str(Path(script).resolve()),
        "--version",
    ]
    resultado = _executar_comando(cmd, cwd=server_home, timeout=45)
    return {
        "status": "OK" if resultado.get("returncode") == 0 else "FALHA",
        "returncode": resultado.get("returncode"),
        "timeout": resultado.get("timeout"),
        "saida_redigida": DiagnosticoLogger._redact(resultado.get("saida", ""))[-4000:],
    }


def diagnosticar_video(url: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if not url or not url.strip():
        raise ValueError("Informe uma URL do YouTube.")

    ambiente = preparar_ambiente()
    logger = DiagnosticoLogger()
    op_js = _opcoes_js()
    op_provider = _opcoes_provider()

    extractor_args = {
        **op_js.get("extractor_args", {}),
        "youtube": {
            **op_js.get("extractor_args", {}).get("youtube", {}),
            "pot_trace": ["true"],
        },
        **op_provider,
    }

    ydl_opts = {
        "quiet": True,
        "no_warnings": False,
        "verbose": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "logger": logger,
        "js_runtimes": op_js["js_runtimes"],
        "remote_components": op_js["remote_components"],
        "extractor_args": extractor_args,
    }

    config = {
        "js_runtimes": op_js.get("js_runtimes"),
        "remote_components": op_js.get("remote_components"),
        "extractor_args": extractor_args,
    }

    url_normalizada = normalizar_url(url)

    # O teste direto é feito antes da extração principal para separar uma
    # falha do gerador BgUtils de uma falha posterior do YouTube/yt-dlp.
    video_id_teste = None
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_normalizada)
    if m:
        video_id_teste = m.group(1)
    elif re.fullmatch(r"[A-Za-z0-9_-]{11}", url_normalizada):
        video_id_teste = url_normalizada

    pot_direto = _testar_pot_direto(ambiente, video_id_teste) if video_id_teste else {
        "status": "não executado",
        "motivo": "não foi possível identificar o video_id na URL",
    }
    script_version = _testar_script_version(ambiente)
    bgutil_http = {
        "url": BGUTIL_HTTP_URL,
        "ping": _ping_bgutil_http(timeout=2.0),
        "log_tail": _tail_bgutil_log(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url_normalizada, download=False)
    except Exception:
        dummy_info = {"formats": []}
        diag = _resumo_diagnostico(dummy_info, logger, ambiente)
        diag["erro_extracao"] = DiagnosticoLogger._redact(traceback.format_exc())
        diag["config_efetiva"] = config
        diag["pot_direto"] = pot_direto
        diag["script_version_deno"] = script_version
        diag["bgutil_http"] = bgutil_http
        raise DiagnosticoErro(diag)

    diag = _resumo_diagnostico(info, logger, ambiente)
    diag["config_efetiva"] = config
    diag["pot_direto"] = pot_direto
    diag["script_version_deno"] = script_version
    diag["bgutil_http"] = bgutil_http
    return info, diag

def extrair_info_video(url: str) -> Dict[str, Any]:
    if not url or not url.strip():
        raise ValueError("Informe uma URL do YouTube.")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        **_opcoes_js(),
        "extractor_args": {
            **_opcoes_js().get("extractor_args", {}),
            **_opcoes_provider(),
        },
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalizar_url(url), download=False)
        if not info:
            raise RuntimeError("O YouTube não retornou informações para essa URL.")
        return info
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        low = msg.lower()
        if "private video" in low:
            raise RuntimeError("O vídeo é privado e não pode ser acessado.") from exc
        if "sign in" in low or "confirm your age" in low or "age" in low:
            raise RuntimeError("O vídeo possui restrição de idade ou exige autenticação.") from exc
        if "not available" in low or "unavailable" in low:
            raise RuntimeError("O vídeo está indisponível ou possui restrição regional.") from exc
        raise RuntimeError(f"Não foi possível acessar o vídeo: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"Erro ao analisar o vídeo: {exc}") from exc


def obter_formatos_disponiveis(info_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Detecta todas as streams de vídeo, sem limitar o container a MP4."""
    alturas = set()
    videos = []
    audios = []

    for fmt in info_dict.get("formats", []):
        height = fmt.get("height")
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")

        if vcodec not in (None, "none") and height:
            try:
                altura = int(height)
                if altura > 0:
                    alturas.add(altura)
                    videos.append(fmt)
            except (TypeError, ValueError):
                pass

        if acodec not in (None, "none") and vcodec in (None, "none"):
            audios.append(fmt)

    return {
        "alturas": sorted(alturas, reverse=True),
        "video": videos,
        "audio": audios,
    }


def montar_opcoes_resolucao(alturas: list[int]) -> Dict[str, int]:
    """Mostra somente resoluções padrão realmente alcançáveis pelo vídeo."""
    if not alturas:
        return {}

    maior = max(alturas)
    disponiveis = [
        (padrao, label)
        for padrao, label in RESOLUCOES_PADRAO
        if maior >= padrao
    ]

    if not disponiveis:
        return {f"{maior}p (máxima disponível)": maior}

    opcoes: Dict[str, int] = {}
    for indice, (padrao, label) in enumerate(disponiveis):
        if indice == 0:
            opcoes[f"{label} (máxima disponível)"] = padrao
        else:
            opcoes[label] = padrao
    return opcoes


class DownloadProgress:
    def __init__(self) -> None:
        self.progress_bar = None
        self.status = None

    def iniciar(self) -> None:
        self.progress_bar = st.progress(0)
        self.status = st.empty()

    def hook(self, data: Dict[str, Any]) -> None:
        if self.progress_bar is None or self.status is None:
            return

        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            if total:
                percentual = min(max(downloaded / total, 0), 1)
                self.progress_bar.progress(int(percentual * 100))
                percentual_texto = f"{percentual * 100:.1f}%"
            else:
                percentual_texto = "calculando"
            self.status.info(
                f"**Baixando:** {percentual_texto}  \n"
                f"**Velocidade:** {formatar_velocidade(data.get('speed'))} • "
                f"**Tempo restante:** {formatar_eta(data.get('eta'))}"
            )
        elif status == "finished":
            self.progress_bar.progress(100)
            self.status.info("Download concluído. Finalizando com FFmpeg...")


def _arquivo_final(pasta: str, extensao: str) -> Path:
    candidatos = [
        p for p in Path(pasta).iterdir()
        if p.is_file() and p.suffix.lower() == extensao.lower()
    ]
    if not candidatos:
        raise FileNotFoundError(
            f"O processamento terminou, mas nenhum arquivo {extensao} foi encontrado."
        )
    return max(candidatos, key=lambda p: p.stat().st_mtime)


def baixar_e_converter(
    url: str,
    formato_escolhido: str,
    qualidade: int,
    pasta_destino: str,
    progress: Optional[DownloadProgress] = None,
) -> Path:
    if progress:
        progress.iniciar()

    common = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 4,
        "fragment_retries": 4,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress.hook] if progress else [],
        "outtmpl": str(Path(pasta_destino) / "%(title).180s.%(ext)s"),
        **_opcoes_js(),
        "extractor_args": {
            **_opcoes_js().get("extractor_args", {}),
            **_opcoes_provider(),
        },
    }

    if formato_escolhido == "video":
        common.update({
            # Não restringe a MP4/M4A. Streams de alta resolução do YouTube
            # podem ser WebM/VP9/AV1 e ainda podem ser processadas pelo FFmpeg.
            "format": (
                f"bestvideo[height<={qualidade}]+bestaudio/"
                f"best[height<={qualidade}]/best"
            ),
            "merge_output_format": "mp4",
        })
        extensao_final = ".mp4"
    elif formato_escolhido == "audio":
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": str(qualidade),
            }],
        })
        extensao_final = ".mp3"
    else:
        raise ValueError("Tipo de mídia inválido.")

    try:
        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.download([normalizar_url(url)])
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        low = msg.lower()
        if "403" in low or "forbidden" in low:
            raise RuntimeError(
                "O YouTube recusou o stream (HTTP 403). O provider de PO Token foi configurado, "
                "mas este vídeo/IP pode exigir outra estratégia de acesso."
            ) from exc
        if "ffmpeg" in low or "ffprobe" in low:
            raise RuntimeError("FFmpeg/FFprobe não está disponível no servidor.") from exc
        raise RuntimeError(f"Falha no download: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"Erro durante o processamento: {exc}") from exc

    return _arquivo_final(pasta_destino, extensao_final)


def mostrar_metadados(info: Dict[str, Any]) -> None:
    esquerda, direita = st.columns([1, 2])
    with esquerda:
        if info.get("thumbnail"):
            st.image(info["thumbnail"], use_container_width=True)

    with direita:
        titulo = info.get("title") or info.get("fulltitle") or "Título indisponível"
        st.subheader(titulo)
        st.write(f"**Canal:** {info.get('channel') or info.get('uploader') or 'N/A'}")
        st.write(f"**Duração:** {formatar_duracao(info.get('duration'))}")
        st.write(f"**Visualizações:** {formatar_visualizacoes(info.get('view_count'))}")


def main() -> None:
    st.title("🎬 YouTube Downloader — Diagnóstico Completo V7")
    st.caption("MP4 com áudio ou extração de áudio MP3 — processamento realizado no servidor.")
    st.info("Use somente conteúdo que você tenha autorização para baixar ou que seja permitido pelos termos e direitos aplicáveis.")

    try:
        preparar_ambiente()
        ambiente_ok = True
        ambiente_erro = ""
    except Exception as exc:
        ambiente_ok = False
        ambiente_erro = str(exc)

    if not ambiente_ok:
        with st.expander("Diagnóstico do servidor", expanded=True):
            st.warning(ambiente_erro or "Falha desconhecida")
        st.error("O componente de acesso ao YouTube não foi inicializado. Confira o diagnóstico acima.")

    url = st.text_input("URL do YouTube", placeholder="https://www.youtube.com/watch?v=...")

    col1, col2 = st.columns([3, 1])
    with col1:
        analisar = st.button("🔎 Analisar Vídeo", type="primary", use_container_width=True)
    with col2:
        diagnostico = st.button("🧪 Diagnóstico", use_container_width=True)

    if diagnostico:
        if not url.strip():
            st.warning("Cole uma URL antes de executar o diagnóstico.")
        elif not ambiente_ok:
            st.error("Corrija primeiro o problema mostrado no diagnóstico do servidor.")
        else:
            with st.spinner("Executando diagnóstico completo do yt-dlp + PO Token... "):
                try:
                    info_diag, diag = diagnosticar_video(url)
                    st.session_state["diagnostico"] = diag
                    st.session_state["video_info"] = info_diag
                    st.session_state["video_url"] = normalizar_url(url)
                except DiagnosticoErro as exc:
                    st.session_state["diagnostico"] = exc.diagnostico
                except Exception as exc:
                    st.session_state["diagnostico"] = {
                        "erro": f"{type(exc).__name__}: {exc}",
                        "erro_extracao": traceback.format_exc(),
                    }

    if analisar:
        if not url.strip():
            st.warning("Cole uma URL antes de continuar.")
        elif not ambiente_ok:
            st.error("Corrija primeiro o problema mostrado no diagnóstico do servidor.")
        else:
            with st.spinner("Analisando o vídeo..."):
                try:
                    info = extrair_info_video(url)
                    st.session_state["video_info"] = info
                    st.session_state["video_url"] = normalizar_url(url)
                except Exception as exc:
                    st.error(str(exc))

    diag = st.session_state.get("diagnostico")
    if diag:
        with st.expander("🧪 Diagnóstico técnico completo", expanded=True):
            if diag.get("erro"):
                st.error(diag["erro"])
            if diag.get("erro_extracao"):
                st.error("A extração falhou, mas todo o diagnóstico do ambiente foi preservado.")
                st.code(diag["erro_extracao"], language="text")

            st.subheader("1. Ambiente")
            st.json({
                "yt_dlp": diag.get("yt_dlp"),
                "python": diag.get("python"),
                "python_executable": diag.get("python_executable"),
                "platform": diag.get("platform"),
                "machine": diag.get("machine"),
                "cwd": diag.get("cwd"),
                "home": diag.get("home"),
            })

            st.subheader("2. BgUtils / Deno / servidor HTTP")
            st.json({
                "deno": diag.get("deno"),
                "bgutil_dir": diag.get("bgutil_dir"),
                "bgutil_server": diag.get("bgutil_server"),
                "generate_once_js": diag.get("script"),
                "generate_once_existe": diag.get("script_existe"),
                "generate_once_bytes": diag.get("script_bytes"),
                "http_server": diag.get("bgutil_http"),
            })

            st.subheader("3. Testes de PO Token")
            st.json({
                "servidor_http": diag.get("bgutil_http"),
                "geracao_direta": diag.get("pot_direto"),
                "versao_generate_once": diag.get("script_version_deno"),
            })

            st.subheader("4. Configuração efetiva do yt-dlp")
            st.json(diag.get("config_efetiva", {}))

            st.subheader("5. Descoberta do plugin")
            st.write("**Diretórios de plugins vistos pelo yt-dlp:**")
            st.code("\n".join(diag.get("plugin_dirs") or ["nenhum detectado"]))
            st.json(diag.get("plugin_modules", {}))

            st.subheader("6. Arquivos relevantes")
            for raiz, dados in (diag.get("filesystem") or {}).items():
                with st.expander(f"`{raiz}` — {'EXISTE' if dados.get('existe') else 'não existe'}"):
                    st.json(dados)

            st.subheader("7. Runtimes e ferramentas")
            st.json(diag.get("commands", {}))

            st.subheader("8. Variáveis de ambiente relevantes")
            st.json(diag.get("environment", {}))

            st.subheader("9. Logs completos do yt-dlp / BgUtils / PO Token")
            st.code("\n".join(diag.get("logs_completos") or ["Nenhum log foi capturado."]), language="text")

            st.subheader("10. Resumo da extração")
            st.json(diag.get("info_resumo", {}))

            st.subheader("11. Formatos retornados pelo YouTube")
            formatos_diag = diag.get("formatos") or []
            if formatos_diag:
                st.dataframe(formatos_diag, use_container_width=True, hide_index=True)
            else:
                st.warning("Nenhum formato foi retornado.")

            diagnostico_json = json.dumps(diag, ensure_ascii=False, indent=2, default=str)
            st.download_button(
                "📄 Baixar diagnóstico completo (JSON)",
                data=diagnostico_json.encode("utf-8"),
                file_name="diagnostico_yt_dlp_bgutil.json",
                mime="application/json",
                use_container_width=True,
            )

    info = st.session_state.get("video_info")
    if not info:
        return

    st.divider()
    mostrar_metadados(info)

    formatos = obter_formatos_disponiveis(info)
    st.divider()
    st.subheader("⚙️ Download")

    tipo = st.radio(
        "Tipo de mídia",
        ["Vídeo MP4 (Com Áudio)", "Apenas Áudio MP3"],
        horizontal=True,
    )

    if tipo == "Vídeo MP4 (Com Áudio)":
        opcoes = montar_opcoes_resolucao(formatos["alturas"])
        if not opcoes:
            st.warning("Nenhum stream de vídeo compatível foi encontrado.")
            return

        escolha = st.selectbox("Qualidade", list(opcoes.keys()))
        qualidade = opcoes[escolha]
        formato = "video"
        maxima = next(iter(opcoes.keys()))
        st.caption(
            f"Máxima disponível neste vídeo: **{maxima}**. "
            f"O servidor procura o melhor vídeo até {escolha} e combina com o melhor áudio usando FFmpeg."
        )
    else:
        opcoes = {
            "320 kbps": 320,
            "192 kbps": 192,
            "128 kbps": 128,
        }
        escolha = st.selectbox("Qualidade do áudio", list(opcoes))
        qualidade = opcoes[escolha]
        formato = "audio"
        st.caption(f"O áudio será convertido para MP3 em {qualidade} kbps com FFmpeg.")

    st.divider()
    if st.button("⬇️ Baixar arquivo", type="primary", use_container_width=True):
        if not ambiente_ok:
            st.error("O componente de acesso ao YouTube não está disponível.")
            return

        progress = DownloadProgress()
        try:
            with tempfile.TemporaryDirectory(prefix="yt_") as temp_dir:
                arquivo = baixar_e_converter(
                    st.session_state["video_url"],
                    formato,
                    qualidade,
                    temp_dir,
                    progress,
                )

                tamanho = arquivo.stat().st_size
                if tamanho > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("O arquivo final ultrapassou o limite de 500 MB.")

                dados = arquivo.read_bytes()
                titulo = sanitizar_nome_arquivo(info.get("title") or "download")

                if formato == "video":
                    nome = f"{titulo}.mp4"
                    mime = "video/mp4"
                else:
                    nome = f"{titulo}.mp3"
                    mime = "audio/mpeg"

                st.success(f"Arquivo pronto — {formatar_bytes(tamanho)}")
                st.download_button(
                    "💾 Salvar no computador",
                    data=dados,
                    file_name=nome,
                    mime=mime,
                    use_container_width=True,
                )
        except Exception as exc:
            st.error(str(exc))


if __name__ == "__main__":
    main()
