"""Fail setup unless real STORM/DSPy imports and CPU semantic retrieval work."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-embedding", action="store_true")
    args = parser.parse_args()
    root = Path(os.environ["STORM_ENV_ROOT"]).resolve()
    for line in Path(__file__).with_name("requirements-client.txt").read_text(encoding="utf-8").splitlines():
        if "==" in line and not line.startswith("#"):
            name, expected = line.strip().split("==", 1)
            actual = importlib.metadata.version(name)
            assert actual == expected, f"{name}: expected {expected}, got {actual}"
    import numpy as np
    import torch
    import dspy
    from knowledge_storm.lm import LitellmModel
    import litellm
    from litellm.caching.caching import Cache
    litellm.cache = Cache(disk_cache_dir=str(root / "model-cache"), type="disk")
    from knowledge_storm.interface import Information
    from knowledge_storm.storm_wiki.modules.storm_dataclass import (
        DialogueTurn, StormInformationTable,
    )
    from sentence_transformers import SentenceTransformer

    model_path = root / "embedding-model"
    if args.prepare_embedding and not (model_path / "modules.json").exists():
        source = os.environ.get("STORM_EMBEDDING_SOURCE")
        options = ({} if source else {"revision": "c9a2bfebc254878aee8c3aca9e6844d5bbb102d1"})
        encoder = SentenceTransformer(
            source or "sentence-transformers/paraphrase-MiniLM-L6-v2",
            device="cpu", cache_folder=str(root / "hf-cache"), **options)
        encoder.save(str(model_path))
    encoder = SentenceTransformer(str(model_path), device="cpu", local_files_only=True)
    info = [
        Information(url="fixture:gpu", title="GPU", description="GPU allocation", snippets=["The model service uses two GPUs on one compute node."]),
        Information(url="fixture:client", title="Client", description="CPU client", snippets=["The application client needs one CPU and no GPU."]),
    ]
    table = StormInformationTable([("fixture", [DialogueTurn(search_results=info)])])
    # Only substitute the hardcoded download/device initialization. Retrieval
    # itself uses the real upstream STORM method with real CPU embeddings.
    table.encoder = encoder
    table.collected_urls = [item.url for item in info]
    table.collected_snippets = [item.snippets[0] for item in info]
    table.encoded_snippets = encoder.encode(table.collected_snippets)
    assert np.isfinite(table.encoded_snippets).all()
    hits = table.retrieve_information("How many GPUs does the model service use?", 1)
    assert hits and hits[0].url == "fixture:gpu", [x.url for x in hits]
    versions = {name: importlib.metadata.version(name) for name in (
        "knowledge-storm", "dspy-ai", "litellm", "openai", "sentence-transformers",
        "transformers", "numpy", "torch",
    )}
    report = {
        "schema_version": 1, "versions": versions, "device": str(encoder.device),
        "embedding_shape": list(table.encoded_snippets.shape), "semantic_retrieval_passed": True,
        "torch_path": torch.__file__, "dspy_path": dspy.__file__,
        "embedding_path": str(model_path), "storm_commit": "fb951af7744dab086e34962e9bc6fe878e145f83",
        "requirements_sha256": hashlib.sha256(Path(__file__).with_name("requirements-client.txt").read_bytes()).hexdigest(),
    }
    tmp = root / "ready.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / "ready.json")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
