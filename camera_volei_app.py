"""
Câmera Vôlei — estúdio com interface (estilo OBS).

Abra com dois cliques em "Abrir Camera Volei.bat".
O que aparece na tela grande é exatamente o que vai para o vídeo: imagem da câmera,
placar de transmissão, relógio, rastro da bola e as animações de fim de set.

O motor (câmera, rastreador da bola, gravação H.264) vem de volei_cam.py.
"""

import argparse
import collections
import os
import queue
import shutil
import sys
import threading
import time
from datetime import datetime

os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")  # webcam abre rápido
import cv2
import numpy as np
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetricsF, QIcon, QImage,
                           QKeySequence, QLinearGradient, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

import motor_ia
import volei_cam as vc

NOME_APP = "Câmera Vôlei"

PLACAR_PADRAO = {
    "titulo": "VÔLEI DE QUINTA",
    "nome_a": "CASA",
    "nome_b": "VISITANTE",
    "cor_a": "#E5484D",
    "cor_b": "#FFB020",
    "pontos_a": 0, "pontos_b": 0,
    "sets_a": 0, "sets_b": 0,
    "set_atual": 1,
    "saque": "",
    "pontos_por_set": 25,
    "pontos_tiebreak": 15,
    "sets_para_vencer": 3,
    "fechar_set_auto": True,
}

VISUAL_PADRAO = {
    "placar": True,
    "relogio": True,
    "trajetoria": True,
    "marcador_bola": True,
    "banner_ponto": False,
    "banner_set": True,
    "logo": "",
    "marca_dagua": "",
    "cor_trajetoria": "#FFC53D",
    "bola_na_tela": True,       # rastro + círculo aparecem na tela
    "bola_na_partida": True,    # ... na gravação da partida inteira
    "bola_no_replay": False,    # ... no corte do Espaço (padrão: limpo)
}

QUALIDADES = [("Máxima (arquivo grande)", 18), ("Alta", 21), ("Normal", 23),
              ("Econômica (arquivo menor)", 26)]
DESEMPENHO = [("Leve — PC fraco", "ultrafast"), ("Equilibrado", "veryfast"),
              ("Arquivo menor — PC forte", "faster")]


def fmt_tempo(seg):
    seg = int(seg)
    h, m, s = seg // 3600, seg % 3600 // 60, seg % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def pasta_abs(p):
    p = p if os.path.isabs(p) else os.path.join(vc.PASTA, p)
    os.makedirs(p, exist_ok=True)
    return p


def abrir_no_sistema(caminho):
    try:
        os.startfile(caminho)  # Windows
    except AttributeError:
        import subprocess
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", caminho])


# ======================================================== estado do estúdio

class Estudio:
    """Estado compartilhado entre a interface (que altera) e o motor (que lê a cada quadro)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.placar = {**PLACAR_PADRAO, **cfg.get("placar", {})}
        self.visual = {**VISUAL_PADRAO, **cfg.get("visual", {})}
        cfg["placar"], cfg["visual"] = self.placar, self.visual
        cfg.setdefault("detector_bola", "ia_precisa")   # ia_precisa | ia_rapida | cor
        cfg.setdefault("gerar_reels", True)
        cfg.setdefault("area_jogo", None)  # [x0, y0, x1, y1] de 0 a 1; fora dela não é bola
        self.lock = threading.RLock()
        self.historico = []
        self.relogio_acum = 0.0
        self.relogio_t0 = None
        self.banner = None
        self.flash = None
        self.seguir = False
        self.mascara = False

    # ---- relógio
    def tempo(self):
        extra = time.monotonic() - self.relogio_t0 if self.relogio_t0 else 0
        return self.relogio_acum + extra

    def relogio_rodando(self):
        return self.relogio_t0 is not None

    def relogio_alternar(self):
        with self.lock:
            if self.relogio_t0:
                self.relogio_acum += time.monotonic() - self.relogio_t0
                self.relogio_t0 = None
            else:
                self.relogio_t0 = time.monotonic()

    def relogio_zerar(self):
        with self.lock:
            self.relogio_acum = 0.0
            self.relogio_t0 = time.monotonic() if self.relogio_t0 else None

    def snapshot(self):
        with self.lock:
            return {"placar": dict(self.placar), "visual": dict(self.visual),
                    "tempo": self.tempo(), "banner": self.banner, "flash": self.flash,
                    "seguir": self.seguir, "mascara": self.mascara,
                    "zoom": self.cfg["zoom_acompanhar"]}

    # ---- placar
    def _guardar(self):
        self.historico.append(dict(self.placar))
        del self.historico[:-300]

    def _mostrar_banner(self, titulo, texto, sub, cor, dur):
        self.banner = {"titulo": titulo, "texto": texto, "sub": sub, "cor": cor,
                       "t0": time.monotonic(), "dur": dur}

    def jogo_encerrado(self):
        p = self.placar
        return max(p["sets_a"], p["sets_b"]) >= p["sets_para_vencer"]

    def ponto(self, lado):
        """Soma um ponto. Retorna 'set', 'jogo' ou 'ponto'."""
        with self.lock:
            self._guardar()
            p = self.placar
            outro = "b" if lado == "a" else "a"
            p[f"pontos_{lado}"] += 1
            p["saque"] = lado
            self.flash = (lado, time.monotonic())
            if self.relogio_t0 is None and self.relogio_acum == 0:
                self.relogio_t0 = time.monotonic()  # 1º ponto dispara o cronômetro

            nome, cor = p[f"nome_{lado}"].upper(), p[f"cor_{lado}"]
            ultimo_set = p["set_atual"] >= 2 * p["sets_para_vencer"] - 1
            alvo = p["pontos_tiebreak"] if ultimo_set else p["pontos_por_set"]
            meus, deles = p[f"pontos_{lado}"], p[f"pontos_{outro}"]
            if (p["fechar_set_auto"] and not self.jogo_encerrado()
                    and meus >= alvo and meus - deles >= 2):
                p[f"sets_{lado}"] += 1
                parcial = f"{p['pontos_a']} × {p['pontos_b']}"
                if self.jogo_encerrado():
                    if self.visual["banner_set"]:
                        self._mostrar_banner("FIM DE JOGO", f"{nome} VENCE",
                                             f"SETS  {p['sets_a']} × {p['sets_b']}", cor, 7)
                    return "jogo"
                if self.visual["banner_set"]:
                    self._mostrar_banner(f"FIM DO {p['set_atual']}º SET", nome,
                                         parcial, cor, 5)
                p["pontos_a"] = p["pontos_b"] = 0
                p["set_atual"] += 1
                return "set"
            if self.visual["banner_ponto"]:
                self._mostrar_banner("PONTO", nome, f"{p['pontos_a']} × {p['pontos_b']}", cor, 2.6)
            return "ponto"

    def ajustar(self, campo, delta):
        with self.lock:
            self._guardar()
            self.placar[campo] = max(0, self.placar[campo] + delta)
            if campo.startswith("sets"):
                self.placar["set_atual"] = max(1, self.placar["sets_a"] + self.placar["sets_b"] + 1)

    def definir_saque(self, lado):
        with self.lock:
            self._guardar()
            self.placar["saque"] = lado

    def desfazer(self):
        with self.lock:
            if not self.historico:
                return False
            self.placar.update(self.historico.pop())
            self.banner = None
            return True

    def trocar_lados(self):
        with self.lock:
            self._guardar()
            p = self.placar
            for c in ("nome", "cor", "pontos", "sets"):
                p[f"{c}_a"], p[f"{c}_b"] = p[f"{c}_b"], p[f"{c}_a"]
            p["saque"] = {"a": "b", "b": "a"}.get(p["saque"], "")

    def novo_jogo(self):
        with self.lock:
            self._guardar()
            self.placar.update(pontos_a=0, pontos_b=0, sets_a=0, sets_b=0, set_atual=1, saque="")
            self.banner = None
            self.relogio_acum, self.relogio_t0 = 0.0, None


# ======================================================== desenho do "programa"

def _fonte(familia, px, peso=QFont.Weight.Bold, espaco=0.0):
    f = QFont(familia)
    f.setPixelSize(max(6, int(round(px))))
    f.setWeight(peso)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if espaco:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, espaco)
    return f


def _familias():
    fams = set(QFontDatabase.families())
    if "Bahnschrift" in fams:  # Windows 10+: fonte "de placar" (estilo DIN)
        cond = "Bahnschrift SemiBold SemiCondensed"
        return "Bahnschrift", cond if cond in fams else "Bahnschrift"
    base = "Segoe UI" if "Segoe UI" in fams else "Arial"
    return base, base


def _mistura(c1, c2, t):
    return QColor(int(c1.red() + (c2.red() - c1.red()) * t),
                  int(c1.green() + (c2.green() - c1.green()) * t),
                  int(c1.blue() + (c2.blue() - c1.blue()) * t),
                  int(c1.alpha() + (c2.alpha() - c1.alpha()) * t))


class Compositor:
    """Desenha os gráficos de transmissão por cima do quadro da câmera (vai para o vídeo)."""

    BRANCO = QColor(255, 255, 255)
    TINTA = QColor(16, 17, 20)

    def __init__(self, w, h):
        self.w, self.h = w, h
        s = self.s = h / 720
        base, cond = _familias()
        W = QFont.Weight
        self.f_titulo = _fonte(base, 12 * s, W.DemiBold, 1.6 * s)
        self.f_relogio = _fonte(base, 13 * s, W.DemiBold, 0.6 * s)
        self.f_nome = _fonte(cond, 19 * s, W.DemiBold, 0.4 * s)
        self.f_sets = _fonte(base, 19 * s, W.Bold)
        self.f_pontos = _fonte(base, 27 * s, W.Bold)
        self.f_ban_tit = _fonte(base, 14 * s, W.Bold, 2.0 * s)
        self.f_ban_txt = _fonte(cond, 30 * s, W.Bold, 0.6 * s)
        self.f_ban_sub = _fonte(base, 22 * s, W.DemiBold, 0.8 * s)
        self.f_marca = _fonte(base, 13 * s, W.DemiBold, 1.4 * s)
        self._logo_caminho, self._logo = None, None

    def desenhar(self, img, snap, pts, bola, agora):
        v = snap["visual"]
        p = QPainter(img)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        cor_rastro = QColor(v["cor_trajetoria"])
        if v["trajetoria"] and len(pts) > 1:
            self._rastro(p, pts, cor_rastro)
        if v["marcador_bola"] and bola is not None:
            self._bola(p, bola, cor_rastro)
        if v["placar"]:
            self._placar(p, snap, agora)
        self._logo_e_marca(p, v)
        b = snap["banner"]
        if b and agora - b["t0"] < b["dur"]:
            self._banner(p, b, agora)
        p.end()

    # ---- bola
    def _rastro(self, p, pts, cor):
        """Rastro afinando e sumindo para trás. Desenhado em trechos (um caminho por trecho)
        para as emendas não formarem "bolinhas" com a transparência."""
        s, n = self.s, len(pts)
        trechos = min(12, n - 1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for camada in (0, 1):
            for k in range(trechos):
                i0 = k * (n - 1) // trechos
                i1 = (k + 1) * (n - 1) // trechos
                f = (k + 1) / trechos
                c = QColor(cor)
                if camada == 0:  # brilho largo e suave
                    c.setAlpha(int(75 * f * f))
                    larg = (5 + 12 * f) * s
                else:            # núcleo fino e claro
                    c = _mistura(c, self.BRANCO, 0.35 * f)
                    c.setAlpha(int(50 + 205 * f))
                    larg = (1.4 + 3.4 * f) * s
                caminho = QPainterPath(QPointF(*pts[i0]))
                for i in range(i0 + 1, i1 + 1):
                    caminho.lineTo(QPointF(*pts[i]))
                p.setPen(QPen(c, larg, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap,
                              Qt.PenJoinStyle.RoundJoin))
                p.drawPath(caminho)

    def _bola(self, p, bola, cor):
        s = self.s
        x, y, r = bola
        r = max(r, 7 * s) + 7 * s
        glow = QColor(cor)
        glow.setAlpha(90)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(glow, 7 * s))
        p.drawEllipse(QPointF(x, y), r, r)
        p.setPen(QPen(QColor(255, 255, 255, 235), 2.2 * s))
        p.drawEllipse(QPointF(x, y), r, r)

    # ---- placar
    def _placar(self, p, snap, agora):
        s, pl, v = self.s, snap["placar"], snap["visual"]
        fm_nome = QFontMetricsF(self.f_nome)
        fm_tit = QFontMetricsF(self.f_titulo)
        fm_rel = QFontMetricsF(self.f_relogio)
        nomes = [pl["nome_a"].upper() or "TIME A", pl["nome_b"].upper() or "TIME B"]
        titulo = pl["titulo"].upper()
        direita = f"SET {pl['set_atual']}"
        if v["relogio"]:
            direita += f"   {fmt_tempo(snap['tempo'])}"

        faixa, sets_w, pts_w = 7 * s, 40 * s, 60 * s
        cab_h, lin_h, pad = 27 * s, 42 * s, 14 * s
        nome_w = max(150 * s, max(fm_nome.horizontalAdvance(n) for n in nomes) + pad + 30 * s)
        largura = faixa + nome_w + sets_w + pts_w
        largura = max(largura, fm_tit.horizontalAdvance(titulo) + fm_rel.horizontalAdvance(direita)
                      + 3 * pad)
        nome_w = largura - faixa - sets_w - pts_w
        x0, y0 = 26 * s, 24 * s
        altura = cab_h + 2 * lin_h
        raio = 9 * s

        # sombra
        p.setPen(Qt.PenStyle.NoPen)
        for i, a in ((3, 38), (6, 22), (10, 12)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawRoundedRect(QRectF(x0 - i * s * 0.3, y0 + i * s * 0.5, largura + i * s * 0.6,
                                     altura + i * s * 0.4), raio + i * s * 0.3, raio + i * s * 0.3)

        moldura = QPainterPath()
        moldura.addRoundedRect(QRectF(x0, y0, largura, altura), raio, raio)
        p.save()
        p.setClipPath(moldura)

        # cabeçalho
        g = QLinearGradient(x0, y0, x0, y0 + cab_h)
        g.setColorAt(0, QColor(10, 10, 12, 240))
        g.setColorAt(1, QColor(20, 20, 24, 240))
        p.fillRect(QRectF(x0, y0, largura, cab_h), g)
        p.setFont(self.f_titulo)
        p.setPen(QColor(255, 255, 255, 215))
        p.drawText(QRectF(x0 + pad, y0, largura - 2 * pad, cab_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, titulo)
        p.setFont(self.f_relogio)
        p.setPen(QColor(255, 255, 255, 170))
        p.drawText(QRectF(x0 + pad, y0, largura - 2 * pad, cab_h),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, direita)

        for i, lado in enumerate(("a", "b")):
            y = y0 + cab_h + i * lin_h
            cor = QColor(pl[f"cor_{lado}"])
            # faixa da cor do time
            p.fillRect(QRectF(x0, y, faixa, lin_h), cor)
            # nome
            gn = QLinearGradient(x0, y, x0, y + lin_h)
            gn.setColorAt(0, QColor(34, 35, 42, 238))
            gn.setColorAt(1, QColor(24, 25, 31, 238))
            p.fillRect(QRectF(x0 + faixa, y, nome_w, lin_h), gn)
            p.setFont(self.f_nome)
            p.setPen(self.BRANCO)
            p.drawText(QRectF(x0 + faixa + pad, y, nome_w - pad, lin_h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, nomes[i])
            if pl["saque"] == lado:  # bolinha de quem saca
                cx = x0 + faixa + nome_w - 14 * s
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(255, 197, 61))
                p.drawEllipse(QPointF(cx, y + lin_h / 2), 4.5 * s, 4.5 * s)
            # sets
            xs = x0 + faixa + nome_w
            p.fillRect(QRectF(xs, y, sets_w, lin_h), QColor(48, 50, 58, 245))
            p.setFont(self.f_sets)
            p.setPen(QColor(255, 255, 255, 225))
            p.drawText(QRectF(xs, y, sets_w, lin_h), Qt.AlignmentFlag.AlignCenter,
                       str(pl[f"sets_{lado}"]))
            # pontos (pisca na cor do time quando marca)
            xp = xs + sets_w
            fundo, tinta = QColor(250, 250, 250), self.TINTA
            fl = snap["flash"]
            if fl and fl[0] == lado and agora - fl[1] < 0.9:
                k = 1 - (agora - fl[1]) / 0.9
                fundo = _mistura(fundo, cor, k)
                tinta = _mistura(tinta, self.BRANCO, k)
            p.fillRect(QRectF(xp, y, pts_w, lin_h), fundo)
            p.setFont(self.f_pontos)
            p.setPen(tinta)
            p.drawText(QRectF(xp, y, pts_w, lin_h), Qt.AlignmentFlag.AlignCenter,
                       str(pl[f"pontos_{lado}"]))

        # divisórias finas
        p.setPen(QPen(QColor(0, 0, 0, 90), max(1.0, s)))
        yl = y0 + cab_h + lin_h
        p.drawLine(QPointF(x0 + faixa, yl), QPointF(x0 + largura, yl))
        p.setPen(QPen(QColor(255, 255, 255, 30), max(1.0, s)))
        p.drawLine(QPointF(x0, y0 + cab_h), QPointF(x0 + largura, y0 + cab_h))
        p.restore()

    # ---- logo / marca d'água
    def _logo_e_marca(self, p, v):
        s = self.s
        caminho = v["logo"]
        if caminho != self._logo_caminho:
            self._logo_caminho = caminho
            self._logo = None
            if caminho and os.path.exists(caminho):
                img = QImage(caminho)
                if not img.isNull():
                    self._logo = img.scaledToHeight(int(70 * s),
                                                    Qt.TransformationMode.SmoothTransformation)
        if self._logo is not None:
            p.setOpacity(0.92)
            p.drawImage(QPointF(self.w - self._logo.width() - 26 * s, 22 * s), self._logo)
            p.setOpacity(1)
        txt = v["marca_dagua"].strip().upper()
        if txt:
            fm = QFontMetricsF(self.f_marca)
            tw, th = fm.horizontalAdvance(txt) + 24 * s, 28 * s
            r = QRectF(self.w - tw - 26 * s, self.h - th - 22 * s, tw, th)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(10, 10, 12, 150))
            p.drawRoundedRect(r, th / 2, th / 2)
            p.setFont(self.f_marca)
            p.setPen(QColor(255, 255, 255, 200))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, txt)

    # ---- faixa animada (fim de set, fim de jogo, ponto)
    def _banner(self, p, b, agora):
        s = self.s
        dt = agora - b["t0"]
        entrada = min(1.0, dt / 0.45)
        ease = 1 - (1 - entrada) ** 3
        alfa = max(0.0, min(1.0, (b["dur"] - dt) / 0.45))

        fm_t = QFontMetricsF(self.f_ban_tit)
        fm_x = QFontMetricsF(self.f_ban_txt)
        fm_s = QFontMetricsF(self.f_ban_sub)
        bloco_w = max(150 * s, fm_t.horizontalAdvance(b["titulo"]) + 40 * s)
        texto_w = fm_x.horizontalAdvance(b["texto"]) + fm_s.horizontalAdvance(b["sub"]) + 80 * s
        W, H = bloco_w + max(300 * s, texto_w), 72 * s
        x = (self.w - W) / 2
        y_final = self.h - H - 46 * s
        y = y_final + (1 - ease) * (H + 60 * s)
        cor = QColor(b["cor"])

        p.save()
        p.setOpacity(alfa)
        caixa = QPainterPath()
        caixa.addRoundedRect(QRectF(x, y, W, H), 10 * s, 10 * s)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 70))
        p.drawRoundedRect(QRectF(x - 2 * s, y + 5 * s, W + 4 * s, H + 2 * s), 12 * s, 12 * s)
        p.setClipPath(caixa)
        g = QLinearGradient(x, y, x, y + H)
        g.setColorAt(0, QColor(30, 31, 38, 242))
        g.setColorAt(1, QColor(14, 14, 18, 242))
        p.fillRect(QRectF(x, y, W, H), g)
        # bloco colorido com "varredura" de entrada
        gb = QLinearGradient(x, y, x + bloco_w, y + H)
        gb.setColorAt(0, _mistura(cor, self.BRANCO, 0.12))
        gb.setColorAt(1, _mistura(cor, QColor(0, 0, 0), 0.18))
        p.fillRect(QRectF(x, y, bloco_w * ease, H), gb)
        p.setFont(self.f_ban_tit)
        p.setPen(self.BRANCO)
        p.drawText(QRectF(x, y, bloco_w, H), Qt.AlignmentFlag.AlignCenter, b["titulo"])
        # texto principal + placar
        p.setFont(self.f_ban_txt)
        p.drawText(QRectF(x + bloco_w + 26 * s, y, W, H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, b["texto"])
        p.setFont(self.f_ban_sub)
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(QRectF(x, y, W - 26 * s, H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, b["sub"])
        # filete na base
        p.fillRect(QRectF(x, y + H - 3 * s, W * ease, 3 * s), cor)
        p.restore()


class Seguidor:
    """Zoom digital que acompanha a bola e volta suave para a quadra inteira."""

    def __init__(self):
        self.cx = self.cy = None
        self.z = 1.0
        self.sem_bola = 0

    def aplicar(self, img, pos, ativo, zoom):
        h, w = img.shape[:2]
        if self.cx is None:
            self.cx, self.cy = w / 2, h / 2
        if ativo and pos is not None:
            self.sem_bola = 0
            ax, ay, az = pos[0], pos[1], zoom
        elif ativo and self.sem_bola < 30:
            self.sem_bola += 1
            ax, ay, az = self.cx, self.cy, self.z
        else:
            self.sem_bola += 1
            ax, ay, az = w / 2, h / 2, 1.0
        self.cx += (ax - self.cx) * 0.12
        self.cy += (ay - self.cy) * 0.12
        self.z += (az - self.z) * 0.06
        if self.z < 1.01:
            return img, (0.0, 0.0, float(w), float(h))
        cw, ch = w / self.z, h / self.z
        x0 = min(max(self.cx - cw / 2, 0), w - cw)
        y0 = min(max(self.cy - ch / 2, 0), h - ch)
        recorte = img[int(y0):int(y0 + ch), int(x0):int(x0 + cw)]
        return cv2.resize(recorte, (w, h), interpolation=cv2.INTER_LINEAR), (x0, y0, cw, ch)


# ======================================================== motor (thread)

class Motor(QThread):
    novo_quadro = Signal()
    info = Signal(object)
    aviso = Signal(str, str)
    arquivo_salvo = Signal(str, str)
    gravacao = Signal(bool, str)
    cor_aprendida = Signal(object)
    falhou = Signal(str)
    camera_trocada = Signal(int)

    def __init__(self, estudio, fonte, ffmpeg, gravar_ao_iniciar):
        super().__init__()
        self.e = estudio
        self.fonte = fonte          # int = câmera; str = arquivo de vídeo
        self.ffmpeg = ffmpeg
        self.gravar_ao_iniciar = gravar_ao_iniciar
        self.cmds = queue.Queue()
        self._parar = False
        self._img = None
        self._img_lock = threading.Lock()
        self._pendente = False
        self._threads = []
        self.partida = None
        self.tamanho = None

    # chamadas pela interface
    def comando(self, *c):
        self.cmds.put(c)

    def parar(self):
        self._parar = True

    def pegar_quadro(self):
        with self._img_lock:
            self._pendente = False
            return self._img

    def _publicar(self, img):
        with self._img_lock:
            self._img = img
            if self._pendente:
                return
            self._pendente = True
        self.novo_quadro.emit()

    # gravação
    def _iniciar_partida(self, fps):
        cfg = self.e.cfg
        self.partida = vc.GravacaoPartida(cfg, fps, self.tamanho, self.ffmpeg,
                                          csv_bola=motor_ia.csv_da_partida)
        self.gravacao.emit(True, self.partida.caminho)

    def _parar_partida(self, esperar=False):
        partida, self.partida = self.partida, None
        if partida is None:
            return

        def fechar():
            partida.parar()
            self.arquivo_salvo.emit(partida.caminho, "partida")

        self.gravacao.emit(False, partida.caminho)
        if esperar:
            fechar()
        else:
            th = threading.Thread(target=fechar)
            th.start()
            self._threads.append(th)

    def _salvar_replay(self, quadros, fps):
        cfg = self.e.cfg
        caminho = vc.caminho_saida(cfg["pasta_cortes"], "replay")
        try:
            g = vc.GravadorVideo(caminho, fps, self.tamanho, cfg, self.ffmpeg)
            for t, jpg in quadros:
                g.adicionar(t, jpg)
            g.fechar()
            self.arquivo_salvo.emit(caminho, "replay")
        except Exception as ex:  # noqa: BLE001
            self.aviso.emit(f"Erro ao salvar o replay: {ex}", "erro")

    def _aprender_cor(self, quadro, nx, ny, rect):
        cfg = self.e.cfg
        x0, y0, cw, ch = rect
        ih, iw = quadro.shape[:2]
        x = int(min(max(x0 + nx * cw, 0), iw - 1))
        y = int(min(max(y0 + ny * ch, 0), ih - 1))
        area = quadro[max(0, y - 4):y + 5, max(0, x - 4):x + 5]
        hsv = cv2.cvtColor(area, cv2.COLOR_BGR2HSV).reshape(-1, 3)
        hh, ss, vv = (int(v) for v in np.median(hsv, axis=0))
        cfg["cor_bola_hsv_min"] = [int((hh - 10) % 180), max(0, ss - 70), max(0, vv - 80)]
        cfg["cor_bola_hsv_max"] = [int((hh + 10) % 180), 255, 255]
        cfg["usar_cor"] = ss > 50  # bola branca/cinza: cor não ajuda, usa só movimento
        self.cor_aprendida.emit([hh, ss, vv])

    def _criar_detector(self, chave):
        """IA do motor open source (motor_vball) ou None = modo antigo cor + movimento."""
        if chave not in motor_ia.MODELOS:
            return None
        try:
            return motor_ia.DetectorBola(chave, int(self.e.cfg["pontos_trajetoria"]))
        except Exception as ex:  # noqa: BLE001
            self.aviso.emit(f"IA indisponível ({ex}). Usando cor + movimento.", "erro")
            return None

    def run(self):
        cfg = self.e.cfg
        arquivo = isinstance(self.fonte, str)
        if not arquivo:
            cfg["camera"] = self.fonte
        cap = vc.abrir_camera(cfg, self.fonte if arquivo else None)
        if not cap.isOpened() and not arquivo and self.fonte != 0:
            cap.release()
            self.aviso.emit(f"Câmera {self.fonte} não encontrada; usando a Câmera 0", "info")
            self.fonte = cfg["camera"] = 0
            self.camera_trocada.emit(0)
            cap = vc.abrir_camera(cfg, None)
        if not cap.isOpened():
            self.falhou.emit("Não consegui abrir a câmera. Confira o cabo USB ou escolha "
                             "outra câmera na aba Câmera.")
            return
        ok, quadro = cap.read()
        if not ok:
            cap.release()
            self.falhou.emit("A câmera abriu mas não enviou imagem. Ela pode estar em uso "
                             "por outro programa (Teams, Meet, OBS...).")
            return

        h, w = quadro.shape[:2]
        self.tamanho = (w, h)
        fps_arq = cap.get(cv2.CAP_PROP_FPS) if arquivo else 0
        fps = int(round(fps_arq)) if fps_arq and fps_arq > 1 else cfg["fps"]
        rastreador = vc.RastreadorBola(cfg)          # modo antigo (cor + movimento)
        chave_det = cfg.get("detector_bola", "ia_precisa")
        detector = self._criar_detector(chave_det)   # motor de IA (projeto base)
        seguidor = Seguidor()
        comp = Compositor(w, h)
        buffer = collections.deque()
        deteccoes = collections.deque(maxlen=int(fps * 3))
        rect = (0.0, 0.0, float(w), float(h))
        pendentes = collections.deque()  # (id, horário, quadro) esperando a resposta da IA
        fid = 0
        periodo_medio = 1 / fps
        t_anterior = None
        if self.gravar_ao_iniciar:
            self._iniciar_partida(fps)

        def dentro(p):
            area = cfg.get("area_jogo")
            if not area or p is None:
                return True
            return area[0] <= p[0] / w <= area[2] and area[1] <= p[1] / h <= area[3]

        def compor(tq, q, pos, pts_brutos):
            """Monta o quadro do programa (câmera + gráficos), grava e manda para a tela."""
            nonlocal rect
            deteccoes.append(pos is not None)
            snap = self.e.snapshot()
            saida, rect = seguidor.aplicar(q, pos, snap["seguir"], snap["zoom"])
            x0, y0, cw, ch = rect
            kx, ky = w / cw, h / ch
            pts = [((x - x0) * kx, (y - y0) * ky) for x, y in pts_brutos]
            bola = None if pos is None else ((pos[0] - x0) * kx, (pos[1] - y0) * ky, pos[2] * kx)

            # Cada destino escolhe se leva o rastro/círculo da bola (aba Visual). Compõe só
            # as versões necessárias (com e/ou sem bola), direto na memória da QImage.
            v = snap["visual"]
            quer = {"tela": v["bola_na_tela"], "replay": v["bola_no_replay"],
                    "partida": v["bola_na_partida"] if self.partida else None}
            base = cv2.cvtColor(saida, cv2.COLOR_BGR2BGRA)
            versoes = {}
            for com_bola in {q for q in quer.values() if q is not None}:
                img_v = QImage(w, h, QImage.Format.Format_RGB32)
                arr = np.ndarray((h, img_v.bytesPerLine() // 4, 4), np.uint8, img_v.bits())[:, :w]
                arr[:] = base
                comp.desenhar(img_v, snap, pts if com_bola else [], bola if com_bola else None, tq)
                _, jpg_v = cv2.imencode(".jpg", cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR),
                                        [cv2.IMWRITE_JPEG_QUALITY, cfg["qualidade_jpeg_buffer"]])
                del arr
                versoes[com_bola] = (img_v, jpg_v.tobytes())
            img = versoes[quer["tela"]][0]
            buffer.append((tq, versoes[quer["replay"]][1]))
            while buffer and tq - buffer[0][0] > cfg["segundos_do_corte"]:
                buffer.popleft()
            if self.partida:
                self.partida.adicionar(tq, versoes[quer["partida"]][1], bola)

            if snap["mascara"]:
                m = detector.heatmap if detector else rastreador.ultima_mascara
                if m is not None:
                    m = np.ascontiguousarray(m)
                    img = QImage(m.data, m.shape[1], m.shape[0], m.strides[0],
                                 QImage.Format.Format_Grayscale8).copy()
            self._publicar(img)

        periodo = 1 / fps
        t_prox = time.monotonic()
        t_info, n_info = time.monotonic(), 0
        primeiro = True
        while not self._parar:
            if not primeiro:
                ok, quadro = cap.read()
                if not ok and arquivo:  # vídeo de teste: volta ao começo
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, quadro = cap.read()
                if not ok:
                    self.falhou.emit("A câmera parou de enviar imagem (cabo solto?). "
                                     "Clique em Conectar na aba Câmera.")
                    break
            primeiro = False
            if arquivo:  # arquivo lê rápido demais: segura no ritmo real
                t_prox += periodo
                espera = t_prox - time.monotonic()
                if espera > 0:
                    time.sleep(espera)
                elif espera < -0.5:
                    t_prox = time.monotonic()
            t = time.monotonic()
            if t_anterior is not None:
                periodo_medio += (min(t - t_anterior, 0.5) - periodo_medio) * 0.05
            t_anterior = t

            while True:
                try:
                    cmd = self.cmds.get_nowait()
                except queue.Empty:
                    break
                if cmd[0] == "replay" and len(buffer) > 1:
                    th = threading.Thread(target=self._salvar_replay, args=(list(buffer), fps))
                    th.start()
                    self._threads.append(th)
                    dur = buffer[-1][0] - buffer[0][0]
                    self.aviso.emit(f"Salvando replay dos últimos {dur:.0f} s…", "info")
                elif cmd[0] == "rec_ligar" and self.partida is None:
                    self._iniciar_partida(fps)
                elif cmd[0] == "rec_desligar":
                    self._parar_partida()
                elif cmd[0] == "cor":
                    self._aprender_cor(quadro, cmd[1], cmd[2], rect)

            n_traj = int(cfg["pontos_trajetoria"])
            if cfg.get("detector_bola", "ia_precisa") != chave_det:  # trocou na aba Bola
                chave_det = cfg.get("detector_bola", "ia_precisa")
                if detector:
                    detector.parar()
                detector = self._criar_detector(chave_det)
                for _, tq, q in pendentes:  # o que esperava a IA antiga sai sem bola
                    compor(tq, q, None, [])
                pendentes.clear()

            if detector:
                # A IA responde em blocos de 9 quadros; um atraso fixo mantém a imagem lisa.
                detector.ajustar_rastro(n_traj)
                fid += 1
                detector.enviar(fid, quadro)
                pendentes.append((fid, t, quadro))
                atraso = (detector.seq + 2) * periodo_medio + detector.ms_por_bloco / 1000 + 0.05
                while pendentes:
                    f0, t0, q0 = pendentes[0]
                    if t - t0 < atraso:
                        break
                    pronto, pos = detector.resultado(f0)
                    if not pronto and t - t0 < atraso + 1.0:
                        break
                    pendentes.popleft()
                    if not dentro(pos):  # fora da área de jogo (ex.: gente na lateral)
                        pos = None
                    pos = detector.filtrar(pos, w)  # descarta "bolas" que teletransportam
                    compor(t0, q0, pos, detector.atualizar_rastro(pos))
            else:
                pos = rastreador.atualizar(quadro)
                if rastreador.trajetoria.maxlen != n_traj:
                    rastreador.trajetoria = collections.deque(rastreador.trajetoria,
                                                              maxlen=n_traj)
                compor(t, quadro, pos if dentro(pos) else None,
                       [p for p in rastreador.trajetoria if dentro(p)])

            n_info += 1
            if t - t_info >= 0.5:
                rec = self.partida
                info = {"fps": n_info / (t - t_info),
                        "buffer": buffer[-1][0] - buffer[0][0] if len(buffer) > 1 else 0,
                        "det": 100 * sum(deteccoes) / max(1, len(deteccoes)),
                        "w": w, "h": h, "rec": rec is not None,
                        "detector": motor_ia.MODELOS[chave_det][0] if detector
                        else "Cor + movimento",
                        "ia_ms": detector.ms_por_bloco if detector else 0,
                        "ia_atrasada": bool(detector and detector.atrasado)}
                if rec:
                    info["rec_s"] = time.monotonic() - rec.inicio
                    try:
                        info["rec_mb"] = os.path.getsize(rec.caminho) / 1e6
                    except OSError:
                        info["rec_mb"] = 0
                self.info.emit(info)
                t_info, n_info = t, 0

        for _, tq, q in pendentes:  # últimos ~0,4 s ainda vão para a gravação
            compor(tq, q, None, [])
        if detector:
            detector.parar()
        cap.release()
        self._parar_partida(esperar=True)
        for th in self._threads:
            th.join()


# ======================================================== interface

class Preview(QWidget):
    """Tela grande: mostra o programa, recebe cliques e exibe avisos (que não vão para o vídeo)."""

    clicou = Signal(float, float)
    duplo_clique = Signal()
    area_marcada = Signal(object)

    def __init__(self):
        super().__init__()
        self.img = None
        self.mensagem = "Conectando à câmera…"
        self.modo_cor = False
        self.modo_area = False
        self.mostrar_area = False
        self.area = None          # [x0, y0, x1, y1] normalizados
        self._arrasto = None
        self.toasts = []
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._timer = QTimer(self, interval=100, timeout=self._limpar_toasts)
        self._timer.start()

    def definir_imagem(self, img):
        self.img = img
        self.update()

    def toast(self, texto, tipo="ok"):
        self.toasts.append((texto, tipo, time.monotonic() + 3.5))
        self.toasts = self.toasts[-3:]
        self.update()

    def _limpar_toasts(self):
        agora = time.monotonic()
        n = len(self.toasts)
        self.toasts = [t for t in self.toasts if t[2] > agora]
        if n != len(self.toasts):
            self.update()

    def _alvo(self):
        iw, ih = (16, 9) if self.img is None else (self.img.width(), self.img.height())
        k = min(self.width() / iw, self.height() / ih)
        w, h = iw * k, ih * k
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing |
                         QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor("#050506"))
        r = self._alvo()
        if self.img is None:
            p.fillRect(r, QColor("#0B0C0F"))
            p.setPen(QColor("#6B6E78"))
            p.setFont(_fonte("Segoe UI", 16, QFont.Weight.DemiBold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.mensagem)
        else:
            p.drawImage(r, self.img)

        area = self._arrasto or self.area
        if area and (self.modo_area or self.mostrar_area):
            ax0, ay0, ax1, ay1 = area
            ra = QRectF(r.x() + min(ax0, ax1) * r.width(), r.y() + min(ay0, ay1) * r.height(),
                        abs(ax1 - ax0) * r.width(), abs(ay1 - ay0) * r.height())
            fora = QPainterPath()
            fora.addRect(r)
            dentro_area = QPainterPath()
            dentro_area.addRect(ra)
            p.fillPath(fora.subtracted(dentro_area), QColor(0, 0, 0, 120))
            p.setPen(QPen(QColor(255, 176, 32), 2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(ra)
        if self.modo_area:
            self._pilula(p, "Arraste um retângulo em volta da quadra (inclua o alto, por onde "
                            "a bola passa)  ·  Esc cancela", QColor(255, 176, 32), QColor(20, 20, 20),
                         r.center().x(), r.top() + 26)
        if self.modo_cor:
            self._pilula(p, "Clique em cima da bola para ensinar a cor dela  ·  Esc cancela",
                         QColor(255, 176, 32), QColor(20, 20, 20), r.center().x(), r.top() + 26)
        y = r.top() + (72 if self.modo_cor else 28)
        for texto, tipo, _ in reversed(self.toasts):
            fundo = {"erro": QColor(229, 72, 77), "info": QColor(40, 42, 50)}.get(
                tipo, QColor(30, 160, 90))
            self._pilula(p, texto, fundo, QColor(255, 255, 255), r.center().x(), y)
            y += 44

    def _pilula(self, p, texto, fundo, tinta, cx, cy):
        f = _fonte("Segoe UI", 15, QFont.Weight.DemiBold)
        w = QFontMetricsF(f).horizontalAdvance(texto) + 36
        rr = QRectF(cx - w / 2, cy - 17, w, 34)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        p.drawRoundedRect(rr.translated(0, 3), 17, 17)
        p.setBrush(fundo)
        p.drawRoundedRect(rr, 17, 17)
        p.setFont(f)
        p.setPen(tinta)
        p.drawText(rr, Qt.AlignmentFlag.AlignCenter, texto)

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton or self.img is None:
            return
        r = self._alvo()
        pos = e.position()
        if not r.contains(pos):
            return
        nx, ny = (pos.x() - r.x()) / r.width(), (pos.y() - r.y()) / r.height()
        if self.modo_area:
            self._arrasto = [nx, ny, nx, ny]
            self.update()
        else:
            self.clicou.emit(nx, ny)

    def mouseMoveEvent(self, e):
        if self.modo_area and self._arrasto:
            r = self._alvo()
            pos = e.position()
            self._arrasto[2] = min(max((pos.x() - r.x()) / r.width(), 0), 1)
            self._arrasto[3] = min(max((pos.y() - r.y()) / r.height(), 0), 1)
            self.update()

    def mouseReleaseEvent(self, _e):
        if self.modo_area and self._arrasto:
            x0, y0, x1, y1 = self._arrasto
            self._arrasto = None
            area = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
            if area[2] - area[0] > 0.05 and area[3] - area[1] > 0.05:
                self.area_marcada.emit(area)
            self.update()

    def mouseDoubleClickEvent(self, _e):
        if not self.modo_cor and not self.modo_area:
            self.duplo_clique.emit()


ESTILO = """
* { font-family: 'Segoe UI'; }
QMainWindow, QWidget#raiz { background: #0E0F12; }
QWidget { color: #E7E8EC; font-size: 13px; }
QLabel#secao { color: #8B8E98; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#dica { color: #7D808A; font-size: 12px; }
QLabel#pill { background: #1E2026; color: #C9CBD2; border-radius: 10px; padding: 3px 10px;
              font-size: 11px; font-weight: 700; letter-spacing: 1px; }
QLabel#pillRec { background: #E5484D; color: white; border-radius: 10px; padding: 3px 10px;
                 font-size: 12px; font-weight: 700; }
QLabel#info { color: #9FA2AC; font-size: 12px; }
QLabel#pontos { font-family: 'Bahnschrift'; font-size: 40px; font-weight: 700; color: white; }
QLabel#sets { font-family: 'Bahnschrift'; font-size: 20px; font-weight: 700; color: #E7E8EC; }
QLabel#relogio { font-family: 'Bahnschrift'; font-size: 30px; font-weight: 600; color: white; }
QFrame#barra, QFrame#controles { background: #15161A; border: 1px solid #22242A; border-radius: 12px; }
QFrame#cartao { background: #15161A; border: 1px solid #22242A; border-radius: 12px; }
QFrame#telaFundo { background: #050506; border: 1px solid #22242A; border-radius: 12px; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab { background: transparent; color: #8B8E98; padding: 9px 10px; margin-right: 2px;
               border: none; border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:hover { color: #D5D7DD; }
QTabBar::tab:selected { color: white; border-bottom: 2px solid #E5484D; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #2C2F36; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QPushButton { background: #23252C; border: 1px solid #2E3139; border-radius: 9px;
              padding: 8px 12px; font-weight: 600; color: #E7E8EC; }
QPushButton:hover { background: #2A2D35; border-color: #3A3D46; }
QPushButton:pressed { background: #1B1D22; }
QPushButton:disabled { color: #5B5E66; }
QPushButton:checked { background: #33261A; border: 1px solid #FFB020; color: #FFC53D; }
QPushButton#rec { background: #E5484D; border: none; color: white; font-size: 15px; font-weight: 700;
                  padding: 12px 20px; }
QPushButton#rec:hover { background: #EE5A5F; }
QPushButton#rec[gravando="true"] { background: #2A1416; border: 2px solid #E5484D; color: #FF8A8E; }
QPushButton#replay { background: #FFB020; border: none; color: #17181B; font-size: 15px;
                     font-weight: 700; padding: 12px 20px; }
QPushButton#replay:hover { background: #FFC24D; }
QPushButton#grande { font-size: 15px; font-weight: 700; padding: 12px 16px; }
QPushButton#mais { background: #2A2C33; font-size: 15px; font-weight: 700; padding: 10px; }
QLineEdit, QSpinBox, QComboBox { background: #1B1D22; border: 1px solid #2E3139; border-radius: 8px;
              padding: 6px 9px; selection-background-color: #E5484D; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #E5484D; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox::down-arrow { image: url(@UI@/seta_baixo.png); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: #1B1D22; border: 1px solid #2E3139;
              selection-background-color: #2E3139; outline: none; }
QSpinBox { padding-right: 22px; }
QSpinBox::up-button, QSpinBox::down-button { width: 20px; border: none; background: transparent; }
QSpinBox::up-arrow { image: url(@UI@/seta_cima.png); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url(@UI@/seta_baixo.png); width: 10px; height: 10px; }
QCheckBox { spacing: 9px; padding: 3px 0; }
QCheckBox::indicator { width: 36px; height: 20px; image: url(@UI@/toggle_off.png); }
QCheckBox::indicator:checked { image: url(@UI@/toggle_on.png); }
QSlider::groove:horizontal { height: 4px; background: #2E3139; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #E5484D; border-radius: 2px; }
QSlider::handle:horizontal { background: white; width: 16px; height: 16px; margin: -6px 0;
                             border-radius: 8px; }
QListWidget { background: #121317; border: 1px solid #22242A; border-radius: 10px; padding: 4px;
              outline: none; }
QListWidget::item { padding: 8px 6px; border-radius: 6px; }
QListWidget::item:selected { background: #26282F; color: white; }
QListWidget::item:hover { background: #1C1E23; }
QStatusBar { background: #0A0B0D; color: #7D808A; border-top: 1px solid #1B1D22; }
QStatusBar QLabel { color: #8B8E98; padding: 0 10px; font-size: 12px; }
QToolTip { background: #1B1D22; color: #E7E8EC; border: 1px solid #2E3139; padding: 6px; }
"""


def icone_app():
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#E5484D"))
    p.drawRoundedRect(QRectF(8, 8, 240, 240), 56, 56)
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(QPointF(128, 128), 74, 74)
    p.setPen(QPen(QColor("#E5484D"), 12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(40, 40, 176, 176), 200 * 16, 110 * 16)
    p.drawArc(QRectF(88, 20, 190, 190), 150 * 16, 90 * 16)
    p.drawArc(QRectF(-10, 70, 190, 190), 300 * 16, 90 * 16)
    p.setBrush(QColor("#FFB020"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(200, 56), 26, 26)
    p.end()
    return pm


class SinaisMelhores(QObject):
    progresso = Signal(str)
    fim = Signal(object, str)


class Janela(QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.args = args
        cfg = vc.carregar_config()
        self.e = Estudio(cfg)
        self.ffmpeg = vc.achar_ffmpeg()
        self.motor = None
        self.gravando = False
        self.rec_info = (0.0, 0.0)
        self._sinais_melhores = SinaisMelhores()
        self._sinais_melhores.progresso.connect(self._melhores_progresso)
        self._sinais_melhores.fim.connect(self._melhores_fim)
        self._salvar_timer = QTimer(self, singleShot=True, interval=600,
                                    timeout=lambda: vc.salvar_config(self.e.cfg))

        self.setWindowTitle(NOME_APP)
        self.setWindowIcon(QIcon(icone_app()))
        self.resize(1480, 900)
        self.setMinimumSize(1280, 680)

        raiz = QWidget(objectName="raiz")
        self.setCentralWidget(raiz)
        lay = QHBoxLayout(raiz)
        lay.setContentsMargins(14, 14, 14, 10)
        lay.setSpacing(14)

        # ---------- coluna da tela
        col = QVBoxLayout()
        col.setSpacing(10)
        lay.addLayout(col, 1)
        self.barra = self._barra_superior()
        col.addWidget(self.barra)
        self.preview = Preview()
        self.preview.clicou.connect(self._clique_preview)
        self.preview.duplo_clique.connect(self._tela_cheia)
        self.preview.area_marcada.connect(self._area_marcada)
        self.preview.area = cfg.get("area_jogo")
        col.addWidget(self.preview, 1)
        self.controles = self._controles()
        col.addWidget(self.controles)

        # ---------- painel lateral
        self.lateral = QTabWidget()
        self.lateral.setFixedWidth(372)
        self.lateral.addTab(self._rolavel(self._aba_placar()), "Placar")
        self.lateral.addTab(self._rolavel(self._aba_visual()), "Visual")
        self.lateral.addTab(self._rolavel(self._aba_bola()), "Bola")
        self.lateral.addTab(self._rolavel(self._aba_gravacao()), "Gravação")
        self.lateral.addTab(self._rolavel(self._aba_camera()), "Câmera")
        lay.addWidget(self.lateral)

        # ---------- barra de status
        sb = self.statusBar()
        self.st_camera = QLabel("Câmera: —")
        self.st_bola = QLabel("Bola: —")
        self.st_disco = QLabel("")
        self.st_ffmpeg = QLabel("H.264 ✓" if self.ffmpeg else "⚠ ffmpeg ausente: vídeos maiores")
        for w in (self.st_camera, self.st_bola):
            sb.addWidget(w)
        sb.addPermanentWidget(self.st_ffmpeg)
        sb.addPermanentWidget(self.st_disco)

        self._atalhos()
        self._atualizar_placar_ui()
        self._atualizar_arquivos()
        self._rotulo_area()
        self._atualizar_disco()
        QTimer(self, interval=250, timeout=self._tique).start()
        QTimer(self, interval=10000, timeout=self._atualizar_disco).start()

        fonte = args.video if args.video else int(cfg["camera"])
        QTimer.singleShot(50, lambda: self._ligar_motor(fonte, cfg["gravar_partida_inteira"]))

    # ================================================= montagem da interface
    def _rolavel(self, w):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(w)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        return sa

    def _pagina(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(2, 12, 8, 12)
        v.setSpacing(12)
        return w, v

    def _cartao(self, titulo=None):
        c = QFrame(objectName="cartao")
        v = QVBoxLayout(c)
        v.setContentsMargins(14, 12, 14, 14)
        v.setSpacing(9)
        if titulo:
            v.addWidget(QLabel(titulo.upper(), objectName="secao"))
        return c, v

    def _dica(self, texto):
        lb = QLabel(texto, objectName="dica")
        lb.setWordWrap(True)
        return lb

    def _barra_superior(self):
        f = QFrame(objectName="barra")
        h = QHBoxLayout(f)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(12)
        h.addWidget(QLabel("PROGRAMA", objectName="pill"))
        self.lb_fonte = QLabel("", objectName="info")
        h.addWidget(self.lb_fonte)
        h.addStretch(1)
        self.lb_buffer = QLabel("Replay: —", objectName="info")
        h.addWidget(self.lb_buffer)
        self.lb_fps = QLabel("", objectName="info")
        h.addWidget(self.lb_fps)
        self.lb_rec = QLabel("", objectName="pillRec")
        self.lb_rec.hide()
        h.addWidget(self.lb_rec)
        return f

    def _controles(self):
        f = QFrame(objectName="controles")
        h = QHBoxLayout(f)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(10)
        self.bt_rec = QPushButton("●   GRAVAR", objectName="rec")
        self.bt_rec.setMinimumWidth(190)
        self.bt_rec.setToolTip("Grava a partida inteira (atalho: R)")
        self.bt_rec.clicked.connect(self._alternar_gravacao)
        self.bt_replay = QPushButton(objectName="replay")
        self.bt_replay.setToolTip("Salva os últimos segundos como um vídeo separado (atalho: Espaço)")
        self.bt_replay.clicked.connect(self._replay)
        h.addWidget(self.bt_rec)
        h.addWidget(self.bt_replay)
        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setStyleSheet("background:#2A2C33;")
        h.addSpacing(4)
        h.addWidget(sep)
        h.addSpacing(4)
        self.bt_pa = QPushButton(objectName="grande")
        self.bt_pa.clicked.connect(lambda: self._ponto("a"))
        self.bt_pb = QPushButton(objectName="grande")
        self.bt_pb.clicked.connect(lambda: self._ponto("b"))
        bt_undo = QPushButton("↩", objectName="grande")
        bt_undo.setFixedWidth(52)
        bt_undo.setToolTip("Desfazer o último ponto (Ctrl+Z)")
        bt_undo.clicked.connect(self._desfazer)
        h.addWidget(self.bt_pa)
        h.addWidget(self.bt_pb)
        h.addWidget(bt_undo)
        h.addStretch(1)
        self.bt_seguir = QPushButton("◎  Seguir bola", objectName="grande")
        self.bt_seguir.setCheckable(True)
        self.bt_seguir.setToolTip("Zoom digital que acompanha a bola (atalho: F)")
        self.bt_seguir.toggled.connect(self._seguir)
        h.addWidget(self.bt_seguir)
        for b in f.findChildren(QPushButton):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        return f

    # ---------- aba Placar
    def _aba_placar(self):
        w, v = self._pagina()
        c, cv = self._cartao("Jogo")
        self.ed_titulo = QLineEdit(self.e.placar["titulo"])
        self.ed_titulo.setPlaceholderText("Nome do jogo / evento")
        self.ed_titulo.setMaxLength(32)
        self.ed_titulo.textEdited.connect(lambda t: self._set_placar("titulo", t))
        cv.addWidget(self.ed_titulo)
        v.addWidget(c)

        self.ui_time = {}
        for lado in ("a", "b"):
            v.addWidget(self._cartao_time(lado))

        linha = QHBoxLayout()
        for texto, fn, dica in (("↩  Desfazer", self._desfazer, "Ctrl+Z"),
                                ("⇄  Trocar lados", self._trocar, "Troca os times de posição"),
                                ("Novo jogo", self._novo_jogo, "Zera pontos, sets e relógio")):
            b = QPushButton(texto)
            b.setToolTip(dica)
            b.clicked.connect(fn)
            linha.addWidget(b)
        v.addLayout(linha)

        c, cv = self._cartao("Cronômetro")
        h = QHBoxLayout()
        self.lb_relogio = QLabel("00:00", objectName="relogio")
        h.addWidget(self.lb_relogio, 1)
        self.bt_relogio = QPushButton("▶  Iniciar")
        self.bt_relogio.clicked.connect(self._relogio)
        bz = QPushButton("Zerar")
        bz.clicked.connect(lambda: (self.e.relogio_zerar(), self._tique()))
        h.addWidget(self.bt_relogio)
        h.addWidget(bz)
        cv.addLayout(h)
        cv.addWidget(self._dica("Começa sozinho no primeiro ponto."))
        v.addWidget(c)

        c, cv = self._cartao("Regras")
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        for campo, rotulo, mn, mx in (("pontos_por_set", "Pontos por set", 5, 50),
                                      ("pontos_tiebreak", "Pontos no tie-break", 5, 50),
                                      ("sets_para_vencer", "Sets para vencer", 1, 5)):
            sp = QSpinBox()
            sp.setRange(mn, mx)
            sp.setValue(self.e.placar[campo])
            sp.valueChanged.connect(lambda val, c=campo: self._set_placar(c, val))
            form.addRow(rotulo, sp)
        cv.addLayout(form)
        ck = QCheckBox("Fechar o set automaticamente")
        ck.setChecked(self.e.placar["fechar_set_auto"])
        ck.toggled.connect(lambda val: self._set_placar("fechar_set_auto", val))
        cv.addWidget(ck)
        cv.addWidget(self._dica("Ao chegar nos pontos com 2 de vantagem, soma o set, mostra a "
                                "faixa de fim de set e zera os pontos. Errou? Ctrl+Z desfaz."))
        v.addWidget(c)

        v.addWidget(self._dica("Atalhos:  1 / 2 = ponto  ·  Ctrl+Z desfazer  ·  Espaço replay  ·  "
                               "R gravar  ·  F seguir bola  ·  F11 tela cheia"))
        v.addStretch(1)
        return w

    def _cartao_time(self, lado):
        c, cv = self._cartao()
        ui = {}
        topo = QHBoxLayout()
        ui["faixa"] = QFrame()
        ui["faixa"].setFixedSize(6, 30)
        topo.addWidget(ui["faixa"])
        ui["nome"] = QLineEdit(self.e.placar[f"nome_{lado}"])
        ui["nome"].setMaxLength(18)
        ui["nome"].setPlaceholderText("Nome do time")
        ui["nome"].textEdited.connect(lambda t: self._set_placar(f"nome_{lado}", t))
        topo.addWidget(ui["nome"], 1)
        ui["cor"] = QPushButton()
        ui["cor"].setFixedSize(34, 34)
        ui["cor"].setToolTip("Cor do time")
        ui["cor"].clicked.connect(lambda: self._escolher_cor_time(lado))
        topo.addWidget(ui["cor"])
        cv.addLayout(topo)

        pts = QHBoxLayout()
        menos = QPushButton("−1")
        menos.setFixedWidth(48)
        menos.clicked.connect(lambda: self._ajustar(f"pontos_{lado}", -1))
        ui["pontos"] = QLabel("0", objectName="pontos")
        ui["pontos"].setAlignment(Qt.AlignmentFlag.AlignCenter)
        mais = QPushButton("+1 ponto", objectName="mais")
        mais.clicked.connect(lambda: self._ponto(lado))
        pts.addWidget(menos)
        pts.addWidget(ui["pontos"], 1)
        pts.addWidget(mais, 1)
        cv.addLayout(pts)

        sets = QHBoxLayout()
        sets.addWidget(QLabel("Sets", objectName="dica"))
        sm = QPushButton("−")
        sm.setFixedWidth(34)
        sm.clicked.connect(lambda: self._ajustar(f"sets_{lado}", -1))
        ui["sets"] = QLabel("0", objectName="sets")
        ui["sets"].setAlignment(Qt.AlignmentFlag.AlignCenter)
        ui["sets"].setFixedWidth(34)
        sp = QPushButton("+")
        sp.setFixedWidth(34)
        sp.clicked.connect(lambda: self._ajustar(f"sets_{lado}", +1))
        sets.addWidget(sm)
        sets.addWidget(ui["sets"])
        sets.addWidget(sp)
        sets.addStretch(1)
        ui["saque"] = QPushButton("● Saque")
        ui["saque"].setCheckable(True)
        ui["saque"].setToolTip("Marca quem está sacando (muda sozinho a cada ponto)")
        ui["saque"].clicked.connect(lambda: self._saque(lado))
        sets.addWidget(ui["saque"])
        cv.addLayout(sets)
        self.ui_time[lado] = ui
        return c

    # ---------- aba Visual
    def _aba_visual(self):
        w, v = self._pagina()
        c, cv = self._cartao("O que aparece no vídeo")
        for chave, rotulo in (("placar", "Placar"), ("relogio", "Relógio no placar"),
                              ("trajetoria", "Rastro da bola"),
                              ("marcador_bola", "Círculo em volta da bola"),
                              ("banner_set", "Faixa animada de fim de set / jogo"),
                              ("banner_ponto", "Faixa animada a cada ponto")):
            ck = QCheckBox(rotulo)
            ck.setChecked(self.e.visual[chave])
            ck.toggled.connect(lambda val, k=chave: self._set_visual(k, val))
            cv.addWidget(ck)
        v.addWidget(c)

        c, cv = self._cartao("Rastro e círculo da bola aparecem em")
        for chave, rotulo in (("bola_na_tela", "Tela"),
                              ("bola_na_partida", "Gravação da partida"),
                              ("bola_no_replay", "Replay (corte do Espaço)")):
            ck = QCheckBox(rotulo)
            ck.setChecked(self.e.visual[chave])
            ck.toggled.connect(lambda val, k=chave: self._set_visual(k, val))
            cv.addWidget(ck)
        cv.addWidget(self._dica("O placar aparece em todos. Desligado aqui = vídeo limpo, "
                                "só com o placar."))
        v.addWidget(c)

        c, cv = self._cartao("Rastro da bola")
        h = QHBoxLayout()
        h.addWidget(QLabel("Cor"))
        self.bt_cor_rastro = QPushButton()
        self.bt_cor_rastro.setFixedSize(34, 34)
        self.bt_cor_rastro.clicked.connect(self._escolher_cor_rastro)
        self._pintar_botao(self.bt_cor_rastro, self.e.visual["cor_trajetoria"])
        h.addWidget(self.bt_cor_rastro)
        h.addStretch(1)
        cv.addLayout(h)
        cv.addWidget(QLabel("Comprimento"))
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(10, 90)
        sl.setValue(int(self.e.cfg["pontos_trajetoria"]))
        sl.valueChanged.connect(lambda val: self._set_cfg("pontos_trajetoria", val))
        cv.addWidget(sl)
        v.addWidget(c)

        c, cv = self._cartao("Marca")
        h = QHBoxLayout()
        bl = QPushButton("Escolher logo (PNG)…")
        bl.clicked.connect(self._escolher_logo)
        br = QPushButton("Remover")
        br.clicked.connect(lambda: (self._set_visual("logo", ""), self._atualizar_logo_ui()))
        h.addWidget(bl, 1)
        h.addWidget(br)
        cv.addLayout(h)
        self.lb_logo = self._dica("")
        cv.addWidget(self.lb_logo)
        self._atualizar_logo_ui()
        cv.addWidget(QLabel("Texto no canto inferior (ex.: @seuinstagram)"))
        ed = QLineEdit(self.e.visual["marca_dagua"])
        ed.setMaxLength(32)
        ed.setPlaceholderText("vazio = não mostra")
        ed.textEdited.connect(lambda t: self._set_visual("marca_dagua", t))
        cv.addWidget(ed)
        v.addWidget(c)

        c, cv = self._cartao("Seguir bola")
        cv.addWidget(QLabel("Zoom"))
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(12, 30)
        sl.setValue(int(round(self.e.cfg["zoom_acompanhar"] * 10)))
        sl.valueChanged.connect(lambda val: self._set_cfg("zoom_acompanhar", val / 10))
        cv.addWidget(sl)
        cv.addWidget(self._dica("A C270 não gira sozinha: é um zoom digital que segue a bola e "
                                "volta para a quadra toda quando a perde. Quanto mais zoom, "
                                "menos nítido (a câmera é 720p)."))
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ---------- aba Bola
    def _aba_bola(self):
        w, v = self._pagina()
        c, cv = self._cartao("Como achar a bola")
        self.cb_detector = QComboBox()
        for chave, (rotulo, _arq) in motor_ia.MODELOS.items():
            self.cb_detector.addItem(rotulo + ("  — recomendado" if chave == "ia_precisa" else ""),
                                     chave)
        self.cb_detector.addItem("Cor + movimento (modo antigo)", "cor")
        self.cb_detector.setCurrentIndex(
            max(0, self.cb_detector.findData(self.e.cfg["detector_bola"])))
        self.cb_detector.currentIndexChanged.connect(self._trocar_detector)
        cv.addWidget(self.cb_detector)
        self.lb_detector = self._dica("")
        cv.addWidget(self.lb_detector)
        if not motor_ia.disponivel():
            self.lb_detector.setText(f"⚠ Motor de IA indisponível: {motor_ia.erro_motor()}")
        cv.addWidget(self._dica(
            "A IA é o motor open source VballNet (pasta motor_vball), treinado só com vôlei: "
            "acha a bola de qualquer cor, sem precisar ensinar nada. A imagem sai uns 0,4 s "
            "atrasada porque a IA olha 9 quadros de cada vez. PC travando? Use a IA rápida."))
        v.addWidget(c)

        c, cv = self._cartao("Área de jogo")
        cv.addWidget(self._dica("Tem gente se mexendo fora da quadra (banco, lateral, "
                                "arquibancada)? Marque a área de jogo: o que aparecer fora dela "
                                "nunca vira bola nem rastro."))
        h = QHBoxLayout()
        self.bt_area = QPushButton("▭  Marcar área de jogo", objectName="grande")
        self.bt_area.setCheckable(True)
        self.bt_area.toggled.connect(self._modo_area)
        h.addWidget(self.bt_area, 1)
        bl = QPushButton("Limpar")
        bl.clicked.connect(lambda: self._area_marcada(None))
        h.addWidget(bl)
        cv.addLayout(h)
        self.lb_area = self._dica("")
        cv.addWidget(self.lb_area)
        v.addWidget(c)

        c, cv = self._cartao("Ensinar a cor da bola (só no modo antigo)")
        cv.addWidget(self._dica("Peça para alguém segurar a bola parada na frente da câmera, "
                                "clique no botão e depois clique em cima da bola na tela."))
        h = QHBoxLayout()
        self.bt_cor_bola = QPushButton("🎯  Ensinar cor da bola", objectName="grande")
        self.bt_cor_bola.setCheckable(True)
        self.bt_cor_bola.toggled.connect(self._modo_cor)
        h.addWidget(self.bt_cor_bola, 1)
        self.amostra_bola = QFrame()
        self.amostra_bola.setFixedSize(44, 44)
        h.addWidget(self.amostra_bola)
        cv.addLayout(h)
        self._pintar_amostra_bola()
        v.addWidget(c)

        c, cv = self._cartao("Conferir a detecção")
        self.ck_mascara = QCheckBox("Ver o que o programa enxerga (máscara)")
        self.ck_mascara.toggled.connect(self._mascara)
        cv.addWidget(self.ck_mascara)
        cv.addWidget(self._dica("Branco = onde ele acha que está a bola (na IA, o “mapa de "
                                "calor” do modelo). Só muda a tela; a gravação continua normal."))
        self.lb_det = QLabel("Bola encontrada em —", objectName="info")
        cv.addWidget(self.lb_det)
        v.addWidget(c)

        c, cv = self._cartao("Ajuste fino (só no modo antigo)")
        for chave, rotulo in (("usar_cor", "Procurar pela cor"),
                              ("usar_movimento", "Procurar pelo movimento")):
            ck = QCheckBox(rotulo)
            ck.setChecked(self.e.cfg[chave])
            ck.toggled.connect(lambda val, k=chave: self._set_cfg(k, val))
            cv.addWidget(ck)
            setattr(self, f"ck_{chave}", ck)
        form = QFormLayout()
        for chave, rotulo in (("raio_min_px", "Tamanho mínimo"), ("raio_max_px", "Tamanho máximo")):
            sp = QSpinBox()
            sp.setRange(1, 120)
            sp.setValue(int(self.e.cfg[chave]))
            sp.valueChanged.connect(lambda val, k=chave: self._set_cfg(k, val))
            form.addRow(rotulo, sp)
        cv.addLayout(form)
        cv.addWidget(self._dica("Bola branca em fundo claro é difícil: nesse caso o programa usa "
                                "só o movimento. Luz boa ajuda muito."))
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ---------- aba Gravação
    def _aba_gravacao(self):
        w, v = self._pagina()
        c, cv = self._cartao("Replay")
        form = QFormLayout()
        sp = QSpinBox()
        sp.setRange(10, 300)
        sp.setSuffix(" s")
        sp.setValue(int(self.e.cfg["segundos_do_corte"]))
        sp.valueChanged.connect(lambda val: (self._set_cfg("segundos_do_corte", val),
                                             self._rotulo_replay()))
        form.addRow("Duração do replay", sp)
        cv.addLayout(form)
        cv.addWidget(self._dica("O programa guarda sempre esse tempo na memória. Aconteceu "
                                "uma jogada boa? Aperte Espaço."))
        v.addWidget(c)

        c, cv = self._cartao("Qualidade do vídeo")
        form = QFormLayout()
        cq = QComboBox()
        for rot, crf in QUALIDADES:
            cq.addItem(rot, crf)
        cq.setCurrentIndex(max(0, cq.findData(int(self.e.cfg["qualidade_crf"]))))
        cq.currentIndexChanged.connect(lambda i: self._set_cfg("qualidade_crf", cq.itemData(i)))
        form.addRow("Qualidade", cq)
        cd = QComboBox()
        for rot, pre in DESEMPENHO:
            cd.addItem(rot, pre)
        cd.setCurrentIndex(max(0, cd.findData(self.e.cfg["preset_x264"])))
        cd.currentIndexChanged.connect(lambda i: self._set_cfg("preset_x264", cd.itemData(i)))
        form.addRow("Uso do PC", cd)
        cv.addLayout(form)
        ck = QCheckBox("Começar gravando ao abrir o programa")
        ck.setChecked(self.e.cfg["gravar_partida_inteira"])
        ck.toggled.connect(lambda val: self._set_cfg("gravar_partida_inteira", val))
        cv.addWidget(ck)
        cv.addWidget(self._dica("Vale para a próxima gravação. Partida de 30 min ≈ 0,5–1 GB. "
                                "Se a imagem travar, use “Leve — PC fraco”."))
        v.addWidget(c)

        c, cv = self._cartao("Melhores momentos (IA)")
        cv.addWidget(self._dica(
            "Depois do jogo, o motor VballNet separa sozinho cada rally da partida gravada e "
            "monta um vídeo só com os rallies, mais um reel vertical (9:16) de cada um, "
            "seguindo a bola — pronto para Instagram/WhatsApp."))
        self.bt_melhores = QPushButton("✨  Gerar da última partida", objectName="grande")
        self.bt_melhores.clicked.connect(lambda: self._gerar_melhores(None))
        cv.addWidget(self.bt_melhores)
        bo = QPushButton("Escolher outro vídeo (ex.: cartão da action cam)…")
        bo.clicked.connect(self._gerar_melhores_de_arquivo)
        cv.addWidget(bo)
        self.bt_melhores_outro = bo
        ck = QCheckBox("Criar também os reels verticais")
        ck.setChecked(self.e.cfg["gerar_reels"])
        ck.toggled.connect(lambda val: self._set_cfg("gerar_reels", val))
        cv.addWidget(ck)
        self.lb_melhores = QLabel("", objectName="info")
        self.lb_melhores.setWordWrap(True)
        cv.addWidget(self.lb_melhores)
        v.addWidget(c)

        c, cv = self._cartao("Vídeos salvos")
        h = QHBoxLayout()
        b1 = QPushButton("Pasta de replays")
        b1.clicked.connect(lambda: abrir_no_sistema(pasta_abs(self.e.cfg["pasta_cortes"])))
        b2 = QPushButton("Pasta de partidas")
        b2.clicked.connect(lambda: abrir_no_sistema(pasta_abs(self.e.cfg["pasta_partidas"])))
        h.addWidget(b1)
        h.addWidget(b2)
        cv.addLayout(h)
        self.lista = QListWidget()
        self.lista.setMinimumHeight(260)
        self.lista.itemDoubleClicked.connect(
            lambda it: abrir_no_sistema(it.data(Qt.ItemDataRole.UserRole)))
        cv.addWidget(self.lista)
        cv.addWidget(self._dica("Dois cliques para assistir."))
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ---------- aba Câmera
    def _aba_camera(self):
        w, v = self._pagina()
        c, cv = self._cartao("Fonte de imagem")
        self.cb_camera = QComboBox()
        self.nomes_cameras = vc.listar_cameras()
        if self.nomes_cameras:
            for i, nome in enumerate(self.nomes_cameras):
                self.cb_camera.addItem(f"{i} · {nome}", i)
        else:
            for i in range(5):
                self.cb_camera.addItem(f"Câmera {i}", i)
        atual = int(self.e.cfg["camera"])
        if self.nomes_cameras and atual >= len(self.nomes_cameras):
            atual = self.e.cfg["camera"] = 0  # a câmera escolhida antes não está ligada
        self.cb_camera.setCurrentIndex(max(0, self.cb_camera.findData(atual)))
        cv.addWidget(self.cb_camera)
        h = QHBoxLayout()
        b = QPushButton("Conectar", objectName="grande")
        b.clicked.connect(lambda: self._trocar_fonte(self.cb_camera.currentData()))
        h.addWidget(b, 1)
        bv = QPushButton("Usar vídeo de teste…")
        bv.clicked.connect(self._abrir_video)
        h.addWidget(bv)
        cv.addLayout(h)
        self.lb_cam_info = self._dica("")
        cv.addWidget(self.lb_cam_info)
        v.addWidget(c)

        c, cv = self._cartao("Dicas")
        cv.addWidget(self._dica(
            "• A lista mostra o nome de cada câmera ligada. Ligou a C270 com o programa "
            "aberto? Feche e abra de novo para ela aparecer.\n"
            "• Na primeira vez com cada câmera o programa testa os modos dela (alguns "
            "segundos) e depois lembra o melhor.\n"
            "• Posicione a câmera alta, de lado para a rede, pegando a quadra inteira.\n"
            "• Imagem lenta (menos de 30 fps)? A C270 reduz o fps com pouca luz: acenda mais luz.\n"
            "• Feche Teams/Meet/OBS antes: só um programa usa a câmera por vez."))
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ================================================= motor
    def _ligar_motor(self, fonte, gravar):
        self.preview.img = None
        self.preview.mensagem = ("Conectando à câmera…\n\nNa primeira vez o programa testa "
                                 "os modos da câmera e pode levar alguns segundos.")
        self.preview.update()
        self.lb_fonte.setText(os.path.basename(fonte) if isinstance(fonte, str)
                              else self._nome_camera(fonte))
        m = Motor(self.e, fonte, self.ffmpeg, gravar)
        m.novo_quadro.connect(self._quadro)
        m.info.connect(self._info)
        m.aviso.connect(self.preview.toast)
        m.arquivo_salvo.connect(self._arquivo_salvo)
        m.gravacao.connect(self._gravacao)
        m.cor_aprendida.connect(self._cor_aprendida)
        m.falhou.connect(self._falhou)
        m.camera_trocada.connect(self._camera_trocada)
        self.motor = m
        m.start()

    def _desligar_motor(self):
        if self.motor:
            self.motor.parar()
            self.motor.wait()
            self.motor = None

    def _trocar_fonte(self, fonte):
        if self.gravando and QMessageBox.question(
                self, NOME_APP, "Trocar a câmera encerra a gravação atual (o arquivo fica salvo). "
                                "Continuar?") != QMessageBox.StandardButton.Yes:
            return
        self._desligar_motor()
        if isinstance(fonte, int):
            self._set_cfg("camera", fonte)
        self._ligar_motor(fonte, False)

    def _abrir_video(self):
        arq, _ = QFileDialog.getOpenFileName(self, "Vídeo de teste", vc.PASTA,
                                             "Vídeos (*.mp4 *.mov *.avi *.mkv)")
        if arq:
            self._trocar_fonte(arq)

    # ---- sinais do motor
    def _quadro(self):
        if self.motor:
            img = self.motor.pegar_quadro()
            if img is not None:
                self.preview.definir_imagem(img)

    def _info(self, i):
        self.lb_fps.setText(f"{i['fps']:.0f} fps")
        self.lb_buffer.setText(f"Replay pronto: {i['buffer']:.0f} s")
        self.lb_det.setText(f"Bola encontrada em {i['det']:.0f}% dos quadros (últimos 3 s)")
        self.st_camera.setText(f"{self.lb_fonte.text()}  ·  {i['w']}×{i['h']}  ·  {i['fps']:.1f} fps")
        self.st_bola.setText(f"{i['detector']}  ·  bola em {i['det']:.0f}%")
        extra = f"  ·  {i['ia_ms']:.0f} ms a cada 9 quadros" if i["ia_ms"] else ""
        if motor_ia.disponivel() or i["detector"] == "Cor + movimento":
            self.lb_detector.setText(f"Usando: {i['detector']}{extra}")
        if i["ia_atrasada"] and not getattr(self, "_avisou_ia", False):
            self._avisou_ia = True
            self.preview.toast("O PC não está dando conta da IA precisa: troque para IA rápida "
                               "na aba Bola", "erro")
        self.lb_cam_info.setText(f"Recebendo {i['w']}×{i['h']} a {i['fps']:.1f} fps.")
        if i["rec"]:
            self.rec_info = (i["rec_s"], i["rec_mb"])

    def _gravacao(self, ligado, caminho):
        self.gravando = ligado
        self.bt_rec.setProperty("gravando", "true" if ligado else "false")
        self.bt_rec.style().unpolish(self.bt_rec)
        self.bt_rec.style().polish(self.bt_rec)
        if ligado:
            self.rec_info = (0.0, 0.0)
            self.lb_rec.show()
            self.preview.toast("Gravando a partida", "erro")
        else:
            self.lb_rec.hide()
            self.bt_rec.setText("●   GRAVAR")
            self.preview.toast("Finalizando o arquivo da partida…", "info")
        self._tique()

    def _arquivo_salvo(self, caminho, tipo):
        nome = "Replay salvo" if tipo == "replay" else "Partida salva"
        self.preview.toast(f"{nome}: {os.path.basename(caminho)}", "ok")
        self._atualizar_arquivos()

    def _cor_aprendida(self, hsv):
        self._agendar_salvar()
        self._pintar_amostra_bola()
        self.ck_usar_cor.setChecked(self.e.cfg["usar_cor"])
        extra = "" if self.e.cfg["usar_cor"] else " (bola sem cor forte: usando movimento)"
        self.preview.toast("Cor da bola aprendida" + extra, "ok")

    def _camera_trocada(self, idx):
        self.lb_fonte.setText(self._nome_camera(idx))
        i = self.cb_camera.findData(idx)
        if i >= 0:
            self.cb_camera.setCurrentIndex(i)

    def _nome_camera(self, idx):
        nomes = self.nomes_cameras
        return f"Câmera {idx}" + (f" · {nomes[idx]}" if idx < len(nomes) else "")

    def _falhou(self, msg):
        self.preview.img = None
        self.preview.mensagem = msg
        self.preview.update()
        self.lateral.setCurrentIndex(4)

    # ================================================= ações
    def _alternar_gravacao(self):
        if self.motor:
            self.motor.comando("rec_desligar" if self.gravando else "rec_ligar")

    def _replay(self):
        if self.motor:
            self.motor.comando("replay")

    def _ponto(self, lado):
        r = self.e.ponto(lado)
        nome = self.e.placar[f"nome_{lado}"].upper()
        if r == "set":
            self.preview.toast(f"Set para {nome}! Novo set começando", "ok")
        elif r == "jogo":
            self.preview.toast(f"Fim de jogo — {nome} venceu", "ok")
        self._mudou_placar()

    def _ajustar(self, campo, d):
        self.e.ajustar(campo, d)
        self._mudou_placar()

    def _saque(self, lado):
        self.e.definir_saque(lado)
        self._mudou_placar()

    def _desfazer(self):
        if self.e.desfazer():
            self.preview.toast("Desfeito", "info")
        self._mudou_placar()

    def _trocar(self):
        self.e.trocar_lados()
        for lado in ("a", "b"):
            self.ui_time[lado]["nome"].setText(self.e.placar[f"nome_{lado}"])
        self._mudou_placar()

    def _novo_jogo(self):
        if QMessageBox.question(self, NOME_APP, "Zerar pontos, sets e cronômetro?") == \
                QMessageBox.StandardButton.Yes:
            self.e.novo_jogo()
            self._mudou_placar()

    def _relogio(self):
        self.e.relogio_alternar()
        self._tique()

    def _seguir(self, ligado):
        self.e.seguir = ligado

    def _mascara(self, ligado):
        self.e.mascara = ligado
        self.preview.mostrar_area = ligado
        self.preview.update()

    def _modo_cor(self, ligado):
        self.preview.modo_cor = ligado
        self.preview.setCursor(Qt.CursorShape.CrossCursor if ligado else Qt.CursorShape.ArrowCursor)
        if ligado:
            self.bt_seguir.setChecked(False)
        self.preview.update()

    def _clique_preview(self, nx, ny):
        if self.preview.modo_cor and self.motor:
            self.motor.comando("cor", nx, ny)
            self.bt_cor_bola.setChecked(False)

    def _tela_cheia(self):
        cheia = not self.isFullScreen()
        self.lateral.setVisible(not cheia)
        self.controles.setVisible(not cheia)
        self.barra.setVisible(not cheia)
        self.statusBar().setVisible(not cheia)
        self.showFullScreen() if cheia else self.showNormal()

    def _esc(self):
        if self.preview.modo_area:
            self.bt_area.setChecked(False)
        elif self.preview.modo_cor:
            self.bt_cor_bola.setChecked(False)
        elif self.isFullScreen():
            self._tela_cheia()

    def _atalhos(self):
        for tecla, fn in (("Space", self._replay), ("R", self._alternar_gravacao),
                          ("1", lambda: self._ponto("a")), ("2", lambda: self._ponto("b")),
                          ("Ctrl+Z", self._desfazer), ("F", self.bt_seguir.toggle),
                          ("F11", self._tela_cheia), ("Esc", self._esc)):
            QShortcut(QKeySequence(tecla), self, activated=fn)

    # ================================================= estado → tela
    def _set_placar(self, k, val):
        with self.e.lock:
            self.e.placar[k] = val
        self._mudou_placar()

    def _set_visual(self, k, val):
        with self.e.lock:
            self.e.visual[k] = val
        self._agendar_salvar()

    def _set_cfg(self, k, val):
        self.e.cfg[k] = val
        self._agendar_salvar()

    def _agendar_salvar(self):
        self._salvar_timer.start()

    def _mudou_placar(self):
        self._atualizar_placar_ui()
        self._agendar_salvar()

    def _pintar_botao(self, bt, cor):
        bt.setStyleSheet(f"QPushButton {{ background: {cor}; border: 2px solid #2E3139; "
                         f"border-radius: 9px; }} QPushButton:hover {{ border-color: #FFFFFF; }}")

    def _atualizar_placar_ui(self):
        p = self.e.placar
        for lado in ("a", "b"):
            ui = self.ui_time[lado]
            cor = p[f"cor_{lado}"]
            ui["faixa"].setStyleSheet(f"background:{cor}; border-radius:3px;")
            self._pintar_botao(ui["cor"], cor)
            ui["pontos"].setText(str(p[f"pontos_{lado}"]))
            ui["sets"].setText(str(p[f"sets_{lado}"]))
            ui["saque"].setChecked(p["saque"] == lado)
            nome = (p[f"nome_{lado}"] or f"Time {lado.upper()}").upper()
            bt = self.bt_pa if lado == "a" else self.bt_pb
            bt.setText(f"+1  {nome if len(nome) <= 11 else nome[:10] + '…'}")
            bt.setToolTip(f"Ponto para {nome} (atalho: {'1' if lado == 'a' else '2'})")
            bt.setStyleSheet(f"QPushButton {{ border-left: 5px solid {cor}; }}")
        self._rotulo_replay()

    def _rotulo_replay(self):
        self.bt_replay.setText(f"⟲   REPLAY  {int(self.e.cfg['segundos_do_corte'])} s")

    def _tique(self):
        t = self.e.tempo()
        self.lb_relogio.setText(fmt_tempo(t))
        self.bt_relogio.setText("⏸  Pausar" if self.e.relogio_rodando() else "▶  Iniciar")
        if self.gravando:
            seg, mb = self.rec_info
            piscar = "●" if int(time.monotonic() * 2) % 2 == 0 else "○"
            tam = f"{mb / 1000:.2f} GB" if mb >= 1000 else f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"
            self.lb_rec.setText(f"{piscar}  REC  {fmt_tempo(seg)}  ·  {tam}")
            self.bt_rec.setText(f"■   PARAR  {fmt_tempo(seg)}")

    def _atualizar_disco(self):
        try:
            livre = shutil.disk_usage(vc.PASTA).free / 1e9
            self.st_disco.setText(f"Disco livre: {livre:.0f} GB")
        except OSError:
            pass

    def _atualizar_arquivos(self):
        itens = []
        for chave, tipo in (("pasta_cortes", "Replay"), ("pasta_partidas", "Partida")):
            pasta = pasta_abs(self.e.cfg[chave])
            for raiz, pastas, nomes in os.walk(pasta):
                pastas[:] = [p for p in pastas if p.endswith("_reels")]
                for nome in nomes:
                    if not nome.lower().endswith(".mp4"):
                        continue
                    cam = os.path.join(raiz, nome)
                    try:
                        st = os.stat(cam)
                    except OSError:
                        continue
                    t = ("Reel" if raiz.endswith("_reels") else
                         "Melhores momentos" if "_melhores_momentos" in nome else tipo)
                    itens.append((st.st_mtime, cam, t, st.st_size))
        itens.sort(reverse=True)
        self.lista.clear()
        for mtime, cam, tipo, tam in itens[:60]:
            quando = datetime.fromtimestamp(mtime).strftime("%d/%m  %H:%M")
            icone = {"Replay": "⟲", "Melhores momentos": "✨", "Reel": "▯"}.get(tipo, "●")
            it = QListWidgetItem(f"{icone}  {tipo}  ·  {quando}  ·  {tam / 1e6:.0f} MB")
            it.setData(Qt.ItemDataRole.UserRole, cam)
            it.setToolTip(cam)
            self.lista.addItem(it)
        if not itens:
            it = QListWidgetItem("Nenhum vídeo ainda")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.lista.addItem(it)

    def _modo_area(self, ligado):
        self.preview.modo_area = ligado
        self.preview.setCursor(Qt.CursorShape.CrossCursor if ligado else Qt.CursorShape.ArrowCursor)
        if ligado:
            self.bt_cor_bola.setChecked(False)
            self.bt_seguir.setChecked(False)
        self.preview.update()

    def _area_marcada(self, area):
        self._set_cfg("area_jogo", area)
        self.preview.area = area
        self.bt_area.setChecked(False)
        self._rotulo_area()
        self.preview.mostrar_area = True
        QTimer.singleShot(3000, lambda: (setattr(self.preview, "mostrar_area", False),
                                         self.preview.update()))
        self.preview.toast("Área de jogo marcada" if area else "Área de jogo removida (tela toda)",
                           "ok" if area else "info")

    def _rotulo_area(self):
        a = self.e.cfg.get("area_jogo")
        self.lb_area.setText("Sem área marcada: a tela toda vale." if not a else
                             f"Marcada: {int((a[2] - a[0]) * 100)}% da largura × "
                             f"{int((a[3] - a[1]) * 100)}% da altura.")

    def _trocar_detector(self, i):
        chave = self.cb_detector.itemData(i)
        self._set_cfg("detector_bola", chave)
        self._avisou_ia = False
        self.preview.toast(f"Achando a bola com: {self.cb_detector.itemText(i).split('  —')[0]}",
                           "info")

    def _ultima_partida(self):
        pasta = pasta_abs(self.e.cfg["pasta_partidas"])
        videos = [os.path.join(pasta, n) for n in os.listdir(pasta)
                  if n.startswith("partida_") and n.endswith(".mp4")
                  and "_melhores_momentos" not in n]
        if self.gravando and self.motor and self.motor.partida:
            atual = os.path.abspath(self.motor.partida.caminho)
            videos = [v for v in videos if os.path.abspath(v) != atual]
        return max(videos, key=os.path.getmtime) if videos else None

    def _gerar_melhores_de_arquivo(self):
        arq, _ = QFileDialog.getOpenFileName(self, "Vídeo do jogo", pasta_abs(
            self.e.cfg["pasta_partidas"]), "Vídeos (*.mp4 *.mov *.avi *.mkv)")
        if arq:
            self._gerar_melhores(arq)

    def _gerar_melhores(self, video):
        if getattr(self, "_gerando", False):
            return
        video = video or self._ultima_partida()
        if not video:
            self.preview.toast("Nenhuma partida gravada ainda (a que está gravando agora "
                               "precisa ser parada antes)", "erro")
            return
        cap = cv2.VideoCapture(video)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        cap.release()
        cfg = self.e.cfg
        modelo = cfg["detector_bola"] if cfg["detector_bola"] in motor_ia.MODELOS \
            else "ia_precisa"
        self._gerando = True
        self.bt_melhores.setEnabled(False)
        self.bt_melhores_outro.setEnabled(False)
        self.lb_melhores.setText(f"{os.path.basename(video)}: começando…")
        sinais = self._sinais_melhores

        def trabalho():
            try:
                r = motor_ia.gerar_melhores_momentos(
                    video, fps, self.ffmpeg, cfg["qualidade_crf"], cfg["preset_x264"],
                    reels=cfg["gerar_reels"], modelo=modelo,
                    progresso=lambda txt: sinais.progresso.emit(txt))
                sinais.fim.emit(r, "")
            except Exception as ex:  # noqa: BLE001
                sinais.fim.emit({}, str(ex))

        threading.Thread(target=trabalho, daemon=True).start()

    def _melhores_progresso(self, txt):
        self.lb_melhores.setText(txt)

    def _melhores_fim(self, r, erro):
        self._gerando = False
        self.bt_melhores.setEnabled(True)
        self.bt_melhores_outro.setEnabled(True)
        if erro:
            self.lb_melhores.setText(f"⚠ Não deu certo: {erro}")
            self.preview.toast("Erro ao gerar os melhores momentos", "erro")
        elif not r.get("rallies"):
            self.lb_melhores.setText("Nenhum rally encontrado nesse vídeo (a IA não achou a "
                                     "bola em jogo por tempo suficiente).")
        else:
            partes = [f"{r['rallies']} rallies"]
            if r.get("melhores"):
                partes.append(os.path.basename(r["melhores"]))
            if r.get("reels"):
                partes.append(f"{len(r['reels'])} reels")
            self.lb_melhores.setText("✓ " + "  ·  ".join(partes))
            self.preview.toast(f"Melhores momentos prontos: {r['rallies']} rallies", "ok")
            if r.get("melhores"):
                abrir_no_sistema(os.path.dirname(r["melhores"]))
        self._atualizar_arquivos()

    def _atualizar_logo_ui(self):
        logo = self.e.visual["logo"]
        self.lb_logo.setText(os.path.basename(logo) if logo else "Sem logo")

    def _pintar_amostra_bola(self):
        lo, hi = self.e.cfg["cor_bola_hsv_min"], self.e.cfg["cor_bola_hsv_max"]
        hh = ((lo[0] + ((hi[0] - lo[0]) % 180) / 2) % 180)
        bgr = cv2.cvtColor(np.uint8([[[hh, 200, 230]]]), cv2.COLOR_HSV2BGR)[0, 0]
        cor = f"rgb({bgr[2]},{bgr[1]},{bgr[0]})" if self.e.cfg["usar_cor"] else "#DDDDDD"
        self.amostra_bola.setStyleSheet(f"background:{cor}; border-radius:22px; "
                                        f"border:2px solid #2E3139;")
        self.amostra_bola.setToolTip("Cor que o programa procura")

    # ---- diálogos
    def _escolher_cor_time(self, lado):
        c = QColorDialog.getColor(QColor(self.e.placar[f"cor_{lado}"]), self, "Cor do time")
        if c.isValid():
            self._set_placar(f"cor_{lado}", c.name())

    def _escolher_cor_rastro(self):
        c = QColorDialog.getColor(QColor(self.e.visual["cor_trajetoria"]), self, "Cor do rastro")
        if c.isValid():
            self._set_visual("cor_trajetoria", c.name())
            self._pintar_botao(self.bt_cor_rastro, c.name())

    def _escolher_logo(self):
        arq, _ = QFileDialog.getOpenFileName(self, "Logo", vc.PASTA, "Imagens (*.png *.jpg *.jpeg)")
        if arq:
            self._set_visual("logo", arq)
            self._atualizar_logo_ui()

    def closeEvent(self, ev):
        if self.gravando:
            self.preview.toast("Fechando e salvando a gravação…", "info")
            self.preview.repaint()
        self._desligar_motor()
        vc.salvar_config(self.e.cfg)
        ev.accept()


def _imagens_ui():
    """Desenha os ícones do tema (toggle, setas) numa pasta temporária para o QSS usar."""
    import tempfile
    pasta = os.path.join(tempfile.gettempdir(), "camera_volei_ui")
    os.makedirs(pasta, exist_ok=True)

    def salvar(nome, w, h, desenhar):
        pm = QPixmap(w * 2, h * 2)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(2, 2)
        desenhar(p)
        p.end()
        pm.save(os.path.join(pasta, nome))

    def toggle(ligado):
        def d(p):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E5484D" if ligado else "#34373F"))
            p.drawRoundedRect(QRectF(1, 1, 34, 18), 9, 9)
            p.setBrush(QColor("#FFFFFF" if ligado else "#A5A8B0"))
            p.drawEllipse(QPointF(26 if ligado else 10, 10), 7, 7)
        return d

    def seta(para_cima):
        def d(p):
            p.setPen(QPen(QColor("#A5A8B0"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            ys = (8, 4) if para_cima else (4, 8)
            p.drawPolyline([QPointF(2, ys[0]), QPointF(6, ys[1]), QPointF(10, ys[0])])
        return d

    salvar("toggle_on.png", 36, 20, toggle(True))
    salvar("toggle_off.png", 36, 20, toggle(False))
    salvar("seta_baixo.png", 12, 12, seta(False))
    salvar("seta_cima.png", 12, 12, seta(True))
    return pasta.replace("\\", "/")


def paleta_escura(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for papel, cor in ((QPalette.ColorRole.Window, "#0E0F12"),
                       (QPalette.ColorRole.WindowText, "#E7E8EC"),
                       (QPalette.ColorRole.Base, "#1B1D22"),
                       (QPalette.ColorRole.AlternateBase, "#15161A"),
                       (QPalette.ColorRole.Text, "#E7E8EC"),
                       (QPalette.ColorRole.Button, "#23252C"),
                       (QPalette.ColorRole.ButtonText, "#E7E8EC"),
                       (QPalette.ColorRole.Highlight, "#E5484D"),
                       (QPalette.ColorRole.HighlightedText, "#FFFFFF"),
                       (QPalette.ColorRole.ToolTipBase, "#1B1D22"),
                       (QPalette.ColorRole.ToolTipText, "#E7E8EC"),
                       (QPalette.ColorRole.PlaceholderText, "#6B6E78")):
        pal.setColor(papel, QColor(cor))
    app.setPalette(pal)
    app.setStyleSheet(ESTILO.replace("@UI@", _imagens_ui()))


def main():
    ap = argparse.ArgumentParser(description=NOME_APP)
    ap.add_argument("--video", help="usar um arquivo de vídeo no lugar da webcam (teste)")
    ap.add_argument("--demo", help="(teste) simula um jogo, salva um print nesta pasta e fecha")
    args = ap.parse_args()

    if sys.stdout is None:  # aberto com pythonw (sem janela preta): manda os logs para arquivo
        log = open(os.path.join(vc.PASTA, "camera_volei.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    cv2.setNumThreads(2)
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("camera.volei.app")
    app = QApplication(sys.argv)
    app.setApplicationName(NOME_APP)
    paleta_escura(app)
    janela = Janela(args)
    janela.showMaximized()
    if args.demo:
        _roteiro_demo(janela, args.demo)
    return app.exec()


def _roteiro_demo(j, pasta):
    """Simula uma partida para conferir a interface e o visual sem precisar de câmera."""
    os.makedirs(pasta, exist_ok=True)
    passos = []

    def em(seg, fn):
        passos.append(QTimer.singleShot(int(seg * 1000), fn))

    def pontos(seq):
        for lado in seq:
            j._ponto(lado)

    em(1.0, lambda: pontos("ab" * 18 + "a" * 6))   # 24 x 18
    em(2.0, lambda: pontos("a"))                    # 25 x 18: fecha o 1º set
    em(3.5, lambda: j.grab().save(os.path.join(pasta, "1_fim_set.png")))
    em(3.6, lambda: j.preview.img.save(os.path.join(pasta, "1_programa_fim_set.png")))
    em(9.0, lambda: pontos("abbabaab"))
    em(9.3, lambda: j._replay())
    em(10.5, lambda: j.grab().save(os.path.join(pasta, "2_janela.png")))
    em(10.6, lambda: j.preview.img.save(os.path.join(pasta, "2_programa.png")))
    em(11.0, lambda: j.lateral.setCurrentIndex(3))
    em(13.0, lambda: j.grab().save(os.path.join(pasta, "3_aba_gravacao.png")))
    em(13.5, lambda: j.bt_seguir.setChecked(True))
    em(16.0, lambda: j.preview.img.save(os.path.join(pasta, "4_programa_seguindo.png")))
    em(16.5, lambda: j.lateral.setCurrentIndex(1))
    em(17.0, lambda: j.grab().save(os.path.join(pasta, "5_aba_visual.png")))
    em(18.0, j.close)


if __name__ == "__main__":
    sys.exit(main())
