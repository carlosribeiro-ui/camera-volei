"""
Câmera Vôlei - grava o jogo, rastreia a bola e salva cortes do último minuto.

Teclas (com a janela da câmera em foco):
  ESPAÇO  salva um corte dos últimos N segundos (padrão 60)
  R       liga/desliga a gravação da partida inteira
  F       liga/desliga o modo "acompanhar a bola" (zoom digital)
  T       mostra/esconde a trajetória da bola
  M       mostra a máscara de detecção (para ajustar a cor)
  Clique  clique em cima da bola para ensinar a cor dela ao programa
  Q / ESC sai
"""

import argparse
import collections
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

# abre webcams pelo Media Foundation muito mais rápido (precisa vir antes do import cv2)
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
import cv2
import numpy as np

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQUIVO_CONFIG = os.path.join(PASTA, "config.json")

CONFIG_PADRAO = {
    "camera": 0,                      # índice da webcam (0 = primeira)
    "largura": 1280,
    "altura": 720,
    "fps": 30,
    "segundos_do_corte": 60,          # duração do corte ao apertar ESPAÇO
    "pasta_cortes": "cortes",
    "pasta_partidas": "partidas",
    "gravar_partida_inteira": True,   # começa gravando a partida inteira ao abrir
    "qualidade_crf": 23,              # H.264: menor = melhor e maior (18-28)
    "preset_x264": "veryfast",        # ultrafast/superfast/veryfast/faster/fast
    "qualidade_jpeg_buffer": 85,
    "desenhar_trajetoria_nas_gravacoes": True,
    "acompanhar_nas_gravacoes": False,  # aplica o zoom que segue a bola também no arquivo
    "zoom_acompanhar": 1.8,
    "cor_bola_hsv_min": [18, 90, 110],  # amarelo (bola Mikasa amarela/azul)
    "cor_bola_hsv_max": [38, 255, 255],
    "usar_cor": True,
    "usar_movimento": True,
    "raio_min_px": 3,                 # medido na imagem reduzida para 640 px de largura
    "raio_max_px": 40,
    "pontos_trajetoria": 45,
}


# ---------------------------------------------------------------- utilidades

def carregar_config():
    cfg = dict(CONFIG_PADRAO)
    if os.path.exists(ARQUIVO_CONFIG):
        with open(ARQUIVO_CONFIG, encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        salvar_config(cfg)
    return cfg


def salvar_config(cfg):
    with open(ARQUIVO_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def achar_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def caminho_saida(pasta, prefixo):
    pasta = os.path.join(PASTA, pasta) if not os.path.isabs(pasta) else pasta
    os.makedirs(pasta, exist_ok=True)
    nome = f"{prefixo}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mp4"
    return os.path.join(pasta, nome)


# Cada webcam aceita modos diferentes. A C270 só dá 720p a 30 fps em MJPG pelo DirectShow;
# já muitas câmeras de notebook entregam imagem PRETA a 1 fps nesse modo e funcionam
# bem pelo Media Foundation (MSMF). Por isso o programa testa os modos em ordem e
# lembra no config.json o que funcionou em cada câmera.
MODOS_CAMERA = {
    # nome: (backend, forçar MJPG, pedir a resolução do config)
    "dshow_mjpg": ("DSHOW", True, True),
    "msmf": ("MSMF", False, True),
    "dshow": ("DSHOW", False, True),
    "dshow_padrao": ("DSHOW", False, False),
}


def _abrir_no_modo(cfg, modo):
    nome_backend, mjpg, hd = MODOS_CAMERA[modo]
    cap = cv2.VideoCapture(cfg["camera"], getattr(cv2, "CAP_" + nome_backend))
    if not cap.isOpened():
        cap.release()
        return None
    if mjpg:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    if hd:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["largura"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["altura"])
        cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def _avaliar(cap, segundos):
    """Lê a câmera por um tempinho e devolve (fps, brilho médio do último quadro)."""
    t0 = time.monotonic()
    n = 0
    while n < 5 and time.monotonic() - t0 < 1.5:  # aquecimento
        ok, _ = cap.read()
        if not ok:
            return 0.0, 0.0
        n += 1
    t1, n, ultimo = time.monotonic(), 0, None
    while time.monotonic() - t1 < segundos:
        ok, q = cap.read()
        if not ok:
            break
        n, ultimo = n + 1, q
    fps = n / max(time.monotonic() - t1, 1e-3)
    return fps, (float(ultimo.mean()) if ultimo is not None else 0.0)


def abrir_camera(cfg, fonte):
    if fonte is not None:
        return cv2.VideoCapture(fonte)
    so = platform.system()
    if so != "Windows":
        backend = {"Darwin": cv2.CAP_AVFOUNDATION, "Linux": cv2.CAP_V4L2}.get(so, cv2.CAP_ANY)
        cap = cv2.VideoCapture(cfg["camera"], backend)
        if not cap.isOpened():
            cap = cv2.VideoCapture(cfg["camera"])
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg["largura"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["altura"])
        cap.set(cv2.CAP_PROP_FPS, cfg["fps"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        return cap

    lembrados = cfg.setdefault("modos_camera", {})
    chave = str(cfg["camera"])
    ordem = list(MODOS_CAMERA)
    lembrado = lembrados.get(chave)
    if lembrado in ordem:
        ordem.remove(lembrado)
        ordem.insert(0, lembrado)

    melhor = None  # (imagem ok?, fps, modo)
    for modo in ordem:
        cap = _abrir_no_modo(cfg, modo)
        if cap is None:
            continue
        fps, brilho = _avaliar(cap, 0.6 if modo == lembrado else 1.0)
        imagem_ok = brilho > 1.5  # quadro todo preto = modo não suportado
        print(f"[camera] {chave} modo {modo}: {fps:.1f} fps, brilho {brilho:.0f}")
        if imagem_ok and fps >= (10 if modo == lembrado else 15):
            lembrados[chave] = modo
            return cap
        cap.release()
        if fps > 0 and (melhor is None or (imagem_ok, fps) > melhor[:2]):
            melhor = (imagem_ok, fps, modo)

    if melhor is None:  # nenhum modo abriu
        return cv2.VideoCapture(cfg["camera"], cv2.CAP_DSHOW)
    lembrados[chave] = melhor[2]
    return _abrir_no_modo(cfg, melhor[2]) or cv2.VideoCapture(cfg["camera"], cv2.CAP_DSHOW)


def listar_cameras():
    """Nomes das webcams na ordem do DirectShow (a mesma dos índices 0, 1, 2...). Só Windows."""
    if platform.system() != "Windows":
        return []
    exe = achar_ffmpeg()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow",
                            "-i", "dummy"], capture_output=True, text=True, errors="replace",
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return []
    nomes, secao_video = [], False
    for linha in r.stderr.splitlines():
        if "DirectShow video devices" in linha:
            secao_video = True
        elif "DirectShow audio devices" in linha:
            secao_video = False
        elif '"' in linha and "Alternative name" not in linha:
            if "(video)" in linha or (secao_video and "(audio)" not in linha):
                nomes.append(linha.split('"')[1])
    return nomes


# ------------------------------------------------------- gravação de vídeo

class GravadorVideo:
    """Recebe quadros JPEG com horário de captura e grava um MP4 a fps constante.

    Se a câmera entregar menos quadros que o esperado (pouca luz, por exemplo),
    quadros são repetidos para o vídeo não ficar acelerado.
    """

    def __init__(self, caminho, fps, tamanho, cfg, ffmpeg, a_prova_de_queda=False):
        self.caminho = caminho
        self.fps = fps
        self.t0 = None
        self.escritos = 0
        self.proc = None
        self.writer = None
        if ffmpeg:
            movflags = ("+frag_keyframe+empty_moov+default_base_moof"
                        if a_prova_de_queda else "+faststart")
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "image2pipe", "-vcodec", "mjpeg", "-framerate", str(fps),
                   "-i", "-",
                   "-c:v", "libx264", "-preset", cfg["preset_x264"],
                   "-crf", str(cfg["qualidade_crf"]), "-pix_fmt", "yuv420p",
                   "-g", str(int(fps * 2)), "-movflags", movflags, caminho]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, creationflags=flags)
        else:
            # Sem ffmpeg: usa o codificador do próprio OpenCV (arquivo ~3x maior).
            self.writer = cv2.VideoWriter(caminho, cv2.VideoWriter_fourcc(*"mp4v"),
                                          fps, tamanho)

    def _escrever(self, jpeg):
        if self.proc:
            self.proc.stdin.write(jpeg)
        else:
            self.writer.write(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR))
        self.escritos += 1

    def adicionar(self, t, jpeg):
        """Escreve o quadro (repetido se preciso) e devolve o índice do 1º quadro escrito
        e quantos foram escritos."""
        if self.t0 is None:
            self.t0 = t
        inicio = self.escritos
        alvo = int(round((t - self.t0) * self.fps)) + 1
        while self.escritos < alvo:
            self._escrever(jpeg)
        return inicio, self.escritos - inicio

    def fechar(self):
        if self.proc:
            self.proc.stdin.close()
            self.proc.wait()
        elif self.writer:
            self.writer.release()


def salvar_corte(quadros, caminho, fps, tamanho, cfg, ffmpeg, avisar):
    """Roda em uma thread separada para não travar a câmera."""
    try:
        g = GravadorVideo(caminho, fps, tamanho, cfg, ffmpeg)
        for t, jpeg in quadros:
            g.adicionar(t, jpeg)
        g.fechar()
        avisar(f"Corte salvo: {os.path.basename(caminho)}")
        print(f"[corte] salvo em {caminho}")
    except Exception as e:  # noqa: BLE001
        avisar("Erro ao salvar corte (veja o terminal)")
        print(f"[corte] erro: {e}", file=sys.stderr)


class GravacaoPartida:
    """Gravação contínua em uma thread; o quadro entra numa fila e é escrito em paralelo."""

    def __init__(self, cfg, fps, tamanho, ffmpeg, csv_bola=None):
        """csv_bola: função caminho_do_video -> caminho do CSV. Se vier, grava a posição da
        bola de cada quadro do vídeo (Frame,Visibility,X,Y,Radius), o formato do motor de IA."""
        self.caminho = caminho_saida(cfg["pasta_partidas"], "partida")
        self.csv = None
        if csv_bola:
            caminho_csv = csv_bola(self.caminho)
            os.makedirs(os.path.dirname(caminho_csv), exist_ok=True)
            self.csv = open(caminho_csv, "w", encoding="utf-8", newline="")
            self.csv.write("Frame,Visibility,X,Y,Radius\n")
        self.fila = queue.Queue(maxsize=int(fps * 10))
        self.gravador = GravadorVideo(self.caminho, fps, tamanho, cfg, ffmpeg,
                                      a_prova_de_queda=True)
        self.inicio = time.monotonic()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        print(f"[partida] gravando em {self.caminho}")

    def _loop(self):
        try:
            while True:
                item = self.fila.get()
                if item is None:
                    break
                t, jpeg, bola = item
                inicio, n = self.gravador.adicionar(t, jpeg)
                if self.csv and n:
                    v, x, y, r = ((1, int(bola[0]), int(bola[1]), int(bola[2])) if bola
                                  else (0, -1, -1, 0))
                    self.csv.write("".join(f"{inicio + k},{v},{x},{y},{r}\n" for k in range(n)))
        except (BrokenPipeError, OSError) as e:
            self.erro = e
            print(f"[partida] a gravação parou: {e}", file=sys.stderr)
        finally:
            try:
                self.gravador.fechar()
            except (BrokenPipeError, OSError):
                pass
            if self.csv:
                self.csv.close()

    erro = None

    def adicionar(self, t, jpeg, bola=None):
        try:
            self.fila.put_nowait((t, jpeg, bola))
        except queue.Full:
            pass  # disco/CPU lentos: pula o quadro (o próximo é repetido no lugar)

    def parar(self):
        while self.thread.is_alive():
            try:
                self.fila.put(None, timeout=0.5)
                break
            except queue.Full:
                pass
        self.thread.join()
        print(f"[partida] arquivo fechado: {self.caminho}")


# ------------------------------------------------------- rastreamento da bola

class RastreadorBola:
    LARGURA_PROC = 640

    def __init__(self, cfg):
        self.cfg = cfg
        self.fundo = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32,
                                                        detectShadows=False)
        self.kalman = None
        self.perdidos = 0
        self.trajetoria = collections.deque(maxlen=cfg["pontos_trajetoria"])
        self.ultima_mascara = None
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def _novo_kalman(self, x, y):
        k = cv2.KalmanFilter(4, 2)
        k.transitionMatrix = np.array([[1, 0, 1, 0], [0, 1, 0, 1],
                                       [0, 0, 1, 0], [0, 0, 0, 1]], np.float32)
        k.measurementMatrix = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], np.float32)
        k.processNoiseCov = np.eye(4, dtype=np.float32) * 0.5
        k.measurementNoiseCov = np.eye(2, dtype=np.float32) * 2
        k.statePost = np.array([[x], [y], [0], [0]], np.float32)
        k.errorCovPost = np.eye(4, dtype=np.float32) * 10
        return k

    def mascara(self, pequeno):
        cfg = self.cfg
        m = None
        if cfg["usar_movimento"]:
            mov = self.fundo.apply(pequeno)
            mov = cv2.dilate(mov, self.kernel, iterations=2)
            m = mov
        if cfg["usar_cor"]:
            hsv = cv2.cvtColor(cv2.GaussianBlur(pequeno, (5, 5), 0), cv2.COLOR_BGR2HSV)
            lo = np.array(cfg["cor_bola_hsv_min"], np.uint8)
            hi = np.array(cfg["cor_bola_hsv_max"], np.uint8)
            if lo[0] <= hi[0]:
                cor = cv2.inRange(hsv, lo, hi)
            else:  # faixa de matiz que dá a volta (vermelho)
                cor = cv2.inRange(hsv, lo, np.array([179, hi[1], hi[2]], np.uint8)) | \
                      cv2.inRange(hsv, np.array([0, lo[1], lo[2]], np.uint8), hi)
            m = cor if m is None else cv2.bitwise_and(cor, m)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, self.kernel)
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, self.kernel)
        return m

    def atualizar(self, quadro):
        """Retorna (x, y, raio) em pixels do quadro original, ou None."""
        h, w = quadro.shape[:2]
        esc = self.LARGURA_PROC / w
        pequeno = cv2.resize(quadro, (self.LARGURA_PROC, int(h * esc)))
        m = self.mascara(pequeno)
        self.ultima_mascara = m

        previsto = None
        if self.kalman is not None:
            p = self.kalman.predict()
            previsto = (float(p[0, 0]), float(p[1, 0]))

        contornos, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        melhor, melhor_nota = None, -1e9
        for c in contornos:
            area = cv2.contourArea(c)
            (cx, cy), r = cv2.minEnclosingCircle(c)
            if r < self.cfg["raio_min_px"] or r > self.cfg["raio_max_px"] or area < 6:
                continue
            circularidade = area / (np.pi * r * r)
            if circularidade < 0.45:
                continue
            nota = circularidade * 2 + min(area, 400) / 400
            if previsto is not None:
                dist = np.hypot(cx - previsto[0], cy - previsto[1])
                limite = 60 + 20 * self.perdidos
                if dist > limite:
                    continue
                nota -= dist / limite
            if nota > melhor_nota:
                melhor, melhor_nota = (cx, cy, r), nota

        if melhor is not None:
            cx, cy, r = melhor
            if self.kalman is None:
                self.kalman = self._novo_kalman(cx, cy)
            else:
                self.kalman.correct(np.array([[cx], [cy]], np.float32))
            self.perdidos = 0
            s = self.kalman.statePost
            pos = (float(s[0, 0]) / esc, float(s[1, 0]) / esc, r / esc)
            self.trajetoria.append((int(pos[0]), int(pos[1])))
            return pos

        self.perdidos += 1
        if self.perdidos > 15:  # meio segundo sem ver a bola: esquece a trajetória
            self.kalman = None
            self.trajetoria.clear()
        return None

    def desenhar(self, img, pos):
        pts = list(self.trajetoria)
        n = len(pts)
        for i in range(1, n):
            esp = max(1, int(1 + 5 * i / n))
            cv2.line(img, pts[i - 1], pts[i], (0, 200, 255), esp, cv2.LINE_AA)
        if pos is not None:
            x, y, r = pos
            cv2.circle(img, (int(x), int(y)), int(max(r, 6)) + 4, (0, 255, 0), 2, cv2.LINE_AA)


class Acompanhador:
    """Zoom digital que segue a bola suavemente (a C270 não se move sozinha)."""

    def __init__(self, zoom):
        self.zoom = zoom
        self.cx = self.cy = None
        self.z = 1.0
        self.sem_bola = 0

    def aplicar(self, img, pos):
        h, w = img.shape[:2]
        if self.cx is None:
            self.cx, self.cy = w / 2, h / 2
        if pos is not None:
            self.sem_bola = 0
            alvo_x, alvo_y, alvo_z = pos[0], pos[1], self.zoom
        else:
            self.sem_bola += 1
            if self.sem_bola > 30:  # 1 s sem bola: abre para a quadra toda
                alvo_x, alvo_y, alvo_z = w / 2, h / 2, 1.0
            else:
                alvo_x, alvo_y, alvo_z = self.cx, self.cy, self.z
        self.cx += (alvo_x - self.cx) * 0.12
        self.cy += (alvo_y - self.cy) * 0.12
        self.z += (alvo_z - self.z) * 0.05
        cw, ch = w / self.z, h / self.z
        x0 = int(min(max(self.cx - cw / 2, 0), w - cw))
        y0 = int(min(max(self.cy - ch / 2, 0), h - ch))
        recorte = img[y0:y0 + int(ch), x0:x0 + int(cw)]
        return cv2.resize(recorte, (w, h), interpolation=cv2.INTER_LINEAR)


# ------------------------------------------------------------------ programa

def main():
    ap = argparse.ArgumentParser(description="Câmera para jogos de vôlei")
    ap.add_argument("--video", help="usar um arquivo de vídeo no lugar da webcam (teste)")
    ap.add_argument("--sem-janela", action="store_true", help="roda sem abrir janela (teste)")
    ap.add_argument("--corte-em", type=float, action="append", default=[],
                    help="simula ESPAÇO após N segundos (teste; pode repetir)")
    ap.add_argument("--duracao", type=float, help="encerra após N segundos (teste)")
    args = ap.parse_args()

    cv2.setNumThreads(1)  # menos CPU total; o trabalho por quadro é pequeno
    cfg = carregar_config()
    ffmpeg = achar_ffmpeg()
    if not ffmpeg:
        print("[aviso] ffmpeg não encontrado: os vídeos serão gravados em mp4v (maiores). "
              "Rode 'pip install imageio-ffmpeg' para resolver.")

    cap = abrir_camera(cfg, args.video)
    if not cap.isOpened():
        print("Não consegui abrir a câmera. Confira o cabo USB ou troque \"camera\" "
              "no config.json (0, 1, 2...).")
        return 1

    ok, quadro = cap.read()
    if not ok:
        print("A câmera abriu mas não enviou imagem.")
        return 1
    h, w = quadro.shape[:2]
    fps_arquivo = cap.get(cv2.CAP_PROP_FPS) if args.video else 0
    fps = int(round(fps_arquivo)) if fps_arquivo and fps_arquivo > 1 else cfg["fps"]
    print(f"Câmera: {w}x{h} @ {fps} fps")

    rastreador = RastreadorBola(cfg)
    acompanhador = Acompanhador(cfg["zoom_acompanhar"])
    buffer = collections.deque()
    partida = GravacaoPartida(cfg, fps, (w, h), ffmpeg) if cfg["gravar_partida_inteira"] else None

    estado = {"acompanhar": False, "trajetoria": True, "mascara": False,
              "aviso": "", "aviso_ate": 0.0}

    def avisar(txt):
        estado["aviso"], estado["aviso_ate"] = txt, time.monotonic() + 3

    janela = "Camera Volei"
    if not args.sem_janela:
        cv2.namedWindow(janela, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(janela, 1280, 720)
        ultimo = {"quadro": quadro, "exibido": quadro}

        def clique(evento, x, y, _flags, _param):
            if evento != cv2.EVENT_LBUTTONDOWN or estado["acompanhar"]:
                return
            img = ultimo["quadro"]
            ih, iw = img.shape[:2]
            dh, dw = ultimo["exibido"].shape[:2]
            x, y = int(x * iw / dw), int(y * ih / dh)
            area = img[max(0, y - 4):y + 5, max(0, x - 4):x + 5]
            hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV).reshape(-1, 3)
            hh, ss, vv = np.median(hsv, axis=0).astype(int)
            cfg["cor_bola_hsv_min"] = [int((hh - 10) % 180), max(0, ss - 70), max(0, vv - 80)]
            cfg["cor_bola_hsv_max"] = [int((hh + 10) % 180), 255, 255]
            cfg["usar_cor"] = ss > 50  # bola branca/cinza: cor não ajuda, usa só movimento
            salvar_config(cfg)
            avisar(f"Cor da bola aprendida (H={hh} S={ss} V={vv})" +
                   ("" if cfg["usar_cor"] else " - pouca cor, usando movimento"))

        cv2.setMouseCallback(janela, clique)

    inicio = time.monotonic()
    n_quadro = 0
    cortes_pendentes = sorted(args.corte_em)
    fps_medido, t_fps, n_fps = 0.0, time.monotonic(), 0
    deteccoes = 0

    while True:
        if n_quadro > 0:
            ok, quadro = cap.read()
            if not ok:
                break
        n_quadro += 1
        t = n_quadro / fps if args.video else time.monotonic()
        decorrido = t - (1 / fps if args.video else inicio)

        pos = rastreador.atualizar(quadro)
        deteccoes += pos is not None

        # quadro que vai para o buffer/gravação
        gravar = quadro
        if cfg["desenhar_trajetoria_nas_gravacoes"] and estado["trajetoria"]:
            gravar = quadro.copy()
            rastreador.desenhar(gravar, pos)
        if cfg["acompanhar_nas_gravacoes"] and estado["acompanhar"]:
            gravar = acompanhador.aplicar(gravar, pos)
        ok, jpeg = cv2.imencode(".jpg", gravar, [cv2.IMWRITE_JPEG_QUALITY,
                                                  cfg["qualidade_jpeg_buffer"]])
        jpeg = jpeg.tobytes()

        buffer.append((t, jpeg))
        while buffer and t - buffer[0][0] > cfg["segundos_do_corte"]:
            buffer.popleft()
        if partida:
            partida.adicionar(t, jpeg)

        pediu_corte = False
        if cortes_pendentes and decorrido >= cortes_pendentes[0]:
            cortes_pendentes.pop(0)
            pediu_corte = True

        n_fps += 1
        if time.monotonic() - t_fps >= 1:
            fps_medido = n_fps / (time.monotonic() - t_fps)
            t_fps, n_fps = time.monotonic(), 0

        tecla = -1
        if not args.sem_janela:
            if estado["mascara"] and rastreador.ultima_mascara is not None:
                tela = cv2.cvtColor(rastreador.ultima_mascara, cv2.COLOR_GRAY2BGR)
            else:
                tela = quadro.copy()
                if estado["trajetoria"]:
                    rastreador.desenhar(tela, pos)
                if estado["acompanhar"]:
                    tela = acompanhador.aplicar(tela, pos)
            desenhar_painel(tela, estado, buffer, partida, fps_medido, pos)
            ultimo["quadro"], ultimo["exibido"] = quadro, tela
            cv2.imshow(janela, tela)
            tecla = cv2.waitKey(1) & 0xFF
            if cv2.getWindowProperty(janela, cv2.WND_PROP_VISIBLE) < 1:
                break
        elif estado["acompanhar"] and not cfg["acompanhar_nas_gravacoes"]:
            acompanhador.aplicar(quadro, pos)

        if tecla == ord(" ") or pediu_corte:
            quadros = list(buffer)
            caminho = caminho_saida(cfg["pasta_cortes"], "corte")
            dur = quadros[-1][0] - quadros[0][0]
            avisar(f"Salvando corte de {dur:.0f} s...")
            threading.Thread(target=salvar_corte, daemon=False,
                             args=(quadros, caminho, fps, (w, h), cfg, ffmpeg, avisar)).start()
        elif tecla in (ord("r"), ord("R")):
            if partida:
                partida.parar()
                avisar(f"Partida salva: {os.path.basename(partida.caminho)}")
                partida = None
            else:
                partida = GravacaoPartida(cfg, fps, (w, h), ffmpeg)
                avisar("Gravando a partida inteira")
        elif tecla in (ord("f"), ord("F")):
            estado["acompanhar"] = not estado["acompanhar"]
        elif tecla in (ord("t"), ord("T")):
            estado["trajetoria"] = not estado["trajetoria"]
        elif tecla in (ord("m"), ord("M")):
            estado["mascara"] = not estado["mascara"]
        elif tecla in (ord("q"), ord("Q"), 27):
            break

        if args.duracao and decorrido >= args.duracao:
            break

    cap.release()
    if partida:
        partida.parar()
    if not args.sem_janela:
        cv2.destroyAllWindows()
    for th in threading.enumerate():
        if th is not threading.current_thread() and not th.daemon:
            th.join()
    print(f"Quadros: {n_quadro}, bola detectada em {deteccoes} ({100 * deteccoes / max(n_quadro, 1):.0f}%)")
    return 0


def desenhar_painel(img, estado, buffer, partida, fps, pos):
    seg = buffer[-1][0] - buffer[0][0] if len(buffer) > 1 else 0
    linhas = [f"{fps:4.1f} fps | buffer {seg:4.0f} s | bola: {'sim' if pos else 'nao'}"]
    if partida:
        dur = int(time.monotonic() - partida.inicio)
        linhas[0] += f" | REC partida {dur // 60:02d}:{dur % 60:02d}"
    linhas.append("ESPACO corte  R partida  F acompanhar  T trajetoria  M mascara  clique=cor  Q sair")
    y = 28
    for txt in linhas:
        cv2.putText(img, txt, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, txt, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        y += 26
    if partida:
        cv2.circle(img, (img.shape[1] - 25, 25), 10, (0, 0, 255), -1)
    if estado["acompanhar"]:
        cv2.putText(img, "ACOMPANHANDO", (img.shape[1] - 200, 32), cv2.FONT_HERSHEY_SIMPLEX,
                    0.6, (0, 255, 255), 2, cv2.LINE_AA)
    if time.monotonic() < estado["aviso_ate"]:
        cv2.putText(img, estado["aviso"], (12, img.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 0, 0), 5, cv2.LINE_AA)
        cv2.putText(img, estado["aviso"], (12, img.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX,
                    0.8, (0, 255, 255), 2, cv2.LINE_AA)


if __name__ == "__main__":
    sys.exit(main())
