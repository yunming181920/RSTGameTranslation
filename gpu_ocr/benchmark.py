"""Repeatable synthetic OCR benchmark, plus optional user-provided screenshots."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import time

from backend import Backend


def fixtures(directory):
    from PIL import Image, ImageDraw, ImageFont
    directory.mkdir(parents=True, exist_ok=True)
    samples = [
        ("en", "The ancient gate is sealed. Find the silver key.", "arial.ttf"),
        ("zh", "前方的道路充满危险，请准备好武器。", "msyh.ttc"),
        ("ja", "この先は危険です。気をつけてください。", "YuGothM.ttc"),
    ]
    result = []
    for language, text, font_name in samples:
        for size, background, foreground in [(32, "white", "black"), (24, "#202830", "white")]:
            image = Image.new("RGB", (1000, 180), background)
            font = ImageFont.truetype(str(Path("C:/Windows/Fonts") / font_name), size)
            ImageDraw.Draw(image).text((30, 60), text, fill=foreground, font=font)
            path = directory / f"{language}_{size}.png"
            image.save(path)
            result.append((path, text))
    blank = directory / "blank.png"
    Image.new("RGB", (1000, 180), "#202830").save(blank)
    result.append((blank, ""))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=("cpu", "dml"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--image", type=Path, action="append", default=[])
    parser.add_argument("--profile", action="store_true", help="Collect node placement; adds timing overhead")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    cases = fixtures(args.output.parent / "fixtures") + [(p, None) for p in args.image]
    profile_dir = args.output.parent / (args.device + "_profiles")
    profile_dir.mkdir(exist_ok=True)
    start = time.perf_counter()
    backend = Backend(args.device, profile_dir if args.profile else None)
    report = {**backend.info(), "init_ms": (time.perf_counter() - start) * 1000,
              "runs": args.runs, "profiling": args.profile, "det_limit_type": "max",
              "det_limit_side_len": 960, "cpu_threads": 4, "samples": []}
    for path, expected in cases:
        first = backend.recognize(str(path))
        backend.recognize(str(path))
        timings = [backend.recognize(str(path))["elapsed_ms"] for _ in range(args.runs)]
        text = "".join(line["text"] for line in first["lines"])
        report["samples"].append({"image": str(path), "expected": expected, "text": text,
            "exact_match": text == expected if expected is not None else None,
            "cold_ms": first["elapsed_ms"], "median_ms": statistics.median(timings),
            "min_ms": min(timings), "max_ms": max(timings), "times_ms": timings})
        print(args.device, path.name, round(statistics.median(timings), 1), text, flush=True)
    report["executed_nodes"] = {}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, session in backend.sessions.items():
        if not args.profile:
            continue
        events = json.loads(Path(session.end_profiling()).read_text(encoding="utf-8"))
        counts = Counter(e.get("args", {}).get("provider") for e in events if e.get("cat") == "Node")
        counts.pop(None, None)
        report["executed_nodes"][name] = dict(counts)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
