# Câmera Vôlei

Programa com janela (estilo OBS) que grava o jogo com a webcam (Logitech C270) apontada para a quadra.
O vídeo sai com **cara de transmissão**: placar com nomes, sets e pontos, relógio, rastro da bola e
faixa animada de fim de set. O programa guarda sempre o último minuto na memória e salva um
**replay** quando você aperta um botão. Também grava a partida inteira.

**O que aparece na tela grande é exatamente o que vai para o vídeo.** Os avisos que surgem por cima
(“Replay salvo”, “Gravando…”) aparecem só na tela e não entram no vídeo.

## Como abrir

Dê dois cliques em **“Câmera Vôlei”** na Área de Trabalho (ou em `Abrir Camera Volei.bat` nesta pasta).

- Na primeira vez em outro computador, ele instala sozinho o que precisa (leva alguns minutos).
  Só precisa ter o **Python 3.14** (a versão que o motor de IA exige) (https://www.python.org/downloads/, marque
  **“Add Python to PATH”** no instalador).

## A janela

```
┌──────────────────────────────────────────────┬──────────────────────┐
│ PROGRAMA            Replay pronto · fps · REC│ Placar│Visual│Bola│…  │
│                                              │                      │
│         imagem da câmera + placar            │  times, pontos,      │
│         (é isso que é gravado)               │  sets, cronômetro,   │
│                                              │  regras              │
├──────────────────────────────────────────────┤                      │
│ ● GRAVAR  ⟲ REPLAY  +1 CASA  +1 VISIT.  ↩  ◎ │                      │
└──────────────────────────────────────────────┴──────────────────────┘
```

### Botões de baixo

| Botão | Atalho | O que faz |
|---|---|---|
| **● GRAVAR / ■ PARAR** | R | grava a partida inteira (salva em `partidas/`) |
| **⟲ REPLAY** | Espaço | salva os últimos 60 s como um vídeo separado (em `cortes/`) |
| **+1 CASA** / **+1 VISITANTE** | 1 / 2 | ponto para o time; o placar pisca na cor do time |
| **↩** | Ctrl+Z | desfaz o último ponto (ou fim de set) |
| **◎ Seguir bola** | F | zoom digital que acompanha a bola |
| (dois cliques na imagem) | F11 | tela cheia; Esc volta |

### Abas da direita

- **Placar**: nome do jogo, nome e cor de cada time, pontos, sets, quem saca, cronômetro
  (começa sozinho no primeiro ponto) e regras. Com **“Fechar o set automaticamente”** ligado,
  ao chegar em 25 pontos com 2 de vantagem (15 no tie-break) o set é somado, a faixa
  “FIM DO SET” aparece no vídeo e os pontos zeram. Ao ganhar os sets necessários, aparece
  “FIM DE JOGO”. Errou? **Ctrl+Z**. Também tem **Trocar lados** e **Novo jogo**.
- **Visual → Rastro e círculo da bola aparecem em**: escolha separadamente **Tela**,
  **Gravação da partida** e **Replay (corte do Espaço)**. Por padrão o replay sai **sem rastro**
  (só com o placar).
- **Visual**: liga/desliga cada elemento do vídeo (placar, relógio, rastro, círculo na bola,
  faixas animadas), cor e comprimento do rastro, **logo (PNG)** no canto superior direito,
  um texto no canto inferior (ex.: @instagram) e o zoom do “Seguir bola”.
- **Bola**: **Ensinar cor da bola** (clique no botão e depois em cima da bola na tela), ver a
  máscara de detecção e ajuste fino.
- **Gravação**: duração do replay, qualidade, uso do PC, “começar gravando ao abrir” e a lista
  dos vídeos salvos (dois cliques para assistir).
- **Câmera**: escolher qual câmera usar (0, 1, 2…) e abrir um vídeo de teste.

## Motor de IA (projeto open source)

A bola é encontrada pelo **VballNet**, do projeto open source
[fast-volleyball-tracking-inference](https://github.com/asigatchov/fast-volleyball-tracking-inference)
(licença MIT). Ele fica na pasta `motor_vball/`, **copiado sem nenhuma alteração** (veja
`motor_vball/UPSTREAM.txt`). O programa conversa com ele só pelo arquivo `motor_ia.py`.

- Aba **Bola → Como achar a bola**:
  - **IA precisa**: o padrão.
  - **IA rápida**: para PC fraco.
  - **Cor + movimento**: o modo antigo. Ele confunde gente andando com a bola.
- A IA olha 9 quadros de cada vez, então a imagem sai uns **0,4 s atrasada**. Isso não aparece
  no vídeo gravado.
- **Área de jogo** (aba Bola): arraste um retângulo em volta da quadra, incluindo o alto, por
  onde a bola passa. O que aparecer fora dele (banco, lateral, gente passando) nunca vira bola
  nem rastro.
- **Melhores momentos** (aba Gravação), depois do jogo: o motor separa os rallies da partida
  gravada e cria dois tipos de vídeo em `partidas/`:
  - `…_melhores_momentos.mp4`: só os rallies, em sequência;
  - `…_reels/`: um vídeo vertical 9:16 de cada rally, seguindo a bola.
  O botão **Escolher outro vídeo** faz o mesmo com qualquer arquivo, por exemplo o cartão de
  memória de uma action cam. Nesse caso o motor procura a bola no arquivo inteiro antes.

## Primeira vez com a câmera

1. Na aba **Câmera** a lista mostra o nome de cada câmera ligada (ex.: “HD Webcam” do notebook,
   “C270 HD WEBCAM”). Escolha e clique em **Conectar**. Ligou a C270 com o programa aberto?
   Feche e abra de novo para ela aparecer na lista.
   Na primeira vez com cada câmera o programa testa sozinho os modos dela (demora uns 10 s)
   e guarda o que funcionou melhor; das próximas vezes abre em 1–2 s.
2. Aba **Bola**: peça para alguém segurar a bola parada na frente da câmera, clique em
   **Ensinar cor da bola** e clique em cima dela na tela.
3. Ligue **Ver o que o programa enxerga** e jogue a bola: ela deve aparecer como uma mancha
   branca. Desligue de novo.
4. Aba **Placar**: coloque os nomes e as cores dos times. Pronto.

Dica de posicionamento: câmera alta (tripé ou prateleira), de lado para a rede, pegando a quadra inteira.
A bola fica pequena de longe, então a detecção funciona melhor com bola colorida e boa luz.

## Sobre “Seguir bola”

A C270 não tem motor, então ela não gira sozinha. O **Seguir bola** faz um zoom digital que
acompanha a bola e volta para a quadra inteira quando a perde. Ele entra no vídeo gravado
(o que você vê é o que grava). Como a C270 é 720p, quanto mais zoom, menos nítido.

## Espaço em disco e computador

Por padrão o programa **já começa gravando a partida inteira** ao abrir (dá para desligar na aba
Gravação). O replay continua funcionando ao mesmo tempo. Para 30 minutos em 720p a 30 fps:

- **Disco:** cerca de 0,5 a 1 GB por partida. Com qualidade **Econômica**, uns 300 a 600 MB.
  O espaço livre aparece no canto inferior direito.
- **CPU:** se a imagem travar (fps abaixo de 30 na barra de cima), escolha **Uso do PC → Leve**.
- **Memória:** o replay de 60 s usa uns 150 a 300 MB de RAM.
- O arquivo da partida é gravado em pedaços: se o programa fechar de repente, o que já foi
  gravado continua abrindo.

## Problemas comuns

- **Imagem lenta (menos de 30 fps):** a C270 reduz o fps com pouca luz. Acenda mais luz.
- **Imagem preta ou travando depois de trocar de câmera/cabo:** apague a linha `"modos_camera"` do
  `config.json` para o programa testar os modos da câmera de novo.
- **“A câmera abriu mas não enviou imagem”:** feche Teams/Meet/OBS; só um programa usa a câmera por vez.
- **Não acha a bola:** ensine a cor de novo com boa luz e confira com a máscara. Bola branca em
  fundo claro é difícil: o programa passa a usar só o movimento.
- **Aviso “ffmpeg ausente” no rodapé:** os vídeos ficam maiores. Rode `pip install imageio-ffmpeg`.
- Erros ficam registrados em `camera_volei.log` nesta pasta.

## Arquivos

| Arquivo | Para quê |
|---|---|
| `Abrir Camera Volei.bat` | abre o programa (instala o que falta na primeira vez) |
| `camera_volei_app.py` | a janela, o placar e os gráficos do vídeo |
| `motor_ia.py` | ponte com o motor de IA (detecção ao vivo + melhores momentos) |
| `motor_vball/` | o projeto open source VballNet, sem alterações |
| `volei_cam.py` | câmera, gravação e o detector antigo (cor + movimento) |
| `config.json` | criado na primeira vez; guarda placar, times, cores e ajustes |
| `cortes/` e `partidas/` | os vídeos salvos |
