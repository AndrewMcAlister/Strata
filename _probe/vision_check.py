"""One image question against a running Strata server, with the answer checked.

Draws a picture (a code of letters and digits, some red circles, some blue squares),
sends it as an OpenAI `image_url` part and compares the answer with what was drawn.
The same test as bench/results/2026-10-02-community-rtx2080ti-11gb/image_check.py,
without that file's bench-framework imports.

    python _probe/vision_check.py [--url http://127.0.0.1:8080] [--runs 1]
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import random
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
QUESTION = ("Look at the image. What is the code written in black letters, how many red circles are there, and how "
            "many blue squares are there? Answer in exactly this format and nothing else: CODE; circles; squares")


def font(size: int):
    for f in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf",
              r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\seguiemj.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def draw(rnd: random.Random):
    """The picture, what is written on it, and the answer that counts as right."""
    code = "".join(rnd.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(3)) + "-" + str(rnd.randint(10, 99))
    circles, squares = rnd.randint(2, 5), rnd.randint(1, 4)
    w, h = 768, 512
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((40, 30), code, fill="black", font=font(72))
    cells = [(x, y) for x in range(40, w - 100, 120) for y in (190, 330)]   # non-overlapping cells
    rnd.shuffle(cells)
    for i, (x, y) in enumerate(cells[:circles + squares]):
        if i < circles:
            d.ellipse((x, y, x + 90, y + 90), fill=(220, 30, 30), outline="black", width=2)
        else:
            d.rectangle((x, y, x + 90, y + 90), fill=(30, 60, 220), outline="black", width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), code, circles, squares


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--seed", type=int, default=431)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    bad = 0
    for run in range(1, a.runs + 1):
        png, code, circles, squares = draw(rnd)
        png_path = HERE / f"vision-check-run{run}.png"
        png_path.write_bytes(png)
        req = {"model": "strata", "temperature": 0, "reasoning_effort": "none", "max_tokens": 40, "stream": False,
               "messages": [{"role": "user", "content": [
                   {"type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
                   {"type": "text", "text": QUESTION}]}]}
        head = {"Content-Type": "application/json"}
        if a.api_key:
            head["Authorization"] = "Bearer " + a.api_key
        r = urllib.request.Request(a.url.rstrip("/") + "/v1/chat/completions",
                                   data=json.dumps(req).encode("utf-8"), headers=head)
        # the server is on 127.0.0.1: an HTTP proxy set in the environment would take the request elsewhere
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        t0 = time.time()
        try:
            with opener.open(r, timeout=900) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"image run {run}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}", flush=True)
            bad += 1
            continue
        dt = time.time() - t0
        got = (body.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        want = f"{code}; {circles}; {squares}"
        ok = [p.strip() for p in got.split(";")] == [code, str(circles), str(squares)]
        print(f"image run {run} ({png_path.name}, {dt:.1f} s): expected {want!r} got {got!r} -> "
              f"{'OK' if ok else 'WRONG'}  usage={body.get('usage')}", flush=True)
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
