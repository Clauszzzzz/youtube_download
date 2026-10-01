import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st
import yt_dlp


# ============================================================
# CONFIGURAÇÃO
# ============================================================

st.set_page_config(
    page_title="YouTube Downloader",
    page_icon="🎬",
    layout="wide",
)

MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB


# ============================================================
# UTILITÁRIOS
# ============================================================

def formatar_duracao(segundos: Optional[int]) -> str:
    if not segundos:
        return "N/A"

    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)

    if horas:
        return f"{horas:02d}:{minutos:02d}:{segundos:02d}"
    return f"{minutos:02d}:{segundos:02d}"


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
    if valor is None:
        return "N/A"
    return f"{formatar_bytes(valor)}/s"


def formatar_eta(segundos: Optional[int]) -> str:
    if segundos is None:
        return "N/A"

    segundos = max(0, int(segundos))
    horas, resto = divmod(segundos, 3600)
    minutos, segundos = divmod(resto, 60)

    if horas:
        return f"{horas}h {minutos:02d}m"
    if minutos:
        return f"{minutos}m {segundos:02d}s"
    return f"{segundos}s"


def sanitizar_nome_arquivo(nome: str, limite: int = 180) -> str:
    nome = nome or "download"
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", nome)
    nome = re.sub(r"\s+", " ", nome).strip().rstrip(". ")

    if not nome:
        nome = "download"

    return nome[:limite]


def normalizar_url(url: str) -> str:
    url = url.strip()

    # Evita alguns formatos de URL de playlist quando o usuário
    # cola uma URL contendo parâmetros adicionais.
    # O noplaylist=True continua sendo a proteção principal.
    return url


# ============================================================
# EXTRAÇÃO
# ============================================================

def extrair_info_video(url: str) -> Dict[str, Any]:
    """Obtém metadados sem baixar o vídeo."""
    if not url or not url.strip():
        raise ValueError("Informe uma URL do YouTube.")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalizar_url(url), download=False)

        if not info:
            raise RuntimeError("O YouTube não retornou informações para essa URL.")

        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            first = next((item for item in entries if item), None)

            if not first:
                raise RuntimeError("A URL não contém um vídeo acessível.")

            # Reextrai o primeiro vídeo de uma playlist para obter
            # todos os metadados e formatos.
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(first.get("webpage_url"), download=False)

        return info

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)

        if "Private video" in msg:
            raise RuntimeError("O vídeo é privado e não pode ser acessado.") from exc

        if "Sign in" in msg or "age" in msg.lower() or "confirm your age" in msg.lower():
            raise RuntimeError(
                "O vídeo possui restrição de idade ou exige autenticação."
            ) from exc

        if "not available" in msg.lower() or "unavailable" in msg.lower():
            raise RuntimeError(
                "O vídeo está indisponível, foi removido ou possui restrição regional."
            ) from exc

        if "timed out" in msg.lower() or "timeout" in msg.lower():
            raise RuntimeError(
                "A conexão com o YouTube expirou. Tente novamente."
            ) from exc

        raise RuntimeError(f"Não foi possível acessar o vídeo: {msg}") from exc

    except Exception as exc:
        raise RuntimeError(f"Erro ao analisar o vídeo: {exc}") from exc


# ============================================================
# FORMATOS
# ============================================================

def obter_formatos_disponiveis(info_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analisa os formatos e retorna apenas resoluções de vídeo
    que possuem stream de vídeo MP4 disponível.

    O download efetivo usa o seletor do yt-dlp para combinar
    vídeo + áudio.
    """
    formatos = info_dict.get("formats", [])

    resolucoes = set()
    videos_mp4: List[Dict[str, Any]] = []
    audios: List[Dict[str, Any]] = []

    for fmt in formatos:
        ext = fmt.get("ext")
        height = fmt.get("height")
        vcodec = fmt.get("vcodec")
        acodec = fmt.get("acodec")

        if ext == "mp4" and vcodec not in (None, "none") and height:
            resolucoes.add(int(height))
            videos_mp4.append(fmt)

        if (
            acodec not in (None, "none")
            and vcodec in (None, "none")
        ):
            audios.append(fmt)

    return {
        "resolucoes": sorted(resolucoes, reverse=True),
        "video": videos_mp4,
        "audio": audios,
    }


# ============================================================
# PROGRESSO
# ============================================================

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
                f"**Velocidade:** {formatar_velocidade(data.get('speed'))}  •  "
                f"**Tempo restante:** {formatar_eta(data.get('eta'))}"
            )

        elif status == "finished":
            self.progress_bar.progress(100)
            self.status.info(
                "Download concluído. Finalizando merge/conversão com FFmpeg..."
            )


# ============================================================
# DOWNLOAD
# ============================================================

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
    """
    Executa download e pós-processamento.

    formato_escolhido:
      - video
      - audio

    qualidade:
      - vídeo: altura máxima em pixels
      - áudio: bitrate em kbps
    """
    if progress:
        progress.iniciar()

    common = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress.hook] if progress else [],
        "outtmpl": str(Path(pasta_destino) / "%(title).180s.%(ext)s"),
        # O EJS é necessário para o suporte atual do YouTube.
        # Deno é fornecido pelo pacote Python "deno".
        "js_runtimes": {"deno": {}},
        "remote_components": {"ejs": ["github"]},
    }

    if formato_escolhido == "video":
        # Prioriza vídeo MP4 + áudio M4A para manter o contêiner MP4.
        # A opção "best" é um fallback para vídeos que não exponham
        # uma combinação separada compatível.
        common.update({
            "format": (
                f"bestvideo[height<={qualidade}][ext=mp4]+"
                f"bestaudio[ext=m4a]/"
                f"bestvideo[height<={qualidade}][ext=mp4]+"
                f"bestaudio/"
                f"best[height<={qualidade}][ext=mp4]/"
                f"best[height<={qualidade}]"
            ),
            "merge_output_format": "mp4",
        })

        extensao_final = ".mp4"

    elif formato_escolhido == "audio":
        common.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(qualidade),
                }
            ],
        })

        extensao_final = ".mp3"

    else:
        raise ValueError("Tipo de mídia inválido.")

    try:
        with yt_dlp.YoutubeDL(common) as ydl:
            ydl.download([normalizar_url(url)])

    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)

        if "ffmpeg" in msg.lower() or "ffprobe" in msg.lower():
            raise RuntimeError(
                "O FFmpeg/FFprobe não está disponível no servidor. "
                "Verifique packages.txt e reinicie o deploy."
            ) from exc

        if "Sign in" in msg or "age" in msg.lower():
            raise RuntimeError(
                "O YouTube exige autenticação/restrição de idade para este vídeo."
            ) from exc

        if "not available" in msg.lower() or "unavailable" in msg.lower():
            raise RuntimeError(
                "O vídeo ficou indisponível durante o download."
            ) from exc

        raise RuntimeError(f"Falha no download: {msg}") from exc

    except Exception as exc:
        raise RuntimeError(f"Erro durante o processamento: {exc}") from exc

    return _arquivo_final(pasta_destino, extensao_final)


# ============================================================
# UI
# ============================================================

def mostrar_metadados(info: Dict[str, Any]) -> None:
    esquerda, direita = st.columns([1, 2])

    with esquerda:
        thumbnail = info.get("thumbnail")
        if thumbnail:
            st.image(thumbnail, use_container_width=True)

    with direita:
        st.subheader(info.get("title") or "Título indisponível")

        canal = info.get("channel") or info.get("uploader") or "N/A"
        st.write(f"**Canal:** {canal}")
        st.write(f"**Duração:** {formatar_duracao(info.get('duration'))}")
        st.write(
            f"**Visualizações:** "
            f"{formatar_visualizacoes(info.get('view_count'))}"
        )


def mensagem_erro(exc: Exception) -> None:
    texto = str(exc)

    if "HTTP Error 429" in texto:
        st.error(
            "O YouTube limitou temporariamente as requisições deste servidor "
            "(HTTP 429). Aguarde e tente novamente."
        )
        return

    st.error(texto)


def main() -> None:
    st.title("🎬 YouTube Downloader")
    st.caption(
        "MP4 com áudio ou extração de áudio MP3 — processamento realizado no servidor."
    )

    st.info(
        "Use somente conteúdo que você tenha autorização para baixar "
        "ou que seja permitido pelos termos e direitos aplicáveis."
    )

    url = st.text_input(
        "URL do YouTube",
        placeholder="https://www.youtube.com/watch?v=...",
    )

    if st.button(
        "🔎 Analisar Vídeo",
        type="primary",
        use_container_width=True,
    ):
        if not url.strip():
            st.warning("Cole uma URL antes de continuar.")
        else:
            with st.spinner("Analisando o vídeo..."):
                try:
                    info = extrair_info_video(url)
                    st.session_state["video_info"] = info
                    st.session_state["video_url"] = normalizar_url(url)
                except Exception as exc:
                    mensagem_erro(exc)

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
        resolucoes = formatos["resolucoes"]

        if not resolucoes:
            st.warning(
                "Nenhum stream de vídeo MP4 foi encontrado para este vídeo."
            )
            return

        labels = []
        mapa = {}

        for altura in resolucoes:
            if altura >= 2160:
                nome = f"{altura}p / 4K"
            elif altura >= 1440:
                nome = f"{altura}p / 2K"
            else:
                nome = f"{altura}p"

            labels.append(nome)
            mapa[nome] = altura

        escolha = st.selectbox("Qualidade", labels)
        qualidade = mapa[escolha]
        formato = "video"

        st.caption(
            "O servidor combina o melhor vídeo MP4 disponível até a resolução "
            "selecionada com o melhor áudio e usa FFmpeg para o merge."
        )

    else:
        opcoes = {
            "320 kbps — Alta qualidade": 320,
            "192 kbps — Média qualidade": 192,
            "128 kbps — Padrão": 128,
        }

        escolha = st.selectbox("Qualidade do áudio", list(opcoes))
        qualidade = opcoes[escolha]
        formato = "audio"

        st.caption(
            f"O áudio será convertido para MP3 em {qualidade} kbps com FFmpeg."
        )

    st.divider()

    if st.button(
        "⬇️ Baixar arquivo",
        type="primary",
        use_container_width=True,
    ):
        progress = DownloadProgress()

        try:
            with tempfile.TemporaryDirectory(prefix="yt_") as temp_dir:
                arquivo = baixar_e_converter(
                    url=st.session_state["video_url"],
                    formato_escolhido=formato,
                    qualidade=qualidade,
                    pasta_destino=temp_dir,
                    progress=progress,
                )

                tamanho = arquivo.stat().st_size

                if tamanho > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        "O arquivo final ultrapassou o limite de 500 MB "
                        "definido para esta aplicação."
                    )

                dados = arquivo.read_bytes()

                titulo = sanitizar_nome_arquivo(
                    info.get("title") or "download"
                )

                if formato == "video":
                    nome = f"{titulo}.mp4"
                    mime = "video/mp4"
                else:
                    nome = f"{titulo}.mp3"
                    mime = "audio/mpeg"

                st.success(
                    f"Arquivo pronto — {formatar_bytes(tamanho)}"
                )

                st.download_button(
                    "💾 Salvar no computador",
                    data=dados,
                    file_name=nome,
                    mime=mime,
                    use_container_width=True,
                )

                st.caption(
                    "O arquivo temporário do servidor será removido ao final desta execução."
                )

        except Exception as exc:
            mensagem_erro(exc)


if __name__ == "__main__":
    main()
