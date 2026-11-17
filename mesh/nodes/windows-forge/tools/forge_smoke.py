import json, sys, time
import requests

API = "http://127.0.0.1:8188"

def run(workflow_path, poll_deadline_s):
    wf = json.load(open(workflow_path, encoding="utf-8"))
    wf.pop("_meta", None)
    r = requests.post(API + "/prompt", json={"prompt": wf})
    if r.status_code != 200:
        print("SUBMIT_FAIL", r.status_code, r.text[:600])
        return 1
    pid = r.json()["prompt_id"]
    print("submitted", pid, "from", workflow_path)
    deadline = time.time() + poll_deadline_s
    meta = None
    while time.time() < deadline:
        time.sleep(5)
        h = requests.get(API + "/history/" + pid).json()
        if pid in h:
            out = h[pid]
            if out.get("status", {}).get("status_str") == "error":
                print("GEN_ERROR", json.dumps(out.get("status"))[:800])
                return 1
            for nid, o in out.get("outputs", {}).items():
                if "images" in o:
                    meta = o["images"][0]
            break
    if not meta:
        # text outputs (SaveText/TextGenerate lane)
        texts = []
        for nid, o in h.get(pid, {}).get("outputs", {}).items():
            if "text" in o:
                texts.extend(o["text"])
        if texts:
            txt = "\n".join(texts)
            out_path = r"C:\Users\morgi\AppData\Local\Temp\opencode\smoke_text_" + pid[:8] + ".txt"
            open(out_path, "w", encoding="utf-8").write(txt)
            print("OK text", len(txt), "chars ->", out_path)
            print(txt[:400])
            return 0
        print("POLL_TIMEOUT job still running - fetch later from /history/" + pid)
        return 2
    img = requests.get(API + "/view", params={"filename": meta["filename"],
                                              "subfolder": meta.get("subfolder", ""),
                                              "type": meta["type"]})
    out_path = r"C:\Users\morgi\AppData\Local\Temp\opencode\smoke_" + meta["filename"].replace("/", "_")
    open(out_path, "wb").write(img.content)
    print("OK", len(img.content), "bytes ->", out_path, "| comfy:", meta.get("subfolder", "") + meta["filename"])
    return 0

if __name__ == "__main__":
    sys.exit(run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 600))
