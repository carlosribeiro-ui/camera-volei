"""
Ponte entre o Câmera Vôlei e o motor open source que fica em motor_vball/.

motor_vball/ é uma cópia fiel do projeto "fast-volleyball-tracking-inference"
(https://github.com/asigatchov/fast-volleyball-tracking-inference, licença MIT).
Nada lá dentro é editado: este arquivo só chama as funções e os scripts originais.

- DetectorBola: acha a bola AO VIVO com o modelo VballNet, em blocos de 9 quadros,
  exatamente como inference_onnx_seq_gray_v2.py faz (load_onnx_model,
  preprocess_frames, run_inference, decode_predictions, model_radius_to_pixels,
  estimate_ball_radius, BallTrackState).
- gerar_melhores_momentos: depois do jogo, roda os scripts originais
  track_calculator.py (separa os rallies), track_processor.py (junta os rallies)
  e make_reels.py (vídeos verticais 9:16 seguindo a bola).
"""

import collections
import os
import queue
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

PASTA = os.path.dirname(os.path.abspath(__file__))
MOTOR = os.path.join(PASTA, "motor_vball")
MOTOR_SRC = os.path.join(MOTOR, "src")
MOTOR_MODELOS = os.path.join(MOTOR, "models")

# chave no config.json -> (nome na tela, arquivo do modelo em motor_vball/models)
MODELOS = {
    "ia_precisa": ("IA precisa (VballNet V4c)", "VballNetV4c_seq9_grayscale_20260908_213829.onnx"),
    "ia_rapida": ("IA rápida (VballNet FastV1)", "VballNetFastV1_seq9_grayscale_233_h288_w512.onnx"),
}

_motor = None
_erro_motor = None


def motor():
    """Importa o módulo de inferência original do projeto base (uma vez só)."""
    global _motor, _erro_motor
    if _motor is None and _erro_motor is None:
        try:
            if MOTOR_SRC not in sys.path:
                sys.path.insert(0, MOTOR_SRC)
            import inference_onnx_seq_gray_v2 as m
            _motor = m
        except Exception as e:  # noqa: BLE001 - faltou onnxruntime/pandas, etc.
            _erro_motor = f"{type(e).__name__}: {e}"
    return _motor


def disponivel():
    return motor() is not None


def erro_motor():
    motor()
    return _erro_motor


# ============================================================ ao vivo

class FiltroTrajetoria:
    """Descarta "bolas" falsas sem perder bola rápida nem devolução no primeiro toque.

    A IA às vezes confunde por um quadro alguém parado na lateral com a bola. Regras:
    1. Coisa parada não é bola em jogo: se algo é detectado no mesmo lugar ao longo de mais
       de 2 s, é ignorado (pessoa na lateral, poste). Bola segura para o saque também fica
       de fora, mas o rastro começa no lançamento.
    2. O ponto novo precisa estar perto de onde a bola deveria estar (posição + velocidade)
       ou de onde ela estava (para aceitar a volta seca de uma devolução/bloqueio). A folga
       cresce com a velocidade da bola.
    3. Um ponto longe de tudo só entra se aparecer 3 quadros seguidos E andando.
    """

    SALTO_MIN = 0.10       # folga mínima por quadro (fração da largura)
    FOLGA_VELOCIDADE = 1.4 # folga = 1,4 x a velocidade atual da bola
    CONFIRMAR = 3
    MOVIMENTO_MIN = 0.02   # gente andando anda bem menos que isso em 3 quadros; bola em jogo mais
    RAIO_PARADO = 0.015    # "mesmo lugar" para achar coisa parada (fração da largura)
    PARADO_MIN_DETECCOES = 5
    PARADO_QUADROS = 60    # ~2 s a 30 fps

    def __init__(self):
        self.ultimo = None
        self.vel = (0.0, 0.0)
        self.perdidos = 0
        self.candidatos = []
        self.novo_trecho = False  # True quando aceitou um ponto longe: o rastro recomeça
        self.quadro = 0
        self.historico = collections.deque(maxlen=300)  # (quadro, x, y) das detecções

    def _parado(self, pos, largura):
        """Algo detectado quase no MESMO ponto (a menos de 1,5% da largura) 5+ vezes ao longo
        de mais de 2 s é coisa parada. A bola em jogo até passa pela mesma região várias
        vezes, mas não cai repetidamente no mesmo ponto exato."""
        raio = self.RAIO_PARADO * largura
        vizinhos = [q for q, x, y in self.historico
                    if self.quadro - q <= 150 and abs(x - pos[0]) <= raio and abs(y - pos[1]) <= raio]
        self.historico.append((self.quadro, pos[0], pos[1]))
        return (len(vizinhos) >= self.PARADO_MIN_DETECCOES
                and self.quadro - min(vizinhos) >= self.PARADO_QUADROS)

    def _perdeu(self):
        self.perdidos = min(self.perdidos + 1, 1000)
        if self.perdidos > 15:  # perdeu a bola de vez; os candidatos seguem valendo
            self.ultimo, self.vel = None, (0.0, 0.0)
        return None

    def filtrar(self, pos, largura):
        self.novo_trecho = False
        self.quadro += 1
        if pos is None or self._parado(pos, largura):
            return self._perdeu()

        if self.ultimo is not None:
            passos = 1 + self.perdidos
            vx, vy = self.vel
            previsto = (self.ultimo[0] + vx * passos, self.ultimo[1] + vy * passos)
            folga = max(self.SALTO_MIN * largura, self.FOLGA_VELOCIDADE * np.hypot(vx, vy))
            folga *= min(passos, 3)
            if (np.hypot(pos[0] - previsto[0], pos[1] - previsto[1]) <= folga
                    or np.hypot(pos[0] - self.ultimo[0], pos[1] - self.ultimo[1]) <= folga):
                self.vel = ((pos[0] - self.ultimo[0]) / passos, (pos[1] - self.ultimo[1]) / passos)
                self.ultimo, self.perdidos, self.candidatos = pos, 0, []
                return pos

        # longe de tudo: vira candidato. Cada candidato é acompanhado separado (a IA pode
        # alternar entre a bola e outra coisa) e só entra com 3 quadros seguidos andando.
        salto = 0.22 * largura  # por quadro: cobre até corte forte perto da câmera
        self.candidatos = [c for c in self.candidatos if c["idade"] < 5]
        for c in self.candidatos:
            c["idade"] += 1
        perto = min(self.candidatos, default=None,
                    key=lambda c: np.hypot(pos[0] - c["pts"][-1][0], pos[1] - c["pts"][-1][1]))
        if perto is None or np.hypot(pos[0] - perto["pts"][-1][0],
                                     pos[1] - perto["pts"][-1][1]) > salto * perto["idade"]:
            perto = {"pts": [], "idade": 0}
            self.candidatos.append(perto)
        perto["pts"].append(pos)
        perto["idade"] = 0
        pts = perto["pts"][-self.CONFIRMAR:]
        if len(pts) >= self.CONFIRMAR:
            andou = np.hypot(pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1])
            if andou >= self.MOVIMENTO_MIN * largura:
                self.vel = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
                self.ultimo, self.perdidos, self.candidatos = pos, 0, []
                self.novo_trecho = True
                return pos
        return self._perdeu()


class DetectorBola:
    """Roda o VballNet numa thread própria.

    O motor trabalha em blocos de `seq` quadros (9): o app manda cada quadro com
    `enviar(id, quadro)` e busca a resposta com `resultado(id)`. A resposta de um
    quadro só existe depois que o bloco dele fecha, por isso o app atrasa a
    imagem uns 0,3 s (o suficiente para o placar e o rastro saírem certinhos).
    """

    def __init__(self, chave, pontos_rastro=45):
        m = motor()
        if m is None:
            raise RuntimeError(f"Motor de IA indisponível: {_erro_motor}")
        self.m = m
        self.chave = chave
        caminho = os.path.join(MOTOR_MODELOS, MODELOS[chave][1])
        (self.sessao, self.gru, _out_dim, h0_shape, self.seq, self.entradas, self.saidas,
         self.params) = m.load_onnx_model(caminho)
        # O motor abre a sessão usando todos os núcleos (bom para processar arquivo). Ao vivo
        # isso sufoca a câmera e a gravação, então reabrimos o mesmo modelo com 2 núcleos.
        opcoes = m.ort.SessionOptions()
        opcoes.intra_op_num_threads = 2
        opcoes.inter_op_num_threads = 1
        self.sessao = m.ort.InferenceSession(caminho, opcoes, providers=["CPUExecutionProvider"])
        self.h0 = np.zeros(h0_shape, dtype=np.float32) if self.gru and h0_shape else None
        self.ih, self.iw = self.params["input_height"], self.params["input_width"]
        self.raio_do_modelo = self.params["planes"] in (2, 4)
        self.limiar = m.DEFAULT_HEATMAP_THRESHOLD

        # rastro: a mesma estrutura do motor (BallTrackState); some depois de ~0,5 s sem bola
        self.filtro = FiltroTrajetoria()
        self.n_rastro = pontos_rastro
        self.rastro = m.BallTrackState(maxlen=pontos_rastro, max_missing=15)
        self._estado_raio = {"filtered_history": collections.deque(maxlen=m.BALL_SIZE_HISTORY),
                             "raw_history": collections.deque(maxlen=m.BALL_RAW_SIZE_HISTORY),
                             "smoothed_radius": 0.0}
        self._cinza_anterior = None

        self._bloco = []
        self._fila = queue.Queue(maxsize=6)
        self._res = {}
        self._lock = threading.Lock()
        self.heatmap = None       # último mapa de calor (para a tela "o que o programa enxerga")
        self.ms_por_bloco = 0.0
        self.atrasado = False     # PC não está dando conta deste modelo
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---- lado do app (thread da câmera)
    def enviar(self, fid, quadro):
        h, w = quadro.shape[:2]
        pequeno = self.m.preprocess_frames([quadro], self.ih, self.iw)[0]
        cinza = None if self.raio_do_modelo else cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY)
        self._bloco.append((fid, pequeno, cinza, w, h))
        if len(self._bloco) >= self.seq:
            bloco, self._bloco = self._bloco, []
            try:
                self._fila.put_nowait(bloco)
            except queue.Full:  # a IA ficou para trás: esse bloco sai sem bola
                self.atrasado = True
                with self._lock:
                    for item in bloco:
                        self._res[item[0]] = None

    def resultado(self, fid):
        """(pronto, (x, y, raio) | None) — posição em pixels do quadro enviado."""
        with self._lock:
            if fid in self._res:
                return True, self._res.pop(fid)
            if len(self._res) > 300:  # respostas que chegaram tarde demais e ninguém buscou
                for velho in [k for k in self._res if k < fid]:
                    del self._res[velho]
        return False, None

    def parar(self):
        self._fila.put(None)

    # ---- thread da IA
    def _loop(self):
        m = self.m
        while True:
            bloco = self._fila.get()
            if bloco is None:
                return
            t0 = time.perf_counter()
            # mesmo empacotamento de inference_onnx_seq_gray_v2.main()
            tensor = np.stack([b[1] for b in bloco], axis=2)
            tensor = np.transpose(np.expand_dims(tensor, axis=0), (0, 3, 1, 2))
            saida, novo_h0 = m.run_inference(self.sessao, tensor, self.gru, self.h0,
                                             self.entradas, self.saidas)
            if self.gru and novo_h0 is not None:
                self.h0 = novo_h0
            previsoes = m.decode_predictions(saida, self.params, self.limiar)
            respostas = {}
            for i, (visivel, x, y, raio_norm) in enumerate(previsoes[:len(bloco)]):
                fid, _, cinza, w, h = bloco[i]
                pos = None
                if visivel:
                    xo, yo = x * w / self.iw, y * h / self.ih
                    if self.raio_do_modelo:
                        raio = m.model_radius_to_pixels(raio_norm, w)
                    else:
                        raio, _ = m.estimate_ball_radius(self._cinza_anterior, cinza, int(xo),
                                                         int(yo), self._estado_raio)
                    pos = (float(xo), float(yo), float(raio or 8))
                if cinza is not None:
                    self._cinza_anterior = cinza
                respostas[fid] = pos
            if self.params["family"] == "heatmap":
                hm = saida[0, len(bloco) - 1]
                self.heatmap = np.clip(hm * 255, 0, 255).astype(np.uint8)
            self.ms_por_bloco = (time.perf_counter() - t0) * 1000
            with self._lock:
                self._res.update(respostas)

    # ---- rastro (chamado pelo app na ordem dos quadros)
    def ajustar_rastro(self, n):
        if n != self.n_rastro:
            self.n_rastro = n
            self.rastro = self.m.BallTrackState(maxlen=n, max_missing=15)

    def filtrar(self, pos, largura):
        """Aplica o FiltroTrajetoria (chamado pelo app na ordem dos quadros)."""
        pos = self.filtro.filtrar(pos, largura)
        if self.filtro.novo_trecho:  # não liga o rastro antigo ao ponto novo
            self.rastro.reset()
        return pos

    def atualizar_rastro(self, pos):
        self.rastro.update(None if pos is None else (int(pos[0]), int(pos[1])))
        if self.rastro.is_lost():
            self.rastro.reset()
        # sem bola há mais de 3 quadros: esconde o rastro para ele não ficar "solto" na tela
        self._sem_bola = 0 if pos is not None else getattr(self, "_sem_bola", 0) + 1
        return [] if self._sem_bola > 3 else self.rastro.points()


# ============================================================ depois do jogo

def pasta_analise(video):
    return os.path.join(os.path.dirname(os.path.abspath(video)), "analise")


def csv_da_partida(video):
    """Onde o app grava a posição da bola quadro a quadro — o caminho e o formato
    (Frame,Visibility,X,Y,Radius) que o track_calculator.py do motor espera."""
    base = os.path.splitext(os.path.basename(video))[0]
    return os.path.join(pasta_analise(video), base, "ball.csv")


def _rodar_script(script, argumentos, log):
    cmd = [sys.executable, os.path.join(MOTOR_SRC, script), *argumentos]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log.write(f"\n$ {' '.join(cmd)}\n")
    log.flush()
    r = subprocess.run(cmd, cwd=MOTOR_SRC, env=env, stdout=log, stderr=subprocess.STDOUT,
                       creationflags=flags)
    if r.returncode != 0:
        raise RuntimeError(f"{script} falhou (veja {log.name})")


def _h264(ffmpeg, origem, destino, crf, preset):
    """Os scripts do motor gravam em mp4v; converte para H.264 (menor, abre em qualquer celular)."""
    if not ffmpeg:
        os.replace(origem, destino)
        return
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", origem,
                    "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", destino],
                   check=True, creationflags=flags)


def gerar_melhores_momentos(video, fps, ffmpeg, crf=23, preset="veryfast", reels=True,
                            modelo="ia_precisa", progresso=print):
    """Roda o pipeline do motor sobre uma partida gravada.

    Devolve {"melhores": caminho | None, "reels": [caminhos], "rallies": n}.
    """
    video = os.path.abspath(video)
    base = os.path.splitext(os.path.basename(video))[0]
    analise = pasta_analise(video)
    pasta_base = os.path.join(analise, base)
    os.makedirs(pasta_base, exist_ok=True)
    csv = csv_da_partida(video)
    tracks = os.path.join(pasta_base, "tracks")
    with open(os.path.join(pasta_base, "log.txt"), "a", encoding="utf-8") as log:
        if not os.path.exists(csv):
            # vídeo gravado sem IA (ou vindo de fora): o motor acha a bola no arquivo
            progresso("Procurando a bola no vídeo inteiro (pode levar alguns minutos)…")
            _rodar_script("inference_onnx_seq_gray_v2.py",
                          ["--video_path", video,
                           "--model_path", os.path.join(MOTOR_MODELOS, MODELOS[modelo][1]),
                           "--output_dir", analise, "--only_csv"], log)

        progresso("Separando os rallies…")
        _rodar_script("track_calculator.py",
                      ["--csv_path", csv, "--output_dir", analise, "--fps", str(fps)], log)
        n_rallies = len([f for f in os.listdir(tracks) if f.endswith(".json")]) \
            if os.path.isdir(tracks) else 0
        if n_rallies == 0:
            return {"melhores": None, "reels": [], "rallies": 0}

        progresso("Montando o vídeo de melhores momentos…")
        combinado = os.path.join(pasta_base, "combined.mp4")
        if os.path.exists(combinado):
            os.remove(combinado)
        _rodar_script("track_processor.py",
                      ["--video_path", video, "--output_dir", analise, "--no-mark"], log)
        if not os.path.exists(combinado):  # o motor classificou tudo como "não é rally"
            return {"melhores": None, "reels": [], "rallies": 0}
        saida = {"melhores": None, "reels": [], "rallies": n_rallies}
        if os.path.exists(combinado):
            destino = os.path.join(os.path.dirname(video), f"{base}_melhores_momentos.mp4")
            progresso("Comprimindo o vídeo de melhores momentos…")
            _h264(ffmpeg, combinado, destino, crf, preset)
            saida["melhores"] = destino

        if reels:
            progresso("Criando os reels verticais…")
            pasta_reels = os.path.join(pasta_base, "reels")
            _rodar_script("make_reels.py",
                          ["--video_path", video, "--json_dir", tracks, "--output_dir", analise],
                          log)
            if os.path.isdir(pasta_reels):
                destino_reels = os.path.join(os.path.dirname(video), f"{base}_reels")
                os.makedirs(destino_reels, exist_ok=True)
                for i, nome in enumerate(sorted(os.listdir(pasta_reels)), 1):
                    if nome.endswith(".mp4"):
                        d = os.path.join(destino_reels, f"reel_{i:02d}.mp4")
                        _h264(ffmpeg, os.path.join(pasta_reels, nome), d, crf, preset)
                        saida["reels"].append(d)
        return saida
