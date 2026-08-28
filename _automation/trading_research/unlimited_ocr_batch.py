from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one local Unlimited-OCR batch")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default="baidu/Unlimited-OCR")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    import torch
    from transformers import AutoModel, AutoTokenizer

    if not torch.cuda.is_available():
        raise RuntimeError("Unlimited-OCR requires an available CUDA GPU")
    tokenizer = AutoTokenizer.from_pretrained(
        args.model,
        trust_remote_code=True,
        local_files_only=True,
    )
    model = AutoModel.from_pretrained(
        args.model,
        trust_remote_code=True,
        use_safetensors=True,
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    ).eval().cuda()
    original_generate = model.generate

    def generate_with_limit(*values, **kwargs):
        kwargs.pop("max_length", None)
        kwargs["max_new_tokens"] = 768
        return original_generate(*values, **kwargs)

    model.generate = generate_with_limit
    results: dict[str, dict[str, str]] = {}
    for item in manifest:
        post_id = str(item["post_id"])
        parts: list[str] = []
        errors: list[str] = []
        for image in item.get("images") or []:
            try:
                text = model.infer(
                    tokenizer,
                    prompt="<image>document parsing.",
                    image_file=str(image),
                    output_path=str(Path(args.output).parent),
                    base_size=1024,
                    image_size=640,
                    crop_mode=True,
                    max_length=3600,
                    no_repeat_ngram_size=35,
                    ngram_window=128,
                    save_results=False,
                    eval_mode=True,
                )
                if text:
                    parts.append(str(text))
            except Exception as exc:
                errors.append(f"{Path(image).name}: {exc}")
        results[post_id] = {
            "text": "\n\n".join(parts),
            "error": "; ".join(errors) if not parts else "",
        }
    Path(args.output).write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
