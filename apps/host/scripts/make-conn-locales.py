"""Writes src/locales/conn-<lang>.json: what the chat says when the line to the model dropped mid-answer
(lane CONN, the "Failed to fetch" of 11-24: the stream ended with the browser's own TypeError words, and the screen
showed them as they were).
Run from apps/host:  python scripts/make-conn-locales.py
The two sentences are src/lib/plain-errors.ts RULES 'connection' (the message, then the next step). Every entry names
all nine languages; keys are conn01.. in the order of S. The eight translations are first drafts for the owner's later
look."""
import json
import re
from pathlib import Path

LANGS = ["en", "zh", "ja", "de", "es", "ru", "pt", "ko", "fr"]
S = []


def add(en, zh, ja, de, es, ru, pt, ko, fr):
    S.append(dict(en=en, zh=zh, ja=ja, de=de, es=es, ru=ru, pt=pt, ko=ko, fr=fr))


add("Strata lost its connection to the model.",
    "Strata 与模型的连接中断了。",
    "Strata とモデルの接続が切れました。",
    "Strata hat die Verbindung zum Modell verloren.",
    "Strata ha perdido la conexión con el modelo.",
    "Strata потеряла связь с моделью.",
    "A Strata perdeu a ligação ao modelo.",
    "Strata가 모델과의 연결을 잃었습니다.",
    "Strata a perdu la connexion au modèle.")

add("The model may have stopped or be restarting. Press Try again.",
    "模型可能已停止或正在重新启动。请按“重试”。",
    "モデルが停止したか、再起動中かもしれません。[もう一度] を押してください。",
    "Das Modell wurde vielleicht beendet oder startet gerade neu. Bitte auf „Erneut versuchen“ drücken.",
    "Puede que el modelo se haya detenido o se esté reiniciando. Pulsa «Reintentar».",
    "Возможно, модель остановилась или перезапускается. Нажмите «Повторить».",
    "O modelo pode ter parado ou estar a reiniciar. Prima «Tentar novamente».",
    "모델이 중지되었거나 다시 시작 중일 수 있습니다. [다시 시도]를 누르세요.",
    "Le modèle s’est peut-être arrêté ou redémarre. Appuyez sur « Réessayer ».")

here = Path(__file__).resolve().parent.parent / "src" / "locales"
NBSP = " "


def french(text):
    text = re.sub(r"(?<=[A-Za-zÀ-ſ])'(?=[A-Za-zÀ-ſ{])", "’", text)
    text = re.sub(r"(?<=\S) ([:;?!])", NBSP + r"\1", text)
    return text


for lang in LANGS:
    data = {}
    for i, entry in enumerate(S):
        text = entry[lang]
        assert sorted(re.findall(r"\{\d+\}", entry["en"])) == sorted(re.findall(r"\{\d+\}", text)), (lang, entry["en"], text)
        data[f"conn{i + 1:02d}"] = french(text) if lang == "fr" else text
    with (here / f"conn-{lang}.json").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
print("written", len(S), "strings in", len(LANGS), "languages")
