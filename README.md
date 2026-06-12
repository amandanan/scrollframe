# scrollframe

CLI em Python que prepara vídeos de portfólio para o efeito de **scroll
scrubbing** (frames sincronizados com a rolagem da página). Num comando só:

> corte → ajuste de cor (escurecer + aquecer) → zoom progressivo (opcional)
> → extração de frames → otimização → relatório

O trabalho pesado é feito pelo [FFmpeg](https://ffmpeg.org/). Este script é a
camada de conveniência: valida a entrada, monta o filtergraph dinamicamente
conforme as flags e relata quantos frames saíram (número que você usa na lógica
de scroll do site).

## Requisitos

- **Python 3.10+** (usa `X | None` nos type hints)
- **FFmpeg** (inclui `ffprobe`) — obrigatório
- **jpegoptim** — opcional, para otimizar os JPEGs

```bash
# macOS (Homebrew)
brew install ffmpeg jpegoptim

# Debian/Ubuntu
sudo apt install ffmpeg jpegoptim
```

Sem `jpegoptim` a ferramenta funciona normalmente — apenas pula a otimização e
avisa como instalá-lo.

## Instalação

```bash
git clone https://github.com/<voce>/scrollframe.git
cd scrollframe
python3 editar.py --help
```

Não há dependências de PyPI: só biblioteca padrão.

## Uso

```bash
python3 editar.py --input VIDEO --output NOME [opções]
```

Os frames são salvos em `public/frames/{output}/` como `frame-001.jpg`,
`frame-002.jpg`, …

### Exemplos

**Hero — escurecido, aquecido, com vinheta:**

```bash
python3 editar.py --input video.mp4 --output hero --start 2 --duration 12 \
    --brightness -0.06 --contrast 1.08 --temperature 5200 --vignette \
    --fps 12 --width 1280 --quality 4
```

**Sobre — com zoom progressivo:**

```bash
python3 editar.py --input video2.mp4 --output sobre --start 0 --duration 18 \
    --zoom --zoom-max 2.0 --fps 10 --width 1280
```

## Parâmetros

| Flag | Padrão | Descrição |
|------|--------|-----------|
| `--input` | — | **(obrigatório)** Caminho do vídeo bruto. |
| `--output` | — | **(obrigatório)** Nome base → `public/frames/{output}/`. |
| `--start` | `0` | Início do corte (segundos). |
| `--duration` | até o fim | Duração do corte (segundos). |
| `--brightness` | `0.0` | Brilho (`eq`): −1.0 a 1.0. Negativo escurece. |
| `--contrast` | `1.0` | Contraste (`eq`): 1.0 = neutro. |
| `--saturation` | `1.0` | Saturação (`eq`): 1.0 = neutro. |
| `--temperature` | não aplica | Temperatura em Kelvin. **Menor = mais quente.** |
| `--vignette` | desligado | Vinheta suave nas bordas. |
| `--zoom` | desligado | Zoom progressivo centralizado (`zoompan`). |
| `--zoom-max` | `2.0` | Zoom máximo ao fim do clipe. |
| `--fps` | `12` | Frames por segundo a extrair (mire em 90–150 totais). |
| `--width` | `1280` | Largura de saída (altura automática, mantém o aspecto). |
| `--quality` | `4` | Qualidade JPEG: 1 melhor … 10 pior. |
| `--force` | desligado | Sobrescreve frames existentes sem perguntar. |

## Como mirar 90–150 frames

O total de frames é aproximadamente `fps × duration`. Para um clipe de 12s:

- `--fps 10` → ~120 frames
- `--fps 12` → ~144 frames

A ferramenta avisa no relatório final se a contagem ficar fora dessa faixa.

## A ordem do filtergraph (para curiosos)

Os estágios são montados nesta ordem, e a ordem importa:

1. **scale** — reduz os pixels primeiro, acelerando todo o resto.
2. **eq** — brilho / contraste / saturação num filtro só.
3. **colortemperature** — aquece ou esfria depois da correção base.
4. **zoompan** — recorta *antes* da vinheta, para a vinheta emoldurar o resultado.
5. **vignette** — escurece as bordas da imagem já corrigida.
6. **fps** — a amostragem é a última etapa (omitida quando há `--zoom`, pois o
   `zoompan` já controla a própria taxa).

Cada estágio só entra na cadeia se a flag correspondente estiver presente.

## Licença

MIT.
