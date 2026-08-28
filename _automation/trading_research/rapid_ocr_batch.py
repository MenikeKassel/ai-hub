from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one local RapidOCR batch")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def serialise_box(box: Any) -> list[list[float]]:
    values = box.tolist() if hasattr(box, "tolist") else box
    return [[round(float(axis), 2) for axis in point] for point in values]


def _rapidocr_rows(output: Any):
    texts = getattr(output, "txts", None)
    scores = getattr(output, "scores", None)
    boxes = getattr(output, "boxes", None)
    texts = [] if texts is None else texts
    scores = [] if scores is None else scores
    boxes = [] if boxes is None else boxes
    return zip(texts, scores, boxes)


def main() -> None:
    args = parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    from rapidocr import RapidOCR

    engine = RapidOCR()
    results: dict[str, dict[str, Any]] = {}
    for item in manifest:
        post_id = str(item["post_id"])
        text_parts: list[str] = []
        lines: list[dict[str, Any]] = []
        errors: list[str] = []
        for image in item.get("images") or []:
            try:
                output = engine(str(image))
                if output is None:
                    continue
                image_lines: list[str] = []
                for text, confidence, box in _rapidocr_rows(output):
                    clean = str(text).strip()
                    if not clean:
                        continue
                    score = round(float(confidence), 6)
                    image_lines.append(clean)
                    lines.append({
                        "text": clean,
                        "confidence": score,
                        "box": serialise_box(box),
                        "image": Path(image).name,
                    })
                if image_lines:
                    text_parts.append("\n".join(image_lines))
            except Exception as exc:
                errors.append(f"{Path(image).name}: {exc}")
        average = sum(float(line["confidence"]) for line in lines) / len(lines) if lines else 0
        results[post_id] = {
            "text": "\n\n".join(text_parts),
            "error": "; ".join(errors) if not text_parts else "",
            "average_confidence": round(average, 6),
            "lines": lines,
        }
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
