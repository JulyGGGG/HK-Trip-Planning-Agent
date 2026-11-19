"""Gateway-only download of a pinned, public embedding snapshot (no GPU)."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
REVISION = 'c9a2bfebc254878aee8c3aca9e6844d5bbb102d1'
REPO = 'sentence-transformers/paraphrase-MiniLM-L6-v2'
FILES = ['1_Pooling/config.json', 'config.json', 'config_sentence_transformers.json',
         'model.safetensors', 'modules.json', 'sentence_bert_config.json',
         'special_tokens_map.json', 'tokenizer.json', 'tokenizer_config.json', 'vocab.txt']


def main():
    target = ROOT / 'data' / 'embedding-model'
    target.mkdir(parents=True, exist_ok=True)
    def fetch(name):
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        url = 'https://huggingface.co/' + REPO + '/resolve/' + REVISION + '/' + name
        tmp = path.with_suffix(path.suffix + '.part')
        if not path.exists():
            for attempt in range(3):
                try:
                    with urlopen(url, timeout=120) as response, tmp.open('wb') as out:
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            out.write(block)
                    tmp.replace(path)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(2)
        return {'path': name, 'bytes': path.stat().st_size,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'source': url}
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        records = list(pool.map(fetch, FILES))
    provenance = {'repository': REPO, 'revision': REVISION, 'downloaded_at': time.time(), 'files': records}
    (target / 'download-manifest.json').write_text(json.dumps(provenance, indent=2))
    print(json.dumps({'directory': str(target), 'revision': REVISION,
                      'total_bytes': sum(x['bytes'] for x in records)}))


if __name__ == '__main__':
    main()
