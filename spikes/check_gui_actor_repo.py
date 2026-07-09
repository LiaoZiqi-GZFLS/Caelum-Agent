"""Check GUI-Actor-3B HuggingFace repo and Microsoft GitHub repo availability."""
import json
import urllib.request
from huggingface_hub import HfApi


def main():
    api = HfApi()
    repo_id = "microsoft/GUI-Actor-3B-Qwen2.5-VL"
    exists = api.repo_exists(repo_id=repo_id)
    print(f"HuggingFace repo exists: {exists}")
    if exists:
        files = api.list_repo_files(repo_id)
        print(f"Total files: {len(files)}")
        print("Key files:")
        for f in files:
            if f.endswith(".safetensors") or f.endswith(".json") or f == "README.md":
                print(f"  - {f}")

    gh_url = "https://api.github.com/repos/microsoft/GUI-Actor/contents/src/gui_actor"
    with urllib.request.urlopen(gh_url, timeout=15) as resp:
        data = json.loads(resp.read())
    print("\nGUI-Actor src/gui_actor files:")
    for item in data:
        print(f"  - {item['name']}")


if __name__ == "__main__":
    main()
